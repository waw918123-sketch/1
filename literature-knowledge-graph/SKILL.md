---
name: literature-knowledge-graph
description: >-
  Evidence-grounded scientific literature Knowledge Graph construction, cross-paper
  entity alignment vs. forbidden-merge guarding, JATS paragraph_id & XML math
  cross-checking, incremental update ID preservation, and deterministic graph query
  skill. Resolves corpus-manifest.json and multi-format paper twins without
  cross-paper metadata leakage or fake PDF pages, executes verifiable subgraph
  queries, and exports GraphML, JSON-LD, and Markdown alongside result.json.
---

# Literature Knowledge Graph Construction (`literature-knowledge-graph`)

Transform single or multi-paper scientific inputs (`corpus-manifest.json`, `material.json`, `primary/secondary-material.json`, JATS XML, PDF, Markdown/TXT, Office) and optional existing graphs into an **evidence-bound, alignment-audited, queryable Knowledge Graph** within Harbor's budget (`64` requests / `900s` / `16,384` tokens per response).

## Core Principles (Non-Negotiable)

1. **Read actual files first (`抽得准`)**: Inspect `/input/case.json`, `/input/task.md`, and ingested chunks. Extract only genuine scientific objects (`researcher`, `material`, `method`, `task`, `metric`, `conclusion`, `dataset`, `condition`) and mark `provenance_role` (`author_self_statement` vs `cited_or_third_party`). Never invent missing data or infer values from unprovided figure pixels, supplements, or raw experimental/clinical datasets.
2. **Isolate papers & format variants (`独立溯源`)**: Let `prep_kg.py` lock per-paper metadata from `corpus-manifest.json` / material / XML. Root `case.json` DOI/title/authors belong ONLY to the primary paper—never propagate them onto secondary papers. Format variants of the same paper (e.g., `secondary-material.json` + `secondary-publisher.xml`) share ONE `source_id` and NEVER count as cross-paper evidence.
3. **Never fabricate PDF pages (`定位真实`)**: When `has_pdf_pages == false`, keep `page: null` and cite `(source_id, paragraph_id, quote)` using organizer JATS `paragraph_id`. Never emit `"page": 1` as an original PDF page. Always inspect `query_kg.py --formula-diffs` when `secondary-publisher.xml` is present so linearized `<sub>`/`<sup>`/`<inline-formula>` notations are cross-checked accurately.
4. **Accurate directed relations (`连得对`)**: Use controlled relations in [references/kg-vocab.json](./references/kg-vocab.json) with explicit `direction`, `conditions`, and `statistics`. Never turn `co_occurrence`, paraphrase, or speculation into direct edges; never use `causes` on observational studies; never fuse two papers' samples, conditions, or numeric results into one conclusion.
5. **Principled alignment & non-merge guard (`对得齐`)**: Record approved alias/abbreviation/concept merges in `entity_alignment.merged_candidates` AND explicitly record homonyms, distinct physical samples, model instances, datasets, or conditions that MUST NOT be merged in `entity_alignment.non_merge_records`. Keep intra-source and cross-paper conflicts explicit in `conflicts`.
6. **Executable queries & export (`查得到、回得去`)**: Define at least `3` structured queries in `query_specs` (including any task-mandated questions) so `kg_audit.py --strict` executes them deterministically via `query_kg.py` against the audited graph, returning exact `result_subgraph`, `supporting_evidence`, and `indeterminate_relations`, plus GraphML, JSON-LD, and Markdown exports.

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

## Standard 4-Stage Execution Workflow

### Stage 1 — Prepare Corpus, Lock Paper Metadata & Baseline Graph
Run `prep_kg.py` upfront to resolve `corpus-manifest.json`, pair format variants (`secondary-material.json` + `secondary-publisher.xml`), cross-check linearized XML math, detect any base graph for incremental updates, write compact `/app/work/kg_brief.json`, and initialize `/app/output/result.json`:

```sh
mkdir -p /app/work /app/output
python3 "$SKILL_DIR/scripts/prep_kg.py" \
  --input-dir /input \
  --work-dir /app/work \
  --init-result /app/output/result.json
```

Inspect `/app/work/kg_brief.json` and each paper's `/app/work/<source_id>/outline.md`. If `status == "blocked"`, document the block honestly in `/app/output/result.json` and stop.

### Stage 2 — Low-Token Chunk, Cue & Formula-Diff Retrieval
Read section chunks sequentially (`<= 8,000` chars) and retrieve targeted cues, table rows, and XML formula diffs without dumping large raw files:

```sh
python3 "$SKILL_DIR/scripts/query_kg.py" --work-dir /app/work --source-id primary --chunk chunk_001
python3 "$SKILL_DIR/scripts/query_kg.py" --work-dir /app/work --cues --formula-diffs
python3 "$SKILL_DIR/scripts/query_kg.py" --work-dir /app/work --table tbl_1 --row "Total"
```

Consult [references/kg-schema.md](./references/kg-schema.md) for node/edge/query fields and [references/alignment-and-evidence.md](./references/alignment-and-evidence.md) for cross-paper merge vs non-merge rules.

### Stage 3 — Write Compact KG Patch + Query Specs & Run 6-Pillar Strict Audit
Write `/app/work/patch.json` using this compact schema (set `"append": true` for multi-chunk incremental merging; preserve existing IDs when `incremental_mode.enabled` is true):

```json
{
  "status": "completed",
  "nodes": [
    {
      "node_id": "n_001", "canonical_name": "...", "entity_type": "method|material|researcher|task|metric|conclusion|dataset|condition|concept",
      "scope_level": "paper_scoped_instance|single_paper_entity|cross_paper_concept",
      "source_ids": ["primary"], "provenance_role": "author_self_statement|cited_or_third_party",
      "aliases": [{"surface_form": "...", "source_id": "primary", "paragraph_id": "..."}],
      "evidence_anchors": [{"source_id": "primary", "paragraph_id": "...", "page": null, "quote": "..."}]
    }
  ],
  "edges": [
    {
      "edge_id": "e_001", "source_node_id": "n_001", "relation_type": "uses|proposes|reports_metric|evaluates_on|outperforms|compares_with|supports|contradicts|...",
      "target_node_id": "n_002", "source_id": "primary", "conditions": "...", "statistics": "...",
      "evidence_type": "direct_evidence|statistical_comparison|author_claim|indirect_inference|cited_prior_work",
      "provenance_role": "author_self_statement|cited_or_third_party", "confidence": "high|medium|low",
      "evidence_anchors": [{"source_id": "primary", "paragraph_id": "...", "page": null, "quote": "..."}]
    }
  ],
  "entity_alignment": {"merged_candidates": [], "non_merge_records": []},
  "conflicts": {"intra_source_conflicts": [], "cross_paper_conflicts": []},
  "query_specs": [
    {"query_id": "q_001", "question": "...", "query_type": "entity_neighborhood|relation_triple|cross_paper_comparison|shortest_path|conflict_and_uncertainty", "query_params": {"term": "..."}}
  ]
}
```

Then merge, execute queries deterministically, audit strictly, and export all formats in one call:

```sh
python3 "$SKILL_DIR/scripts/kg_audit.py" \
  --result /app/output/result.json \
  --merge /app/work/patch.json \
  --case /input/case.json \
  --work-dir /app/work \
  --strict
```

### Stage 4 — Interactive Query Inspection & Edge-Case Recovery
Verify any individual query or cross-paper comparison directly against `/app/output/knowledge_graph.json`:

```sh
python3 "$SKILL_DIR/scripts/query_kg.py" --graph /app/output/knowledge_graph.json --kg-query-compare "<concept>"
python3 "$SKILL_DIR/scripts/query_kg.py" --graph /app/output/knowledge_graph.json --kg-query-conflicts
```

Consult [references/troubleshooting.md](./references/troubleshooting.md) for non-PDF `paragraph_id` citations, unavailable figures/supplements/raw data, linearized formula diffs, homonym collisions, and zero-hit queries.
