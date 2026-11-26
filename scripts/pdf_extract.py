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
    r"^(?:Figure|Fig\.?|Table|Tab\.?|Scheme|Algorithm|Listing|图|表)\s*\d+[A-Za-z0-9.\-]*(?:[:.\s]|$)",
    re.I,
)
HEADING_RE = re.compile(
    r"^(?:"
    r"Abstract|Introduction|Background|Related\s+Work|Methods?|Methodology|Materials?\s+and\s+Methods?|"
    r"Experimental(?:\s+Setup)?|Results?(?:\s+and\s+Discussion)?|Discussion|Conclusions?|"
    r"Acknowledgements?|References|Bibliography|Appendix(?:\s+[A-Z0-9]+)?|"
    r"摘要|引言|前言|相关工作|方法|实验|结果与讨论|结论|致谢|参考文献|附录|"
    r"(?:\d+(?:\.\d+){0,3}\.?|[IVX]{1,5}\.|[A-Z]\.)\s+\S+"
    r")$",
    re.I,
)
HEADING_PREFIX_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+){0,3}\.?|[IVX]{1,5}\.|[A-Z]\.)\s+\S+|"
    r"(?:Abstract|Introduction|Background|Related\s+Work|Methods?|Methodology|Materials?\s+and\s+Methods?|"
    r"Experimental\s+Setup|Results?(?:\s+and\s+Discussion)?|Discussion|Conclusions?|Acknowledgements?|References|Bibliography|Appendix)\b)",
    re.I,
)
REF_SECTION_RE = re.compile(r"^(?:\d+\.?\s*)?(?:References|Bibliography|参考文献)\b", re.I)
REF_ITEM_RE = re.compile(r"^(?:\[\d+\]|\(\d+\)|\d+\.\s+[A-Z\u4e00-\u9fff])")
FOOTNOTE_RE = re.compile(r"^(?:\*+|†|‡|§|\d{1,2})\s*\S+")
EQ_LABEL_RE = re.compile(r"(?:\((\d+(?:\.\d+)?[a-z]?)\)|\[(\d+(?:\.\d+)?[a-z]?)\])$")
MATH_SYMBOLS = set("=≈≠≤≥<>±×÷·∑∏∫∂∇√∞∈∉⊂⊆∪∩∀∃αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ→←↔⇒⇔")


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
    fmt_parts: List[str] = []
    has_unreadable = False

    for s in spans:
        txt = normalize_ligatures(s.get("text", ""))
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

        if stripped and is_super and len(stripped) <= 12:
            fmt_parts.append(f"^{{{stripped}}}")
        elif stripped and is_sub and len(stripped) <= 12:
            fmt_parts.append(f"_{{{stripped}}}")
        else:
            fmt_parts.append(txt)

    return "".join(raw_parts), "".join(fmt_parts), has_unreadable


def _join_block_lines(lines_info: List[Dict[str, Any]], use_formatted: bool = False) -> str:
    """Join lines within a block, carefully healing end-of-line hyphenation."""
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
        else:
            out = out + " " + seg
    return out.strip()


def _detect_column_gutters(blocks: List[Dict[str, Any]], content_x0: float, content_x1: float) -> List[float]:
    """Detect vertical column gutters inside a horizontal band of blocks."""
    width = content_x1 - content_x0
    if len(blocks) < 2 or width <= 50.0:
        return []
    non_spanning = [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) < 0.58 * width]
    if len(non_spanning) < 2:
        return []

    bins = 100
    occ = [0] * bins
    for b in non_spanning:
        b0 = max(0, min(bins - 1, int(((b["bbox"][0] - content_x0) / width) * bins)))
        b1 = max(0, min(bins - 1, int(((b["bbox"][2] - content_x0) / width) * bins)))
        for idx in range(b0, b1 + 1):
            occ[idx] += 1

    gutters: List[float] = []
    run_start: Optional[int] = None
    for i in range(15, 86):
        if occ[i] == 0:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and (i - run_start) >= 2:
                has_left = any(occ[j] > 0 for j in range(0, run_start))
                has_right = any(occ[j] > 0 for j in range(i, bins))
                if has_left and has_right:
                    gutters.append(content_x0 + ((run_start + i) / 2.0) / bins * width)
            run_start = None
    return gutters[:2]


def _order_band_blocks(blocks: List[Dict[str, Any]], content_x0: float, content_x1: float) -> List[Dict[str, Any]]:
    """Order blocks inside a horizontal band by column (left-to-right), then top-to-bottom."""
    if not blocks:
        return []
    gutters = _detect_column_gutters(blocks, content_x0, content_x1)
    if not gutters:
        for b in blocks:
            b["column"] = 1
        return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))

    cols: List[List[Dict[str, Any]]] = [[] for _ in range(len(gutters) + 1)]
    for b in blocks:
        cx = 0.5 * (b["bbox"][0] + b["bbox"][2])
        col_idx = 0
        for g in gutters:
            if cx > g:
                col_idx += 1
        b["column"] = col_idx + 1
        cols[col_idx].append(b)

    ordered: List[Dict[str, Any]] = []
    for col_blocks in cols:
        col_blocks.sort(key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))
        ordered.extend(col_blocks)
    return ordered


def _xy_cut_order(blocks: List[Dict[str, Any]], page_w: float) -> List[Dict[str, Any]]:
    """Order page blocks using spanning-block band splitting + multi-column X-Y cut."""
    if not blocks:
        return []
    content_x0 = min(b["bbox"][0] for b in blocks)
    content_x1 = max(b["bbox"][2] for b in blocks)
    content_w = max(page_w * 0.6, content_x1 - content_x0)
    mid_x = 0.5 * (content_x0 + content_x1)

    spanning: List[Dict[str, Any]] = []
    regular: List[Dict[str, Any]] = []
    for b in blocks:
        bw = b["bbox"][2] - b["bbox"][0]
        crosses_mid = b["bbox"][0] < mid_x - 0.05 * content_w and b["bbox"][2] > mid_x + 0.05 * content_w
        if bw >= 0.58 * content_w and crosses_mid:
            b["column"] = 0  # spanning
            spanning.append(b)
        else:
            regular.append(b)

    spanning.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
    if not spanning:
        return _order_band_blocks(regular, content_x0, content_x1)

    ordered: List[Dict[str, Any]] = []
    prev_y = -1e9
    for sp in spanning:
        sp_cy = 0.5 * (sp["bbox"][1] + sp["bbox"][3])
        band = [b for b in regular if prev_y <= 0.5 * (b["bbox"][1] + b["bbox"][3]) < sp_cy]
        ordered.extend(_order_band_blocks(band, content_x0, content_x1))
        ordered.append(sp)
        prev_y = sp_cy
    tail = [b for b in regular if 0.5 * (b["bbox"][1] + b["bbox"][3]) >= prev_y]
    ordered.extend(_order_band_blocks(tail, content_x0, content_x1))
    return ordered


def _rows_to_markdown(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    norm_rows = [
        [re.sub(r"\s+", " ", (c or "").replace("|", "\\|")).strip() for c in (r + [""] * (ncols - len(r)))]
        for r in rows
    ]
    header = "| " + " | ".join(norm_rows[0]) + " |"
    sep = "| " + " | ".join(["---"] * ncols) + " |"
    body = ["| " + " | ".join(r) + " |" for r in norm_rows[1:]]
    return "\n".join([header, sep, *body])


def _rows_to_html(rows: List[List[str]], n_header_rows: int = 1) -> str:
    if not rows:
        return "<table></table>"
    ncols = max(len(r) for r in rows)
    norm_rows = [
        [re.sub(r"\s+", " ", (c or "")).strip() for c in (r + [""] * (ncols - len(r)))]
        for r in rows
    ]

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
                clean_rows = [[normalize_ligatures(c or "").strip() for c in row] for row in data]
                if len(clean_rows) >= 2 and any(any(cell for cell in r) for r in clean_rows):
                    candidates.append(("pdfplumber_lines", [round(x, 2) for x in tbl.bbox], clean_rows))

            # Strategy 2: horizontal-rule bounded three-line tables (lines or thin filled rects)
            h_rules: List[Dict[str, float]] = []
            for ln in (page.lines or []):
                if abs(ln["y0"] - ln["y1"]) < 2.0 and (ln["x1"] - ln["x0"]) > 0.25 * page.width:
                    h_rules.append({
                        "x0": float(ln["x0"]),
                        "x1": float(ln["x1"]),
                        "top": float(ln["top"]),
                        "bottom": float(ln["bottom"]),
                    })
            for rc in (page.rects or []):
                if abs(rc["bottom"] - rc["top"]) <= 3.0 and (rc["x1"] - rc["x0"]) > 0.25 * page.width:
                    h_rules.append({
                        "x0": float(rc["x0"]),
                        "x1": float(rc["x1"]),
                        "top": float(rc["top"]),
                        "bottom": float(rc["bottom"]),
                    })
            h_rules.sort(key=lambda ln: ln["top"])
            if len(h_rules) >= 2:
                groups: List[List[Dict[str, float]]] = [[h_rules[0]]]
                for ln in h_rules[1:]:
                    if ln["top"] - groups[-1][-1]["top"] <= 260.0:
                        groups[-1].append(ln)
                    else:
                        groups.append([ln])
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
                    if any(bbox_iou(gbox, c_bbox) > 0.25 for _, c_bbox, _ in candidates):
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
                        clean_rows = [[normalize_ligatures(c or "").strip() for c in row] for row in data]
                        non_empty_rows = [r for r in clean_rows if any(c for c in r)]
                        if len(non_empty_rows) >= 2 and len(non_empty_rows[0]) >= 2:
                            candidates.append((
                                "pdfplumber_threeline",
                                gbox,
                                non_empty_rows,
                            ))

            # Deduplicate overlapping tables
            kept: List[Dict[str, Any]] = []
            for method, bbox, rows in candidates:
                if any(bbox_iou(bbox, k["bbox"]) > 0.5 for k in kept):
                    continue
                kept.append({"method": method, "bbox": bbox, "rows": rows})
            by_page[pno] = kept
    return by_page


def _extract_tables_fitz(doc: Any, pages: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Extract tables via PyMuPDF find_tables when available."""
    by_page: Dict[int, List[Dict[str, Any]]] = {p: [] for p in pages}
    for pno in pages:
        page = doc[pno - 1]
        if not hasattr(page, "find_tables"):
            continue
        try:
            finder = page.find_tables()
            for tbl in getattr(finder, "tables", []):
                data = tbl.extract() or []
                clean_rows = [[normalize_ligatures(c or "").strip() for c in row] for row in data]
                clean_rows = [r for r in clean_rows if any(c for c in r)]
                if len(clean_rows) >= 2:
                    by_page[pno].append({
                        "method": "pymupdf_find_tables",
                        "bbox": [round(float(x), 2) for x in tbl.bbox],
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


def _extract_with_fitz(pdf_path: Path, page_spec: Optional[str], caps: Dict[str, Any]) -> Dict[str, Any]:
    fitz = import_pymupdf()

    doc = fitz.open(str(pdf_path))
    if doc.needs_pass:
        return {"file": str(pdf_path), "status": "encrypted_blocked", "error": "PDF requires a password."}

    total_pages = len(doc)
    pages = parse_page_spec(page_spec, total_pages)

    # Table extraction (merge pdfplumber + fitz, keeping richer row/cell candidate on overlap)
    tables_by_page = {p: [] for p in pages}
    if caps["python_modules"].get("pdfplumber"):
        try:
            tables_by_page = _extract_tables_pdfplumber(pdf_path, pages)
        except Exception:
            pass
    fitz_tables = _extract_tables_fitz(doc, pages)
    for pno in pages:
        for ft in fitz_tables.get(pno, []):
            overlaps = [i for i, et in enumerate(tables_by_page[pno]) if bbox_iou(ft["bbox"], et["bbox"]) > 0.45]
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
        p_tables = tables_by_page.get(pno, [])

        for blk in tdict.get("blocks", []):
            if blk.get("type") != 0:
                continue
            bb = [round(float(x), 2) for x in blk["bbox"]]
            # Skip blocks completely inside extracted table boxes to avoid duplicating table text in body
            in_table = any(bbox_contains_center(t["bbox"], bb, margin=2.0) for t in p_tables)

            lines_info: List[Dict[str, Any]] = []
            block_sizes: List[float] = []
            bold_chars = 0
            total_chars = 0
            for ln in blk.get("lines", []):
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

        ordered = _xy_cut_order(body_candidates, pw)
        for idx, b in enumerate(ordered):
            bid = f"p{pno}_b{idx + 1}"
            b["block_id"] = bid
            txt = b["raw_text"]
            ftxt = b["text"]
            bb = b["bbox"]
            fs = b["font_size"]

            # Role classification
            role = "paragraph"
            if CAPTION_RE.match(txt):
                role = "caption"
                cap_counter += 1
                cap_kind = "table_caption" if re.match(r"^(?:Table|Tab\.?|表)", txt, re.I) else "figure_caption"
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
            elif (
                len(txt) <= 180
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
            else:
                eq_m = EQ_LABEL_RE.search(txt)
                math_cnt = sum(1 for ch in txt if ch in MATH_SYMBOLS)
                if (eq_m and len(txt) <= 240) or (math_cnt >= 4 and len(txt) <= 220 and fs <= 1.15 * body_fs):
                    role = "equation"
                    eq_counter += 1
                    equations.append({
                        "equation_id": f"eq_{eq_counter}",
                        "label": (eq_m.group(1) or eq_m.group(2)) if eq_m else None,
                        "page": pno,
                        "block_id": bid,
                        "bbox": bb,
                        "raw_text": txt,
                        "script_text": ftxt,
                        "has_unreadable": b["defects"]["has_defects"],
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
                is_cont_caption = "continued" in cap_txt or "cont." in cap_txt or "续表" in cap_txt
                same_cols_adjacent = prev["page"] == pno - 1 and prev["num_cols"] == ncols and tb[1] < 220.0
                if is_cont_caption or (same_cols_adjacent and (not entry["caption"] or prev["rows"][0] == rows[0])):
                    entry["continues_from"] = prev["table_id"]
                    prev["continued_on"] = tid

            structured_tables.append(entry)

    return {
        "file": str(pdf_path),
        "backend": "pymupdf",
        "status": "ok",
        "page_count": total_pages,
        "extracted_pages": pages,
        "body_font_size": body_fs,
        "counts": {
            "blocks": len(all_blocks),
            "sections": len(sections),
            "tables": len(structured_tables),
            "captions": len(captions),
            "footnotes": len(footnotes),
            "equations": len(equations),
            "references": len(references),
            "headers_footers": len(headers_footers),
            "unextracted_regions": len(unextracted_regions),
        },
        "sections": sections,
        "tables": structured_tables,
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
        tag = f"[{b['block_id']}|col{b.get('column', 1)}|{b['role']}|bbox={b['bbox']}]"
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

    return "\n".join(md_lines), "\n".join(tbl_lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract layout-ordered text, sections, tables, equations, and anchors from a PDF.")
    parser.add_argument("pdf_path", help="Path to the input PDF file.")
    parser.add_argument("--out-dir", required=True, help="Directory to write document.json, reading_order.md, tables.json, tables.md.")
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
