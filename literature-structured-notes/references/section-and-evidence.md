# Section Alignment, Condition Retention & Anchor Verification (`section-and-evidence.md`)

## 1. 对得上 — Section Alignment Rules
- Never reorganize a paper's empirical narrative into generic boilerplate headings if the paper uses a different structure (e.g., `2. Theoretical Model -> 3. Experimental Validation -> 4. Sensitivity Analysis -> 5. Limitations`).
- Always preserve the ascending `order_index` from `outline.md` / `document.json`.
- Record `transition_from_prev` for non-opening sections to preserve argumentative continuity (e.g., *Section 3 tests the kinetic hypothesis formulated in Section 2 under varying CO2 partial pressures*).

## 2. 留得住 — Preconditions & Limitations Rules
- Treat every quantitative result or mechanistic claim as **conditional** until its operating domain is attached:
  - Physical/experimental: temperature, pressure, concentration, catalyst loading, flow regime, duration.
  - Computational/ML: dataset split, sample size `N`, hyperparameters, baseline definitions, steady-state/linearity assumptions.
  - Statistical: confidence/credible intervals, p-values, exclusion criteria.
- Run `query_notes.py --conditions --limitations` before finalizing `patch.json` so no condition or limitation cue is missed.
- If a requested parameter or limitation is absent in the paper, explicitly write `"unreported"` and cite the checked section(s) in `uncertainty`.

## 3. 找得回 — Anchor Granularity & Verbatim Policy
- Prose claims -> cite `doc_id` + `page` (1-based physical page) + `block_id` + verbatim `quote` (`>= 8` chars).
- Printed page discrepancy -> include `printed_page` alongside physical `page` when `page_numbering_discrepancy: true`.
- Table numbers -> cite `doc_id` + `page` + `table_id` + `row_label` + `col_header` + `value` (prevents adjacent-column misattribution).
- Never merge two distant sentences into one fake `quote`; use multiple `evidence_anchors` instead.

## 4. 用得上 — Terminology Normalization & Human-Review Boundary
- Merge abbreviation + full name (e.g., `Metal-Organic Framework` and `MOF-74`) into one `knowledge_points` entry with `aliases`.
- Assign `verification_mode`:
  - `"deterministic"`: verbatim quote, section index, numeric table lookup, abbreviation expansion.
  - `"human_review_required"`: scanned/OCR text with `mean_confidence < 75`, encoding-defect blocks, ambiguous causal claims, or unresolved conflicts between abstract, text, and tables.
