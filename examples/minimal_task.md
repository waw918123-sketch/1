# Synthetic Minimal Task Example (Illustrative Only)

> Note: This is a synthetic example demonstrating how the skill is invoked and how outputs are structured. It is NOT a formal evaluation case.

## Input
- `/input/synthetic_paper.pdf` (a two-column PDF containing a three-line catalyst performance table and one equation).

## Questions
1. What calcination temperature (with unit) was used for Catalyst B according to Section 2?
2. Extract Table 1 (Catalyst Activity Comparison) and report the selectivity of Catalyst B.
3. Report whether the paper specifies the reactor inner diameter.

## Expected Invocation
```sh
python3 "$SKILL_DIR/scripts/run_pipeline.py" --input-dir /input --work-dir /app/work
python3 "$SKILL_DIR/scripts/query_doc.py" --doc /app/work/parse/document.json --query "Catalyst B"
python3 "$SKILL_DIR/scripts/audit_result.py" --result /app/output/result.json --case /input/case.json --doc /app/work/parse/document.json --strict
```
