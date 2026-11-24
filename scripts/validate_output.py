#!/usr/bin/env python3
"""
Harbor Output Contract & Provenance Validator for scientific-pdf-parser.

Verifies:
1. result.json is valid UTF-8 JSON and preserves case_id from case.json.
2. Every required key declared in case.json's output_contract is present.
3. No unresolved placeholder strings (TBD, [XX], TODO, etc.) appear in outputs.
4. Every cited pdf_page, block_id, table_id, and eq_id exists in parsed document.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

PLACEHOLDER_RE = re.compile(r"\b(?:TBD|TODO|FIXME)\b|\[(?:XX|INSERT|PLACEHOLDER)\]", re.IGNORECASE)


def collect_required_keys(output_contract: Any) -> List[str]:
    if isinstance(output_contract, dict):
        if "required" in output_contract and isinstance(output_contract["required"], list):
            return [str(k) for k in output_contract["required"]]
        if "fields" in output_contract and isinstance(output_contract["fields"], dict):
            return list(output_contract["fields"].keys())
        return [k for k in output_contract.keys() if k not in ("description", "type", "notes")]
    if isinstance(output_contract, list):
        return [str(k) for k in output_contract]
    return []


def walk_check(
    obj: Any,
    path: str,
    valid_pages: Set[int],
    valid_blocks: Set[str],
    valid_tables: Set[str],
    valid_eqs: Set[str],
    errors: List[str],
    warnings: List[str],
) -> None:
    if isinstance(obj, dict):
        if "pdf_page" in obj and obj["pdf_page"] is not None:
            try:
                p = int(obj["pdf_page"])
                if valid_pages and p not in valid_pages:
                    errors.append(f"{path}.pdf_page={p} out of range (valid: 1..{max(valid_pages)}).")
            except Exception:
                errors.append(f"{path}.pdf_page is not an integer: {obj['pdf_page']!r}")
        if "block_id" in obj and obj["block_id"]:
            bid = str(obj["block_id"])
            if valid_blocks and bid not in valid_blocks:
                errors.append(f"{path}.block_id={bid!r} not found in document.json.")
        if "table_id" in obj and obj["table_id"]:
            tid = str(obj["table_id"])
            if valid_tables and tid not in valid_tables:
                errors.append(f"{path}.table_id={tid!r} not found in document.json.")
        if "eq_id" in obj and obj["eq_id"]:
            eid = str(obj["eq_id"])
            if valid_eqs and eid not in valid_eqs:
                errors.append(f"{path}.eq_id={eid!r} not found in document.json.")
        for k, v in obj.items():
            walk_check(v, f"{path}.{k}", valid_pages, valid_blocks, valid_tables, valid_eqs, errors, warnings)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            walk_check(item, f"{path}[{i}]", valid_pages, valid_blocks, valid_tables, valid_eqs, errors, warnings)
    elif isinstance(obj, str):
        if PLACEHOLDER_RE.search(obj):
            warnings.append(f"{path} contains possible placeholder text: {obj[:80]!r}")


def validate(case_path: Optional[Path], parsed_path: Optional[Path], result_path: Path) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    if not result_path.exists():
        return {"valid": False, "errors": [f"result.json not found: {result_path}"], "warnings": []}

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"valid": False, "errors": [f"Invalid JSON in {result_path}: {exc}"], "warnings": []}

    if not isinstance(result, dict):
        return {"valid": False, "errors": ["result.json top-level value must be a JSON object."], "warnings": []}

    # Check case.json contract
    if case_path and case_path.exists():
        case_data = json.loads(case_path.read_text(encoding="utf-8"))
        expected_case_id = case_data.get("case_id")
        if expected_case_id is not None and result.get("case_id") != expected_case_id:
            errors.append(
                f"case_id mismatch: expected {expected_case_id!r}, got {result.get('case_id')!r}."
            )
        req_keys = collect_required_keys(case_data.get("output_contract"))
        for k in req_keys:
            if k not in result:
                errors.append(f"Missing required output_contract key: {k!r}.")
    else:
        if "case_id" not in result:
            warnings.append("No case.json provided and result.json has no 'case_id' field.")

    if "status" not in result:
        warnings.append("result.json does not include a top-level 'status' field.")

    # Load parsed document.json provenance index
    valid_pages: Set[int] = set()
    valid_blocks: Set[str] = set()
    valid_tables: Set[str] = set()
    valid_eqs: Set[str] = set()
    if parsed_path and parsed_path.exists():
        doc = json.loads(parsed_path.read_text(encoding="utf-8"))
        num_pages = int(doc.get("metadata", {}).get("num_pages", 0))
        valid_pages = set(range(1, num_pages + 1))
        valid_blocks = {b["block_id"] for b in doc.get("blocks", []) if "block_id" in b}
        valid_tables = {t["table_id"] for t in doc.get("tables", []) if "table_id" in t}
        valid_eqs = {e["eq_id"] for e in doc.get("equations", []) if "eq_id" in e}

    walk_check(result, "$", valid_pages, valid_blocks, valid_tables, valid_eqs, errors, warnings)
    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Harbor result.json against case.json and document.json.")
    parser.add_argument("--case", type=Path, default=None, help="Path to /input/case.json")
    parser.add_argument("--parsed", type=Path, default=None, help="Path to parsed document.json")
    parser.add_argument("--result", type=Path, required=True, help="Path to /app/output/result.json")
    args = parser.parse_args()

    report = validate(args.case, args.parsed, args.result)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
