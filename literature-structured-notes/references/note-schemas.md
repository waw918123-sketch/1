# Field Dictionary & Output Schemas (`note-schemas.md`)

## 1. Top-Level `result.json` Contract
Always preserve `case_id` verbatim and every field required by `/input/case.json`'s `output_contract`, plus:
- `status`: `"completed"` | `"partial"` | `"blocked"` | `"indeterminate"`
- `paper_cards`: list of comparable per-paper cards (use `paper_card` if contract expects singular object)
- `section_notes`: section-aligned summaries preserving original `section_id` & `order_index`
- `knowledge_points`: atomic searchable concepts/variables/methods/findings
- `conflicts`: explicit contradictions across abstract vs body vs tables or across papers
- `human_review_items`: items requiring manual confirmation (`verification_mode: "human_review_required"`)
- `sources`: list of inspected files with representative anchors
- `uncertainty`: list of unreported fields, low-confidence OCR pages, or ambiguity notes
- `change_records`: deterministic processing and audit trail

## 2. `paper_cards[]` Schema (Comparable Paper Matrix)
| Field | Type | Purpose |
|---|---|---|
| `doc_id` | string | Document identifier (`doc_1`, `doc_2`, ...) |
| `title` | string | Paper title |
| `research_question` | string | Core problem/hypothesis investigated |
| `data_or_materials` | string | Datasets, catalysts, samples, or `"unreported"` |
| `method_and_conditions` | string | Methods together with operating/boundary preconditions |
| `key_findings` | string | Main quantitative/qualitative findings with scope |
| `limitations` | string | Author-stated or observed limitations, or `"unreported"` |
| `evidence_anchors` | array | List of `evidence_anchor` objects |

## 3. `section_notes[]` Schema (Section-Aligned Summary)
| Field | Type | Purpose |
|---|---|---|
| `doc_id` | string | Target document ID |
| `section_id` | string | Exact ID from `document.json` (e.g. `sec_2`) |
| `section_title` | string | Original section heading |
| `transition_from_prev` | string | How this section follows logically from the previous section |
| `purpose` | string | Section objective |
| `methods` | string | Methods & preconditions introduced in this section |
| `results` | string | Key results/observations in this section |
| `conclusion` | string | Section takeaway |
| `limitations` | string | Section-level caveats or `"unreported"` |
| `evidence_anchors` | array | Verbatim anchors pointing into this section |

## 4. `knowledge_points[]` Schema (Searchable Atomic Entries)
| Field | Type | Purpose |
|---|---|---|
| `canonical_term` | string | Normalized primary term/concept/metric |
| `aliases` | array[string] | Abbreviations, synonyms, surface forms found in text |
| `kind` | string | `"concept"` \| `"variable"` \| `"method"` \| `"finding"` \| `"conclusion"` |
| `statement` | string | Concise self-contained definition or claim |
| `preconditions` | string | Validity conditions (required for `method`/`finding`/`conclusion`; `"unreported"` if absent) |
| `verification_mode` | string | `"deterministic"` \| `"human_review_required"` |
| `evidence_anchors` | array | Required non-empty list of source anchors |

## 5. `evidence_anchor` Schema
```json
{
  "doc_id": "doc_1",
  "page": 2,
  "printed_page": "10",
  "section_id": "sec_3",
  "block_id": "p2_b4",
  "table_id": null,
  "row_label": null,
  "col_header": null,
  "value": null,
  "bbox": [50.0, 120.0, 290.0, 165.0],
  "quote": "Verbatim substring from the cited page or table."
}
```
For table cell citations, provide `table_id`, `row_label`, `col_header`, and `value` so `notes_audit.py` verifies exact row/column cell membership.
