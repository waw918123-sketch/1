#!/usr/bin/env python3
"""One-shot translation pipeline preparer: discovers inputs, extracts census/units & glossary, shards compact batches, and initializes result.json."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.dont_write_bytecode = True

from glossary_tools import extract_glossary
from xml_pdf_extract import extract_document


def _infer_scope_from_task(case_data: Dict[str, Any], task_md_text: str) -> Dict[str, Any]:
    """Infer explicit scope constraints only when task/case restricts translation to specific figures/pages/sections."""
    scope: Dict[str, Any] = {}
    explicit_scope = case_data.get("scope") or case_data.get("target_scope") or {}
    if isinstance(explicit_scope, dict) and explicit_scope:
        scope.update(explicit_scope)

    combined = f"{json.dumps(case_data, ensure_ascii=False)}\n{task_md_text}"
    # Detect explicit figure/table ID restriction (e.g. Slide 13: 'only g001, g002' / '只翻 g001、g002')
    m_only_ids = re.search(
        r"(?:only\s+(?:translate\s+)?|只翻译?|仅翻译?)[^.\n。]{0,80}?((?:g\d+|fig\d+|tbl\d+|table\d+)(?:\s*[,、和与and\s]+(?:g\d+|fig\d+|tbl\d+|table\d+))*)",
        combined,
        re.I,
    )
    if m_only_ids and "target_ids" not in scope:
        ids = re.findall(r"\b(?:g\d+|fig\d+|tbl\d+|table\d+)\b", m_only_ids.group(1), re.I)
        if ids:
            scope["target_ids"] = list(dict.fromkeys(ids))

    # Detect page subset constraint (e.g. 'only translate pages 1-3')
    m_pages = re.search(r"(?:only\s+(?:translate\s+)?pages?|只翻译?第|仅翻译?第)\s*(\d+)\s*[-–到至]\s*(\d+)", combined, re.I)
    if m_pages and "pages" not in scope:
        a, b = int(m_pages.group(1)), int(m_pages.group(2))
        if 1 <= a <= b <= 200:
            scope["pages"] = list(range(a, b + 1))

    return scope


def _discover_inputs(input_dir: Path, case_data: Dict[str, Any]) -> Tuple[List[Path], List[Path]]:
    """Find primary XML/PDF documents and auxiliary glossary/text files."""
    primary_docs: List[Path] = []
    other_files: List[Path] = []
    if input_dir.exists():
        for p in sorted(input_dir.rglob("*")):
            if not p.is_file() or p.name in ("case.json", "task.md"):
                continue
            ext = p.suffix.lower()
            if ext in (".xml", ".nxml", ".jats", ".pdf"):
                primary_docs.append(p)
            else:
                other_files.append(p)
    # Prefer XML over PDF if both represent the same paper stem? Keep both listed cleanly unless duplicate
    return primary_docs, other_files


def _build_batches(units: List[Dict[str, Any]], max_chars: int = 9000) -> List[Dict[str, Any]]:
    """Partition translation units into context-safe batches <= max_chars (Slide 7/12)."""
    batches: List[Dict[str, Any]] = []
    cur_units: List[Dict[str, Any]] = []
    cur_chars = 0

    for u in units:
        usz = max(80, u.get("char_count", len(u["source_text"])))
        if cur_units and (cur_chars + usz > max_chars or len(cur_units) >= 24):
            batches.append({
                "batch_id": f"batch_{len(batches) + 1:03d}",
                "unit_count": len(cur_units),
                "total_chars": cur_chars,
                "units": cur_units,
            })
            cur_units = []
            cur_chars = 0
        cur_units.append({
            "unit_id": u["unit_id"],
            "locator": u["locator"],
            "role": u.get("role"),
            "section": u.get("section"),
            "source_text": u["source_text"],
            "sentences": [
                {"sentence_id": s["sentence_id"], "locator": s["locator"], "source_text": s["source_text"]}
                for s in u.get("sentences", [])
            ],
        })
        cur_chars += usz

    if cur_units:
        batches.append({
            "batch_id": f"batch_{len(batches) + 1:03d}",
            "unit_count": len(cur_units),
            "total_chars": cur_chars,
            "units": cur_units,
        })
    return batches


def _collect_contract_keys(contract: Any) -> List[str]:
    fields: List[str] = []
    if isinstance(contract, dict):
        if isinstance(contract.get("required_fields"), list):
            fields.extend(str(f) for f in contract["required_fields"])
        elif isinstance(contract.get("fields"), list):
            for it in contract["fields"]:
                fields.append(str(it["name"] if isinstance(it, dict) and it.get("name") else it))
        elif isinstance(contract.get("properties"), dict):
            req = contract.get("required")
            fields.extend(str(f) for f in (req if isinstance(req, list) else contract["properties"].keys()))
        elif isinstance(contract.get("result_schema"), dict) and isinstance(contract["result_schema"].get("required"), list):
            fields.extend(str(f) for f in contract["result_schema"]["required"])
    elif isinstance(contract, list):
        for it in contract:
            fields.append(str(it["name"] if isinstance(it, dict) and it.get("name") else it))
    return list(dict.fromkeys(fields))


def _build_initial_result(
    case_data: Dict[str, Any],
    primary_doc: Dict[str, Any],
    glossary: Dict[str, Any],
) -> Dict[str, Any]:
    """Immediately materialize a non-empty baseline result.json with exact census locators & locked glossary."""
    contract = case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output")
    req_fields = _collect_contract_keys(contract)
    units = primary_doc.get("units", [])
    status = "blocked" if primary_doc.get("status") in ("encrypted_blocked", "failed_missing_parser") else "partial"

    base_translations = [
        {
            "unit_id": u["unit_id"],
            "locator": u["locator"],
            "role": u.get("role"),
            "source_text": u["source_text"],
            "translation": None,
            "sentences": [
                {
                    "sentence_id": s["sentence_id"],
                    "locator": s["locator"],
                    "source_text": s["source_text"],
                    "translation": None,
                }
                for s in u.get("sentences", [])
            ],
        }
        for u in units
    ]

    uncertainty: List[str] = []
    if primary_doc.get("scanned_pages"):
        uncertainty.append(f"Scanned/low-text PDF pages detected (require visual/OCR verification): {primary_doc['scanned_pages']}")
    unr = [u["unit_id"] for u in units if u.get("has_unreadable")]
    if unr:
        uncertainty.append(f"Encoding defects detected in units: {unr[:10]}")

    res: Dict[str, Any] = {
        "case_id": case_data.get("case_id"),
        "status": status,
        "glossary": glossary.get("locked_terms", []),
        "candidate_terms": glossary.get("candidate_terms", []),
        "census": primary_doc.get("census", {}),
        "translations": base_translations,
        "human_review_items": [],
        "sources": [
            {"locator": u["locator"], "unit_id": u["unit_id"], "quote": u["source_text"][:200]}
            for u in units[:3]
        ],
        "uncertainty": "; ".join(uncertainty) if uncertainty else "Initial extraction complete; pending batch translation merge.",
        "change_records": [
            {"stage": "prep_init", "detail": f"Initialized {len(units)} units ({primary_doc.get('census', {}).get('in_scope_sentence_count', 0)} sentences) with {len(glossary.get('locked_terms', []))} locked glossary terms."}
        ],
    }

    for k in ("bilingual_segments", "paragraphs", "segments", "translated_segments"):
        if k in req_fields:
            res[k] = res["translations"]
    if "terminology" in req_fields or "glossary_table" in req_fields:
        for tk in ("terminology", "glossary_table"):
            if tk in req_fields:
                res[tk] = res["glossary"]
    for f in req_fields:
        if f not in res:
            res[f] = None
    return res


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare XML/PDF translation batches, locked glossary, census, and baseline result.json.")
    parser.add_argument("--input-dir", default="/input")
    parser.add_argument("--work-dir", default="/app/work")
    parser.add_argument("--max-chars", type=int, default=9000, help="Max source chars per translation batch (default: 9000).")
    parser.add_argument("--init-result", default=None, help="Optional output path for immediate baseline result.json.")
    args = parser.parse_args()

    in_dir = Path(args.input_dir)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    case_path = in_dir / "case.json"
    task_path = in_dir / "task.md"
    case_data = json.loads(case_path.read_text(encoding="utf-8")) if case_path.exists() else {}
    task_txt = task_path.read_text(encoding="utf-8", errors="replace") if task_path.exists() else ""

    scope = _infer_scope_from_task(case_data, task_txt)
    primary_docs, other_files = _discover_inputs(in_dir, case_data)

    # Prefer XML when both XML and PDF are present unless multiple distinct papers exist
    doc_results: List[Dict[str, Any]] = []
    for idx, doc_p in enumerate(primary_docs):
        doc_res = extract_document(doc_p, scope)
        doc_json_p = work_dir / f"doc_{idx + 1}_{doc_p.stem}_units.json"
        doc_json_p.write_text(json.dumps(doc_res, ensure_ascii=False, indent=2), encoding="utf-8")
        doc_res["_saved_path"] = str(doc_json_p)
        doc_results.append(doc_res)

    primary = next((d for d in doc_results if d.get("format") == "xml" and d.get("status") == "ok"), doc_results[0] if doc_results else {"status": "no_primary_document", "units": [], "all_units_context": [], "census": {}})

    full_src = "\n".join(u.get("source_text", "") for u in primary.get("all_units_context", primary.get("units", [])))
    glossary = extract_glossary(case_data, task_txt, other_files, full_src)
    glossary_p = work_dir / "glossary.json"
    glossary_p.write_text(json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")

    batches = _build_batches(primary.get("units", []), max_chars=args.max_chars)
    batch_dir = work_dir / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    batch_index: List[Dict[str, Any]] = []
    for b in batches:
        bp = batch_dir / f"{b['batch_id']}.json"
        bp.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
        batch_index.append({"batch_id": b["batch_id"], "path": str(bp), "unit_count": b["unit_count"], "total_chars": b["total_chars"]})

    manifest: Dict[str, Any] = {
        "case_id": case_data.get("case_id"),
        "task_md_preview": task_txt[:900] if task_txt else None,
        "output_contract": case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output"),
        "primary_file": primary.get("file"),
        "primary_format": primary.get("format"),
        "doc_units_path": primary.get("_saved_path"),
        "scope_inferred": scope,
        "census": primary.get("census", {}),
        "glossary_path": str(glossary_p),
        "locked_terms": glossary.get("locked_terms", [])[:30],
        "context_disambiguations": glossary.get("context_disambiguations", []),
        "batch_count": len(batch_index),
        "batches": batch_index,
        "other_files": [str(p) for p in other_files],
    }
    if args.init_result:
        init_p = Path(args.init_result)
        init_p.parent.mkdir(parents=True, exist_ok=True)
        init_obj = _build_initial_result(case_data, primary, glossary)
        init_p.write_text(json.dumps(init_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["initial_result_path"] = str(init_p)

    man_p = work_dir / "manifest.json"
    man_txt = json.dumps(manifest, ensure_ascii=False, indent=2)
    man_p.write_text(man_txt, encoding="utf-8")
    if len(man_txt) > 24000:
        slim = dict(manifest)
        slim["batches"] = batch_index[:25]
        slim["truncated"] = True
        slim["hint"] = f"Full manifest saved at {man_p}"
        man_txt = json.dumps(slim, ensure_ascii=False, indent=2)
    print(man_txt[:24000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
