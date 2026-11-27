# Troubleshooting & Edge-Case Handling Matrix (`troubleshooting.md`)

| Condition | Detection Signal | Required Action |
|---|---|---|
| **Encrypted or corrupt PDF** | `needs_password: true` or `valid_pdf_header: false` in `nanopub_brief.json` | Emit `status: "blocked"` in `/app/output/result.json`, record in `human_review_items`, never fabricate claims. |
| **Damaged p-value / inequality glyphs (`p ,0.03`)** | `stat_anomalies` non-empty in `nanopub_brief.json` / `outline.md` | Inspect targeted crop PNG / `ocr_results.json` (`prep_nanopub.py` renders up to 4 crops automatically); if OCR unavailable or `mean_confidence < 75`, flag in `human_review_items` and do not guess `<` vs `>`. |
| **Scanned / image-only PDF** | `scanned_pages` non-empty in `probe_summary` | Check `ocr_results.json`; if no OCR engine or `mean_confidence < 75`, cap assertion `confidence` at `"low"`/`"medium"` and log `human_review_items`. |
| **Two-column interleaving** | `multi_column_pages` in `probe_summary` | Read `section_chunks/` (ordered by recursive X-Y cut) rather than raw `pdftotext`. |
| **Large / multi-row tables** | `tables` index in `outline.md` | Never dump whole large tables into prompts; query one table or filtered row via `query_nanopub.py --table tbl_1 --row "<label>"` and bind exact `(table_id, row_label, col_header, value)`. |
| **Abstract vs Table / Cross-paper conflict** | Contradictory values or polarities | Keep both assertions with separate `evidence_anchors`, log in `conflicts[]` and `human_review_items[]`. |
| **Missing `pubinfo` fields** | `doi: "unreported"`, `year: "unreported"`, etc. | Keep `"unreported"` verbatim; never invent DOIs, authors, or licenses from memory. |
| **Tight Harbor budget (64 reqs / 900s)** | `chunk_count >= 5` or slow execution | Merge assertions incrementally after every 1–2 chunks via `nanopub_audit.py --merge` so valid `result.json` + `.trig` are always on disk. |
