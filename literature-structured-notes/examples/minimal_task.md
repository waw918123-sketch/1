# Synthetic Minimal Task Example (Illustrative Only)

> Note: This is a synthetic example showing how `literature-structured-notes` is invoked and how its outputs are structured. It is NOT a formal evaluation case.

## Input
- `/input/synthetic_paper.pdf`

## Questions / Instructions
1. Produce a section-aligned summary preserving the paper's native section structure and inter-section transitions.
2. Build a comparable `paper_cards` entry retaining experimental preconditions and limitations.
3. Extract canonicalized `knowledge_points` with aliases and exact evidence anchors.

## Expected Invocation
```sh
python3 "$SKILL_DIR/scripts/prep_notes.py" --input-dir /input --work-dir /app/work --init-result /app/output/result.json
python3 "$SKILL_DIR/scripts/query_notes.py" --work-dir /app/work --conditions --limitations --terms
python3 "$SKILL_DIR/scripts/notes_audit.py" --result /app/output/result.json --merge /app/work/patch.json --case /input/case.json --work-dir /app/work --export-md /app/output/notes.md --strict
```
