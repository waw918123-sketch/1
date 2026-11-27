#!/usr/bin/env python3
"""Render scanned/garbled PDF pages or defect bounding boxes and run optional OCR with confidence reporting."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True

from doc_common import discover_capabilities, import_pymupdf, normalize_ligatures, parse_page_spec


def _parse_crop(crop_str: str) -> Tuple[int, List[float]]:
    p_str, coords_str = crop_str.split(":", 1)
    coords = [float(c.strip()) for c in coords_str.split(",")]
    if len(coords) != 4:
        raise ValueError(f"Crop must have 4 coordinates x0,y0,x1,y1: {crop_str}")
    return int(p_str.strip()), coords


def _preprocess_image(img_path: Path, is_crop: bool = False) -> Path:
    """Apply grayscale, optional 2x crop upscaling, autocontrast, and sharpening if Pillow is installed."""
    try:
        from PIL import Image, ImageFilter, ImageOps
    except Exception:
        return img_path
    img = Image.open(img_path).convert("L")
    if is_crop and img.width < 900:
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
        img = img.resize((img.width * 2, img.height * 2), resample=resample)
    img = ImageOps.autocontrast(img, cutoff=1)
    img = img.filter(ImageFilter.SHARPEN)
    proc_path = img_path.with_name(f"{img_path.stem}_pre{img_path.suffix}")
    img.save(proc_path)
    return proc_path


def _run_tesseract_cli(img_path: Path, lang: str, tesseract_bin: str, psm: str = "6") -> Dict[str, Any]:
    proc_txt = subprocess.run(
        [tesseract_bin, str(img_path), "stdout", "-l", lang, "--psm", psm],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    text = normalize_ligatures(proc_txt.stdout or "").strip()
    proc_tsv = subprocess.run(
        [tesseract_bin, str(img_path), "stdout", "-l", lang, "--psm", psm, "tsv"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
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
    return {
        "engine": "tesseract_cli",
        "lang": lang,
        "text": text,
        "word_count": len(confs) if confs else len(text.split()),
        "mean_confidence": round(sum(confs) / len(confs), 2) if confs else None,
    }


def _run_rapidocr(img_path: Path) -> Dict[str, Any]:
    from rapidocr_onnxruntime import RapidOCR  # type: ignore

    res, _ = RapidOCR()(str(img_path))
    lines, confs = [], []
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


def render_and_ocr(
    pdf_path: Path,
    out_dir: Path,
    page_spec: Optional[str] = None,
    dpi: int = 250,
    crops: Optional[List[str]] = None,
    ocr_mode: str = "auto",
    lang: str = "auto",
) -> Dict[str, Any]:
    """Render requested pages/crops and execute OCR if available."""
    crops = crops or []
    caps = discover_capabilities()
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered: List[Dict[str, Any]] = []

    use_fitz = bool(caps["python_modules"].get("pymupdf_fitz"))
    use_pdftoppm = bool(caps["cli_tools"].get("pdftoppm"))
    has_pdfium = bool(caps["python_modules"].get("pypdfium2"))
    if not use_fitz and not has_pdfium and not use_pdftoppm:
        return {
            "file": str(pdf_path),
            "status": "failed_missing_renderer",
            "error": "No PDF renderer (pymupdf, pypdfium2, or pdftoppm) installed.",
            "rendered": [],
        }

    if use_fitz:
        fitz = import_pymupdf()
        doc = fitz.open(str(pdf_path))
        if doc.needs_pass:
            return {"file": str(pdf_path), "status": "encrypted_blocked", "rendered": []}
        total_pages = len(doc)
        pages = parse_page_spec(page_spec, total_pages) if (page_spec or not crops) else []
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        for pno in pages:
            pix = doc[pno - 1].get_pixmap(matrix=mat, alpha=False)
            png_path = out_dir / f"page_{pno:03d}.png"
            pix.save(str(png_path))
            rendered.append({"kind": "page", "page": pno, "dpi": dpi, "image_path": str(png_path)})
        for idx, c_str in enumerate(crops):
            pno, coords = _parse_crop(c_str)
            if 1 <= pno <= total_pages:
                pad = 6.0
                pr = doc[pno - 1].rect
                clip = fitz.Rect(
                    max(0.0, coords[0] - pad),
                    max(0.0, coords[1] - pad),
                    min(float(pr.width), coords[2] + pad),
                    min(float(pr.height), coords[3] + pad),
                )
                pix = doc[pno - 1].get_pixmap(matrix=fitz.Matrix(max(dpi, 300) / 72.0, max(dpi, 300) / 72.0), clip=clip, alpha=False)
                png_path = out_dir / f"crop_{idx + 1:02d}_p{pno:03d}.png"
                pix.save(str(png_path))
                rendered.append({"kind": "crop", "page": pno, "bbox": coords, "dpi": max(dpi, 300), "image_path": str(png_path)})
        doc.close()
    elif has_pdfium:
        import pypdfium2 as pdfium  # type: ignore
        pdf_doc = pdfium.PdfDocument(str(pdf_path))
        total_pages = len(pdf_doc)
        pages = parse_page_spec(page_spec, total_pages) if (page_spec or not crops) else []
        for pno in pages:
            bmp = pdf_doc[pno - 1].render(scale=dpi / 72.0)
            png_path = out_dir / f"page_{pno:03d}.png"
            bmp.to_pil().save(str(png_path))
            rendered.append({"kind": "page", "page": pno, "dpi": dpi, "image_path": str(png_path)})
        pdf_doc.close()
    elif use_pdftoppm:
        pages = parse_page_spec(page_spec, 9999) if page_spec else [1]
        for pno in pages:
            prefix = out_dir / f"page_{pno:03d}"
            subprocess.run(
                [caps["cli_tools"]["pdftoppm"], "-png", "-r", str(dpi), "-f", str(pno), "-l", str(pno), "-singlefile", str(pdf_path), str(prefix)],
                capture_output=True, text=True, check=False,
            )
            png_path = Path(f"{prefix}.png")
            if png_path.exists():
                rendered.append({"kind": "page", "page": pno, "dpi": dpi, "image_path": str(png_path)})

    ocr_engine = None
    if ocr_mode != "none":
        if ocr_mode in ("auto", "tesseract") and caps["cli_tools"].get("tesseract"):
            ocr_engine = "tesseract"
        elif ocr_mode in ("auto", "rapidocr") and caps["python_modules"].get("rapidocr_onnxruntime"):
            ocr_engine = "rapidocr"

    tess_lang = lang
    if tess_lang == "auto" and ocr_engine == "tesseract":
        avail = set(caps.get("tesseract_langs", []))
        tess_lang = "chi_sim+eng" if ("chi_sim" in avail and "eng" in avail) else ("eng" if "eng" in avail else (next(iter(avail)) if avail else "eng"))

    for item in rendered:
        img_p = Path(item["image_path"])
        if ocr_engine == "tesseract":
            proc_p = _preprocess_image(img_p, is_crop=(item["kind"] == "crop"))
            item["ocr"] = _run_tesseract_cli(proc_p, tess_lang, caps["cli_tools"]["tesseract"], psm="6" if item["kind"] == "crop" else "3")
        elif ocr_engine == "rapidocr":
            proc_p = _preprocess_image(img_p, is_crop=(item["kind"] == "crop"))
            item["ocr"] = _run_rapidocr(proc_p)
        else:
            item["ocr"] = {
                "engine": None,
                "text": None,
                "mean_confidence": None,
                "note": "No OCR engine available; inspect PNG visually or mark human_review_required.",
            }

    payload = {
        "file": str(pdf_path),
        "status": "ok" if rendered else "no_pages_rendered",
        "ocr_engine_used": ocr_engine,
        "rendered_count": len(rendered),
        "rendered": rendered,
    }
    (out_dir / "ocr_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Render PDF pages/crops and optionally run OCR.")
    parser.add_argument("pdf_path")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--pages", default=None)
    parser.add_argument("--dpi", type=int, default=250)
    parser.add_argument("--crop", action="append", default=[])
    parser.add_argument("--ocr", default="auto", choices=("auto", "tesseract", "rapidocr", "none"))
    parser.add_argument("--lang", default="auto")
    args = parser.parse_args()
    res = render_and_ocr(Path(args.pdf_path), Path(args.out_dir), args.pages, args.dpi, args.crop, args.ocr, args.lang)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("status") in ("ok", "no_pages_rendered") else 1


if __name__ == "__main__":
    raise SystemExit(main())
