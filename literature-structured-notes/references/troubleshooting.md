# Troubleshooting & Edge-Case Handling Matrix (`troubleshooting.md`)

| Condition | Detection Signal | Required Action |
|---|---|---|
| **Encrypted or invalid PDF** | `needs_password: true` or `valid_pdf_header: false` in `notes_brief.json` | Emit `status: "blocked"` in `result.json`, log in `human_review_items`, and never guess contents. |
| **Scanned / image-only PDF** | `scanned_pages` non-empty in `probe_summary` | Inspect `ocr_results.json`. If `ocr_engine_used` is `null` or `mean_confidence < 75`, mark affected notes `human_review_required` and note in `uncertainty`. |
| **Garbled font / CID tokens** | `unextracted_regions` with `encoding_defects_detected` | Do not infer unreadable symbols; inspect rendered crops or log in `human_review_items`. |
| **Two-column interleaving** | `multi_column_pages` in `probe_summary` | Trust `doc_ingest.py` / `section_chunks/` (X-Y cut ordered) rather than raw `pdftotext`. |
| **Missing figures / formulas** | Empty `equations` or `image_based_table_near_caption` | Flag missing visual/formula layer in `uncertainty` / `human_review_items`; never invent equation parameters. |
| **Ultra-long paper (>20 pages)** | `chunk_count >= 5` in `notes_brief.json` | Iterate `section_chunks/chunk_001.json` ... incrementally and merge via `notes_audit.py --merge` after each chunk/section batch to preserve progress under the 64-request / 900s budget. |
| **Non-Chinese / multilingual paper** | English, bilingual, or mixed-script manuscripts | Preserve original verbatim `quote` in `evidence_anchors` while writing structured summaries in the language requested by `task.md` / `output_contract`. |
| **Missing `pymupdf` in host** | `backend: pdftotext_bbox_layout` or `pypdf` | Pipeline degrades automatically (`pymupdf -> pdftotext -> pypdf`); report reduced layout/table precision in `change_records`. |
