#!/usr/bin/env python3
"""Layout-aware scientific PDF probing and extraction for Nanopublication pipelines.

Reconstructs multi-column reading order (recursive X-Y cut + spanning-band peeling),
strips repeating headers/footers, extracts bordered/three-line/continued tables,
preserves subscripts/superscripts/Symbol-font Greek letters, extracts verified PubInfo
metadata (DOI, year, journal, author-vs-editor filtering), and flags damaged stat glyphs.
"""

from __future__ import annotations

import re
import statistics
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from doc_common import (
    DOI_PATTERN,
    bbox_contains_center,
    bbox_iou,
    decode_symbol_pua,
    detect_encoding_defects,
    discover_capabilities,
    import_pymupdf,
    is_cjk,
    merge_bbox,
    normalize_header_footer_key,
    normalize_ligatures,
    parse_page_spec,
    sha256_file,
)

CAPTION_RE = re.compile(
    r"^(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Figure|Fig\.?|Table|Tab\.?|Scheme|Algorithm|Listing|附图|附表|图|表)\s*[A-Za-z]?\d+(?:[.\-]\d+)*[A-Za-z]?(?=\s*(?:[:：.|—–\-]|\(|（|\s|$))",
    re.I,
)
_CAPTION_CONT_RE = re.compile(r"^\s*(?:\((?:continued|cont\.?|cont'd)\)|（续）|\(续\))\s*", re.I)
_CAPTION_PROSE_VERB_RE = re.compile(
    r"^\s*(?:is|are|was|were|has|have|had|contains?|shows?|demonstrates?|illustrates?|presents?|summarizes?|compares?|lists?|depicts?|plots?|gives?|reports?|provides?|indicates?|suggests?|reveals?|highlights?|describes?|displays?|summarises?|can|may|will|also|表明|显示|给出|展示|说明|列出|总结|揭示)\b",
    re.I,
)
HEADING_RE = re.compile(
    r"^(?:"
    r"Abstract|Introduction|Background|Related\s+Work|Methods?|Methodology|Materials?\s+and\s+Methods?|"
    r"Experimental(?:\s+Setup)?|Results?(?:\s+and\s+Discussion)?|Discussion|Conclusions?|Limitations?|"
    r"Acknowledgements?|References|Bibliography|Appendix(?:\s+[A-Z0-9]+)?|"
    r"摘要|引言|前言|研究背景|相关工作|方法|材料与方法|实验|实验结果|结果与讨论|结果与分析|讨论|局限性?|结论|结语|致谢|参考文献|附录(?:\s*[A-Z0-9]+)?|"
    r"(?:\d+(?:\.\d+){0,3}\.?|[IVX]{1,5}\.|[A-Z]\.|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+、|第[一二三四五六七八九十\d]+[章节])\s*\S+"
    r")$",
    re.I,
)
HEADING_PREFIX_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+){0,3}\.?|[IVX]{1,5}\.|[A-Z]\.|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+、|第[一二三四五六七八九十\d]+[章节])\s+[A-Z\u4e00-\u9fff][^\n]{2,}|"
    r"(?:Abstract|Introduction|Background|Related\s+Work|Methods?|Methodology|Materials?\s+and\s+Methods?|"
    r"Experimental\s+Setup|Results?(?:\s+and\s+Discussion)?|Discussion|Limitations?|Conclusions?|Acknowledgements?|References|Bibliography|Appendix)\b)",
    re.I,
)
REF_SECTION_RE = re.compile(r"^(?:\d+\.?\s*)?(?:References|Bibliography|参考文献)\b", re.I)
FOOTNOTE_RE = re.compile(r"^(?:\*+|†|‡|§|\d{1,2})\s*\S+")
EQ_LABEL_RE = re.compile(r"(?:\((\d+(?:\.\d+)?[a-z]?)\)|\[(\d+(?:\.\d+)?[a-z]?)\])$")
PURE_EQ_LABEL_RE = re.compile(r"^\s*(?:\((\d+(?:\.\d+)?[a-z]?)\)|\[(\d+(?:\.\d+)?[a-z]?)\])\s*$")
MATH_SYMBOLS = set("=≈≠≤≥<>±×÷·∑∏∫∂∇√∞∈∉⊂⊆∪∩∀∃αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ→←↔⇒⇔")
EDITOR_OR_META_RE = re.compile(
    r"^(?:Academic\s+Editor|Editor(?:s)?|Edited\s+by|Reviewed\s+by|Received|Accepted|Published|Citation|Copyright|Funding|Data\s+Availability|Competing\s+Interests|Corresponding\s+author)\b",
    re.I,
)
FRONTMATTER_BOX_RE = re.compile(
    r"\b(?:Citation:|Academic\s+Editor:|Received\s+[A-Z][a-z]+|Accepted\s+[A-Z][a-z]+|Published\s+[A-Z][a-z]+|Competing\s+Interests:|Copyright:|open-access\s+article)\b",
    re.I,
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
    first = rest.lstrip()
    if not first or first[0] in (".", ":", "：", "|", "—", "–", "-"):
        return True
    if _CAPTION_PROSE_VERB_RE.match(first):
        return False
    if re.match(r"^(?:附图|附表|图|表)", st) and is_cjk(first[0]):
        return len(st) <= 180
    return len(first.split()) <= 5 and not st.endswith((".", "。"))


def _span_text_with_scripts(spans: List[Dict[str, Any]]) -> Tuple[str, str, bool]:
    """Reconstruct raw line text and script-annotated text (^{...}, _{...})."""
    if not spans:
        return "", "", False
    char_sizes: List[float] = []
    non_flag_sizes: List[float] = []
    for s in spans:
        st = s.get("text", "").strip()
        sfs = float(s.get("size", 0.0))
        if not st or sfs <= 0:
            continue
        rep = min(len(st), 6)
        char_sizes.extend([sfs] * rep)
        if not (int(s.get("flags", 0)) & 1):
            non_flag_sizes.extend([sfs] * rep)
    ref_pool = sorted(non_flag_sizes or char_sizes)
    med_fs = ref_pool[min(len(ref_pool) - 1, int(0.85 * len(ref_pool)))] if ref_pool else 10.0

    base_ys = [
        0.5 * (s.get("bbox", [0, 0, 0, 0])[1] + s.get("bbox", [0, 0, 0, 0])[3])
        for s in spans
        if s.get("text", "").strip() and float(s.get("size", med_fs)) >= 0.88 * med_fs and not (int(s.get("flags", 0)) & 1)
    ]
    med_cy = statistics.median(base_ys) if base_ys else 0.0

    raw_parts: List[str] = []
    fmt_runs: List[Tuple[str, str]] = []
    has_unreadable = False

    for s in spans:
        txt = decode_symbol_pua(normalize_ligatures(s.get("text", "")), str(s.get("font", "")))
        if not txt:
            continue
        if detect_encoding_defects(txt)["has_defects"]:
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

    fmt_parts = [f"^{{{v}}}" if k == "super" else (f"_{{{v}}}" if k == "sub" else v) for k, v in fmt_runs]
    return "".join(raw_parts), "".join(fmt_parts), has_unreadable


def _join_block_lines(lines_info: List[Dict[str, Any]], use_formatted: bool = False) -> str:
    key = "fmt_text" if use_formatted else "raw_text"
    out = ""
    for item in lines_info:
        seg = item.get(key, "").rstrip()
        if not seg:
            continue
        if not out:
            out = seg
        elif out.endswith("-") and len(out) >= 2 and out[-2].isalpha() and seg[0].islower():
            out = out[:-1] + seg
        elif is_cjk(out[-1]) and is_cjk(seg[0]):
            out = out + seg
        else:
            out = out + " " + seg
    return out.strip()


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
                gap_pts = ((i - run_start) / bins) * width
                if gap_pts >= 6.0 and any(occ[j] > 0 for j in range(0, run_start)) and any(occ[j] > 0 for j in range(i, bins)):
                    gutters.append(content_x0 + ((run_start + i) / 2.0) / bins * width)
            run_start = None
    return gutters[:2]


def _sort_leaf_by_rows(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(blocks) <= 1:
        return list(blocks)
    by_y = sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))
    rows: List[Tuple[float, float, List[Dict[str, Any]]]] = []
    for b in by_y:
        y0, y1 = float(b["bbox"][1]), float(b["bbox"][3])
        h = max(1.0, y1 - y0)
        if rows:
            ry0, ry1, rlist = rows[-1]
            if min(y1, ry1) - max(y0, ry0) >= 0.45 * min(h, max(1.0, ry1 - ry0)):
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
    if len(blocks) <= 1:
        return [list(blocks)] if blocks else []
    bands: List[Tuple[float, float, List[Dict[str, Any]]]] = []
    for b in sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0])):
        y0, y1 = float(b["bbox"][1]), float(b["bbox"][3])
        if bands and y0 <= bands[-1][1] + min_y_gap:
            by0, by1, blist = bands[-1]
            blist.append(b)
            bands[-1] = (by0, max(by1, y1), blist)
        else:
            bands.append((y0, y1, [b]))
    return [blist for _, _, blist in bands]


def _xy_cut_recursive(
    blocks: List[Dict[str, Any]], page_w: float, col_offset: int = 0, allow_y_cut: bool = True, depth: int = 0
) -> List[Dict[str, Any]]:
    """Order blocks via recursive X-cut gutters, Y-whitespace bands, and spanning-block peeling."""
    if not blocks:
        return []
    if len(blocks) == 1 or depth >= 6:
        for b in blocks:
            bw = b["bbox"][2] - b["bbox"][0]
            b["column"] = 0 if (col_offset == 0 and bw >= 0.62 * page_w) else max(1, col_offset or 1)
        return _sort_leaf_by_rows(blocks)

    bx0 = min(b["bbox"][0] for b in blocks)
    bx1 = max(b["bbox"][2] for b in blocks)
    gutters = _detect_column_gutters(blocks, bx0, bx1)
    if gutters:
        cols: List[List[Dict[str, Any]]] = [[] for _ in range(len(gutters) + 1)]
        for b in blocks:
            cx = 0.5 * (b["bbox"][0] + b["bbox"][2])
            c_idx = sum(1 for g in gutters if cx > g)
            cols[c_idx].append(b)
        non_empty = [c for c in cols if c]
        if len(non_empty) >= 2:
            ordered: List[Dict[str, Any]] = []
            for seq_idx, col_blocks in enumerate(non_empty):
                sub_col = (col_offset + seq_idx) if col_offset > 0 else (seq_idx + 1)
                ordered.extend(_xy_cut_recursive(col_blocks, page_w, col_offset=sub_col, allow_y_cut=True, depth=depth + 1))
            return ordered

    if allow_y_cut:
        y_bands = _split_y_bands(blocks)
        if len(y_bands) >= 2:
            merged_bands: List[List[Dict[str, Any]]] = [y_bands[0]]
            for nxt_band in y_bands[1:]:
                cur_band = merged_bands[-1]
                cur_g = _detect_column_gutters(cur_band, min(b["bbox"][0] for b in cur_band), max(b["bbox"][2] for b in cur_band))
                comb = cur_band + nxt_band
                comb_g = _detect_column_gutters(comb, min(b["bbox"][0] for b in comb), max(b["bbox"][2] for b in comb))
                if cur_g and comb_g and len(comb_g) >= len(cur_g):
                    nxt_g = _detect_column_gutters(nxt_band, min(b["bbox"][0] for b in nxt_band), max(b["bbox"][2] for b in nxt_band))
                    y_gap = min(b["bbox"][1] for b in nxt_band) - max(b["bbox"][3] for b in cur_band)
                    crosses = any(b["bbox"][0] < g - 2.0 and b["bbox"][2] > g + 2.0 for b in nxt_band for g in comb_g)
                    if not crosses and (nxt_g or y_gap <= 18.0):
                        merged_bands[-1] = comb
                        continue
                merged_bands.append(nxt_band)
            ordered = []
            for band in merged_bands:
                ordered.extend(_xy_cut_recursive(band, page_w, col_offset=col_offset, allow_y_cut=False, depth=depth + 1))
            return ordered

    span_x = bx1 - bx0
    content_w = max(page_w * 0.6, span_x)
    mid_x = 0.5 * (bx0 + bx1) if span_x >= 0.62 * page_w else 0.5 * page_w
    spanning, regular = [], []
    for b in blocks:
        bw = b["bbox"][2] - b["bbox"][0]
        if col_offset == 0 and bw >= 0.62 * content_w and b["bbox"][0] < mid_x - 0.05 * content_w and b["bbox"][2] > mid_x + 0.05 * content_w:
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


def _coalesce_same_line_fragments(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(blocks) <= 1:
        return blocks
    merged: List[Dict[str, Any]] = [dict(blocks[0])]
    for nxt in blocks[1:]:
        cur = merged[-1]
        if cur.get("column") != nxt.get("column") or cur.get("page") != nxt.get("page"):
            merged.append(dict(nxt))
            continue
        if PURE_EQ_LABEL_RE.match(nxt["raw_text"]) or _is_caption_text(cur["raw_text"]) or _is_caption_text(nxt["raw_text"]):
            merged.append(dict(nxt))
            continue
        cb, nb = cur["bbox"], nxt["bbox"]
        ch, nh = max(1.0, cb[3] - cb[1]), max(1.0, nb[3] - nb[1])
        fs_max = max(float(cur.get("font_size") or 10.0), float(nxt.get("font_size") or 10.0))
        y_ov = min(cb[3], nb[3]) - max(cb[1], nb[1])
        dx = nb[0] - cb[2]
        nxt_txt = nxt.get("text", "")
        is_script = nxt_txt.lstrip().startswith(("^{", "_{"))
        if ch <= 2.4 * fs_max and nh <= 2.4 * fs_max and y_ov >= 0.45 * min(ch, nh) and (
            (-3.0 <= dx <= 10.0) or (is_script and -3.0 <= dx <= 22.0)
        ):
            cur_r, nxt_r = cur["raw_text"].rstrip(), nxt["raw_text"].lstrip()
            cur_t, nxt_t = cur["text"].rstrip(), nxt_txt.lstrip()
            no_sp = is_script or (bool(cur_r and nxt_r) and is_cjk(cur_r[-1]) and is_cjk(nxt_r[0]))
            cur["raw_text"] = (cur_r + nxt_r) if no_sp else (cur_r + " " + nxt_r)
            cur["text"] = (cur_t + nxt_t) if no_sp else (cur_t + " " + nxt_t)
            cur["bbox"] = merge_bbox([cb, nb])
            cur["font_size"] = max(float(cur.get("font_size") or 0.0), float(nxt.get("font_size") or 0.0))
            cur["defects"] = detect_encoding_defects(cur["raw_text"])
            continue
        merged.append(dict(nxt))
    return merged


def _merge_split_equation_labels(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    used: set[int] = set()
    lbl_to_formula: Dict[int, int] = {}
    for idx, b in enumerate(blocks):
        if not PURE_EQ_LABEL_RE.match(b["raw_text"]):
            continue
        cy = 0.5 * (b["bbox"][1] + b["bbox"][3])
        best_j, best_dx = None, 1e9
        for j, cand in enumerate(blocks):
            if j == idx or j in used or PURE_EQ_LABEL_RE.match(cand["raw_text"]):
                continue
            ccy = 0.5 * (cand["bbox"][1] + cand["bbox"][3])
            if abs(ccy - cy) <= 18.0 and cand["bbox"][2] <= b["bbox"][0] + 12.0 and len(cand["raw_text"]) <= 200:
                dx = b["bbox"][0] - cand["bbox"][2]
                if dx < best_dx:
                    best_dx, best_j = dx, j
        if best_j is not None:
            used.add(idx)
            lbl_to_formula[best_j] = idx
    out: List[Dict[str, Any]] = []
    for idx, b in enumerate(blocks):
        if idx in used:
            continue
        if idx in lbl_to_formula:
            lb = blocks[lbl_to_formula[idx]]
            b = dict(b)
            b["raw_text"] = f"{b['raw_text']} {lb['raw_text'].strip()}"
            b["text"] = f"{b['text']} {lb['text'].strip()}"
            b["bbox"] = merge_bbox([b["bbox"], lb["bbox"]])
        out.append(b)
    return out


def _bbox_containment(a: List[float], b: List[float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    return inter / min(max(1e-6, (a[2] - a[0]) * (a[3] - a[1])), max(1e-6, (b[2] - b[0]) * (b[3] - b[1])))


def _clean_table_rows(rows: List[List[str]]) -> List[List[str]]:
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    norm = [[re.sub(r"\s+", " ", (c or "")).strip() for c in (r + [""] * (ncols - len(r)))] for r in rows]
    norm = [r for r in norm if any(c for c in r)]
    if not norm:
        return []
    keep = [c for c in range(ncols) if any(r[c] for r in norm)]
    return [[r[c] for c in keep] for r in norm] if keep else []


def _rows_to_markdown(rows: List[List[str]], n_header_rows: int = 1, headers: Optional[List[str]] = None) -> str:
    clean = _clean_table_rows(rows)
    if not clean:
        return ""
    ncols = len(clean[0])
    hdr = headers if (headers and len(headers) == ncols) else clean[0]
    lines = [
        "| " + " | ".join(str(c).replace("|", "\\|") for c in hdr) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    for r in clean[max(1, n_header_rows):]:
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
    return "\n".join(lines)


def _is_false_frontmatter_table(clean: List[List[str]], pno: int, has_table_cap: bool = False) -> bool:
    """Reject Page-1 title/abstract/citation boxes or single-letter drop-cap split artifacts."""
    if not clean:
        return True
    flat = " ".join(" ".join(r) for r in clean)
    if FRONTMATTER_BOX_RE.search(flat) and not has_table_cap:
        return True
    if pno == 1 and not has_table_cap:
        short_col0 = sum(1 for r in clean if r and len(r[0].strip()) <= 2)
        if short_col0 >= max(3, int(0.35 * len(clean))):
            return True
        if re.search(r"\bAbstract\b", flat) and len(clean[0]) <= 3:
            return True
    return False


def _structure_table_rows(raw_rows: List[List[str]], header_rows_hint: Optional[int] = None) -> Dict[str, Any]:
    rows = _clean_table_rows(raw_rows)
    embedded_caption, note = None, None
    # Strip embedded caption row (row 0 or row 1 if row 0 is empty spacer)
    while len(rows) >= 3:
        r0_ne = [c for c in rows[0] if c]
        if len(r0_ne) == 1 and _is_caption_text(r0_ne[0]):
            embedded_caption = embedded_caption or r0_ne[0]
            rows = _clean_table_rows(rows[1:])
        else:
            break
    # Strip trailing footnote / DOI rows
    while len(rows) >= 3:
        rl_ne = [c for c in rows[-1] if c]
        if len(rl_ne) == 1 and (
            re.match(r"^(?:DOI|Note|Notes|Source|Sources|Abbreviations?|Predictions|All\s+predictions|注)[:.\s]", rl_ne[0], re.I)
            or (len(rl_ne[0]) >= 85 and "doi:" in rl_ne[0].lower())
        ):
            note = rl_ne[0] if not note else f"{rl_ne[0]} {note}"
            rows = _clean_table_rows(rows[:-1])
        else:
            break
    if not rows:
        return {"rows": [], "header_rows": 1, "headers": [], "embedded_caption": embedded_caption, "note": note}

    ncols = len(rows[0])
    n_hdr = 1
    if header_rows_hint is not None and header_rows_hint >= 2 and len(rows) > header_rows_hint:
        n_hdr = min(4, header_rows_hint)
    elif len(rows) >= 3:
        # Detect multi-line wrapped headers (rows with blank cols where subsequent header rows complete them)
        r0, r1 = rows[0], rows[1]
        if any(r0[c] == "" and r1[c] != "" for c in range(ncols)) and sum(1 for c in r1 if re.match(r"^-?\d+(?:\.\d+)?%?$", c)) <= max(0, ncols // 2):
            n_hdr = 2
            while n_hdr < min(4, len(rows) - 1):
                rn = rows[n_hdr]
                non_empty = [c for c in rn if c]
                if not non_empty or sum(1 for c in rn if re.match(r"^-?\d+(?:,\d{3})*(?:\.\d+)?%?$", c)) > max(1, ncols // 3):
                    break
                if any(rows[n_hdr - 1][c] == "" and rn[c] != "" for c in range(ncols)) or any(len(rn[c]) <= 16 and rows[0][c] == "" for c in range(ncols) if rn[c]):
                    n_hdr += 1
                else:
                    break

    if n_hdr == 1:
        headers = list(rows[0])
    else:
        r0 = rows[0]
        exp_top = list(r0)
        grp_counts: Counter[int] = Counter()
        grp_owner = [-1] * ncols
        last_idx = -1
        for c in range(ncols):
            if r0[c]:
                last_idx = c
                grp_owner[c] = c
                grp_counts[c] += 1
            elif last_idx >= 0 and any(rows[hr][c] for hr in range(1, min(n_hdr, len(rows)))):
                exp_top[c] = r0[last_idx]
                grp_owner[c] = last_idx
                grp_counts[last_idx] += 1
        headers = []
        for c in range(ncols):
            sub = " ".join(rows[hr][c] for hr in range(1, min(n_hdr, len(rows))) if rows[hr][c]).strip()
            top = exp_top[c].strip()
            owner = grp_owner[c]
            if top and sub:
                headers.append(f"{top} / {sub}" if (owner >= 0 and grp_counts[owner] >= 2) else f"{top} {sub}")
            else:
                headers.append(sub or top)
    return {"rows": rows, "header_rows": n_hdr, "headers": headers, "embedded_caption": embedded_caption, "note": note}


def _collect_pdfplumber_hrules(page: Any, candidates: List[Tuple[str, List[float], List[List[str]], Optional[int]]]) -> List[Dict[str, float]]:
    h_rules: List[Dict[str, float]] = []
    for item_list, y_key_a, y_key_b, max_h in (
        (page.lines or [], "y0", "y1", 2.0),
        (page.rects or [], "top", "bottom", 3.0),
    ):
        for el in item_list:
            if abs(el[y_key_a] - el[y_key_b]) <= max_h and (el["x1"] - el["x0"]) > 0.25 * page.width:
                rbox = [float(el["x0"]), float(el["top"]), float(el["x1"]), float(el["bottom"])]
                if not any(bbox_contains_center(cb, rbox, margin=3.0) for _, cb, _, _ in candidates):
                    h_rules.append({"x0": rbox[0], "x1": rbox[2], "top": rbox[1], "bottom": rbox[3]})
    return h_rules


def _extract_tables_pdfplumber(pdf_path: Path, pages: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    import pdfplumber  # type: ignore

    by_page: Dict[int, List[Dict[str, Any]]] = {p: [] for p in pages}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pno in pages:
            if pno < 1 or pno > len(pdf.pages):
                continue
            page = pdf.pages[pno - 1]
            cands: List[Tuple[str, List[float], List[List[str]], Optional[int]]] = []
            for tbl in page.find_tables(table_settings={"text_x_tolerance": 1.0}):
                data = tbl.extract() or []
                clean = _clean_table_rows([[normalize_ligatures(c or "").strip() for c in row] for row in data])
                if len(clean) >= 2 and len(clean[0]) >= 2 and not _is_false_frontmatter_table(clean, pno, has_table_cap=any("table" in " ".join(r).lower() for r in clean[:2])):
                    cands.append(("pdfplumber_lines", [round(x, 2) for x in tbl.bbox], clean, None))

            cap_boxes: List[Tuple[float, float, float]] = []
            try:
                for tln in (page.extract_text_lines() or []):
                    if _is_caption_text(normalize_ligatures(tln.get("text") or "").strip()):
                        cap_boxes.append((float(tln["x0"]), float(tln["x1"]), 0.5 * (float(tln["top"]) + float(tln["bottom"]))))
            except Exception:
                pass

            h_rules = _collect_pdfplumber_hrules(page, cands)
            if len(h_rules) >= 2:
                lanes: List[Dict[str, Any]] = []
                for ln in sorted(h_rules, key=lambda r: (-(r["x1"] - r["x0"]), r["x0"], r["top"])):
                    lw = max(1e-3, ln["x1"] - ln["x0"])
                    best_lane, best_ratio = None, 0.0
                    for lane in lanes:
                        lane_w = max(1e-3, lane["x1"] - lane["x0"])
                        ov = max(0.0, min(ln["x1"], lane["x1"]) - max(ln["x0"], lane["x0"]))
                        ratio = ov / max(lw, lane_w)
                        if ov >= 0.5 * min(lw, lane_w) and ratio >= 0.45 and ratio > best_ratio:
                            best_ratio, best_lane = ratio, lane
                    if best_lane is not None:
                        best_lane["rules"].append(ln)
                    else:
                        lanes.append({"x0": ln["x0"], "x1": ln["x1"], "rules": [ln]})
                groups: List[List[Dict[str, float]]] = []
                for lane in lanes:
                    lrules = sorted(lane["rules"], key=lambda r: r["top"])
                    if len(lrules) < 2:
                        continue
                    lgrps: List[List[Dict[str, float]]] = [[lrules[0]]]
                    lane_w = max(1e-3, lane["x1"] - lane["x0"])
                    for ln in lrules[1:]:
                        prev = lgrps[-1]
                        gap = ln["top"] - prev[-1]["top"]
                        same_span = abs(ln["x0"] - prev[0]["x0"]) <= 35.0 and abs(ln["x1"] - prev[0]["x1"]) <= 35.0
                        cap_between = any(
                            prev[-1]["bottom"] < cy < ln["top"]
                            and cx1 >= lane["x0"] - 30.0 and cx0 <= lane["x1"] + 30.0
                            and ((cx1 - cx0) >= 0.55 * page.width or (min(cx1, lane["x1"]) - max(cx0, lane["x0"])) >= 0.25 * min(max(1.0, cx1 - cx0), lane_w))
                            for cx0, cx1, cy in cap_boxes
                        )
                        if not cap_between and (gap <= 260.0 or (len(prev) == 2 and same_span and gap <= 550.0)):
                            prev.append(ln)
                        else:
                            lgrps.append([ln])
                    groups.extend(lgrps)
                for grp in groups:
                    if len(grp) < 2:
                        continue
                    gx0 = max(0.0, min(r["x0"] for r in grp) - 4.0)
                    gx1 = min(float(page.width), max(r["x1"] for r in grp) + 4.0)
                    gtop = max(0.0, min(r["top"] for r in grp) - 2.0)
                    gbot = min(float(page.height), max(r["bottom"] for r in grp) + 2.0)
                    if gbot - gtop < 20.0:
                        continue
                    gbox = [round(gx0, 2), round(gtop, 2), round(gx1, 2), round(gbot, 2)]
                    if any(bbox_iou(gbox, cb) > 0.25 or bbox_contains_center(gbox, cb, margin=2.0) for _, cb, _, _ in cands):
                        continue
                    has_tcap_near = any(abs(cy - gtop) <= 80.0 and cx1 >= gx0 - 30.0 and cx0 <= gx1 + 30.0 for cx0, cx1, cy in cap_boxes)
                    hdr_hint = None
                    if len(grp) >= 3 and (grp[1]["top"] - grp[0]["bottom"]) >= 16.0:
                        try:
                            hlines = page.crop((gx0, max(0.0, grp[0]["bottom"] - 1.0), gx1, grp[1]["top"] + 1.0)).extract_text_lines() or []
                            if len(hlines) >= 2:
                                hdr_hint = len(hlines)
                        except Exception:
                            pass
                    for tbl in page.crop((gx0, gtop, gx1, gbot)).find_tables(table_settings={
                        "vertical_strategy": "text", "horizontal_strategy": "text",
                        "explicit_vertical_lines": [gx0, gx1], "snap_y_tolerance": 4, "intersection_tolerance": 4,
                        "min_words_vertical": 2, "min_words_horizontal": 1, "text_x_tolerance": 1.0,
                    }):
                        data = tbl.extract() or []
                        clean = _clean_table_rows([[normalize_ligatures(c or "").strip() for c in row] for row in data])
                        if len(clean) >= 2 and len(clean[0]) >= 2 and not _is_false_frontmatter_table(clean, pno, has_table_cap=has_tcap_near):
                            cands.append(("pdfplumber_threeline", gbox, clean, hdr_hint))

            kept: List[Dict[str, Any]] = []
            for method, bbox, rows, h_hint in cands:
                if any(bbox_iou(bbox, k["bbox"]) > 0.40 or _bbox_containment(bbox, k["bbox"]) >= 0.65 for k in kept):
                    continue
                kept.append({"method": method, "bbox": bbox, "rows": rows, "header_rows_hint": h_hint})
            by_page[pno] = kept
    return by_page


def _extract_tables_fitz(doc: Any, pages: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    by_page: Dict[int, List[Dict[str, Any]]] = {p: [] for p in pages}
    for pno in pages:
        page = doc[pno - 1]
        if not hasattr(page, "find_tables"):
            continue
        try:
            for tbl in getattr(page.find_tables(), "tables", []):
                clean = _clean_table_rows([[normalize_ligatures(c or "").strip() for c in r] for r in (tbl.extract() or [])])
                if len(clean) >= 2 and len(clean[0]) >= 2:
                    avg_len = sum(len(c) for r in clean for c in r) / max(1, sum(len(r) for r in clean))
                    has_cap = any(_is_caption_text(c) for r in clean[:2] for c in r)
                    if (avg_len <= 180.0 or has_cap) and not _is_false_frontmatter_table(clean, pno, has_table_cap=has_cap):
                        by_page[pno].append({"method": "pymupdf_find_tables", "bbox": [round(float(x), 2) for x in tbl.bbox], "rows": clean})
        except Exception:
            pass
    return by_page


def _split_wide_multicolumn_lines(lines: List[Dict[str, Any]], pw: float) -> List[List[Dict[str, Any]]]:
    if len(lines) < 2 or pw <= 50.0:
        return [lines]
    valid = [ln.get("bbox") for ln in lines if ln.get("bbox") and (ln["bbox"][2] - ln["bbox"][0]) > 1.0]
    if len(valid) < 2:
        return [lines]
    bins = 200
    occ = [0] * bins
    for bb in valid:
        x0, x1 = float(bb[0]), float(bb[2])
        if x1 - x0 > 6.0:
            x0 += 1.0
            x1 -= 1.0
        for i in range(max(0, min(bins - 1, int((x0 / pw) * bins))), max(0, min(bins - 1, int((x1 / pw) * bins))) + 1):
            occ[i] += 1
    gutters: List[float] = []
    run_start: Optional[int] = None
    min_gap = max(8.0, 0.014 * pw)
    for i in range(int(0.10 * bins), int(0.90 * bins) + 1):
        if occ[i] == 0:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                if ((i - run_start) / bins) * pw >= min_gap and any(occ[j] > 0 for j in range(0, run_start)) and any(occ[j] > 0 for j in range(i, bins)):
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
        if any(bb[0] < g - 2.0 and bb[2] > g + 2.0 for g in gutters):
            return [lines]
        cx = 0.5 * (bb[0] + bb[2])
        buckets[sum(1 for g in gutters if cx > g)].append(ln)
    non_empty = [b for b in buckets if b]
    return non_empty if len(non_empty) >= 2 else [lines]


def _extract_printed_page_map(headers_footers: List[Dict[str, Any]]) -> Tuple[Dict[int, str], bool]:
    label_map: Dict[int, str] = {}
    seq: List[Tuple[int, int]] = []
    for hf in headers_footers:
        pno = int(hf["page"])
        m = re.search(r"(?:page|pp\.?|第)\s*(\d+)\b|^\s*[-–—]?\s*(\d{1,4})\s*[-–—]?\s*$", hf["text"].strip(), re.I)
        if m and pno not in label_map:
            lbl = m.group(1) or m.group(2)
            label_map[pno] = lbl
            if lbl.isdigit():
                seq.append((pno, int(lbl)))
    discrepancy = False
    if len(seq) >= 2:
        seq.sort()
        discrepancy = any((n2 - n1) != (p2 - p1) for (p1, n1), (p2, n2) in zip(seq, seq[1:]))
    return label_map, discrepancy


def _clean_author_list(raw_lines: List[str]) -> List[str]:
    """Split comma/semicolon-separated author lines and strip trailing superscript affiliation markers."""
    authors: List[str] = []
    for line in raw_lines:
        cleaned_line = re.sub(r"\^\{[^}]*\}", "", line)
        for part in re.split(r"\s*,\s*(?![^()]*\))|\s*;\s*|\s+and\s+", cleaned_line):
            s = re.sub(r"\s*(?:\d+(?:,\d+)*|[*†‡§]+)+\s*$", "", part.strip()).strip(" ,;*†‡")
            s = re.sub(r"\s+", " ", s)
            if len(s) >= 3 and not re.match(r"^(?:[\d,\-\s*]+|and|et al\.)$", s, re.I):
                authors.append(s)
    return authors


def _extract_metadata(
    blocks: List[Dict[str, Any]],
    headers_footers: List[Dict[str, Any]],
    pdf_meta: Optional[Dict[str, Any]] = None,
    pdf_path: Optional[Path] = None,
    raw_p1_text: str = "",
) -> Dict[str, Any]:
    """Extract verified PubInfo metadata (title, authors excluding editors, year, journal, DOI, sha256)."""
    p1 = [b for b in blocks if b["page"] == 1 and b["role"] not in ("caption", "footnote")]
    top = [b for b in p1 if b["bbox"][1] < 480.0 and len(b["raw_text"]) >= 4]
    title_cands = [b for b in top if not HEADING_PREFIX_RE.match(b["raw_text"].strip()) and not EDITOR_OR_META_RE.match(b["raw_text"].strip())]
    title_blk = max(title_cands or top, key=lambda b: (b["font_size"], -b["bbox"][1])) if top else None

    authors_raw, affiliations, author_bids = [], [], []
    if title_blk and not HEADING_PREFIX_RE.match(title_blk["raw_text"].strip()):
        t_idx = next((i for i, b in enumerate(p1) if b["block_id"] == title_blk["block_id"]), 0)
        for b in p1[t_idx + 1:]:
            txt = b["raw_text"].strip()
            if b["role"] == "heading" or re.match(r"^(?:Abstract|摘要|1\.?\s+Introduction|Introduction|一、)\b", txt, re.I):
                break
            if EDITOR_OR_META_RE.match(txt):
                continue
            has_aff = bool(re.search(r"[@]|^\d+\s+[A-Z]|University|Department|Institute|School|College|Lab|Center|Centre|Hospital|大学|学院|研究所", txt, re.I))
            if txt.endswith((".", "。")) and len(txt) > 55 and not has_aff:
                break
            if len(txt) <= 320:
                if has_aff:
                    affiliations.append(txt)
                else:
                    authors_raw.append(txt)
                    author_bids.append(b["block_id"])
    authors = _clean_author_list(authors_raw)

    abs_texts, abs_bids, kw_val, kw_bid, in_abs = [], [], None, None, False
    for b in blocks:
        if b["page"] > 2:
            break
        txt = b["raw_text"].strip()
        if re.match(r"^(?:Abstract|摘要)\s*[:：]?\s*$", txt, re.I):
            in_abs = True
            continue
        if re.match(r"^(?:Abstract|摘要)\s*[:：]\s+\S+", txt, re.I):
            abs_texts.append(txt)
            abs_bids.append(b["block_id"])
            in_abs = True
            continue
        if re.match(r"^(?:Keywords?|Index\s+Terms|关键词)\s*[:：]", txt, re.I):
            kw_val, kw_bid, in_abs = txt, b["block_id"], False
            continue
        if in_abs:
            if b["role"] == "heading" or EDITOR_OR_META_RE.match(txt):
                in_abs = False
            else:
                abs_texts.append(txt)
                abs_bids.append(b["block_id"])
                if len(abs_texts) >= 8:
                    in_abs = False

    # Scan early blocks + headers/footers + raw Page 1 text for DOI, year, journal, license
    front_texts = [b["raw_text"] for b in blocks if b["page"] <= 2] + [hf["text"] for hf in headers_footers if hf["page"] <= 2]
    if raw_p1_text:
        front_texts.insert(0, raw_p1_text)
    doi_val = "unreported"
    year_val = "unreported"
    journal_val = "unreported"
    license_val = "unreported"
    for ft in front_texts:
        if doi_val == "unreported":
            for dm in DOI_PATTERN.finditer(ft):
                cand_doi = dm.group(1).rstrip(".,;)]")
                if not re.search(r"\.(?:t|g|s)\d{3}$", cand_doi, re.I):
                    doi_val = cand_doi
                    break
        if year_val == "unreported":
            ym = re.search(r"(?:Published|Accepted|©|\bPLoS|\bJournal|\bVol\.|\(\s*)(?:[^0-9]{0,25})((?:19|20)\d{2})\b", ft, re.I)
            if ym:
                year_val = ym.group(1)
        if journal_val == "unreported":
            jm_hi = re.search(
                r"\b(PLOS\s+(?:ONE|Biol(?:ogy)?|Med(?:icine)?|Genet(?:ics)?|Pathog(?:ens)?|Comput\.?\s*Biol\.?|Negl\.?\s*Trop\.?\s*Dis\.?)|"
                r"PLoS\s+(?:ONE|Biol(?:ogy)?|Med(?:icine)?|Genet(?:ics)?|Pathog(?:ens)?)|"
                r"Nature\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|PNAS|Proceedings\s+of\s+the\s+National\s+Academy\s+of\s+Sciences|"
                r"New\s+England\s+Journal\s+of\s+Medicine|N\s+Engl\s+J\s+Med|The\s+Lancet|Lancet|"
                r"eLife|PeerJ|Scientific\s+Reports|Cell(?:\s+[A-Z][a-z]+)?|Neuron|Immunity|Genome\s+Research|"
                r"Nucleic\s+Acids\s+Research|Bioinformatics|BMC\s+[A-Z][A-Za-z]+|"
                r"Journal\s+of\s+[A-Z][A-Za-z\s&]+)\b",
                ft,
            )
            if jm_hi:
                journal_val = re.sub(r"\s+", " ", jm_hi.group(1)).strip()
        if license_val == "unreported":
            lm = re.search(r"(Creative\s+Commons\s+[A-Za-z0-9\-\s]+License|CC\s+BY[^\s.;]*)", ft, re.I)
            if lm:
                license_val = re.sub(r"\s+", " ", lm.group(1)).strip()

    if journal_val == "unreported":
        for ft in front_texts:
            if re.search(r"University|Department|Institute|Centre|Center|Hospital|Research\s+Centre", ft, re.I) and not re.search(r"Citation:", ft, re.I):
                continue
            jm_lo = re.search(r"(?<![A-Za-z\-])(Nature|Science|BMJ|JAMA)(?![A-Za-z\-])", ft)
            if jm_lo:
                journal_val = jm_lo.group(1).strip()
                break

    if year_val == "unreported" and pdf_meta and pdf_meta.get("creationDate"):
        cm = re.search(r"((?:19|20)\d{2})", str(pdf_meta["creationDate"]))
        if cm:
            year_val = cm.group(1)

    pdf_title = (pdf_meta or {}).get("title") or ""
    title_final = title_blk["raw_text"] if title_blk else (pdf_title.strip() or "unreported")
    return {
        "title": title_final,
        "title_block_id": title_blk["block_id"] if title_blk else None,
        "authors": authors or ["unreported"],
        "affiliations": affiliations or None,
        "author_block_ids": author_bids or None,
        "year": year_val,
        "journal": journal_val,
        "doi": doi_val,
        "pmid": "unreported",
        "version": "unreported",
        "license": license_val,
        "source_file": str(pdf_path) if pdf_path else None,
        "file_sha256": sha256_file(pdf_path) if pdf_path else None,
        "abstract": " ".join(abs_texts) if abs_texts else None,
        "abstract_block_ids": abs_bids or None,
        "keywords": kw_val,
        "keywords_block_id": kw_bid,
    }


def _build_merged_tables(structured_tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {t["table_id"]: t for t in structured_tables}
    merged: List[Dict[str, Any]] = []
    for t in structured_tables:
        if t.get("continues_from") or not t.get("continued_on"):
            continue
        chain, cur = [t], t
        while cur.get("continued_on") and cur["continued_on"] in by_id:
            nxt = by_id[cur["continued_on"]]
            if nxt in chain:
                break
            chain.append(nxt)
            cur = nxt
        n_hdr = int(chain[0].get("header_rows") or 1)
        rows = list(chain[0]["rows"])
        for part in chain[1:]:
            pr, ph = part["rows"], int(part.get("header_rows") or 1)
            if pr and len(rows) >= n_hdr and len(pr) >= ph and pr[:ph] == rows[:ph]:
                rows.extend(pr[ph:])
            elif pr and rows and pr[0] == rows[0]:
                rows.extend(pr[1:])
            else:
                rows.extend(pr)
        headers = chain[0].get("headers") or (rows[0] if rows else [])
        merged.append({
            "merged_table_id": f"merged_{t['table_id']}",
            "source_table_ids": [c["table_id"] for c in chain],
            "pages": [c["page"] for c in chain],
            "caption": t.get("caption"),
            "header_rows": n_hdr,
            "headers": headers,
            "num_rows": len(rows),
            "num_cols": max((len(r) for r in rows), default=0),
            "rows": rows,
            "markdown": _rows_to_markdown(rows, n_header_rows=n_hdr, headers=headers),
        })
    return merged


def probe_pdf(pdf_path: Path) -> Dict[str, Any]:
    """Inspect PDF validity, encryption, scanned/garbled pages, stat anomalies, and column layout."""
    caps = discover_capabilities()
    header = b""
    try:
        with pdf_path.open("rb") as fh:
            header = fh.read(1024)
    except Exception as exc:
        return {"file": str(pdf_path), "valid_pdf_header": False, "needs_password": False, "error": str(exc), "summary": {"overall_status": "read_error"}}
    if b"%PDF-" not in header:
        return {"file": str(pdf_path), "valid_pdf_header": False, "needs_password": False, "summary": {"overall_status": "invalid_pdf_header"}}

    if caps["python_modules"].get("pymupdf_fitz"):
        fitz = import_pymupdf()
        doc = fitz.open(str(pdf_path))
        if doc.needs_pass:
            doc.close()
            return {"file": str(pdf_path), "valid_pdf_header": True, "is_encrypted": True, "needs_password": True, "page_count": 0, "summary": {"overall_status": "encrypted_blocked"}}
        scanned, garbled, multi_col, stat_anomaly_pages = [], [], [], []
        pages_diag = []
        for idx in range(len(doc)):
            page = doc[idx]
            pno = idx + 1
            pw, ph = float(page.rect.width), float(page.rect.height)
            raw = normalize_ligatures(page.get_text("text") or "")
            defects = detect_encoding_defects(raw)
            imgs = page.get_images(full=True)
            tblocks = [{"bbox": list(b["bbox"]), "text": ""} for b in page.get_text("dict").get("blocks", []) if b.get("type") == 0 and (b["bbox"][1] > 0.08 * ph and b["bbox"][3] < 0.92 * ph)]
            est_cols = 1 + len(_detect_column_gutters(tblocks, 0.0, pw)) if len(tblocks) >= 2 else 1
            if len(raw.strip()) < 40 and len(imgs) > 0:
                scanned.append(pno)
                cls = "scanned_or_image_only"
            elif defects["defect_rate"] >= 0.08 or defects["cid_tokens"] >= 10:
                garbled.append(pno)
                cls = "garbled_encoding"
            elif est_cols >= 2:
                cls = "multi_column"
            else:
                cls = "native_text"
            if defects["stat_glyph_anomalies"]:
                stat_anomaly_pages.append(pno)
            if est_cols >= 2:
                multi_col.append(pno)
            pages_diag.append({"page": pno, "classification": cls, "char_count": len(raw.strip()), "estimated_columns": est_cols, "defects": defects})
        total = len(doc)
        doc.close()
        return {
            "file": str(pdf_path),
            "valid_pdf_header": True,
            "is_encrypted": False,
            "needs_password": False,
            "page_count": total,
            "pages": pages_diag,
            "summary": {
                "overall_status": "scanned_or_ocr_needed" if (scanned or garbled) else ("multi_column_native" if multi_col else "native_text"),
                "scanned_pages": scanned,
                "garbled_pages": garbled,
                "stat_anomaly_pages": stat_anomaly_pages,
                "multi_column_pages": multi_col,
            },
        }

    if caps["python_modules"].get("pypdf"):
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(str(pdf_path))
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    return {"file": str(pdf_path), "valid_pdf_header": True, "is_encrypted": True, "needs_password": True, "page_count": 0, "summary": {"overall_status": "encrypted_blocked"}}
            except Exception:
                return {"file": str(pdf_path), "valid_pdf_header": True, "is_encrypted": True, "needs_password": True, "page_count": 0, "summary": {"overall_status": "encrypted_blocked"}}
        scanned = [i + 1 for i, p in enumerate(reader.pages) if len((p.extract_text() or "").strip()) < 40]
        return {"file": str(pdf_path), "valid_pdf_header": True, "is_encrypted": False, "needs_password": False, "page_count": len(reader.pages), "summary": {"overall_status": "scanned_or_ocr_needed" if scanned else "native_text", "scanned_pages": scanned, "garbled_pages": [], "stat_anomaly_pages": [], "multi_column_pages": []}}

    return {"file": str(pdf_path), "valid_pdf_header": True, "is_encrypted": False, "needs_password": False, "page_count": None, "summary": {"overall_status": "probe_limited_no_pymupdf", "scanned_pages": [], "garbled_pages": [], "stat_anomaly_pages": [], "multi_column_pages": []}}


def extract_pdf(pdf_path: Path, page_spec: Optional[str] = None) -> Dict[str, Any]:
    """Extract layout-ordered blocks, sections, tables, equations, figures, PubInfo, and defects from PDF."""
    caps = discover_capabilities()
    if caps["python_modules"].get("pymupdf_fitz"):
        return _extract_with_fitz(pdf_path, page_spec, caps)
    if caps["cli_tools"].get("pdftotext"):
        return _extract_with_pdftotext_xml(pdf_path, page_spec, caps["cli_tools"]["pdftotext"])
    if caps["python_modules"].get("pypdf"):
        return _extract_with_pypdf(pdf_path, page_spec)
    return {"file": str(pdf_path), "backend": None, "status": "failed_no_pdf_backend", "error": "No PDF backend (pymupdf, pdftotext, or pypdf) available."}


def _extract_with_fitz(pdf_path: Path, page_spec: Optional[str], caps: Dict[str, Any]) -> Dict[str, Any]:
    fitz = import_pymupdf()
    doc = fitz.open(str(pdf_path))
    if doc.needs_pass:
        doc.close()
        return {"file": str(pdf_path), "backend": "pymupdf", "status": "encrypted_blocked", "error": "PDF requires a password."}

    pdf_meta = dict(doc.metadata or {})
    total_pages = len(doc)
    raw_p1_text = normalize_ligatures(doc[0].get_text("text") or "") if total_pages >= 1 else ""
    pages = parse_page_spec(page_spec, total_pages)
    plumber_pages = sorted({
        q
        for pno in pages
        if (len(doc[pno - 1].get_drawings()) >= 2 or re.search(r"\b(?:Table|Tab\.?|附表|续表)\s*[A-Za-z]?\d*|\bcont(?:inued)?\b|表\s*[A-Za-z]?\d+", doc[pno - 1].get_text("text") or "", re.I))
        for q in (pno, pno + 1)
        if q in pages
    })
    tables_by_page: Dict[int, List[Dict[str, Any]]] = {p: [] for p in pages}
    if caps["python_modules"].get("pdfplumber") and plumber_pages:
        try:
            tables_by_page.update(_extract_tables_pdfplumber(pdf_path, plumber_pages))
        except Exception:
            pass
    for pno, flist in _extract_tables_fitz(doc, pages).items():
        for ft in flist:
            ov = [i for i, et in enumerate(tables_by_page[pno]) if bbox_iou(ft["bbox"], et["bbox"]) > 0.35 or _bbox_containment(ft["bbox"], et["bbox"]) >= 0.60]
            if not ov:
                tables_by_page[pno].append(ft)
            else:
                et = tables_by_page[pno][ov[0]]
                et_flat = " ".join(" ".join(r) for r in et["rows"])
                ft_flat = " ".join(" ".join(r) for r in ft["rows"])
                et_glued = len(re.findall(r"[A-Za-z]{16,}|[a-z][A-Z]", et_flat))
                ft_glued = len(re.findall(r"[A-Za-z]{16,}|[a-z][A-Z]", ft_flat))
                if et_glued > ft_glued or (et_glued == ft_glued and len(ft["rows"]) > len(et["rows"])):
                    tables_by_page[pno][ov[0]] = ft
        tables_by_page[pno].sort(key=lambda t: (round(t["bbox"][1], 1), round(t["bbox"][0], 1)))

    raw_pages: List[Dict[str, Any]] = []
    fs_counter: Counter[float] = Counter()
    hf_pages: Dict[str, set[int]] = {}
    for pno in pages:
        page = doc[pno - 1]
        pw, ph = float(page.rect.width), float(page.rect.height)
        p_tables = tables_by_page.get(pno, [])
        page_blocks, page_images = [], []
        for blk in page.get_text("dict").get("blocks", []):
            if blk.get("type") == 1:
                ibb = [round(float(x), 2) for x in blk.get("bbox", [0, 0, 0, 0])]
                if (ibb[2] - ibb[0]) >= 24.0 and (ibb[3] - ibb[1]) >= 24.0:
                    page_images.append({"bbox": ibb, "pixel_width": int(blk.get("width") or 0), "pixel_height": int(blk.get("height") or 0), "ext": blk.get("ext")})
                continue
            if blk.get("type") != 0:
                continue
            blk_lines = blk.get("lines", [])
            bb_raw = [round(float(x), 2) for x in blk["bbox"]]
            line_groups = _split_wide_multicolumn_lines(blk_lines, pw) if (bb_raw[2] - bb_raw[0]) >= 0.36 * pw else [blk_lines]
            for grp in line_groups:
                gboxes = [ln.get("bbox") for ln in grp if ln.get("bbox")]
                bb = merge_bbox(gboxes) if gboxes else bb_raw
                in_tbl = any(bbox_contains_center(t["bbox"], bb, margin=2.0) for t in p_tables)
                merged_lines: List[Dict[str, Any]] = []
                for ln in grp:
                    lbb, lspans = ln.get("bbox"), list(ln.get("spans", []))
                    if not lbb or not lspans:
                        continue
                    if merged_lines:
                        pbb = merged_lines[-1]["bbox"]
                        y_ov = min(pbb[3], lbb[3]) - max(pbb[1], lbb[1])
                        dx = lbb[0] - pbb[2]
                        lfs = max(float(lspans[0].get("size", 10.0)), 8.0)
                        max_same_line_dx = min(22.0, max(12.0, 1.8 * lfs))
                        if y_ov >= 0.40 * min(max(1.0, pbb[3] - pbb[1]), max(1.0, lbb[3] - lbb[1])) and -2.0 <= dx <= max_same_line_dx:
                            ns = list(merged_lines[-1]["spans"])
                            if dx >= 3.5:
                                ns.append({"text": " ", "size": ns[-1].get("size", 10.0), "flags": 0, "bbox": [pbb[2], pbb[1], lbb[0], pbb[3]], "font": ""})
                            ns.extend(lspans)
                            merged_lines[-1] = {"bbox": merge_bbox([pbb, list(lbb)]), "spans": ns}
                            continue
                    merged_lines.append({"bbox": list(lbb), "spans": lspans})
                lines_info, bsizes, bold_ch, tot_ch = [], [], 0, 0
                for ln in merged_lines:
                    raw_ln, fmt_ln, has_unr = _span_text_with_scripts(ln.get("spans", []))
                    if not raw_ln.strip():
                        continue
                    for sp in ln.get("spans", []):
                        st = sp.get("text", "").strip()
                        if not st:
                            continue
                        fs = round(float(sp.get("size", 10.0)), 1)
                        bsizes.extend([fs] * len(st))
                        if not in_tbl:
                            fs_counter[fs] += len(st)
                        tot_ch += len(st)
                        fname = str(sp.get("font", "")).lower()
                        if (int(sp.get("flags", 0)) & 16) or "bold" in fname or "black" in fname:
                            bold_ch += len(st)
                    lines_info.append({"raw_text": raw_ln, "fmt_text": fmt_ln, "has_unreadable": has_unr})
                if not lines_info:
                    continue
                raw_text = _join_block_lines(lines_info, False)
                fmt_text = _join_block_lines(lines_info, True)
                if not raw_text:
                    continue
                if (bb[1] < 0.09 * ph or bb[3] > 0.91 * ph) and len(raw_text) <= 140 and not in_tbl:
                    hfk = normalize_header_footer_key(raw_text)
                    if hfk:
                        hf_pages.setdefault(hfk, set()).add(pno)
                page_blocks.append({
                    "page": pno, "bbox": bb, "raw_text": raw_text, "text": fmt_text,
                    "font_size": round(statistics.median(bsizes) if bsizes else 10.0, 2),
                    "is_bold": (bold_ch / max(1, tot_ch)) >= 0.6, "in_table": in_tbl,
                    "defects": detect_encoding_defects(raw_text),
                })
        raw_pages.append({"page": pno, "width": round(pw, 1), "height": round(ph, 1), "blocks": page_blocks, "images": page_images})
    doc.close()

    body_fs = fs_counter.most_common(1)[0][0] if fs_counter else 10.0
    repeating_hf = {k for k, pset in hf_pages.items() if len(pset) >= 2}
    all_blocks, headers_footers, captions, footnotes, equations, sections, references, unextracted = [], [], [], [], [], [], [], []
    in_refs = False
    sec_c = eq_c = cap_c = fn_c = ref_c = 0

    for rp in raw_pages:
        pno, pw, ph = rp["page"], rp["width"], rp["height"]
        body_cands = []
        for b in rp["blocks"]:
            if b["in_table"]:
                continue
            bb = b["bbox"]
            if (bb[1] < 0.09 * ph or bb[3] > 0.91 * ph) and len(b["raw_text"]) <= 140 and normalize_header_footer_key(b["raw_text"]) in repeating_hf:
                headers_footers.append({"page": pno, "bbox": bb, "position": "header" if bb[1] < 0.5 * ph else "footer", "text": b["raw_text"]})
                continue
            body_cands.append(b)
        ordered = _merge_split_equation_labels(_coalesce_same_line_fragments(_xy_cut_recursive(body_cands, pw)))
        for idx, b in enumerate(ordered):
            bid = f"p{pno}_b{idx + 1}"
            txt, ftxt, bb, fs = b["raw_text"], b["text"], b["bbox"], b["font_size"]
            eq_m = EQ_LABEL_RE.search(txt)
            math_cnt = sum(1 for ch in txt if ch in MATH_SYMBOLS)
            eq_lbl = (eq_m.group(1) or eq_m.group(2)) if eq_m else None
            is_yr = bool(eq_lbl and re.fullmatch(r"(?:19|20)\d{2}", eq_lbl.strip()))
            has_cit = bool(re.search(r"\b(?:Ref\.|Refs\.|et\s+al\.|Fig\.|Figs\.|Table|Vol\.|pp\.)", txt, re.I))
            has_op = math_cnt >= 1 or ftxt != txt or (not re.search(r"https?://", txt, re.I) and bool(re.search(r"[=<>^_]|[a-zA-Z0-9\)\]]\s*[+\-*/=<>]\s*[a-zA-Z0-9\(\[]", txt)))
            is_eq = (
                (eq_m is not None and not is_yr and not has_cit and (not in_refs or math_cnt >= 4) and ((has_op and len(txt) <= 240) or (len(txt) <= 120 and len(txt.split()) <= 8)))
                or (math_cnt >= 4 and len(txt) <= 220 and fs <= 1.15 * body_fs and (not in_refs or math_cnt >= 6))
            )
            role = "paragraph"
            if _is_caption_text(txt):
                role = "caption"
                cap_c += 1
                ck = "table_caption" if re.match(r"^(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Table|Tab\.?|附表|表)", txt, re.I) else "figure_caption"
                captions.append({"caption_id": f"cap_{cap_c}", "kind": ck, "page": pno, "block_id": bid, "bbox": bb, "text": ftxt})
            elif bb[1] > 0.78 * ph and fs <= 0.88 * body_fs and FOOTNOTE_RE.match(txt):
                role = "footnote"
                fn_c += 1
                footnotes.append({"footnote_id": f"fn_{fn_c}", "page": pno, "block_id": bid, "bbox": bb, "text": ftxt})
            elif is_eq and not (HEADING_PREFIX_RE.match(txt) and "=" not in txt and not eq_lbl):
                role = "equation"
                eq_c += 1
                equations.append({"equation_id": f"eq_{eq_c}", "label": eq_lbl, "page": pno, "block_id": bid, "bbox": bb, "raw_text": txt, "script_text": ftxt, "has_unreadable": b["defects"]["has_defects"]})
            elif "=" not in txt and len(txt) <= 180 and "\n" not in txt and not (pno == 1 and idx >= 1 and idx <= 4 and not b["is_bold"] and not HEADING_RE.match(txt) and not HEADING_PREFIX_RE.match(txt)) and (
                (fs >= 1.10 * body_fs and (b["is_bold"] or HEADING_PREFIX_RE.match(txt) or (idx == 0 and fs >= 1.30 * body_fs)))
                or (b["is_bold"] and HEADING_RE.match(txt))
                or (HEADING_PREFIX_RE.match(txt) and fs >= 0.98 * body_fs and len(txt.split()) <= 16)
            ):
                role = "heading"
                sec_c += 1
                lvl = 3 if re.match(r"^\d+\.\d+\.\d+", txt) else (2 if re.match(r"^\d+\.\d+", txt) or (fs < 1.12 * body_fs and not re.match(r"^\d+\.?\s+", txt)) else 1)
                if REF_SECTION_RE.match(txt):
                    in_refs = True
                elif re.match(r"^(?:\d+\.?\s*)?(?:Appendix|Acknowledgements?|Supplementary|附录|致谢|补充材料)\b", txt, re.I):
                    in_refs = False
                b["heading_level"] = lvl
                sections.append({"section_id": f"sec_{sec_c}", "title": txt, "level": lvl, "page_start": pno, "block_id": bid, "bbox": bb})
            elif in_refs:
                role = "reference"
                ref_c += 1
                references.append({"ref_id": f"ref_{ref_c}", "page": pno, "block_id": bid, "bbox": bb, "text": ftxt})

            if b["defects"]["has_defects"]:
                reason = "stat_glyph_anomaly" if b["defects"].get("stat_glyph_anomalies") else "encoding_defects_detected"
                unextracted.append({"page": pno, "block_id": bid, "bbox": bb, "reason": reason, "defects": b["defects"]})
            all_blocks.append({
                "block_id": bid, "page": pno, "column": b.get("column", 1), "role": role,
                "heading_level": b.get("heading_level"), "bbox": bb, "font_size": fs,
                "is_bold": b["is_bold"], "raw_text": txt, "text": ftxt, "defects": b["defects"],
            })

    structured_tables: List[Dict[str, Any]] = []
    tbl_c = 0
    for pno in pages:
        pcaps = [c for c in captions if c["page"] == pno and c["kind"] == "table_caption"]
        for t in tables_by_page.get(pno, []):
            tbl_c += 1
            tb = t["bbox"]
            matched = min((c for c in pcaps if min(abs(tb[1] - c["bbox"][3]), abs(c["bbox"][1] - tb[3])) < 120.0), key=lambda c: min(abs(tb[1] - c["bbox"][3]), abs(c["bbox"][1] - tb[3])), default=None)
            ts = _structure_table_rows(t["rows"], t.get("header_rows_hint"))
            if not ts["rows"]:
                tbl_c -= 1
                continue
            ncols = max((len(r) for r in ts["rows"]), default=0)
            entry = {
                "table_id": f"tbl_{tbl_c}", "page": pno, "bbox": tb, "method": t["method"],
                "header_rows": ts["header_rows"], "headers": ts["headers"], "num_rows": len(ts["rows"]),
                "num_cols": ncols, "caption": (matched["text"] if matched else None) or ts.get("embedded_caption"),
                "caption_block_id": matched["block_id"] if matched else None, "note": ts.get("note"),
                "rows": ts["rows"], "markdown": _rows_to_markdown(ts["rows"], ts["header_rows"], ts["headers"]),
                "continues_from": None, "continued_on": None,
            }
            if structured_tables:
                prev = structured_tables[-1]
                cap_l = (entry["caption"] or "").lower()
                is_cont = any(k in cap_l for k in ("continued", "cont.", "cont'd", "续表", "(续)", "（续）"))
                if is_cont or (prev["page"] == pno - 1 and prev["num_cols"] == ncols and tb[1] < 240.0 and (not entry["caption"] or prev["rows"][0] == entry["rows"][0])):
                    entry["continues_from"] = prev["table_id"]
                    prev["continued_on"] = entry["table_id"]
            structured_tables.append(entry)

    figures: List[Dict[str, Any]] = []
    fig_c = 0
    for rp in raw_pages:
        pno = rp["page"]
        fcaps = [c for c in captions if c["page"] == pno and c["kind"] == "figure_caption"]
        ptbls = [t for t in structured_tables if t["page"] == pno]
        for img in rp["images"]:
            ib = img["bbox"]
            if any(bbox_iou(ib, t["bbox"]) > 0.25 for t in ptbls):
                continue
            near = min((c for c in fcaps if max(0.0, max(ib[1] - c["bbox"][3], c["bbox"][1] - ib[3])) <= 180.0), key=lambda c: max(0.0, max(ib[1] - c["bbox"][3], c["bbox"][1] - ib[3])), default=None)
            if (ib[2] - ib[0]) >= 0.85 * rp["width"] and (ib[3] - ib[1]) >= 0.85 * rp["height"] and not near:
                continue
            fig_c += 1
            figures.append({
                "figure_id": f"fig_{fig_c}", "page": pno, "bbox": ib,
                "pixel_size": [img["pixel_width"], img["pixel_height"]],
                "caption": near["text"] if near else None, "caption_block_id": near["block_id"] if near else None,
            })

    merged_tables = _build_merged_tables(structured_tables)
    metadata = _extract_metadata(all_blocks, headers_footers, pdf_meta=pdf_meta, pdf_path=pdf_path, raw_p1_text=raw_p1_text)
    page_label_map, page_disc = _extract_printed_page_map(headers_footers)
    return {
        "file": str(pdf_path), "format": "pdf", "backend": "pymupdf", "status": "ok",
        "page_count": total_pages, "extracted_pages": pages, "body_font_size": body_fs,
        "metadata": metadata, "page_label_map": page_label_map, "page_numbering_discrepancy": page_disc,
        "sections": sections, "tables": structured_tables, "merged_tables": merged_tables,
        "figures": figures, "captions": captions, "footnotes": footnotes, "equations": equations,
        "references": references, "headers_footers": headers_footers, "unextracted_regions": unextracted,
        "blocks": all_blocks,
    }


def _extract_with_pdftotext_xml(pdf_path: Path, page_spec: Optional[str], bin_path: str) -> Dict[str, Any]:
    proc = subprocess.run([bin_path, "-bbox-layout", str(pdf_path), "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"file": str(pdf_path), "format": "pdf", "backend": "pdftotext", "status": "failed", "error": proc.stderr}
    root = ET.fromstring(proc.stdout)
    ns = root.tag.split("}", 1)[0] + "}" if root.tag.startswith("{") else ""
    pages_el = root.findall(f".//{ns}page")
    wanted = set(parse_page_spec(page_spec, len(pages_el)))
    all_blocks, sections, unextracted = [], [], []
    sec_c = 0
    for idx, p_el in enumerate(pages_el):
        pno = idx + 1
        if pno not in wanted:
            continue
        pw = float(p_el.attrib.get("width", 612))
        p_blocks = []
        for b_el in p_el.findall(f".//{ns}block"):
            bb = [round(float(b_el.attrib.get(k, 0)), 2) for k in ("xMin", "yMin", "xMax", "yMax")]
            lines = [{"raw_text": normalize_ligatures(" ".join(w.text or "" for w in l_el.findall(f"{ns}word"))), "fmt_text": ""} for l_el in b_el.findall(f".//{ns}line")]
            txt = _join_block_lines(lines, False)
            if txt:
                p_blocks.append({"page": pno, "bbox": bb, "raw_text": txt, "text": txt, "font_size": 10.0, "is_bold": False, "defects": detect_encoding_defects(txt)})
        for b_idx, b in enumerate(_xy_cut_recursive(p_blocks, pw)):
            bid = f"p{pno}_b{b_idx + 1}"
            b["block_id"] = bid
            if HEADING_RE.match(b["raw_text"]) and len(b["raw_text"]) <= 120:
                b["role"] = "heading"
                sec_c += 1
                sections.append({"section_id": f"sec_{sec_c}", "title": b["raw_text"], "level": 1, "page_start": pno, "block_id": bid, "bbox": b["bbox"]})
            else:
                b["role"] = "caption" if _is_caption_text(b["raw_text"]) else "paragraph"
            if b["defects"]["has_defects"]:
                unextracted.append({"page": pno, "block_id": bid, "bbox": b["bbox"], "reason": "stat_glyph_anomaly" if b["defects"].get("stat_glyph_anomalies") else "encoding_defects_detected", "defects": b["defects"]})
            all_blocks.append(b)
    return {
        "file": str(pdf_path), "format": "pdf", "backend": "pdftotext_bbox_layout", "status": "ok",
        "page_count": len(pages_el), "extracted_pages": sorted(wanted),
        "metadata": _extract_metadata(all_blocks, [], pdf_path=pdf_path),
        "page_label_map": {}, "page_numbering_discrepancy": False, "sections": sections,
        "tables": [], "merged_tables": [], "figures": [], "captions": [], "footnotes": [],
        "equations": [], "references": [], "headers_footers": [], "unextracted_regions": unextracted, "blocks": all_blocks,
    }


def _extract_with_pypdf(pdf_path: Path, page_spec: Optional[str]) -> Dict[str, Any]:
    import pypdf  # type: ignore

    reader = pypdf.PdfReader(str(pdf_path))
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                return {"file": str(pdf_path), "format": "pdf", "backend": "pypdf", "status": "encrypted_blocked", "error": "PDF requires a password."}
        except Exception:
            return {"file": str(pdf_path), "format": "pdf", "backend": "pypdf", "status": "encrypted_blocked", "error": "PDF requires a password."}
    total = len(reader.pages)
    pages = parse_page_spec(page_spec, total)
    all_blocks, sections, unextracted = [], [], []
    sec_c = 0
    for pno in pages:
        raw_txt = normalize_ligatures(reader.pages[pno - 1].extract_text() or "")
        paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n+", raw_txt) if p.strip()] or ([re.sub(r"\s+", " ", raw_txt).strip()] if raw_txt.strip() else [])
        for b_idx, para in enumerate(paras):
            bid = f"p{pno}_b{b_idx + 1}"
            defects = detect_encoding_defects(para)
            role = "caption" if _is_caption_text(para) else ("heading" if HEADING_RE.match(para) else "paragraph")
            if role == "heading":
                sec_c += 1
                sections.append({"section_id": f"sec_{sec_c}", "title": para, "level": 1, "page_start": pno, "block_id": bid, "bbox": [0.0, 0.0, 0.0, 0.0]})
            all_blocks.append({"block_id": bid, "page": pno, "column": 1, "role": role, "bbox": [0.0, 0.0, 0.0, 0.0], "font_size": 10.0, "is_bold": False, "raw_text": para, "text": para, "defects": defects})
            if defects["has_defects"]:
                unextracted.append({"page": pno, "block_id": bid, "bbox": [0.0, 0.0, 0.0, 0.0], "reason": "stat_glyph_anomaly" if defects.get("stat_glyph_anomalies") else "encoding_defects_detected", "defects": defects})
    return {
        "file": str(pdf_path), "format": "pdf", "backend": "pypdf", "status": "ok",
        "page_count": total, "extracted_pages": pages,
        "metadata": _extract_metadata(all_blocks, [], pdf_path=pdf_path),
        "page_label_map": {}, "page_numbering_discrepancy": False, "sections": sections,
        "tables": [], "merged_tables": [], "figures": [], "captions": [], "footnotes": [],
        "equations": [], "references": [], "headers_footers": [], "unextracted_regions": unextracted, "blocks": all_blocks,
    }
