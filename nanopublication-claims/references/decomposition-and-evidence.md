# Atomic Decomposition, Evidence Grounding & Causal Calibration (`decomposition-and-evidence.md`)

## 1. 拆得细 — Atomic Assertion Decomposition Rules
1. **One independently testable relation per Nanopublication**:
   - Split coordinated verbs/clauses: `"A increases B and reduces C"` -> two assertions (`A increases B`, `A decreases C`).
   - Split multi-metric rows: `"Table 2 reports Earth=8.74e6 and Ocean=2.21e6"` -> two assertions (one bound to `Earth` row, one bound to `Ocean` row).
   - Do NOT dump a paragraph summary into `assertion_text` or `object`.
2. **Use the Controlled Predicate Vocabulary** (`references/predicate-vocab.json`):
   - Prefer `increases`, `decreases`, `correlates_with`, `differs_from`, `no_significant_difference`, `measured_as`, `predicts`, `derived_from`, `causes`, `inhibits`, `activates`.
   - Avoid vague predicates (`relates_to`, `involves`, `shows`, `affects`).
3. **Preserve Conditions, Scope & Statistics**:
   - Never strip `N`, species, temperature, pressure, concentration, follow-up window, or statistical dispersion (`± SE`, `95% CI`, `p = ...`).

## 2. 对得上 — Minimal Sufficient Evidence & Exact Table Cell Grounding
1. **Minimal Sufficient Quote**:
   - Quote the shortest contiguous clause or sentence that fully supports the assertion (`<= 400` chars). Never copy an entire paragraph or an entire table into `quote`.
2. **Exact Row-Column Cell Binding (Prevents Adjacent-Row/Col Swaps)**:
   - Whenever an assertion reports a table number, ALWAYS specify `table_id`, `row_label`, `col_header`, and `value` in `evidence_anchors[]`.
   - Never rely on "number appears somewhere in table" — `nanopub_audit.py --strict` verifies the value sits at the exact `(row_label, col_header)` intersection.
3. **Direct Evidence vs. Indirect Inference / Extrapolation**:
   - Distinguish `direct_observation` / `statistical_comparison` from `indirect_inference` / `extrapolation` (e.g., higher-taxon extrapolation, model assumptions).
   - **Confidence Calibration**: `confidence: "high"` is ONLY allowed for direct measurements/tests backed by intact verbatim text or exact table cells with no symbol defects. Extrapolations, indirect inferences, or OCR `mean_confidence < 75` MUST be capped at `"medium"` or `"low"`.

## 3. Causal & Statistical Overreach Rules (Zero Hallucination)
1. **`p > 0.05` / `no_significant_difference` ≠ Equivalence**:
   - If the paper says *"no statistically significant differences in age (p = 0.33)"*, use `predicate: "no_significant_difference"` and `polarity: "no_difference"` — NEVER claim `"groups have identical age"`.
2. **Observational / Correlational Studies ≠ Causal Proof**:
   - Even if the paper title contains `"Prognostic Value"` or `"Effect"`, an observational cohort (`N=21`, survived vs died) supports `correlates_with` / `differs_from` / `predicts`, NOT `causes`.
3. **PDF Glyph Damage near Statistics**:
   - PDF text extraction sometimes turns `<` into `,` (e.g., `p ,0.03 2p ,0.001`). Always verify rendered/OCR crops from `prep_nanopub.py` or mark `human_review_items[]` before quoting damaged inequalities.

## 4. 追得回 & 读得懂 — Provenance, Deduplication & Machine-Readable Export
- Let `prep_nanopub.py` lock `pubinfo` (`title`, `authors` excluding editors, `year`, `journal`, `doi`, `file_sha256`); never alter locked `pubinfo` manually.
- Deduplicate identical `(subject, canonical_predicate, object)` triples across sections by attaching multiple `evidence_anchors[]`; if numbers/polarities conflict, preserve both and log in `conflicts[]`.
- Every `--merge --strict` run exports `/app/output/nanopublications.trig` (W3C RDF TriG), `/app/output/nanopublications.jsonld`, and `/app/output/nanopubs_report.md`.
