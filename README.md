# Scientific PDF Parser Skill (`scientific-pdf-parser`)

Submission for Bohrium Product Arena Challenge **"阅读01｜文献文档解析"** (`product-arena-08-3027d350`, `ARMI:2611.000030v14`).

## Overview

`scientific-pdf-parser` is a deterministic, evidence-traceable PDF parsing skill designed for academic and scientific literature reading under the Bohrium Harbor runtime contract. Rather than dumping unstructured text, it preserves document layout, multi-column reading order, section hierarchies, complex/cross-page tables, inline super/subscripts, Greek/math symbols, numbered equations, footnotes, and exact `(pdf_page, printed_page, block_id, bbox)` provenance.

## Key Capabilities

1. **Gutter-Aware XY-Cut Multi-Column Reading Order (`order_lines_xy_cut`)**:
   - Detects vertical column gutters via horizontal crossing-score optimization.
   - Separates full-width spanning bands (title, authors, abstract, wide figures/tables) from two-column bands so left columns always finish before right columns without column bleeding.
2. **Cross-Page Running Header/Footer Isolation (`strip_running_headers_footers`)**:
   - Statistically identifies repeated top/bottom margin text and standalone page numbers across pages, extracting `printed_page` labels while preventing running headers/footers from corrupting body paragraphs.
3. **Geometry-Aware Super/Subscripts & Equation Extraction (`build_lines_from_chars`)**:
   - Inspects character-level font sizes and baseline offsets to reconstruct inline `^{...}` superscripts and `_{...}` subscripts (e.g., `NiCo_{2}O_{4}`, `10^{-3}`), preserves Greek/math Unicode symbols, and indexes numbered display equations.
4. **Complex & Cross-Page Table Reconstruction (`extract_page_tables_pdfplumber`, `stitch_cross_page_tables`)**:
   - Uses lattice-first then stream fallback table extraction, attaches nearby captions, and stitches cross-page continued tables (`Table X (continued)` / `续表` or matching multi-column headers on consecutive pages) into unified Markdown and structured JSON grids with multi-page provenance.
5. **Adaptive Page Quality Triage & OCR Fallback**:
   - Classifies every page (`native_text`, `mixed_or_low_text`, `garbled_font`, `scanned_image`, `encrypted_or_error`), detects `(cid:...)` / `\ufffd` font-encoding failures, renders flagged pages to PNG (`pypdfium2` / `pdftoppm`), applies grayscale + autocontrast + sharpen preprocessing, and runs `tesseract` OCR with explicit confidence reporting.
6. **Harbor Output Contract & Provenance Verification (`scripts/validate_output.py`)**:
   - Verifies `case_id`, all `output_contract` required fields, absence of placeholder strings, and that every cited `pdf_page`, `block_id`, `table_id`, and `eq_id` resolves to a real parsed element in `document.json`.

## Package Structure

```text
submission/
├── SKILL.md                     # Agent-callable skill definition & Harbor workflow
├── README.md                    # Architecture, dependencies, usage, and limitations
├── GITHUB_URL.txt               # Public GitHub repository link (+5 bonus)
├── references/
│   └── output_schema.json       # Formal JSON schema for document.json
├── examples/
│   └── minimal_example.md       # Clearly labeled illustrative invocation & output
└── scripts/
    ├── parse_paper.py           # Core multi-backend scientific PDF parser
    ├── validate_output.py       # Harbor result.json contract & provenance validator
    └── self_check.py            # End-to-end synthetic verification test suite
```

## Dependencies & Environment Compatibility

Designed for the Bohrium Harbor container (`scientific-python`, `pdf-text-tool`, `pdf-render-tool`, `ocr-tool`):
- **Required**: Python 3.9+ standard library.
- **Recommended Python packages** (graceful fallback if any are absent):
  - `pdfplumber` (primary character-geometry & table backend)
  - `pypdf` (encryption check & fallback text backend)
  - `pypdfium2` (fast PDF-to-PNG rendering)
  - `Pillow` + `pytesseract` (image preprocessing & OCR confidence extraction)
  - `reportlab` (only required for running `scripts/self_check.py`)
- **Recommended CLI tools** (used automatically if Python wrappers are absent):
  - `pdftoppm`, `pdfinfo`, `tesseract`

## Quick Verification

```bash
python3 scripts/self_check.py
```

## Known Limitations & Honest Handling

- **Complex display math (multi-line matrices / nested fractions)**: Character-level geometry reliably preserves inline super/subscripts, Greek letters, and single-line numbered equations, but cannot reconstruct 2D LaTeX ASTs for complex nested matrices; such regions are preserved with page/bbox anchors and PNG previews for visual verification.
- **Image-only tables**: Rasterized tables without vector lines or text layers cannot be reliably segmented into exact cells by plain OCR; the parser renders page PNGs and flags them in `uncertainties` for visual inspection rather than fabricating cell values.
- **Password-protected PDFs**: Detected immediately via `check_pdf_encryption`; returns `status: "blocked_encrypted"` with clear manual takeover guidance.
