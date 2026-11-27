---
name: literature-structured-notes
description: >-
  Evidence-grounded scientific literature summarization and structured note-taking
  skill. Preserves the paper's native section skeleton and inter-section transitions
  (对得上), retains research questions, data, experimental preconditions, and
  limitations (留得住), links every note and knowledge point back to exact page,
  block, table-cell, and verbatim-quote anchors (找得回), and produces searchable,
  cross-paper comparable Paper Cards, canonicalized Knowledge Points, conflict
  flags, and reusable Markdown notes (用得上).
---

# Literature Summary & Structured Notes (`literature-structured-notes`)

Turn real scientific papers (single or multi-paper; PDF, JATS XML, Markdown/TXT, DOCX/XLSX/PPTX) into **section-aligned, condition-preserving, anchor-linked, reusable structured notes** under strict Harbor execution budgets (`64` requests / `900s` / `16,384` tokens per response).

## Core Principles (Non-Negotiable)

1. **Read actual files, never assume from labels**: Always inspect `/input/case.json`, `/input/task.md`, and the actual ingested document chunks. Never fabricate missing numbers, assumptions, or citations.
2. **对得上 (Section & Argument Alignment)**: Follow the paper's own section hierarchy (`section_id`, `order_index`) and record inter-section logical transitions (`transition_from_prev`) instead of forcing a generic one-size-fits-all summary template.
3. **留得住 (Precondition & Limitation Retention)**: Never strip conditions that make a claim valid. Record temperature/pressure/sample-size/assumptions/boundary conditions (`preconditions`) and explicit `limitations`; mark absent items `"unreported"`.
4. **找得回 (Fine-Grained Verbatim Anchors)**: Every note, paper-card claim, and knowledge point must cite `(doc_id, page, printed_page, section, block_id / table_id+row_label+col_header, quote)`.
5. **用得上 (Reusable, Normalized & Audited)**: Deduplicate abbreviations/aliases into `canonical_term`, surface cross-section/table `conflicts`, distinguish `"deterministic"` vs `"human_review_required"` boundaries, and export reusable Markdown notes alongside `/app/output/result.json`.

## Skill Resource Discovery

Locate `SKILL_DIR` dynamically in any POSIX shell:

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

## Standard 4-Stage Execution Workflow

### Stage 1 — One-Command Preparation, Chunking & Baseline Checkpointing

Run `prep_notes.py` immediately to ingest all papers under `/input`, build the hierarchical section tree, generate section-aligned chunks (`<= 8,500` chars), extract abbreviation/precondition/limitation cues, run targeted OCR on scanned/garbled pages if needed, and initialize `/app/output/result.json`:

```sh
mkdir -p /app/work /app/output
python3 "$SKILL_DIR/scripts/prep_notes.py" \
  --input-dir /input \
  --work-dir /app/work \
  --init-result /app/output/result.json
```

Inspect `/app/work/notes_brief.json` and `/app/work/doc_1/outline.md` (plus `doc_2/outline.md` in multi-paper cases). If `status == "blocked"` (encrypted/corrupted file), stop and document the block honestly in `/app/output/result.json` without fabricating text.

### Stage 2 — Section-by-Section Reading & Cue Retrieval

Read section-aligned chunks sequentially (never dump giant `document.json` into context) and query targeted cues via `query_notes.py`:

```sh
python3 "$SKILL_DIR/scripts/query_notes.py" --work-dir /app/work --chunk chunk_001
python3 "$SKILL_DIR/scripts/query_notes.py" --work-dir /app/work --conditions --limitations --terms
python3 "$SKILL_DIR/scripts/query_notes.py" --work-dir /app/work --table tbl_1
```

Consult [references/note-schemas.md](./references/note-schemas.md) for the exact field dictionary (`paper_cards`, `section_notes`, `knowledge_points`, `conflicts`, `human_review_items`) and [references/section-and-evidence.md](./references/section-and-evidence.md) for alignment and anchoring rules.

### Stage 3 — Incremental Merge, Strict 4-Pillar Audit & Markdown Export

Write a compact `/app/work/patch.json` containing the task's required `output_contract` fields plus structured `paper_cards`, `section_notes`, `knowledge_points`, `conflicts`, and `human_review_items`. Merge, audit strictly, and export reusable Markdown notes in one command:

```sh
python3 "$SKILL_DIR/scripts/notes_audit.py" \
  --result /app/output/result.json \
  --merge /app/work/patch.json \
  --case /input/case.json \
  --work-dir /app/work \
  --export-md /app/output/notes.md \
  --strict
```

The strict audit enforces:
- `case_id` & `output_contract` field completeness;
- Section order monotonicity against `document.json` (`对得上`);
- Required `method_and_conditions` / `preconditions` / `limitations` coverage (`留得住`);
- Exact page, `block_id`, `(table_id, row_label, col_header, value)`, and verbatim `quote` verification (`找得回`);
- Canonical-term duplicate detection and undeclared numeric conflict detection (`用得上`).

### Stage 4 — Exception & Human-Review Discipline

Consult [references/troubleshooting.md](./references/troubleshooting.md) for:
- Scanned PDFs or missing OCR (`human_review_required`, never guess unreadable glyphs);
- Encrypted or invalid PDFs (`status: "blocked"`);
- Multi-column interleaving & printed-vs-physical page discrepancies (`page_label_map`);
- Ultra-long documents (>20 pages -> iterate `section_chunks/` sequentially);
- Non-Chinese / multilingual papers & cross-paper terminology alignment.
