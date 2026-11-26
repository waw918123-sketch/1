---
name: evidence-aligned-translation
description: "Evidence-aligned scientific literature translation workflow for Publisher XML (JATS/PLOS) and multi-column PDFs. Enforces independent raw-node census coverage (including supplementary tags), supplied glossary locking, verbatim inline markup (<sub>/<italic>/^{}/_{}) and panel labels (A2), batch chunking <=9,000 chars, incremental result checkpointing, and clause-level negation/numeric auditing."
---

# Evidence-Aligned Literature Translation (`literature-translation`)

Produces faithful, sentence- and locator-aligned bilingual scientific translations under Harbor `64`-request / `900s` / `16,384`-token budgets without dropping supplementary segments, breaking inline markup, drifting terminology, or losing progress on timeout.

Locate Skill scripts dynamically:

```sh
SKILL_DIR="$(cd "$(dirname "$0")" 2>/dev/null || printf '%s' '/skills/weekly-skill')"
[ -d "$SKILL_DIR/scripts" ] || SKILL_DIR="/skills/weekly-skill"
```

## Core Rules (Non-Negotiable)
1. **Value-Order Checkpointing**: Always initialize `/app/output/result.json` BEFORE translating (`--init-result`). Merge each translated batch immediately (`--merge`) so partial progress is permanently saved on disk.
2. **Independent Census Coverage**: Never omit `<supplementary-material>`, `S1–S14 Fig`, `S1 Text`, `S1–S4 Table`, `<fig>`, `<table-wrap>`, or captions inside requested scope. Coverage denominator comes strictly from raw document census.
3. **Locked Glossary Enforcement**: Lock one translation per term sense from `glossary.json` and preserve that exact term across titles, body, captions, tables, and conclusions (e.g. `credible interval` ≠ `confidence interval`).
4. **Verbatim Markup & Negation Polarity**: Keep `<sub>`, `<sup>`, `<italic>`, `<xref>`, `^{...}`, `_{...}`, numbers, units, and panel labels `(A2)`/`(B1)` verbatim. Never invert negation or contrastive scope (`not increase A, but reduce B` ≠ `增加 A，但不减少 B`).
5. **No Fabrication**: For scanned/garbled PDF regions without clear text, flag in `human_review_items` and `uncertainty`; never guess missing numbers or formulas.

## 4-Stage Execution Workflow

### Stage 1 — Prepare Census, Glossary, Compact Batches & Baseline `result.json`
```sh
mkdir -p /app/work /app/output
python3 "$SKILL_DIR/scripts/run_translate_prep.py" \
  --input-dir /input \
  --work-dir /app/work \
  --max-chars 9000 \
  --init-result /app/output/result.json
```

Inspect `/app/work/manifest.json` and `/app/work/glossary.json`:
- Check `scope_inferred` (`target_ids` / `pages`) and `census` (`independent_raw_in_scope_count`, `in_scope_sentence_count`).
- Review `locked_terms` and `context_disambiguations` (which capture Methods distinctions like `credible interval` vs `confidence interval`).
- If `status == "blocked"`, emit `status: "blocked"` honestly in `result.json` with exact cause.

### Stage 2 — Translate Compact Batches & Merge Incrementally
Read `/app/work/batches/batch_001.json`, `batch_002.json`, ... one batch at a time (each `<= 9,000` chars). For each batch, write a tiny `/app/work/patch_001.json` mapping `unit_id` and `sentence_id` to exact translations (preserving `<sub|sup|italic|xref>`, `^{...}`, `_{...}`, and `(A2)` panel tags):

```json
{
  "translations": {
    "P00001": "……",
    "P00001.S001": "……"
  },
  "human_review_items": []
}
```

Merge immediately after each batch (locks `case_id`, `locator`, `source_text`, and `census` against overwrite):

```sh
python3 "$SKILL_DIR/scripts/translate_audit.py" \
  --result /app/output/result.json \
  --merge /app/work/patch_001.json \
  --case /input/case.json \
  --manifest /app/work/manifest.json \
  --glossary /app/work/glossary.json
```

### Stage 3 — Final Strict Bilingual, Markup & Negation Audit
Run strict audit after the final batch (stdout capped `<= 24 KB`):

```sh
python3 "$SKILL_DIR/scripts/translate_audit.py" \
  --result /app/output/result.json \
  --case /input/case.json \
  --manifest /app/work/manifest.json \
  --glossary /app/work/glossary.json \
  --strict
```

Fix any reported missing units, glossary violations, inline tag mismatches, missing panel labels, or clause negation flips in a targeted patch, re-merge, and re-run `--strict` until `"passed": true`.

## References
- XML/PDF locator conventions & scope filtering: [references/xml-and-pdf-locators.md](./references/xml-and-pdf-locators.md)
- Glossary locking, inline markup & negation polarity checks: [references/glossary-and-fidelity.md](./references/glossary-and-fidelity.md)
- Fallbacks & edge-case troubleshooting: [references/troubleshooting.md](./references/troubleshooting.md)
