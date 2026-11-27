#!/usr/bin/env python3
"""Merge structured-note patches, enforce 4-pillar strict audit, detect term duplicates/conflicts, and export reusable Markdown notes."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True

from doc_common import collect_contract_keys, normalize_for_match, normalize_term_key


def _merge_patch(result_path: Path, patch_path: Path) -> Dict[str, Any]:
    res = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    if not isinstance(res, dict) or not isinstance(patch, dict):
        raise ValueError("Both result.json and patch.json must be JSON objects.")

    locked_cid = res.get("case_id")
    for k, v in patch.items():
        if k == "case_id" and locked_cid:
            continue
        if k == "sources" and isinstance(res.get("sources"), list) and isinstance(v, list):
            existing = {json.dumps(s, sort_keys=True, ensure_ascii=False) for s in res["sources"]}
            merged_sources = list(res["sources"])
            for item in v:
                sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if sig not in existing:
                    existing.add(sig)
                    merged_sources.append(item)
            res["sources"] = merged_sources
        elif k in ("uncertainty", "change_records", "human_review_items", "conflicts") and isinstance(res.get(k), list) and isinstance(v, list):
            if k == "uncertainty" and len(res[k]) == 1 and "Initial deterministic ingestion completed" in str(res[k][0]):
                res[k] = list(v)
            else:
                res[k].extend(v)
        else:
            res[k] = v
    result_path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def _walk_evidence_items(obj: Any, path: str = "$") -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        p_val = obj.get("page") if isinstance(obj.get("page"), int) else obj.get("page_number")
        has_q = any(isinstance(obj.get(k), str) and obj[k].strip() for k in ("quote", "snippet", "excerpt", "verbatim", "evidence_text"))
        has_id = any(isinstance(obj.get(k), str) for k in ("block_id", "table_id", "equation_id", "figure_id"))
        if isinstance(p_val, int) or has_q or has_id:
            found.append({"path": path, "item": obj})
        for k, v in obj.items():
            found.extend(_walk_evidence_items(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(_walk_evidence_items(v, f"{path}[{i}]"))
    return found


def _check_table_cell(item: Dict[str, Any], tbl: Dict[str, Any]) -> Optional[str]:
    val = item.get("value") if item.get("value") is not None else item.get("reported_value")
    if val is None or isinstance(val, (dict, list, bool)):
        return None
    norm_val = normalize_for_match(str(val))
    rows = tbl.get("rows") or []
    if not norm_val or not rows:
        return None
    n_hdr = int(tbl.get("header_rows") or 1)
    headers = tbl.get("headers") or rows[0]
    row_sel = item.get("row_label") or item.get("row_key")
    col_sel = item.get("col_header") or item.get("col_name")
    if row_sel is None and col_sel is None:
        return None

    r_ids = [
        i for i, r in enumerate(rows)
        if i >= n_hdr and (row_sel is None or normalize_for_match(str(row_sel)) in normalize_for_match(" ".join(r[:2])))
    ]
    ncols = max((len(r) for r in rows), default=0)
    c_ids = [
        c for c in range(ncols)
        if col_sel is None or normalize_for_match(str(col_sel)) in normalize_for_match(headers[c] if c < len(headers) else "")
    ]
    if not r_ids or not c_ids:
        return None
    cells = [normalize_for_match(rows[r][c]) for r in r_ids for c in c_ids if c < len(rows[r])]
    if any(norm_val in ct for ct in cells):
        return None
    return f"value {val!r} not found at cited cell row={row_sel!r}, col={col_sel!r}"


def _audit_knowledge_points(kps: List[Dict[str, Any]], errors: List[str], warnings: List[str], conflicts_declared: List[Any]) -> None:
    """Detect duplicate canonical terms, missing preconditions on findings/methods, and undeclared numeric/polarity conflicts."""
    seen_terms: Dict[str, int] = {}
    claims_by_term: Dict[str, List[Tuple[int, str, str]]] = {}

    for idx, kp in enumerate(kps):
        if not isinstance(kp, dict):
            continue
        term = str(kp.get("canonical_term") or kp.get("term") or kp.get("title") or "").strip()
        if not term:
            errors.append(f"knowledge_points[{idx}] missing canonical_term/title.")
            continue
        key = normalize_term_key(term)
        if key in seen_terms:
            warnings.append(f"Duplicate knowledge_point canonical_term {term!r} (indices {seen_terms[key]} and {idx}); merge aliases instead of duplicating.")
        else:
            seen_terms[key] = idx
        for alias in kp.get("aliases") or []:
            akey = normalize_term_key(str(alias))
            if akey and akey in seen_terms and seen_terms[akey] != idx:
                warnings.append(f"Alias {alias!r} in knowledge_points[{idx}] collides with canonical_term at index {seen_terms[akey]}.")

        kind = str(kp.get("kind") or kp.get("category") or "").lower()
        precond = kp.get("preconditions") or kp.get("scope_conditions") or kp.get("conditions")
        if kind in ("finding", "conclusion", "method", "result", "claim") and not precond:
            warnings.append(f"knowledge_points[{idx}] ({term!r}, kind={kind}) lacks explicit preconditions/scope_conditions (mark 'unreported' if none stated).")

        anchors = kp.get("evidence_anchors") or ([kp["evidence"]] if kp.get("evidence") else [])
        if not anchors:
            errors.append(f"knowledge_points[{idx}] ({term!r}) has no source evidence anchor.")

        stmt = str(kp.get("statement") or kp.get("summary") or kp.get("definition") or "")
        claims_by_term.setdefault(key, []).append((idx, term, stmt))

    # Flag potential numeric mismatches on identical canonical terms if conflicts[] is empty
    num_re = re.compile(r"-?\d+(?:\.\d+)?\s*(?:%|K|°C|bar|MPa|kPa|mg|g|mol|h|min|s)\b")
    for key, entries in claims_by_term.items():
        if len(entries) >= 2:
            num_sets = {tuple(sorted(num_re.findall(stmt))) for _, _, stmt in entries if num_re.search(stmt)}
            if len(num_sets) >= 2 and not conflicts_declared:
                warnings.append(f"Potential numeric conflict across duplicate entries for {entries[0][1]!r}: {num_sets}; document in conflicts[].")


def _load_all_docs(work_dir: Optional[Path], doc_path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    docs: Dict[str, Dict[str, Any]] = {}
    if doc_path and doc_path.exists():
        d = json.loads(doc_path.read_text(encoding="utf-8"))
        did = d.get("doc_id") or "doc_1"
        d["_dir"] = str(doc_path.parent)
        docs[did] = d
        return docs
    if work_dir and work_dir.exists():
        for dp in sorted(work_dir.glob("*/document.json")):
            d = json.loads(dp.read_text(encoding="utf-8"))
            did = d.get("doc_id") or dp.parent.name
            d["_dir"] = str(dp.parent)
            docs[did] = d
    return docs


def audit_result(
    result_path: Path,
    case_path: Optional[Path],
    work_dir: Optional[Path],
    doc_path: Optional[Path] = None,
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    evidence_checks: List[Dict[str, Any]] = []

    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        return {"passed": False, "errors": ["result.json root must be a JSON object"], "warnings": [], "evidence_checks": []}

    raw_status = str(result.get("status") or "").strip().lower()
    if raw_status in ("todo", "placeholder", "nonsense", ""):
        errors.append(f"invalid result status: {raw_status!r}")

    if case_path and case_path.exists():
        case_data = json.loads(case_path.read_text(encoding="utf-8"))
        exp_cid = case_data.get("case_id")
        if exp_cid and result.get("case_id") != exp_cid:
            errors.append(f"case_id mismatch: expected {exp_cid!r}, got {result.get('case_id')!r}")
        for f in collect_contract_keys(case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output")):
            if f not in result:
                errors.append(f"missing required output_contract field: {f!r}")
            elif result[f] is None or result[f] == "":
                warnings.append(f"required field {f!r} is empty/null (ensure uncertainty explains why if unreported)")

    # Boilerplate repetition check
    str_counts: Dict[str, int] = {}
    def _scan_bp(obj: Any, pfx: str = "") -> None:
        if any(x in pfx for x in ("sources", "blocks", "references")):
            return
        if isinstance(obj, str):
            st = obj.strip()
            if len(st) >= 35:
                str_counts[st] = str_counts.get(st, 0) + 1
        elif isinstance(obj, dict):
            for k, v in obj.items():
                _scan_bp(v, f"{pfx}.{k}")
        elif isinstance(obj, list):
            for v in obj[:40]:
                _scan_bp(v, pfx)
    _scan_bp(result)
    for txt, cnt in str_counts.items():
        if cnt >= 4:
            warnings.append(f"possible template boilerplate repeated {cnt} times: {txt[:60]!r}")

    # Load ingested document structures & OCR outputs
    docs = _load_all_docs(work_dir, doc_path)
    doc_indices: Dict[str, Dict[str, Any]] = {}
    for did, d in docs.items():
        ptxt: Dict[int, str] = {}
        bmap, tmap = {}, {}
        for b in d.get("blocks", []):
            p = int(b["page"])
            ptxt[p] = ptxt.get(p, "") + "\n" + b.get("raw_text", "") + "\n" + b.get("text", "")
            bmap[b["block_id"]] = b
        for t in d.get("tables", []):
            p = int(t["page"])
            ptxt[p] = ptxt.get(p, "") + "\n" + " ".join(" ".join(r) for r in t.get("rows", []))
            tmap[t["table_id"]] = t
        ocr_p = Path(d["_dir"]) / "ocr" / "ocr_results.json"
        if ocr_p.exists():
            ocr_d = json.loads(ocr_p.read_text(encoding="utf-8"))
            for it in ocr_d.get("rendered", []):
                p = it.get("page")
                ot = (it.get("ocr") or {}).get("text") or ""
                if isinstance(p, int) and ot.strip():
                    ptxt[p] = ptxt.get(p, "") + "\n" + ot
        doc_indices[did] = {
            "page_count": int(d.get("page_count") or 1),
            "norm_pages": {p: normalize_for_match(t) for p, t in ptxt.items()},
            "blocks": bmap,
            "tables": tmap,
            "sections": {s["section_id"]: s for s in d.get("sections", [])},
            "section_titles": {normalize_term_key(s["title"]): s for s in d.get("sections", [])},
        }

    # Pillar 1: Section alignment check (对得上)
    sec_notes = result.get("section_notes")
    if isinstance(sec_notes, list) and sec_notes and doc_indices:
        last_order_by_doc: Dict[str, int] = {}
        for idx, sn in enumerate(sec_notes):
            if not isinstance(sn, dict):
                continue
            did = sn.get("doc_id") or next(iter(doc_indices))
            didx = doc_indices.get(did)
            if didx and didx["sections"]:
                sid = sn.get("section_id")
                stitle = str(sn.get("section_title") or sn.get("title") or "")
                sec_obj = didx["sections"].get(sid) or didx["section_titles"].get(normalize_term_key(stitle))
                if not sec_obj:
                    warnings.append(f"section_notes[{idx}] ({stitle or sid!r}) does not match extracted paper section skeleton.")
                else:
                    oi = int(sec_obj.get("order_index", 0))
                    if did in last_order_by_doc and oi < last_order_by_doc[did]:
                        errors.append(f"section_notes[{idx}] breaks paper's original section order (order_index {oi} < {last_order_by_doc[did]}).")
                    last_order_by_doc[did] = oi

    # Pillar 2: Paper Card completeness & condition retention (留得住)
    cards = result.get("paper_cards") if isinstance(result.get("paper_cards"), list) else ([result["paper_card"]] if isinstance(result.get("paper_card"), dict) else [])
    for ci, card in enumerate(cards):
        for req_c in ("research_question", "data_or_materials", "method_and_conditions", "key_findings", "limitations"):
            if req_c not in card or card[req_c] in (None, "", []):
                warnings.append(f"paper_card[{ci}] missing or empty {req_c!r}; explicitly record value or 'unreported'.")

    # Pillar 4: Knowledge Points dedup/conflict/precondition check (用得上)
    kps = result.get("knowledge_points")
    if isinstance(kps, list) and kps:
        _audit_knowledge_points(kps, errors, warnings, result.get("conflicts") or [])

    # Pillar 3: Verbatim anchor verification (找得回)
    if doc_indices:
        for ev in _walk_evidence_items(result):
            item = ev["item"]
            did = item.get("doc_id")
            cand_docs = [doc_indices[did]] if (did and did in doc_indices) else list(doc_indices.values())
            p = item.get("page") if isinstance(item.get("page"), int) else item.get("page_number")
            bid = item.get("block_id")
            tid = item.get("table_id")
            quote = next((item[k] for k in ("quote", "snippet", "excerpt", "verbatim", "evidence_text") if isinstance(item.get(k), str) and item[k].strip()), None)

            matched_ok = False
            best_err = None
            for d_idx in cand_docs:
                ok_here = True
                if p is not None and not (1 <= p <= d_idx["page_count"]):
                    ok_here = False
                    best_err = f"page {p} out of range [1, {d_idx['page_count']}]"
                if ok_here and bid is not None and d_idx["blocks"]:
                    if bid not in d_idx["blocks"]:
                        ok_here = False
                        best_err = f"block_id {bid!r} not found"
                    elif p is not None and d_idx["blocks"][bid]["page"] != p:
                        ok_here = False
                        best_err = f"block_id {bid!r} belongs to page {d_idx['blocks'][bid]['page']}, not {p}"
                if ok_here and tid is not None and d_idx["tables"]:
                    if tid not in d_idx["tables"]:
                        ok_here = False
                        best_err = f"table_id {tid!r} not found"
                    else:
                        cerr = _check_table_cell(item, d_idx["tables"][tid])
                        if cerr:
                            ok_here = False
                            best_err = cerr
                if ok_here and quote:
                    nq = normalize_for_match(quote)
                    if len(nq) >= 8:
                        pages_chk = [d_idx["norm_pages"].get(p, "")] if p is not None else list(d_idx["norm_pages"].values())
                        if not any(nq in pt for pt in pages_chk):
                            ok_here = False
                            best_err = f"quote not matched verbatim on cited page {p}: {nq[:70]!r}"
                if ok_here:
                    matched_ok = True
                    break
            if not matched_ok and best_err:
                errors.append(f"{ev['path']}: {best_err}")
                evidence_checks.append({"path": ev["path"], "status": "error", "detail": best_err})
            else:
                evidence_checks.append({"path": ev["path"], "status": "ok"})

    if raw_status == "completed" and not result.get("sources"):
        errors.append("status='completed' requires non-empty sources[] with page/block/quote anchors.")

    return {
        "passed": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "evidence_checked_count": len(evidence_checks),
    }


def export_markdown_notes(result: Dict[str, Any], out_md: Path) -> None:
    """Export result.json into reusable, searchable, Obsidian/Zettelkasten-friendly Markdown notes."""
    cid = result.get("case_id") or "unknown"
    status = result.get("status") or "unknown"
    lines = [
        "---",
        f"case_id: {cid}",
        f"status: {status}",
        "tags: [literature-notes, structured-summary, evidence-linked]",
        "---",
        "",
        f"# Structured Literature Notes ({cid})",
        "",
    ]

    cards = result.get("paper_cards") if isinstance(result.get("paper_cards"), list) else ([result["paper_card"]] if isinstance(result.get("paper_card"), dict) else [])
    if cards:
        lines.extend(["## 1. Paper Cards (Comparable Matrix)", ""])
        lines.append("| Doc | Title | Research Question | Data / Materials | Method & Preconditions | Key Findings | Limitations |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for c in cards:
            row = [
                str(c.get("doc_id", "doc_1")),
                str(c.get("title", "")),
                str(c.get("research_question", "")),
                str(c.get("data_or_materials", "")),
                str(c.get("method_and_conditions", "")),
                str(c.get("key_findings", "")),
                str(c.get("limitations", "")),
            ]
            lines.append("| " + " | ".join(x.replace("|", "\\|").replace("\n", " ") for x in row) + " |")
        lines.append("")

    sec_notes = result.get("section_notes") or []
    if sec_notes:
        lines.extend(["## 2. Section-Aligned Summary (Paper Skeleton)", ""])
        for sn in sec_notes:
            hdr = f"### [{sn.get('section_id', '')}] {sn.get('section_title') or sn.get('title') or 'Section'}"
            lines.append(hdr)
            if sn.get("transition_from_prev"):
                lines.append(f"- **Transition from previous**: {sn['transition_from_prev']}")
            for fld, lbl in (("purpose", "Purpose"), ("methods", "Methods & Conditions"), ("results", "Results"), ("conclusion", "Conclusion"), ("limitations", "Limitations")):
                if sn.get(fld):
                    lines.append(f"- **{lbl}**: {sn[fld]}")
            anchors = sn.get("evidence_anchors") or ([sn["evidence"]] if sn.get("evidence") else [])
            for a in anchors[:4]:
                lines.append(f"  - Anchor: `[{a.get('doc_id', 'doc_1')}|p.{a.get('page')}|{a.get('block_id') or a.get('table_id')}]` — \"{str(a.get('quote', ''))[:140]}\"")
            lines.append("")

    kps = result.get("knowledge_points") or []
    if kps:
        lines.extend(["## 3. Atomic Knowledge Points (Searchable Index)", ""])
        for kp in kps:
            aliases = f" (aliases: {', '.join(kp['aliases'])})" if kp.get("aliases") else ""
            lines.append(f"- **`{kp.get('canonical_term')}`**{aliases} [{kp.get('kind', 'concept')} | `{kp.get('verification_mode', 'deterministic')}`]: {kp.get('statement', '')}")
            if kp.get("preconditions"):
                lines.append(f"  - Preconditions: {kp['preconditions']}")
            for a in (kp.get("evidence_anchors") or [])[:2]:
                lines.append(f"  - Source: `[{a.get('doc_id', 'doc_1')}|p.{a.get('page')}|{a.get('block_id') or a.get('table_id')}]` — \"{str(a.get('quote', ''))[:120]}\"")
        lines.append("")

    conflicts = result.get("conflicts") or []
    if conflicts:
        lines.extend(["## 4. Detected Conflicts & Discrepancies", ""])
        for cf in conflicts:
            lines.append(f"- {json.dumps(cf, ensure_ascii=False)}")
        lines.append("")

    hr = result.get("human_review_items") or []
    if hr:
        lines.extend(["## 5. Human Review Checklist", ""])
        for it in hr:
            lines.append(f"- [ ] {json.dumps(it, ensure_ascii=False)}")
        lines.append("")

    unc = result.get("uncertainty")
    if unc:
        lines.extend(["## 6. Uncertainty & Unreported Items", "", f"- {unc}" if isinstance(unc, str) else "\n".join(f"- {u}" for u in unc), ""])

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge, audit, and export structured literature notes.")
    parser.add_argument("--result", required=True)
    parser.add_argument("--merge", default=None)
    parser.add_argument("--case", default=None)
    parser.add_argument("--work-dir", default="/app/work")
    parser.add_argument("--doc", default=None)
    parser.add_argument("--export-md", default=None)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    res_p = Path(args.result)
    if args.merge:
        _merge_patch(res_p, Path(args.merge))

    report = audit_result(res_p, Path(args.case) if args.case else None, Path(args.work_dir) if args.work_dir else None, Path(args.doc) if args.doc else None)
    if args.export_md and res_p.exists():
        export_markdown_notes(json.loads(res_p.read_text(encoding="utf-8")), Path(args.export_md))
        report["exported_md"] = args.export_md

    print(json.dumps(report, ensure_ascii=False, indent=2)[:24000])
    if args.strict and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
