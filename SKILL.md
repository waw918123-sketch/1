---
name: literature-doc-parser
description: >-
  Evidence-grounded scientific PDF and literature document parsing skill for
  multi-column reading order reconstruction, complex/three-line table extraction,
  section hierarchy recovery, formula/symbol preservation, scanned-PDF OCR/visual
  verification, and page/block provenance auditing. Use whenever a task requires
  reading, parsing, extracting, translating, verifying, or structuring information
  from scientific PDFs or research documents under strict citation and
  output-contract requirements.
---

# Scientific Literature Document Parsing Skill

Parse real scientific PDFs into layout-ordered, structure-aware, page-and-block-grounded artifacts so every extracted claim, parameter, table cell, equation, or answer traces back to verifiable source evidence.

## Core Principles (Non-Negotiable)

1. **Read actual files, never assume from labels**: Always inspect `/input/case.json`, `task.md`, and the actual PDF bytes/pages. Never guess or fill missing content from titles, abstracts, or prior knowledge.
2. **Evidence-first grounding (`核得回`)**: Every non-trivial claim, parameter, or answer must cite exact provenance (`page` 1-based, `block_id` / `section` / `table_id` / `equation_id`, and verbatim `quote` or `bbox`).
3. **Honest uncertainty (`不臆测补齐`)**: For unreadable glyphs, low-confidence OCR, encrypted files, unreported metrics, or conflicting statements across sections/tables, explicitly report `unreported` / `indeterminate` / `conflict` with location and reason.
4. **Preserve layout and structure (`提得全、排得对、拆得清`)**: Never feed naive raw `pdftotext` output from multi-column papers directly into reasoning when column interleaving, header/footer pollution, or broken tables are present.
5. **Budget awareness**: Keep total work well within the Harbor execution budget (64 model calls, 900s wall time). Run deterministic Python extraction scripts first, then inspect only targeted pages/crops visually when needed.

## Skill Resource Discovery

At runtime in Harbor, this skill is typically installed at `/skills/weekly-skill` (or alongside your skill root). Locate `SKILL_DIR` in any POSIX `sh`/`bash` shell:

```sh
SKILL_DIR="/skills/weekly-skill"
for d in /skills/weekly-skill /app/submission .; do
  if [ -d "$d/scripts" ]; then
    SKILL_DIR="$d"
    break
  fi
done
```

All working files go under `/app/work/`; final deliverables go under `/app/output/` (including `/app/output/result.json`).

---

## Fast-Start 1-Command Pipeline (Recommended First Step)

Instead of manually chaining multiple scripts on turn 1, inspect `/input/case.json` + `/input/task.md` and run `run_pipeline.py` immediately:

```sh
mkdir -p /app/work /app/output
python3 "$SKILL_DIR/scripts/run_pipeline.py" --input-dir /input --work-dir /app/work
```

This single command automatically:
1. Discovers all PDFs and non-PDF materials under `/input/`.
2. Probes encryption, page types (`native_text`, `complex_hybrid`, `scanned_or_image_only`, `garbled_encoding`), and column structure (`probe.json`).
3. Reconstructs multi-column X-Y cut reading order, strips repeating headers/footers, classifies sections/captions/footnotes/equations/references, and extracts bordered, three-line, borderless, and cross-page continued tables (`document.json`, `reading_order.md`, `tables.md`, `outline.md`).
4. Runs targeted 250 DPI rendering + OCR on scanned/garbled pages and unextracted defect/table crops (`ocr/ocr_results.json`).
5. Writes `/app/work/case_brief.json` summarizing every input, contract field, and output path.

Then read `/app/work/parse/outline.md` (or `/app/work/doc_*/outline.md`) first—**never dump a huge `document.json` into context**—and use `query_doc.py` to pull exact blocks/tables with provenance:

```sh
python3 "$SKILL_DIR/scripts/query_doc.py" --doc /app/work/parse/document.json --ocr /app/work/parse/ocr/ocr_results.json --query "<keyword_or_regex>"
```

---

## Standard 5-Stage Workflow (Detailed Reference)

### Stage 1 — Contract & Input Boundary Inspection
1. Read `/input/case.json` and `/input/task.md` (or `task.md` in the input directory).
2. Extract:
   - `case_id` (must appear verbatim in `/app/output/result.json`)
   - `output_contract` (every required field name, type, and format)
   - Input PDF / document paths and specific questions or extraction targets
3. See [references/output-contract.md](./references/output-contract.md) for contract mapping and status semantics.

### Stage 2 — PDF Triage & Environment Probe
Run `pdf_probe.py` before full extraction:

```bash
mkdir -p /app/work/parse /app/output
python3 "$SKILL_DIR/scripts/pdf_probe.py" <input_pdf> --out /app/work/parse/probe.json
```

Inspect `probe.json`:
- **`needs_password: true` or `valid_pdf_header: false`**: Stop extraction, record `status: "blocked"` or `"partial"` with exact error in `result.json`, and do not fabricate text.
- **`estimated_columns >= 2` / `complex_hybrid`**: Multi-column X-Y cut and table extraction are required.
- **`scanned_pages` or `garbled_pages` non-empty**: Plan targeted rendering/OCR in Stage 4.

### Stage 3 — Layout-Ordered Text, Structure & Table Extraction
Run `pdf_extract.py` to reconstruct multi-column reading order, strip repeating headers/footers, separate sections/captions/footnotes/equations/references, and extract tables:

```bash
python3 "$SKILL_DIR/scripts/pdf_extract.py" <input_pdf> --out-dir /app/work/parse
```

Outputs in `/app/work/parse/`:
- `reading_order.md` — blocks in true reading order tagged with `[p{page}_b{idx}|col{col}|{role}|bbox=[...]]`
- `document.json` — full structured tree (`sections`, `tables`, `captions`, `footnotes`, `equations`, `references`, `headers_footers`, `unextracted_regions`, `blocks`)
- `tables.md` & `tables.json` — extracted tables in Markdown + HTML (`colspan`), with `continues_from`/`continued_on` cross-page links

Key rules when reading these outputs:
- Check `unextracted_regions` in `document.json`: these mark encoding defects (`\ufffd`, `cid:`, PUA) or unparsed horizontal-rule tables. **Never guess `[UNREADABLE_DEFECT]` text**; inspect via Stage 4.
- For complex merged headers, three-line tables, and cross-page continuations, consult [references/layout-and-tables.md](./references/layout-and-tables.md).

### Stage 4 — Targeted Rendering, OCR & Visual Verification
Only render pages that actually need visual/OCR inspection (`scanned_pages`, `garbled_pages`, dense equation blocks, or `unextracted_regions` from `document.json`):

```bash
python3 "$SKILL_DIR/scripts/pdf_render_ocr.py" <input_pdf> \
  --out-dir /app/work/ocr \
  --pages 1,4-5 \
  --dpi 250 \
  --ocr auto
```

For specific problematic table or formula bounding boxes (`page:x0,y0,x1,y1` from `document.json`):

```bash
python3 "$SKILL_DIR/scripts/pdf_render_ocr.py" <input_pdf> \
  --out-dir /app/work/crops \
  --crop "3:60,180,540,420" \
  --dpi 300 \
  --ocr auto
```

- Check `ocr_results.json` (`mean_confidence`, `ocr_engine_used`).
- If `ocr_engine_used` is `null` or `mean_confidence < 75`, inspect the generated PNGs directly (or mark low-confidence spans explicitly in `uncertainty`).
- See [references/formulas-and-ocr.md](./references/formulas-and-ocr.md) for handling subscripts/superscripts, Greek letters, and Symbol-font defects.

### Stage 5 — Synthesize Answer, Audit Evidence & Write `/app/output/result.json`
1. Answer every task question / populate every `output_contract` field strictly from verified blocks, tables, equations, or OCR/visual inspections.
2. Attach structured provenance (`page`, `block_id` / `table_id` / `equation_id`, `quote`) and explicit `status`, `sources`, `uncertainty`, and `change_records` (or the exact schema requested by `output_contract`).
3. If the task produces editable Office/slide/diagram outputs, render real PDF/PNG previews (e.g. via `soffice` + `pdftoppm`/`fitz`) and inspect them before finalizing.
4. Run the evidence & contract auditor before finishing (pass `--ocr` whenever scanned/OCR pages were cited):

```sh
python3 "$SKILL_DIR/scripts/audit_result.py" \
  --result /app/output/result.json \
  --case /input/case.json \
  --doc /app/work/parse/document.json \
  --ocr /app/work/parse/ocr/ocr_results.json \
  --strict
```

Fix any `case_id` mismatch, missing `output_contract` fields, out-of-range page numbers, or mismatched `quote` citations, then re-run `audit_result.py`.

## Task-Type Playbooks (QA / Translation / Structuring / Verification)

- **Scientific QA & Claim Verification**: Use `query_doc.py` + `reading_order.md` + `tables.md`; never rely solely on abstract/introduction if Experimental/Results or a table gives exact conditions. When a question asks for an unreported metric, explicitly write `"unreported"`/`null` and cite the checked sections/tables.
- **Faithful Academic Translation**: Translate from `reading_order.md` (never raw column-interleaved text) while preserving exact numbers, units, chemical formulas, `^{...}`/`_{...}` scripts, equation numbers, and section hierarchy.
- **Structured Extraction (Tables/Equations/Metadata)**: Cross-check `tables.json` (`continues_from` / `continued_on`, HTML `colspan`) with `captions`; for `unextracted_regions`, inspect the cropped PNG before writing cell values.
- **Material Conflict Detection**: If body prose and a table (or two papers in `/input`) disagree, report both values with separate `(page, block_id/table_id, quote)` provenance entries and set explicit conflict/uncertainty notes.

## Exception Handling Quick Reference

| Condition | Required Action |
|---|---|
| Encrypted or password-protected PDF | Do not bypass or guess; emit `status: "blocked"` with error details in `result.json`. |
| Scanned PDF / no text layer | Render pages at 250–300 DPI via `pdf_render_ocr.py`, run OCR and/or visual inspection, record confidence. |
| Garbled glyphs (`\ufffd`, `cid:`, PUA) | Keep `[UNREADABLE_DEFECT]`, crop bbox PNG at 300 DPI, verify visually or report unreadable span. |
| Missing/unreported paper value | Explicitly state `"unreported"` / `null` + cite the searched sections/tables; never extrapolate. |
| Conflict between text and table | Report both values with exact `(page, block_id/table_id, quote)` citations and flag the discrepancy. |
| Missing optional Python package | Scripts auto-fallback (`fitz` -> `pdfplumber` -> `pdftotext`/`pypdf`/`pdfinfo`); see [references/troubleshooting.md](./references/troubleshooting.md). |
