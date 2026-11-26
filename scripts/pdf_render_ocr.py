#!/usr/bin/env python3
"""Render PDF pages or cropped bounding boxes to PNG and run optional OCR with honest confidence reporting."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pdf_common import discover_capabilities, import_pymupdf, normalize_ligatures, parse_page_spec


def _parse_crop(crop_str: str) -> Tuple[int, List[float]]:
    """Parse 'page:x0,y0,x1,y1' crop specification."""
    p_str, coords_str = crop_str.split(":", 1)
    coords = [float(c.strip()) for c in coords_str.split(",")]
    if len(coords) != 4:
        raise ValueError(f"Crop must have 4 coordinates x0,y0,x1,y1: {crop_str}")
    return int(p_str.strip()), coords


def _preprocess_image_for_ocr(img_path: Path) -> Path:
    """Apply lightweight grayscale + autocontrast + sharpen preprocessing if Pillow is installed."""
    try:
        from PIL import Image, ImageFilter, ImageOps
    except Exception:
        return img_path

    img = Image.open(img_path).convert("L")
    img = ImageOps.autocontrast(img, cutoff=1)
    img = img.filter(ImageFilter.SHARPEN)
    proc_path = img_path.with_name(f"{img_path.stem}_pre{img_path.suffix}")
    img.save(proc_path)
    return proc_path


def _run_tesseract_cli(img_path: Path, lang: str, tesseract_bin: str) -> Dict[str, Any]:
    """Run tesseract CLI and extract TSV confidence + plain text."""
    # Plain text
    proc_txt = subprocess.run(
        [tesseract_bin, str(img_path), "stdout", "-l", lang, "--psm", "6"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    text = normalize_ligatures(proc_txt.stdout or "").strip()

    # TSV for word-level confidence
    proc_tsv = subprocess.run(
        [tesseract_bin, str(img_path), "stdout", "-l", lang, "--psm", "6", "tsv"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    confs: List[float] = []
    for line in (proc_tsv.stdout or "").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 12 and parts[11].strip():
            try:
                c = float(parts[10])
                if c >= 0:
                    confs.append(c)
            except ValueError:
                pass
    mean_conf = round(sum(confs) / len(confs), 2) if confs else None
    return {
        "engine": "tesseract_cli",
        "lang": lang,
        "text": text,
        "word_count": len(confs) if confs else len(text.split()),
        "mean_confidence": mean_conf,
        "stderr": (proc_txt.stderr or "").strip() or None,
    }


def _run_rapidocr(img_path: Path) -> Dict[str, Any]:
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    res, _ = engine(str(img_path))
    lines = []
    confs = []
    if res:
        for item in res:
            if len(item) >= 3:
                lines.append(str(item[1]))
                try:
                    confs.append(float(item[2]) * 100.0)
                except Exception:
                    pass
    return {
        "engine": "rapidocr_onnxruntime",
        "text": "\n".join(lines).strip(),
        "word_count": len(lines),
        "mean_confidence": round(sum(confs) / len(confs), 2) if confs else None,
    }


def render_pages(
    pdf_path: Path,
    out_dir: Path,
    page_spec: Optional[str],
    dpi: int,
    crops: List[str],
    ocr_mode: str,
    lang: str,
) -> Dict[str, Any]:
    caps = discover_capabilities()
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered: List[Dict[str, Any]] = []

    use_fitz = bool(caps["python_modules"].get("pymupdf_fitz"))
    use_pdftoppm = bool(caps["cli_tools"].get("pdftoppm"))

    if not use_fitz and not use_pdftoppm:
        return {
            "file": str(pdf_path),
            "status": "failed_missing_renderer",
            "error": "Neither PyMuPDF (fitz) nor pdftoppm is available to render PDF pages.",
            "capabilities": caps,
        }

    # Determine total pages
    total_pages = 0
    if use_fitz:
        fitz = import_pymupdf()

        doc = fitz.open(str(pdf_path))
        if doc.needs_pass:
            return {"file": str(pdf_path), "status": "encrypted_blocked", "rendered": []}
        total_pages = len(doc)
        pages = parse_page_spec(page_spec, total_pages) if (page_spec or not crops) else []
        scale = dpi / 72.0
        mat = fitz.Matrix(scale, scale)

        for pno in pages:
            page = doc[pno - 1]
            pix = page.get_pixmap(matrix=mat, alpha=False)
            png_path = out_dir / f"page_{pno:03d}.png"
            pix.save(str(png_path))
            rendered.append({"kind": "page", "page": pno, "dpi": dpi, "image_path": str(png_path)})

        for idx, c_str in enumerate(crops):
            pno, coords = _parse_crop(c_str)
            if 1 <= pno <= total_pages:
                page = doc[pno - 1]
                clip = fitz.Rect(*coords)
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                png_path = out_dir / f"crop_{idx + 1:02d}_p{pno:03d}.png"
                pix.save(str(png_path))
                rendered.append({
                    "kind": "crop",
                    "page": pno,
                    "bbox": coords,
                    "dpi": dpi,
                    "image_path": str(png_path),
                })
        doc.close()
    else:
        # pdftoppm fallback for whole pages
        if caps["cli_tools"].get("pdfinfo"):
            proc_info = subprocess.run([caps["cli_tools"]["pdfinfo"], str(pdf_path)], capture_output=True, text=True, check=False)
            for line in (proc_info.stdout or "").splitlines():
                if line.startswith("Pages:"):
                    total_pages = int(line.split(":", 1)[1].strip())
        pages = parse_page_spec(page_spec, total_pages or 1000)
        for pno in pages:
            prefix = out_dir / f"page_{pno:03d}"
            subprocess.run(
                [caps["cli_tools"]["pdftoppm"], "-png", "-r", str(dpi), "-f", str(pno), "-l", str(pno), "-singlefile", str(pdf_path), str(prefix)],
                capture_output=True,
                check=False,
            )
            png_path = Path(f"{prefix}.png")
            if png_path.exists():
                rendered.append({"kind": "page", "page": pno, "dpi": dpi, "image_path": str(png_path)})

    # OCR stage
    tesseract_bin = caps["cli_tools"].get("tesseract")
    has_rapidocr = bool(caps["python_modules"].get("rapidocr_onnxruntime"))
    avail_langs = set(caps.get("tesseract_langs", []))
    effective_lang = lang
    if tesseract_bin and avail_langs:
        requested = [x.strip() for x in lang.split("+") if x.strip()]
        valid = [x for x in requested if x in avail_langs]
        if valid:
            effective_lang = "+".join(valid)
        elif "eng" in avail_langs:
            effective_lang = "eng"

    ocr_engine_used: Optional[str] = None
    for item in rendered:
        if ocr_mode == "none":
            item["ocr"] = None
            continue
        img_p = Path(item["image_path"])
        proc_img = _preprocess_image_for_ocr(img_p)
        if ocr_mode in ("auto", "rapidocr") and has_rapidocr:
            ocr_res = _run_rapidocr(proc_img)
            ocr_engine_used = "rapidocr_onnxruntime"
        elif ocr_mode in ("auto", "tesseract") and tesseract_bin:
            ocr_res = _run_tesseract_cli(proc_img, effective_lang, tesseract_bin)
            ocr_engine_used = "tesseract_cli"
        else:
            ocr_res = {
                "engine": None,
                "text": "",
                "mean_confidence": None,
                "action_required": "inspect_rendered_png_visually",
            }
        if proc_img != img_p and proc_img.exists():
            item["preprocessed_image_path"] = str(proc_img)
        item["ocr"] = ocr_res

    result = {
        "file": str(pdf_path),
        "status": "ok" if rendered else "no_pages_rendered",
        "ocr_engine_used": ocr_engine_used,
        "rendered_count": len(rendered),
        "rendered": rendered,
        "notes": (
            "OCR completed; verify low-confidence segments visually against the PNGs."
            if ocr_engine_used
            else "No OCR engine available; inspect the rendered PNG images directly with visual reading."
        ),
    }
    (out_dir / "ocr_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Render PDF pages/crops to PNG and optionally run OCR.")
    parser.add_argument("pdf_path", help="Path to PDF file.")
    parser.add_argument("--out-dir", required=True, help="Output directory for PNGs and ocr_results.json.")
    parser.add_argument("--pages", default=None, help="1-based page spec, e.g. '1,3-5'.")
    parser.add_argument("--dpi", type=int, default=250, help="Render DPI (default: 250).")
    parser.add_argument("--crop", action="append", default=[], help="Crop region 'page:x0,y0,x1,y1' in PDF points.")
    parser.add_argument("--ocr", choices=["auto", "tesseract", "rapidocr", "none"], default="auto")
    parser.add_argument("--lang", default="eng+chi_sim", help="Tesseract language codes (default: eng+chi_sim).")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(json.dumps({"error": f"PDF not found: {pdf_path}"}, ensure_ascii=False), file=sys.stderr)
        return 2

    res = render_pages(pdf_path, Path(args.out_dir), args.pages, args.dpi, args.crop, args.ocr, args.lang)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
