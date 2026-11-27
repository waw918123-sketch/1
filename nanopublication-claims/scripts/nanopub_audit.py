#!/usr/bin/env python3
"""Merge Nanopublication patches, deterministically inject URIs/PubInfo/hashes, enforce 5-pillar strict audit, and export TriG/JSON-LD."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.dont_write_bytecode = True

from doc_common import (
    collect_contract_keys,
    load_predicate_vocab,
    normalize_for_match,
    normalize_term_key,
    sha256_text,
)
from nanopub_export import export_all

NON_SIG_DIFF_RE = re.compile(
    r"\bno\s+(?:statistically\s+)?significant\s+differences?\b|\bp\s*(?:=|>|≥)\s*0?\.(?:0[5-9]|[1-9]\d*)\b|无显著差异",
    re.I,
)
EQUIVALENCE_CLAIM_RE = re.compile(
    r"\b(?:identical|same\s+as|equal(?:s)?\s+to|no\s+difference\s+exists|equivalent)\b|相同|完全一致|相等",
    re.I,
)
EXTRAPOLATION_METHOD_RE = re.compile(
    r"(?:extrapolat|higher[- ]taxon|indirect|assum|estimat|model|proxy|interpolat|推测|外推)",
    re.I,
)
COMPOUND_SPLIT_RE = re.compile(
    r"(?:\b(?:and\s+(?:increases?|decreases?|reduces?|elevates?|inhibits?|activates?|causes?|correlates?))\b)",
    re.I,
)
NUM_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])((?:(?<!\d)-)?\d+(?:,\d{3})*(?:\.\d+)?)\b")


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


def _enrich_and_merge_patch(
    result_path: Path,
    patch_path: Path,
    docs: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    res = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    if not isinstance(res, dict) or not isinstance(patch, dict):
        raise ValueError("Both result.json and patch.json must be JSON objects.")

    vocab = load_predicate_vocab()
    alias_map = vocab.get("alias_to_canonical") or {}
    locked_cid = res.get("case_id")
    locked_pub = res.get("pubinfo")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Accept nanopublications / assertions / claims seamlessly from patch
    incoming_nps = patch.get("nanopublications")
    if incoming_nps is None:
        incoming_nps = patch.get("assertions") if isinstance(patch.get("assertions"), list) else patch.get("claims")

    for k, v in patch.items():
        if k == "case_id" and locked_cid:
            continue  # Never allow patch to corrupt locked case_id
        if k == "pubinfo" and isinstance(locked_pub, dict) and locked_pub:
            continue  # Never allow patch to overwrite deterministic PubInfo (authors/DOI/year)
        if k == "sources" and isinstance(res.get("sources"), list) and isinstance(v, list):
            seen = {json.dumps(s, sort_keys=True, ensure_ascii=False) for s in res["sources"]}
            merged = list(res["sources"])
            for item in v:
                sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if sig not in seen:
                    seen.add(sig)
                    merged.append(item)
            res["sources"] = merged
        elif k in ("uncertainty", "change_records", "human_review_items", "conflicts") and isinstance(res.get(k), list) and isinstance(v, list):
            if k == "uncertainty" and len(res[k]) == 1 and "Initial deterministic ingestion completed" in str(res[k][0]):
                res[k] = list(v)
            else:
                seen = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in res[k]}
                for item in v:
                    sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                    if sig not in seen:
                        seen.add(sig)
                        res[k].append(item)
        elif k not in ("nanopublications", "assertions", "claims"):
            res[k] = v

    if isinstance(incoming_nps, list):
        # Support incremental append when patch sets append_nanopubs: true, else replace list
        existing_nps = list(res.get("nanopublications") or []) if patch.get("append_nanopubs") else []
        raw_nps = existing_nps + list(incoming_nps)
        enriched: List[Dict[str, Any]] = []
        block_lookup = {did: {b["block_id"]: b for b in d.get("blocks", [])} for did, d in docs.items()}
        table_lookup = {did: {t["table_id"]: t for t in d.get("tables", [])} for did, d in docs.items()}
        page_label_lookup = {did: (d.get("page_label_map") or {}) for did, d in docs.items()}

        for idx, item in enumerate(raw_nps, start=1):
            if not isinstance(item, dict):
                continue
            np_obj = json.loads(json.dumps(item))
            np_id = f"np_{idx:03d}"
            np_obj["nanopub_id"] = np_id

            ass = np_obj.get("assertion") if isinstance(np_obj.get("assertion"), dict) else {}
            if not ass and ("subject" in np_obj or "predicate" in np_obj):
                ass = {
                    k: np_obj.pop(k)
                    for k in ("subject", "predicate", "object", "assertion_text", "polarity", "conditions", "statistics", "study_design")
                    if k in np_obj
                }
            raw_pred = str(ass.get("predicate") or "").strip()
            can_pred = alias_map.get(normalize_term_key(raw_pred))
            if can_pred:
                ass["canonical_predicate"] = can_pred
            np_obj["assertion"] = ass

            prov = np_obj.get("provenance") if isinstance(np_obj.get("provenance"), dict) else {}
            anchors = prov.get("evidence_anchors") or ([prov["evidence"]] if isinstance(prov.get("evidence"), dict) else [])
            if not anchors and isinstance(np_obj.get("evidence_anchors"), list):
                anchors = np_obj.pop("evidence_anchors")

            primary_did = None
            for anc in anchors:
                if not isinstance(anc, dict):
                    continue
                did = anc.get("doc_id") or (next(iter(docs)) if docs else "doc_1")
                anc["doc_id"] = did
                if primary_did is None:
                    primary_did = did
                bid = anc.get("block_id")
                tid = anc.get("table_id")
                bmap = block_lookup.get(did, {})
                tmap = table_lookup.get(did, {})
                if bid and bid in bmap:
                    blk = bmap[bid]
                    anc.setdefault("bbox", blk.get("bbox"))
                    anc.setdefault("section_id", blk.get("section_id"))
                    if anc.get("page") is None:
                        anc["page"] = blk.get("page")
                if tid and tid in tmap:
                    tbl = tmap[tid]
                    anc.setdefault("bbox", tbl.get("bbox"))
                    anc.setdefault("section_id", tbl.get("section_id"))
                    if anc.get("page") is None:
                        anc["page"] = tbl.get("page")
                pno = anc.get("page")
                plmap = page_label_lookup.get(did, {})
                if pno is not None and not anc.get("printed_page"):
                    lbl = plmap.get(pno) or plmap.get(str(pno))
                    if lbl:
                        anc["printed_page"] = lbl
                qtxt = str(anc.get("quote") or "")
                if qtxt:
                    anc["quote_sha256"] = sha256_text(normalize_for_match(qtxt))[:16]
            prov["evidence_anchors"] = anchors
            np_obj["provenance"] = prov

            # Inherit locked PubInfo deterministically
            pub = {}
            if isinstance(locked_pub, dict):
                if primary_did and primary_did in locked_pub and isinstance(locked_pub[primary_did], dict):
                    pub = dict(locked_pub[primary_did])
                elif locked_pub:
                    first_val = next(iter(locked_pub.values()))
                    if isinstance(first_val, dict):
                        pub = dict(first_val)
            pub.setdefault("created_at", now_iso)
            pub.setdefault("generated_by", "nanopublication-claims/nanopub_audit.py")
            np_obj["pubinfo"] = pub

            sig_src = f"{ass.get('subject','')}|{ass.get('canonical_predicate') or raw_pred}|{ass.get('object','')}|{pub.get('doi','')}|{idx}"
            hash_suf = sha256_text(sig_src)[:16]
            np_uri = f"http://purl.org/np/RA{hash_suf}"
            np_obj["nanopub_uri"] = np_uri
            np_obj["head"] = {
                "nanopub_uri": np_uri,
                "assertion_graph": f"{np_uri}#assertion",
                "provenance_graph": f"{np_uri}#provenance",
                "pubinfo_graph": f"{np_uri}#pubinfo",
            }
            enriched.append(np_obj)

        res["nanopublications"] = enriched
        # Keep contract-required aliases synchronized if requested by case.json
        for alias_k in ("assertions", "claims"):
            if alias_k in res and isinstance(res[alias_k], list):
                if not patch.get(alias_k) or patch.get(alias_k) is incoming_nps:
                    res[alias_k] = enriched

    result_path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


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
        return f"row_label={row_sel!r} or col_header={col_sel!r} not found in {tbl['table_id']}"
    cells = [normalize_for_match(rows[r][c]) for r in r_ids for c in c_ids if c < len(rows[r])]
    if any(norm_val in ct for ct in cells):
        return None
    return f"value {val!r} not found at cited cell row={row_sel!r}, col={col_sel!r} in {tbl['table_id']} (actual cell(s): {cells[:2]})"


def _extract_numeric_tokens(text: str) -> List[str]:
    return [m.replace(",", "") for m in NUM_TOKEN_RE.findall(text or "")]


def audit_result(
    result_path: Path,
    case_path: Optional[Path],
    work_dir: Optional[Path],
    doc_path: Optional[Path] = None,
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    checks_summary = {"nanopubs_checked": 0, "anchors_checked": 0, "table_cells_verified": 0}

    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        return {"passed": False, "error_count": 1, "warning_count": 0, "errors": ["result.json root must be a JSON object"], "warnings": []}

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
                warnings.append(f"required field {f!r} is empty/null (ensure uncertainty explains why)")

    docs = _load_all_docs(work_dir, doc_path)
    doc_indices: Dict[str, Dict[str, Any]] = {}
    for did, d in docs.items():
        ptxt: Dict[int, str] = {}
        bmap, tmap = {}, {}
        ocr_page_confs: Dict[int, float] = {}
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
                ocr_obj = it.get("ocr") or {}
                ot = ocr_obj.get("text") or ""
                if isinstance(p, int) and ot.strip():
                    ptxt[p] = ptxt.get(p, "") + "\n" + ot
                if isinstance(p, int) and isinstance(ocr_obj.get("mean_confidence"), (int, float)):
                    ocr_page_confs[p] = float(ocr_obj["mean_confidence"])
        doc_indices[did] = {
            "page_count": int(d.get("page_count") or 1),
            "norm_pages": {p: normalize_for_match(t) for p, t in ptxt.items()},
            "blocks": bmap,
            "tables": tmap,
            "ocr_page_confs": ocr_page_confs,
        }

    vocab = load_predicate_vocab()
    preds_meta = vocab.get("predicates") or {}
    alias_map = vocab.get("alias_to_canonical") or {}
    vague_preds = set(vocab.get("vague_predicates") or [])

    nps = result.get("nanopublications") or result.get("assertions") or result.get("claims") or []
    if raw_status == "completed" and not nps:
        errors.append("status='completed' requires non-empty nanopublications[].")
    if raw_status == "completed" and not result.get("sources"):
        errors.append("status='completed' requires non-empty sources[].")

    seen_sigs: Dict[Tuple[str, str, str], int] = {}
    pair_claims: Dict[Tuple[str, str], List[Tuple[int, str, List[str]]]] = {}

    for idx, np_item in enumerate(nps or []):
        checks_summary["nanopubs_checked"] += 1
        if not isinstance(np_item, dict):
            errors.append(f"nanopublications[{idx}] must be an object.")
            continue
        np_id = str(np_item.get("nanopub_id") or f"np_{idx + 1:03d}")
        ass = np_item.get("assertion")
        prov = np_item.get("provenance")
        pub = np_item.get("pubinfo")

        # External mandatory checklist (prevents deleting fields to pass silently)
        if not isinstance(ass, dict):
            errors.append(f"{np_id}: missing 'assertion' object.")
            continue
        for req_a in ("subject", "predicate", "object", "assertion_text", "polarity", "conditions"):
            if not str(ass.get(req_a) or "").strip():
                errors.append(f"{np_id}: assertion missing required field {req_a!r}.")
        if not isinstance(prov, dict):
            errors.append(f"{np_id}: missing 'provenance' object.")
            continue
        for req_p in ("evidence_type", "extraction_method", "confidence", "confidence_basis"):
            if not str(prov.get(req_p) or "").strip():
                errors.append(f"{np_id}: provenance missing required field {req_p!r}.")
        if not isinstance(pub, dict) or not str(pub.get("title") or "").strip():
            errors.append(f"{np_id}: missing verified 'pubinfo' metadata.")

        # Atomicity & controlled predicate check
        subj = str(ass.get("subject") or "")
        raw_pred = str(ass.get("predicate") or "")
        obj = str(ass.get("object") or "")
        atxt = str(ass.get("assertion_text") or "")
        pol = str(ass.get("polarity") or "").lower()
        design = str(ass.get("study_design") or "").lower()
        can_pred = alias_map.get(normalize_term_key(raw_pred))
        if normalize_term_key(raw_pred) in vague_preds:
            warnings.append(f"{np_id}: vague predicate {raw_pred!r}; replace with a specific predicate from predicate-vocab.json.")
        elif not can_pred:
            warnings.append(f"{np_id}: predicate {raw_pred!r} is outside controlled predicate vocabulary.")

        if COMPOUND_SPLIT_RE.search(atxt) or len(atxt.split()) > 42 or len(atxt) > 260:
            warnings.append(f"{np_id}: assertion_text appears compound or overly long ({len(atxt)} chars); consider splitting into atomic assertions.")

        # Evidence anchor validation & cell binding
        anchors = prov.get("evidence_anchors") or []
        if not isinstance(anchors, list) or not anchors:
            errors.append(f"{np_id}: provenance.evidence_anchors must be a non-empty list.")
            anchors = []

        anchor_corpus_norm = ""
        anchor_cell_vals: Set[str] = set()
        uses_low_ocr = False
        uses_defect_blk = False
        uses_table_with_cell = False

        for ai, anc in enumerate(anchors):
            if not isinstance(anc, dict):
                errors.append(f"{np_id}.evidence_anchors[{ai}] must be an object.")
                continue
            checks_summary["anchors_checked"] += 1
            did = anc.get("doc_id")
            p = anc.get("page") if isinstance(anc.get("page"), int) else anc.get("page_number")
            bid = anc.get("block_id")
            tid = anc.get("table_id")
            quote = str(anc.get("quote") or "").strip()

            if p is None and bid is None and tid is None:
                errors.append(f"{np_id}.evidence_anchors[{ai}]: missing location (page, block_id, or table_id).")

            if len(quote) > 480:
                warnings.append(f"{np_id}.evidence_anchors[{ai}]: quote ({len(quote)} chars) is bloated; use shortest sufficient contiguous evidence.")

            if doc_indices:
                cand_docs = [doc_indices[did]] if (did and did in doc_indices) else list(doc_indices.values())
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
                        else:
                            blk = d_idx["blocks"][bid]
                            if p is not None and blk["page"] != p:
                                ok_here = False
                                best_err = f"block_id {bid!r} belongs to page {blk['page']}, not {p}"
                            elif (blk.get("defects") or {}).get("has_defects"):
                                uses_defect_blk = True
                    if ok_here and tid is not None and d_idx["tables"]:
                        if tid not in d_idx["tables"]:
                            ok_here = False
                            best_err = f"table_id {tid!r} not found"
                        else:
                            cerr = _check_table_cell(anc, d_idx["tables"][tid])
                            if cerr:
                                ok_here = False
                                best_err = cerr
                            elif anc.get("value") is not None and (anc.get("row_label") or anc.get("col_header")):
                                uses_table_with_cell = True
                                checks_summary["table_cells_verified"] += 1
                                anchor_cell_vals.add(normalize_for_match(str(anc["value"])).replace(",", ""))
                    if ok_here and quote:
                        nq = normalize_for_match(quote)
                        if len(nq) >= 8:
                            pages_chk = [d_idx["norm_pages"].get(p, "")] if p is not None else list(d_idx["norm_pages"].values())
                            if not any(nq in pt for pt in pages_chk):
                                ok_here = False
                                best_err = f"quote not matched verbatim on cited page {p}: {nq[:70]!r}"
                    if ok_here:
                        matched_ok = True
                        if p is not None and p in d_idx["ocr_page_confs"] and d_idx["ocr_page_confs"][p] < 75.0:
                            uses_low_ocr = True
                        break
                if not matched_ok and best_err:
                    errors.append(f"{np_id}.evidence_anchors[{ai}]: {best_err}")

            anchor_corpus_norm += " " + normalize_for_match(quote)
            if anc.get("value") is not None:
                anchor_corpus_norm += " " + str(anc["value"])

        # Numeric grounding check: numbers stated in assertion.object must appear in bound anchor quote/cell
        obj_nums = _extract_numeric_tokens(obj)
        if obj_nums and anchors:
            corp_nums = set(_extract_numeric_tokens(anchor_corpus_norm)) | anchor_cell_vals
            corp_floats = []
            for cn in corp_nums:
                try:
                    corp_floats.append(float(cn))
                except ValueError:
                    pass
            for num in obj_nums:
                num_ok = (num in corp_nums) or (num in anchor_corpus_norm.replace(",", ""))
                if not num_ok:
                    try:
                        nf = float(num)
                        num_ok = any(abs(nf - cf) < 1e-9 for cf in corp_floats)
                    except ValueError:
                        pass
                if not num_ok:
                    errors.append(f"{np_id}: assertion.object cites numeric value {num!r} absent from bound evidence_anchors (possible misattribution or fabrication).")
            if any(a.get("table_id") for a in anchors) and not uses_table_with_cell and not any(a.get("quote") for a in anchors):
                errors.append(f"{np_id}: cites table_id with numeric value {obj_nums} but lacks row_label + col_header + value cell binding.")

        # Causal & statistical overreach check (Slide 19)
        if NON_SIG_DIFF_RE.search(anchor_corpus_norm) and (
            EQUIVALENCE_CLAIM_RE.search(atxt) or EQUIVALENCE_CLAIM_RE.search(raw_pred)
        ):
            errors.append(f"{np_id}: statistical overreach — non-significant p-value / no significant difference cannot be stated as exact equivalence/identical.")

        if can_pred and can_pred in preds_meta:
            forb = set(preds_meta[can_pred].get("forbidden_study_designs") or [])
            if design and design in forb:
                errors.append(f"{np_id}: causal overreach — predicate {can_pred!r} is incompatible with study_design={design!r}; use 'correlates_with'/'predicts'/'differs_from'.")

        # Confidence calibration check (Slide 20)
        conf = str(prov.get("confidence") or "").lower()
        ev_type = str(prov.get("evidence_type") or "").lower()
        ext_m = str(prov.get("extraction_method") or "")
        if conf == "high":
            if ev_type in ("indirect_inference", "extrapolation") or EXTRAPOLATION_METHOD_RE.search(ext_m):
                errors.append(f"{np_id}: confidence='high' is forbidden for indirect_inference / extrapolation ({ext_m!r}); downgrade to 'medium' or 'low'.")
            elif uses_low_ocr or uses_defect_blk:
                warnings.append(f"{np_id}: confidence='high' on OCR <75 or encoding/stat-defect block; consider downgrading to 'medium'/'low' and flagging human_review_items.")

        # Deduplication & conflict detection
        sig = (normalize_term_key(subj), can_pred or normalize_term_key(raw_pred), normalize_term_key(obj))
        if sig in seen_sigs:
            warnings.append(f"{np_id}: duplicate assertion triple matching {nps[seen_sigs[sig]].get('nanopub_id')}; merge evidence_anchors instead of duplicating.")
        else:
            seen_sigs[sig] = idx
        pair_claims.setdefault((normalize_term_key(subj), can_pred or normalize_term_key(raw_pred)), []).append((idx, pol, obj_nums))

    conflicts_declared = result.get("conflicts") or []
    for (s_k, p_k), entries in pair_claims.items():
        if len(entries) >= 2 and not conflicts_declared:
            pols = {e[1] for e in entries if e[1] and e[1] != "unreported"}
            num_sets = {tuple(sorted(e[2])) for e in entries if e[2]}
            if len(pols) >= 2 or len(num_sets) >= 2:
                warnings.append(f"Potential undeclared conflict on ({s_k!r}, {p_k!r}): differing polarities/values {pols or num_sets}; log in conflicts[].")

    return {
        "passed": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "checks_summary": checks_summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge, audit strictly, and export Nanopublications (TriG/JSON-LD/MD).")
    parser.add_argument("--result", required=True)
    parser.add_argument("--merge", default=None)
    parser.add_argument("--case", default=None)
    parser.add_argument("--work-dir", default="/app/work")
    parser.add_argument("--doc", default=None)
    parser.add_argument("--export-dir", default=None, help="Directory for nanopublications.trig, .jsonld, and report.md (defaults to result.json parent).")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    res_p = Path(args.result)
    docs = _load_all_docs(Path(args.work_dir) if args.work_dir else None, Path(args.doc) if args.doc else None)

    if args.merge:
        _enrich_and_merge_patch(res_p, Path(args.merge), docs)

    report = audit_result(res_p, Path(args.case) if args.case else None, Path(args.work_dir) if args.work_dir else None, Path(args.doc) if args.doc else None)

    if res_p.exists():
        res_obj = json.loads(res_p.read_text(encoding="utf-8"))
        exp_dir = Path(args.export_dir) if args.export_dir else res_p.parent
        exp_info = export_all(res_obj, exp_dir)
        if not exp_info["trig_valid"]:
            report["errors"].extend(exp_info["trig_errors"])
            report["error_count"] = len(report["errors"])
            report["passed"] = False
        res_obj["exports"] = {
            "trig": exp_info["trig_path"],
            "jsonld": exp_info["jsonld_path"],
            "markdown": exp_info["markdown_path"],
            "trig_valid": exp_info["trig_valid"],
        }
        res_p.write_text(json.dumps(res_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        report["exports"] = res_obj["exports"]

    print(json.dumps(report, ensure_ascii=False, indent=2)[:18000])
    if args.strict and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
