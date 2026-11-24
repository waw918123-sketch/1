---
name: scientific-pdf-parser
description: >
  Structure-preserving, evidence-traceable scientific PDF parsing skill for
  academic literature reading, extraction, verification, and Harbor benchmark
  evaluation. Handles multi-column reading order, running header/footer
  removal, section hierarchy, complex and cross-page tables, inline
  super/subscripts, Greek/math symbols, numbered equations, footnotes,
  references, and adaptive OCR fallback for scanned or garbled-font PDFs,
  attaching exact page and block provenance to every extracted element.
---

# Scientific PDF Parser (`scientific-pdf-parser`)

## 1. Purpose & Scope

Use this skill whenever an agent must read, parse, extract structured content from, verify facts in, or answer evidence-grounded questions about scientific or academic PDF documents — especially under the Bohrium Harbor runtime contract (`/input/case.json`, `task.md`, `/app/work`, `/app/output/result.json`).

**Core guarantees (提得全 · 排得对 · 拆得清 · 核得回):**
- **Complete extraction (提得全)**: Captures body text, titles, authors, abstracts, figure/table captions, footnotes, and references while isolating running headers, footers, and page numbers from body prose.
- **Faithful reading order (排得对)**: Uses gutter-aware XY-cut column segmentation to separate full-width spanning bands (titles, abstracts, wide figures/tables) from multi-column regions without column bleeding or cross-column paragraph merging.
- **Structured decomposition (拆得清)**: Reconstructs hierarchical sections, complex and cross-page continued tables (Markdown + structured cell grids), numbered equations, and character-geometry superscripts/subscripts/Greek symbols.
- **Verifiable provenance (核得回)**: Every extracted block, table, caption, footnote, and equation carries a deterministic anchor (`block_id`, `pdf_page`, `printed_page`, `bbox`, `method`). Unreadable, encrypted, or low-confidence regions are explicitly flagged as `uncertain` or `unreadable` — never guessed.

---

## 2. Harbor Runtime Contract & Directory Layout

When running inside the Bohrium Harbor evaluation harness:
- **Inputs**: Read `/input/case.json` and `task.md` (or the input directory specified by the task) first to identify `case_id`, target PDF path(s), exact questions/extraction tasks, and `output_contract`.
- **Working directory**: Store intermediate parsed JSON, Markdown, rendered page previews, and logs under `/app/work/`.
- **Final outputs**: Write final deliverables to `/app/output/`, **always** including `/app/output/result.json` that preserves `case_id` and every field required by `output_contract`.
- **Visual previews**: If any page is scanned, has garbled fonts, or contains image-only tables/figures, render cropped or full-page PNG previews to `/app/output/previews/` so reviewers (or vision-capable agents) can verify them directly.
- **No fabricated outputs**: Never substitute pre-written answers. Every value in `result.json` must come from running the parser and inspecting the actual input PDF(s).

---

## 3. Standard Execution Workflow

Follow these five steps in order for every case:

### Step 1 — Inspect Task Boundaries & Input Contract
1. Read `/input/case.json` and `task.md`.
2. Record:
   - `case_id`
   - All input PDF file paths
   - Required `output_contract` schema/fields
   - Specific user questions, target sections, tables, equations, or verification criteria.
3. Confirm all referenced PDF files exist and are readable. If a file is missing or corrupted, record `status: "failed_missing_input"` or `"failed_corrupted_pdf"` honestly in `result.json` and stop.

### Step 2 — Run the Deterministic Scientific PDF Parser
Execute `scripts/parse_paper.py` on each input PDF (located relative to this skill directory, e.g., `/skills/weekly-skill/scripts/parse_paper.py`):

```bash
python3 /skills/weekly-skill/scripts/parse_paper.py \
  --pdf "<path/to/paper.pdf>" \
  --out-dir "/app/work/parsed_<doc_id>" \
  --ocr-fallback auto \
  --render-previews auto
```

Key outputs generated in `--out-dir`:
- `document.json`: Full structured parse containing:
  - `metadata`: page count, encryption status, parser backend used, per-page quality diagnostics (`native_text`, `garbled_font`, `scanned_image`, `mixed_or_low_text`, `encrypted_or_error`), printed page labels.
  - `front_matter`: title, authors/affiliations (when detected), abstract, keywords.
  - `sections`: hierarchical section tree with heading level, numbering, page range, and ordered `block_ids`.
  - `blocks`: reading-order text blocks with `block_id` (`p1_b001`), `pdf_page`, `printed_page`, `column_index`, `bbox`, `role` (`title`, `heading`, `paragraph`, `caption`, `footnote`, `equation`, `reference`), `text`, `rich_text` (preserving `^{...}` / `_{...}`), `method`, and `warnings`.
  - `tables`: extracted tables with `table_id`, `pages` (including stitched cross-page continuations), `caption`, `headers`, `rows`, `markdown`, `bbox`, and `continuation_merged` flag.
  - `equations`: numbered display equations with `eq_id`, `label`, `text`, `pdf_page`, `bbox`.
  - `figures`: detected figure captions and page/bbox anchors.
  - `headers_footers`: stripped running headers/footers kept separately for auditability.
  - `uncertainties`: explicit list of garbled glyphs, low-confidence OCR pages, image-only tables, or unreadable spans.
- `document.md`: Clean reading-order Markdown with inline provenance tags (`[p.3 | p3_b012]`) and rendered Markdown tables.
- `previews/`: PNG renders of any scanned, garbled, or flagged pages (copy relevant previews to `/app/output/previews/` if needed for final review).

### Step 3 — Inspect Diagnostics & Resolve Edge Cases
Before answering or structuring final outputs, inspect `document.json -> metadata.page_diagnostics` and `uncertainties`:
- **Encrypted PDF (`status == "blocked_encrypted"`)**: Do not guess content. Report the encryption status, exact file path, and manual takeover requirement in `result.json`.
- **Garbled font / CID glyphs (`garbled_font`)**: Check if `--ocr-fallback auto` already ran OCR on that page. If OCR confidence is low or math symbols remain degraded, inspect the rendered PNG in `previews/` and explicitly mark affected spans in `uncertainties`.
- **Scanned pages (`scanned_image`)**: Verify OCR output in `blocks` (`method: "ocr_fallback"`). Preserve OCR confidence warnings rather than silently "correcting" ambiguous technical terms or numbers.
- **Image-only tables or complex multi-span tables**: Check `tables` against page text and rendered previews. If a table is embedded as a raster image and OCR cannot reliably recover cell boundaries, flag it as `image_only_table_requires_visual_check` with its page and bbox, and save the page PNG to `/app/output/previews/`.
- **Cross-page tables**: Confirm stitched tables (`continuation_merged: true`) kept column alignment intact across page boundaries.

### Step 4 — Fulfill Task Requirements with Verifiable Evidence
Using `document.json` and `document.md`:
1. Answer every question or populate every required structured field in `output_contract`.
2. Attach exact provenance to every key claim, extracted value, table, or equation:
   - `pdf_page` (1-indexed physical page)
   - `printed_page` (journal/paper page number if different)
   - `block_id` / `table_id` / `eq_id`
   - `quote` (short verbatim excerpt from the parsed PDF supporting the claim)
3. Explicitly separate:
   - **Verified findings** (backed by exact text/table/equation evidence)
   - **Not reported in source** (`"not_reported"` — explicitly state that the paper does not report the value after checking relevant sections)
   - **Uncertain / unreadable** (`"uncertain"` — e.g., low-DPI scan, garbled font, conflicting numbers across text vs. table, with both conflicting locations cited).

### Step 5 — Validate `/app/output/result.json` Before Finishing
Run the contract & provenance validator:

```bash
python3 /skills/weekly-skill/scripts/validate_output.py \
  --case "/input/case.json" \
  --parsed "/app/work/parsed_<doc_id>/document.json" \
  --result "/app/output/result.json"
```

Fix any schema, missing `case_id`, invalid page/block reference, or placeholder issues reported by `validate_output.py` before finishing.

---

## 4. Output Status & Honesty Protocol

Always set a truthful top-level `status` in `/app/output/result.json` (matching `output_contract` if it defines specific enum values):
- `"success"`: All required elements extracted and verified with provenance.
- `"partial"`: Core document parsed, but specific items are unreadable, low-confidence OCR, image-only tables, or missing in the source (enumerate each in `uncertainties` with manual takeover pointers).
- `"unable_to_determine"`: Document parsed cleanly, but the requested information is not reported or is contradictory across sections (cite conflicting locations).
- `"failed"` / `"blocked_encrypted"`: File missing, corrupted, or password-protected; state exact reason and what human input is needed.

**Strict anti-hallucination rules:**
1. Never invent missing numbers, table cells, formula symbols, or citations.
2. Never merge running headers/footers into paragraphs.
3. Never cite a page number or block ID that does not exist in `document.json`.
4. Never present illustrative examples as real case outputs.

---

## 5. Dependencies & Environment Fallback Order

`scripts/parse_paper.py` uses only Python 3 standard library plus pre-installed scientific/PDF tools, automatically selecting the best available backend:
1. **Primary layout & table engine**: `pdfplumber` (character-level coordinates, super/subscript geometry, lattice+stream table extraction)
2. **Secondary / fallback text engines**: `pymupdf` (`fitz`), `pypdf`, or `pdftotext` CLI
3. **PDF-to-image rendering (for scanned/garbled pages)**: `pypdfium2` or `pdftoppm` CLI
4. **OCR engine (adaptive fallback)**: `pytesseract` or `tesseract` CLI (with `PIL` / `numpy` image preprocessing when available)

See `README.md` for full architecture details, `references/output_schema.json` for exact JSON schemas, and `examples/minimal_example.md` for a minimal invocation walkthrough.
