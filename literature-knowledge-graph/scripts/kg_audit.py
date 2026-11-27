#!/usr/bin/env python3
"""合并图谱补丁、保护既有元数据与增量 ID、执行 6 维严格图谱审计、执行图谱查询并导出可读文件。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.dont_write_bytecode = True

from doc_common import (
    collect_contract_keys,
    load_kg_vocab,
    normalize_doi,
    normalize_for_match,
    normalize_term_key,
    sha256_text,
)
from kg_export import export_all
from query_kg import execute_kg_query

NON_SIG_DIFF_RE = re.compile(
    r"\bno\s+(?:statistically\s+)?significant\s+differences?\b|\bp\s*(?:=|>|≥)\s*0?\.(?:0[5-9]|[1-9]\d*)\b|无显著差异",
    re.I,
)
EQUIVALENCE_CLAIM_RE = re.compile(
    r"\b(?:identical|same\s+as|equal(?:s)?\s+to|no\s+difference\s+exists|equivalent)\b|相同|完全一致|相等",
    re.I,
)
EXTRAPOLATION_METHOD_RE = re.compile(
    r"(?:extrapolat|higher[- ]taxon|indirect|assum|estimat|model|proxy|interpolat|推测|外推)",
    re.I,
)
NUM_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])((?:(?<!\d)-)?\d+(?:,\d{3})*(?:\.\d+)?)\b")


def _load_all_docs(work_dir: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    docs: Dict[str, Dict[str, Any]] = {}
    if work_dir and work_dir.exists():
        for dp in sorted(work_dir.glob("*/document.json")):
            d = json.loads(dp.read_text(encoding="utf-8"))
            sid = str(d.get("source_id") or dp.parent.name)
            d["_dir"] = str(dp.parent)
            docs[sid] = d
    return docs


def _enrich_anchors(
    anchors: List[Any],
    docs: Dict[str, Dict[str, Any]],
    fallback_sid: Optional[str] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    block_lookup = {sid: {b["block_id"]: b for b in d.get("blocks", [])} for sid, d in docs.items()}
    pid_lookup = {sid: {str(b.get("paragraph_id")): b for b in d.get("blocks", []) if b.get("paragraph_id")} for sid, d in docs.items()}
    table_lookup = {sid: {t["table_id"]: t for t in d.get("tables", [])} for sid, d in docs.items()}

    for raw_a in anchors or []:
        if not isinstance(raw_a, dict):
            continue
        anc = dict(raw_a)
        sid = str(anc.get("source_id") or anc.get("doc_id") or fallback_sid or (next(iter(docs)) if docs else "primary"))
        anc["source_id"] = sid
        d_obj = docs.get(sid)
        has_pdf = bool(d_obj.get("has_pdf_pages")) if d_obj else False

        bid = anc.get("block_id")
        pid = anc.get("paragraph_id")
        tid = anc.get("table_id")

        blk = None
        if bid and bid in block_lookup.get(sid, {}):
            blk = block_lookup[sid][bid]
        elif pid and str(pid) in pid_lookup.get(sid, {}):
            blk = pid_lookup[sid][str(pid)]
        if blk is not None:
            anc.setdefault("block_id", blk.get("block_id"))
            anc.setdefault("paragraph_id", blk.get("paragraph_id") or blk.get("block_id"))
            anc.setdefault("section_id", blk.get("section_id"))
            anc.setdefault("section_title", blk.get("section_title"))
            if has_pdf and anc.get("page") is None:
                anc["page"] = blk.get("page")
            if blk.get("xml_rich_text") and not anc.get("xml_verified_text"):
                anc["xml_verified_text"] = blk["xml_rich_text"]

        if tid and tid in table_lookup.get(sid, {}):
            tbl = table_lookup[sid][tid]
            anc.setdefault("paragraph_id", tbl.get("paragraph_id") or tid)
            anc.setdefault("section_id", tbl.get("section_id"))
            if has_pdf and anc.get("page") is None:
                anc["page"] = tbl.get("page")

        if not has_pdf and "page" not in anc:
            anc["page"] = None

        qtxt = str(anc.get("quote") or "")
        if qtxt:
            anc["quote_sha256"] = sha256_text(normalize_for_match(qtxt))[:16]
        out.append(anc)
    return out


def _merge_patch(
    result_path: Path,
    patch_path: Path,
    docs: Dict[str, Dict[str, Any]],
    work_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    res = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    if not isinstance(res, dict) or not isinstance(patch, dict):
        raise ValueError("Both result.json and patch.json must be JSON objects.")

    vocab = load_kg_vocab()
    ent_map = vocab.get("entity_alias_to_canonical") or {}
    rel_map = vocab.get("relation_alias_to_canonical") or {}
    locked_cid = res.get("case_id")
    locked_papers = res.get("papers")

    base_graph_locked = None
    if work_dir and (work_dir / "base_graph_locked.json").exists():
        base_graph_locked = json.loads((work_dir / "base_graph_locked.json").read_text(encoding="utf-8"))

    append_mode = bool(patch.get("append"))

    # 安全合并普通顶层字段，严禁 patch 覆盖已锁定的 case_id 与独立 papers 元数据
    for k, v in patch.items():
        if k in ("case_id",) and locked_cid:
            continue
        if k in ("papers", "pubinfo") and isinstance(locked_papers, dict) and locked_papers:
            continue
        if k in ("nodes", "entities", "edges", "relations", "triples", "queries", "query_specs", "entity_alignment", "conflicts", "append", "incremental_update"):
            continue
        if k == "sources" and isinstance(res.get("sources"), list) and isinstance(v, list):
            seen = {json.dumps(s, sort_keys=True, ensure_ascii=False) for s in res["sources"]}
            for item in v:
                sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if sig not in seen:
                    seen.add(sig)
                    res["sources"].append(item)
        elif k in ("uncertainty", "change_records", "human_review_items", "unavailable_materials") and isinstance(res.get(k), list) and isinstance(v, list):
            if k == "uncertainty" and len(res[k]) == 1 and "Initial deterministic corpus ingestion completed" in str(res[k][0]):
                res[k] = list(v)
            else:
                seen = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in res[k]}
                for item in v:
                    sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                    if sig not in seen:
                        seen.add(sig)
                        res[k].append(item)
        else:
            res[k] = v

    # 合并节点
    in_nodes = patch.get("nodes") if isinstance(patch.get("nodes"), list) else patch.get("entities")
    if isinstance(in_nodes, list):
        existing_nodes = list(res.get("nodes") or [])
        base_by_id = {str(n.get("node_id")): dict(n) for n in existing_nodes if isinstance(n, dict) and n.get("node_id")} if (append_mode or base_graph_locked) else {}
        merged_nodes_map: Dict[str, Dict[str, Any]] = dict(base_by_id)
        order_ids: List[str] = list(base_by_id.keys())

        for idx, raw_n in enumerate(in_nodes, 1):
            if not isinstance(raw_n, dict):
                continue
            n = json.loads(json.dumps(raw_n))
            nid = str(n.get("node_id") or n.get("id") or f"n_{len(order_ids) + idx:03d}")
            n["node_id"] = nid
            et = str(n.get("entity_type") or "concept").strip()
            n["entity_type"] = ent_map.get(normalize_term_key(et), et)
            n.setdefault("scope_level", "single_paper_entity")
            n.setdefault("provenance_role", "author_self_statement")
            n.setdefault("status", "verified")
            n.setdefault("aliases", [])
            anchors = n.get("evidence_anchors") or ([n["evidence"]] if isinstance(n.get("evidence"), dict) else [])
            sids = [str(s) for s in (n.get("source_ids") or ([n["source_id"]] if n.get("source_id") else [])) if s]
            n["evidence_anchors"] = _enrich_anchors(anchors, docs, fallback_sid=sids[0] if sids else None)
            if not sids:
                sids = list(dict.fromkeys(str(a.get("source_id")) for a in n["evidence_anchors"] if a.get("source_id")))
            n["source_ids"] = sids

            if nid in merged_nodes_map:
                old = merged_nodes_map[nid]
                # 增量合并时保留原 canonical_name 与 node_id，追加 aliases / source_ids / anchors
                old_sids = list(dict.fromkeys(list(old.get("source_ids") or []) + sids))
                old["source_ids"] = old_sids
                old_al = list(old.get("aliases") or [])
                seen_al = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in old_al}
                for al in n["aliases"]:
                    sig = json.dumps(al, sort_keys=True, ensure_ascii=False)
                    if sig not in seen_al:
                        seen_al.add(sig)
                        old_al.append(al)
                old["aliases"] = old_al
                old_anc = list(old.get("evidence_anchors") or [])
                seen_anc = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in old_anc}
                for a in n["evidence_anchors"]:
                    sig = json.dumps(a, sort_keys=True, ensure_ascii=False)
                    if sig not in seen_anc:
                        seen_anc.add(sig)
                        old_anc.append(a)
                old["evidence_anchors"] = old_anc
            else:
                merged_nodes_map[nid] = n
                order_ids.append(nid)

        res["nodes"] = [merged_nodes_map[nid] for nid in order_ids]

    # 合并边
    in_edges = patch.get("edges") if isinstance(patch.get("edges"), list) else (patch.get("relations") if isinstance(patch.get("relations"), list) else patch.get("triples"))
    if isinstance(in_edges, list):
        existing_edges = list(res.get("edges") or []) if (append_mode or base_graph_locked) else []
        edge_map: Dict[str, Dict[str, Any]] = {str(e.get("edge_id")): dict(e) for e in existing_edges if isinstance(e, dict) and e.get("edge_id")}
        e_order: List[str] = list(edge_map.keys())

        for idx, raw_e in enumerate(in_edges, 1):
            if not isinstance(raw_e, dict):
                continue
            e = json.loads(json.dumps(raw_e))
            eid = str(e.get("edge_id") or e.get("id") or f"e_{len(e_order) + idx:03d}")
            e["edge_id"] = eid
            rt = str(e.get("relation_type") or e.get("predicate") or "").strip()
            e["relation_type"] = rt
            can_r = rel_map.get(normalize_term_key(rt))
            if can_r:
                e["canonical_relation"] = can_r
            e.setdefault("direction", "directed")
            e.setdefault("conditions", "unreported")
            e.setdefault("statistics", "unreported")
            e.setdefault("evidence_type", "direct_evidence")
            e.setdefault("provenance_role", "author_self_statement")
            e.setdefault("confidence", "medium")
            e.setdefault("confidence_basis", "Directly grounded in cited verbatim excerpt.")
            anchors = e.get("evidence_anchors") or ([e["evidence"]] if isinstance(e.get("evidence"), dict) else [])
            e["evidence_anchors"] = _enrich_anchors(anchors, docs, fallback_sid=e.get("source_id"))
            if not e.get("source_id") and e["evidence_anchors"]:
                sids = list(dict.fromkeys(str(a.get("source_id")) for a in e["evidence_anchors"] if a.get("source_id")))
                e["source_id"] = sids[0] if len(sids) == 1 else "cross_paper"
            if eid not in edge_map:
                e_order.append(eid)
            edge_map[eid] = e
        res["edges"] = [edge_map[eid] for eid in e_order]

    # 合并实体对齐与禁止合并记录
    in_align = patch.get("entity_alignment")
    if isinstance(in_align, dict):
        cur_al = res.setdefault("entity_alignment", {"merged_candidates": [], "non_merge_records": []})
        for subk in ("merged_candidates", "non_merge_records"):
            if isinstance(in_align.get(subk), list):
                cur_al[subk] = list(in_align[subk]) if not append_mode else (cur_al.get(subk, []) + in_align[subk])

    # 合并冲突记录
    in_cf = patch.get("conflicts")
    if isinstance(in_cf, dict):
        cur_cf = res.setdefault("conflicts", {"intra_source_conflicts": [], "cross_paper_conflicts": []})
        for subk in ("intra_source_conflicts", "cross_paper_conflicts"):
            if isinstance(in_cf.get(subk), list):
                cur_cf[subk] = list(in_cf[subk]) if not append_mode else (cur_cf.get(subk, []) + in_cf[subk])
    elif isinstance(in_cf, list):
        res["conflicts"] = in_cf

    # 合并增量更新记录
    if base_graph_locked:
        pres_nids = set(base_graph_locked.get("preserved_node_ids") or [])
        pres_eids = set(base_graph_locked.get("preserved_edge_ids") or [])
        cur_nids = {n.get("node_id") for n in (res.get("nodes") or []) if isinstance(n, dict)}
        cur_eids = {e.get("edge_id") for e in (res.get("edges") or []) if isinstance(e, dict)}
        inc = res.setdefault("incremental_update", {})
        if isinstance(patch.get("incremental_update"), dict):
            inc.update(patch["incremental_update"])
        inc["enabled"] = True
        inc["base_graph_preserved_ids"] = {"node_ids": sorted(pres_nids), "edge_ids": sorted(pres_eids)}
        inc["added_nodes"] = sorted(cur_nids - pres_nids)
        inc["added_edges"] = sorted(cur_eids - pres_eids)
        inc["id_stability_verified"] = pres_nids.issubset(cur_nids) and pres_eids.issubset(cur_eids)

    # 执行查询规格（query_specs 或 queries 中含 query_type 的结构化查询）
    in_qspecs = patch.get("query_specs") or patch.get("queries")
    if isinstance(in_qspecs, list) and in_qspecs:
        executed_queries: List[Dict[str, Any]] = []
        for qi, qs in enumerate(in_qspecs, 1):
            if not isinstance(qs, dict):
                continue
            if qs.get("query_type"):
                q_copy = dict(qs)
                q_copy.setdefault("query_id", f"q_{qi:03d}")
                executed_queries.append(execute_kg_query(res, q_copy))
            else:
                executed_queries.append(qs)
        res["queries"] = executed_queries

    result_path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def _check_table_cell(item: Dict[str, Any], tbl: Dict[str, Any]) -> Optional[str]:
    val = item.get("value") if item.get("value") is not None else item.get("reported_value")
    if val is None or isinstance(val, (dict, list, bool)):
        return None
    norm_val = normalize_for_match(str(val))
    rows = tbl.get("rows") or []
    if not norm_val or not rows:
        return None
    n_hdr = int(tbl.get("header_rows") or 1)
    headers = tbl.get("headers") or rows[0]
    row_sel = item.get("row_label") or item.get("row_key")
    col_sel = item.get("col_header") or item.get("col_name")
    if row_sel is None and col_sel is None:
        return None

    r_ids = [
        i for i, r in enumerate(rows)
        if i >= n_hdr and (row_sel is None or normalize_for_match(str(row_sel)) in normalize_for_match(" ".join(r[:2])))
    ]
    ncols = max((len(r) for r in rows), default=0)
    c_ids = [
        c for c in range(ncols)
        if col_sel is None or normalize_for_match(str(col_sel)) in normalize_for_match(headers[c] if c < len(headers) else "")
    ]
    if not r_ids or not c_ids:
        return f"row_label={row_sel!r} or col_header={col_sel!r} not found in {tbl['table_id']}"
    cells = [normalize_for_match(rows[r][c]) for r in r_ids for c in c_ids if c < len(rows[r])]
    if any(norm_val in ct for ct in cells):
        return None
    return f"value {val!r} not found at cited cell row={row_sel!r}, col={col_sel!r} in {tbl['table_id']} (actual cell(s): {cells[:2]})"


def _sync_contract_aliases(res: Dict[str, Any], req_fields: List[str]) -> None:
    alias_map = {
        "entities": res.get("nodes"),
        "relations": res.get("edges"),
        "triples": res.get("edges"),
        "pubinfo": res.get("papers"),
        "corpus_sources": res.get("papers"),
        "graph_queries": res.get("queries"),
        "query_results": res.get("queries"),
        "alignment": res.get("entity_alignment"),
        "entity_merges": (res.get("entity_alignment") or {}).get("merged_candidates"),
        "non_merge_records": (res.get("entity_alignment") or {}).get("non_merge_records"),
        "knowledge_graph": {
            "papers": res.get("papers"),
            "nodes": res.get("nodes"),
            "edges": res.get("edges"),
            "entity_alignment": res.get("entity_alignment"),
            "conflicts": res.get("conflicts"),
            "queries": res.get("queries"),
        },
    }
    for f in req_fields:
        if f in alias_map and alias_map[f] is not None:
            res[f] = alias_map[f]


def audit_result(
    result_path: Path,
    case_path: Optional[Path],
    work_dir: Optional[Path],
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    checks_summary = {"nodes_checked": 0, "edges_checked": 0, "anchors_checked": 0, "table_cells_verified": 0, "queries_checked": 0}

    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        return {"passed": False, "error_count": 1, "warning_count": 0, "errors": ["result.json root must be a JSON object"], "warnings": []}

    raw_status = str(result.get("status") or "").strip().lower()
    if raw_status in ("todo", "placeholder", "nonsense", ""):
        errors.append(f"invalid result status: {raw_status!r}")

    req_fields: List[str] = []
    if case_path and case_path.exists():
        case_data = json.loads(case_path.read_text(encoding="utf-8"))
        exp_cid = case_data.get("case_id")
        if exp_cid and result.get("case_id") != exp_cid:
            errors.append(f"case_id mismatch: expected {exp_cid!r}, got {result.get('case_id')!r}")
        req_fields = collect_contract_keys(case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output"))
        if req_fields:
            _sync_contract_aliases(result, req_fields)
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        for f in req_fields:
            if f not in result:
                errors.append(f"missing required output_contract field: {f!r}")
            elif result[f] is None or result[f] == "":
                warnings.append(f"required field {f!r} is empty/null (ensure uncertainty explains why)")

    docs = _load_all_docs(work_dir)
    doc_indices: Dict[str, Dict[str, Any]] = {}
    for sid, d in docs.items():
        has_pdf = bool(d.get("has_pdf_pages"))
        norm_blocks: Dict[str, str] = {}
        norm_pages: Dict[int, str] = {}
        bmap, pmap, tmap = {}, {}, {}
        for b in d.get("blocks", []):
            bid = b["block_id"]
            pid = str(b.get("paragraph_id") or bid)
            bmap[bid] = b
            pmap[pid] = b
            full_t = normalize_for_match(b.get("raw_text", "") + "\n" + b.get("text", "") + "\n" + b.get("xml_rich_text", ""))
            norm_blocks[bid] = full_t
            norm_blocks[pid] = full_t
            if has_pdf and isinstance(b.get("page"), int):
                pg = int(b["page"])
                norm_pages[pg] = norm_pages.get(pg, "") + " " + full_t
        for t in d.get("tables", []):
            tmap[t["table_id"]] = t
            if t.get("native_table_id"):
                tmap[t["native_table_id"]] = t
            t_txt = normalize_for_match(" ".join(" ".join(r) for r in t.get("rows", [])))
            if has_pdf and isinstance(t.get("page"), int):
                pg = int(t["page"])
                norm_pages[pg] = norm_pages.get(pg, "") + " " + t_txt
            norm_blocks[t["table_id"]] = t_txt
            if t.get("paragraph_id"):
                norm_blocks[str(t["paragraph_id"])] = t_txt
        doc_indices[sid] = {
            "has_pdf_pages": has_pdf,
            "page_count": int(d.get("page_count") or 0) if has_pdf else 0,
            "norm_pages": norm_pages,
            "norm_blocks": norm_blocks,
            "blocks": bmap,
            "paragraphs": pmap,
            "tables": tmap,
            "full_norm": " ".join(norm_blocks.values()),
            "companion_formats": d.get("companion_formats", []),
        }

    # 1. 元数据隔离核验（防止第一篇 DOI/title 套到第二篇）
    papers_res = result.get("papers") or {}
    if len(docs) >= 2 and isinstance(papers_res, dict) and len(papers_res) >= 2:
        sids = list(docs.keys())
        p1_doi = normalize_doi((papers_res.get(sids[0]) or {}).get("doi"))
        p2_doi = normalize_doi((papers_res.get(sids[1]) or {}).get("doi"))
        true_p2_doi = normalize_doi((docs[sids[1]].get("metadata") or {}).get("doi"))
        if p1_doi and p2_doi and p1_doi == p2_doi and true_p2_doi and true_p2_doi != p1_doi:
            errors.append(f"metadata leakage: {sids[1]} carries primary DOI {p2_doi!r} instead of its own {true_p2_doi!r}.")

    vocab = load_kg_vocab()
    ent_types = set((vocab.get("entity_types") or {}).keys())
    rel_map = vocab.get("relation_alias_to_canonical") or {}
    rel_meta = vocab.get("relation_types") or {}
    raw_vague = vocab.get("vague_or_forbidden_relations") or []
    vague_rels = {str(x).lower() for x in raw_vague} | {normalize_term_key(str(x)) for x in raw_vague}
    raw_forb_ev = vocab.get("forbidden_evidence_types") or []
    forb_evtypes = {str(x).lower() for x in raw_forb_ev} | {normalize_term_key(str(x)) for x in raw_forb_ev}

    nodes = result.get("nodes") or []
    edges = result.get("edges") or []
    if raw_status == "completed" and (not nodes or not edges):
        errors.append("status='completed' requires non-empty nodes[] and edges[].")

    def _verify_anchor(anc: Dict[str, Any], ctx: str) -> str:
        checks_summary["anchors_checked"] += 1
        sid = str(anc.get("source_id") or "")
        pid = anc.get("paragraph_id")
        bid = anc.get("block_id")
        tid = anc.get("table_id")
        pg = anc.get("page")
        quote = str(anc.get("quote") or "").strip()

        if doc_indices and sid not in doc_indices:
            errors.append(f"{ctx}: unknown source_id {sid!r} (known: {list(doc_indices.keys())}).")
            return ""
        d_idx = doc_indices.get(sid)
        if not d_idx:
            return normalize_for_match(quote)

        # 非 PDF 语料严禁自造页码 1
        if not d_idx["has_pdf_pages"]:
            if isinstance(pg, int) or (isinstance(pg, str) and pg.strip().isdigit()):
                errors.append(f"{ctx}: non-PDF source {sid!r} has forbidden page={pg!r}; use paragraph_id and set page=null.")
            if not pid and not bid and not tid:
                errors.append(f"{ctx}: missing paragraph_id/block_id/table_id for non-PDF source {sid!r}.")
        else:
            if pg is not None and (not isinstance(pg, int) or not (1 <= pg <= d_idx["page_count"])):
                errors.append(f"{ctx}: PDF page {pg!r} out of range [1, {d_idx['page_count']}] in {sid!r}.")

        if tid is not None:
            if tid not in d_idx["tables"]:
                errors.append(f"{ctx}: table_id {tid!r} not found in {sid!r}.")
            else:
                cerr = _check_table_cell(anc, d_idx["tables"][tid])
                if cerr:
                    errors.append(f"{ctx}: {cerr}")
                elif anc.get("value") is not None and (anc.get("row_label") or anc.get("col_header")):
                    checks_summary["table_cells_verified"] += 1

        loc_key = str(pid or bid or tid or "")
        if pid and str(pid) not in d_idx["paragraphs"] and str(pid) not in d_idx["norm_blocks"]:
            errors.append(f"{ctx}: paragraph_id {pid!r} not found in {sid!r}.")
        elif bid and bid not in d_idx["blocks"] and bid not in d_idx["norm_blocks"]:
            errors.append(f"{ctx}: block_id {bid!r} not found in {sid!r}.")

        if quote:
            nq = normalize_for_match(quote)
            if len(nq) >= 8:
                hay = d_idx["norm_blocks"].get(loc_key) or (d_idx["norm_pages"].get(pg) if isinstance(pg, int) else "") or d_idx["full_norm"]
                if nq not in hay and nq not in d_idx["full_norm"]:
                    errors.append(f"{ctx}: quote not matched verbatim in {sid!r} ({loc_key}): {nq[:60]!r}")
        else:
            errors.append(f"{ctx}: empty verbatim quote in evidence_anchors.")
        return normalize_for_match(quote)

    # 2. 审计节点（抽得准、对得齐、位置精确）
    node_by_id: Dict[str, Dict[str, Any]] = {}
    seen_intra_names: Dict[Tuple[str, str], str] = {}
    for ni, n in enumerate(nodes):
        checks_summary["nodes_checked"] += 1
        if not isinstance(n, dict) or not n.get("node_id"):
            errors.append(f"nodes[{ni}] missing node_id.")
            continue
        nid = str(n["node_id"])
        node_by_id[nid] = n
        cname = str(n.get("canonical_name") or "").strip()
        etype = str(n.get("entity_type") or "").strip()
        if not cname:
            errors.append(f"node {nid}: missing canonical_name.")
        if etype not in ent_types:
            warnings.append(f"node {nid}: entity_type {etype!r} outside controlled taxonomy {sorted(ent_types)}.")
        sids = [str(s) for s in (n.get("source_ids") or []) if s]
        scope = str(n.get("scope_level") or "")
        if len(sids) >= 2 and scope != "cross_paper_concept" and etype in ("material", "method", "dataset", "condition"):
            warnings.append(
                f"node {nid} ({cname!r}, type={etype}) spans multiple papers {sids} without scope_level='cross_paper_concept'; "
                "separate paper-scoped operational instances or document merge_basis vs non_merge_records."
            )
        anchors = n.get("evidence_anchors") or []
        if not anchors and scope != "cross_paper_concept":
            errors.append(f"node {nid}: missing evidence_anchors[].")
        for ai, anc in enumerate(anchors):
            if isinstance(anc, dict):
                _verify_anchor(anc, f"node {nid}.evidence_anchors[{ai}]")
        for sid in sids:
            k = (sid, normalize_term_key(cname))
            if k in seen_intra_names and seen_intra_names[k] != nid:
                warnings.append(f"Duplicate entity {cname!r} inside {sid!r}: {seen_intra_names[k]} and {nid}; merge or explain in non_merge_records.")
            else:
                seen_intra_names[k] = nid

    # 3. 审计边（连得对、方向、条件、反共现、反跨篇结果拼接）
    for ei, e in enumerate(edges):
        checks_summary["edges_checked"] += 1
        if not isinstance(e, dict):
            errors.append(f"edges[{ei}] must be an object.")
            continue
        eid = str(e.get("edge_id") or f"e_{ei + 1}")
        src_id = str(e.get("source_node_id") or "")
        tgt_id = str(e.get("target_node_id") or "")
        if src_id not in node_by_id:
            errors.append(f"edge {eid}: dangling source_node_id {src_id!r}.")
        if tgt_id not in node_by_id:
            errors.append(f"edge {eid}: dangling target_node_id {tgt_id!r}.")

        raw_rel = str(e.get("relation_type") or "").strip()
        rkey = normalize_term_key(raw_rel)
        can_rel = rel_map.get(rkey)
        if rkey in vague_rels:
            errors.append(f"edge {eid}: forbidden vague/co-occurrence relation_type {raw_rel!r}.")
        elif not can_rel:
            warnings.append(f"edge {eid}: relation_type {raw_rel!r} outside kg-vocab.json.")

        ev_type = str(e.get("evidence_type") or "").strip().lower()
        if ev_type in forb_evtypes or normalize_term_key(ev_type) in forb_evtypes:
            errors.append(f"edge {eid}: forbidden evidence_type {ev_type!r} (co-occurrence/speculation is not direct evidence).")

        anchors = e.get("evidence_anchors") or []
        if not anchors:
            errors.append(f"edge {eid}: missing evidence_anchors[].")
        anchor_corpus = ""
        anchor_sids: Set[str] = set()
        for ai, anc in enumerate(anchors):
            if isinstance(anc, dict):
                anchor_sids.add(str(anc.get("source_id") or ""))
                anchor_corpus += " " + _verify_anchor(anc, f"edge {eid}.evidence_anchors[{ai}]")
                if anc.get("value") is not None:
                    anchor_corpus += " " + str(anc["value"])

        # 防跨篇条件与数值混合拼接
        if len(anchor_sids) >= 2 and str(e.get("source_id") or "") != "cross_paper":
            errors.append(f"edge {eid}: single-paper edge mixes anchors from multiple papers {sorted(anchor_sids)}; separate per-paper edges.")
        if len(anchor_sids) >= 2 and can_rel not in ("supports", "contradicts", "extends", "compares_with", "instance_of"):
            errors.append(f"edge {eid} ({can_rel}): cross-paper relation cannot fuse two papers' measurements/conditions into one non-epistemic edge.")

        # 统计/因果/数值落地校验
        if NON_SIG_DIFF_RE.search(anchor_corpus) and (EQUIVALENCE_CLAIM_RE.search(raw_rel) or EQUIVALENCE_CLAIM_RE.search(str(e.get("conditions") or ""))):
            errors.append(f"edge {eid}: statistical overreach — non-significant difference cannot be written as equivalence.")
        design = str(e.get("study_design") or "").lower()
        if can_rel and can_rel in rel_meta:
            forb = set(rel_meta[can_rel].get("forbidden_study_designs") or [])
            if design and design in forb:
                errors.append(f"edge {eid}: causal overreach — {can_rel!r} forbidden with study_design={design!r}.")
        if str(e.get("confidence") or "").lower() == "high" and (ev_type == "indirect_inference" or EXTRAPOLATION_METHOD_RE.search(ev_type)):
            errors.append(f"edge {eid}: confidence='high' forbidden for indirect_inference/extrapolation.")
        stat_str = json.dumps(e.get("statistics"), ensure_ascii=False) if isinstance(e.get("statistics"), (dict, list)) else str(e.get("statistics") or "")
        if stat_str and stat_str.lower() != "unreported" and anchors:
            stat_nums = [m.replace(",", "") for m in NUM_TOKEN_RE.findall(stat_str)]
            corp_clean = anchor_corpus.replace(",", "")
            for num in stat_nums:
                if num not in corp_clean:
                    errors.append(f"edge {eid}: statistics cites numeric value {num!r} absent from bound evidence_anchors.")

    # 4. 审计跨篇对齐与禁止合并记录
    align = result.get("entity_alignment") or {}
    if len(docs) >= 2 and raw_status == "completed":
        if not (align.get("non_merge_records") or []):
            warnings.append("Multi-paper corpus has empty entity_alignment.non_merge_records[]; document shared generic terms or distinct objects that must NOT be merged.")

    # 5. 审计实际执行的图谱查询 (>= 3 条且子图 ID 全部存在)
    queries = result.get("queries") or []
    if raw_status == "completed" and len(queries) < 3:
        errors.append(f"status='completed' requires at least 3 executed graph queries in queries[] (found {len(queries)}).")
    for qi, q in enumerate(queries):
        checks_summary["queries_checked"] += 1
        if not isinstance(q, dict):
            errors.append(f"queries[{qi}] must be an object.")
            continue
        sg = q.get("result_subgraph") or {}
        for nid in sg.get("node_ids") or []:
            if nid not in node_by_id:
                errors.append(f"queries[{qi}].result_subgraph references unknown node_id {nid!r}.")
        edge_ids_set = {str(e.get("edge_id")) for e in edges if isinstance(e, dict)}
        for eid in sg.get("edge_ids") or []:
            if eid not in edge_ids_set:
                errors.append(f"queries[{qi}].result_subgraph references unknown edge_id {eid!r}.")
        if "indeterminate_relations" not in q:
            warnings.append(f"queries[{qi}] missing 'indeterminate_relations' field.")

    # 6. 增量更新 ID 稳定性校验
    if work_dir and (work_dir / "base_graph_locked.json").exists():
        bgl = json.loads((work_dir / "base_graph_locked.json").read_text(encoding="utf-8"))
        pres_n = set(bgl.get("preserved_node_ids") or [])
        pres_e = set(bgl.get("preserved_edge_ids") or [])
        cur_n = set(node_by_id.keys())
        cur_e = {str(e.get("edge_id")) for e in edges if isinstance(e, dict)}
        missing_n = sorted(pres_n - cur_n)
        missing_e = sorted(pres_e - cur_e)
        if missing_n:
            errors.append(f"Incremental update broke node_id stability; missing preserved IDs: {missing_n}")
        if missing_e:
            errors.append(f"Incremental update broke edge_id stability; missing preserved IDs: {missing_e}")

    if req_fields:
        _sync_contract_aliases(result, req_fields)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "passed": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "checks_summary": checks_summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge KG patch, execute queries, strictly audit 6 pillars, and export GraphML/JSON-LD/MD.")
    parser.add_argument("--result", required=True)
    parser.add_argument("--merge", default=None)
    parser.add_argument("--case", default=None)
    parser.add_argument("--work-dir", default="/app/work")
    parser.add_argument("--export-dir", default=None)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    res_p = Path(args.result)
    w_dir = Path(args.work_dir) if args.work_dir else None
    docs = _load_all_docs(w_dir)

    if args.merge:
        _merge_patch(res_p, Path(args.merge), docs, work_dir=w_dir)

    report = audit_result(res_p, Path(args.case) if args.case else None, w_dir)

    if res_p.exists():
        res_obj = json.loads(res_p.read_text(encoding="utf-8"))
        exp_dir = Path(args.export_dir) if args.export_dir else res_p.parent
        exp_info = export_all(res_obj, exp_dir)
        if not exp_info["graphml_valid"]:
            report["errors"].extend(exp_info["graphml_errors"])
            report["error_count"] = len(report["errors"])
            report["passed"] = False
        res_obj["exports"] = exp_info
        res_p.write_text(json.dumps(res_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        report["exports"] = exp_info

    print(json.dumps(report, ensure_ascii=False, indent=2)[:18000])
    if args.strict and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
