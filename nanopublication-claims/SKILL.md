---
name: nanopublication-claims
description: >-
  Evidence-grounded Nanopublication extraction, atomic assertion decomposition,
  table-cell provenance binding, causal/confidence calibration, and W3C TriG/JSON-LD
  export skill. Splits compound claims into single testable assertions, binds
  minimal sufficient verbatim quotes and exact (table_id, row_label, col_header,
  value) cells, locks deterministic PubInfo metadata, flags statistical glyph
  anomalies and observational-vs-causal overreach, and exports machine-readable
  nanopublications.trig and nanopublications.jsonld alongside result.json.
---

# Nanopublication Atomic Claims & Provenance (`nanopublication-claims`)

Convert scientific papers (PDF, JATS XML, Markdown/TXT, DOCX/XLSX/PPTX; single or multi-document) into **atomic, minimally evidenced, fully traceable, machine-readable Nanopublications** under Harbor's execution budget (`64` requests / `900s` / `16,384` tokens per response).

## Core Principles (Non-Negotiable)

1. **Read actual files first**: Inspect `/input/case.json`, `/input/task.md`, and ingested document chunks. Never fabricate DOIs, authors, p-values, or missing parameters.
2. **拆得细 (One Independently Testable Relation)**: Split compound clauses (`"A increases B and reduces C"` -> two assertions) and multi-metric rows. Keep `assertion_text` concise (`<= 35` words), attach `conditions`, `statistics`, `polarity`, `study_design`, and map `predicate` to [references/predicate-vocab.json](./references/predicate-vocab.json).
3. **对得上 (Minimal Sufficient Evidence & Cell Binding)**: Bind the shortest contiguous supporting `quote` (`8–400` chars) with `(doc_id, page, printed_page, block_id)`. For table numbers, ALWAYS specify `(table_id, row_label, col_header, value)` so exact cell membership is verified.
4. **准且诚 (Causal Guard & Confidence Calibration)**: Never turn `p > 0.05` / *"no significant difference"* into *"identical"*; never use `causes` for observational/retrospective cohorts; never mark `confidence: "high"` for `indirect_inference`, `extrapolation`, or damaged/low-confidence OCR blocks.
5. **追得回 & 读得懂 (Locked PubInfo + TriG/JSON-LD Export)**: Let `prep_nanopub.py` lock deterministic `pubinfo` (`title`, `authors` excluding editors, `year`, `journal`, `doi`, `file_sha256`), and let `nanopub_audit.py --merge --strict` inject URIs/hashes and export `/app/output/nanopublications.trig` and `/app/output/nanopublications.jsonld`.

## Skill Resource Discovery

```sh
SKILL_DIR="/skills/weekly-skill"
for d in /skills/weekly-skill /app/submission .; do
  if [ -d "$d/scripts" ]; then
    SKILL_DIR="$d"
    break
  fi
done
```

---

## Standard 3-Stage Workflow (Budget-Conserving)

### Stage 1 — Prepare, Lock PubInfo & Initialize Baseline Result

Run `prep_nanopub.py` once upfront to ingest all inputs (`.pdf`, `.xml`/`.jats`, `.html`, `.md`/`.txt`, Office), detect damaged statistical glyphs (e.g., `p ,0.03`, `,8.7 million`), render targeted OCR/crops (`<= 4`), write compact `nanopub_brief.json` (`task_md_preview`, `section_tree`, `table_index`, `stat_anomalies`, `rendered_crops`, `doc_dir_name`), and lock baseline `/app/output/result.json`:

```sh
mkdir -p /app/work /app/output
python3 "$SKILL_DIR/scripts/prep_nanopub.py" \
  --input-dir /input \
  --work-dir /app/work \
  --init-result /app/output/result.json
```

Inspect `/app/work/nanopub_brief.json` first. If `task_md_truncated` is `true` (or to verify exact schemas), read `/input/task.md` and `/input/case.json` in full. For full block/section details, read `/app/work/<doc_dir_name>/outline.md` (note multi-doc folders are named `doc_1_<stem>`, `doc_2_<stem>`). If `status == "blocked"`, record the block in `/app/output/result.json` and stop. If any extracted `pubinfo` field is still `"unreported"`, you may supply the corrected field in `/app/work/patch.json`'s `pubinfo`.

### Stage 2 — Targeted Chunk, Cue & Table-Row Retrieval

Never dump entire `document.json` or expand huge tables into context. Read `section_chunks/` sequentially (prioritizing Abstract, Results, Conclusions, Tables, Methods) and query targeted cues or tables (`--doc-id doc_2` when `document_count > 1`, `--row` filters large tables):

```sh
python3 "$SKILL_DIR/scripts/query_nanopub.py" --work-dir /app/work --chunk chunk_001
python3 "$SKILL_DIR/scripts/query_nanopub.py" --work-dir /app/work --claim-cues --stats --methods-design
python3 "$SKILL_DIR/scripts/query_nanopub.py" --work-dir /app/work --table tbl_1 --row "Total"
```

When `stat_anomalies` appear (e.g., `p ,0.03` or `,8.7 million` from PDF Symbol/font mapping errors), inspect the rendered PNGs in `rendered_crops` or `ocr_results.json` rather than guessing; log unresolved ambiguities in `human_review_items[]`. For table-backed claims, bind `table_id`, `row_label` (include section subheader like `"Eukaryotes Total"` if rows repeat), `col_header` (e.g. `"Earth / Predicted"`), and exact `value`. Consult [references/nanopub-schema.md](./references/nanopub-schema.md) and [references/decomposition-and-evidence.md](./references/decomposition-and-evidence.md).

### Stage 3 — Early Patch Merge, Strict 5-Pillar Audit & TriG/JSON-LD Export

Save early and iterate: write `/app/work/patch.json` right after extracting Abstract + core Results/Tables (`"status": "completed"`, `"nanopublications"`, `"sources"`, `"conflicts"`, `"human_review_items"`, `"uncertainty"`, plus answers to ALL questions/fields requested by `/input/task.md` & `output_contract`). Omit repetitive `pubinfo`/`nanopub_uri`/`quote_sha256` inside each nanopub—`nanopub_audit.py` injects them deterministically—then append additional chunks using `"append_nanopubs": true`:

```sh
python3 "$SKILL_DIR/scripts/nanopub_audit.py" \
  --result /app/output/result.json \
  --merge /app/work/patch.json \
  --case /input/case.json \
  --work-dir /app/work \
  --strict
```

This verifies contract completeness, controlled predicates, causal/confidence discipline, verbatim quotes, exact table cell intersections, deduplication/conflicts, and exports `/app/output/nanopublications.trig`, `/app/output/nanopublications.jsonld`, and `/app/output/nanopubs_report.md`. See [references/troubleshooting.md](./references/troubleshooting.md) for edge cases.
