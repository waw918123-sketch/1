#!/usr/bin/env python3
"""Extract supplied glossaries and disambiguated domain terminology candidates from case inputs and source text."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.dont_write_bytecode = True

from trans_common import normalize_ligatures


def _add_term(
    store: Dict[str, Dict[str, Any]],
    source: str,
    target: str,
    origin: str,
    note: str | None = None,
) -> None:
    src = re.sub(r"\s+", " ", normalize_ligatures(source or "")).strip().strip("`\"'*")
    tgt = re.sub(r"\s+", " ", normalize_ligatures(target or "")).strip().strip("`\"'*")
    if not src or not tgt or len(src) > 120 or len(tgt) > 120:
        return
    if src.lower() in ("source", "term", "english", "原文", "术语", "英文", "---"):
        return
    key = src.lower()
    if key in store and store[key].get("locked"):
        return
    store[key] = {
        "source": src,
        "target": tgt,
        "locked": True,
        "origin": origin,
        "note": note,
    }


def _parse_text_glossary(text: str, origin: str, store: Dict[str, Dict[str, Any]]) -> None:
    """Parse markdown tables, arrow/colon bullet lists, and inline glossary lines."""
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln or set(ln) <= set("|-: "):
            continue
        # Markdown table row: | source | target | optional note |
        if ln.startswith("|") and "|" in ln[1:]:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) >= 2 and not all(set(c) <= set("-: ") for c in cells):
                _add_term(store, cells[0], cells[1], origin, cells[2] if len(cells) >= 3 else None)
            continue
        # Bullet or plain mapping: '- credible interval -> 可信区间' / '* term: 译名'
        clean_ln = re.sub(r"^[-*•\d.)]+\s*", "", ln)
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9\-\s/()]{1,75}?)\s*(?:->|→|=>|=|：|:)\s*([^\s].{0,80})$", clean_ln)
        if m:
            src_cand, tgt_cand = m.group(1).strip(), m.group(2).strip()
            # Ensure target actually looks like a translation/CJK or explicit glossary target rather than prose metadata
            if any(0x4E00 <= ord(ch) <= 0x9FFF for ch in tgt_cand) or "glossary" in origin.lower():
                if src_cand.lower() not in ("case_id", "task", "note", "status", "source", "file", "url"):
                    _add_term(store, src_cand, tgt_cand, origin)
        # Prose patterns in task instructions: translate "X" as "Y" / 将 X 译为「Y」
        for pm in re.finditer(
            r"(?:translate\s+[\"'`]([A-Za-z][A-Za-z0-9\-\s]{2,60})[\"'`]\s+(?:as|to|into)\s+[\"'`「]([^\"'`」]{1,60})[\"'`」]|"
            r"(?:将|把)\s*[\"'`]?([A-Za-z][A-Za-z0-9\-\s]{2,60})[\"'`]?\s*(?:固定)?译[作为成]\s*[\"'`「]?([^\s\"'`」，。、；]{1,60})[\"'`」]?)",
            clean_ln,
            re.I,
        ):
            s_val = pm.group(1) or pm.group(3)
            t_val = pm.group(2) or pm.group(4)
            if s_val and t_val:
                _add_term(store, s_val, t_val, origin)


def _extract_from_structured_obj(obj: Any, origin: str, store: Dict[str, Dict[str, Any]]) -> None:
    """Recursively extract glossary/terminology entries from case.json dicts/lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if any(w in kl for w in ("glossary", "terminology", "term_map", "terms", "lexicon", "dict")):
                if isinstance(v, dict):
                    for sk, sv in v.items():
                        if isinstance(sv, str):
                            _add_term(store, sk, sv, f"{origin}:{k}")
                        elif isinstance(sv, dict) and sv.get("target"):
                            _add_term(store, sk, str(sv["target"]), f"{origin}:{k}", sv.get("note"))
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            s = item.get("source") or item.get("term") or item.get("en") or item.get("english")
                            t = item.get("target") or item.get("translation") or item.get("zh") or item.get("chinese")
                            if isinstance(s, str) and isinstance(t, str):
                                _add_term(store, s, t, f"{origin}:{k}", item.get("note"))
                        elif isinstance(item, str):
                            _parse_text_glossary(item, f"{origin}:{k}", store)
                elif isinstance(v, str):
                    _parse_text_glossary(v, f"{origin}:{k}", store)
            else:
                _extract_from_structured_obj(v, origin, store)
    elif isinstance(obj, list):
        for item in obj:
            _extract_from_structured_obj(item, origin, store)


def _parse_attachment(path: Path, store: Dict[str, Dict[str, Any]]) -> None:
    ext = path.suffix.lower()
    name_low = path.name.lower()
    if ext in (".csv", ".tsv"):
        delim = "\t" if ext == ".tsv" else ","
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                reader = csv.reader(f, delimiter=delim)
                for row in reader:
                    if len(row) >= 2:
                        _add_term(store, row[0], row[1], path.name, row[2] if len(row) >= 3 else None)
        except Exception:
            pass
    elif ext == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()) and any(
                w in name_low for w in ("glossary", "term", "dict")
            ):
                for k, v in data.items():
                    _add_term(store, k, v, path.name)
            else:
                _extract_from_structured_obj(data, path.name, store)
        except Exception:
            pass
    elif ext in (".txt", ".md"):
        if any(w in name_low for w in ("glossary", "term", "dict", "vocab")):
            _parse_text_glossary(path.read_text(encoding="utf-8", errors="replace"), path.name, store)
    elif ext == ".xlsx":
        try:
            with zipfile.ZipFile(path, "r") as zf:
                shared: List[str] = []
                if "xl/sharedStrings.xml" in zf.namelist():
                    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                    for si in root.findall(".//{*}si"):
                        shared.append("".join(t.text or "" for t in si.findall(".//{*}t")))
                for sname in sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
                    sroot = ET.fromstring(zf.read(sname))
                    for row_el in sroot.findall(".//{*}row"):
                        vals: List[str] = []
                        for c_el in row_el.findall("{*}c"):
                            t_attr = c_el.attrib.get("t")
                            v_el = c_el.find("{*}v")
                            if v_el is None or v_el.text is None:
                                continue
                            if t_attr == "s" and v_el.text.isdigit() and int(v_el.text) < len(shared):
                                vals.append(shared[int(v_el.text)])
                            else:
                                vals.append(v_el.text)
                        if len(vals) >= 2 and any(w in name_low for w in ("glossary", "term", "dict")):
                            _add_term(store, vals[0], vals[1], path.name, vals[2] if len(vals) >= 3 else None)
        except Exception:
            pass


def extract_glossary(
    case_data: Dict[str, Any],
    task_md_text: str,
    other_paths: List[Path],
    full_source_text: str = "",
) -> Dict[str, Any]:
    """Build locked glossary + candidate domain terms + context disambiguation hints."""
    locked: Dict[str, Dict[str, Any]] = {}
    if case_data:
        _extract_from_structured_obj(case_data, "case.json", locked)
    if task_md_text:
        _parse_text_glossary(task_md_text, "task.md", locked)
    for p in other_paths:
        if p.exists():
            _parse_attachment(p, locked)

    # Mine recurring multi-word noun phrases / abbreviations in full source text (for consistency lock)
    candidates: List[Dict[str, Any]] = []
    if full_source_text:
        phrases = re.findall(r"\b[A-Za-z][a-zA-Z\-]{2,}(?:\s+[a-zA-Z\-]{3,}){1,2}\b", full_source_text)
        cnt = Counter(p.lower() for p in phrases)
        for phrase, n in cnt.most_common(30):
            if n >= 3 and phrase not in locked and not re.match(r"^(?:in the|of the|on the|to the|for the|and the|with the|at the)\b", phrase):
                candidates.append({"source": phrase, "count": n, "status": "candidate_lock_one_target"})
                if len(candidates) >= 15:
                    break

        # Detect explicit contrast definitions in paper Methods (e.g. 'credible interval' vs 'confidence interval')
        distinctions: List[str] = []
        for pat in (
            r"[^.?!]{0,120}\bcredible\s+intervals?\b[^.?!]{0,160}\bconfidence\s+intervals?\b[^.?!]{0,120}\.",
            r"[^.?!]{0,120}\bdifferent\s+from\b[^.?!]{0,180}\.",
            r"[^.?!]{0,120}\bdefined\s+as\b[^.?!]{0,180}\.",
        ):
            for m in re.finditer(pat, full_source_text, re.I):
                distinctions.append(re.sub(r"\s+", " ", m.group(0)).strip()[:240])
                if len(distinctions) >= 6:
                    break
    else:
        distinctions = []

    return {
        "locked_terms": list(locked.values()),
        "candidate_terms": candidates,
        "context_disambiguations": distinctions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract supplied and candidate glossary terms for consistent translation.")
    parser.add_argument("--case", default=None, help="Path to case.json")
    parser.add_argument("--task", default=None, help="Path to task.md")
    parser.add_argument("--attach", action="append", default=[], help="Optional attachment path(s)")
    parser.add_argument("--source-json", default=None, help="Optional extracted doc_units.json for candidate term mining")
    parser.add_argument("--out", required=True, help="Output glossary.json path")
    args = parser.parse_args()

    case_data = json.loads(Path(args.case).read_text(encoding="utf-8")) if args.case and Path(args.case).exists() else {}
    task_txt = Path(args.task).read_text(encoding="utf-8", errors="replace") if args.task and Path(args.task).exists() else ""
    src_txt = ""
    if args.source_json and Path(args.source_json).exists():
        doc_u = json.loads(Path(args.source_json).read_text(encoding="utf-8"))
        src_txt = "\n".join(u.get("source_text", "") for u in doc_u.get("all_units_context", doc_u.get("units", [])))
    res = extract_glossary(case_data, task_txt, [Path(p) for p in args.attach], src_txt)
    out_p = Path(args.out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"locked_count": len(res["locked_terms"]), "candidate_count": len(res["candidate_terms"]), "out": str(out_p)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
