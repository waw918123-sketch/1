# Entity Alignment, Non-Merge Discipline & Evidence Verification (`alignment-and-evidence.md`)

## 1. 抽得准 — Author Self-Statement vs. Cited Background
1. Extract ONLY scientific objects genuinely present in the supplied papers (`researcher`, `material`, `method`, `task`, `metric`, `conclusion`, `dataset`, `condition`). Never complete unstated concepts from prior knowledge.
2. Mark `provenance_role`:
   - `"author_self_statement"`: methods proposed/executed, materials tested, measurements obtained, or conclusions claimed by the current paper's authors.
   - `"cited_or_third_party"`: background claims, baseline descriptions, or prior results attributed to cited works (`[12]`, `Smith et al.`). Never promote cited claims to the current paper's own experimental findings.

## 2. 连得对 — Relation Direction, Conditions & Anti-Cooccurrence
1. **No Co-occurrence / Paraphrase / Speculation**:
   - Two entities appearing in the same sentence or paragraph do NOT automatically form a direct edge.
   - Forbidden `evidence_type`: `"co_occurrence"`, `"speculation"`, `"commonsense_completion"`.
   - Forbidden vague `relation_type`: `"relates_to"`, `"co_occurs_with"`, `"mentions"`, `"links"`, `"affects"`.
2. **Preserve Direction & Applicability Conditions**:
   - `A outperforms B` differs from `B outperforms A`; always record `conditions` (dataset, sample size `N`, temperature/pressure, dose, hyperparameters, cohort) and `statistics`.
   - Never use `causes` on observational/retrospective/correlational designs (use `correlates_with` or `predicts`).
   - Never convert `p > 0.05` / *"no significant difference"* into equivalence.

## 3. 对得齐 — Cross-Paper Alignment vs. Forbidden Merges (`non_merge_records`)
1. **Same-Paper Format Variants ≠ Cross-Paper Evidence**:
   - If the same paper appears in multiple formats (e.g., `secondary-material.json` + `secondary-publisher.xml`, or `paper.pdf` + `paper.xml`), both share ONE `source_id` (`secondary`). Do NOT create two `source_id`s and do NOT claim "cross-paper corroboration" from format variants!
2. **Metadata Isolation**:
   - Root `case.json` `doi`/`title`/`authors` belong ONLY to the primary paper (`doc_1`/`primary`), NEVER copied onto `secondary` or other papers. Each paper keeps its own verified `corpus-manifest.json` / material / XML metadata.
3. **Concept Alignment vs. Operational Instance Separation**:
   - Identical generic method/material terms (e.g., `"DFT"`, `"ResNet-50"`, `"graphene"`, `"AUC"`, `"mice"`) across Paper 1 and Paper 2 do NOT imply the two papers used the same physical sample, model weights/version, dataset split, or experimental conditions!
   - Strategy:
     - Keep operational entities paper-scoped (`scope_level: "paper_scoped_instance"`, e.g. `DFT setup (primary)` vs `DFT setup (secondary)`) OR attach a shared abstract `concept` node via `instance_of` while keeping all `reports_metric` / `uses` / `outperforms` edges strictly scoped to a single `source_id` and its own `conditions`!
     - NEVER merge conditions, samples, and numerical results from two papers into one composite edge or conclusion!
4. **Mandatory `non_merge_records[]`**:
   - Explicitly document every candidate pair that looks lexically similar (abbreviations, shared generic terms, homonyms) but MUST NOT be merged, specifying `rejection_reason` and verbatim evidence from both sides.

## 4. 查得到、回得去 — JATS `paragraph_id`, XML Math Cross-Check & Unavailable Data
1. **Non-PDF Locators**: When `has_pdf_pages == false`, cite `(source_id, paragraph_id, quote)` using the exact organizer JATS `paragraph_id`. Never emit `"page": 1` or call it an original PDF page.
2. **XML Companion Cross-Check (`secondary-publisher.xml`)**:
   - Linearized JSON text flattens `<sub>`, `<sup>`, and `<inline-formula>` (e.g., `10^{5}` -> `105`, `CO_{2}` -> `CO2`).
   - Always run `query_kg.py --formula-diffs` and inspect `xml_rich_text` from `secondary-publisher.xml` before reporting numeric/chemical/math assertions from `secondary-material.json`.
3. **Unavailable Materials**: Never infer numerical values from unprovided figure pixels, supplementary files, or underlying clinical/experimental datasets. Record them in `unavailable_materials` and `uncertainty`.
