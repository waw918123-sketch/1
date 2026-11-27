# Nanopublication Field Dictionary & Output Schemas (`nanopub-schema.md`)

## 1. Top-Level `/app/output/result.json` Contract
Always preserve `case_id` verbatim and every field required by `/input/case.json`'s `output_contract`, plus:
- `status`: `"completed"` | `"partial"` | `"blocked"` | `"indeterminate"`
- `pubinfo`: Verified publication metadata extracted deterministically per document (`doc_id`, `title`, `authors`, `year`, `journal`, `doi`, `pmid`, `version`, `license`, `source_file`, `file_sha256`). Missing fields are `"unreported"`.
- `nanopublications`: Ordered list of atomic, evidence-bound Nanopublication objects.
- `conflicts`: Contradictions between abstract, body text, tables, or cross-paper assertions.
- `human_review_items`: Items requiring human inspection (e.g., `p ,0.03` garbled inequality glyphs, OCR `mean_confidence < 75`, observational vs causal ambiguity, encrypted/corrupt files).
- `sources`: Verified input file metadata and representative anchors.
- `uncertainty`: Unreported parameters, missing supplementary files, symbol-damage warnings, or scope caveats.
- `exports`: Paths to machine-readable exports (`nanopublications.trig`, `nanopublications.jsonld`, `nanopubs_report.md`).
- `change_records`: Processing and strict audit log.

## 2. Nanopublication Object (`nanopublications[]`)
Each item represents the 4 standard Nanopublication named graphs: **Head**, **Assertion**, **Provenance**, **PubInfo**.

### 2.1 Head Fields (auto-populated by `nanopub_audit.py`)
| Field | Type | Description |
|---|---|---|
| `nanopub_id` | string | Sequential ID (`np_001`, `np_002`, ...) |
| `nanopub_uri` | string | Deterministic hash URI `http://purl.org/np/RA...` |
| `head` | object | URIs for `assertion_graph`, `provenance_graph`, `pubinfo_graph` |

### 2.2 Assertion (`assertion`)
| Field | Type | Required | Description |
|---|---|---|---|
| `subject` | string | Yes | Normalized subject entity (with population/material/batch qualifier if needed) |
| `predicate` | string | Yes | Controlled predicate from `references/predicate-vocab.json` |
| `canonical_predicate` | string | Auto | Canonical predicate ID resolved by `nanopub_audit.py` |
| `object` | string | Yes | Single target entity, measured quantity + unit, or outcome |
| `assertion_text` | string | Yes | Concise single-clause natural language statement (`<= 35` words) |
| `polarity` | string | Yes | `"positive"` \| `"negative"` \| `"no_difference"` \| `"neutral"` \| `"unreported"` |
| `conditions` | string | Yes | Experimental/cohort/model preconditions (`N`, temp, pressure, dose, window, species; `"unreported"` if absent) |
| `statistics` | string/object | If present | Exact test, p-value, CI, SE/SD, R², or `"unreported"` |
| `study_design` | string | Recommended | `"experimental"` \| `"rct"` \| `"observational"` \| `"computational_model"` \| `"extrapolation"` \| `"review"` \| `"unreported"` |

### 2.3 Provenance (`provenance`)
| Field | Type | Required | Description |
|---|---|---|---|
| `evidence_type` | string | Yes | `"direct_observation"` \| `"statistical_comparison"` \| `"indirect_inference"` \| `"extrapolation"` |
| `extraction_method` | string | Yes | Concrete method (e.g., `"direct_measurement"`, `"mann_whitney_u"`, `"higher_taxon_extrapolation"`, `"table_lookup"`) |
| `confidence` | string | Yes | `"high"` \| `"medium"` \| `"low"` (capped automatically when indirect/extrapolated/low-OCR) |
| `confidence_basis` | string | Yes | Explicit justification linking confidence to direct evidence & design limits |
| `evidence_anchors` | array | Yes | Non-empty list of minimal sufficient source anchors |

### 2.4 Evidence Anchor (`evidence_anchors[]`)
```json
{
  "doc_id": "doc_1",
  "page": 5,
  "printed_page": "5",
  "section_id": "sec_3",
  "block_id": "p5_b12",
  "table_id": null,
  "row_label": null,
  "col_header": null,
  "value": null,
  "bbox": [315.0, 561.0, 554.0, 635.0],
  "quote_sha256": "auto-filled by nanopub_audit.py",
  "quote": "There were no statistically significant differences between the groups in terms of age (p = 0.33)."
}
```
- **Prose / caption / equation evidence**: provide `doc_id`, `page`, `block_id`, and the **shortest sufficient contiguous `quote`** (`8`–`400` chars).
- **Table cell evidence**: provide `doc_id`, `page`, `table_id`, `row_label`, `col_header`, and exact `value` (plus `quote` if helpful) so `nanopub_audit.py` verifies exact row/column cell membership.

### 2.5 PubInfo (`pubinfo`, auto-inherited & locked from `prep_nanopub.py`)
`doc_id`, `title`, `authors`, `year`, `journal`, `doi`, `pmid`, `version`, `license`, `source_file`, `file_sha256`, `created_at`, `generated_by`.
