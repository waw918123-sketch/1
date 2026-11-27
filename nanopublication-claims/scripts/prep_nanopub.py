#!/usr/bin/env python3
"""One-shot Nanopublication preparer: discovers inputs, ingests docs, runs targeted OCR/crops, writes compact brief & locked baseline result.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

sys.dont_write_bytecode = True

from doc_common import collect_contract_keys
from doc_ingest import ingest_document
from pdf_ocr import render_and_ocr

SUPPORTED_EXTS = {
    ".pdf", ".xml", ".nxml", ".jats", ".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".docx", ".pptx", ".xlsx",
}


def _discover_case_paths(obj: Any, base_dir: Path) -> List[Path]:
    found: List[Path] = []
    seen: Set[str] = set()

    def _walk(val: Any) -> None:
        if isinstance(val, str):
            s = val.strip()
            if not s or len(s) > 300 or "\n" in s:
                return
            p = Path(s)
            if p.suffix.lower() not in SUPPORTED_EXTS:
                return
            for cand in ([p] if p.is_absolute() else [base_dir / p, base_dir.parent / p]):
                try:
                    if cand.exists() and cand.is_file():
                        rp = str(cand.resolve())
                        if rp not in seen:
                            seen.add(rp)
                            found.append(cand)
                        break
                except OSError:
                    continue
        elif isinstance(val, list):
            for item in val:
                _walk(item)
        elif isinstance(val, dict):
            for k, item in val.items():
                if k in ("output_contract", "output_schema", "expected_output", "result_schema"):
                    continue
                _walk(item)

    _walk(obj)
    return found


def _build_initial_result(case_data: Dict[str, Any], docs_info: List[Dict[str, Any]]) -> Dict[str, Any]:
    contract = case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output")
    req_fields = collect_contract_keys(contract)

    all_blocked = bool(docs_info) and all(d.get("status") == "blocked" for d in docs_info)
    pubinfo_map: Dict[str, Any] = {}
    sources: List[Dict[str, Any]] = []
    uncertainty: List[str] = []
    human_review: List[Dict[str, Any]] = []

    for d in docs_info:
        did = d["doc_id"]
        if d.get("status") == "blocked":
            uncertainty.append(f"Blocked input {Path(d['file']).name}: {d.get('probe', {}).get('summary', {}).get('overall_status')}")
            human_review.append({
                "doc_id": did,
                "item": f"Unreadable/encrypted document {Path(d['file']).name}",
                "reason": "Document requires password or has invalid header.",
                "verification_mode": "human_review_required",
            })
            continue
        doc_path = Path(d["document_json_path"])
        if not doc_path.exists():
            continue
        doc_obj = json.loads(doc_path.read_text(encoding="utf-8"))
        meta = dict(doc_obj.get("metadata") or {})
        meta["doc_id"] = did
        pubinfo_map[did] = {
            k: meta.get(k, "unreported")
            for k in ("doc_id", "title", "authors", "year", "journal", "doi", "pmid", "version", "license", "source_file", "file_sha256")
        }
        for b in doc_obj.get("blocks", [])[:2]:
            sources.append({"doc_id": did, "file": d["file"], "page": b["page"], "block_id": b["block_id"], "quote": b["raw_text"][:200]})
        if doc_obj.get("page_numbering_discrepancy"):
            uncertainty.append(f"Printed vs physical page numbering discrepancy in {Path(d['file']).name}.")
        for reg in doc_obj.get("unextracted_regions", [])[:6]:
            reason = reg.get("reason")
            uncertainty.append(f"Defect/anomaly in {did} p.{reg.get('page')} {reg.get('block_id')} ({reason})")
            human_review.append({
                "doc_id": did, "page": reg.get("page"), "block_id": reg.get("block_id"),
                "reason": f"{reason}: {reg.get('defects', {}).get('stat_glyph_anomalies') or 'encoding defect'}",
                "verification_mode": "human_review_required",
            })

    res: Dict[str, Any] = {
        "case_id": case_data.get("case_id"),
        "status": "blocked" if all_blocked else "partial",
        "pubinfo": pubinfo_map,
        "nanopublications": [],
        "conflicts": [],
        "human_review_items": human_review,
        "sources": sources,
        "uncertainty": uncertainty if uncertainty else ["Initial deterministic ingestion completed; awaiting atomic Nanopublication patch merge and strict audit."],
        "change_records": [
            {"stage": "prep_nanopub_init", "detail": f"Ingested {len(docs_info)} document(s) and locked verified pubinfo metadata."}
        ],
    }
    for f in req_fields:
        if f not in res:
            res[f] = [] if f in ("assertions", "claims", "answers", "nanopublications", "conflicts", "sources", "human_review_items") else None
    return res


def run_prep(
    input_dir: Path,
    work_dir: Path,
    explicit_files: Optional[List[Path]] = None,
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

    declared = _discover_case_paths(case_data, input_dir) if case_data else []
    files: List[Path] = [p for p in (explicit_files or []) if p.exists()]
    if not files:
        seen: Set[str] = set()
        cands = (
            [p for p in sorted(input_dir.rglob("*")) if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS and p.name not in ("case.json", "task.md")]
            if input_dir.exists()
            else []
        ) + declared
        for p in cands:
            rp = str(p.resolve())
            if rp not in seen:
                seen.add(rp)
                files.append(p)

    prio = {".xml": 0, ".nxml": 0, ".jats": 0, ".pdf": 1}
    files.sort(key=lambda p: (prio.get(p.suffix.lower(), 2), p.name.lower()))

    docs_info: List[Dict[str, Any]] = []
    for idx, fpath in enumerate(files):
        doc_id = f"doc_{idx + 1}"
        slug = f"{doc_id}_{fpath.stem}" if len(files) > 1 else "doc_1"
        doc_dir = work_dir / slug
        info = ingest_document(fpath, doc_dir, doc_id=doc_id, max_chunk_chars=max_chunk_chars)

        if info.get("status") == "ok" and fpath.suffix.lower() == ".pdf":
            summ = info.get("probe_summary") or {}
            need_pages = sorted(set((summ.get("scanned_pages") or []) + (summ.get("garbled_pages") or [])))[:max_ocr_pages]
            doc_obj = json.loads(Path(info["document_json_path"]).read_text(encoding="utf-8"))
            anomaly_crops = [
                f"{u['page']}:{u['bbox'][0]},{u['bbox'][1]},{u['bbox'][2]},{u['bbox'][3]}"
                for u in doc_obj.get("unextracted_regions", [])
                if u.get("reason") == "stat_glyph_anomaly" and u.get("bbox") and any(float(x) > 0 for x in u["bbox"])
            ][:max_crops]
            if need_pages or anomaly_crops:
                ocr_res = render_and_ocr(
                    fpath,
                    doc_dir / "ocr",
                    page_spec=",".join(str(p) for p in need_pages) if need_pages else None,
                    dpi=250,
                    crops=anomaly_crops,
                    ocr_mode="auto",
                )
                info["ocr_results_path"] = str(doc_dir / "ocr" / "ocr_results.json")
                info["ocr_engine_used"] = ocr_res.get("ocr_engine_used")
                info["ocr_rendered_count"] = ocr_res.get("rendered_count", 0)
        docs_info.append(info)

    contract = case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output")
    brief = {
        "case_id": case_data.get("case_id"),
        "has_case_json": case_path.exists(),
        "has_task_md": task_path.exists(),
        "task_md_preview": task_path.read_text(encoding="utf-8", errors="replace")[:1000] if task_path.exists() else None,
        "output_contract": contract,
        "required_fields": collect_contract_keys(contract),
        "document_count": len(docs_info),
        "documents": [
            {k: v for k, v in d.items() if k != "chunk_paths"} | {"chunk_count": len(d.get("chunk_paths", []))}
            for d in docs_info
        ],
    }

    if init_result is not None:
        init_result.parent.mkdir(parents=True, exist_ok=True)
        init_obj = _build_initial_result(case_data, docs_info)
        init_result.write_text(json.dumps(init_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        brief["initial_result_path"] = str(init_result)

    (work_dir / "nanopub_brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    return brief


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Nanopublication workspace and locked baseline result.json.")
    parser.add_argument("--input-dir", default="/input")
    parser.add_argument("--work-dir", default="/app/work")
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--max-chunk-chars", type=int, default=8000)
    parser.add_argument("--max-ocr-pages", type=int, default=4)
    parser.add_argument("--max-crops", type=int, default=4)
    parser.add_argument("--init-result", default=None)
    args = parser.parse_args()

    brief = run_prep(
        Path(args.input_dir),
        Path(args.work_dir),
        explicit_files=[Path(f) for f in args.file],
        max_chunk_chars=args.max_chunk_chars,
        max_ocr_pages=args.max_ocr_pages,
        max_crops=args.max_crops,
        init_result=Path(args.init_result) if args.init_result else None,
    )
    print(json.dumps(brief, ensure_ascii=False, indent=2)[:16000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
