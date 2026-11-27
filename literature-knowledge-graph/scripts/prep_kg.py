#!/usr/bin/env python3
"""Stage 1 一键入口：识别多篇语料与既有基线图谱、执行定向 OCR、生成紧凑 kg_brief.json 并锁定基线 result.json。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.dont_write_bytecode = True

from corpus_ingest import ingest_corpus
from doc_common import collect_contract_keys
from pdf_ocr import render_and_ocr


def _extract_task_queries(case_data: Dict[str, Any], task_text: str) -> List[str]:
    """从 case.json 或 task.md 提取题面指定的图谱查询问题。"""
    found: List[str] = []
    for k in ("queries", "questions", "target_queries", "graph_queries"):
        val = case_data.get(k)
        if isinstance(val, list):
            for it in val:
                if isinstance(it, str) and it.strip():
                    found.append(it.strip())
                elif isinstance(it, dict):
                    q = it.get("question") or it.get("query") or it.get("prompt")
                    if isinstance(q, str) and q.strip():
                        found.append(q.strip())
    if task_text:
        for line in task_text.splitlines():
            s = line.strip()
            if re.match(r"^(?:Q\d+|Query\s*\d+|问题\s*\d+|查询\s*\d+)[:：]", s, re.I):
                found.append(s)
    return list(dict.fromkeys(found))


import re


def _load_base_graph(base_graph_path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if not base_graph_path or not base_graph_path.exists():
        return None
    obj = json.loads(base_graph_path.read_text(encoding="utf-8"))
    if isinstance(obj.get("knowledge_graph"), dict):
        obj = obj["knowledge_graph"]
    nodes = list(obj.get("nodes") or [])
    edges = list(obj.get("edges") or [])
    return {
        "path": str(base_graph_path),
        "nodes": nodes,
        "edges": edges,
        "preserved_node_ids": [str(n.get("node_id") or n.get("id")) for n in nodes if (n.get("node_id") or n.get("id"))],
        "preserved_edge_ids": [str(e.get("edge_id") or e.get("id")) for e in edges if (e.get("edge_id") or e.get("id"))],
    }


def _build_initial_result(
    case_data: Dict[str, Any],
    corpus_info: Dict[str, Any],
    base_graph: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    contract = case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output")
    req_fields = collect_contract_keys(contract)

    papers_summ = corpus_info.get("papers", [])
    all_blocked = bool(papers_summ) and all(p.get("status") == "blocked" for p in papers_summ)

    papers_meta: Dict[str, Any] = {}
    sources: List[Dict[str, Any]] = []
    uncertainty: List[str] = []
    human_review: List[Dict[str, Any]] = []
    unavailable: List[Dict[str, Any]] = []

    for p in papers_summ:
        sid = p["source_id"]
        if p.get("status") == "blocked":
            uncertainty.append(f"Blocked input {Path(p['file']).name} (source_id={sid}): encrypted or invalid header.")
            human_review.append({
                "source_id": sid,
                "item": f"Unreadable input {Path(p['file']).name}",
                "reason": "Requires password or has corrupted container.",
                "verification_mode": "human_review_required",
            })
            continue

        doc_obj = json.loads(Path(p["document_json_path"]).read_text(encoding="utf-8"))
        cues_obj = json.loads(Path(p["cues_path"]).read_text(encoding="utf-8"))
        fdiffs = json.loads(Path(p["formula_diffs_path"]).read_text(encoding="utf-8"))
        meta = dict(doc_obj.get("metadata") or {})
        meta["source_id"] = sid
        meta["has_pdf_pages"] = bool(doc_obj.get("has_pdf_pages"))
        meta["locator_mode"] = doc_obj.get("locator_mode")
        meta["companion_formats"] = doc_obj.get("companion_formats", [])
        if sid != (papers_summ[0]["source_id"] if papers_summ else sid):
            meta["metadata_isolation_note"] = "Independent paper metadata; root case.json DOI/title/authors NOT inherited."
        papers_meta[sid] = meta

        sources.append({
            "source_id": sid,
            "file": p["file"],
            "format": p.get("format"),
            "companion_formats": doc_obj.get("companion_formats", []),
            "has_pdf_pages": bool(doc_obj.get("has_pdf_pages")),
            "locator_mode": doc_obj.get("locator_mode"),
            "doi": meta.get("doi"),
            "title": meta.get("title"),
            "license": meta.get("license"),
            "file_sha256": meta.get("file_sha256"),
        })

        if doc_obj.get("page_numbering_discrepancy"):
            uncertainty.append(f"Printed vs physical PDF page numbering discrepancy in {sid} ({Path(p['file']).name}).")
        if not doc_obj.get("has_pdf_pages"):
            uncertainty.append(f"{sid}: No original PDF pages in this package (locator_mode={doc_obj.get('locator_mode')}); cite source_id + paragraph_id only.")
        if fdiffs:
            uncertainty.append(f"{sid}: Detected {len(fdiffs)} linearized inline formula/sub/superscript differences against companion XML; check formula_diffs.json before asserting numeric/chemical values.")
        for uref in cues_obj.get("unavailable_material_refs", [])[:6]:
            unavailable.append(uref)
        for reg in doc_obj.get("unextracted_regions", [])[:6]:
            reason = reg.get("reason")
            uncertainty.append(f"Defect/anomaly in {sid} p.{reg.get('page')} {reg.get('block_id')} ({reason})")
            human_review.append({
                "source_id": sid, "page": reg.get("page"), "block_id": reg.get("block_id"),
                "reason": f"{reason}: {reg.get('defects', {}).get('stat_glyph_anomalies') or 'encoding defect'}",
                "verification_mode": "human_review_required",
            })

    if unavailable:
        uncertainty.append("Input does not supply referenced figure pixels, supplementary files, or raw clinical/experimental data; missing values must NOT be inferred.")

    init_nodes = list(base_graph["nodes"]) if base_graph else []
    init_edges = list(base_graph["edges"]) if base_graph else []
    inc_block = {
        "enabled": bool(base_graph),
        "base_graph_path": base_graph["path"] if base_graph else None,
        "base_graph_preserved_ids": {
            "node_ids": base_graph["preserved_node_ids"] if base_graph else [],
            "edge_ids": base_graph["preserved_edge_ids"] if base_graph else [],
        },
        "added_nodes": [],
        "merged_nodes": [],
        "added_edges": [],
        "conflict_edges": [],
        "rejected_merges": [],
        "id_stability_verified": True if base_graph else None,
    }

    res: Dict[str, Any] = {
        "case_id": case_data.get("case_id"),
        "status": "blocked" if all_blocked else "partial",
        "papers": papers_meta,
        "nodes": init_nodes,
        "edges": init_edges,
        "entity_alignment": {
            "merged_candidates": [],
            "non_merge_records": [],
        },
        "conflicts": {
            "intra_source_conflicts": [],
            "cross_paper_conflicts": [],
        },
        "queries": [],
        "incremental_update": inc_block,
        "sources": sources,
        "unavailable_materials": unavailable,
        "uncertainty": uncertainty if uncertainty else ["Initial deterministic corpus ingestion completed; awaiting KG patch merge, executed queries, and strict audit."],
        "human_review_items": human_review,
        "change_records": [
            {
                "stage": "prep_kg_init",
                "detail": f"Ingested {len(papers_summ)} canonical paper(s) (deduped {len(corpus_info.get('same_paper_format_dedups', []))} format variant(s)) and locked verified paper metadata.",
            }
        ],
    }
    for f in req_fields:
        if f not in res:
            res[f] = [] if f in ("entities", "relations", "triples", "graph_queries", "query_results") else None
    return res


def run_prep(
    input_dir: Path,
    work_dir: Path,
    explicit_files: Optional[List[Path]] = None,
    base_graph_path: Optional[Path] = None,
    max_chunk_chars: int = 8000,
    max_ocr_pages: int = 4,
    max_crops: int = 4,
    init_result: Optional[Path] = None,
) -> Dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    case_path = input_dir / "case.json"
    task_path = input_dir / "task.md"
    case_data: Dict[str, Any] = {}
    if case_path.exists():
        try:
            case_data = json.loads(case_path.read_text(encoding="utf-8"))
        except Exception as exc:
            case_data = {"parse_error": str(exc)}
    task_text = task_path.read_text(encoding="utf-8", errors="replace") if task_path.exists() else ""

    corpus_info = ingest_corpus(
        input_dir=input_dir,
        work_dir=work_dir,
        explicit_files=explicit_files,
        case_data=case_data,
        base_graph_path=base_graph_path,
        max_chunk_chars=max_chunk_chars,
    )

    # 仅针对 PDF 触发定向 OCR/截图
    for p in corpus_info.get("papers", []):
        if p.get("status") == "ok" and p.get("format") == "pdf":
            summ = p.get("probe_summary") or {}
            need_pages = sorted(set((summ.get("scanned_pages") or []) + (summ.get("garbled_pages") or [])))[:max_ocr_pages]
            doc_obj = json.loads(Path(p["document_json_path"]).read_text(encoding="utf-8"))
            anomaly_crops = [
                f"{u['page']}:{u['bbox'][0]},{u['bbox'][1]},{u['bbox'][2]},{u['bbox'][3]}"
                for u in doc_obj.get("unextracted_regions", [])
                if u.get("reason") == "stat_glyph_anomaly" and u.get("bbox") and any(float(x) > 0 for x in u["bbox"])
            ][:max_crops]
            if need_pages or anomaly_crops:
                p_dir = Path(p["document_json_path"]).parent
                ocr_res = render_and_ocr(
                    Path(p["file"]),
                    p_dir / "ocr",
                    page_spec=",".join(str(pg) for pg in need_pages) if need_pages else None,
                    dpi=250,
                    crops=anomaly_crops,
                    ocr_mode="auto",
                )
                p["ocr_results_path"] = str(p_dir / "ocr" / "ocr_results.json")
                p["ocr_engine_used"] = ocr_res.get("ocr_engine_used")
                p["ocr_rendered_count"] = ocr_res.get("rendered_count", 0)

    resolved_bg = Path(corpus_info["base_graph_path"]) if corpus_info.get("base_graph_path") else None
    base_graph = _load_base_graph(resolved_bg)
    if base_graph:
        (work_dir / "base_graph_locked.json").write_text(json.dumps(base_graph, ensure_ascii=False, indent=2), encoding="utf-8")

    contract = case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output")
    brief = {
        "case_id": case_data.get("case_id"),
        "has_case_json": case_path.exists(),
        "has_task_md": task_path.exists(),
        "task_md_preview": task_text[:1000] if task_text else None,
        "task_queries_detected": _extract_task_queries(case_data, task_text),
        "output_contract": contract,
        "required_fields": collect_contract_keys(contract),
        "manifest_file": corpus_info.get("manifest_file"),
        "manifest_hash_checks": corpus_info.get("manifest_hash_checks"),
        "material_index_audit": corpus_info.get("material_index_audit"),
        "root_metadata_isolated_to": corpus_info.get("root_metadata_isolated_to"),
        "same_paper_format_dedups": corpus_info.get("same_paper_format_dedups"),
        "incremental_mode": {
            "enabled": bool(base_graph),
            "base_graph_path": base_graph["path"] if base_graph else None,
            "existing_node_count": len(base_graph["nodes"]) if base_graph else 0,
            "existing_edge_count": len(base_graph["edges"]) if base_graph else 0,
        },
        "paper_count": corpus_info.get("paper_count"),
        "papers": [
            {k: v for k, v in p.items() if k != "chunk_paths"} | {"chunk_count": len(p.get("chunk_paths", []))}
            for p in corpus_info.get("papers", [])
        ],
    }

    if init_result is not None:
        init_result.parent.mkdir(parents=True, exist_ok=True)
        init_obj = _build_initial_result(case_data, corpus_info, base_graph)
        init_result.write_text(json.dumps(init_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        brief["initial_result_path"] = str(init_result)

    (work_dir / "kg_brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    return brief


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Literature Knowledge Graph workspace and locked baseline result.json.")
    parser.add_argument("--input-dir", default="/input")
    parser.add_argument("--work-dir", default="/app/work")
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--base-graph", default=None)
    parser.add_argument("--max-chunk-chars", type=int, default=8000)
    parser.add_argument("--max-ocr-pages", type=int, default=4)
    parser.add_argument("--max-crops", type=int, default=4)
    parser.add_argument("--init-result", default=None)
    args = parser.parse_args()

    brief = run_prep(
        Path(args.input_dir),
        Path(args.work_dir),
        explicit_files=[Path(f) for f in args.file],
        base_graph_path=Path(args.base_graph) if args.base_graph else None,
        max_chunk_chars=args.max_chunk_chars,
        max_ocr_pages=args.max_ocr_pages,
        max_crops=args.max_crops,
        init_result=Path(args.init_result) if args.init_result else None,
    )
    print(json.dumps(brief, ensure_ascii=False, indent=2)[:16000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
