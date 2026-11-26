# Output Contract & Evidence Grounding Specification

## 1. Runtime Paths in Harbor

- **Skill installation path**: `/skills/weekly-skill/`
- **Inputs**:
  - `/input/case.json` — machine-readable case descriptor (`case_id`, input material paths, `output_contract`, constraints)
  - `/input/task.md` (or `task.md`) — human-readable task instructions and questions
- **Working directory**: `/app/work/` — store intermediate `probe.json`, `document.json`, `reading_order.md`, `tables.json`, rendered PNGs, and `ocr_results.json` here.
- **Final output directory**: `/app/output/` — write `/app/output/result.json` and any requested editable deliverables (plus rendered PDF/PNG previews if Office/slide/diagram files are produced).

## 2. Mandatory Fields in `/app/output/result.json`

Always preserve:
1. `case_id`: exact string from `/input/case.json`.
2. **Every field defined in `output_contract`** from `/input/case.json` (do not rename, drop, or nest them unexpectedly).
3. Honest operational metadata (either inside the contracted fields or as top-level fields if allowed by contract):
   - `status`: `"completed"` | `"partial"` | `"blocked"` | `"indeterminate"`
   - `sources`: list of input file paths and page ranges actually inspected
   - `uncertainty`: explicit list of unreadable glyphs, low-confidence OCR pages, missing values, or ambiguities
   - `change_records`: brief audit trail of extraction fallbacks or corrections applied

## 3. Evidence Citation Schema

For every extracted parameter, factual answer, table value, or verification verdict, attach an evidence record using exact coordinates from `document.json` / `reading_order.md`:

```json
{
  "page": 3,
  "block_id": "p3_b7",
  "section": "2.2 Experimental Setup",
  "table_id": null,
  "bbox": [72.0, 214.5, 290.1, 288.0],
  "quote": "The reaction temperature was maintained at 353 K for 4 h."
}
```

Rules for `quote`:
- Must be copied verbatim (or whitespace-normalized) from the cited `page`.
- Never synthesize a quote from memory or combine sentences from different pages into one fake quote.
- If evidence comes from a visual/OCR inspection of a scanned page, record `"source_type": "ocr"` or `"visual_png"` along with the PNG filename and OCR confidence.

## 4. Verification Before Submission

Run:
```bash
python3 "$SKILL_DIR/scripts/audit_result.py" \
  --result /app/output/result.json \
  --case /input/case.json \
  --doc /app/work/parse/document.json \
  --strict
```

Do not consider the task complete until `audit_result.py` reports `"passed": true` with zero unresolved `errors`.
