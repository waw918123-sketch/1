# Minimal Runnable Example (Illustrative Only)

> **Notice**: All inputs and outputs below are **synthetic illustrative examples** demonstrating how an agent invokes `scientific-pdf-parser`. They are **not** official Bohrium Harbor test case answers.

## 1. Illustrative Input (`/input/case.json`)

```json
{
  "case_id": "demo-paper-001",
  "pdf_path": "/input/synthetic_paper.pdf",
  "output_contract": {
    "required": ["case_id", "status", "title", "equations", "tables", "evidence", "uncertainties"]
  }
}
```

## 2. Invocation Steps

```bash
# Step 1: Parse the input PDF into structured JSON + provenance-tagged Markdown
python3 /skills/weekly-skill/scripts/parse_paper.py \
  --pdf "/input/synthetic_paper.pdf" \
  --out-dir "/app/work/parsed_demo" \
  --ocr-fallback auto \
  --render-previews auto

# Step 2: Validate /app/output/result.json against /input/case.json and document.json
python3 /skills/weekly-skill/scripts/validate_output.py \
  --case "/input/case.json" \
  --parsed "/app/work/parsed_demo/document.json" \
  --result "/app/output/result.json"
```

## 3. Illustrative Output (`/app/output/result.json` — Synthetic Example)

```json
{
  "case_id": "demo-paper-001",
  "status": "success",
  "title": "Structure-Preserving Extraction of Catalytic Kinetics",
  "equations": [
    {
      "eq_id": "eq_01",
      "label": "1",
      "text": "r = k * C_A^2 * exp(-E_a / RT) (1)",
      "pdf_page": 1,
      "block_id": "p1_b007"
    }
  ],
  "tables": [
    {
      "table_id": "p2_t01",
      "pages": [2, 3],
      "continuation_merged": true,
      "caption": "Table 1: Catalytic performance metrics across synthetic formulations.",
      "headers": ["Catalyst", "Temp (K)", "Rate (mM/s)", "Selectivity (%)"],
      "row_count": 4
    }
  ],
  "evidence": [
    {
      "claim": "Catalyst NiCo_{2}O_{4} yielded 10^{-3} mol/L.",
      "pdf_page": 1,
      "block_id": "p1_b006"
    }
  ],
  "uncertainties": []
}
```

## 4. Running the Built-in Self-Check

You can reproduce this entire synthetic test locally (including two-column ordering, header/footer stripping, super/subscripts, cross-page table stitching, scanned-page OCR fallback, encrypted PDF handling, and contract validation) by running:

```bash
python3 scripts/self_check.py
```
