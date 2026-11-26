#!/usr/bin/env python3
"""Audit /app/output/result.json against /input/case.json output_contract and extracted PDF evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

from pdf_common import normalize_for_match


def _collect_required_fields(case_data: Dict[str, Any]) -> List[str]:
    """Extract expected top-level output fields from case.json's output_contract/output_schema."""
    contract = (
        case_data.get("output_contract")
        or case_data.get("output_schema")
        or case_data.get("expected_output")
    )
    fields: List[str] = []
    if isinstance(contract, dict):
        if isinstance(contract.get("required_fields"), list):
            fields.extend(str(f) for f in contract["required_fields"])
        elif isinstance(contract.get("fields"), list):
            for item in contract["fields"]:
                if isinstance(item, str):
                    fields.append(item)
                elif isinstance(item, dict) and item.get("name"):
                    fields.append(str(item["name"]))
        elif isinstance(contract.get("properties"), dict):
            req = contract.get("required")
            if isinstance(req, list):
                fields.extend(str(f) for f in req)
            else:
                fields.extend(str(k) for k in contract["properties"].keys())
        else:
            # Treat non-meta keys as expected fields if contract is a flat template dict
            meta_keys = {"format", "description", "notes", "schema_version", "file", "filename"}
            for k in contract.keys():
                if k not in meta_keys:
                    fields.append(str(k))
    elif isinstance(contract, list):
        for item in contract:
            if isinstance(item, str):
                fields.append(item)
            elif isinstance(item, dict) and item.get("name"):
                fields.append(str(item["name"]))
    return list(dict.fromkeys(fields))


def _walk_evidence_items(obj: Any, path: str = "$") -> List[Dict[str, Any]]:
    """Find dict objects that declare page/block_id/quote provenance."""
    found: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        has_page = "page" in obj and isinstance(obj["page"], int)
        has_quote = any(k in obj and isinstance(obj[k], str) and obj[k].strip() for k in ("quote", "snippet", "excerpt", "evidence_text", "verbatim"))
        has_block = "block_id" in obj and isinstance(obj["block_id"], str)
        if has_page or has_quote or has_block:
            found.append({"path": path, "item": obj})
        for k, v in obj.items():
            found.extend(_walk_evidence_items(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            found.extend(_walk_evidence_items(v, f"{path}[{idx}]"))
    return found


def audit(
    result_path: Path,
    case_path: Path | None,
    doc_path: Path | None,
    ocr_path: Path | None = None,
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    evidence_checks: List[Dict[str, Any]] = []

    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        return {"passed": False, "errors": ["result.json root must be a JSON object"], "warnings": [], "evidence_checks": []}

    # Contract check
    if case_path and case_path.exists():
        case_data = json.loads(case_path.read_text(encoding="utf-8"))
        expected_cid = case_data.get("case_id")
        if expected_cid and result.get("case_id") != expected_cid:
            errors.append(f"case_id mismatch: expected {expected_cid!r}, got {result.get('case_id')!r}")
        for field in _collect_required_fields(case_data):
            if field not in result:
                errors.append(f"missing required output_contract field: {field!r}")
            elif result[field] is None or result[field] == "":
                warnings.append(f"required field {field!r} is empty/null (ensure uncertainty is documented if unreported)")

    # Evidence provenance check against document.json (+ optional ocr_results.json)
    page_count = 0
    page_texts: Dict[int, str] = {}
    block_map: Dict[str, Dict[str, Any]] = {}
    if doc_path and doc_path.exists():
        doc_data = json.loads(doc_path.read_text(encoding="utf-8"))
        page_count = int(doc_data.get("page_count") or 0)
        for blk in doc_data.get("blocks", []):
            p = int(blk["page"])
            page_texts[p] = page_texts.get(p, "") + "\n" + blk.get("raw_text", "") + "\n" + blk.get("text", "")
            block_map[blk["block_id"]] = blk
        for tbl in doc_data.get("tables", []):
            p = int(tbl["page"])
            tbl_flat = " ".join(" ".join(r) for r in tbl.get("rows", []))
            page_texts[p] = page_texts.get(p, "") + "\n" + tbl_flat

    if ocr_path and ocr_path.exists():
        ocr_data = json.loads(ocr_path.read_text(encoding="utf-8"))
        for item in ocr_data.get("rendered", []):
            p = item.get("page")
            ocr_txt = (item.get("ocr") or {}).get("text") or ""
            if isinstance(p, int) and ocr_txt.strip():
                page_texts[p] = page_texts.get(p, "") + "\n" + ocr_txt

    if page_texts or page_count > 0:
        norm_page_texts = {p: normalize_for_match(t) for p, t in page_texts.items()}

        for ev in _walk_evidence_items(result):
            item = ev["item"]
            p = item.get("page")
            bid = item.get("block_id")
            quote = next((item[k] for k in ("quote", "snippet", "excerpt", "evidence_text", "verbatim") if isinstance(item.get(k), str) and item[k].strip()), None)

            status = "ok"
            notes: List[str] = []
            if p is not None and page_count > 0 and not (1 <= p <= page_count):
                status = "error"
                notes.append(f"page {p} out of range [1, {page_count}]")
                errors.append(f"{ev['path']}: page {p} out of range [1, {page_count}]")

            if bid is not None:
                if bid not in block_map:
                    status = "warning" if status == "ok" else status
                    notes.append(f"block_id {bid!r} not found in document.json")
                    warnings.append(f"{ev['path']}: block_id {bid!r} not found in document.json")
                elif p is not None and block_map[bid]["page"] != p:
                    status = "error"
                    notes.append(f"block_id {bid!r} belongs to page {block_map[bid]['page']}, not {p}")
                    errors.append(f"{ev['path']}: block_id {bid!r} page mismatch")

            if quote:
                nq = normalize_for_match(quote)
                if p is not None and p in norm_page_texts:
                    if nq and nq not in norm_page_texts[p]:
                        status = "warning" if status == "ok" else status
                        notes.append(f"quote not matched verbatim on page {p} (check OCR/crop or partial quote)")
                        warnings.append(f"{ev['path']}: quote not matched verbatim on page {p}")
                else:
                    notes.append("no extracted page text to match quote against")

            evidence_checks.append({
                "path": ev["path"],
                "page": p,
                "block_id": bid,
                "status": status,
                "notes": notes,
            })

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "evidence_checked_count": len(evidence_checks),
        "evidence_checks": evidence_checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Harbor result.json against case.json and extracted document.json.")
    parser.add_argument("--result", required=True, help="Path to result.json.")
    parser.add_argument("--case", default=None, help="Optional path to /input/case.json.")
    parser.add_argument("--doc", default=None, help="Optional path to extracted document.json.")
    parser.add_argument("--ocr", default=None, help="Optional path to ocr_results.json.")
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 if any errors are found.")
    args = parser.parse_args()

    res_path = Path(args.result)
    if not res_path.exists():
        print(json.dumps({"passed": False, "errors": [f"result.json not found: {res_path}"]}), file=sys.stderr)
        return 2

    report = audit(
        res_path,
        Path(args.case) if args.case else None,
        Path(args.doc) if args.doc else None,
        Path(args.ocr) if args.ocr else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
