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
5. Writes `/app/work/case_brief.json` summarizing every input (including `.docx`/`.xlsx`/`.pptx`/`.csv`/`.txt` attachments), contract field, diagnostic gain metrics, and output path.

Then read `/app/work/case_brief.json` and `/app/work/parse/outline.md` (or `/app/work/doc_*/outline.md`) first—**never dump a huge `document.json` into context**—and use `query_doc.py` to pull exact blocks/tables with provenance:

```sh
python3 "$SKILL_DIR/scripts/query_doc.py" --doc /app/work/parse/document.json --ocr /app/work/parse/ocr/ocr_results.json --query "<keyword_or_regex>"
python3 "$SKILL_DIR/scripts/query_doc.py" --doc /app/work/parse/document.json --table tbl_1
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

```sh
mkdir -p /app/work/parse /app/output
python3 "$SKILL_DIR/scripts/run_pipeline.py" --input-dir /input --work-dir /app/work --init-result /app/output/result.json
```

This single call:
1. Discovers `/input` PDFs, `case.json`-referenced files, and non-PDF attachments (`.docx`/`.pptx`/`.xlsx`/text), saving `case_brief.json` (`<=24 KB` stdout).
2. Writes a non-empty deterministic baseline `/app/output/result.json` upfront (locking `case_id`, extracted `tables`, `metadata`, `sections`) so partial progress is safe under `64`-request / `900s` budgets.
3. Inspect `case_brief.json`: if `needs_password: true` or `valid_pdf_header: false`, emit `status: "blocked"` and do not fabricate text.

### Stage 3 — Targeted Navigation & Low-Budget Lookup
Read compact `/app/work/parse/outline.md` first (`metadata`, `sections`, `tables` with hierarchical `headers`, `merged_tables`, `figures`, `equations`, `page_label_map`, and `unextracted_regions`), then inspect ONLY needed evidence with `query_doc.py` (never dump giant `document.json` into context):

```sh
python3 "$SKILL_DIR/scripts/query_doc.py" --doc /app/work/parse/document.json --pages 1-3 --query "<term>" --figures --equations
python3 "$SKILL_DIR/scripts/query_doc.py" --doc /app/work/parse/document.json --table tbl_1
```

Key extraction rules:
- Multi-row headers (`header_rows >= 2`) use `headers` paths (e.g. `"True Relationship / Yes"`) and `html` `colspan`; captions embedded inside table grids are separated automatically.
- Paragraphs or captions ending mid-sentence across breaks carry `continues_across_break` (`|cont->...` in `reading_order.md`).
- If `page_numbering_discrepancy: true`, cite both 1-based physical PDF `page` and printed label (`page_label_map`).
- Consult [references/layout-and-tables.md](./references/layout-and-tables.md) for complex merged headers and continuations.

### Stage 4 — Targeted Crop/OCR Verification (Only When Needed)
Only inspect `unextracted_regions` (`encoding_defects_detected` / `image_based_table_near_caption`) or scanned pages (`--crop page:x0,y0,x1,y1` at 300 DPI):

```sh
python3 "$SKILL_DIR/scripts/pdf_render_ocr.py" <input_pdf> --out-dir /app/work/crops --crop "3:60,180,540,420" --dpi 300 --ocr auto
```

- Check `ocr_results.json` (`mean_confidence`); if `ocr_engine_used` is `null` or `mean_confidence < 75`, verify cropped PNGs visually or mark uncertainty explicitly. See [references/formulas-and-ocr.md](./references/formulas-and-ocr.md).

### Stage 5 — Write Compact Patch, Bind Provenance & Strict Audit
Do NOT manually re-transcribe deterministic `tables`/`sections`/`metadata` already written into `/app/output/result.json`. Write a small `/app/work/patch.json` containing only task answers (`status`, analytical fields, `(page, block_id/table_id, row_label, col_header, value, quote)` provenance, `uncertainty`, `change_records`), then merge and validate in one command:

```sh
python3 "$SKILL_DIR/scripts/audit_result.py" \
  --result /app/output/result.json \
  --merge /app/work/patch.json \
  --case /input/case.json \
  --doc /app/work/parse/document.json \
  --ocr /app/work/parse/ocr/ocr_results.json \
  --strict
```

## Task-Type & Exception Discipline (Zero Fabrication)
- **Strict Grounding**: When citing a table number (`value`), include `table_id` + `row_label` + `col_header` + shortest sufficient `quote` so `audit_result.py` verifies exact row/col binding (preventing adjacent-column swaps).
- **Unreported & Conflicts**: If a metric is absent (or supplementary material missing), set `"unreported"`/`null` and cite checked sections; if text and table (or two papers) disagree, preserve both with separate `(page, block_id/table_id, quote)` entries.
- **Fallbacks**: See [references/troubleshooting.md](./references/troubleshooting.md) (`fitz` -> `pdfplumber` -> `pypdfium2` -> `pdftotext`/`pypdf`/`pdfinfo`).
