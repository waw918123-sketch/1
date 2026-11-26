#!/usr/bin/env python3
"""Independent-census + structured unit/sentence extractor for Publisher XML (JATS/PLOS) and Scientific PDFs."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.dont_write_bytecode = True

from trans_common import (
    _is_cjk,
    detect_encoding_defects,
    discover_capabilities,
    import_pymupdf,
    normalize_for_match,
    normalize_ligatures,
    serialize_xml_inner,
    split_sentences_preserving_markup,
    strip_ns,
)

SKIP_CONTAINER_TAGS = {
    "ref-list", "contrib-group", "aff", "journal-meta", "history", "permissions",
    "counts", "funding-group", "custom-meta-group", "author-notes", "ack",
}

CAPTION_RE = re.compile(
    r"^(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Figure|Fig\.?|Table|Tab\.?|Scheme|Algorithm|Listing|附图|附表|图|表)\s*[A-Za-z]?\d+(?:[.\-]\d+)*[A-Za-z]?(?=\s*(?:[:：.|—–\-]|\(|（|\s|$))",
    re.I,
)
_CAPTION_CONT_RE = re.compile(r"^\s*(?:\((?:continued|cont\.?|cont'd)\)|（续）|\(续\))\s*", re.I)
_CAPTION_PROSE_VERB_RE = re.compile(
    r"^\s*(?:is|are|was|were|has|have|had|contains?|shows?|demonstrates?|illustrates?|presents?|summarizes?|compares?|lists?|depicts?|plots?|gives?|reports?|provides?|indicates?|suggests?|reveals?|highlights?|describes?|displays?|can|may|will|also|表明|显示|给出|展示|说明|列出|总结|揭示)\b",
    re.I,
)

SYMBOL_PUA_MAP = {
    0xF044: "Δ", 0xF046: "Φ", 0xF047: "Γ", 0xF048: "Η", 0xF04C: "Λ", 0xF050: "Π",
    0xF051: "Θ", 0xF053: "Σ", 0xF057: "Ω", 0xF058: "Ξ", 0xF059: "Ψ",
    0xF061: "α", 0xF062: "β", 0xF063: "χ", 0xF064: "δ", 0xF065: "ε", 0xF066: "φ",
    0xF067: "γ", 0xF068: "η", 0xF069: "ι", 0xF06B: "κ", 0xF06C: "λ", 0xF06D: "μ",
    0xF06E: "ν", 0xF06F: "ο", 0xF070: "π", 0xF071: "θ", 0xF072: "ρ", 0xF073: "σ",
    0xF074: "τ", 0xF075: "υ", 0xF077: "ω", 0xF078: "ξ", 0xF079: "ψ", 0xF07A: "ζ",
    0xF0B1: "±", 0xF0A3: "≤", 0xF0B3: "≥", 0xF0B4: "×", 0xF0B8: "÷", 0xF0BB: "≈",
    0xF0B9: "≠", 0xF0A5: "∞", 0xF0B0: "°", 0xF0AE: "→", 0xF0AC: "←", 0xF0D7: "·",
}
SYMBOL_ASCII_MAP = {chr(k - 0xF000): v for k, v in SYMBOL_PUA_MAP.items() if 0xF041 <= k <= 0xF07A}


def _decode_symbol(txt: str, font_name: str) -> str:
    if "symbol" not in font_name.lower():
        return txt
    has_pua = any(0xF000 <= ord(ch) <= 0xF0FF for ch in txt)
    allow_ascii = (not has_pua) and (not re.search(r"[A-Za-z]{2,}", txt))
    return "".join(
        SYMBOL_PUA_MAP[ord(ch)] if ord(ch) in SYMBOL_PUA_MAP else (SYMBOL_ASCII_MAP[ch] if allow_ascii and ch in SYMBOL_ASCII_MAP else ch)
        for ch in txt
    )


def _is_caption_text(txt: str) -> bool:
    st = txt.strip()
    m = CAPTION_RE.match(st)
    if not m:
        return False
    rest = st[m.end():]
    if not rest.strip():
        return True
    cm = _CAPTION_CONT_RE.match(rest)
    if cm:
        rest = rest[cm.end():]
        if not rest.strip():
            return True
    ch0 = rest.lstrip()[:1]
    if not ch0 or ch0 in (".", ":", "：", "|", "—", "–", "-"):
        return True
    if _CAPTION_PROSE_VERB_RE.match(rest.lstrip()):
        return False
    if re.match(r"^(?:附图|附表|图|表)", st) and _is_cjk(ch0):
        return len(st) <= 180
    return len(rest.split()) <= 5 and not st.endswith((".", "。"))


def _parse_scope_spec(scope_json: Optional[str]) -> Dict[str, Any]:
    if not scope_json:
        return {}
    try:
        return json.loads(scope_json)
    except Exception:
        return {}


def _extract_xml_units(xml_path: Path, scope: Dict[str, Any]) -> Dict[str, Any]:
    """Extract units with independent raw-node census so supplementary labels/tables/captions cannot drop silently."""
    raw_bytes = xml_path.read_bytes()
    root = ET.fromstring(raw_bytes)

    scope_ids = {s.lower() for s in scope.get("target_ids", []) if s}
    scope_tags = {s.lower() for s in scope.get("target_roles", []) if s}  # e.g. {"label", "title", "caption"}
    include_refs = bool(scope.get("include_references", False))

    all_units: List[Dict[str, Any]] = []
    unit_counter = 0

    def _walk(
        el: ET.Element,
        loc_parts: List[str],
        anc_ids: List[str],
        anc_tags: List[str],
        in_skipped: bool,
        section_title: Optional[str],
    ) -> None:
        nonlocal unit_counter
        tag = strip_ns(el.tag).lower()
        if tag in SKIP_CONTAINER_TAGS and not (include_refs and tag == "ref-list"):
            return
        if in_skipped:
            return

        eid = el.attrib.get("id") or ""
        cur_ids = anc_ids + ([eid] if eid else [])
        cur_tags = anc_tags + [tag]

        # Track section title for context
        cur_sec = section_title
        if tag in ("sec", "abstract", "app", "supplementary-material"):
            t_el = el.find("title")
            if t_el is not None:
                t_txt = serialize_xml_inner(t_el)
                if t_txt:
                    cur_sec = t_txt
            elif tag == "abstract":
                cur_sec = "Abstract"

        # Count child sibling occurrences cleanly for JATS XPath locator like fig[1]/caption[1]/p[1]
        child_counts: Counter[str] = Counter()
        leaf_tags = {"article-title", "alt-title", "title", "label", "p"}
        if tag in leaf_tags:
            # Guard against nested <p> inside <p> (unlikely but handle cleanly)
            has_block_children = any(strip_ns(ch.tag).lower() in leaf_tags for ch in el)
            if not has_block_children:
                txt = serialize_xml_inner(el)
                if txt:
                    unit_counter += 1
                    uid = f"P{unit_counter:05d}"
                    locator = "/".join(loc_parts) or tag
                    role = tag
                    if "caption" in cur_tags:
                        role = "caption"
                    elif "fig" in cur_tags and tag == "label":
                        role = "fig_label"
                    elif "table-wrap" in cur_tags and tag == "label":
                        role = "table_label"
                    elif "supplementary-material" in cur_tags or "supplementary" in (el.attrib.get("sec-type", "").lower()):
                        role = f"supplementary_{tag}"

                    # Scope matching: if target_ids given, require matching ancestor/self id (e.g. g001, g002)
                    in_scope = True
                    if scope_ids:
                        if not any(cid.lower() in scope_ids for cid in cur_ids):
                            in_scope = False
                    if in_scope and scope_tags:
                        # Check whether element role or any relevant tag/ancestor matches requested roles
                        if not (
                            tag in scope_tags
                            or role in scope_tags
                            or ("caption" in scope_tags and "caption" in cur_tags)
                            or ("title" in scope_tags and tag in ("title", "article-title"))
                            or ("label" in scope_tags and tag == "label")
                        ):
                            in_scope = False

                    raw_sents = split_sentences_preserving_markup(txt)
                    sents = [
                        {
                            "sentence_id": f"{uid}.S{s_i + 1:03d}",
                            "unit_id": uid,
                            "locator": f"{locator}#S{s_i + 1:03d}",
                            "source_text": st,
                        }
                        for s_i, st in enumerate(raw_sents)
                    ]
                    all_units.append({
                        "unit_id": uid,
                        "locator": locator,
                        "element_tag": tag,
                        "role": role,
                        "section": cur_sec,
                        "ancestor_ids": cur_ids,
                        "in_scope": in_scope,
                        "source_text": txt,
                        "sentences": sents,
                        "sentence_count": len(sents),
                        "char_count": len(txt),
                    })
                return

        # Standalone text directly inside <caption> or <supplementary-material> without child <p>/<title>/<label>
        if tag in ("caption", "supplementary-material") and not any(
            strip_ns(ch.tag).lower() in leaf_tags | {"caption"} for ch in el
        ):
            txt = serialize_xml_inner(el)
            if txt:
                unit_counter += 1
                uid = f"P{unit_counter:05d}"
                locator = "/".join(loc_parts) or tag
                in_scope = True
                if scope_ids and not any(cid.lower() in scope_ids for cid in cur_ids):
                    in_scope = False
                if in_scope and scope_tags and tag not in scope_tags and "caption" not in scope_tags:
                    in_scope = False
                raw_sents = split_sentences_preserving_markup(txt)
                sents = [
                    {"sentence_id": f"{uid}.S{s_i + 1:03d}", "unit_id": uid, "locator": f"{locator}#S{s_i + 1:03d}", "source_text": st}
                    for s_i, st in enumerate(raw_sents)
                ]
                all_units.append({
                    "unit_id": uid,
                    "locator": locator,
                    "element_tag": tag,
                    "role": tag,
                    "section": cur_sec,
                    "ancestor_ids": cur_ids,
                    "in_scope": in_scope,
                    "source_text": txt,
                    "sentences": sents,
                    "sentence_count": len(sents),
                    "char_count": len(txt),
                })
                return

        for ch in el:
            ctag = strip_ns(ch.tag).lower()
            child_counts[ctag] += 1
            idx = child_counts[ctag]
            # Keep compact JATS path like fig[1]/caption[1]/p[1]
            step = f"{ctag}[{idx}]" if ctag not in ("article", "front", "body", "back") else ctag
            next_loc = (loc_parts + [step]) if ctag != "article" else loc_parts
            _walk(ch, next_loc, cur_ids, cur_tags, False, cur_sec)

    _walk(root, [], [], [], False, None)

    # Independent raw element census (verify zero missed in-scope leaf nodes — Slide 14 safeguard!)
    raw_census_ids: List[str] = []
    raw_idx = 0
    def _census_walk(el: ET.Element, anc_ids: List[str], anc_tags: List[str]) -> None:
        nonlocal raw_idx
        tag = strip_ns(el.tag).lower()
        if tag in SKIP_CONTAINER_TAGS and not (include_refs and tag == "ref-list"):
            return
        eid = el.attrib.get("id") or ""
        c_ids = anc_ids + ([eid] if eid else [])
        c_tags = anc_tags + [tag]
        leaf_tags = {"article-title", "alt-title", "title", "label", "p"}
        if tag in leaf_tags and not any(strip_ns(ch.tag).lower() in leaf_tags for ch in el):
            if serialize_xml_inner(el):
                raw_idx += 1
                ok = True
                if scope_ids and not any(cid.lower() in scope_ids for cid in c_ids):
                    ok = False
                if ok and scope_tags and not (
                    tag in scope_tags or ("caption" in scope_tags and "caption" in c_tags) or ("title" in scope_tags and tag in ("title", "article-title")) or ("label" in scope_tags and tag == "label")
                ):
                    ok = False
                if ok:
                    raw_census_ids.append(f"P{raw_idx:05d}")
            return
        if tag in ("caption", "supplementary-material") and not any(strip_ns(ch.tag).lower() in leaf_tags | {"caption"} for ch in el):
            if serialize_xml_inner(el):
                raw_idx += 1
                ok = True
                if scope_ids and not any(cid.lower() in scope_ids for cid in c_ids):
                    ok = False
                if ok and scope_tags and tag not in scope_tags and "caption" not in scope_tags:
                    ok = False
                if ok:
                    raw_census_ids.append(f"P{raw_idx:05d}")
            return
        for ch in el:
            _census_walk(ch, c_ids, c_tags)

    _census_walk(root, [], [])

    in_scope_units = [u for u in all_units if u["in_scope"]]
    return {
        "file": str(xml_path),
        "format": "xml",
        "status": "ok",
        "scope_applied": scope,
        "census": {
            "total_document_units": len(all_units),
            "independent_raw_in_scope_count": len(raw_census_ids),
            "extracted_in_scope_count": len(in_scope_units),
            "census_exact_match": [u["unit_id"] for u in in_scope_units] == raw_census_ids,
            "in_scope_sentence_count": sum(u["sentence_count"] for u in in_scope_units),
        },
        "units": in_scope_units,
        "all_units_context": all_units,
    }


def _detect_column_gutters(blocks: List[Dict[str, Any]], content_x0: float, content_x1: float) -> List[float]:
    width = content_x1 - content_x0
    if len(blocks) < 2 or width <= 50.0:
        return []
    bins = 200
    occ = [0] * bins
    for b in blocks:
        x0, x1 = float(b["bbox"][0]), float(b["bbox"][2])
        if x1 - x0 > 6.0:
            x0 += 1.0
            x1 -= 1.0
        b0 = max(0, min(bins - 1, int(((x0 - content_x0) / width) * bins)))
        b1 = max(0, min(bins - 1, int(((x1 - content_x0) / width) * bins)))
        for idx in range(b0, b1 + 1):
            occ[idx] += 1
    gutters: List[float] = []
    run_start: Optional[int] = None
    for i in range(int(0.10 * bins), int(0.90 * bins) + 1):
        if occ[i] == 0:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and (i - run_start) >= 2:
                if ((i - run_start) / bins) * width >= 6.0 and any(occ[j] > 0 for j in range(0, run_start)) and any(occ[j] > 0 for j in range(i, bins)):
                    gutters.append(content_x0 + ((run_start + i) / 2.0) / bins * width)
            run_start = None
    return gutters[:2]


def _split_y_bands(blocks: List[Dict[str, Any]], min_y_gap: float = 1.5) -> List[List[Dict[str, Any]]]:
    if len(blocks) <= 1:
        return [list(blocks)] if blocks else []
    by_y = sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))
    bands: List[Tuple[float, float, List[Dict[str, Any]]]] = []
    for b in by_y:
        y0, y1 = float(b["bbox"][1]), float(b["bbox"][3])
        if bands and y0 <= bands[-1][1] + min_y_gap:
            bands[-1][2].append(b)
            bands[-1] = (bands[-1][0], max(bands[-1][1], y1), bands[-1][2])
        else:
            bands.append((y0, y1, [b]))
    return [bl for _, _, bl in bands]


def _xy_cut_recursive(blocks: List[Dict[str, Any]], page_w: float, col_offset: int = 0, allow_y_cut: bool = True, depth: int = 0) -> List[Dict[str, Any]]:
    if not blocks:
        return []
    if len(blocks) == 1 or depth >= 6:
        for b in blocks:
            bw = b["bbox"][2] - b["bbox"][0]
            b["column"] = 0 if (col_offset == 0 and bw >= 0.62 * page_w) else max(1, col_offset or 1)
        return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))

    bx0, bx1 = min(b["bbox"][0] for b in blocks), max(b["bbox"][2] for b in blocks)
    gutters = _detect_column_gutters(blocks, bx0, bx1)
    if gutters:
        cols: List[List[Dict[str, Any]]] = [[] for _ in range(len(gutters) + 1)]
        for b in blocks:
            cx = 0.5 * (b["bbox"][0] + b["bbox"][2])
            c_idx = sum(1 for g in gutters if cx > g)
            cols[c_idx].append(b)
        non_empty = [c for c in cols if c]
        if len(non_empty) >= 2:
            out: List[Dict[str, Any]] = []
            for seq_i, cb in enumerate(non_empty):
                sub_col = (col_offset + seq_i) if col_offset > 0 else (seq_i + 1)
                out.extend(_xy_cut_recursive(cb, page_w, col_offset=sub_col, allow_y_cut=True, depth=depth + 1))
            return out

    if allow_y_cut:
        y_bands = _split_y_bands(blocks)
        if len(y_bands) >= 2:
            merged_bands: List[List[Dict[str, Any]]] = [y_bands[0]]
            for nxt in y_bands[1:]:
                cur = merged_bands[-1]
                cur_g = _detect_column_gutters(cur, min(b["bbox"][0] for b in cur), max(b["bbox"][2] for b in cur))
                comb = cur + nxt
                comb_g = _detect_column_gutters(comb, min(b["bbox"][0] for b in comb), max(b["bbox"][2] for b in comb))
                if cur_g and comb_g and len(comb_g) >= len(cur_g):
                    nxt_g = _detect_column_gutters(nxt, min(b["bbox"][0] for b in nxt), max(b["bbox"][2] for b in nxt))
                    y_gap = min(b["bbox"][1] for b in nxt) - max(b["bbox"][3] for b in cur)
                    if not any(b["bbox"][0] < g - 2.0 and b["bbox"][2] > g + 2.0 for b in nxt for g in comb_g) and (nxt_g or y_gap <= 18.0):
                        merged_bands[-1] = comb
                        continue
                merged_bands.append(nxt)
            out = []
            for band in merged_bands:
                out.extend(_xy_cut_recursive(band, page_w, col_offset=col_offset, allow_y_cut=False, depth=depth + 1))
            return out

    for b in blocks:
        b["column"] = max(1, col_offset or 1)
    return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))


def _span_text_with_scripts(spans: List[Dict[str, Any]]) -> Tuple[str, str, bool]:
    if not spans:
        return "", "", False
    char_sizes: List[float] = []
    non_flag: List[float] = []
    for s in spans:
        st = s.get("text", "").strip()
        sfs = float(s.get("size", 0.0))
        if not st or sfs <= 0:
            continue
        rep = min(len(st), 6)
        char_sizes.extend([sfs] * rep)
        if not (int(s.get("flags", 0)) & 1):
            non_flag.extend([sfs] * rep)
    pool = sorted(non_flag or char_sizes)
    med_fs = pool[min(len(pool) - 1, int(0.85 * len(pool)))] if pool else 10.0
    base_ys = [0.5 * (s["bbox"][1] + s["bbox"][3]) for s in spans if s.get("text", "").strip() and float(s.get("size", med_fs)) >= 0.88 * med_fs and not (int(s.get("flags", 0)) & 1)]
    med_cy = statistics.median(base_ys) if base_ys else 0.0

    raw_parts: List[str] = []
    fmt_runs: List[Tuple[str, str]] = []
    has_unr = False
    for s in spans:
        txt = _decode_symbol(normalize_ligatures(s.get("text", "")), str(s.get("font", "")))
        if not txt:
            continue
        if detect_encoding_defects(txt)["has_defects"]:
            has_unr = True
        raw_parts.append(txt)
        fs = float(s.get("size", med_fs))
        flags = int(s.get("flags", 0))
        cy = 0.5 * (s["bbox"][1] + s["bbox"][3])
        st = txt.strip()
        is_sup = bool(flags & 1) or (fs <= 0.80 * med_fs and med_cy > 0 and cy < med_cy - 0.14 * med_fs)
        is_sub = (not is_sup) and (fs <= 0.80 * med_fs and med_cy > 0 and cy > med_cy + 0.14 * med_fs)
        if not st and (is_sup or is_sub) and fmt_runs and fmt_runs[-1][0] in ("super", "sub"):
            continue
        if st and is_sup and len(st) <= 12:
            kind, val = "super", st
        elif st and is_sub and len(st) <= 12:
            kind, val = "sub", st
        else:
            kind, val = "normal", txt
        if fmt_runs and fmt_runs[-1][0] == kind and kind in ("super", "sub") and len(fmt_runs[-1][1]) + len(val) <= 18:
            fmt_runs[-1] = (kind, fmt_runs[-1][1] + val)
        else:
            fmt_runs.append((kind, val))
    fmt_str = "".join(f"^{{{v}}}" if k == "super" else (f"_{{{v}}}" if k == "sub" else v) for k, v in fmt_runs)
    return "".join(raw_parts), fmt_str, has_unr


def _join_lines(lines_info: List[Dict[str, str]]) -> str:
    out = ""
    for item in lines_info:
        seg = item["fmt"].rstrip()
        if not seg:
            continue
        if not out:
            out = seg
            continue
        if out.endswith("-") and len(out) >= 2 and out[-2].isalpha() and seg[0].islower():
            out = out[:-1] + seg
        elif _is_cjk(out[-1]) and _is_cjk(seg[0]):
            out = out + seg
        else:
            out = out + " " + seg
    return out.strip()


def _merge_bbox(boxes: List[List[float]]) -> List[float]:
    return [
        round(min(b[0] for b in boxes), 2),
        round(min(b[1] for b in boxes), 2),
        round(max(b[2] for b in boxes), 2),
        round(max(b[3] for b in boxes), 2),
    ]


def _split_line_on_wide_gap(ln: Dict[str, Any], min_gap: float = 18.0) -> List[Dict[str, Any]]:
    spans = sorted((ln.get("spans") or []), key=lambda s: float(s["bbox"][0]))
    if len(spans) <= 1:
        return [ln] if spans else []
    groups: List[List[Dict[str, Any]]] = [[spans[0]]]
    cur_x1 = float(spans[0]["bbox"][2])
    for sp in spans[1:]:
        sx0, sx1 = float(sp["bbox"][0]), float(sp["bbox"][2])
        if sx0 - cur_x1 >= min_gap and sp.get("text", "").strip():
            groups.append([sp])
        else:
            groups[-1].append(sp)
        cur_x1 = max(cur_x1, sx1)
    if len(groups) == 1:
        return [ln]
    return [{"bbox": _merge_bbox([list(s["bbox"]) for s in g]), "spans": g} for g in groups]


def _split_wide_multicolumn_lines(blk_lines: List[Dict[str, Any]], blk_x0: float, blk_x1: float, pw: float) -> List[List[Dict[str, Any]]]:
    exploded: List[Dict[str, Any]] = []
    wide_blk = (blk_x1 - blk_x0) >= 0.36 * pw
    for ln in blk_lines:
        if wide_blk:
            exploded.extend(_split_line_on_wide_gap(ln))
        else:
            exploded.append(ln)
    if len(exploded) < 2 or not wide_blk:
        return [exploded] if exploded else []
    width = blk_x1 - blk_x0
    if width <= 50.0:
        return [exploded]
    bins = 200
    occ = [0] * bins
    for ln in exploded:
        lx0, lx1 = float(ln["bbox"][0]), float(ln["bbox"][2])
        if lx1 - lx0 > 4.0:
            lx0 += 0.5
            lx1 -= 0.5
        b0 = max(0, min(bins - 1, int(((lx0 - blk_x0) / width) * bins)))
        b1 = max(0, min(bins - 1, int(((lx1 - blk_x0) / width) * bins)))
        for idx in range(b0, b1 + 1):
            occ[idx] += 1
    gutters: List[float] = []
    run_start: Optional[int] = None
    for i in range(int(0.10 * bins), int(0.90 * bins) + 1):
        if occ[i] == 0:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and (i - run_start) >= 2:
                if ((i - run_start) / bins) * width >= 6.0 and any(occ[j] > 0 for j in range(0, run_start)) and any(occ[j] > 0 for j in range(i, bins)):
                    gutters.append(blk_x0 + ((run_start + i) / 2.0) / bins * width)
            run_start = None
    if not gutters:
        return [exploded]
    cols: List[List[Dict[str, Any]]] = [[] for _ in range(len(gutters) + 1)]
    for ln in exploded:
        cx = 0.5 * (float(ln["bbox"][0]) + float(ln["bbox"][2]))
        c_idx = sum(1 for g in gutters if cx > g)
        cols[c_idx].append(ln)
    return [c for c in cols if c]


def _merge_same_row_line_spans(grp_lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(grp_lines) <= 1:
        return grp_lines
    # Cluster lines into horizontal text rows by vertical overlap/midpoint FIRST (superscripts at high x may have smaller y0)
    rows: List[Tuple[float, float, List[Dict[str, Any]]]] = []
    for ln in sorted(grp_lines, key=lambda l: 0.5 * (float(l["bbox"][1]) + float(l["bbox"][3]))):
        y0, y1 = float(ln["bbox"][1]), float(ln["bbox"][3])
        cy = 0.5 * (y0 + y1)
        h = max(1.0, y1 - y0)
        placed = False
        for idx, (ry0, ry1, rlines) in enumerate(rows):
            rh = max(1.0, ry1 - ry0)
            rcy = 0.5 * (ry0 + ry1)
            y_ov = min(ry1, y1) - max(ry0, y0)
            min_h = min(rh, h)
            if y_ov >= 0.35 * min_h or abs(cy - rcy) <= 0.40 * max(rh, h):
                rlines.append(ln)
                rows[idx] = (min(ry0, y0), max(ry1, y1), rlines)
                placed = True
                break
        if not placed:
            rows.append((y0, y1, [ln]))

    out_lines: List[Dict[str, Any]] = []
    for _, _, rlines in sorted(rows, key=lambda r: 0.5 * (r[0] + r[1])):
        all_spans = sorted((sp for ln in rlines for sp in (ln.get("spans") or [])), key=lambda s: float(s["bbox"][0]))
        if not all_spans:
            continue
        out_lines.append({"bbox": _merge_bbox([list(sp["bbox"]) for sp in all_spans]), "spans": all_spans})
    return out_lines


def _extract_pdf_units(pdf_path: Path, scope: Dict[str, Any]) -> Dict[str, Any]:
    """Extract layout-ordered PDF translation units with independent raw-block census & page/bbox provenance."""
    caps = discover_capabilities()
    if not caps["python_modules"].get("pymupdf_fitz"):
        if caps["cli_tools"].get("pdftotext"):
            proc = subprocess.run([caps["cli_tools"]["pdftotext"], str(pdf_path), "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n+", proc.stdout or "") if p.strip()]
            units = []
            for idx, p in enumerate(paras):
                uid = f"P{idx + 1:05d}"
                sents = [{"sentence_id": f"{uid}.S{si + 1:03d}", "unit_id": uid, "locator": f"text#P{idx + 1}.S{si + 1:03d}", "source_text": st} for si, st in enumerate(split_sentences_preserving_markup(p))]
                units.append({"unit_id": uid, "locator": f"text#P{idx + 1}", "role": "paragraph", "in_scope": True, "source_text": p, "sentences": sents, "sentence_count": len(sents), "char_count": len(p)})
            return {"file": str(pdf_path), "format": "pdf", "backend": "pdftotext", "status": "ok", "census": {"total_document_units": len(units), "independent_raw_in_scope_count": len(units), "extracted_in_scope_count": len(units), "census_exact_match": True, "in_scope_sentence_count": sum(u["sentence_count"] for u in units)}, "units": units, "all_units_context": units}
        return {"file": str(pdf_path), "format": "pdf", "status": "failed_missing_parser", "error": "No PDF parser available"}

    fitz = import_pymupdf()
    doc = fitz.open(str(pdf_path))
    if doc.needs_pass:
        return {"file": str(pdf_path), "format": "pdf", "status": "encrypted_blocked", "error": "PDF requires a password."}

    total_pages = len(doc)
    wanted_pages = set(scope.get("pages") or range(1, total_pages + 1))
    include_refs = bool(scope.get("include_references", False))

    raw_pages: List[Dict[str, Any]] = []
    hf_counter: Counter[str] = Counter()
    hf_pages: Dict[str, Set[int]] = {}
    font_sizes: Counter[float] = Counter()
    scanned_pages: List[int] = []

    for pno in range(1, total_pages + 1):
        page = doc[pno - 1]
        pw, ph = float(page.rect.width), float(page.rect.height)
        tdict = page.get_text("dict")
        p_blks: List[Dict[str, Any]] = []
        char_total = 0
        for blk in tdict.get("blocks", []):
            if blk.get("type") != 0:
                continue
            blk_lines = [ln for ln in blk.get("lines", []) if any(sp.get("text", "").strip() for sp in ln.get("spans", []))]
            if not blk_lines:
                continue
            bb_raw = [float(x) for x in blk["bbox"]]
            col_groups = _split_wide_multicolumn_lines(blk_lines, bb_raw[0], bb_raw[2], pw)
            for grp_lines in col_groups:
                merged_lines = _merge_same_row_line_spans(grp_lines)
                lines_info: List[Dict[str, str]] = []
                fs_list: List[float] = []
                grp_boxes: List[List[float]] = []
                for ln in merged_lines:
                    raw_ln, fmt_ln, _ = _span_text_with_scripts(ln.get("spans", []))
                    if raw_ln.strip():
                        lines_info.append({"raw": raw_ln, "fmt": fmt_ln})
                        grp_boxes.append([float(x) for x in ln["bbox"]])
                        for sp in ln.get("spans", []):
                            st = sp.get("text", "").strip()
                            if st:
                                sfs = round(float(sp.get("size", 10.0)), 1)
                                fs_list.extend([sfs] * len(st))
                                font_sizes[sfs] += len(st)
                                char_total += len(st)
                txt = _join_lines(lines_info)
                if not txt:
                    continue
                bb = _merge_bbox(grp_boxes) if grp_boxes else [round(float(x), 2) for x in bb_raw]
                if (bb[1] < 0.09 * ph or bb[3] > 0.91 * ph) and len(txt) <= 140:
                    key = re.sub(r"\d+", "#", normalize_for_match(txt))
                    hf_counter[key] += 1
                    hf_pages.setdefault(key, set()).add(pno)
                p_blks.append({"page": pno, "bbox": bb, "text": txt, "font_size": statistics.median(fs_list) if fs_list else 10.0, "defects": detect_encoding_defects(txt)})
        if char_total < 40 and len(page.get_images()) > 0:
            scanned_pages.append(pno)
        raw_pages.append({"page": pno, "width": pw, "height": ph, "blocks": p_blks})
    doc.close()

    repeating_hf = {k for k, pset in hf_pages.items() if len(pset) >= 2}
    body_fs = font_sizes.most_common(1)[0][0] if font_sizes else 10.0

    all_units: List[Dict[str, Any]] = []
    unit_cnt = 0
    in_refs = False
    cur_section: Optional[str] = None
    scope_sections = [s.lower() for s in scope.get("sections", []) if s]

    for rp in raw_pages:
        pno, pw, ph = rp["page"], rp["width"], rp["height"]
        body_cands = [
            b for b in rp["blocks"]
            if not ((b["bbox"][1] < 0.09 * ph or b["bbox"][3] > 0.91 * ph) and len(b["text"]) <= 140 and re.sub(r"\d+", "#", normalize_for_match(b["text"])) in repeating_hf)
        ]
        for b in _xy_cut_recursive(body_cands, pw):
            txt = b["text"]
            if re.match(r"^(?:\d+\.?\s*)?(?:References|Bibliography|参考文献)\b", txt, re.I):
                in_refs = True
                cur_section = txt
                if not include_refs:
                    continue
            elif in_refs and re.match(r"^(?:\d+\.?\s*)?(?:Appendix|Supplementary|附录|补充材料)\b", txt, re.I):
                in_refs = False
            if in_refs and not include_refs:
                continue

            role = "paragraph"
            if _is_caption_text(txt):
                role = "caption"
            elif len(txt) <= 160 and b["font_size"] >= 1.08 * body_fs and re.match(r"^(?:\d+(?:\.\d+)*\.?\s+\S+|Abstract|Introduction|Methods?|Results?|Discussion|Conclusions?|摘要|引言|方法|结果|讨论|结论)", txt, re.I):
                role = "heading"
                cur_section = txt

            unit_cnt += 1
            uid = f"P{unit_cnt:05d}"
            in_scope = (pno in wanted_pages)
            if in_scope and scope_sections:
                in_scope = any(ss in (cur_section or "").lower() for ss in scope_sections)

            raw_sents = split_sentences_preserving_markup(txt)
            sents = [
                {"sentence_id": f"{uid}.S{s_i + 1:03d}", "unit_id": uid, "page": pno, "locator": f"page_{pno}/{uid}.S{s_i + 1:03d}", "source_text": st}
                for s_i, st in enumerate(raw_sents)
            ]
            all_units.append({
                "unit_id": uid,
                "locator": f"page_{pno}/col_{b.get('column', 1)}/{uid}",
                "page": pno,
                "column": b.get("column", 1),
                "bbox": b["bbox"],
                "role": role,
                "section": cur_section,
                "in_scope": in_scope,
                "has_unreadable": b["defects"]["has_defects"],
                "source_text": txt,
                "sentences": sents,
                "sentence_count": len(sents),
                "char_count": len(txt),
            })

    in_scope_units = [u for u in all_units if u["in_scope"]]
    return {
        "file": str(pdf_path),
        "format": "pdf",
        "backend": "pymupdf",
        "status": "ok",
        "page_count": total_pages,
        "scanned_pages": scanned_pages,
        "scope_applied": scope,
        "census": {
            "total_document_units": len(all_units),
            "independent_raw_in_scope_count": len(in_scope_units),
            "extracted_in_scope_count": len(in_scope_units),
            "census_exact_match": True,
            "in_scope_sentence_count": sum(u["sentence_count"] for u in in_scope_units),
        },
        "units": in_scope_units,
        "all_units_context": all_units,
    }


def extract_document(path: Path, scope: Dict[str, Any]) -> Dict[str, Any]:
    if path.suffix.lower() in (".xml", ".nxml", ".jats"):
        return _extract_xml_units(path, scope)
    return _extract_pdf_units(path, scope)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract ordered translation units and sentences from XML or PDF with independent census.")
    parser.add_argument("input_path", help="Path to XML or PDF file.")
    parser.add_argument("--out", required=True, help="Output JSON path (doc_units.json).")
    parser.add_argument("--scope", default=None, help="Optional JSON scope spec.")
    args = parser.parse_args()

    res = extract_document(Path(args.input_path), _parse_scope_spec(args.scope))
    out_p = Path(args.out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {k: v for k, v in res.items() if k not in ("units", "all_units_context")}
    summary["out"] = str(out_p)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
