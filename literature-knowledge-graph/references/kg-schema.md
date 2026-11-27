# Literature Knowledge Graph Schema & Output Contract (`kg-schema.md`)

## 1. Top-Level `/app/output/result.json` Contract
Always preserve `case_id` verbatim and every field required by `/input/case.json`'s `output_contract`.
Canonical top-level fields maintained by `prep_kg.py` and `kg_audit.py`:

- `case_id`: Verbatim string from `/input/case.json`.
- `status`: `"completed"` | `"partial"` | `"blocked"` | `"indeterminate"`.
- `papers`: Dict keyed by independent `source_id` (`primary`, `secondary`, `doc_1`, ...). Each paper strictly isolates its own `doi`, `title`, `authors`, `year`, `journal`, `license`, `file_sha256`, `has_pdf_pages`, `locator_mode`, and `companion_formats` (never leak root/primary metadata to secondary papers).
- `nodes`: List of canonical/instance KG entity objects.
- `edges`: List of directed, condition-bound, evidence-backed KG relation objects.
- `entity_alignment`:
  - `merged_candidates`: Approved alias/abbreviation/concept alignments with `merge_basis` and anchors.
  - `non_merge_records`: Explicitly rejected merge candidates (homonyms, distinct physical samples/datasets/model instances/conditions, or same-paper format variants).
- `conflicts`:
  - `intra_source_conflicts`: Contradictions preserved within a single paper.
  - `cross_paper_conflicts`: Contradictions across distinct papers with separate conditions noted.
- `queries`: List of `>= 3` actually executed structured queries from `query_kg.py` (`query_id`, `question`, `query_type`, `query_params`, `executed_command`, `status`, `answer_summary`, `result_subgraph`, `supporting_evidence`, `indeterminate_relations`).
- `incremental_update`: (Populated when `--base-graph` or existing graph input exists) `base_graph_preserved_ids`, `added_nodes`, `merged_nodes`, `added_edges`, `conflict_edges`, `rejected_merges`, `id_stability_verified`.
- `sources`: Verified corpus manifest & ingested file inventory.
- `unavailable_materials`: Detected references to unprovided figures/pixels, supplementary files, or raw clinical/experimental data (never infer values from them).
- `uncertainty`: Unreported parameters, linearized formula warnings, indeterminate items, scope caveats.
- `human_review_items`: OCR defects, stat glyph anomalies, unresolved homonyms, intra-source conflicts.
- `exports`: Paths to `/app/output/knowledge_graph.json`, `/app/output/knowledge_graph.graphml`, `/app/output/knowledge_graph.jsonld`, `/app/output/kg_report.md`.
- `change_records`: Deterministic execution and strict audit log.

> **Contract Alias Synchronization**: When `/input/case.json` specifies aliases such as `entities` (`nodes`), `relations`/`triples` (`edges`), `pubinfo`/`corpus_sources` (`papers`), `graph_queries`/`query_results` (`queries`), `alignment`/`merge_decisions` (`entity_alignment`), or `knowledge_graph`, `kg_audit.py` automatically synchronizes them so all contract fields are populated consistently.

---

## 2. Node Schema (`nodes[]`)
| Field | Type | Required | Description |
|---|---|---|---|
| `node_id` | string | Yes | Stable ID (`n_001`, ...; preserved verbatim in incremental updates) |
| `canonical_name` | string | Yes | Normalized surface name |
| `entity_type` | string | Yes | Controlled type from `references/kg-vocab.json` (`researcher`, `material`, `method`, `task`, `metric`, `conclusion`, `dataset`, `condition`, `concept`) |
| `scope_level` | string | Yes | `"paper_scoped_instance"` \| `"single_paper_entity"` \| `"cross_paper_concept"` |
| `aliases` | array | Yes | Observed surface forms `[{surface_form, source_id, paragraph_id}]` or strings |
| `source_ids` | array | Yes | Independent paper `source_id`(s) attesting this node |
| `provenance_role` | string | Yes | `"author_self_statement"` \| `"cited_or_third_party"` \| `"mixed"` |
| `status` | string | Yes | `"verified"` \| `"indeterminate"` \| `"conflict"` \| `"human_review_required"` |
| `evidence_anchors` | array | Yes | Non-empty list of verbatim anchors |

---

## 3. Edge Schema (`edges[]`)
| Field | Type | Required | Description |
|---|---|---|---|
| `edge_id` | string | Yes | Stable ID (`e_001`, ...; preserved verbatim in incremental updates) |
| `source_node_id` | string | Yes | Must exist in `nodes[].node_id` |
| `relation_type` | string | Yes | Controlled predicate from `references/kg-vocab.json` |
| `canonical_relation` | string | Auto | Canonical relation mapped by `kg_audit.py` |
| `target_node_id` | string | Yes | Must exist in `nodes[].node_id` |
| `direction` | string | Yes | `"directed"` (`source_node_id -> target_node_id`) |
| `source_id` | string | Yes | Single paper `source_id`, or `"cross_paper"` ONLY for explicit epistemic comparison (`supports`/`contradicts`/`extends`) referencing distinct `source_id` anchors without fusing conditions |
| `conditions` | string | Yes | Explicit sample/dataset/parameter/cohort conditions (`"unreported"` if absent) |
| `statistics` | string/object | Recommended | Exact value, unit, p-value, CI, or `"unreported"` |
| `study_design` | string | Recommended | `"experimental"` \| `"rct"` \| `"observational"` \| `"computational_model"` \| `"extrapolation"` \| `"review"` \| `"unreported"` |
| `evidence_type` | string | Yes | `"direct_evidence"` \| `"author_claim"` \| `"statistical_comparison"` \| `"indirect_inference"` \| `"cited_prior_work"` (NEVER `co_occurrence` or `speculation`) |
| `provenance_role` | string | Yes | `"author_self_statement"` \| `"cited_or_third_party"` |
| `confidence` | string | Yes | `"high"` \| `"medium"` \| `"low"` |
| `confidence_basis` | string | Yes | Short justification citing directness & limits |
| `evidence_anchors` | array | Yes | Non-empty list of verbatim source anchors |

---

## 4. Evidence Anchor Schema (`evidence_anchors[]`)
```json
{
  "source_id": "secondary",
  "paragraph_id": "sec2-p3",
  "page": null,
  "printed_page": null,
  "section_id": "sec_2",
  "section_title": "Methods",
  "block_id": "sec2-p3",
  "table_id": null,
  "row_label": null,
  "col_header": null,
  "value": null,
  "xml_verified_text": "Optional rich notation from secondary-publisher.xml (e.g. 10^{-5}, CO_{2})",
  "quote_sha256": "auto-filled by kg_audit.py",
  "quote": "Shortest contiguous verbatim excerpt from the cited paragraph or cell."
}
```
- **Non-PDF Corpus (`has_pdf_pages == false`)**: `page` MUST be `null`, and `paragraph_id` MUST carry the exact organizer/JATS paragraph ID (e.g., from `primary-material.json` / `secondary-material.json` / JATS XML `@id`). NEVER fabricate `"page": 1` or call it an original PDF page!
- **PDF Corpus (`has_pdf_pages == true`)**: `page` MUST be the 1-based physical PDF page (`1..page_count`), accompanied by `block_id` or `(table_id, row_label, col_header, value)` and verbatim `quote`.
