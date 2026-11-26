# Formula Preservation, Encoding Defects & Scanned PDF OCR

## 1. Subscripts, Superscripts, and Special Symbols

Standard plain-text extraction flattens `10^{-5}`, `CO_2`, `R^2`, or `α_i` into `10-5`, `CO2`, `R2`, `αi`.
`pdf_extract.py` inspects span-level font size, PyMuPDF superscript flags (`flags & 1`), and baseline `y`-center offsets relative to the line's dominant baseline:
- Superscripts are emitted as `^{...}` in `text` (`reading_order.md` and `equations[*].script_text`), while `raw_text` preserves the unannotated string for verbatim matching.
- Subscripts are emitted as `_{...}`.
- Common typographic ligatures (`ﬁ, ﬂ, ﬀ, ﬃ, ﬄ`) and soft hyphens (`\u00ad`) are normalized without altering Greek letters (`α–ω, Α–Ω`) or mathematical operators.

## 2. Font Encoding Defects (Never Guess!)

Some scientific PDFs—especially older journals or improperly subsetted Type-3 / Symbol fonts—emit:
- Replacement characters (`\ufffd`)
- `(cid:NNN)` tokens
- Private Use Area glyphs (`U+E000–U+F8FF`)

`pdf_probe.py` and `pdf_extract.py` quantify `defect_rate` per page and per block. Whenever a block has defects:
1. It is flagged in `document.json -> unextracted_regions[]` and marked `[UNREADABLE_DEFECT: ...]` in `reading_order.md`.
2. **Never infer or hallucinate missing numbers, units, or Greek symbols from context.**
3. Crop the exact `bbox` at 300 DPI using `pdf_render_ocr.py` and verify visually from the PNG. If the PNG itself is illegible, report the field as unreadable/indeterminate.

## 3. Scanned PDF & OCR Strategy

When `pdf_probe.py` marks pages as `scanned_or_image_only` or `garbled_encoding`:
1. Render target pages at `250–300 DPI`:
   ```bash
   python3 "$SKILL_DIR/scripts/pdf_render_ocr.py" <pdf> --out-dir /app/work/ocr --pages <list> --dpi 250 --ocr auto
   ```
2. `pdf_render_ocr.py` automatically applies grayscale + autocontrast + sharpen preprocessing (when Pillow is available) before invoking `rapidocr_onnxruntime` or `tesseract` (`eng+chi_sim` when both language packs exist, falling back to `eng`).
3. Check `ocr_results.json`:
   - `mean_confidence >= 85`: generally reliable for prose; still spot-check numbers, units, and chemical/math formulas against the PNG.
   - `mean_confidence < 85` or `ocr_engine_used: null`: inspect the rendered PNG directly with visual reading and document any uncertain characters in `uncertainty`.
