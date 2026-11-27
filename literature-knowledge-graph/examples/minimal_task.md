# Synthetic Minimal Task Example (Illustrative Only)

> Note: This is a synthetic example demonstrating how `literature-knowledge-graph` works on a multi-paper JSON+XML corpus. It is NOT a formal evaluation case.

## Input
- `examples/sample_kg_corpus/corpus-manifest.json`
- `examples/sample_kg_corpus/primary-material.json`
- `examples/sample_kg_corpus/secondary-material.json`
- Optional runtime companion XML illustrated in `examples/companion-xml-example.md`

## Task Instructions
1. Keep independent `source_id`, DOI, authors, and license for each paper (never leak root DOI/title/authors to `secondary`; never treat companion XML formats as a 3rd paper).
2. Cite `(source_id, paragraph_id, quote)` with `page: null` (`has_pdf_pages: false`) and cross-check linearized subscripts/superscripts against companion JATS XML when supplied in `/input/`.
3. Extract entities, directed condition-bound relations, alignment vs. non-merge records, and execute at least 3 graph queries returning subgraphs, evidence, and indeterminate links.

## Expected Invocation
```sh
python3 "$SKILL_DIR/scripts/prep_kg.py" --input-dir examples/sample_kg_corpus --work-dir /tmp/kg_work --init-result /tmp/kg_out/result.json
python3 "$SKILL_DIR/scripts/query_kg.py" --work-dir /tmp/kg_work --cues --formula-diffs
python3 "$SKILL_DIR/scripts/kg_audit.py" --result /tmp/kg_out/result.json --merge /tmp/kg_work/patch.json --work-dir /tmp/kg_work --strict
```
