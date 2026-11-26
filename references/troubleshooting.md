# Troubleshooting & Edge-Case Handling Matrix

| Symptom / Failure | Diagnosis | Resolution |
|---|---|---|
| `needs_password: true` in `probe.json` | PDF is encrypted with user password. | Cannot extract without user-supplied password. Write `status: "blocked"` and clear explanation to `/app/output/result.json`. |
| `valid_pdf_header: false` | File is HTML error page, corrupted download, or non-PDF format (e.g., `.docx`/`.txt`). | Inspect file type (`file` / first bytes); if Office document, parse natively or convert via `soffice --headless --convert-to pdf` before running pipeline. |
| Missing `fitz` (`PyMuPDF`) | Minimal container lacks `pymupdf`. | `pdf_probe.py` falls back to `pypdf`/`pdfinfo`; `pdf_extract.py` falls back to `pdftotext -bbox-layout` XML; `pdf_render_ocr.py` falls back to `pdftoppm`. |
| `pdf_extract.py` misses a borderless table | Table uses thin spacing without horizontal/vertical vector rules. | Use `pdf_render_ocr.py --crop` on the page region around the `Table X` caption and extract rows visually from the PNG. |
| Merged cells (`rowspan`/`colspan`) misaligned | Multi-level headers or vertically merged stubs shifted left. | Check `tables.json[*].html` and verify row/column alignment against a 250–300 DPI page/table crop before copying numbers. |
| Chinese/non-ASCII OCR garbled | `tesseract` missing `chi_sim` traineddata (`tesseract_langs` in `probe.json`). | Check `probe.json -> capabilities.tesseract_langs`; if `chi_sim` is absent, rely on `rapidocr` (if installed) or direct visual inspection of rendered PNGs. |
| `audit_result.py` warns `quote not matched verbatim` | Quote came from OCR, crossed a column/page break, or paraphrased text. | Replace with exact verbatim span from `reading_order.md` or mark `"source_type": "ocr"` / `"visual_png"` with exact page + PNG path. |
| Very large PDF (>50 pages) approaching time budget | Extracting/rendering all pages wastes budget. | Run `pdf_probe.py` (fast), locate target sections/pages via `pdf_extract.py`, and render/OCR only the relevant pages (`--pages`). |
