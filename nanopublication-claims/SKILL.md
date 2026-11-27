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

Run `prep_nanopub.py` once upfront to ingest all inputs, detect damaged statistical glyphs (e.g., `p ,0.03`), run targeted OCR/crops (`<= 4` pages/crops), write compact `nanopub_brief.json` (`<= 16 KB`), and lock baseline `/app/output/result.json`:

```sh
mkdir -p /app/work /app/output
python3 "$SKILL_DIR/scripts/prep_nanopub.py" \
  --input-dir /input \
  --work-dir /app/work \
  --init-result /app/output/result.json
```

Inspect `/app/work/nanopub_brief.json` and `/app/work/doc_1/outline.md`. If `status == "blocked"` (encrypted/corrupt input), record the block honestly in `/app/output/result.json` and stop.

### Stage 2 — Targeted Chunk, Cue & Table-Row Retrieval

Never dump entire `document.json` or expand huge tables into context. Read `section_chunks/` sequentially and query targeted cues or individual tables (`--row` filters large tables):

```sh
python3 "$SKILL_DIR/scripts/query_nanopub.py" --work-dir /app/work --chunk chunk_001
python3 "$SKILL_DIR/scripts/query_nanopub.py" --work-dir /app/work --claim-cues --stats --methods-design
python3 "$SKILL_DIR/scripts/query_nanopub.py" --work-dir /app/work --table tbl_1 --row "Total"
```

Consult [references/nanopub-schema.md](./references/nanopub-schema.md) for required fields and [references/decomposition-and-evidence.md](./references/decomposition-and-evidence.md) for splitting and causal rules.

### Stage 3 — Compact Patch Merge, Strict 5-Pillar Audit & TriG/JSON-LD Export

Write `/app/work/patch.json` containing `status`, `nanopublications` (plus any `output_contract` fields, `conflicts`, `human_review_items`, `uncertainty`). Omit repetitive `pubinfo`/`nanopub_uri`/`quote_sha256` fields—`nanopub_audit.py` injects them deterministically from locked `document.json`—and merge/audit/export in one command (for long papers, merge incrementally after every 1–2 chunks with `"append_nanopubs": true`):

```sh
python3 "$SKILL_DIR/scripts/nanopub_audit.py" \
  --result /app/output/result.json \
  --merge /app/work/patch.json \
  --case /input/case.json \
  --work-dir /app/work \
  --strict
```

This verifies contract completeness, controlled predicates, causal/confidence discipline, verbatim quotes, exact table cell intersections, deduplication/conflicts, and exports `/app/output/nanopublications.trig`, `/app/output/nanopublications.jsonld`, and `/app/output/nanopubs_report.md`. See [references/troubleshooting.md](./references/troubleshooting.md) for edge cases.
