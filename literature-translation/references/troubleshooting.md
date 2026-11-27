# Troubleshooting & Exception Handling

| Condition | Automated Action |
|---|---|
| Encrypted or password-protected PDF | `xml_pdf_extract.py` returns `status: "encrypted_blocked"`; emit `status: "blocked"` in `result.json`. |
| Scanned or low-text PDF pages | `run_translate_prep.py` flags `scanned_pages` in `uncertainty`; list affected units in `human_review_items`. |
| Missing `PyMuPDF` (`fitz`) | Automatically falls back to `pdftotext` CLI for PDF text extraction; XML mode uses Python stdlib only. |
| Output exceeds context budget | `run_translate_prep.py` caps stdout at `<= 8,000` chars, incremental `--merge` returns a compact checkpoint, and `--strict` caps at `<= 24,000` chars; full files stay in `/app/work/`. |
| Negation or glossary audit failure | Inspect exact `unit_id` / `sentence_id` in `unit_issues`, write targeted `patch_fix.json`, run `translate_audit.py --merge patch_fix.json --strict`. |
