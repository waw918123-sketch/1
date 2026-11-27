#!/usr/bin/env python3
"""双模知识图谱检索与确定性查询引擎。

模式 A（语料检索）：按 chunk、关键词、表格行列、关系/引用/缺失材料线索与 XML 公式差异检索，严格限长 <= 18,000 字符。
模式 B（图谱查询）：针对已构建的 knowledge_graph.json / result.json 执行真实图谱查询，返回精确子图、原文证据与无法确定的关系。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.dont_write_bytecode = True

from doc_common import normalize_for_match, normalize_term_key, parse_page_spec


def _load_docs(work_dir: Optional[Path], source_id: Optional[str]) -> List[Dict[str, Any]]:
    loaded: List[Dict[str, Any]] = []
    if work_dir and work_dir.exists():
        for dp in sorted(work_dir.glob("*/document.json")):
            d = json.loads(dp.read_text(encoding="utf-8"))
            if source_id and d.get("source_id") != source_id:
                continue
            d["_dir"] = str(dp.parent)
            loaded.append(d)
    return loaded


def _filter_table(t: Dict[str, Any], row_filter: Optional[str]) -> Dict[str, Any]:
    rows = t.get("rows") or []
    n_hdr = int(t.get("header_rows") or 1)
    if row_filter:
        rf = normalize_for_match(row_filter).lower()
        hdr_rows = rows[:n_hdr]
        matched = [r for r in rows[n_hdr:] if rf in normalize_for_match(" ".join(r)).lower()]
        return {
            "table_id": t["table_id"],
            "paragraph_id": t.get("paragraph_id"),
            "page": t.get("page"),
            "section_id": t.get("section_id"),
            "caption": t.get("caption"),
            "headers": t.get("headers"),
            "header_rows": n_hdr,
            "row_filter": row_filter,
            "matched_row_count": len(matched),
            "rows": hdr_rows + matched,
        }
    return {
        "table_id": t["table_id"],
        "paragraph_id": t.get("paragraph_id"),
        "page": t.get("page"),
        "section_id": t.get("section_id"),
        "caption": t.get("caption"),
        "headers": t.get("headers"),
        "header_rows": n_hdr,
        "num_rows": t.get("num_rows"),
        "num_cols": t.get("num_cols"),
        "markdown": t.get("markdown"),
    }


def _match_node_term(node: Dict[str, Any], term: str) -> bool:
    if not term:
        return True
    tl = term.strip().lower()
    tkey = normalize_term_key(term)
    if str(node.get("node_id") or "").lower() == tl:
        return True
    cname = str(node.get("canonical_name") or "")
    if tl in cname.lower() or (tkey and tkey in normalize_term_key(cname)):
        return True
    for al in node.get("aliases") or []:
        s = al.get("surface_form") if isinstance(al, dict) else str(al)
        if s and (tl in s.lower() or (tkey and tkey in normalize_term_key(s))):
            return True
    return False


def _collect_evidence_from_items(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    ev: List[Dict[str, Any]] = []
    for e in edges:
        for a in e.get("evidence_anchors") or []:
            if not isinstance(a, dict):
                continue
            item = {
                "from_edge_id": e.get("edge_id"),
                "source_id": a.get("source_id") or e.get("source_id"),
                "paragraph_id": a.get("paragraph_id") or a.get("block_id"),
                "page": a.get("page"),
                "table_id": a.get("table_id"),
                "quote": a.get("quote"),
            }
            sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if sig not in seen:
                seen.add(sig)
                ev.append(item)
    for n in nodes:
        for a in n.get("evidence_anchors") or []:
            if not isinstance(a, dict):
                continue
            item = {
                "from_node_id": n.get("node_id"),
                "source_id": a.get("source_id"),
                "paragraph_id": a.get("paragraph_id") or a.get("block_id"),
                "page": a.get("page"),
                "table_id": a.get("table_id"),
                "quote": a.get("quote"),
            }
            sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if sig not in seen:
                seen.add(sig)
                ev.append(item)
    return ev[:30]


def execute_kg_query(graph_obj: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    """针对图谱 JSON 执行确定性结构化查询，返回子图、原文证据及无法确定的关系。"""
    nodes = list(graph_obj.get("nodes") or [])
    edges = list(graph_obj.get("edges") or [])
    node_by_id = {str(n.get("node_id")): n for n in nodes if n.get("node_id")}
    alignment = graph_obj.get("entity_alignment") or {}
    non_merges = list(alignment.get("non_merge_records") or [])
    merges = list(alignment.get("merged_candidates") or [])
    conflicts_obj = graph_obj.get("conflicts") or {}
    all_conflicts = (
        (conflicts_obj.get("intra_source_conflicts") or []) + (conflicts_obj.get("cross_paper_conflicts") or [])
        if isinstance(conflicts_obj, dict)
        else (conflicts_obj if isinstance(conflicts_obj, list) else [])
    )

    qid = str(spec.get("query_id") or "q_001")
    qtype = str(spec.get("query_type") or "entity_neighborhood")
    params = dict(spec.get("query_params") or {})
    question = str(spec.get("question") or f"Execute {qtype} on {params}")

    matched_nodes: List[Dict[str, Any]] = []
    matched_edges: List[Dict[str, Any]] = []
    indeterminate: List[Dict[str, Any]] = []
    extra: Dict[str, Any] = {}

    if qtype == "entity_neighborhood":
        term = str(params.get("term") or params.get("entity") or "")
        seed_nodes = [n for n in nodes if _match_node_term(n, term)]
        seed_ids = {n["node_id"] for n in seed_nodes}
        rel_edges = [e for e in edges if e.get("source_node_id") in seed_ids or e.get("target_node_id") in seed_ids]
        nbr_ids = set(seed_ids)
        for e in rel_edges:
            nbr_ids.add(e.get("source_node_id"))
            nbr_ids.add(e.get("target_node_id"))
        matched_nodes = [node_by_id[nid] for nid in nbr_ids if nid in node_by_id]
        matched_edges = rel_edges

    elif qtype == "relation_triple":
        src_pat = str(params.get("source") or "")
        rel_pat = str(params.get("relation") or params.get("relation_type") or "")
        tgt_pat = str(params.get("target") or "")
        rel_key = normalize_term_key(rel_pat)
        for e in edges:
            sn = node_by_id.get(e.get("source_node_id"), {})
            tn = node_by_id.get(e.get("target_node_id"), {})
            if src_pat and not _match_node_term(sn, src_pat):
                continue
            if tgt_pat and not _match_node_term(tn, tgt_pat):
                continue
            erel = str(e.get("canonical_relation") or e.get("relation_type") or "")
            if rel_pat and rel_key not in normalize_term_key(erel) and rel_pat.lower() not in erel.lower():
                continue
            matched_edges.append(e)
        nids = {e.get("source_node_id") for e in matched_edges} | {e.get("target_node_id") for e in matched_edges}
        matched_nodes = [node_by_id[nid] for nid in nids if nid in node_by_id]

    elif qtype == "cross_paper_comparison":
        concept = str(params.get("concept") or params.get("term") or "")
        seed_nodes = [n for n in nodes if _match_node_term(n, concept)]
        seed_ids = {n["node_id"] for n in seed_nodes}
        # 扩展 instance_of 关系连接的跨篇实例节点
        for e in edges:
            if e.get("relation_type") in ("instance_of", "aligned_with") and (
                e.get("source_node_id") in seed_ids or e.get("target_node_id") in seed_ids
            ):
                seed_ids.add(e.get("source_node_id"))
                seed_ids.add(e.get("target_node_id"))
        rel_edges = [e for e in edges if e.get("source_node_id") in seed_ids or e.get("target_node_id") in seed_ids]
        nids = set(seed_ids)
        for e in rel_edges:
            nids.add(e.get("source_node_id"))
            nids.add(e.get("target_node_id"))
        matched_nodes = [node_by_id[nid] for nid in nids if nid in node_by_id]
        matched_edges = rel_edges

        per_paper: Dict[str, List[Dict[str, Any]]] = {}
        for e in matched_edges:
            per_paper.setdefault(str(e.get("source_id") or "unknown"), []).append({
                "edge_id": e.get("edge_id"),
                "triple": f"{node_by_id.get(e.get('source_node_id'), {}).get('canonical_name')} -[{e.get('relation_type')}]-> {node_by_id.get(e.get('target_node_id'), {}).get('canonical_name')}",
                "conditions": e.get("conditions"),
                "statistics": e.get("statistics"),
            })
        rel_non_merges = [
            nm for nm in non_merges
            if normalize_term_key(concept) in normalize_term_key(json.dumps(nm, ensure_ascii=False))
        ]
        rel_merges = [
            mg for mg in merges
            if normalize_term_key(concept) in normalize_term_key(json.dumps(mg, ensure_ascii=False))
        ]
        extra = {"per_paper_subgraphs": per_paper, "relevant_merges": rel_merges, "relevant_non_merges": rel_non_merges}
        for nm in rel_non_merges:
            indeterminate.append({
                "type": "forbidden_cross_paper_merge",
                "reason": nm.get("rejection_reason"),
                "detail": nm.get("rationale"),
            })

    elif qtype == "shortest_path":
        src_pat = str(params.get("source") or "")
        tgt_pat = str(params.get("target") or "")
        max_hops = int(params.get("max_hops") or 3)
        starts = [n["node_id"] for n in nodes if _match_node_term(n, src_pat)]
        goals = {n["node_id"] for n in nodes if _match_node_term(n, tgt_pat)}
        adj: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
        for e in edges:
            u, v = e.get("source_node_id"), e.get("target_node_id")
            if u and v:
                adj.setdefault(u, []).append((v, e))
                adj.setdefault(v, []).append((u, e))
        found_edges: List[Dict[str, Any]] = []
        if starts and goals:
            q = deque([(s, []) for s in starts])
            visited = set(starts)
            while q:
                cur, path_e = q.popleft()
                if cur in goals and path_e:
                    found_edges = path_e
                    break
                if len(path_e) >= max_hops:
                    continue
                for nxt, edge in adj.get(cur, []):
                    if nxt not in visited:
                        visited.add(nxt)
                        q.append((nxt, path_e + [edge]))
        matched_edges = found_edges
        nids = {e.get("source_node_id") for e in matched_edges} | {e.get("target_node_id") for e in matched_edges}
        matched_nodes = [node_by_id[nid] for nid in nids if nid in node_by_id]
        if not matched_edges:
            indeterminate.append({
                "type": "no_evidenced_path",
                "reason": f"No evidenced graph path within {max_hops} hops between {src_pat!r} and {tgt_pat!r}.",
            })

    elif qtype == "conflict_and_uncertainty":
        matched_edges = [
            e for e in edges
            if str(e.get("relation_type")) in ("contradicts", "not_reported")
            or str(e.get("evidence_type")) == "indirect_inference"
        ]
        nids = {e.get("source_node_id") for e in matched_edges} | {e.get("target_node_id") for e in matched_edges}
        for n in nodes:
            if n.get("status") in ("indeterminate", "conflict", "human_review_required"):
                nids.add(n["node_id"])
        matched_nodes = [node_by_id[nid] for nid in nids if nid in node_by_id]
        extra = {"conflicts_catalog": all_conflicts, "non_merge_records": non_merges}

    # 检查命中边中的未报告、未确定与冲突关系
    matched_nids = {n.get("node_id") for n in matched_nodes}
    for e in matched_edges:
        if e.get("relation_type") == "not_reported" or str(e.get("conditions") or "").lower() == "unreported":
            indeterminate.append({
                "type": "unreported_or_unconditioned_relation",
                "edge_id": e.get("edge_id"),
                "relation": e.get("relation_type"),
                "detail": f"Conditions={e.get('conditions')!r}, source_id={e.get('source_id')}",
            })
        if e.get("relation_type") == "contradicts":
            indeterminate.append({
                "type": "contradictory_relation",
                "edge_id": e.get("edge_id"),
                "detail": f"Explicit contradiction documented in {e.get('source_id')}.",
            })
    for cf in all_conflicts:
        if not isinstance(cf, dict):
            continue
        cf_txt = json.dumps(cf, ensure_ascii=False)
        if any(nid in cf_txt for nid in matched_nids) or any(
            str(n.get("canonical_name", "")).lower() in cf_txt.lower() for n in matched_nodes if n.get("canonical_name")
        ):
            indeterminate.append({"type": "linked_conflict", "conflict": cf})

    if not matched_nodes and not matched_edges and not indeterminate:
        indeterminate.append({
            "type": "no_evidenced_match",
            "reason": "Query returned no matching entities or evidenced edges in the current knowledge graph.",
        })

    status = (
        "no_evidenced_match"
        if (not matched_nodes and not matched_edges)
        else ("partial_with_indeterminate" if indeterminate else "answered")
    )

    ev_list = _collect_evidence_from_items(matched_nodes, matched_edges)
    edge_clauses: List[str] = []
    for e in matched_edges[:8]:
        sn = node_by_id.get(e.get("source_node_id"), {}).get("canonical_name") or e.get("source_node_id")
        tn = node_by_id.get(e.get("target_node_id"), {}).get("canonical_name") or e.get("target_node_id")
        rel = e.get("canonical_relation") or e.get("relation_type")
        stat = e.get("statistics")
        cond = e.get("conditions")
        det_parts = [f"source={e.get('source_id')}"]
        if cond and str(cond).lower() != "unreported":
            det_parts.append(f"conditions={cond}")
        if stat and str(stat).lower() != "unreported":
            det_parts.append(f"stats={stat}")
        edge_clauses.append(f"{sn} -[{rel}]-> {tn} ({'; '.join(det_parts)})")
    if edge_clauses:
        summary = " | ".join(edge_clauses)
        if indeterminate:
            summary += f" [Indeterminate/Conflict notes: {len(indeterminate)}]"
    else:
        node_names = [str(n.get("canonical_name") or n.get("node_id")) for n in matched_nodes[:6]]
        summary = (
            f"Matched nodes {node_names} with 0 evidenced directed edges; "
            f"{len(indeterminate)} indeterminate/unreported note(s)."
        )

    res = {
        "query_id": qid,
        "question": question,
        "query_type": qtype,
        "query_params": params,
        "executed_command": spec.get("executed_command") or f"query_kg.py --exec-query {qid}",
        "status": status,
        "answer_summary": spec.get("answer_summary") or summary,
        "result_subgraph": {
            "node_ids": [n.get("node_id") for n in matched_nodes],
            "edge_ids": [e.get("edge_id") for e in matched_edges],
            "nodes": [
                {
                    "node_id": n.get("node_id"),
                    "canonical_name": n.get("canonical_name"),
                    "entity_type": n.get("entity_type"),
                    "scope_level": n.get("scope_level"),
                    "source_ids": n.get("source_ids"),
                }
                for n in matched_nodes
            ],
            "edges": [
                {
                    "edge_id": e.get("edge_id"),
                    "source_node_id": e.get("source_node_id"),
                    "relation_type": e.get("canonical_relation") or e.get("relation_type"),
                    "target_node_id": e.get("target_node_id"),
                    "source_id": e.get("source_id"),
                    "conditions": e.get("conditions"),
                    "statistics": e.get("statistics"),
                }
                for e in matched_edges
            ],
        },
        "supporting_evidence": ev_list,
        "indeterminate_relations": indeterminate,
    }
    if extra:
        res["analysis"] = extra
    return res


def main() -> int:
    parser = argparse.ArgumentParser(description="Corpus retriever and deterministic Knowledge Graph query engine.")
    parser.add_argument("--work-dir", default="/app/work")
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--query", default=None, help="Regex/keyword search over corpus blocks and tables.")
    parser.add_argument("--chunk", default=None, help="Read a specific section chunk (e.g. chunk_001).")
    parser.add_argument("--table", default=None, help="Return matching table_id or caption substring.")
    parser.add_argument("--row", default=None, help="Optional row filter substring.")
    parser.add_argument("--pages", default=None, help="Filter PDF pages, e.g. '1-3'.")
    parser.add_argument("--cues", action="store_true", help="Include pre-extracted KG relation/citation/unavailable cues.")
    parser.add_argument("--formula-diffs", action="store_true", help="Include linearized math/subscript/superscript XML cross-checks.")
    parser.add_argument("--limit", type=int, default=12)

    # 图谱查询模式参数
    parser.add_argument("--graph", default="/app/output/knowledge_graph.json", help="Path to knowledge_graph.json or result.json.")
    parser.add_argument("--kg-query-entity", default=None)
    parser.add_argument("--kg-query-relation", default=None, help="Source entity pattern for triple search.")
    parser.add_argument("--rel", default=None, help="Relation type filter for triple search.")
    parser.add_argument("--target", default=None, help="Target entity pattern for triple search.")
    parser.add_argument("--kg-query-compare", default=None, help="Cross-paper comparison on entity/concept.")
    parser.add_argument("--kg-query-path", default=None, help="Start node for shortest-path query (pair with --to).")
    parser.add_argument("--to", default=None, help="End node for shortest-path query.")
    parser.add_argument("--max-hops", type=int, default=3)
    parser.add_argument("--kg-query-conflicts", action="store_true")
    parser.add_argument("--exec-queries-file", default=None, help="Execute a JSON list of query specs against --graph.")
    args = parser.parse_args()

    # 图谱查询模式
    is_kg_mode = any([
        args.kg_query_entity, args.kg_query_relation, args.rel, args.target,
        args.kg_query_compare, args.kg_query_path, args.kg_query_conflicts, args.exec_queries_file,
    ])
    if is_kg_mode:
        gp = Path(args.graph)
        if not gp.exists():
            print(json.dumps({"error": f"Graph file not found: {gp}"}, ensure_ascii=False))
            return 1
        g_obj = json.loads(gp.read_text(encoding="utf-8"))
        if isinstance(g_obj.get("knowledge_graph"), dict) and "nodes" not in g_obj:
            g_obj = g_obj["knowledge_graph"]

        if args.exec_queries_file:
            specs = json.loads(Path(args.exec_queries_file).read_text(encoding="utf-8"))
            if isinstance(specs, dict):
                specs = specs.get("queries") or specs.get("query_specs") or [specs]
            results = [execute_kg_query(g_obj, sp) for sp in specs if isinstance(sp, dict)]
            print(json.dumps({"executed_count": len(results), "queries": results}, ensure_ascii=False, indent=2)[:18000])
            return 0

        if args.kg_query_entity:
            spec = {"query_id": "q_entity", "query_type": "entity_neighborhood", "question": f"Entity neighborhood of {args.kg_query_entity}", "query_params": {"term": args.kg_query_entity}, "executed_command": f"query_kg.py --graph {gp} --kg-query-entity {args.kg_query_entity!r}"}
        elif args.kg_query_compare:
            spec = {"query_id": "q_compare", "query_type": "cross_paper_comparison", "question": f"Cross-paper comparison on {args.kg_query_compare}", "query_params": {"concept": args.kg_query_compare}, "executed_command": f"query_kg.py --graph {gp} --kg-query-compare {args.kg_query_compare!r}"}
        elif args.kg_query_path:
            spec = {"query_id": "q_path", "query_type": "shortest_path", "question": f"Shortest evidenced path from {args.kg_query_path} to {args.to}", "query_params": {"source": args.kg_query_path, "target": args.to or "", "max_hops": args.max_hops}, "executed_command": f"query_kg.py --graph {gp} --kg-query-path {args.kg_query_path!r} --to {args.to!r}"}
        elif args.kg_query_conflicts:
            spec = {"query_id": "q_conflicts", "query_type": "conflict_and_uncertainty", "question": "Conflicts, non-merges, and indeterminate relations", "query_params": {}, "executed_command": f"query_kg.py --graph {gp} --kg-query-conflicts"}
        else:
            spec = {"query_id": "q_triple", "query_type": "relation_triple", "question": f"Relation triple query ({args.kg_query_relation}, {args.rel}, {args.target})", "query_params": {"source": args.kg_query_relation or "", "relation": args.rel or "", "target": args.target or ""}, "executed_command": f"query_kg.py --graph {gp} --kg-query-relation {args.kg_query_relation!r} --rel {args.rel!r} --target {args.target!r}"}
        print(json.dumps(execute_kg_query(g_obj, spec), ensure_ascii=False, indent=2)[:18000])
        return 0

    # 语料检索模式
    docs = _load_docs(Path(args.work_dir), args.source_id)
    if not docs:
        print(json.dumps({"error": "No document.json found. Run prep_kg.py first."}))
        return 1

    if args.chunk:
        for d in docs:
            cp = Path(d["_dir"]) / "section_chunks" / f"{args.chunk}.json"
            if cp.exists():
                print(cp.read_text(encoding="utf-8")[:18000])
                return 0
        print(json.dumps({"error": f"Chunk {args.chunk!r} not found."}))
        return 1

    pat = re.compile(args.query, re.I) if args.query else None
    out: Dict[str, Any] = {"papers_queried": [d.get("source_id") for d in docs], "block_hits": [], "table_hits": []}
    if args.cues:
        out["cues"] = []
    if args.formula_diffs:
        out["formula_diffs"] = []

    for d in docs:
        sid = d.get("source_id", "primary")
        has_pdf = bool(d.get("has_pdf_pages"))
        page_labels = d.get("page_label_map") or {}
        p_filter: Optional[Set[int]] = None
        if args.pages and has_pdf:
            p_filter = set(parse_page_spec(args.pages, int(d.get("page_count") or 1)))

        if args.table:
            ql = args.table.lower()
            for t in d.get("tables", []):
                if t.get("table_id") == args.table or t.get("native_table_id") == args.table or ql in str(t.get("caption") or "").lower():
                    out["table_hits"].append({"source_id": sid, **_filter_table(t, args.row)})
            continue

        if args.query or args.pages:
            for b in d.get("blocks", []):
                if p_filter is not None and b.get("page") not in p_filter:
                    continue
                hay = f"{b.get('raw_text', '')}\n{b.get('xml_rich_text', '')}"
                if pat and not pat.search(hay):
                    continue
                p_val = b.get("page") if has_pdf else None
                hit = {
                    "source_id": sid,
                    "paragraph_id": b.get("paragraph_id") or b["block_id"],
                    "page": p_val,
                    "printed_page": (page_labels.get(p_val) or page_labels.get(str(p_val))) if p_val is not None else None,
                    "section": b.get("section_title"),
                    "block_id": b["block_id"],
                    "role": b.get("role"),
                    "quote": b.get("raw_text"),
                }
                if b.get("xml_rich_text"):
                    hit["xml_rich_text"] = b["xml_rich_text"]
                out["block_hits"].append(hit)
                if len(out["block_hits"]) >= args.limit:
                    break

            for t in d.get("tables", []):
                hay = f"{t.get('caption') or ''}\n{t.get('markdown') or ''}"
                if pat and not pat.search(hay):
                    continue
                out["table_hits"].append({
                    "source_id": sid,
                    "table_id": t["table_id"],
                    "paragraph_id": t.get("paragraph_id"),
                    "page": t.get("page") if has_pdf else None,
                    "caption": t.get("caption"),
                    "headers": t.get("headers"),
                    "num_rows": t.get("num_rows"),
                })

        if args.cues:
            cp = Path(d["_dir"]) / "kg_cues.json"
            if cp.exists():
                c_obj = json.loads(cp.read_text(encoding="utf-8"))
                out["cues"].append({
                    "source_id": sid,
                    "relation_cues": c_obj.get("relation_cues", [])[:args.limit],
                    "author_self_cues": c_obj.get("author_self_cues", [])[:args.limit],
                    "cited_third_party_cues": c_obj.get("cited_third_party_cues", [])[:args.limit],
                    "unavailable_material_refs": c_obj.get("unavailable_material_refs", [])[:args.limit],
                    "stat_anomalies": c_obj.get("stat_anomalies", []),
                })

        if args.formula_diffs:
            fp = Path(d["_dir"]) / "formula_diffs.json"
            if fp.exists():
                f_obj = json.loads(fp.read_text(encoding="utf-8"))
                out["formula_diffs"].append({"source_id": sid, "diff_count": len(f_obj), "diffs": f_obj[:args.limit]})

    txt = json.dumps(out, ensure_ascii=False, indent=2)
    while len(txt) > 18000 and len(out["block_hits"]) > 1:
        out["block_hits"].pop()
        out["truncated"] = True
        txt = json.dumps(out, ensure_ascii=False, indent=2)
    print(txt[:18000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
