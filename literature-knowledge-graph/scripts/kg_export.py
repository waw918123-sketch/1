#!/usr/bin/env python3
"""确定性图谱导出器：导出 knowledge_graph.json、GraphML (.graphml)、JSON-LD (.jsonld) 与 kg_report.md 并校验证书级格式合法性。"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _build_canonical_kg(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": "literature-knowledge-graph/v1",
        "case_id": result.get("case_id"),
        "status": result.get("status"),
        "papers": result.get("papers") or {},
        "nodes": result.get("nodes") or [],
        "edges": result.get("edges") or [],
        "entity_alignment": result.get("entity_alignment") or {"merged_candidates": [], "non_merge_records": []},
        "conflicts": result.get("conflicts") or {"intra_source_conflicts": [], "cross_paper_conflicts": []},
        "queries": result.get("queries") or [],
        "incremental_update": result.get("incremental_update") or {},
        "unavailable_materials": result.get("unavailable_materials") or [],
        "uncertainty": result.get("uncertainty") or [],
        "human_review_items": result.get("human_review_items") or [],
    }


def _build_graphml(kg: Dict[str, Any]) -> str:
    """构建标准 GraphML XML 字符串，支持 Gephi / Cytoscape / NetworkX 直接加载。"""
    ns = "http://graphml.graphdrawing.org/xmlns"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}graphml")

    node_keys = [
        ("n_label", "canonical_name", "string"),
        ("n_type", "entity_type", "string"),
        ("n_scope", "scope_level", "string"),
        ("n_sources", "source_ids", "string"),
        ("n_prov", "provenance_role", "string"),
        ("n_status", "status", "string"),
        ("n_aliases", "aliases", "string"),
        ("n_evidence", "evidence_summary", "string"),
    ]
    edge_keys = [
        ("e_rel", "relation_type", "string"),
        ("e_src_paper", "source_id", "string"),
        ("e_cond", "conditions", "string"),
        ("e_stat", "statistics", "string"),
        ("e_evtype", "evidence_type", "string"),
        ("e_prov", "provenance_role", "string"),
        ("e_conf", "confidence", "string"),
        ("e_evidence", "evidence_summary", "string"),
    ]
    for kid, kname, ktype in node_keys:
        ET.SubElement(root, f"{{{ns}}}key", id=kid, **{"for": "node", "attr.name": kname, "attr.type": ktype})
    for kid, kname, ktype in edge_keys:
        ET.SubElement(root, f"{{{ns}}}key", id=kid, **{"for": "edge", "attr.name": kname, "attr.type": ktype})

    graph_el = ET.SubElement(root, f"{{{ns}}}graph", id="literature_kg", edgedefault="directed")

    def _ev_str(anchors: List[Dict[str, Any]]) -> str:
        parts = []
        for a in (anchors or [])[:3]:
            if not isinstance(a, dict):
                continue
            loc = f"p.{a['page']}" if isinstance(a.get("page"), int) else f"pid={a.get('paragraph_id') or a.get('block_id')}"
            parts.append(f"[{a.get('source_id')}|{loc}] {str(a.get('quote') or '')[:140]}")
        return " ; ".join(parts)

    def _add_data(parent: ET.Element, key: str, val: Any) -> None:
        d = ET.SubElement(parent, f"{{{ns}}}data", key=key)
        d.text = "" if val is None else str(val)

    valid_nids = set()
    for n in kg.get("nodes") or []:
        if not isinstance(n, dict) or not n.get("node_id"):
            continue
        nid = str(n["node_id"])
        valid_nids.add(nid)
        nel = ET.SubElement(graph_el, f"{{{ns}}}node", id=nid)
        _add_data(nel, "n_label", n.get("canonical_name") or nid)
        _add_data(nel, "n_type", n.get("entity_type") or "concept")
        _add_data(nel, "n_scope", n.get("scope_level") or "single_paper_entity")
        _add_data(nel, "n_sources", ", ".join(str(s) for s in (n.get("source_ids") or [])))
        _add_data(nel, "n_prov", n.get("provenance_role") or "unreported")
        _add_data(nel, "n_status", n.get("status") or "verified")
        als = [a.get("surface_form") if isinstance(a, dict) else str(a) for a in (n.get("aliases") or [])]
        _add_data(nel, "n_aliases", "; ".join(x for x in als if x))
        _add_data(nel, "n_evidence", _ev_str(n.get("evidence_anchors") or []))

    for idx, e in enumerate(kg.get("edges") or [], 1):
        if not isinstance(e, dict):
            continue
        src = str(e.get("source_node_id") or "")
        tgt = str(e.get("target_node_id") or "")
        if src not in valid_nids or tgt not in valid_nids:
            continue
        eid = str(e.get("edge_id") or f"e_{idx:03d}")
        eel = ET.SubElement(graph_el, f"{{{ns}}}edge", id=eid, source=src, target=tgt)
        _add_data(eel, "e_rel", e.get("canonical_relation") or e.get("relation_type") or "not_reported")
        _add_data(eel, "e_src_paper", e.get("source_id") or "unreported")
        _add_data(eel, "e_cond", e.get("conditions") or "unreported")
        stat = e.get("statistics")
        _add_data(eel, "e_stat", json.dumps(stat, ensure_ascii=False) if isinstance(stat, (dict, list)) else (stat or "unreported"))
        _add_data(eel, "e_evtype", e.get("evidence_type") or "direct_evidence")
        _add_data(eel, "e_prov", e.get("provenance_role") or "author_self_statement")
        _add_data(eel, "e_conf", e.get("confidence") or "medium")
        _add_data(eel, "e_evidence", _ev_str(e.get("evidence_anchors") or []))

    if hasattr(ET, "indent"):
        ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def validate_graphml(xml_text: str) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    if not xml_text.strip():
        return False, ["GraphML output is empty."]
    try:
        root = ET.fromstring(xml_text)
        if not root.tag.endswith("graphml"):
            errors.append(f"Unexpected GraphML root tag: {root.tag}")
    except Exception as exc:
        errors.append(f"GraphML XML parse error: {exc}")
    return len(errors) == 0, errors


def _build_jsonld(kg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "@context": {
            "kg": "http://purl.org/literature-kg/vocab/",
            "prov": "http://www.w3.org/ns/prov#",
            "dct": "http://purl.org/dc/terms/",
            "cito": "http://purl.org/spar/cito/",
            "schema": "https://schema.org/",
        },
        "@type": "kg:LiteratureKnowledgeGraph",
        "case_id": kg.get("case_id"),
        "status": kg.get("status"),
        "papers": kg.get("papers"),
        "nodes": [
            {"@id": f"urn:kg:node:{n.get('node_id')}", "@type": f"kg:{n.get('entity_type', 'Entity')}", **n}
            for n in (kg.get("nodes") or []) if isinstance(n, dict)
        ],
        "edges": [
            {"@id": f"urn:kg:edge:{e.get('edge_id')}", "@type": "kg:DirectedRelation", **e}
            for e in (kg.get("edges") or []) if isinstance(e, dict)
        ],
        "entity_alignment": kg.get("entity_alignment"),
        "conflicts": kg.get("conflicts"),
        "queries": kg.get("queries"),
    }


def _fmt_anchor(a: Dict[str, Any]) -> str:
    loc = f"p.{a['page']}" if isinstance(a.get("page"), int) else f"pid=`{a.get('paragraph_id') or a.get('block_id')}`"
    cell = f" [{a.get('row_label')} | {a.get('col_header')} = {a.get('value')}]" if a.get("table_id") else ""
    return f"`{a.get('source_id')}` | {loc}{cell} | \"{str(a.get('quote') or '')[:160]}\""


def _build_markdown_report(kg: Dict[str, Any]) -> str:
    cid = kg.get("case_id") or "unknown"
    lines = [
        "---",
        f"case_id: {cid}",
        f"status: {kg.get('status')}",
        "tags: [literature-knowledge-graph, provenance, alignment, graph-queries]",
        "---",
        "",
        f"# Literature Knowledge Graph Report (`{cid}`)",
        "",
        "## 1. Independent Paper Metadata (`papers`)",
        "",
    ]
    for sid, meta in (kg.get("papers") or {}).items():
        if not isinstance(meta, dict):
            continue
        lines.append(
            f"- **`{sid}`**: {meta.get('title')} | Authors: `{meta.get('authors')}` | Year: `{meta.get('year')}` | "
            f"DOI: `{meta.get('doi')}` | License: `{meta.get('license')}` | Locator: `{meta.get('locator_mode')}` | `has_pdf_pages={meta.get('has_pdf_pages')}`"
        )
        if meta.get("companion_formats"):
            lines.append(f"  - Same-paper companion format(s): `{meta.get('companion_formats')}` (NOT cross-paper)")
    lines.append("")

    nodes = kg.get("nodes") or []
    edges = kg.get("edges") or []
    node_name = {n.get("node_id"): n.get("canonical_name") for n in nodes if isinstance(n, dict)}

    lines.extend([f"## 2. Entities (`nodes`: {len(nodes)})", ""])
    for n in nodes:
        if not isinstance(n, dict):
            continue
        lines.append(
            f"- **`{n.get('node_id')}`** `{n.get('canonical_name')}` (`{n.get('entity_type')}`, scope=`{n.get('scope_level')}`, "
            f"sources=`{n.get('source_ids')}`, role=`{n.get('provenance_role')}`, status=`{n.get('status')}`)"
        )
        for a in (n.get("evidence_anchors") or [])[:2]:
            if isinstance(a, dict):
                lines.append(f"  - Anchor: {_fmt_anchor(a)}")
    lines.append("")

    lines.extend([f"## 3. Evidenced Directed Relations (`edges`: {len(edges)})", ""])
    for e in edges:
        if not isinstance(e, dict):
            continue
        sn = node_name.get(e.get("source_node_id"), e.get("source_node_id"))
        tn = node_name.get(e.get("target_node_id"), e.get("target_node_id"))
        rel = e.get("canonical_relation") or e.get("relation_type")
        lines.append(
            f"- **`{e.get('edge_id')}`**: `{sn}` —[`{rel}`]→ `{tn}` (paper=`{e.get('source_id')}`, "
            f"conditions=`{e.get('conditions')}`, stats=`{e.get('statistics') or 'unreported'}`, "
            f"ev_type=`{e.get('evidence_type')}`, role=`{e.get('provenance_role')}`, conf=`{e.get('confidence')}`)"
        )
        for a in (e.get("evidence_anchors") or [])[:2]:
            if isinstance(a, dict):
                lines.append(f"  - Evidence: {_fmt_anchor(a)}")
    lines.append("")

    align = kg.get("entity_alignment") or {}
    lines.extend(["## 4. Cross-Paper Alignment & Forbidden Merges", ""])
    lines.append(f"### 4.1 Merged Candidates ({len(align.get('merged_candidates') or [])})")
    for mg in align.get("merged_candidates") or []:
        lines.append(f"- {json.dumps(mg, ensure_ascii=False)}")
    lines.extend(["", f"### 4.2 Non-Merge Records / Conflicts ({len(align.get('non_merge_records') or [])})"])
    for nm in align.get("non_merge_records") or []:
        lines.append(f"- {json.dumps(nm, ensure_ascii=False)}")
    lines.append("")

    queries = kg.get("queries") or []
    lines.extend([f"## 5. Executed Knowledge Graph Queries ({len(queries)})", ""])
    for q in queries:
        if not isinstance(q, dict):
            continue
        sg = q.get("result_subgraph") or {}
        lines.append(f"### `{q.get('query_id')}` — {q.get('question')} (`status={q.get('status')}`)")
        lines.append(f"- **Command**: `{q.get('executed_command')}`")
        lines.append(f"- **Summary**: {q.get('answer_summary')}")
        lines.append(f"- **Subgraph**: nodes={sg.get('node_ids')}, edges={sg.get('edge_ids')}")
        if q.get("indeterminate_relations"):
            lines.append(f"- **Indeterminate / Unresolved**: {json.dumps(q.get('indeterminate_relations'), ensure_ascii=False)}")
        lines.append("")

    if kg.get("conflicts"):
        lines.extend(["## 6. Conflicts & Contradictions", "", json.dumps(kg["conflicts"], ensure_ascii=False, indent=2), ""])
    if kg.get("unavailable_materials"):
        lines.extend(["## 7. Unavailable Materials (Not Inferred)", ""])
        for u in kg["unavailable_materials"][:10]:
            lines.append(f"- {json.dumps(u, ensure_ascii=False)}")
        lines.append("")
    if kg.get("uncertainty"):
        lines.extend(["## 8. Uncertainty & Unreported Limits", ""])
        for u in kg["uncertainty"]:
            lines.append(f"- {u}")
        lines.append("")
    if kg.get("human_review_items"):
        lines.extend(["## 9. Human Review Checklist", ""])
        for h in kg["human_review_items"]:
            lines.append(f"- [ ] {json.dumps(h, ensure_ascii=False)}")
        lines.append("")

    return "\n".join(lines)


def export_all(result: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    """写出 knowledge_graph.json、GraphML、JSON-LD 与 kg_report.md 并校验 GraphML XML。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    kg = _build_canonical_kg(result)

    kg_json_path = out_dir / "knowledge_graph.json"
    graphml_path = out_dir / "knowledge_graph.graphml"
    jsonld_path = out_dir / "knowledge_graph.jsonld"
    md_path = out_dir / "kg_report.md"

    kg_json_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")

    graphml_txt = _build_graphml(kg)
    gml_ok, gml_errs = validate_graphml(graphml_txt)
    graphml_path.write_text(graphml_txt, encoding="utf-8")

    jsonld_obj = _build_jsonld(kg)
    jsonld_path.write_text(json.dumps(jsonld_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path.write_text(_build_markdown_report(kg), encoding="utf-8")

    return {
        "knowledge_graph_json": str(kg_json_path),
        "graphml": str(graphml_path),
        "graphml_valid": gml_ok,
        "graphml_errors": gml_errs,
        "jsonld": str(jsonld_path),
        "markdown": str(md_path),
    }
