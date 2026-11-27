# Synthetic Minimal Task Example (Illustrative Only)

> Note: This is a synthetic example demonstrating how `nanopublication-claims` works. It is NOT a formal evaluation case.

## Input
- `/input/synthetic_study.pdf`

## Task Instructions
1. Extract atomic, independently verifiable Nanopublications (`subject - predicate - object`, conditions, statistics, study design).
2. Bind minimal sufficient verbatim quotes or exact `(table_id, row_label, col_header, value)` table cells.
3. Preserve verified `pubinfo`, document any observational-vs-causal limits or discrepancies, and export valid W3C TriG (`nanopublications.trig`) and JSON-LD (`nanopublications.jsonld`).

## Expected Invocation
```sh
python3 "$SKILL_DIR/scripts/prep_nanopub.py" --input-dir /input --work-dir /app/work --init-result /app/output/result.json
python3 "$SKILL_DIR/scripts/query_nanopub.py" --work-dir /app/work --claim-cues --stats --methods-design
python3 "$SKILL_DIR/scripts/nanopub_audit.py" --result /app/output/result.json --merge /app/work/patch.json --case /input/case.json --work-dir /app/work --strict
```
