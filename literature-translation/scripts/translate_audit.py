#!/usr/bin/env python3
"""Bilingual translation merger and strict fidelity auditor: census coverage, glossary lock, inline tags, numbers, and clause-level negation polarity."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True

from trans_common import PANEL_RE, normalize_ligatures

EN_NEG_RE = re.compile(
    r"\b(?:no|not|never|neither|nor|without|none|nothing|nobody|nowhere|lack(?:s|ed|ing)?|absence|fail(?:s|ed)?\s+to|unable\s+to|cannot|can't|did\s+not|does\s+not|do\s+not|won't|would\s+not|is\s+not|are\s+not|was\s+not|were\s+not)\b",
    re.I,
)
ZH_NEG_RE = re.compile(r"(?:不[会能要易曾一定仅必用再可增减升高低变同均]|未曾?|没有|并非?|无[法法需关疑论]|并非|而非|未能|缺少|缺乏|不\w{0,2}(?:增加|减少|上升|下降|提高|降低|改变|影响|含|存在))")
EN_PIVOT_SPLIT_RE = re.compile(r"(?:[,;:]|\b(?:but|whereas|however|although|though|yet|while|instead)\b)", re.I)
ZH_PIVOT_SPLIT_RE = re.compile(r"(?:[，；：]|但是?|然而|却|而|不过|相反)")


def _schema_allows_type(type_spec: Any, expected: str) -> bool:
    if isinstance(type_spec, str):
        return type_spec == expected
    if isinstance(type_spec, list):
        return expected in type_spec
    return False


def _collect_required_fields(case_data: Dict[str, Any]) -> List[str]:
    contract = case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output")
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


def _extract_inline_tags(text: str) -> Counter[str]:
    tags = re.findall(r"</?(?:sub|sup|italic|bold|sc|monospace|underline|overline|xref|ext-link)\b[^>]*>", text or "", re.I)
    # Normalize tags by tag name + ref-type/rid if present
    return Counter(re.sub(r"\s+", " ", t.lower().strip()) for t in tags)


def _extract_scripts(text: str) -> Counter[str]:
    return Counter(re.findall(r"(?:\^\{[^}]+\}|_\{[^}]+\})", text or ""))


def _extract_numbers(text: str) -> Counter[str]:
    # Capture pure numeric values cleanly (preserving decimals and integers, normalized without trailing commas)
    nums = re.findall(r"(?<![A-Za-z_])\d+(?:[.,]\d+)?%?", text or "")
    cleaned = [n.replace(",", "") for n in nums]
    return Counter(cleaned)


def _check_clause_negation_flip(src: str, tgt: str) -> Optional[str]:
    """Detect negation scope swaps across contrastive clauses (e.g. 'does not increase A, but reduces B' -> '增加A，但不减少B')."""
    if not src or not tgt:
        return None
    src_clauses = [c.strip() for c in EN_PIVOT_SPLIT_RE.split(src) if c and c.strip()]
    tgt_clauses = [c.strip() for c in ZH_PIVOT_SPLIT_RE.split(tgt) if c and c.strip()]
    if len(src_clauses) < 2 or len(src_clauses) != len(tgt_clauses):
        # Fallback: compare overall polarity parity when simple 2-part contrast differs cleanly
        if len(src_clauses) >= 2 and len(tgt_clauses) >= 2:
            s_vec = [1 if EN_NEG_RE.search(c) else 0 for c in src_clauses[:2]]
            t_vec = [1 if ZH_NEG_RE.search(c) else 0 for c in tgt_clauses[:2]]
            if sum(s_vec) == sum(t_vec) and s_vec != t_vec:
                return f"clause negation polarity flipped: source={s_vec} vs translation={t_vec}"
        return None
    s_vec = [1 if EN_NEG_RE.search(c) else 0 for c in src_clauses]
    t_vec = [1 if ZH_NEG_RE.search(c) else 0 for c in tgt_clauses]
    if s_vec != t_vec and (sum(s_vec) > 0 or sum(t_vec) > 0):
        return f"clause negation polarity mismatch: source={s_vec} vs translation={t_vec}"
    return None


def merge_patch(base_path: Path, patch_path: Path) -> Dict[str, Any]:
    """Incrementally merge batch translation patches into baseline result.json without losing source locators or census."""
    base = json.loads(base_path.read_text(encoding="utf-8")) if base_path.exists() else {}
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    if not isinstance(base, dict) or not isinstance(patch, dict):
        raise ValueError("Both result.json and patch.json must be JSON objects.")

    merged = dict(base)
    unit_updates = patch.get("translations") or patch.get("units") or {}
    # Index patch unit translations by unit_id and sentence_id
    by_uid: Dict[str, Any] = {}
    by_sid: Dict[str, str] = {}
    if isinstance(unit_updates, dict):
        for k, v in unit_updates.items():
            if ".S" in k:
                by_sid[k] = str(v)
            else:
                by_uid[k] = v
    elif isinstance(unit_updates, list):
        for item in unit_updates:
            if not isinstance(item, dict):
                continue
            uid = item.get("unit_id")
            if uid:
                by_uid[uid] = item
            for s in item.get("sentences", []):
                if isinstance(s, dict) and s.get("sentence_id") and s.get("translation") is not None:
                    by_sid[s["sentence_id"]] = str(s["translation"])

    # Also allow explicit patch["sentences"] dict {sentence_id: translation}
    if isinstance(patch.get("sentences"), dict):
        for sid, val in patch["sentences"].items():
            by_sid[sid] = str(val)

    cur_trans = merged.get("translations", [])
    if isinstance(cur_trans, list):
        for u in cur_trans:
            uid = u.get("unit_id")
            upd = by_uid.get(uid)
            if isinstance(upd, str):
                u["translation"] = upd
            elif isinstance(upd, dict):
                if upd.get("translation") is not None:
                    u["translation"] = str(upd["translation"])
                for s in upd.get("sentences", []):
                    if isinstance(s, dict) and s.get("sentence_id") and s.get("translation") is not None:
                        by_sid[s["sentence_id"]] = str(s["translation"])

            sent_list = u.get("sentences", [])
            sent_texts: List[str] = []
            for s in sent_list:
                sid = s.get("sentence_id")
                if sid in by_sid:
                    s["translation"] = by_sid[sid]
                if isinstance(s.get("translation"), str) and s["translation"].strip():
                    sent_texts.append(s["translation"].strip())
            # Auto-assemble full unit translation from translated sentences if unit-level string missing
            if (not u.get("translation")) and sent_list and len(sent_texts) == len(sent_list):
                u["translation"] = "".join(sent_texts)

        # Keep synced mirror aliases if contract requested them
        for alias in ("bilingual_segments", "paragraphs", "segments", "translated_segments"):
            if alias in merged:
                merged[alias] = cur_trans

    # Merge top-level non-locked analytical fields cleanly
    locked = {"case_id", "census", "translations", "bilingual_segments", "paragraphs", "segments", "translated_segments"}
    for k, v in patch.items():
        if k in locked or k in ("units", "sentences"):
            continue
        if k == "glossary" and isinstance(base.get("glossary"), list) and base["glossary"] and (v is None or v == []):
            continue
        if k == "change_records" and isinstance(base.get("change_records"), list) and isinstance(v, list):
            merged[k] = list(base[k]) + list(v)
            continue
        if k == "human_review_items" and isinstance(base.get("human_review_items"), list) and isinstance(v, list):
            merged[k] = list(base[k]) + list(v)
            continue
        merged[k] = v

    # Auto-update status to completed once 100% of translations are filled (unless explicitly set)
    if merged.get("status") == "partial" and "status" not in patch and cur_trans:
        done = sum(1 for u in cur_trans if isinstance(u.get("translation"), str) and u["translation"].strip())
        if done == len(cur_trans):
            merged["status"] = "completed"

    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def audit_translation(
    result_path: Path,
    case_path: Optional[Path],
    manifest_path: Optional[Path],
    glossary_path: Optional[Path],
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    unit_issues: List[Dict[str, Any]] = []

    res = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(res, dict):
        return {"passed": False, "errors": ["result.json root must be a JSON object"], "warnings": [], "unit_issues": []}

    raw_status = str(res.get("status") or "").strip().lower()
    if raw_status in ("nonsense", "todo", "placeholder"):
        errors.append(f"invalid placeholder status: {raw_status!r}")

    if case_path and case_path.exists():
        cdata = json.loads(case_path.read_text(encoding="utf-8"))
        exp_cid = cdata.get("case_id")
        if exp_cid and res.get("case_id") != exp_cid:
            errors.append(f"case_id mismatch: expected {exp_cid!r}, got {res.get('case_id')!r}")
        contract = cdata.get("output_contract") or cdata.get("output_schema") or {}
        props = contract.get("properties") if isinstance(contract, dict) else None
        for f in _collect_required_fields(cdata):
            if f not in res:
                errors.append(f"missing required output_contract field: {f!r}")
            else:
                fspec = props.get(f) if isinstance(props, dict) else None
                allows_null = isinstance(fspec, dict) and _schema_allows_type(fspec.get("type"), "null")
                if res[f] == "" or (res[f] is None and not allows_null):
                    warnings.append(f"required field {f!r} is null/empty")
        if isinstance(props, dict):
            for fk, fspec in props.items():
                if fk in res and isinstance(fspec, dict) and res[fk] is None and not _schema_allows_type(fspec.get("type"), "null"):
                    warnings.append(f"field {fk!r} is null while schema type={fspec.get('type')!r}")

    # Independent Census check (Slide 14: denominator MUST come from independent raw census)
    expected_units = 0
    if manifest_path and manifest_path.exists():
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
        census = man.get("census") or {}
        expected_units = int(census.get("independent_raw_in_scope_count") or census.get("extracted_in_scope_count") or 0)
        if not census.get("census_exact_match", True):
            errors.append("manifest census mismatch: extractor dropped in-scope units relative to independent raw census")

    locked_terms: List[Dict[str, Any]] = []
    if glossary_path and glossary_path.exists():
        gl = json.loads(glossary_path.read_text(encoding="utf-8"))
        locked_terms = [t for t in gl.get("locked_terms", []) if t.get("source") and t.get("target")]
    elif isinstance(res.get("glossary"), list):
        locked_terms = [t for t in res["glossary"] if isinstance(t, dict) and t.get("source") and t.get("target")]

    translations = res.get("translations") or []
    translated_count = 0
    for u in translations:
        if not isinstance(u, dict):
            continue
        uid = u.get("unit_id")
        src = u.get("source_text") or ""
        tgt = u.get("translation")
        if tgt is None or not str(tgt).strip():
            unit_issues.append({"unit_id": uid, "severity": "error", "issue": "missing/null translation"})
            continue
        tgt_s = str(tgt).strip()
        translated_count += 1
        if tgt_s.lower() in ("null", "todo", "tbd", "nonsense", "n/a") or (len(src) > 25 and tgt_s == src.strip() and not re.match(r"^(?:Table|Fig\.?|S\d+)\s+\d+$", src.strip())):
            unit_issues.append({"unit_id": uid, "severity": "warning", "issue": "untranslated or placeholder value"})
            warnings.append(f"{uid}: suspicious placeholder/untranslated text")

        # 1. Locked glossary verification
        for term in locked_terms:
            tsrc, ttgt = term["source"], term["target"]
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(tsrc)}s?(?![A-Za-z0-9])", src, re.I):
                if ttgt not in tgt_s and ttgt.lower() not in tgt_s.lower():
                    msg = f"glossary violation: {tsrc!r} -> expected {ttgt!r}"
                    unit_issues.append({"unit_id": uid, "severity": "error", "issue": msg})
                    errors.append(f"{uid}: {msg}")

        # 2. Inline XML markup & script verification (<sub>, <italic>, ^{}, _{})
        src_tags, tgt_tags = _extract_inline_tags(src), _extract_inline_tags(tgt_s)
        if src_tags != tgt_tags:
            msg = f"inline tag mismatch: src={dict(src_tags)} tgt={dict(tgt_tags)}"
            unit_issues.append({"unit_id": uid, "severity": "error", "issue": msg})
            errors.append(f"{uid}: {msg}")
        src_scr, tgt_scr = _extract_scripts(src), _extract_scripts(tgt_s)
        if src_scr != tgt_scr:
            msg = f"script marker mismatch: src={dict(src_scr)} tgt={dict(tgt_scr)}"
            unit_issues.append({"unit_id": uid, "severity": "warning", "issue": msg})
            warnings.append(f"{uid}: {msg}")

        # 3. Panel labels (A2), (B1) preservation
        src_panels = Counter(PANEL_RE.findall(src))
        tgt_panels = Counter(PANEL_RE.findall(tgt_s))
        for p_lbl, cnt in src_panels.items():
            if tgt_panels[p_lbl] < cnt:
                msg = f"missing panel label ({p_lbl})"
                unit_issues.append({"unit_id": uid, "severity": "error", "issue": msg})
                errors.append(f"{uid}: {msg}")

        # 4. Numeric preservation
        src_nums, tgt_nums = _extract_numbers(src), _extract_numbers(tgt_s)
        missing_nums = [k for k, v in src_nums.items() if tgt_nums[k] < v]
        if missing_nums:
            msg = f"missing numeric tokens {missing_nums}"
            unit_issues.append({"unit_id": uid, "severity": "warning", "issue": msg})
            warnings.append(f"{uid}: {msg}")

        # 5. Sentence-level negation polarity & scope check (Slide 14 catch!)
        sents = u.get("sentences") or []
        if sents and all(isinstance(s.get("translation"), str) and s["translation"].strip() for s in sents):
            for s in sents:
                flip_err = _check_clause_negation_flip(s.get("source_text", ""), s.get("translation", ""))
                if flip_err:
                    msg = f"{s.get('sentence_id')}: {flip_err}"
                    unit_issues.append({"unit_id": uid, "severity": "error", "issue": msg})
                    errors.append(msg)
        else:
            flip_err = _check_clause_negation_flip(src, tgt_s)
            if flip_err:
                unit_issues.append({"unit_id": uid, "severity": "error", "issue": flip_err})
                errors.append(f"{uid}: {flip_err}")

    denom = max(expected_units, len(translations))
    coverage = round(translated_count / max(1, denom), 4) if denom > 0 else 1.0
    if raw_status not in ("blocked", "failed") and denom > 0 and translated_count < denom:
        errors.append(f"incomplete translation coverage: {translated_count}/{denom} ({coverage:.1%})")

    return {
        "passed": len(errors) == 0,
        "coverage": f"{translated_count}/{denom}",
        "coverage_ratio": coverage,
        "errors": errors,
        "warnings": warnings,
        "unit_issue_count": len(unit_issues),
        "unit_issues": unit_issues[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge translation patches and run strict bilingual fidelity/census/negation audit.")
    parser.add_argument("--result", required=True, help="Path to /app/output/result.json.")
    parser.add_argument("--merge", default=None, help="Optional patch JSON to merge incrementally before auditing.")
    parser.add_argument("--case", default=None, help="Optional /input/case.json.")
    parser.add_argument("--manifest", default=None, help="Optional /app/work/manifest.json.")
    parser.add_argument("--glossary", default=None, help="Optional /app/work/glossary.json.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any audit errors exist.")
    args = parser.parse_args()

    res_p = Path(args.result)
    if args.merge:
        merge_patch(res_p, Path(args.merge))

    if not res_p.exists():
        print(json.dumps({"passed": False, "errors": [f"result.json not found: {res_p}"]}), file=sys.stderr)
        return 2

    report = audit_translation(
        res_p,
        Path(args.case) if args.case else None,
        Path(args.manifest) if args.manifest else None,
        Path(args.glossary) if args.glossary else None,
    )
    out_txt = json.dumps(report, ensure_ascii=False, indent=2)
    if len(out_txt) > 24000:
        slim = dict(report)
        slim["unit_issues"] = report["unit_issues"][:15]
        slim["truncated"] = True
        out_txt = json.dumps(slim, ensure_ascii=False, indent=2)
    print(out_txt)
    if args.strict and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
