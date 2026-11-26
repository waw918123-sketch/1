#!/usr/bin/env python3
"""PDF diagnostic probe: inspects encryption, text layer density, font encoding defects, images, and column structure."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from pdf_common import detect_encoding_defects, discover_capabilities, import_pymupdf, normalize_ligatures


def _band_columns(band_blocks: List[Dict[str, Any]], page_w: float) -> Dict[str, Any]:
    """Estimate columns within a single horizontal band using 200-bin occupancy projection."""
    narrow = [b for b in band_blocks if (b["bbox"][2] - b["bbox"][0]) < 0.58 * page_w]
    if len(narrow) < 2:
        return {"estimated_columns": 1, "gutter_positions": []}

    bins = 200
    occ = [0] * bins
    for b in narrow:
        bx0, bx1 = float(b["bbox"][0]), float(b["bbox"][2])
        if bx1 - bx0 > 6.0:
            bx0 += 1.0
            bx1 -= 1.0
        x0 = max(0, min(bins - 1, int((bx0 / page_w) * bins)))
        x1 = max(0, min(bins - 1, int((bx1 / page_w) * bins)))
        for idx in range(x0, x1 + 1):
            occ[idx] += 1

    gutters: List[float] = []
    run_start: int | None = None
    for i in range(int(0.15 * bins), int(0.85 * bins) + 1):
        if occ[i] == 0:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and (i - run_start) >= 2:
                has_left = any(occ[j] > 0 for j in range(10, run_start))
                has_right = any(occ[j] > 0 for j in range(i, bins - 10))
                if has_left and has_right:
                    gutters.append(round(((run_start + i) / 2.0) / bins * page_w, 1))
            run_start = None

    est = min(3, 1 + len(gutters)) if gutters else 1
    return {"estimated_columns": est, "gutter_positions": gutters}


def _estimate_columns_from_blocks(blocks: List[Dict[str, Any]], page_w: float, page_h: float) -> Dict[str, Any]:
    """Estimate column count per spanning-separated horizontal band so title/author blocks never mask body columns."""
    if not blocks or page_w <= 0 or page_h <= 0:
        return {"estimated_columns": 1, "gutter_positions": []}

    body_blocks = [
        b for b in blocks
        if b["bbox"][1] > 0.08 * page_h and b["bbox"][3] < 0.92 * page_h
        and len(b.get("text", "").strip()) >= 12
    ]
    if len(body_blocks) < 2:
        return {"estimated_columns": 1, "gutter_positions": []}

    mid_x = 0.5 * page_w
    spanning = sorted(
        [
            b for b in body_blocks
            if (b["bbox"][2] - b["bbox"][0]) >= 0.55 * page_w
            and b["bbox"][0] < mid_x - 0.05 * page_w
            and b["bbox"][2] > mid_x + 0.05 * page_w
        ],
        key=lambda b: b["bbox"][1],
    )
    if not spanning:
        return _band_columns(body_blocks, page_w)

    bands: List[List[Dict[str, Any]]] = []
    prev_y = -1e9
    for sp in spanning:
        sp_cy = 0.5 * (sp["bbox"][1] + sp["bbox"][3])
        bands.append([b for b in body_blocks if b not in spanning and prev_y <= 0.5 * (b["bbox"][1] + b["bbox"][3]) < sp_cy])
        prev_y = sp_cy
    bands.append([b for b in body_blocks if b not in spanning and 0.5 * (b["bbox"][1] + b["bbox"][3]) >= prev_y])

    best = {"estimated_columns": 1, "gutter_positions": []}
    for band in bands:
        res = _band_columns(band, page_w)
        if res["estimated_columns"] > best["estimated_columns"]:
            best = res
    return best


def probe_with_fitz(pdf_path: Path) -> Dict[str, Any]:
    fitz = import_pymupdf()

    doc = fitz.open(str(pdf_path))
    is_encrypted = bool(doc.is_encrypted)
    needs_pass = bool(doc.needs_pass)
    if needs_pass:
        return {
            "backend": "pymupdf",
            "page_count": 0,
            "is_encrypted": True,
            "needs_password": True,
            "pages": [],
            "summary": {
                "overall_status": "encrypted_blocked",
                "recommended_pipeline": ["report_encrypted_pdf_cannot_extract"],
            },
        }

    pages_diag: List[Dict[str, Any]] = []
    scanned_pages: List[int] = []
    garbled_pages: List[int] = []
    multi_col_pages: List[int] = []
    complex_pages: List[int] = []

    for idx in range(len(doc)):
        page = doc[idx]
        pno = idx + 1
        rect = page.rect
        pw, ph = float(rect.width), float(rect.height)
        raw_text = normalize_ligatures(page.get_text("text") or "")
        defects = detect_encoding_defects(raw_text)

        text_dict = page.get_text("dict")
        text_blocks: List[Dict[str, Any]] = []
        mid_x = 0.5 * pw
        for blk in text_dict.get("blocks", []):
            if blk.get("type") != 0:
                continue
            blk_lines = blk.get("lines", [])
            bw = float(blk["bbox"][2]) - float(blk["bbox"][0])
            groups = [blk_lines]
            if bw >= 0.50 * pw and len(blk_lines) >= 2:
                left_l, right_l, split_ok = [], [], True
                for ln in blk_lines:
                    lbb = ln.get("bbox", [0, 0, 0, 0])
                    if lbb[2] < mid_x + 4.0 and lbb[0] < mid_x - 8.0:
                        left_l.append(ln)
                    elif lbb[0] > mid_x - 4.0 and lbb[2] > mid_x + 8.0:
                        right_l.append(ln)
                    else:
                        split_ok = False
                        break
                if split_ok and left_l and right_l:
                    groups = [left_l, right_l]
            for grp in groups:
                lines_txt = ["".join(sp.get("text", "") for sp in ln.get("spans", [])) for ln in grp]
                btxt = normalize_ligatures("\n".join(lines_txt)).strip()
                if not btxt:
                    continue
                gboxes = [ln.get("bbox") for ln in grp if ln.get("bbox")]
                if gboxes:
                    gb = [min(b[0] for b in gboxes), min(b[1] for b in gboxes), max(b[2] for b in gboxes), max(b[3] for b in gboxes)]
                else:
                    gb = list(blk["bbox"])
                text_blocks.append({"bbox": gb, "text": btxt})

        col_info = _estimate_columns_from_blocks(text_blocks, pw, ph)
        images = page.get_images(full=True)
        img_area = 0.0
        for img_info in page.get_image_info(xrefs=False):
            ib = img_info.get("bbox")
            if ib:
                img_area += max(0.0, ib[2] - ib[0]) * max(0.0, ib[3] - ib[1])
        page_area = max(1.0, pw * ph)
        img_coverage = round(min(1.0, img_area / page_area), 3)

        drawings_count = len(page.get_drawings())
        fonts = page.get_fonts(full=True)
        font_names = sorted({f[3] for f in fonts if len(f) > 3 and f[3]})

        char_count = len(raw_text.strip())
        if char_count < 40 and (len(images) > 0 or img_coverage > 0.25):
            classification = "scanned_or_image_only"
            scanned_pages.append(pno)
        elif defects["defect_rate"] >= 0.08 or defects["cid_tokens"] >= 10:
            classification = "garbled_encoding"
            garbled_pages.append(pno)
        elif col_info["estimated_columns"] >= 2 or drawings_count >= 12 or img_coverage >= 0.25:
            classification = "complex_hybrid"
            complex_pages.append(pno)
        else:
            classification = "native_text"

        if col_info["estimated_columns"] >= 2:
            multi_col_pages.append(pno)

        printed_label = None
        for b in text_blocks:
            bb = b["bbox"]
            if (bb[1] < 0.09 * ph or bb[3] > 0.91 * ph) and len(b["text"]) <= 80:
                m_pg = re.search(r"(?:page|pp\.?|第)\s*(\d+)\b|^\s*[-–—]?\s*(\d{1,4})\s*[-–—]?\s*$", b["text"], re.I)
                if m_pg:
                    printed_label = m_pg.group(1) or m_pg.group(2)
                    break

        pages_diag.append({
            "page": pno,
            "printed_page": printed_label,
            "width": round(pw, 1),
            "height": round(ph, 1),
            "char_count": char_count,
            "text_block_count": len(text_blocks),
            "image_count": len(images),
            "image_coverage": img_coverage,
            "drawing_count": drawings_count,
            "font_count": len(font_names),
            "sample_fonts": font_names[:10],
            "encoding_defects": defects,
            "estimated_columns": col_info["estimated_columns"],
            "gutter_positions": col_info["gutter_positions"],
            "classification": classification,
        })

    doc.close()
    printed_nums = [(d["page"], int(d["printed_page"])) for d in pages_diag if d["printed_page"] and d["printed_page"].isdigit()]
    page_num_discrepancy = any((n2 - n1) != (p2 - p1) for (p1, n1), (p2, n2) in zip(printed_nums, printed_nums[1:]))
    recs: List[str] = ["run pdf_extract.py for layout-ordered text, sections, tables, and anchors"]
    if scanned_pages or garbled_pages:
        need_ocr = sorted(set(scanned_pages + garbled_pages))
        recs.append(f"run pdf_render_ocr.py on pages {need_ocr} (scanned or garbled encoding)")
    if multi_col_pages:
        recs.append(f"multi-column layout detected on pages {multi_col_pages}; verify reading_order.md")
    if page_num_discrepancy:
        recs.append("printed page numbering discrepancy detected; cite 1-based physical PDF page and printed label")

    return {
        "backend": "pymupdf",
        "page_count": len(pages_diag),
        "is_encrypted": is_encrypted,
        "needs_password": False,
        "pages": pages_diag,
        "summary": {
            "overall_status": "ok_with_ocr_needed" if (scanned_pages or garbled_pages) else "ok",
            "scanned_pages": scanned_pages,
            "garbled_pages": garbled_pages,
            "multi_column_pages": multi_col_pages,
            "complex_pages": complex_pages,
            "page_numbering_discrepancy": page_num_discrepancy,
            "recommended_pipeline": recs,
        },
    }


def probe_fallback(pdf_path: Path, caps: Dict[str, Any]) -> Dict[str, Any]:
    """Fallback probe using pypdf or pdfinfo/pdftotext when PyMuPDF is unavailable."""
    if caps["python_modules"].get("pypdf"):
        import pypdf

        reader = pypdf.PdfReader(str(pdf_path))
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    return {
                        "backend": "pypdf",
                        "page_count": 0,
                        "is_encrypted": True,
                        "needs_password": True,
                        "pages": [],
                        "summary": {"overall_status": "encrypted_blocked"},
                    }
            except Exception:
                return {
                    "backend": "pypdf",
                    "page_count": 0,
                    "is_encrypted": True,
                    "needs_password": True,
                    "pages": [],
                    "summary": {"overall_status": "encrypted_blocked"},
                }
        pages_diag = []
        scanned, garbled = [], []
        for idx, page in enumerate(reader.pages):
            pno = idx + 1
            txt = normalize_ligatures(page.extract_text() or "")
            defects = detect_encoding_defects(txt)
            cls = "native_text"
            if len(txt.strip()) < 40:
                cls = "scanned_or_image_only"
                scanned.append(pno)
            elif defects["defect_rate"] >= 0.08:
                cls = "garbled_encoding"
                garbled.append(pno)
            pages_diag.append({
                "page": pno,
                "char_count": len(txt.strip()),
                "encoding_defects": defects,
                "classification": cls,
            })
        return {
            "backend": "pypdf",
            "page_count": len(pages_diag),
            "is_encrypted": False,
            "needs_password": False,
            "pages": pages_diag,
            "summary": {
                "overall_status": "ok_with_ocr_needed" if (scanned or garbled) else "ok",
                "scanned_pages": scanned,
                "garbled_pages": garbled,
            },
        }

    # CLI fallback via pdfinfo
    if caps["cli_tools"].get("pdfinfo"):
        proc = subprocess.run([caps["cli_tools"]["pdfinfo"], str(pdf_path)], capture_output=True, text=True, check=False)
        info_txt = proc.stdout or ""
        m_pages = re.search(r"Pages:\s*(\d+)", info_txt)
        m_enc = re.search(r"Encrypted:\s*(yes|no)", info_txt, re.I)
        n_pages = int(m_pages.group(1)) if m_pages else 0
        enc = bool(m_enc and m_enc.group(1).lower() == "yes")
        return {
            "backend": "pdfinfo",
            "page_count": n_pages,
            "is_encrypted": enc,
            "needs_password": enc,
            "pages": [],
            "raw_pdfinfo": info_txt,
            "summary": {"overall_status": "encrypted_blocked" if enc else "basic_info_only"},
        }

    return {
        "backend": "none",
        "page_count": 0,
        "error": "No PDF inspection library (fitz, pypdf, pdfinfo) found in environment.",
        "summary": {"overall_status": "tooling_missing"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe a PDF for capabilities, layout, encryption, and OCR needs.")
    parser.add_argument("pdf_path", help="Path to the input PDF file.")
    parser.add_argument("--out", help="Optional output JSON path.")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(json.dumps({"error": f"PDF not found: {pdf_path}"}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    header = pdf_path.read_bytes()[:16]
    if not header.startswith(b"%PDF-"):
        res = {
            "file": str(pdf_path),
            "size_bytes": pdf_path.stat().st_size,
            "valid_pdf_header": False,
            "error": f"Invalid PDF header: {header!r}",
        }
        out_str = json.dumps(res, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(out_str, encoding="utf-8")
        print(out_str)
        return 1

    caps = discover_capabilities()
    if caps["python_modules"].get("pymupdf_fitz"):
        diag = probe_with_fitz(pdf_path)
    else:
        diag = probe_fallback(pdf_path, caps)

    report = {
        "file": str(pdf_path),
        "size_bytes": pdf_path.stat().st_size,
        "valid_pdf_header": True,
        "capabilities": caps,
        **diag,
    }
    out_str = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(out_str, encoding="utf-8")
    print(out_str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
