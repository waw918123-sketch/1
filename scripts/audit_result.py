#!/usr/bin/env python3
"""Audit /app/output/result.json against /input/case.json output_contract and extracted PDF evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

sys.dont_write_bytecode = True

from pdf_common import normalize_for_match


def _schema_type_allows(type_spec: Any, expected: str) -> bool:
    """Safely inspect JSON Schema type strings or union lists such as ['string', 'null'] without TypeError."""
    if isinstance(type_spec, str):
        return type_spec == expected
    if isinstance(type_spec, list):
        return expected in type_spec
    return False


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
        elif isinstance(contract.get("result_schema"), dict) and isinstance(contract["result_schema"].get("required"), list):
            fields.extend(str(f) for f in contract["result_schema"]["required"])
        else:
            # Treat non-meta keys as expected fields only when contract looks like a flat field-type template
            meta_keys = {
                "format", "description", "notes", "schema_version", "file", "filename",
                "deliverables", "files", "artifacts", "constraints", "evidence_requirements",
                "primary_output", "rules", "example", "title", "type", "output_dir",
            }
            for k, v in contract.items():
                if k not in meta_keys and not isinstance(v, dict):
                    fields.append(str(k))
    elif isinstance(contract, list):
        for item in contract:
            if isinstance(item, str):
                fields.append(item)
            elif isinstance(item, dict) and item.get("name"):
                fields.append(str(item["name"]))
    return list(dict.fromkeys(fields))


def _check_table_cell_value(item: Dict[str, Any], tbl_obj: Dict[str, Any]) -> Optional[str]:
    """Verify claimed value lies at the cited (row, col) cell rather than merely elsewhere in the table (Slide 17/20)."""
    val = item.get("value") if item.get("value") is not None else item.get("reported_value")
    if val is None or isinstance(val, (dict, list, bool)):
        return None
    norm_val = normalize_for_match(str(val))
    if not norm_val:
        return None

    rows = tbl_obj.get("rows") or []
    if not rows:
        return None
    n_hdr = int(tbl_obj.get("header_rows") or 1)
    flat_headers = tbl_obj.get("headers") or (rows[0] if rows else [])

    row_sel = item.get("row_label") or item.get("row_key") or item.get("row_name")
    row_idx = item.get("row_index") if isinstance(item.get("row_index"), int) else None
    col_sel = item.get("col_header") or item.get("col_name") or item.get("column_header")
    col_idx = item.get("col_index") if isinstance(item.get("col_index"), int) else None

    if row_sel is None and row_idx is None and col_sel is None and col_idx is None:
        return None

    candidate_row_ids: List[int] = []
    if row_idx is not None and 0 <= row_idx < len(rows):
        candidate_row_ids = [row_idx]
    elif row_sel is not None:
        nrs = normalize_for_match(str(row_sel))
        for r_i, r in enumerate(rows):
            if r_i < n_hdr:
                continue
            lead = normalize_for_match(" ".join(r[:2]))
            if nrs and nrs in lead:
                candidate_row_ids.append(r_i)
    else:
        candidate_row_ids = list(range(n_hdr, len(rows)))

    candidate_col_ids: List[int] = []
    ncols = max((len(r) for r in rows), default=0)
    if col_idx is not None and 0 <= col_idx < ncols:
        candidate_col_ids = [col_idx]
    elif col_sel is not None:
        ncs = normalize_for_match(str(col_sel))
        for c_i in range(ncols):
            hdr_text = flat_headers[c_i] if c_i < len(flat_headers) else ""
            sub_hdrs = " ".join((rows[hr][c_i] if c_i < len(rows[hr]) else "") for hr in range(min(n_hdr, len(rows))))
            if ncs and (ncs in normalize_for_match(hdr_text) or ncs in normalize_for_match(sub_hdrs)):
                candidate_col_ids.append(c_i)
    else:
        candidate_col_ids = list(range(ncols))

    if not candidate_row_ids or not candidate_col_ids:
        return None

    cell_texts = [
        normalize_for_match(rows[r_i][c_i])
        for r_i in candidate_row_ids
        for c_i in candidate_col_ids
        if c_i < len(rows[r_i])
    ]
    if any(norm_val in ct for ct in cell_texts):
        return None
    return (
        f"value {val!r} not found at cited cell slice row={row_sel or row_idx!r}, "
        f"col={col_sel or col_idx!r} (matched cells={cell_texts[:4]})"
    )


def _walk_evidence_items(obj: Any, path: str = "$") -> List[Dict[str, Any]]:
    """Find dict objects that declare page/block_id/table_id/equation_id/quote provenance."""
    found: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        page_val = obj.get("page") if isinstance(obj.get("page"), int) else (obj.get("page_number") if isinstance(obj.get("page_number"), int) else obj.get("source_page"))
        has_page = isinstance(page_val, int)
        has_quote = any(k in obj and isinstance(obj[k], str) and obj[k].strip() for k in ("quote", "snippet", "excerpt", "evidence_text", "verbatim"))
        has_block = "block_id" in obj and isinstance(obj["block_id"], str)
        has_table = ("table_id" in obj and isinstance(obj["table_id"], str)) or ("merged_table_id" in obj and isinstance(obj["merged_table_id"], str))
        has_eq = "equation_id" in obj and isinstance(obj["equation_id"], str)
        has_fig = "figure_id" in obj and isinstance(obj["figure_id"], str)
        if has_page or has_quote or has_block or has_table or has_eq or has_fig:
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

    raw_status = str(result.get("status") or "").strip().lower()
    if raw_status in ("nonsense", "todo", "placeholder"):
        errors.append(f"invalid placeholder result status: {raw_status!r}")

    # Validate JSON Schema property types safely if present (Slide 24: avoid TypeError on list union types)
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
        contract = case_data.get("output_contract") or case_data.get("output_schema") or {}
        props = contract.get("properties") if isinstance(contract, dict) else None
        if isinstance(props, dict):
            for fk, fspec in props.items():
                if fk in result and isinstance(fspec, dict) and result[fk] is None and not _schema_type_allows(fspec.get("type"), "null"):
                    warnings.append(f"field {fk!r} is null while schema type is {fspec.get('type')!r}")

    # Anti-boilerplate template guard (Slide 17): flag identical >=35-char string repeated across >=4 fields
    str_counts: Dict[str, int] = {}
    def _scan_boilerplate(obj: Any, pfx: str = "") -> None:
        if "sources" in pfx or "blocks" in pfx or "references" in pfx:
            return
        if isinstance(obj, str):
            st = obj.strip()
            if len(st) >= 35:
                str_counts[st] = str_counts.get(st, 0) + 1
        elif isinstance(obj, dict):
            for k, v in obj.items():
                _scan_boilerplate(v, f"{pfx}.{k}")
        elif isinstance(obj, list):
            for v in obj[:30]:
                _scan_boilerplate(v, pfx)
    _scan_boilerplate(result)
    for txt, cnt in str_counts.items():
        if cnt >= 4:
            warnings.append(f"possible template boilerplate repeated {cnt} times: {txt[:60]!r}...")

    # Evidence provenance check against document.json (+ optional ocr_results.json)
    page_count = 0
    page_texts: Dict[int, str] = {}
    block_map: Dict[str, Dict[str, Any]] = {}
    table_map: Dict[str, Dict[str, Any]] = {}
    merged_table_map: Dict[str, Dict[str, Any]] = {}
    eq_map: Dict[str, Dict[str, Any]] = {}
    fig_map: Dict[str, Dict[str, Any]] = {}
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
            table_map[tbl["table_id"]] = tbl
        for mt in doc_data.get("merged_tables", []):
            merged_table_map[mt["merged_table_id"]] = mt
        for eq in doc_data.get("equations", []):
            eq_map[eq["equation_id"]] = eq
        for fg in doc_data.get("figures", []):
            fig_map[fg["figure_id"]] = fg
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
            p = item.get("page") if isinstance(item.get("page"), int) else (item.get("page_number") if isinstance(item.get("page_number"), int) else item.get("source_page"))
            bid = item.get("block_id")
            tid = item.get("table_id")
            mtid = item.get("merged_table_id")
            eid = item.get("equation_id")
            fid = item.get("figure_id")
            quote = next((item[k] for k in ("quote", "snippet", "excerpt", "evidence_text", "verbatim") if isinstance(item.get(k), str) and item[k].strip()), None)

            status = "ok"
            notes: List[str] = []
            if p is not None and page_count > 0 and not (1 <= p <= page_count):
                status = "error"
                notes.append(f"page {p} out of range [1, {page_count}]")
                errors.append(f"{ev['path']}: page {p} out of range [1, {page_count}]")

            if bid is not None and block_map:
                if bid not in block_map:
                    status = "warning" if status == "ok" else status
                    notes.append(f"block_id {bid!r} not found in document.json")
                    warnings.append(f"{ev['path']}: block_id {bid!r} not found in document.json")
                elif p is not None and block_map[bid]["page"] != p:
                    status = "error"
                    notes.append(f"block_id {bid!r} belongs to page {block_map[bid]['page']}, not {p}")
                    errors.append(f"{ev['path']}: block_id {bid!r} page mismatch")

            if tid is not None and (table_map or page_count > 0):
                if tid not in table_map:
                    status = "warning" if status == "ok" else status
                    notes.append(f"table_id {tid!r} not found in document.json")
                    warnings.append(f"{ev['path']}: table_id {tid!r} not found in document.json")
                else:
                    if p is not None and table_map[tid]["page"] != p:
                        status = "error"
                        notes.append(f"table_id {tid!r} belongs to page {table_map[tid]['page']}, not {p}")
                        errors.append(f"{ev['path']}: table_id {tid!r} page mismatch")
                    cell_err = _check_table_cell_value(item, table_map[tid])
                    if cell_err:
                        status = "error"
                        notes.append(cell_err)
                        errors.append(f"{ev['path']}: {cell_err}")

            if mtid is not None and (merged_table_map or page_count > 0):
                if mtid not in merged_table_map:
                    status = "warning" if status == "ok" else status
                    notes.append(f"merged_table_id {mtid!r} not found in document.json")
                    warnings.append(f"{ev['path']}: merged_table_id {mtid!r} not found in document.json")
                else:
                    if p is not None and p not in merged_table_map[mtid].get("pages", []):
                        status = "error"
                        notes.append(f"merged_table_id {mtid!r} spans pages {merged_table_map[mtid].get('pages')}, not {p}")
                        errors.append(f"{ev['path']}: merged_table_id {mtid!r} page mismatch")
                    cell_err = _check_table_cell_value(item, merged_table_map[mtid])
                    if cell_err:
                        status = "error"
                        notes.append(cell_err)
                        errors.append(f"{ev['path']}: {cell_err}")

            if eid is not None and (eq_map or page_count > 0):
                if eid not in eq_map:
                    status = "warning" if status == "ok" else status
                    notes.append(f"equation_id {eid!r} not found in document.json")
                    warnings.append(f"{ev['path']}: equation_id {eid!r} not found in document.json")
                elif p is not None and eq_map[eid]["page"] != p:
                    status = "error"
                    notes.append(f"equation_id {eid!r} belongs to page {eq_map[eid]['page']}, not {p}")
                    errors.append(f"{ev['path']}: equation_id {eid!r} page mismatch")

            if fid is not None and (fig_map or page_count > 0):
                if fid not in fig_map:
                    status = "warning" if status == "ok" else status
                    notes.append(f"figure_id {fid!r} not found in document.json")
                    warnings.append(f"{ev['path']}: figure_id {fid!r} not found in document.json")
                elif p is not None and fig_map[fid]["page"] != p:
                    status = "error"
                    notes.append(f"figure_id {fid!r} belongs to page {fig_map[fid]['page']}, not {p}")
                    errors.append(f"{ev['path']}: figure_id {fid!r} page mismatch")

            if quote:
                nq = normalize_for_match(quote)
                if p is not None and p in norm_page_texts:
                    if nq and nq not in norm_page_texts[p]:
                        status = "warning" if status == "ok" else status
                        notes.append(f"quote not matched verbatim on page {p} (check OCR/crop or partial quote)")
                        warnings.append(f"{ev['path']}: quote not matched verbatim on page {p}")
                else:
                    notes.append("no extracted page text to match quote against")
                claimed_val = item.get("value") if item.get("value") is not None else item.get("reported_value")
                if claimed_val is not None and not isinstance(claimed_val, (dict, list, bool)):
                    nv = normalize_for_match(str(claimed_val))
                    if len(nv) >= 2 and nq and nv not in nq:
                        status = "warning" if status == "ok" else status
                        notes.append(f"claimed value {claimed_val!r} not found inside quote")
                        warnings.append(f"{ev['path']}: claimed value {claimed_val!r} not found inside quote")

            if str(item.get("confidence") or "").lower() == "high" and not any((quote, bid, tid, mtid, eid, fid)):
                status = "warning" if status == "ok" else status
                notes.append("high confidence claimed without grounded provenance")
                warnings.append(f"{ev['path']}: high confidence claimed without grounded provenance")

            check_row: Dict[str, Any] = {
                "path": ev["path"],
                "page": p,
                "block_id": bid,
                "status": status,
                "notes": notes,
            }
            if tid is not None:
                check_row["table_id"] = tid
            if mtid is not None:
                check_row["merged_table_id"] = mtid
            if eid is not None:
                check_row["equation_id"] = eid
            if fid is not None:
                check_row["figure_id"] = fid
            evidence_checks.append(check_row)

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "evidence_checked_count": len(evidence_checks),
        "evidence_checks": evidence_checks,
    }


def _merge_answer_patch(base_path: Path, patch_path: Path) -> None:
    """Merge a compact LLM answer patch into deterministic base result.json without wiping locked structure (Slide 15/17)."""
    base = json.loads(base_path.read_text(encoding="utf-8")) if base_path.exists() else {}
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    if not isinstance(base, dict) or not isinstance(patch, dict):
        raise ValueError("Both base result and patch must be JSON objects.")

    locked_non_empty = {"metadata", "sections", "tables", "merged_tables", "figures", "equations", "blocks", "page_label_map"}
    merged = dict(base)
    for k, v in patch.items():
        if k == "case_id" and base.get("case_id"):
            continue
        if k in locked_non_empty and base.get(k) and (v is None or v == [] or v == {}):
            continue
        if k == "change_records" and isinstance(base.get(k), list) and isinstance(v, list):
            merged[k] = list(base[k]) + list(v)
            continue
        merged[k] = v

    if merged.get("status") == "partial" and "status" not in patch:
        merged["status"] = "completed"
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Harbor result.json against case.json and extracted document.json.")
    parser.add_argument("--result", required=True, help="Path to result.json.")
    parser.add_argument("--merge", default=None, help="Optional path to compact answer patch JSON to merge into --result before auditing.")
    parser.add_argument("--case", default=None, help="Optional path to /input/case.json.")
    parser.add_argument("--doc", default=None, help="Optional path to extracted document.json.")
    parser.add_argument("--ocr", default=None, help="Optional path to ocr_results.json.")
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 if any errors are found.")
    args = parser.parse_args()

    res_path = Path(args.result)
    if args.merge:
        _merge_answer_patch(res_path, Path(args.merge))

    if not res_path.exists():
        print(json.dumps({"passed": False, "errors": [f"result.json not found: {res_path}"]}), file=sys.stderr)
        return 2

    report = audit(
        res_path,
        Path(args.case) if args.case else None,
        Path(args.doc) if args.doc else None,
        Path(args.ocr) if args.ocr else None,
    )
    out_txt = json.dumps(report, ensure_ascii=False, indent=2)
    if len(out_txt) > 24000:
        slim = dict(report)
        slim["evidence_checks"] = [c for c in report["evidence_checks"] if c.get("status") != "ok"][:40]
        slim["truncated"] = True
        out_txt = json.dumps(slim, ensure_ascii=False, indent=2)
    print(out_txt)
    if args.strict and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
