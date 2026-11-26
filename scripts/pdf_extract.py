#!/usr/bin/env python3
"""Layout-aware scientific PDF extractor.

Reconstructs multi-column reading order (X-Y cut + spanning band detection),
separates headers/footers, classifies sections/captions/footnotes/equations/references,
extracts bordered and three-line tables with cross-page continuation linking,
preserves subscripts/superscripts/symbols, and assigns page+bbox provenance anchors.
"""

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
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True

from pdf_common import (
    bbox_contains_center,
    bbox_iou,
    detect_encoding_defects,
    discover_capabilities,
    import_pymupdf,
    merge_bbox,
    normalize_for_match,
    normalize_header_footer_key,
    normalize_ligatures,
    parse_page_spec,
)

CAPTION_RE = re.compile(
    r"^(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Figure|Fig\.?|Table|Tab\.?|Scheme|Algorithm|Listing|附图|附表|图|表)\s*[A-Za-z]?\d+[A-Za-z0-9.\-]*(?:[:.\s]|$)",
    re.I,
)
HEADING_RE = re.compile(
    r"^(?:"
    r"Abstract|Introduction|Background|Related\s+Work|Methods?|Methodology|Materials?\s+and\s+Methods?|"
    r"Experimental(?:\s+Setup)?|Results?(?:\s+and\s+Discussion)?|Discussion|Conclusions?|"
    r"Acknowledgements?|References|Bibliography|Appendix(?:\s+[A-Z0-9]+)?|"
    r"摘要|引言|前言|研究背景|相关工作|方法|材料与方法|实验|实验结果|结果与讨论|结果与分析|讨论|结论|结语|致谢|参考文献|附录(?:\s*[A-Z0-9]+)?|"
    r"(?:\d+(?:\.\d+){0,3}\.?|[IVX]{1,5}\.|[A-Z]\.|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+、|第[一二三四五六七八九十\d]+[章节])\s*\S+"
    r")$",
    re.I,
)
HEADING_PREFIX_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+){0,3}\.?|[IVX]{1,5}\.|[A-Z]\.|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+、|第[一二三四五六七八九十\d]+[章节])\s*\S+|"
    r"(?:Abstract|Introduction|Background|Related\s+Work|Methods?|Methodology|Materials?\s+and\s+Methods?|"
    r"Experimental\s+Setup|Results?(?:\s+and\s+Discussion)?|Discussion|Conclusions?|Acknowledgements?|References|Bibliography|Appendix)\b)",
    re.I,
)
REF_SECTION_RE = re.compile(r"^(?:\d+\.?\s*)?(?:References|Bibliography|参考文献)\b", re.I)
REF_ITEM_RE = re.compile(r"^(?:\[\d+\]|\(\d+\)|\d+\.\s+[A-Z\u4e00-\u9fff])")
FOOTNOTE_RE = re.compile(r"^(?:\*+|†|‡|§|\d{1,2})\s*\S+")
EQ_LABEL_RE = re.compile(r"(?:\((\d+(?:\.\d+)?[a-z]?)\)|\[(\d+(?:\.\d+)?[a-z]?)\])$")
PURE_EQ_LABEL_RE = re.compile(r"^\s*(?:\((\d+(?:\.\d+)?[a-z]?)\)|\[(\d+(?:\.\d+)?[a-z]?)\])\s*$")
MATH_SYMBOLS = set("=≈≠≤≥<>±×÷·∑∏∫∂∇√∞∈∉⊂⊆∪∩∀∃αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ→←↔⇒⇔")
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


def _decode_symbol_pua(txt: str, font_name: str) -> str:
    """Decode Windows Symbol PUA codepoints (U+F0xx) and isolated PostScript Symbol ASCII letters into Unicode."""
    if "symbol" not in font_name.lower():
        return txt
    has_pua = any(0xF000 <= ord(ch) <= 0xF0FF for ch in txt)
    allow_ascii = (not has_pua) and (not re.search(r"[A-Za-z]{2,}", txt))
    out = []
    for ch in txt:
        if ord(ch) in SYMBOL_PUA_MAP:
            out.append(SYMBOL_PUA_MAP[ord(ch)])
        elif allow_ascii and ch in SYMBOL_ASCII_MAP:
            out.append(SYMBOL_ASCII_MAP[ch])
        else:
            out.append(ch)
    return "".join(out)


def _span_text_with_scripts(spans: List[Dict[str, Any]]) -> Tuple[str, str, bool]:
    """Reconstruct raw line text and script-annotated text (^{...}, _{...})."""
    if not spans:
        return "", "", False
    sizes = [float(s.get("size", 0.0)) for s in spans if s.get("text", "").strip()]
    med_fs = statistics.median(sizes) if sizes else 10.0

    # Weighted baseline from spans near median font size
    base_ys = []
    for s in spans:
        if not s.get("text", "").strip():
            continue
        fs = float(s.get("size", med_fs))
        if fs >= 0.85 * med_fs:
            bb = s.get("bbox", [0, 0, 0, 0])
            base_ys.append(0.5 * (bb[1] + bb[3]))
    med_cy = statistics.median(base_ys) if base_ys else 0.0

    raw_parts: List[str] = []
    fmt_runs: List[Tuple[str, str]] = []
    has_unreadable = False

    for s in spans:
        txt = _decode_symbol_pua(normalize_ligatures(s.get("text", "")), str(s.get("font", "")))
        if not txt:
            continue
        defects = detect_encoding_defects(txt)
        if defects["has_defects"]:
            has_unreadable = True
        raw_parts.append(txt)

        fs = float(s.get("size", med_fs))
        flags = int(s.get("flags", 0))
        bb = s.get("bbox", [0, 0, 0, 0])
        cy = 0.5 * (bb[1] + bb[3])
        stripped = txt.strip()

        is_super = bool(flags & 1) or (fs <= 0.80 * med_fs and med_cy > 0 and cy < med_cy - 0.14 * med_fs)
        is_sub = (not is_super) and (fs <= 0.80 * med_fs and med_cy > 0 and cy > med_cy + 0.14 * med_fs)

        if not stripped and (is_super or is_sub) and fmt_runs and fmt_runs[-1][0] in ("super", "sub"):
            continue
        if stripped and is_super and len(stripped) <= 12:
            kind, val = "super", stripped
        elif stripped and is_sub and len(stripped) <= 12:
            kind, val = "sub", stripped
        else:
            kind, val = "normal", txt

        if fmt_runs and fmt_runs[-1][0] == kind and kind in ("super", "sub") and len(fmt_runs[-1][1]) + len(val) <= 18:
            fmt_runs[-1] = (kind, fmt_runs[-1][1] + val)
        else:
            fmt_runs.append((kind, val))

    fmt_parts: List[str] = []
    for kind, val in fmt_runs:
        if kind == "super":
            fmt_parts.append(f"^{{{val}}}")
        elif kind == "sub":
            fmt_parts.append(f"_{{{val}}}")
        else:
            fmt_parts.append(val)

    return "".join(raw_parts), "".join(fmt_parts), has_unreadable


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (0x4E00 <= code <= 0x9FFF) or (0x3400 <= code <= 0x4DBF) or (0x3000 <= code <= 0x303F) or (0xFF00 <= code <= 0xFFEF)


def _join_block_lines(lines_info: List[Dict[str, Any]], use_formatted: bool = False) -> str:
    """Join lines within a block, healing end-of-line hyphenation and CJK breaks without spurious spaces."""
    key = "fmt_text" if use_formatted else "raw_text"
    out = ""
    for item in lines_info:
        seg = item.get(key, "").rstrip()
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


def _detect_column_gutters(blocks: List[Dict[str, Any]], content_x0: float, content_x1: float) -> List[float]:
    """Detect vertical column gutters inside a horizontal band using 200-bin occupancy projection."""
    width = content_x1 - content_x0
    if len(blocks) < 2 or width <= 50.0:
        return []

    bins = 200
    occ = [0] * bins
    for b in blocks:
        x0 = float(b["bbox"][0])
        x1 = float(b["bbox"][2])
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
                gap_pts = ((i - run_start) / bins) * width
                has_left = any(occ[j] > 0 for j in range(0, run_start))
                has_right = any(occ[j] > 0 for j in range(i, bins))
                if gap_pts >= 6.0 and has_left and has_right:
                    gutters.append(content_x0 + ((run_start + i) / 2.0) / bins * width)
            run_start = None
    return gutters[:2]


def _sort_leaf_by_rows(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort leaf blocks top-to-bottom while keeping same-visual-line fragments ordered left-to-right."""
    if len(blocks) <= 1:
        return list(blocks)
    by_y = sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))
    rows: List[Tuple[float, float, List[Dict[str, Any]]]] = []
    for b in by_y:
        y0, y1 = float(b["bbox"][1]), float(b["bbox"][3])
        h = max(1.0, y1 - y0)
        if rows:
            ry0, ry1, rlist = rows[-1]
            rh = max(1.0, ry1 - ry0)
            y_ov = min(y1, ry1) - max(y0, ry0)
            if y_ov >= 0.45 * min(h, rh):
                rlist.append(b)
                rows[-1] = (min(ry0, y0), max(ry1, y1), rlist)
                continue
        rows.append((y0, y1, [b]))
    out: List[Dict[str, Any]] = []
    for _, _, rlist in rows:
        rlist.sort(key=lambda b: (round(b["bbox"][0], 1), round(b["bbox"][1], 1)))
        out.extend(rlist)
    return out


def _split_y_bands(blocks: List[Dict[str, Any]], min_y_gap: float = 1.5) -> List[List[Dict[str, Any]]]:
    """Partition blocks into non-overlapping horizontal Y-bands separated by vertical whitespace."""
    if len(blocks) <= 1:
        return [list(blocks)] if blocks else []
    by_y = sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))
    bands: List[Tuple[float, float, List[Dict[str, Any]]]] = []
    for b in by_y:
        y0, y1 = float(b["bbox"][1]), float(b["bbox"][3])
        if bands and y0 <= bands[-1][1] + min_y_gap:
            by0, by1, blist = bands[-1]
            blist.append(b)
            bands[-1] = (by0, max(by1, y1), blist)
        else:
            bands.append((y0, y1, [b]))
    return [blist for _, _, blist in bands]


def _xy_cut_recursive(
    blocks: List[Dict[str, Any]],
    page_w: float,
    col_offset: int = 0,
    allow_y_cut: bool = True,
    depth: int = 0,
) -> List[Dict[str, Any]]:
    """Recursive geometric X-Y cut: tries X-gutters first, then Y-whitespace cuts, then spanning fallback."""
    if not blocks:
        return []
    if len(blocks) == 1 or depth >= 6:
        for b in blocks:
            bw = b["bbox"][2] - b["bbox"][0]
            b["column"] = 0 if (col_offset == 0 and bw >= 0.62 * page_w) else max(1, col_offset or 1)
        return _sort_leaf_by_rows(blocks)

    bx0 = min(b["bbox"][0] for b in blocks)
    bx1 = max(b["bbox"][2] for b in blocks)

    # Try X-cut (column gutters across current band)
    gutters = _detect_column_gutters(blocks, bx0, bx1)
    if gutters:
        cols: List[List[Dict[str, Any]]] = [[] for _ in range(len(gutters) + 1)]
        for b in blocks:
            cx = 0.5 * (b["bbox"][0] + b["bbox"][2])
            col_idx = 0
            for g in gutters:
                if cx > g:
                    col_idx += 1
            cols[col_idx].append(b)
        non_empty = [(idx, c) for idx, c in enumerate(cols) if c]
        if len(non_empty) >= 2:
            ordered: List[Dict[str, Any]] = []
            for seq_idx, (_, col_blocks) in enumerate(non_empty):
                sub_col = (col_offset + seq_idx) if col_offset > 0 else (seq_idx + 1)
                ordered.extend(_xy_cut_recursive(col_blocks, page_w, col_offset=sub_col, allow_y_cut=True, depth=depth + 1))
            return ordered

    # No global X-gutter -> try horizontal Y-cut first to isolate full-width headings/captions/bands
    if allow_y_cut:
        y_bands = _split_y_bands(blocks)
        if len(y_bands) >= 2:
            # Coalesce consecutive Y-bands sharing multi-column gutters so aligned rows stay column-ordered.
            merged_bands: List[List[Dict[str, Any]]] = [y_bands[0]]
            for nxt_band in y_bands[1:]:
                cur_band = merged_bands[-1]
                cur_x0 = min(b["bbox"][0] for b in cur_band)
                cur_x1 = max(b["bbox"][2] for b in cur_band)
                cur_g = _detect_column_gutters(cur_band, cur_x0, cur_x1)
                comb = cur_band + nxt_band
                comb_x0 = min(b["bbox"][0] for b in comb)
                comb_x1 = max(b["bbox"][2] for b in comb)
                comb_g = _detect_column_gutters(comb, comb_x0, comb_x1)
                if cur_g and comb_g and len(comb_g) >= len(cur_g):
                    nxt_x0 = min(b["bbox"][0] for b in nxt_band)
                    nxt_x1 = max(b["bbox"][2] for b in nxt_band)
                    nxt_g = _detect_column_gutters(nxt_band, nxt_x0, nxt_x1)
                    y_gap = min(b["bbox"][1] for b in nxt_band) - max(b["bbox"][3] for b in cur_band)
                    crosses_comb = any(
                        b["bbox"][0] < g - 2.0 and b["bbox"][2] > g + 2.0
                        for b in nxt_band
                        for g in comb_g
                    )
                    if not crosses_comb and (nxt_g or y_gap <= 18.0):
                        merged_bands[-1] = comb
                        continue
                merged_bands.append(nxt_band)
            ordered = []
            for band in merged_bands:
                ordered.extend(_xy_cut_recursive(band, page_w, col_offset=col_offset, allow_y_cut=False, depth=depth + 1))
            return ordered

    # Fallback: if a wide spanning block overlaps columns vertically, peel spanning blocks cleanly
    span_x = bx1 - bx0
    content_w = max(page_w * 0.6, span_x)
    mid_x = 0.5 * (bx0 + bx1) if span_x >= 0.62 * page_w else 0.5 * page_w
    spanning: List[Dict[str, Any]] = []
    regular: List[Dict[str, Any]] = []
    for b in blocks:
        bw = b["bbox"][2] - b["bbox"][0]
        crosses_mid = b["bbox"][0] < mid_x - 0.05 * content_w and b["bbox"][2] > mid_x + 0.05 * content_w
        if col_offset == 0 and bw >= 0.62 * content_w and crosses_mid:
            b["column"] = 0
            spanning.append(b)
        else:
            regular.append(b)

    if spanning and regular:
        spanning.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
        ordered = []
        prev_y = -1e9
        for sp in spanning:
            sp_cy = 0.5 * (sp["bbox"][1] + sp["bbox"][3])
            sub = [b for b in regular if prev_y <= 0.5 * (b["bbox"][1] + b["bbox"][3]) < sp_cy]
            ordered.extend(_xy_cut_recursive(sub, page_w, col_offset=col_offset, allow_y_cut=True, depth=depth + 1))
            ordered.append(sp)
            prev_y = sp_cy
        tail = [b for b in regular if 0.5 * (b["bbox"][1] + b["bbox"][3]) >= prev_y]
        ordered.extend(_xy_cut_recursive(tail, page_w, col_offset=col_offset, allow_y_cut=True, depth=depth + 1))
        return ordered

    for b in blocks:
        b["column"] = max(1, col_offset or 1)
    return _sort_leaf_by_rows(blocks)


def _xy_cut_order(blocks: List[Dict[str, Any]], page_w: float) -> List[Dict[str, Any]]:
    """Order page blocks using recursive X-Y cut with visual-row sorting."""
    return _xy_cut_recursive(blocks, page_w, col_offset=0, allow_y_cut=True, depth=0)


def _coalesce_same_line_fragments(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge horizontal single-line fragments split by MuPDF (e.g. inline superscripts) within the same column."""
    if len(blocks) <= 1:
        return blocks
    merged: List[Dict[str, Any]] = [dict(blocks[0])]
    for nxt in blocks[1:]:
        cur = merged[-1]
        if cur.get("column") != nxt.get("column") or cur.get("page") != nxt.get("page"):
            merged.append(dict(nxt))
            continue
        if PURE_EQ_LABEL_RE.match(nxt["raw_text"]) or CAPTION_RE.match(cur["raw_text"]) or CAPTION_RE.match(nxt["raw_text"]):
            merged.append(dict(nxt))
            continue
        cb, nb = cur["bbox"], nxt["bbox"]
        ch = max(1.0, cb[3] - cb[1])
        nh = max(1.0, nb[3] - nb[1])
        fs_max = max(float(cur.get("font_size") or 10.0), float(nxt.get("font_size") or 10.0))
        y_ov = min(cb[3], nb[3]) - max(cb[1], nb[1])
        dx = nb[0] - cb[2]
        nxt_txt = nxt.get("text", "")
        is_script_cont = nxt_txt.lstrip().startswith(("^{", "_{"))
        if ch <= 2.4 * fs_max and nh <= 2.4 * fs_max and y_ov >= 0.45 * min(ch, nh):
            if (-3.0 <= dx <= 10.0) or (is_script_cont and -3.0 <= dx <= 22.0):
                cur_r = cur["raw_text"].rstrip()
                nxt_r = nxt["raw_text"].lstrip()
                cur_t = cur["text"].rstrip()
                nxt_t = nxt_txt.lstrip()
                no_space_fmt = is_script_cont or (bool(cur_t and nxt_t) and _is_cjk(cur_t[-1]) and _is_cjk(nxt_t[0]))
                no_space_raw = is_script_cont or (bool(cur_r and nxt_r) and _is_cjk(cur_r[-1]) and _is_cjk(nxt_r[0]))
                cur["raw_text"] = (cur_r + nxt_r) if no_space_raw else (cur_r + " " + nxt_r)
                cur["text"] = (cur_t + nxt_t) if no_space_fmt else (cur_t + " " + nxt_t)
                cur["bbox"] = merge_bbox([cb, nb])
                cur["font_size"] = max(float(cur.get("font_size") or 0.0), float(nxt.get("font_size") or 0.0))
                cur["defects"] = detect_encoding_defects(cur["raw_text"])
                continue
        merged.append(dict(nxt))
    return merged


def _bbox_containment(a: List[float], b: List[float]) -> float:
    """Compute intersection area divided by the smaller box area to detect nested duplicate detections."""
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(area_a, area_b)


def _clean_table_rows(rows: List[List[str]]) -> List[List[str]]:
    """Normalize whitespace in table cells and drop completely empty columns/rows."""
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    norm = [
        [re.sub(r"\s+", " ", (c or "")).strip() for c in (r + [""] * (ncols - len(r)))]
        for r in rows
    ]
    norm = [r for r in norm if any(c for c in r)]
    if not norm:
        return []
    keep_cols = [c_idx for c_idx in range(ncols) if any(r[c_idx] for r in norm)]
    if not keep_cols:
        return []
    return [[r[c_idx] for c_idx in keep_cols] for r in norm]


def _rows_to_markdown(rows: List[List[str]]) -> str:
    clean = _clean_table_rows(rows)
    if not clean:
        return ""
    ncols = len(clean[0])
    escaped = [[c.replace("|", "\\|") for c in r] for r in clean]
    header = "| " + " | ".join(escaped[0]) + " |"
    sep = "| " + " | ".join(["---"] * ncols) + " |"
    body = ["| " + " | ".join(r) + " |" for r in escaped[1:]]
    return "\n".join([header, sep, *body])


def _rows_to_html(rows: List[List[str]], n_header_rows: int = 1) -> str:
    norm_rows = _clean_table_rows(rows)
    if not norm_rows:
        return "<table></table>"
    ncols = len(norm_rows[0])

    # Simple colspan inference when header cell is followed by empty cells
    def _escape(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    out = ["<table>"]
    if n_header_rows > 0:
        out.append("  <thead>")
        for r_idx in range(min(n_header_rows, len(norm_rows))):
            row = norm_rows[r_idx]
            out.append("    <tr>")
            c = 0
            while c < ncols:
                val = row[c]
                span = 1
                if val:
                    while c + span < ncols and row[c + span] == "":
                        span += 1
                attr = f' colspan="{span}"' if span > 1 else ""
                out.append(f"      <th{attr}>{_escape(val)}</th>")
                c += span
            out.append("    </tr>")
        out.append("  </thead>")
    out.append("  <tbody>")
    for r_idx in range(min(n_header_rows, len(norm_rows)), len(norm_rows)):
        out.append("    <tr>")
        for val in norm_rows[r_idx]:
            out.append(f"      <td>{_escape(val)}</td>")
        out.append("    </tr>")
    out.append("  </tbody>")
    out.append("</table>")
    return "\n".join(out)


def _extract_tables_pdfplumber(pdf_path: Path, pages: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Extract bordered and three-line (text-strategy) tables using pdfplumber."""
    import pdfplumber

    by_page: Dict[int, List[Dict[str, Any]]] = {p: [] for p in pages}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pno in pages:
            if pno < 1 or pno > len(pdf.pages):
                continue
            page = pdf.pages[pno - 1]
            candidates: List[Tuple[str, List[float], List[List[str]]]] = []

            # Strategy 1: explicit/default lines
            for tbl in page.find_tables():
                data = tbl.extract() or []
                clean_rows = _clean_table_rows([[normalize_ligatures(c or "").strip() for c in row] for row in data])
                if len(clean_rows) >= 2 and len(clean_rows[0]) >= 2:
                    candidates.append(("pdfplumber_lines", [round(x, 2) for x in tbl.bbox], clean_rows))

            # Collect caption bounding boxes to avoid merging separate tables across captions
            caption_boxes: List[Tuple[float, float, float]] = []
            try:
                page_txt_lines = (page.extract_text_lines() or [])
                for tln in page_txt_lines:
                    lt = normalize_ligatures(tln.get("text") or "").strip()
                    if CAPTION_RE.match(lt):
                        caption_boxes.append((
                            float(tln["x0"]),
                            float(tln["x1"]),
                            0.5 * (float(tln["top"]) + float(tln["bottom"])),
                        ))
            except Exception:
                pass

            # Strategy 2: horizontal-rule bounded three-line tables (lines or thin filled rects)
            h_rules: List[Dict[str, float]] = []
            for ln in (page.lines or []):
                if abs(ln["y0"] - ln["y1"]) < 2.0 and (ln["x1"] - ln["x0"]) > 0.25 * page.width:
                    rule_box = [float(ln["x0"]), float(ln["top"]), float(ln["x1"]), float(ln["bottom"])]
                    if not any(bbox_contains_center(c_bbox, rule_box, margin=3.0) for _, c_bbox, _ in candidates):
                        h_rules.append({
                            "x0": rule_box[0],
                            "x1": rule_box[2],
                            "top": rule_box[1],
                            "bottom": rule_box[3],
                        })
            for rc in (page.rects or []):
                if abs(rc["bottom"] - rc["top"]) <= 3.0 and (rc["x1"] - rc["x0"]) > 0.25 * page.width:
                    rule_box = [float(rc["x0"]), float(rc["top"]), float(rc["x1"]), float(rc["bottom"])]
                    if not any(bbox_contains_center(c_bbox, rule_box, margin=3.0) for _, c_bbox, _ in candidates):
                        h_rules.append({
                            "x0": rule_box[0],
                            "x1": rule_box[2],
                            "top": rule_box[1],
                            "bottom": rule_box[3],
                        })
            if len(h_rules) >= 2:
                # Cluster horizontal rules into X-lanes first so side-by-side column tables never merge.
                lanes: List[Dict[str, Any]] = []
                for ln in sorted(h_rules, key=lambda r: (-(r["x1"] - r["x0"]), r["x0"], r["top"])):
                    lw = max(1e-3, ln["x1"] - ln["x0"])
                    best_lane = None
                    best_ratio = 0.0
                    for lane in lanes:
                        lane_w = max(1e-3, lane["x1"] - lane["x0"])
                        ov = max(0.0, min(ln["x1"], lane["x1"]) - max(ln["x0"], lane["x0"]))
                        ratio = ov / max(lw, lane_w)
                        if ov >= 0.5 * min(lw, lane_w) and ratio >= 0.45 and ratio > best_ratio:
                            best_ratio = ratio
                            best_lane = lane
                    if best_lane is not None:
                        best_lane["rules"].append(ln)
                    else:
                        lanes.append({"x0": ln["x0"], "x1": ln["x1"], "rules": [ln]})
                groups: List[List[Dict[str, float]]] = []
                for lane in lanes:
                    lane_rules = sorted(lane["rules"], key=lambda r: r["top"])
                    if len(lane_rules) < 2:
                        continue
                    lane_groups: List[List[Dict[str, float]]] = [[lane_rules[0]]]
                    lane_w = max(1e-3, lane["x1"] - lane["x0"])
                    for ln in lane_rules[1:]:
                        prev_grp = lane_groups[-1]
                        prev_y = prev_grp[-1]["bottom"]
                        gap = ln["top"] - prev_grp[-1]["top"]
                        same_span = abs(ln["x0"] - prev_grp[0]["x0"]) <= 35.0 and abs(ln["x1"] - prev_grp[0]["x1"]) <= 35.0
                        has_caption_between = any(
                            prev_y < cy < ln["top"]
                            and (cx1 >= lane["x0"] - 30.0 and cx0 <= lane["x1"] + 30.0)
                            and (
                                (cx1 - cx0) >= 0.55 * page.width
                                or (min(cx1, lane["x1"]) - max(cx0, lane["x0"])) >= 0.25 * min(max(1.0, cx1 - cx0), lane_w)
                            )
                            for cx0, cx1, cy in caption_boxes
                        )
                        if not has_caption_between and (gap <= 260.0 or (len(prev_grp) == 2 and same_span and gap <= 550.0)):
                            prev_grp.append(ln)
                        else:
                            lane_groups.append([ln])
                    groups.extend(lane_groups)
                for grp in groups:
                    if len(grp) < 2:
                        continue
                    gx0 = max(0.0, min(ln["x0"] for ln in grp) - 4.0)
                    gx1 = min(float(page.width), max(ln["x1"] for ln in grp) + 4.0)
                    gtop = max(0.0, min(ln["top"] for ln in grp) - 2.0)
                    gbot = min(float(page.height), max(ln["bottom"] for ln in grp) + 2.0)
                    if gbot - gtop < 20.0:
                        continue
                    gbox = [round(gx0, 2), round(gtop, 2), round(gx1, 2), round(gbot, 2)]
                    if any(bbox_iou(gbox, c_bbox) > 0.25 or bbox_contains_center(gbox, c_bbox, margin=2.0) for _, c_bbox, _ in candidates):
                        continue
                    cropped = page.crop((gx0, gtop, gx1, gbot))
                    tbls_txt = cropped.find_tables(table_settings={
                        "vertical_strategy": "text",
                        "horizontal_strategy": "text",
                        "explicit_vertical_lines": [gx0, gx1],
                        "snap_y_tolerance": 4,
                        "intersection_tolerance": 4,
                    })
                    for tbl in tbls_txt:
                        data = tbl.extract() or []
                        clean_rows = _clean_table_rows([[normalize_ligatures(c or "").strip() for c in row] for row in data])
                        if len(clean_rows) >= 2 and len(clean_rows[0]) >= 2:
                            candidates.append((
                                "pdfplumber_threeline",
                                gbox,
                                clean_rows,
                            ))

            # Deduplicate overlapping tables (including tight inner bounds inside wide rule tables)
            kept: List[Dict[str, Any]] = []
            for method, bbox, rows in candidates:
                if any(bbox_iou(bbox, k["bbox"]) > 0.40 or _bbox_containment(bbox, k["bbox"]) >= 0.65 for k in kept):
                    continue
                kept.append({"method": method, "bbox": bbox, "rows": rows})
            by_page[pno] = kept
    return by_page


def _extract_tables_fitz(doc: Any, pages: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Extract tables via PyMuPDF find_tables (with text-strategy fallback near table captions)."""
    by_page: Dict[int, List[Dict[str, Any]]] = {p: [] for p in pages}
    for pno in pages:
        page = doc[pno - 1]
        if not hasattr(page, "find_tables"):
            continue
        try:
            finder = page.find_tables()
            for tbl in getattr(finder, "tables", []):
                data = tbl.extract() or []
                clean_rows = _clean_table_rows([[normalize_ligatures(c or "").strip() for c in row] for row in data])
                if len(clean_rows) >= 2 and len(clean_rows[0]) >= 2:
                    avg_len = sum(len(c) for r in clean_rows for c in r) / max(1, sum(len(r) for r in clean_rows))
                    if avg_len > 140.0:
                        continue
                    by_page[pno].append({
                        "method": "pymupdf_find_tables",
                        "bbox": [round(float(x), 2) for x in tbl.bbox],
                        "rows": clean_rows,
                    })
        except Exception:
            pass

        # Caption-guided text-strategy fallback for borderless tables when no table overlaps near a Table caption
        try:
            tdict = page.get_text("dict")
            pw, ph = float(page.rect.width), float(page.rect.height)
            for blk in tdict.get("blocks", []):
                if blk.get("type") != 0:
                    continue
                txt = normalize_ligatures(" ".join(
                    sp.get("text", "") for ln in blk.get("lines", []) for sp in ln.get("spans", [])
                )).strip()
                if not re.match(r"^(?:Table|Tab\.?|表)\s*\d+", txt, re.I):
                    continue
                cb = blk["bbox"]
                clip = [max(0.0, cb[0] - 20.0), max(0.0, cb[3] - 2.0), min(pw, max(cb[2] + 40.0, 0.92 * pw)), min(ph, cb[3] + 280.0)]
                if any(bbox_iou(clip, t["bbox"]) > 0.15 or bbox_contains_center(clip, t["bbox"], margin=10.0) for t in by_page[pno]):
                    continue
                finder_txt = page.find_tables(clip=clip, strategy="text")
                for tbl in getattr(finder_txt, "tables", []):
                    data = tbl.extract() or []
                    clean_rows = _clean_table_rows([[normalize_ligatures(c or "").strip() for c in row] for row in data])
                    # Reject prose false positives: require >=2 cols, >=2 rows, and short average cell length
                    if len(clean_rows) >= 2 and len(clean_rows[0]) >= 2:
                        avg_len = sum(len(c) for r in clean_rows for c in r) / max(1, sum(len(r) for r in clean_rows))
                        if avg_len <= 45.0:
                            tb = [round(float(x), 2) for x in tbl.bbox]
                            if not any(bbox_iou(tb, t["bbox"]) > 0.3 for t in by_page[pno]):
                                by_page[pno].append({
                                    "method": "pymupdf_caption_text",
                                    "bbox": tb,
                                    "rows": clean_rows,
                                })
        except Exception:
            pass
    return by_page


def _detect_unextracted_table_candidates(page: Any, extracted_tables: List[Dict[str, Any]], page_w: float) -> List[Dict[str, Any]]:
    """Detect groups of >=2 wide horizontal rules not covered by extracted tables (likely three-line/image tables)."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    h_rules: List[List[float]] = []
    for d in drawings:
        rect = d.get("rect")
        if not rect:
            continue
        w = float(rect.width)
        h = float(rect.height)
        if w >= 0.30 * page_w and h <= 3.0:
            h_rules.append([float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)])
    if len(h_rules) < 2:
        return []
    h_rules.sort(key=lambda r: r[1])
    groups: List[List[List[float]]] = [[h_rules[0]]]
    for r in h_rules[1:]:
        if r[1] - groups[-1][-1][1] <= 260.0:
            groups[-1].append(r)
        else:
            groups.append([r])

    candidates: List[Dict[str, Any]] = []
    for g in groups:
        if len(g) < 2:
            continue
        gb = merge_bbox(g)
        if any(bbox_iou(gb, t["bbox"]) > 0.25 or bbox_contains_center(t["bbox"], gb, margin=15.0) for t in extracted_tables):
            continue
        if (gb[3] - gb[1]) >= 25.0:
            candidates.append({"bbox": gb, "rule_count": len(g), "reason": "horizontal_rules_unparsed_as_table"})
    return candidates


def _split_wide_multicolumn_lines(lines: List[Dict[str, Any]], pw: float) -> List[List[Dict[str, Any]]]:
    """Split a wide MuPDF block into column-aligned line groups using 200-bin X-projection."""
    if len(lines) < 2 or pw <= 50.0:
        return [lines]
    valid_boxes = [ln.get("bbox") for ln in lines if ln.get("bbox") and (ln["bbox"][2] - ln["bbox"][0]) > 1.0]
    if len(valid_boxes) < 2:
        return [lines]

    bins = 200
    occ = [0] * bins
    for bb in valid_boxes:
        x0 = float(bb[0])
        x1 = float(bb[2])
        if x1 - x0 > 6.0:
            x0 += 1.0
            x1 -= 1.0
        b0 = max(0, min(bins - 1, int((x0 / pw) * bins)))
        b1 = max(0, min(bins - 1, int((x1 / pw) * bins)))
        for idx in range(b0, b1 + 1):
            occ[idx] += 1

    gutters: List[float] = []
    run_start: Optional[int] = None
    min_gap_pts = max(8.0, 0.014 * pw)
    for i in range(int(0.10 * bins), int(0.90 * bins) + 1):
        if occ[i] == 0:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                gap_pts = ((i - run_start) / bins) * pw
                has_left = any(occ[j] > 0 for j in range(0, run_start))
                has_right = any(occ[j] > 0 for j in range(i, bins))
                if gap_pts >= min_gap_pts and has_left and has_right:
                    gutters.append(((run_start + i) / 2.0) / bins * pw)
            run_start = None

    if not gutters:
        return [lines]
    gutters = gutters[:2]
    buckets: List[List[Dict[str, Any]]] = [[] for _ in range(len(gutters) + 1)]
    for ln in lines:
        bb = ln.get("bbox")
        if not bb:
            buckets[0].append(ln)
            continue
        for g in gutters:
            if bb[0] < g - 2.0 and bb[2] > g + 2.0:
                return [lines]
        cx = 0.5 * (bb[0] + bb[2])
        c_idx = 0
        for g in gutters:
            if cx > g:
                c_idx += 1
        buckets[c_idx].append(ln)
    non_empty = [b for b in buckets if b]
    return non_empty if len(non_empty) >= 2 else [lines]


def _merge_split_equation_labels(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge standalone right-aligned equation label blocks like '(1)' with the formula block to their left."""
    used_indices: set[int] = set()
    lbl_to_formula: Dict[int, int] = {}
    for idx, b in enumerate(blocks):
        if not PURE_EQ_LABEL_RE.match(b["raw_text"]):
            continue
        cy = 0.5 * (b["bbox"][1] + b["bbox"][3])
        best_j = None
        best_dx = 1e9
        for j, cand in enumerate(blocks):
            if j == idx or j in used_indices or PURE_EQ_LABEL_RE.match(cand["raw_text"]):
                continue
            ccy = 0.5 * (cand["bbox"][1] + cand["bbox"][3])
            if abs(ccy - cy) <= 18.0 and cand["bbox"][2] <= b["bbox"][0] + 12.0 and len(cand["raw_text"]) <= 200:
                dx = b["bbox"][0] - cand["bbox"][2]
                if dx < best_dx:
                    best_dx = dx
                    best_j = j
        if best_j is not None:
            used_indices.add(idx)
            lbl_to_formula[best_j] = idx

    merged: List[Dict[str, Any]] = []
    for idx, b in enumerate(blocks):
        if idx in used_indices:
            continue
        if idx in lbl_to_formula:
            lbl_blk = blocks[lbl_to_formula[idx]]
            b = dict(b)
            b["raw_text"] = f"{b['raw_text']} {lbl_blk['raw_text'].strip()}"
            b["text"] = f"{b['text']} {lbl_blk['text'].strip()}"
            b["bbox"] = merge_bbox([b["bbox"], lbl_blk["bbox"]])
        merged.append(b)
    return merged


def _extract_printed_page_map(headers_footers: List[Dict[str, Any]], total_pages: int) -> Tuple[Dict[int, str], bool]:
    """Extract printed page labels from headers/footers and flag non-sequential numbering discrepancies."""
    label_map: Dict[int, str] = {}
    numeric_seq: List[Tuple[int, int]] = []
    for hf in headers_footers:
        pno = int(hf["page"])
        txt = hf["text"].strip()
        m = re.search(r"(?:page|pp\.?|第)\s*(\d+)\b|^\s*[-–—]?\s*(\d{1,4})\s*[-–—]?\s*$", txt, re.I)
        if m and pno not in label_map:
            lbl = m.group(1) or m.group(2)
            label_map[pno] = lbl
            if lbl.isdigit():
                numeric_seq.append((pno, int(lbl)))
    discrepancy = False
    if len(numeric_seq) >= 2:
        numeric_seq.sort()
        for (p1, n1), (p2, n2) in zip(numeric_seq, numeric_seq[1:]):
            if (n2 - n1) != (p2 - p1):
                discrepancy = True
                break
    return label_map, discrepancy


def _extract_paper_metadata(blocks: List[Dict[str, Any]], sections: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract paper title, authors/affiliations, abstract, and keywords with block provenance."""
    p1_blocks = [b for b in blocks if b["page"] == 1 and b["role"] not in ("caption", "footnote")]
    if not p1_blocks:
        return {}
    top_blocks = [b for b in p1_blocks if b["bbox"][1] < 480.0 and len(b["raw_text"]) >= 4]
    title_cands = [b for b in top_blocks if not HEADING_PREFIX_RE.match(b["raw_text"].strip())]
    title_blk = max(title_cands or top_blocks, key=lambda b: (b["font_size"], -b["bbox"][1])) if top_blocks else None

    abstract_texts: List[str] = []
    abstract_bids: List[str] = []
    keywords_val: Optional[str] = None
    keywords_bid: Optional[str] = None
    authors: List[str] = []
    author_bids: List[str] = []

    if title_blk and not HEADING_PREFIX_RE.match(title_blk["raw_text"].strip()):
        t_idx = next((i for i, b in enumerate(p1_blocks) if b["block_id"] == title_blk["block_id"]), 0)
        for b in p1_blocks[t_idx + 1:]:
            txt = b["raw_text"].strip()
            if b["role"] == "heading" or re.match(r"^(?:Abstract|摘要|1\.?\s+Introduction|一、)\b", txt, re.I):
                break
            has_aff_cue = bool(re.search(r"[@]|University|Department|Institute|School|College|Lab|大学|学院|研究所", txt, re.I))
            if txt.endswith((".", "。")) and len(txt) > 55 and not has_aff_cue:
                break
            if len(txt) <= 280:
                authors.append(txt)
                author_bids.append(b["block_id"])

    in_abs = False
    for b in blocks:
        if b["page"] > 2:
            break
        txt = b["raw_text"].strip()
        if re.match(r"^(?:Abstract|摘要)\s*[:：]?\s*$", txt, re.I):
            in_abs = True
            continue
        if re.match(r"^(?:Abstract|摘要)\s*[:：]\s+\S+", txt, re.I):
            abstract_texts.append(txt)
            abstract_bids.append(b["block_id"])
            in_abs = True
            continue
        if re.match(r"^(?:Keywords?|Index\s+Terms|关键词)\s*[:：]", txt, re.I):
            keywords_val = txt
            keywords_bid = b["block_id"]
            in_abs = False
            continue
        if in_abs:
            if b["role"] == "heading":
                in_abs = False
            else:
                abstract_texts.append(txt)
                abstract_bids.append(b["block_id"])
                if len(abstract_texts) >= 3:
                    in_abs = False

    return {
        "title": title_blk["raw_text"] if title_blk else None,
        "title_block_id": title_blk["block_id"] if title_blk else None,
        "authors_and_affiliations": authors or None,
        "author_block_ids": author_bids or None,
        "abstract": " ".join(abstract_texts) if abstract_texts else None,
        "abstract_block_ids": abstract_bids or None,
        "keywords": keywords_val,
        "keywords_block_id": keywords_bid,
    }


def _build_merged_tables(structured_tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Combine cross-page continued tables into unified logical table entries."""
    by_id = {t["table_id"]: t for t in structured_tables}
    merged: List[Dict[str, Any]] = []
    for t in structured_tables:
        if t.get("continues_from"):
            continue
        if not t.get("continued_on"):
            continue
        chain = [t]
        cur = t
        while cur.get("continued_on") and cur["continued_on"] in by_id:
            nxt = by_id[cur["continued_on"]]
            if nxt in chain:
                break
            chain.append(nxt)
            cur = nxt
        rows = list(chain[0]["rows"])
        for part in chain[1:]:
            part_rows = part["rows"]
            if part_rows and rows and part_rows[0] == rows[0]:
                rows.extend(part_rows[1:])
            else:
                rows.extend(part_rows)
        merged.append({
            "merged_table_id": f"merged_{t['table_id']}",
            "source_table_ids": [c["table_id"] for c in chain],
            "pages": [c["page"] for c in chain],
            "caption": t.get("caption"),
            "num_rows": len(rows),
            "num_cols": max((len(r) for r in rows), default=0),
            "rows": rows,
            "markdown": _rows_to_markdown(rows),
            "html": _rows_to_html(rows),
        })
    return merged


def _extract_with_fitz(pdf_path: Path, page_spec: Optional[str], caps: Dict[str, Any]) -> Dict[str, Any]:
    fitz = import_pymupdf()

    doc = fitz.open(str(pdf_path))
    if doc.needs_pass:
        return {"file": str(pdf_path), "status": "encrypted_blocked", "error": "PDF requires a password."}

    total_pages = len(doc)
    pages = parse_page_spec(page_spec, total_pages)

    # Pre-filter pages for expensive pdfplumber table scanning (efficiency boost on long papers)
    plumber_pages_set: set[int] = set()
    for pno in pages:
        page = doc[pno - 1]
        if len(page.get_drawings()) >= 2 or re.search(
            r"\b(?:Table|Tab\.?|附表|续表)\s*[A-Za-z]?\d*|\bcont(?:inued)?\b|表\s*[A-Za-z]?\d+",
            page.get_text("text") or "",
            re.I,
        ):
            plumber_pages_set.add(pno)
            if pno + 1 <= total_pages and (pno + 1) in pages:
                plumber_pages_set.add(pno + 1)
    plumber_pages = sorted(plumber_pages_set)

    # Table extraction (merge pdfplumber + fitz, keeping richer row/cell candidate on overlap)
    tables_by_page: Dict[int, List[Dict[str, Any]]] = {p: [] for p in pages}
    if caps["python_modules"].get("pdfplumber") and plumber_pages:
        try:
            plum_res = _extract_tables_pdfplumber(pdf_path, plumber_pages)
            for pno, plist in plum_res.items():
                tables_by_page[pno] = plist
        except Exception:
            pass
    fitz_tables = _extract_tables_fitz(doc, pages)
    for pno in pages:
        for ft in fitz_tables.get(pno, []):
            overlaps = [
                i for i, et in enumerate(tables_by_page[pno])
                if bbox_iou(ft["bbox"], et["bbox"]) > 0.40 or _bbox_containment(ft["bbox"], et["bbox"]) >= 0.65
            ]
            if not overlaps:
                tables_by_page[pno].append(ft)
            else:
                idx0 = overlaps[0]
                et = tables_by_page[pno][idx0]
                ft_chars = sum(len(c) for r in ft["rows"] for c in r)
                et_chars = sum(len(c) for r in et["rows"] for c in r)
                if len(ft["rows"]) > len(et["rows"]) or (len(ft["rows"]) == len(et["rows"]) and ft_chars > et_chars):
                    tables_by_page[pno][idx0] = ft

    # First pass: collect raw blocks per page + font size stats + candidate headers/footers
    raw_pages: List[Dict[str, Any]] = []
    font_size_counter: Counter[float] = Counter()
    hf_counter: Counter[str] = Counter()
    hf_pages: Dict[str, set[int]] = {}

    for pno in pages:
        page = doc[pno - 1]
        pw, ph = float(page.rect.width), float(page.rect.height)
        tdict = page.get_text("dict")
        page_blocks: List[Dict[str, Any]] = []
        page_images: List[Dict[str, Any]] = []
        p_tables = tables_by_page.get(pno, [])

        for blk in tdict.get("blocks", []):
            if blk.get("type") == 1:
                ibb = [round(float(x), 2) for x in blk.get("bbox", [0, 0, 0, 0])]
                if (ibb[2] - ibb[0]) >= 24.0 and (ibb[3] - ibb[1]) >= 24.0:
                    page_images.append({
                        "bbox": ibb,
                        "pixel_width": int(blk.get("width") or 0),
                        "pixel_height": int(blk.get("height") or 0),
                        "ext": blk.get("ext"),
                    })
                continue
            if blk.get("type") != 0:
                continue
            blk_lines = blk.get("lines", [])
            bb_raw = [round(float(x), 2) for x in blk["bbox"]]
            line_groups = _split_wide_multicolumn_lines(blk_lines, pw) if (bb_raw[2] - bb_raw[0]) >= 0.36 * pw else [blk_lines]

            for grp_lines in line_groups:
                grp_boxes = [ln.get("bbox") for ln in grp_lines if ln.get("bbox")]
                bb = merge_bbox(grp_boxes) if grp_boxes else bb_raw
                in_table = any(bbox_contains_center(t["bbox"], bb, margin=2.0) for t in p_tables)

                lines_info: List[Dict[str, Any]] = []
                block_sizes: List[float] = []
                bold_chars = 0
                total_chars = 0
                for ln in grp_lines:
                    spans = ln.get("spans", [])
                    raw_ln, fmt_ln, has_unr = _span_text_with_scripts(spans)
                    if not raw_ln.strip():
                        continue
                    for sp in spans:
                        stxt = sp.get("text", "").strip()
                        if not stxt:
                            continue
                        fs = round(float(sp.get("size", 10.0)), 1)
                        block_sizes.extend([fs] * len(stxt))
                        if not in_table:
                            font_size_counter[fs] += len(stxt)
                        total_chars += len(stxt)
                        fname = str(sp.get("font", "")).lower()
                        if (int(sp.get("flags", 0)) & 16) or "bold" in fname or "black" in fname:
                            bold_chars += len(stxt)
                    lines_info.append({"raw_text": raw_ln, "fmt_text": fmt_ln, "has_unreadable": has_unr})

                if not lines_info:
                    continue
                raw_text = _join_block_lines(lines_info, use_formatted=False)
                fmt_text = _join_block_lines(lines_info, use_formatted=True)
                if not raw_text:
                    continue

                dom_fs = statistics.median(block_sizes) if block_sizes else 10.0
                is_bold = (bold_chars / max(1, total_chars)) >= 0.6
                defects = detect_encoding_defects(raw_text)

                is_edge_band = (bb[1] < 0.09 * ph or bb[3] > 0.91 * ph) and len(raw_text) <= 140
                if is_edge_band and not in_table:
                    hf_key = normalize_header_footer_key(raw_text)
                    if hf_key:
                        hf_counter[hf_key] += 1
                        hf_pages.setdefault(hf_key, set()).add(pno)

                page_blocks.append({
                    "page": pno,
                    "bbox": bb,
                    "raw_text": raw_text,
                    "text": fmt_text,
                    "font_size": round(dom_fs, 2),
                    "is_bold": is_bold,
                    "in_table": in_table,
                    "defects": defects,
                })

        unextracted_tbls = _detect_unextracted_table_candidates(page, p_tables, pw)
        raw_pages.append({
            "page": pno,
            "width": round(pw, 1),
            "height": round(ph, 1),
            "blocks": page_blocks,
            "images": page_images,
            "unextracted_table_candidates": unextracted_tbls,
        })

    doc.close()

    body_fs = font_size_counter.most_common(1)[0][0] if font_size_counter else 10.0
    repeating_hf = {k for k, pset in hf_pages.items() if len(pset) >= 2}

    # Second pass: order blocks, separate headers/footers, classify roles, build structures
    all_blocks: List[Dict[str, Any]] = []
    headers_footers: List[Dict[str, Any]] = []
    captions: List[Dict[str, Any]] = []
    footnotes: List[Dict[str, Any]] = []
    equations: List[Dict[str, Any]] = []
    sections: List[Dict[str, Any]] = []
    references: List[Dict[str, Any]] = []
    unextracted_regions: List[Dict[str, Any]] = []

    in_references_section = False
    sec_counter = 0
    eq_counter = 0
    cap_counter = 0
    fn_counter = 0
    ref_counter = 0

    for rp in raw_pages:
        pno = rp["page"]
        pw, ph = rp["width"], rp["height"]
        for cand in rp["unextracted_table_candidates"]:
            unextracted_regions.append({"page": pno, **cand})

        body_candidates: List[Dict[str, Any]] = []
        for b in rp["blocks"]:
            if b["in_table"]:
                continue
            bb = b["bbox"]
            is_edge = (bb[1] < 0.09 * ph or bb[3] > 0.91 * ph) and len(b["raw_text"]) <= 140
            if is_edge and normalize_header_footer_key(b["raw_text"]) in repeating_hf:
                headers_footers.append({
                    "page": pno,
                    "bbox": bb,
                    "position": "header" if bb[1] < 0.5 * ph else "footer",
                    "text": b["raw_text"],
                })
                continue
            body_candidates.append(b)

        ordered = _merge_split_equation_labels(_coalesce_same_line_fragments(_xy_cut_order(body_candidates, pw)))
        for idx, b in enumerate(ordered):
            bid = f"p{pno}_b{idx + 1}"
            b["block_id"] = bid
            txt = b["raw_text"]
            ftxt = b["text"]
            bb = b["bbox"]
            fs = b["font_size"]

            # Precompute equation cues so formulas with larger font near top of page aren't stolen by heading rule
            eq_m = EQ_LABEL_RE.search(txt)
            math_cnt = sum(1 for ch in txt if ch in MATH_SYMBOLS)
            eq_label = (eq_m.group(1) or eq_m.group(2)) if eq_m else None
            is_year_label = bool(eq_label and re.fullmatch(r"(?:19|20)\d{2}", eq_label.strip()))
            has_citation_cue = bool(re.search(r"\b(?:Ref\.|Refs\.|et\s+al\.|Fig\.|Figs\.|Table|Vol\.|pp\.)", txt, re.IGNORECASE))
            is_url = bool(re.search(r"https?://", txt, re.IGNORECASE))
            has_math_op = (
                math_cnt >= 1
                or ftxt != txt
                or (
                    not is_url
                    and bool(
                        re.search(
                            r"[=<>^_]|(?:^|\s)[+\-*/]=?(?:\s|$)|[a-zA-Z0-9\)\]]\s*[+\-*/=<>]\s*[a-zA-Z0-9\(\[]",
                            txt,
                        )
                    )
                )
            )
            is_eq_by_label = (
                eq_m is not None
                and not is_year_label
                and not has_citation_cue
                and (not in_references_section or math_cnt >= 4)
                and (
                    (has_math_op and len(txt) <= 240)
                    or (len(txt) <= 120 and len(txt.split()) <= 8 and not re.search(r"\b(?:19|20)\d{2}\b", txt))
                )
            )
            is_eq_by_symbols = (
                math_cnt >= 4
                and len(txt) <= 220
                and fs <= 1.15 * body_fs
                and (not in_references_section or math_cnt >= 6)
            )

            # Role classification
            role = "paragraph"
            if CAPTION_RE.match(txt):
                role = "caption"
                cap_counter += 1
                cap_kind = "table_caption" if re.match(r"^(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Table|Tab\.?|附表|表)", txt, re.I) else "figure_caption"
                captions.append({
                    "caption_id": f"cap_{cap_counter}",
                    "kind": cap_kind,
                    "page": pno,
                    "block_id": bid,
                    "bbox": bb,
                    "text": ftxt,
                })
            elif bb[1] > 0.78 * ph and fs <= 0.88 * body_fs and FOOTNOTE_RE.match(txt):
                role = "footnote"
                fn_counter += 1
                footnotes.append({
                    "footnote_id": f"fn_{fn_counter}",
                    "page": pno,
                    "block_id": bid,
                    "bbox": bb,
                    "text": ftxt,
                })
            elif (is_eq_by_label or is_eq_by_symbols) and not (HEADING_PREFIX_RE.match(txt) and "=" not in txt and not eq_label):
                role = "equation"
                eq_counter += 1
                equations.append({
                    "equation_id": f"eq_{eq_counter}",
                    "label": eq_label,
                    "page": pno,
                    "block_id": bid,
                    "bbox": bb,
                    "raw_text": txt,
                    "script_text": ftxt,
                    "has_unreadable": b["defects"]["has_defects"],
                })
            elif (
                "=" not in txt
                and len(txt) <= 180
                and "\n" not in txt
                and (
                    (fs >= 1.10 * body_fs and (b["is_bold"] or HEADING_PREFIX_RE.match(txt) or idx <= 3))
                    or (b["is_bold"] and HEADING_RE.match(txt))
                    or (HEADING_PREFIX_RE.match(txt) and fs >= 0.98 * body_fs and len(txt.split()) <= 16)
                )
            ):
                role = "heading"
                sec_counter += 1
                level = 1
                if re.match(r"^\d+\.\d+\.\d+", txt):
                    level = 3
                elif re.match(r"^\d+\.\d+", txt):
                    level = 2
                elif fs < 1.12 * body_fs and not re.match(r"^\d+\.?\s+", txt):
                    level = 2
                if REF_SECTION_RE.match(txt):
                    in_references_section = True
                elif re.match(
                    r"^(?:\d+\.?\s*)?(?:Appendix|Acknowledgements?|Funding|Author\s+Contributions|"
                    r"Data\s+Availability|Competing\s+Interests|Supplementary(?:\s+Material|\s+Information)?|"
                    r"附录|致谢|补充材料)\b",
                    txt,
                    re.I,
                ):
                    in_references_section = False
                b["heading_level"] = level
                sections.append({
                    "section_id": f"sec_{sec_counter}",
                    "title": txt,
                    "level": level,
                    "page_start": pno,
                    "block_id": bid,
                    "bbox": bb,
                })
            elif in_references_section:
                role = "reference"
                ref_counter += 1
                references.append({
                    "ref_id": f"ref_{ref_counter}",
                    "page": pno,
                    "block_id": bid,
                    "bbox": bb,
                    "text": ftxt,
                })

            if b["defects"]["has_defects"]:
                unextracted_regions.append({
                    "page": pno,
                    "block_id": bid,
                    "bbox": bb,
                    "reason": "encoding_defects_detected",
                    "defects": b["defects"],
                })

            b["role"] = role
            all_blocks.append({
                "block_id": bid,
                "page": pno,
                "column": b.get("column", 1),
                "role": role,
                "heading_level": b.get("heading_level"),
                "bbox": bb,
                "font_size": fs,
                "is_bold": b["is_bold"],
                "raw_text": txt,
                "text": ftxt,
                "defects": b["defects"],
            })

    # Post-process tables with IDs, captions, markdown, html, and cross-page continuation detection
    structured_tables: List[Dict[str, Any]] = []
    tbl_counter = 0
    for pno in pages:
        page_caps = [c for c in captions if c["page"] == pno and c["kind"] == "table_caption"]
        for t in tables_by_page.get(pno, []):
            tbl_counter += 1
            tid = f"tbl_{tbl_counter}"
            tb = t["bbox"]
            rows = t["rows"]

            # Match nearest caption above or below table
            matched_cap = None
            best_dist = 1e9
            for c in page_caps:
                dist = min(abs(tb[1] - c["bbox"][3]), abs(c["bbox"][1] - tb[3]))
                if dist < 120.0 and dist < best_dist:
                    best_dist = dist
                    matched_cap = c

            ncols = max((len(r) for r in rows), default=0)
            entry: Dict[str, Any] = {
                "table_id": tid,
                "page": pno,
                "bbox": tb,
                "method": t["method"],
                "num_rows": len(rows),
                "num_cols": ncols,
                "caption": matched_cap["text"] if matched_cap else None,
                "caption_block_id": matched_cap["block_id"] if matched_cap else None,
                "rows": rows,
                "markdown": _rows_to_markdown(rows),
                "html": _rows_to_html(rows),
                "continues_from": None,
                "continued_on": None,
            }

            # Cross-page continuation check against previous table
            if structured_tables:
                prev = structured_tables[-1]
                cap_txt = (entry["caption"] or "").lower()
                is_cont_caption = (
                    "continued" in cap_txt
                    or "cont." in cap_txt
                    or "cont'd" in cap_txt
                    or "续表" in cap_txt
                    or "(续)" in cap_txt
                    or "（续）" in cap_txt
                )
                same_cols_adjacent = prev["page"] == pno - 1 and prev["num_cols"] == ncols and tb[1] < 240.0
                if is_cont_caption or (same_cols_adjacent and (not entry["caption"] or prev["rows"][0] == rows[0])):
                    entry["continues_from"] = prev["table_id"]
                    prev["continued_on"] = tid

            structured_tables.append(entry)

    merged_tables = _build_merged_tables(structured_tables)
    metadata = _extract_paper_metadata(all_blocks, sections)
    page_label_map, page_numbering_discrepancy = _extract_printed_page_map(headers_footers, total_pages)

    # Mark paragraphs continuing mid-sentence across column/page breaks
    para_indices = [i for i, blk in enumerate(all_blocks) if blk.get("role") not in ("caption", "footnote")]
    for pos, idx in enumerate(para_indices[:-1]):
        cur = all_blocks[idx]
        nxt = all_blocks[para_indices[pos + 1]]
        if cur.get("role") != "paragraph" or nxt.get("role") != "paragraph":
            continue
        if cur["page"] == nxt["page"] and cur.get("column") == nxt.get("column"):
            continue
        ct = cur["raw_text"].rstrip()
        nt = nxt["raw_text"].lstrip()
        if not ct or not nt:
            continue
        if ct[-1] in ".。!！?？:：;；\"”’）)]":
            continue
        if ct[-1] in (",", "，", "、", "-") or nt[0].islower() or _is_cjk(nt[0]):
            cur["continues_across_break"] = True
            cur["continued_block_id"] = nxt["block_id"]
            nxt["continues_from_block_id"] = cur["block_id"]

    # Extract figures[] and flag image-based tables near uncovered table captions
    figures: List[Dict[str, Any]] = []
    fig_counter = 0
    matched_tbl_cap_ids = {t.get("caption_block_id") for t in structured_tables if t.get("caption_block_id")}
    for rp in raw_pages:
        pno = rp["page"]
        pw, ph = rp["width"], rp["height"]
        page_tbls = [t for t in structured_tables if t["page"] == pno]
        fig_caps = [c for c in captions if c["page"] == pno and c["kind"] == "figure_caption"]
        uncov_tbl_caps = [
            c for c in captions
            if c["page"] == pno and c["kind"] == "table_caption" and c["block_id"] not in matched_tbl_cap_ids
        ]
        for img in rp.get("images", []):
            ib = img["bbox"]
            iw, ih = ib[2] - ib[0], ib[3] - ib[1]
            is_full_scan = (iw >= 0.85 * pw and ih >= 0.85 * ph)

            def _nearest_cap(cands: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], float]:
                best_c = None
                best_d = 1e9
                for c in cands:
                    cb = c["bbox"]
                    x_ov = min(ib[2], cb[2]) - max(ib[0], cb[0])
                    cap_w = max(1.0, cb[2] - cb[0])
                    same_lane = x_ov > -20.0 or cap_w >= 0.55 * pw
                    if not same_lane:
                        continue
                    v_dist = max(0.0, max(ib[1] - cb[3], cb[1] - ib[3]))
                    if v_dist <= 180.0 and v_dist < best_d:
                        best_d = v_dist
                        best_c = c
                return best_c, best_d

            near_tbl_cap, tbl_dist = _nearest_cap(uncov_tbl_caps)
            near_fig_cap, fig_dist = _nearest_cap(fig_caps)
            overlaps_parsed_tbl = any(bbox_iou(ib, t["bbox"]) > 0.25 for t in page_tbls)

            if near_tbl_cap and not overlaps_parsed_tbl and (not near_fig_cap or tbl_dist <= fig_dist):
                unextracted_regions.append({
                    "page": pno,
                    "bbox": ib,
                    "reason": "image_based_table_near_caption",
                    "caption": near_tbl_cap["text"],
                    "caption_block_id": near_tbl_cap["block_id"],
                })
                continue

            if is_full_scan and not near_fig_cap:
                continue

            if not overlaps_parsed_tbl:
                fig_counter += 1
                figures.append({
                    "figure_id": f"fig_{fig_counter}",
                    "page": pno,
                    "bbox": ib,
                    "width_pt": round(iw, 2),
                    "height_pt": round(ih, 2),
                    "pixel_size": [img["pixel_width"], img["pixel_height"]],
                    "ext": img.get("ext"),
                    "caption": near_fig_cap["text"] if near_fig_cap else None,
                    "caption_block_id": near_fig_cap["block_id"] if near_fig_cap else None,
                })

    return {
        "file": str(pdf_path),
        "backend": "pymupdf",
        "status": "ok",
        "page_count": total_pages,
        "extracted_pages": pages,
        "body_font_size": body_fs,
        "metadata": metadata,
        "page_label_map": page_label_map,
        "page_numbering_discrepancy": page_numbering_discrepancy,
        "counts": {
            "blocks": len(all_blocks),
            "sections": len(sections),
            "tables": len(structured_tables),
            "merged_tables": len(merged_tables),
            "figures": len(figures),
            "captions": len(captions),
            "footnotes": len(footnotes),
            "equations": len(equations),
            "references": len(references),
            "headers_footers": len(headers_footers),
            "unextracted_regions": len(unextracted_regions),
        },
        "sections": sections,
        "tables": structured_tables,
        "merged_tables": merged_tables,
        "figures": figures,
        "captions": captions,
        "footnotes": footnotes,
        "equations": equations,
        "references": references,
        "headers_footers": headers_footers,
        "unextracted_regions": unextracted_regions,
        "blocks": all_blocks,
    }


def _extract_with_pdftotext_xml(pdf_path: Path, page_spec: Optional[str], pdftotext_bin: str) -> Dict[str, Any]:
    """Fallback extraction using `pdftotext -bbox-layout` XML when PyMuPDF is unavailable."""
    proc = subprocess.run(
        [pdftotext_bin, "-bbox-layout", str(pdf_path), "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"file": str(pdf_path), "backend": "pdftotext", "status": "failed", "error": proc.stderr}

    root = ET.fromstring(proc.stdout)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"
    pages_el = root.findall(f".//{ns}page")
    total_pages = len(pages_el)
    wanted = set(parse_page_spec(page_spec, total_pages))

    all_blocks: List[Dict[str, Any]] = []
    for idx, p_el in enumerate(pages_el):
        pno = idx + 1
        if pno not in wanted:
            continue
        pw = float(p_el.attrib.get("width", 612))
        p_blocks: List[Dict[str, Any]] = []
        for b_el in p_el.findall(f".//{ns}block"):
            bb = [
                round(float(b_el.attrib.get("xMin", 0)), 2),
                round(float(b_el.attrib.get("yMin", 0)), 2),
                round(float(b_el.attrib.get("xMax", 0)), 2),
                round(float(b_el.attrib.get("yMax", 0)), 2),
            ]
            lines = []
            for l_el in b_el.findall(f".//{ns}line"):
                words = [w.text or "" for w in l_el.findall(f"{ns}word")]
                lines.append({"raw_text": normalize_ligatures(" ".join(words)), "fmt_text": normalize_ligatures(" ".join(words))})
            txt = _join_block_lines(lines, False)
            if txt:
                p_blocks.append({"page": pno, "bbox": bb, "raw_text": txt, "text": txt, "font_size": 10.0, "is_bold": False, "defects": detect_encoding_defects(txt)})

        ordered = _xy_cut_order(p_blocks, pw)
        for b_idx, b in enumerate(ordered):
            b["block_id"] = f"p{pno}_b{b_idx + 1}"
            b["role"] = "caption" if CAPTION_RE.match(b["raw_text"]) else "paragraph"
            all_blocks.append(b)

    return {
        "file": str(pdf_path),
        "backend": "pdftotext_bbox_layout",
        "status": "ok",
        "page_count": total_pages,
        "extracted_pages": sorted(wanted),
        "counts": {"blocks": len(all_blocks)},
        "sections": [],
        "tables": [],
        "captions": [],
        "footnotes": [],
        "equations": [],
        "references": [],
        "headers_footers": [],
        "unextracted_regions": [],
        "blocks": all_blocks,
    }


def _extract_with_pypdf(pdf_path: Path, page_spec: Optional[str]) -> Dict[str, Any]:
    """Fallback paragraph-level extraction using pypdf when PyMuPDF and pdftotext are unavailable."""
    import pypdf

    reader = pypdf.PdfReader(str(pdf_path))
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                return {"file": str(pdf_path), "backend": "pypdf", "status": "encrypted_blocked", "error": "PDF requires a password."}
        except Exception:
            return {"file": str(pdf_path), "backend": "pypdf", "status": "encrypted_blocked", "error": "PDF requires a password."}

    total_pages = len(reader.pages)
    pages = parse_page_spec(page_spec, total_pages)
    all_blocks: List[Dict[str, Any]] = []
    unextracted_regions: List[Dict[str, Any]] = []

    for pno in pages:
        raw_txt = normalize_ligatures(reader.pages[pno - 1].extract_text() or "")
        paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n+", raw_txt) if p.strip()]
        if not paras and raw_txt.strip():
            paras = [re.sub(r"\s+", " ", raw_txt).strip()]
        for b_idx, para in enumerate(paras):
            bid = f"p{pno}_b{b_idx + 1}"
            defects = detect_encoding_defects(para)
            role = "caption" if CAPTION_RE.match(para) else ("heading" if HEADING_RE.match(para) else "paragraph")
            blk = {
                "block_id": bid,
                "page": pno,
                "column": 1,
                "role": role,
                "bbox": [0.0, 0.0, 0.0, 0.0],
                "font_size": 10.0,
                "is_bold": False,
                "raw_text": para,
                "text": para,
                "defects": defects,
            }
            all_blocks.append(blk)
            if defects["has_defects"]:
                unextracted_regions.append({
                    "page": pno,
                    "block_id": bid,
                    "bbox": [0.0, 0.0, 0.0, 0.0],
                    "reason": "encoding_defects_detected",
                    "defects": defects,
                })

    return {
        "file": str(pdf_path),
        "backend": "pypdf",
        "status": "ok",
        "page_count": total_pages,
        "extracted_pages": pages,
        "counts": {"blocks": len(all_blocks), "unextracted_regions": len(unextracted_regions)},
        "sections": [],
        "tables": [],
        "captions": [],
        "footnotes": [],
        "equations": [],
        "references": [],
        "headers_footers": [],
        "unextracted_regions": unextracted_regions,
        "blocks": all_blocks,
    }


def _render_markdown_outputs(doc_data: Dict[str, Any]) -> Tuple[str, str]:
    """Generate reading_order.md and tables.md from extracted document structure."""
    md_lines = [
        f"# Extracted Document: {Path(doc_data['file']).name}",
        f"- Backend: `{doc_data.get('backend')}` | Pages extracted: `{doc_data.get('extracted_pages')}` / `{doc_data.get('page_count')}`",
        "",
    ]
    current_page = None
    for b in doc_data.get("blocks", []):
        if b["page"] != current_page:
            current_page = b["page"]
            md_lines.extend(["", f"## Page {current_page}", ""])
        cont_tag = f"|cont->{b['continued_block_id']}" if b.get("continues_across_break") and b.get("continued_block_id") else ""
        tag = f"[{b['block_id']}|col{b.get('column', 1)}|{b['role']}{cont_tag}|bbox={b['bbox']}]"
        txt = b["text"]
        if b.get("defects", {}).get("has_defects"):
            txt = f"{txt}\n> [UNREADABLE_DEFECT: page={b['page']}, bbox={b['bbox']}, defects={b['defects']}]"
        if b["role"] == "heading":
            lvl = int(b.get("heading_level") or 1)
            hashes = "#" * min(6, 2 + max(1, lvl))
            md_lines.append(f"{hashes} {txt} `{tag}`")
        else:
            md_lines.append(f"{txt}\n`{tag}`")
        md_lines.append("")

    tbl_lines = [f"# Extracted Tables: {Path(doc_data['file']).name}", ""]
    for t in doc_data.get("tables", []):
        tbl_lines.append(f"## {t['table_id']} (Page {t['page']}, {t['num_rows']}x{t['num_cols']}, method={t['method']})")
        if t.get("caption"):
            tbl_lines.append(f"- Caption ({t.get('caption_block_id')}): {t['caption']}")
        if t.get("continues_from"):
            tbl_lines.append(f"- Continues from: `{t['continues_from']}`")
        if t.get("continued_on"):
            tbl_lines.append(f"- Continued on: `{t['continued_on']}`")
        tbl_lines.append(f"- BBox: `{t['bbox']}`")
        tbl_lines.append("")
        tbl_lines.append(t["markdown"])
        tbl_lines.append("")

    if doc_data.get("merged_tables"):
        tbl_lines.extend(["# Merged Cross-Page Tables", ""])
        for mt in doc_data["merged_tables"]:
            tbl_lines.append(
                f"## {mt['merged_table_id']} (Pages {mt['pages']}, sources={mt['source_table_ids']}, {mt['num_rows']}x{mt['num_cols']})"
            )
            if mt.get("caption"):
                tbl_lines.append(f"- Caption: {mt['caption']}")
            tbl_lines.append("")
            tbl_lines.append(mt["markdown"])
            tbl_lines.append("")

    return "\n".join(md_lines), "\n".join(tbl_lines)


def _render_outline(doc_data: Dict[str, Any]) -> str:
    """Generate compact outline.md index for low-token Agent navigation."""
    meta = doc_data.get("metadata") or {}
    lines = [
        f"# Document Outline: {Path(doc_data['file']).name}",
        f"- Status: `{doc_data.get('status')}` | Backend: `{doc_data.get('backend')}` | Pages: `{doc_data.get('page_count')}`",
        f"- Counts: `{json.dumps(doc_data.get('counts', {}), ensure_ascii=False)}`",
    ]
    if doc_data.get("page_numbering_discrepancy"):
        lines.append(f"- **Page Numbering Discrepancy Detected**: `page_label_map={json.dumps(doc_data.get('page_label_map', {}), ensure_ascii=False)}`")
    if meta.get("title"):
        lines.extend(["", "## Metadata", f"- Title (`{meta.get('title_block_id')}`): {meta['title']}"])
        if meta.get("authors_and_affiliations"):
            lines.append(f"- Authors/Affiliations (`{meta.get('author_block_ids')}`): {' | '.join(meta['authors_and_affiliations'][:4])}")
        if meta.get("keywords"):
            lines.append(f"- Keywords (`{meta.get('keywords_block_id')}`): {meta['keywords']}")
        if meta.get("abstract"):
            lines.append(f"- Abstract (`{meta.get('abstract_block_ids')}`): {meta['abstract'][:320]}{'...' if len(meta['abstract']) > 320 else ''}")
    lines.extend(["", "## Sections"])
    for s in doc_data.get("sections", []):
        indent = "  " * max(0, int(s.get("level", 1)) - 1)
        lines.append(f"{indent}- [{s['section_id']}] {s['title']} (p.{s['page_start']}, `{s['block_id']}`)")
    if not doc_data.get("sections"):
        lines.append("- (no structured headings detected; inspect reading_order.md directly)")

    lines.extend(["", "## Tables"])
    for t in doc_data.get("tables", []):
        cap = t.get("caption") or "(no caption)"
        cont = f" continues `{t['continues_from']}`" if t.get("continues_from") else ""
        lines.append(f"- `{t['table_id']}` (p.{t['page']}, {t['num_rows']}x{t['num_cols']}, {t['method']}{cont}): {cap}")
    for mt in doc_data.get("merged_tables", []):
        lines.append(f"- `{mt['merged_table_id']}` (merged pages {mt['pages']}, {mt['num_rows']}x{mt['num_cols']}, sources={mt['source_table_ids']})")
    if not doc_data.get("tables"):
        lines.append("- (none)")

    lines.extend(["", "## Equations"])
    for eq in doc_data.get("equations", [])[:30]:
        lbl = f"({eq['label']})" if eq.get("label") else eq["equation_id"]
        lines.append(f"- `{eq['equation_id']}` {lbl} (p.{eq['page']}, `{eq['block_id']}`): {eq['script_text'][:120]}")
    if not doc_data.get("equations"):
        lines.append("- (none)")

    lines.extend(["", "## Figures"])
    for fig in doc_data.get("figures", []):
        cap = fig.get("caption") or "(no caption)"
        lines.append(f"- `{fig['figure_id']}` (p.{fig['page']}, bbox=`{fig['bbox']}`): {cap}")
    if not doc_data.get("figures"):
        lines.append("- (none)")

    lines.extend(["", "## Unextracted / Defect Regions (Require Visual/OCR Inspection)"])
    for u in doc_data.get("unextracted_regions", []):
        lines.append(f"- p.{u['page']} bbox=`{u.get('bbox')}` reason=`{u.get('reason')}` block=`{u.get('block_id')}`")
    if not doc_data.get("unextracted_regions"):
        lines.append("- (none)")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract layout-ordered text, sections, tables, equations, and anchors from a PDF.")
    parser.add_argument("pdf_path", help="Path to the input PDF file.")
    parser.add_argument("--out-dir", required=True, help="Directory to write document.json, reading_order.md, tables.json, tables.md, outline.md.")
    parser.add_argument("--pages", default=None, help="1-based page specification, e.g. '1-3,5'.")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(json.dumps({"error": f"PDF not found: {pdf_path}"}), file=sys.stderr)
        return 2

    caps = discover_capabilities()
    if caps["python_modules"].get("pymupdf_fitz"):
        doc_data = _extract_with_fitz(pdf_path, args.pages, caps)
    elif caps["cli_tools"].get("pdftotext"):
        doc_data = _extract_with_pdftotext_xml(pdf_path, args.pages, caps["cli_tools"]["pdftotext"])
    elif caps["python_modules"].get("pypdf"):
        doc_data = _extract_with_pypdf(pdf_path, args.pages)
    else:
        print(json.dumps({"error": "No layout-capable PDF backend (pymupdf, pdftotext, or pypdf) found."}, ensure_ascii=False), file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "document.json").write_text(json.dumps(doc_data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "tables.json").write_text(json.dumps(doc_data.get("tables", []), ensure_ascii=False, indent=2), encoding="utf-8")

    reading_md, tables_md = _render_markdown_outputs(doc_data)
    (out_dir / "reading_order.md").write_text(reading_md, encoding="utf-8")
    (out_dir / "tables.md").write_text(tables_md, encoding="utf-8")
    (out_dir / "outline.md").write_text(_render_outline(doc_data), encoding="utf-8")

    summary = {
        "status": doc_data.get("status"),
        "backend": doc_data.get("backend"),
        "page_count": doc_data.get("page_count"),
        "counts": doc_data.get("counts"),
        "out_dir": str(out_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if doc_data.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
