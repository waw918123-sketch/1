#!/usr/bin/env python3
"""
Scientific PDF Parser for Harbor Challenge: 阅读01｜文献文档解析
(product-arena-08-3027d350)

Deterministic, evidence-traceable PDF parser featuring:
- Multi-backend fallback (pdfplumber -> pymupdf/fitz -> pypdf -> pdftotext)
- Gutter-aware XY-cut multi-column reading order reconstruction
- Cross-page running header/footer statistical detection & separation
- Section hierarchy, footnote, caption, equation, and reference structuring
- Character-geometry superscript/subscript & Greek/math symbol preservation
- Complex table extraction & cross-page continuation table stitching
- Garbled-font (CID/replacement char) & scanned-page detection with adaptive OCR
- Per-block, per-table, per-equation provenance (pdf_page, printed_page, bbox)
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Optional library imports with graceful fallback
try:
    import pdfplumber  # type: ignore
except Exception:
    pdfplumber = None

try:
    import fitz  # type: ignore
except Exception:
    fitz = None

try:
    import pypdf  # type: ignore
except Exception:
    pypdf = None

try:
    import pypdfium2 as pdfium  # type: ignore
except Exception:
    pdfium = None

try:
    from PIL import Image, ImageFilter, ImageOps  # type: ignore
except Exception:
    Image = None
    ImageFilter = None
    ImageOps = None

try:
    import pytesseract  # type: ignore
except Exception:
    pytesseract = None


# ----------------------------- Regex Patterns -----------------------------

CID_RE = re.compile(r"\(cid:\d+\)")
EQUATION_NUM_RE = re.compile(r"(?:^|\s)[\(\[（](\d+(?:\.\d+)*[a-zA-Z]?)[\)\]）]\s*$")
FIGURE_CAPTION_RE = re.compile(
    r"^(?:Fig(?:ure)?\.?|图)\s*(\d+(?:\.\d+)*|[IVX]+)\s*[:.：\-—\s]",
    re.IGNORECASE,
)
TABLE_CAPTION_RE = re.compile(
    r"^(?:Table|Tab\.?|表|续表)\s*(\d+(?:\.\d+)*|[IVX]+)?\s*(?:\(continued\)|（续）|续)?\s*[:.：\-—\s]?",
    re.IGNORECASE,
)
TABLE_CONT_RE = re.compile(
    r"(?:table|tab\.?|表)\s*(\d+(?:\.\d+)*)?\s*(?:\(cont(?:inued|\.)?\)|（续）|续)|^续表",
    re.IGNORECASE,
)
SECTION_NUM_RE = re.compile(
    r"^(?:(\d+(?:\.\d+){0,3})|([IVX]+\.)|(第[一二三四五六七八九十\d]+[章节部分]))\s+(.{1,140})$"
)
STANDARD_HEADING_RE = re.compile(
    r"^(?:abstract|introduction|background|related work|methods?|methodology|"
    r"materials?\s+and\s+methods?|experimental(?:\s+section)?|results?|"
    r"results?\s+and\s+discussion|discussion|conclusions?|acknowledg(?:e)?ments?|"
    r"references|bibliography|appendix|supplementary(?:\s+information)?|"
    r"摘要|引言|绪论|实验部分|材料与方法|结果与讨论|结论|致谢|参考文献|附录)$",
    re.IGNORECASE,
)
FOOTNOTE_START_RE = re.compile(r"^(?:[\*†‡§¶]|\d{1,2})\s+\S+")
PAGE_NUM_RE = re.compile(
    r"^(?:(?:page|p\.?|第)\s*)?(\d{1,4}|[ivxlcdm]{1,6})(?:\s*(?:/\s*\d+|页))?$",
    re.IGNORECASE,
)
GREEK_AND_MATH_CHARS = set(
    "αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"
    "±×÷≈≠≤≥∞∑∏∫∂∇√∝∈∉⊂⊃∪∩→←↔⇒⇔°µÅ℃‰"
)


# ----------------------------- Helper Utilities -----------------------------

def normalize_ws(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def normalize_for_header_match(text: str) -> str:
    """Normalize text for cross-page running header/footer matching."""
    t = normalize_ws(text).lower()
    t = re.sub(r"\d+", "#", t)
    return t


def detect_garbled_ratio(text: str) -> Tuple[float, int]:
    """Return (garbled_ratio, cid_count) to detect font-encoding failures."""
    if not text:
        return 0.0, 0
    cid_matches = CID_RE.findall(text)
    cid_count = len(cid_matches)
    cleaned = CID_RE.sub("", text)
    total_non_ws = sum(1 for c in cleaned if not c.isspace())
    if total_non_ws == 0 and cid_count > 0:
        return 1.0, cid_count
    if total_non_ws == 0:
        return 0.0, 0
    bad_chars = sum(
        1
        for c in cleaned
        if c == "\ufffd" or (ord(c) < 32 and c not in "\n\r\t") or (0xE000 <= ord(c) <= 0xF8FF)
    )
    ratio = (bad_chars + cid_count * 4) / max(1, total_non_ws + cid_count * 4)
    return min(1.0, round(ratio, 4)), cid_count


def bbox_overlaps(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], pad: float = 2.0) -> bool:
    return not (
        a[2] + pad < b[0]
        or a[0] - pad > b[2]
        or a[3] + pad < b[1]
        or a[1] - pad > b[3]
    )


def merge_bbox(boxes: List[Tuple[float, float, float, float]]) -> Tuple[float, float, float, float]:
    return (
        round(min(b[0] for b in boxes), 2),
        round(min(b[1] for b in boxes), 2),
        round(max(b[2] for b in boxes), 2),
        round(max(b[3] for b in boxes), 2),
    )


def format_markdown_table(headers: List[str], rows: List[List[str]]) -> str:
    ncols = max([len(headers)] + [len(r) for r in rows] + [1])
    hdr = [(headers[i] if i < len(headers) else "") for i in range(ncols)]
    hdr = [normalize_ws(c).replace("|", "\\|") for c in hdr]
    lines = [
        "| " + " | ".join(hdr) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    for r in rows:
        vals = [(r[i] if i < len(r) else "") for i in range(ncols)]
        vals = [normalize_ws(c).replace("|", "\\|") for c in vals]
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


# ----------------------------- Encryption Check -----------------------------

def check_pdf_encryption(pdf_path: Path) -> Tuple[bool, Optional[str]]:
    """Return (is_blocked_encrypted, error_message)."""
    if pypdf is not None:
        try:
            reader = pypdf.PdfReader(str(pdf_path))
            if reader.is_encrypted:
                try:
                    res = reader.decrypt("")
                    if res == 0:
                        return True, "PDF is password-protected (empty-password decrypt failed)."
                except Exception as exc:
                    return True, f"PDF is encrypted and could not be decrypted: {exc}"
            return False, None
        except Exception as exc:
            msg = str(exc)
            if "encrypt" in msg.lower() or "password" in msg.lower():
                return True, f"PDF encryption error: {msg}"
    if shutil.which("pdfinfo"):
        proc = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0 and ("password" in proc.stderr.lower() or "encrypt" in proc.stderr.lower()):
            return True, proc.stderr.strip()
    return False, None


# ----------------------------- Page Rendering & OCR -----------------------------

def render_page_png(pdf_path: Path, page_num_1idx: int, out_png: Path, dpi: int = 250) -> bool:
    """Render a 1-indexed PDF page to PNG using pypdfium2 or pdftoppm."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if pdfium is not None:
        try:
            doc = pdfium.PdfDocument(str(pdf_path))
            page = doc[page_num_1idx - 1]
            scale = dpi / 72.0
            bitmap = page.render(scale=scale)
            pil_img = bitmap.to_pil()
            pil_img.save(str(out_png))
            doc.close()
            return True
        except Exception:
            pass
    if shutil.which("pdftoppm"):
        prefix = str(out_png.with_suffix(""))
        proc = subprocess.run(
            [
                "pdftoppm",
                "-png",
                "-r",
                str(dpi),
                "-f",
                str(page_num_1idx),
                "-l",
                str(page_num_1idx),
                "-singlefile",
                str(pdf_path),
                prefix,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode == 0 and out_png.exists():
            return True
    return False


def run_ocr_on_image(png_path: Path, lang: str = "eng") -> Tuple[str, Optional[float], List[str]]:
    """Preprocess image and run Tesseract OCR, returning (text, mean_conf, warnings)."""
    warnings: List[str] = []
    if not png_path.exists():
        return "", None, ["OCR skipped: rendered page image not found."]

    # Pick available languages (prefer eng+chi_sim if installed)
    if shutil.which("tesseract"):
        lp = subprocess.run(
            ["tesseract", "--list-langs"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        avail = set(lp.stdout.splitlines())
        if "chi_sim" in avail and "eng" in avail:
            lang = "eng+chi_sim"
        elif "eng" in avail:
            lang = "eng"

    work_img_path = png_path
    if Image is not None:
        try:
            im = Image.open(png_path)
            im = ImageOps.grayscale(im)
            im = ImageOps.autocontrast(im)
            im = im.filter(ImageFilter.SHARPEN)
            pre_path = png_path.with_name(png_path.stem + "_pre.png")
            im.save(str(pre_path))
            work_img_path = pre_path
        except Exception as exc:
            warnings.append(f"Image preprocessing failed; using raw render: {exc}")

    if pytesseract is not None and Image is not None:
        try:
            im = Image.open(work_img_path)
            data = pytesseract.image_to_data(im, lang=lang, output_type=pytesseract.Output.DICT)
            confs = [float(c) for c in data.get("conf", []) if str(c).strip() not in ("", "-1")]
            mean_conf = round(sum(confs) / len(confs), 2) if confs else None
            text = pytesseract.image_to_string(im, lang=lang)
            if work_img_path != png_path and work_img_path.exists():
                work_img_path.unlink(missing_ok=True)
            if mean_conf is not None and mean_conf < 65.0:
                warnings.append(f"Low OCR confidence ({mean_conf}%): verify numbers and symbols visually.")
            return text.strip(), mean_conf, warnings
        except Exception as exc:
            warnings.append(f"pytesseract failed ({exc}); falling back to tesseract CLI.")

    if shutil.which("tesseract"):
        proc = subprocess.run(
            ["tesseract", str(work_img_path), "stdout", "-l", lang],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if work_img_path != png_path and work_img_path.exists():
            work_img_path.unlink(missing_ok=True)
        if proc.returncode == 0:
            return proc.stdout.strip(), None, warnings
        warnings.append(f"tesseract CLI error: {proc.stderr.strip()}")
    else:
        warnings.append("No OCR engine available (pytesseract/tesseract not installed).")
    return "", None, warnings


# ----------------------------- Character & Line Geometry -----------------------------

@dataclasses.dataclass
class TextLine:
    text: str
    rich_text: str
    bbox: Tuple[float, float, float, float]  # x0, top, x1, bottom
    font_size: float
    is_bold: bool
    has_math_symbols: bool
    garbled_ratio: float
    column_index: int = 0


def build_lines_from_chars(chars: List[Dict[str, Any]], y_tol: float = 3.0) -> List[TextLine]:
    """
    Group pdfplumber chars into visual lines while preserving superscripts (^{...})
    and subscripts (_{...}) using baseline/top & font-size deltas.
    """
    if not chars:
        return []

    # Sort roughly top-to-bottom then left-to-right
    sorted_chars = sorted(chars, key=lambda c: (round(float(c["top"]) / 2.0), float(c["x0"])))

    # Cluster into horizontal lines by vertical overlap / center-y proximity
    raw_lines: List[List[Dict[str, Any]]] = []
    for ch in sorted_chars:
        ch_top = float(ch["top"])
        ch_bot = float(ch["bottom"])
        ch_mid = 0.5 * (ch_top + ch_bot)
        placed = False
        # Check last few lines for vertical overlap (handles superscripts/subscripts)
        for line in reversed(raw_lines[-8:]):
            l_top = min(float(x["top"]) for x in line)
            l_bot = max(float(x["bottom"]) for x in line)
            l_mid = 0.5 * (l_top + l_bot)
            l_h = max(4.0, l_bot - l_top)
            if abs(ch_mid - l_mid) <= max(y_tol, 0.55 * l_h):
                line.append(ch)
                placed = True
                break
        if not placed:
            raw_lines.append([ch])

    result_lines: List[TextLine] = []
    for line_chars in raw_lines:
        line_chars.sort(key=lambda c: float(c["x0"]))
        # Split wide horizontal gaps (e.g., two columns on the same y-level!)
        sizes = [float(c.get("size", 10.0)) for c in line_chars if float(c.get("size", 0)) > 0]
        med_size = statistics.median(sizes) if sizes else 10.0
        col_gap_thresh = max(18.0, med_size * 2.4)

        segments: List[List[Dict[str, Any]]] = [[line_chars[0]]]
        for ch in line_chars[1:]:
            prev = segments[-1][-1]
            gap = float(ch["x0"]) - float(prev["x1"])
            if gap > col_gap_thresh:
                segments.append([ch])
            else:
                segments[-1].append(ch)

        for seg in segments:
            seg_sizes = [float(c.get("size", med_size)) for c in seg if c.get("text", "").strip()]
            base_size = statistics.median(seg_sizes) if seg_sizes else med_size
            # Compute baseline (bottom) of normal-sized chars
            normal_bottoms = [
                float(c["bottom"])
                for c in seg
                if c.get("text", "").strip() and abs(float(c.get("size", base_size)) - base_size) <= 1.2
            ]
            base_bottom = statistics.median(normal_bottoms) if normal_bottoms else statistics.median(
                [float(c["bottom"]) for c in seg]
            )

            plain_parts: List[str] = []
            rich_parts: List[str] = []
            bold_count = 0
            non_ws_count = 0
            active_script: Optional[str] = None  # "sup" or "sub"
            script_buf: List[str] = []

            def flush_script() -> None:
                nonlocal active_script, script_buf
                if active_script and script_buf:
                    content = "".join(script_buf).strip()
                    if content:
                        if active_script == "sup":
                            rich_parts.append(f"^{{{content}}}")
                        else:
                            rich_parts.append(f"_{{{content}}}")
                active_script = None
                script_buf = []

            prev_x1: Optional[float] = None
            for ch in seg:
                t = ch.get("text", "")
                if not t:
                    continue
                x0 = float(ch["x0"])
                x1 = float(ch["x1"])
                if prev_x1 is not None and (x0 - prev_x1) > max(1.2, base_size * 0.22):
                    if plain_parts and not plain_parts[-1].endswith(" "):
                        plain_parts.append(" ")
                    if active_script:
                        flush_script()
                    if rich_parts and not rich_parts[-1].endswith(" "):
                        rich_parts.append(" ")

                plain_parts.append(t)
                c_size = float(ch.get("size", base_size))
                c_bot = float(ch["bottom"])
                fontname = str(ch.get("fontname", "")).lower()
                if t.strip():
                    non_ws_count += 1
                    if "bold" in fontname or "black" in fontname or "heavy" in fontname or "medi" in fontname:
                        bold_count += 1

                is_smaller = c_size <= base_size * 0.84
                dy = base_bottom - c_bot  # positive => raised (superscript); negative => lowered (subscript)
                if t.strip() and is_smaller and dy >= base_size * 0.22:
                    if active_script != "sup":
                        flush_script()
                        active_script = "sup"
                    script_buf.append(t)
                elif t.strip() and is_smaller and dy <= -base_size * 0.14:
                    if active_script != "sub":
                        flush_script()
                        active_script = "sub"
                    script_buf.append(t)
                else:
                    flush_script()
                    rich_parts.append(t)
                prev_x1 = x1

            flush_script()
            plain_str = normalize_ws("".join(plain_parts))
            rich_str = normalize_ws("".join(rich_parts))
            if not plain_str:
                continue

            bbox = (
                round(min(float(c["x0"]) for c in seg), 2),
                round(min(float(c["top"]) for c in seg), 2),
                round(max(float(c["x1"]) for c in seg), 2),
                round(max(float(c["bottom"]) for c in seg), 2),
            )
            garbled_ratio, _ = detect_garbled_ratio(plain_str)
            has_math = any(ch in GREEK_AND_MATH_CHARS for ch in plain_str) or ("^{" in rich_str or "_{" in rich_str)
            result_lines.append(
                TextLine(
                    text=plain_str,
                    rich_text=rich_str,
                    bbox=bbox,
                    font_size=round(base_size, 2),
                    is_bold=(non_ws_count > 0 and (bold_count / non_ws_count) >= 0.6),
                    has_math_symbols=has_math,
                    garbled_ratio=garbled_ratio,
                )
            )

    return result_lines


# ----------------------------- Gutter-Aware Multi-Column Ordering -----------------------------

def detect_gutter_x(lines: List[TextLine], page_width: float) -> Optional[float]:
    """
    Detect a vertical two-column gutter near the center of the page.
    Returns gutter_x if a strong two-column split exists, else None.
    """
    if len(lines) < 6 or page_width <= 0:
        return None

    # Candidate lines that do NOT span across the middle
    mid_lo = page_width * 0.38
    mid_hi = page_width * 0.62
    left_lines = [ln for ln in lines if ln.bbox[2] <= page_width * 0.54]
    right_lines = [ln for ln in lines if ln.bbox[0] >= page_width * 0.46]
    if len(left_lines) < 3 or len(right_lines) < 3:
        return None

    # Scan candidate x positions in [mid_lo, mid_hi] for minimal line crossings
    best_x: Optional[float] = None
    best_score = -1.0
    step = max(2.0, page_width / 120.0)
    x = mid_lo
    while x <= mid_hi:
        left_n = sum(1 for ln in lines if ln.bbox[2] <= x)
        right_n = sum(1 for ln in lines if ln.bbox[0] >= x)
        cross_n = sum(1 for ln in lines if ln.bbox[0] < x < ln.bbox[2])
        if left_n >= 3 and right_n >= 3:
            # Prefer gutters with high left+right balance and very few crossings
            score = min(left_n, right_n) * 2.0 - cross_n * 1.5
            if score > best_score:
                best_score = score
                best_x = x
        x += step

    if best_x is None or best_score <= 2.0:
        return None
    return round(best_x, 2)


def order_lines_xy_cut(lines: List[TextLine], page_width: float) -> Tuple[List[TextLine], bool, Optional[float]]:
    """
    Order lines using gutter-aware band segmentation:
    - Lines spanning across gutter_x act as full-width band dividers (e.g., title, abstract, wide caption).
    - Within each band between full-width dividers, read left column top-to-bottom, then right column top-to-bottom.
    """
    if not lines:
        return [], False, None

    gutter_x = detect_gutter_x(lines, page_width)
    if gutter_x is None:
        ordered = sorted(lines, key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        for ln in ordered:
            ln.column_index = 0
        return ordered, False, None

    # Partition into full-width vs left vs right
    full_lines: List[TextLine] = []
    col_lines: List[TextLine] = []
    for ln in lines:
        if ln.bbox[0] < gutter_x - 8.0 and ln.bbox[2] > gutter_x + 8.0:
            ln.column_index = 0
            full_lines.append(ln)
        elif (ln.bbox[0] + ln.bbox[2]) * 0.5 < gutter_x:
            ln.column_index = 1
            col_lines.append(ln)
        else:
            ln.column_index = 2
            col_lines.append(ln)

    full_lines.sort(key=lambda ln: ln.bbox[1])

    # Create vertical bands separated by full-width lines
    bands: List[Tuple[float, float]] = []
    prev_y = -1e9
    for fl in full_lines:
        bands.append((prev_y, fl.bbox[1]))
        prev_y = fl.bbox[3]
    bands.append((prev_y, 1e9))

    ordered: List[TextLine] = []
    for idx, (y_lo, y_hi) in enumerate(bands):
        band_cols = [
            ln for ln in col_lines if y_lo <= (0.5 * (ln.bbox[1] + ln.bbox[3])) < y_hi
        ]
        left_b = sorted([ln for ln in band_cols if ln.column_index == 1], key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        right_b = sorted([ln for ln in band_cols if ln.column_index == 2], key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        ordered.extend(left_b)
        ordered.extend(right_b)
        if idx < len(full_lines):
            ordered.append(full_lines[idx])

    return ordered, True, gutter_x


# ----------------------------- Block Grouping & Classification -----------------------------

def group_lines_into_blocks(
    ordered_lines: List[TextLine],
    page_num: int,
    printed_page: Optional[str],
    body_font_size: float,
    page_height: float,
    method: str = "native_text",
) -> List[Dict[str, Any]]:
    """Group ordered lines into coherent semantic blocks with provenance."""
    if not ordered_lines:
        return []

    raw_groups: List[List[TextLine]] = []
    for ln in ordered_lines:
        if not raw_groups:
            raw_groups.append([ln])
            continue
        prev_grp = raw_groups[-1]
        prev_ln = prev_grp[-1]

        # Decide whether to break block
        same_col = ln.column_index == prev_ln.column_index
        v_gap = ln.bbox[1] - prev_ln.bbox[3]
        line_h = max(6.0, prev_ln.bbox[3] - prev_ln.bbox[1])
        same_style = abs(ln.font_size - prev_ln.font_size) <= 1.2 and (ln.is_bold == prev_ln.is_bold)

        # Break on explicit structural starts (caption, heading, footnote, numbered equation)
        starts_structural = bool(
            FIGURE_CAPTION_RE.match(ln.text)
            or TABLE_CAPTION_RE.match(ln.text)
            or SECTION_NUM_RE.match(ln.text)
            or STANDARD_HEADING_RE.match(ln.text)
            or (ln.bbox[1] > page_height * 0.80 and FOOTNOTE_START_RE.match(ln.text))
        )
        prev_is_heading = bool(
            (prev_ln.is_bold and prev_ln.font_size >= body_font_size * 0.98 and len(prev_ln.text) < 120)
            or STANDARD_HEADING_RE.match(prev_ln.text)
        )
        indent_break = (ln.bbox[0] - prev_grp[0].bbox[0]) > max(10.0, body_font_size * 1.1)

        if (
            not same_col
            or v_gap < -2.0
            or v_gap > max(8.0, line_h * 0.85)
            or not same_style
            or starts_structural
            or prev_is_heading
            or indent_break
        ):
            raw_groups.append([ln])
        else:
            prev_grp.append(ln)

    blocks: List[Dict[str, Any]] = []
    for idx, grp in enumerate(raw_groups, start=1):
        # Dehyphenate soft line-break hyphens when joining English prose
        plain_joined = ""
        rich_joined = ""
        for i, ln in enumerate(grp):
            if i == 0:
                plain_joined = ln.text
                rich_joined = ln.rich_text
            else:
                if plain_joined.endswith("-") and len(plain_joined) > 2 and plain_joined[-2].isalpha() and ln.text[:1].islower():
                    plain_joined = plain_joined[:-1] + ln.text
                    rich_joined = rich_joined[:-1] + ln.rich_text
                else:
                    plain_joined += " " + ln.text
                    rich_joined += " " + ln.rich_text

        plain_joined = normalize_ws(plain_joined)
        rich_joined = normalize_ws(rich_joined)
        bbox = merge_bbox([ln.bbox for ln in grp])
        avg_size = round(statistics.mean([ln.font_size for ln in grp]), 2)
        is_bold = sum(1 for ln in grp if ln.is_bold) >= max(1, len(grp) / 2)
        max_garbled = max(ln.garbled_ratio for ln in grp)

        role = classify_block_role(
            plain_joined, bbox, avg_size, is_bold, body_font_size, page_num, page_height, idx
        )

        warnings: List[str] = []
        if max_garbled >= 0.08:
            warnings.append(f"garbled_glyph_ratio={max_garbled}")

        blocks.append(
            {
                "block_id": f"p{page_num}_b{idx:03d}",
                "pdf_page": page_num,
                "printed_page": printed_page,
                "column_index": grp[0].column_index,
                "bbox": list(bbox),
                "font_size": avg_size,
                "is_bold": is_bold,
                "role": role,
                "text": plain_joined,
                "rich_text": rich_joined,
                "method": method,
                "warnings": warnings,
            }
        )
    return blocks


def classify_block_role(
    text: str,
    bbox: Tuple[float, float, float, float],
    font_size: float,
    is_bold: bool,
    body_font_size: float,
    page_num: int,
    page_height: float,
    block_idx: int,
) -> str:
    if page_num == 1 and block_idx <= 3 and font_size >= body_font_size * 1.25 and len(text) <= 260:
        return "title"
    if FIGURE_CAPTION_RE.match(text):
        return "figure_caption"
    if TABLE_CAPTION_RE.match(text) and len(text) <= 400:
        return "table_caption"
    if EQUATION_NUM_RE.search(text) and len(text) <= 220 and (
        any(c in text for c in "=+-*/^_∫∑∏") or any(c in GREEK_AND_MATH_CHARS for c in text)
    ):
        return "equation"
    if bbox[1] >= page_height * 0.80 and font_size <= body_font_size * 0.92 and FOOTNOTE_START_RE.match(text):
        return "footnote"
    if STANDARD_HEADING_RE.match(text):
        return "heading"
    if SECTION_NUM_RE.match(text) and len(text) <= 140 and (is_bold or font_size >= body_font_size * 1.05):
        return "heading"
    if is_bold and font_size >= body_font_size * 1.12 and len(text) <= 140 and not text.endswith("."):
        return "heading"
    return "paragraph"


# ----------------------------- Running Header/Footer Detection -----------------------------

def strip_running_headers_footers(
    page_lines_map: Dict[int, List[TextLine]],
    page_heights: Dict[int, float],
) -> Tuple[Dict[int, List[TextLine]], Dict[int, Optional[str]], List[Dict[str, Any]]]:
    """
    Detect repeated running headers/footers & standalone page numbers across pages.
    Returns (cleaned_page_lines_map, printed_page_labels, extracted_headers_footers).
    """
    num_pages = len(page_lines_map)
    top_counts: collections.Counter[str] = collections.Counter()
    bot_counts: collections.Counter[str] = collections.Counter()

    for p, lines in page_lines_map.items():
        h = page_heights.get(p, 792.0)
        seen_top, seen_bot = set(), set()
        for ln in lines:
            norm = normalize_for_header_match(ln.text)
            if ln.bbox[1] <= h * 0.11:
                seen_top.add(norm)
            elif ln.bbox[3] >= h * 0.89:
                seen_bot.add(norm)
        for s in seen_top:
            top_counts[s] += 1
        for s in seen_bot:
            bot_counts[s] += 1

    min_repeat = 2 if num_pages >= 2 else 999
    cleaned_map: Dict[int, List[TextLine]] = {}
    printed_labels: Dict[int, Optional[str]] = {}
    hf_records: List[Dict[str, Any]] = []

    for p, lines in page_lines_map.items():
        h = page_heights.get(p, 792.0)
        kept: List[TextLine] = []
        p_label: Optional[str] = None
        for ln in lines:
            norm = normalize_for_header_match(ln.text)
            in_top = ln.bbox[1] <= h * 0.11
            in_bot = ln.bbox[3] >= h * 0.89
            is_page_num = bool(PAGE_NUM_RE.match(ln.text)) and (in_top or in_bot)
            is_rep_header = in_top and top_counts[norm] >= min_repeat and len(ln.text) <= 160
            is_rep_footer = in_bot and bot_counts[norm] >= min_repeat and len(ln.text) <= 160

            if is_page_num:
                m = PAGE_NUM_RE.match(ln.text)
                if m and p_label is None:
                    p_label = m.group(1)
                hf_records.append(
                    {
                        "pdf_page": p,
                        "kind": "page_number",
                        "text": ln.text,
                        "bbox": list(ln.bbox),
                    }
                )
            elif is_rep_header or is_rep_footer:
                hf_records.append(
                    {
                        "pdf_page": p,
                        "kind": "running_header" if is_rep_header else "running_footer",
                        "text": ln.text,
                        "bbox": list(ln.bbox),
                    }
                )
            else:
                kept.append(ln)
        cleaned_map[p] = kept
        printed_labels[p] = p_label

    return cleaned_map, printed_labels, hf_records


# ----------------------------- Table Extraction & Cross-Page Stitching -----------------------------

def clean_table_grid(raw_grid: List[List[Optional[str]]]) -> Tuple[List[str], List[List[str]]]:
    """Normalize merged/None cells and split header vs data rows."""
    cleaned: List[List[str]] = []
    for row in raw_grid:
        if not row:
            continue
        vals = [normalize_ws(c or "") for c in row]
        if any(vals):
            cleaned.append(vals)
    if not cleaned:
        return [], []
    # Forward-fill horizontal merged header cells in row 0 if row 0 has blanks followed by text
    headers = cleaned[0]
    data_rows = cleaned[1:]
    return headers, data_rows


def extract_page_tables_pdfplumber(page: Any, page_num: int, printed_page: Optional[str]) -> List[Dict[str, Any]]:
    """Extract tables on a pdfplumber page using lattice first, then gentle stream fallback."""
    tables_out: List[Dict[str, Any]] = []
    found_tables = page.find_tables(
        table_settings={"vertical_strategy": "lines", "horizontal_strategy": "lines"}
    )
    if not found_tables:
        found_tables = page.find_tables(
            table_settings={
                "vertical_strategy": "text",
                "horizontal_strategy": "lines",
                "snap_tolerance": 3,
                "join_tolerance": 3,
            }
        )

    for idx, tbl in enumerate(found_tables, start=1):
        raw = tbl.extract()
        headers, rows = clean_table_grid(raw)
        if not headers or (len(headers) < 2 and len(rows) < 2):
            continue
        bbox = tuple(round(float(v), 2) for v in tbl.bbox)
        tables_out.append(
            {
                "table_id": f"p{page_num}_t{idx:02d}",
                "pages": [page_num],
                "printed_pages": [printed_page] if printed_page else [],
                "bbox_by_page": {str(page_num): list(bbox)},
                "caption": None,
                "headers": headers,
                "rows": rows,
                "markdown": format_markdown_table(headers, rows),
                "continuation_merged": False,
                "method": "pdfplumber",
            }
        )
    return tables_out


def stitch_cross_page_tables(tables: List[Dict[str, Any]], blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Attach nearest table caption on the same page and merge cross-page continued tables
    (e.g., 'Table 1 (continued)' / '续表' or identical headers & column count on consecutive pages).
    """
    # Step 1: Attach captions from blocks
    caption_blocks = [b for b in blocks if b["role"] == "table_caption"]
    for tbl in tables:
        p = tbl["pages"][0]
        tbbox = tbl["bbox_by_page"][str(p)]
        best_cap = None
        best_dist = 120.0
        for cb in caption_blocks:
            if cb["pdf_page"] != p:
                continue
            # Prefer captions immediately above (or slightly below) the table
            dist_above = tbbox[1] - cb["bbox"][3]
            dist_below = cb["bbox"][1] - tbbox[3]
            dist = dist_above if -15 <= dist_above <= 120 else (dist_below if 0 <= dist_below <= 60 else 999.0)
            if dist < best_dist:
                best_dist = dist
                best_cap = cb
        if best_cap is not None:
            tbl["caption"] = best_cap["text"]
            tbl["caption_block_id"] = best_cap["block_id"]

    # Step 2: Merge cross-page continuations
    merged: List[Dict[str, Any]] = []
    for tbl in tables:
        if not merged:
            merged.append(tbl)
            continue
        prev = merged[-1]
        prev_last_page = prev["pages"][-1]
        cur_page = tbl["pages"][0]
        same_cols = len(prev["headers"]) == len(tbl["headers"]) and len(prev["headers"]) >= 2
        cap_is_cont = bool(tbl.get("caption") and TABLE_CONT_RE.search(tbl["caption"]))
        same_header = [normalize_ws(x).lower() for x in prev["headers"]] == [
            normalize_ws(x).lower() for x in tbl["headers"]
        ]

        if cur_page == prev_last_page + 1 and same_cols and (cap_is_cont or same_header):
            prev["pages"].append(cur_page)
            if tbl["printed_pages"]:
                prev["printed_pages"].extend(tbl["printed_pages"])
            prev["bbox_by_page"].update(tbl["bbox_by_page"])
            if same_header:
                prev["rows"].extend(tbl["rows"])
            else:
                # Continuation page omitted repeated header row: first row is data
                prev["rows"].append(tbl["headers"])
                prev["rows"].extend(tbl["rows"])
            prev["markdown"] = format_markdown_table(prev["headers"], prev["rows"])
            prev["continuation_merged"] = True
        else:
            merged.append(tbl)

    return merged


# ----------------------------- Section Hierarchy & Front Matter -----------------------------

def build_sections_and_front_matter(blocks: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    front_matter: Dict[str, Any] = {
        "title": None,
        "title_block_id": None,
        "abstract": None,
        "abstract_block_ids": [],
    }
    sections: List[Dict[str, Any]] = []
    current_sec: Optional[Dict[str, Any]] = None
    in_abstract = False

    for b in blocks:
        if b["role"] == "title" and front_matter["title"] is None:
            front_matter["title"] = b["text"]
            front_matter["title_block_id"] = b["block_id"]
            continue

        if b["role"] == "heading":
            in_abstract = bool(re.match(r"^(?:abstract|摘要)\b", b["text"], re.IGNORECASE))
            m = SECTION_NUM_RE.match(b["text"])
            sec_num = m.group(1) or m.group(2) or m.group(3) if m else None
            level = (sec_num.count(".") + 1) if (sec_num and sec_num[0].isdigit()) else 1
            current_sec = {
                "section_id": f"sec_{len(sections) + 1:02d}",
                "number": sec_num,
                "level": level,
                "heading": b["text"],
                "heading_block_id": b["block_id"],
                "start_page": b["pdf_page"],
                "end_page": b["pdf_page"],
                "block_ids": [],
            }
            sections.append(current_sec)
            continue

        # Handle inline "Abstract: ..." paragraph
        if front_matter["abstract"] is None and re.match(r"^(?:abstract|摘要)\s*[:.：—\-]", b["text"], re.IGNORECASE):
            front_matter["abstract"] = b["text"]
            front_matter["abstract_block_ids"].append(b["block_id"])

        if in_abstract and b["role"] == "paragraph":
            front_matter["abstract"] = (
                (front_matter["abstract"] + "\n" + b["text"]).strip()
                if front_matter["abstract"]
                else b["text"]
            )
            front_matter["abstract_block_ids"].append(b["block_id"])

        if current_sec is not None:
            current_sec["block_ids"].append(b["block_id"])
            current_sec["end_page"] = b["pdf_page"]

    return front_matter, sections


# ----------------------------- Main Pipeline -----------------------------

def parse_pdf(
    pdf_path: Path,
    out_dir: Path,
    ocr_fallback: str = "auto",
    render_previews: str = "auto",
) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    previews_dir = out_dir / "previews"

    if not pdf_path.exists():
        doc = {
            "status": "failed_missing_input",
            "source_pdf": str(pdf_path),
            "error": f"Input PDF not found: {pdf_path}",
        }
        (out_dir / "document.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return doc

    is_encrypted, enc_err = check_pdf_encryption(pdf_path)
    if is_encrypted:
        doc = {
            "status": "blocked_encrypted",
            "source_pdf": str(pdf_path),
            "error": enc_err,
            "manual_takeover_needed": "Provide an unlocked PDF or password before parsing.",
        }
        (out_dir / "document.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return doc

    page_lines_map: Dict[int, List[TextLine]] = {}
    page_widths: Dict[int, float] = {}
    page_heights: Dict[int, float] = {}
    page_diagnostics: List[Dict[str, Any]] = []
    raw_tables: List[Dict[str, Any]] = []
    uncertainties: List[Dict[str, Any]] = []
    backend_used = "unknown"
    num_pages = 0

    if pdfplumber is not None:
        backend_used = "pdfplumber"
        with pdfplumber.open(str(pdf_path)) as pdf:
            num_pages = len(pdf.pages)
            for p_idx, page in enumerate(pdf.pages, start=1):
                w, h = float(page.width), float(page.height)
                page_widths[p_idx] = w
                page_heights[p_idx] = h

                # Extract tables first so we can exclude table cell text from body prose
                p_tables = extract_page_tables_pdfplumber(page, p_idx, None)
                table_bboxes = [tuple(t["bbox_by_page"][str(p_idx)]) for t in p_tables]
                raw_tables.extend(p_tables)

                chars = page.chars or []
                body_chars = [
                    c
                    for c in chars
                    if not any(
                        bbox_overlaps(
                            (float(c["x0"]), float(c["top"]), float(c["x1"]), float(c["bottom"])),
                            tb,
                            pad=1.0,
                        )
                        for tb in table_bboxes
                    )
                ]
                lines = build_lines_from_chars(body_chars)
                full_text_sample = " ".join(ln.text for ln in lines)
                garbled_ratio, cid_count = detect_garbled_ratio(full_text_sample)
                img_count = len(page.images or [])

                # Page quality triage
                char_count = len(full_text_sample.strip())
                if char_count < 30 and img_count > 0:
                    p_status = "scanned_image"
                elif garbled_ratio >= 0.12 or cid_count >= 5:
                    p_status = "garbled_font"
                elif char_count < 30 and not p_tables:
                    p_status = "mixed_or_low_text"
                else:
                    p_status = "native_text"

                diag: Dict[str, Any] = {
                    "pdf_page": p_idx,
                    "width": round(w, 2),
                    "height": round(h, 2),
                    "quality": p_status,
                    "char_count": char_count,
                    "image_count": img_count,
                    "table_count": len(p_tables),
                    "garbled_ratio": garbled_ratio,
                    "cid_count": cid_count,
                    "ocr_applied": False,
                    "ocr_confidence": None,
                    "preview_png": None,
                }

                # Render preview & apply OCR fallback when needed
                need_ocr = (
                    (ocr_fallback == "always")
                    or (ocr_fallback == "auto" and p_status in ("scanned_image", "garbled_font"))
                )
                need_preview = (
                    (render_previews == "always")
                    or (render_previews == "auto" and (need_ocr or p_status != "native_text"))
                )
                if need_preview or need_ocr:
                    png_path = previews_dir / f"page_{p_idx:03d}.png"
                    if render_page_png(pdf_path, p_idx, png_path):
                        diag["preview_png"] = str(png_path)
                        if need_ocr:
                            ocr_text, ocr_conf, ocr_warns = run_ocr_on_image(png_path)
                            diag["ocr_applied"] = True
                            diag["ocr_confidence"] = ocr_conf
                            if ocr_text:
                                # Replace or supplement garbled/scanned page lines
                                ocr_lines: List[TextLine] = []
                                y_cursor = 50.0
                                for raw_ln in ocr_text.splitlines():
                                    clean_ln = normalize_ws(raw_ln)
                                    if not clean_ln:
                                        y_cursor += 12.0
                                        continue
                                    ocr_lines.append(
                                        TextLine(
                                            text=clean_ln,
                                            rich_text=clean_ln,
                                            bbox=(40.0, y_cursor, w - 40.0, y_cursor + 12.0),
                                            font_size=10.0,
                                            is_bold=False,
                                            has_math_symbols=any(c in GREEK_AND_MATH_CHARS for c in clean_ln),
                                            garbled_ratio=0.0,
                                        )
                                    )
                                    y_cursor += 15.0
                                lines = ocr_lines
                            else:
                                ocr_warns.append("OCR returned empty text.")
                            for wmsg in ocr_warns:
                                uncertainties.append(
                                    {"pdf_page": p_idx, "type": p_status, "detail": wmsg, "preview_png": str(png_path)}
                                )
                    else:
                        uncertainties.append(
                            {
                                "pdf_page": p_idx,
                                "type": p_status,
                                "detail": "Failed to render page PNG for OCR/visual inspection.",
                            }
                        )

                if p_status in ("scanned_image", "garbled_font") and not diag["ocr_applied"]:
                    uncertainties.append(
                        {
                            "pdf_page": p_idx,
                            "type": p_status,
                            "detail": f"Page {p_idx} flagged as {p_status} without OCR fallback.",
                        }
                    )

                page_lines_map[p_idx] = lines
                page_diagnostics.append(diag)
    elif pypdf is not None:
        backend_used = "pypdf"
        reader = pypdf.PdfReader(str(pdf_path))
        num_pages = len(reader.pages)
        for p_idx, page in enumerate(reader.pages, start=1):
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)
            page_widths[p_idx] = w
            page_heights[p_idx] = h
            txt = page.extract_text() or ""
            lines = []
            y_cursor = 50.0
            for raw_ln in txt.splitlines():
                clean_ln = normalize_ws(raw_ln)
                if not clean_ln:
                    y_cursor += 10.0
                    continue
                lines.append(
                    TextLine(
                        text=clean_ln,
                        rich_text=clean_ln,
                        bbox=(40.0, y_cursor, w - 40.0, y_cursor + 12.0),
                        font_size=10.0,
                        is_bold=False,
                        has_math_symbols=False,
                        garbled_ratio=detect_garbled_ratio(clean_ln)[0],
                    )
                )
                y_cursor += 14.0
            page_lines_map[p_idx] = lines
            page_diagnostics.append({"pdf_page": p_idx, "quality": "native_text", "ocr_applied": False})
    else:
        raise RuntimeError("No supported PDF library found (install pdfplumber or pypdf).")

    # Strip running headers/footers across pages
    cleaned_lines_map, printed_labels, headers_footers = strip_running_headers_footers(
        page_lines_map, page_heights
    )

    # Estimate global body font size
    all_sizes = [ln.font_size for lines in cleaned_lines_map.values() for ln in lines if len(ln.text) > 20]
    body_font_size = round(statistics.median(all_sizes), 2) if all_sizes else 10.0

    # Order lines per page (gutter-aware XY-cut) & build blocks
    all_blocks: List[Dict[str, Any]] = []
    for p_idx in range(1, num_pages + 1):
        diag = page_diagnostics[p_idx - 1]
        ordered_lines, is_multi_col, gutter_x = order_lines_xy_cut(
            cleaned_lines_map[p_idx], page_widths.get(p_idx, 612.0)
        )
        diag["multi_column_detected"] = is_multi_col
        diag["gutter_x"] = gutter_x
        diag["printed_page"] = printed_labels.get(p_idx)
        method = "ocr_fallback" if diag.get("ocr_applied") else "native_text"
        p_blocks = group_lines_into_blocks(
            ordered_lines,
            p_idx,
            printed_labels.get(p_idx),
            body_font_size,
            page_heights.get(p_idx, 792.0),
            method=method,
        )
        all_blocks.extend(p_blocks)

    # Update table printed page labels & stitch cross-page tables
    for tbl in raw_tables:
        p = tbl["pages"][0]
        if printed_labels.get(p):
            tbl["printed_pages"] = [printed_labels[p]]
    stitched_tables = stitch_cross_page_tables(raw_tables, all_blocks)

    # Extract equations, figures, footnotes, references
    equations = []
    figures = []
    footnotes = []
    for b in all_blocks:
        if b["role"] == "equation":
            m = EQUATION_NUM_RE.search(b["text"])
            equations.append(
                {
                    "eq_id": f"eq_{len(equations) + 1:02d}",
                    "label": m.group(1) if m else None,
                    "text": b["text"],
                    "rich_text": b["rich_text"],
                    "pdf_page": b["pdf_page"],
                    "printed_page": b["printed_page"],
                    "block_id": b["block_id"],
                    "bbox": b["bbox"],
                }
            )
        elif b["role"] == "figure_caption":
            m = FIGURE_CAPTION_RE.match(b["text"])
            figures.append(
                {
                    "figure_id": f"fig_{len(figures) + 1:02d}",
                    "label": m.group(1) if m else None,
                    "caption": b["text"],
                    "pdf_page": b["pdf_page"],
                    "printed_page": b["printed_page"],
                    "block_id": b["block_id"],
                    "bbox": b["bbox"],
                }
            )
        elif b["role"] == "footnote":
            footnotes.append(
                {
                    "footnote_id": f"fn_{len(footnotes) + 1:02d}",
                    "text": b["text"],
                    "pdf_page": b["pdf_page"],
                    "printed_page": b["printed_page"],
                    "block_id": b["block_id"],
                    "bbox": b["bbox"],
                }
            )

    front_matter, sections = build_sections_and_front_matter(all_blocks)

    doc_status = "success" if not uncertainties else "partial"
    document = {
        "status": doc_status,
        "source_pdf": str(pdf_path),
        "metadata": {
            "num_pages": num_pages,
            "backend_used": backend_used,
            "estimated_body_font_size": body_font_size,
            "page_diagnostics": page_diagnostics,
        },
        "front_matter": front_matter,
        "sections": sections,
        "blocks": all_blocks,
        "tables": stitched_tables,
        "equations": equations,
        "figures": figures,
        "footnotes": footnotes,
        "headers_footers": headers_footers,
        "uncertainties": uncertainties,
    }

    # Write document.json
    (out_dir / "document.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Write clean, provenance-annotated document.md
    md_lines: List[str] = []
    if front_matter["title"]:
        md_lines.append(f"# {front_matter['title']}  `[p.1 | {front_matter['title_block_id']}]`\n")
    else:
        md_lines.append(f"# Parsed Document: {pdf_path.name}\n")

    for b in all_blocks:
        if b["block_id"] == front_matter["title_block_id"]:
            continue
        tag = f"[p.{b['pdf_page']} | {b['block_id']}]"
        txt = b["rich_text"]
        if b["role"] == "heading":
            md_lines.append(f"\n## {txt} `{tag}`\n")
        elif b["role"] == "equation":
            md_lines.append(f"\n> **Equation** `{tag}`: `{txt}`\n")
        elif b["role"] in ("figure_caption", "table_caption"):
            md_lines.append(f"\n*{txt}* `{tag}`\n")
        elif b["role"] == "footnote":
            md_lines.append(f"\n> Footnote `{tag}`: {txt}\n")
        else:
            md_lines.append(f"{txt} `{tag}`\n")

    if stitched_tables:
        md_lines.append("\n---\n## Extracted Tables\n")
        for t in stitched_tables:
            pages_str = ",".join(str(p) for p in t["pages"])
            cap = t["caption"] or f"Table {t['table_id']}"
            cont_note = " *(cross-page merged)*" if t["continuation_merged"] else ""
            md_lines.append(f"\n### {cap} `[p.{pages_str} | {t['table_id']}]`{cont_note}\n")
            md_lines.append(t["markdown"] + "\n")

    if uncertainties:
        md_lines.append("\n---\n## Uncertainties & Quality Warnings\n")
        for u in uncertainties:
            md_lines.append(f"- Page {u.get('pdf_page')}: [{u.get('type')}] {u.get('detail')}")

    (out_dir / "document.md").write_text("\n".join(md_lines), encoding="utf-8")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description="Structure-preserving scientific PDF parser.")
    parser.add_argument("--pdf", required=True, help="Path to input PDF file.")
    parser.add_argument("--out-dir", required=True, help="Directory for parsed outputs.")
    parser.add_argument(
        "--ocr-fallback",
        choices=["auto", "always", "never"],
        default="auto",
        help="OCR fallback mode for scanned/garbled pages.",
    )
    parser.add_argument(
        "--render-previews",
        choices=["auto", "always", "never"],
        default="auto",
        help="Render PNG previews for flagged or all pages.",
    )
    args = parser.parse_args()
    doc = parse_pdf(
        Path(args.pdf),
        Path(args.out_dir),
        ocr_fallback=args.ocr_fallback,
        render_previews=args.render_previews,
    )
    summary = {
        "status": doc.get("status"),
        "num_pages": doc.get("metadata", {}).get("num_pages", 0),
        "blocks": len(doc.get("blocks", [])),
        "sections": len(doc.get("sections", [])),
        "tables": len(doc.get("tables", [])),
        "equations": len(doc.get("equations", [])),
        "figures": len(doc.get("figures", [])),
        "footnotes": len(doc.get("footnotes", [])),
        "headers_footers_stripped": len(doc.get("headers_footers", [])),
        "uncertainties": len(doc.get("uncertainties", [])),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
