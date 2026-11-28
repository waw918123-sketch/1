#!/usr/bin/env python3
"""自包含合成对照基准测试：全面校验本 Skill 相对于朴素基线在阅读15核心陷阱上的拦截与交付能力。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

sys.dont_write_bytecode = True

from doc_common import discover_capabilities, sha256_file
from kg_audit import _merge_patch, audit_result
from kg_export import export_all
from prep_kg import run_prep
from query_kg import execute_kg_query

ROOT_CASE_JSON = {
    "case_id": "SYNTHETIC_KG_BENCH_01",
    "title": "Primary Synthetic Paper Title (MUST NOT LEAK TO SECONDARY)",
    "doi": "10.9999/synth.primary.2025",
    "authors": ["Lin Wei", "Alex Carter"],
    "output_contract": {
        "required_fields": [
            "case_id", "status", "papers", "nodes", "edges",
            "entity_alignment", "conflicts", "queries", "sources", "uncertainty", "exports",
        ]
    },
}

PRIMARY_JSON = {
    "source_id": "primary",
    "title": "Primary Synthetic Paper Title (MUST NOT LEAK TO SECONDARY)",
    "doi": "10.9999/synth.primary.2025",
    "authors": ["Lin Wei", "Alex Carter"],
    "year": "2025",
    "license": "CC-BY-4.0",
    "paragraphs": [
        {
            "paragraph_id": "p-prim-1",
            "section_title": "1. Introduction",
            "text": "Prior studies by Park et al. (2022) reported catalyst sintering above 723 K [1].",
        },
        {
            "paragraph_id": "p-prim-2",
            "section_title": "2. Results",
            "text": "We prepared Ni-Cat-BatchA (10 wt% Ni/Al2O3) and measured 84.2% CO2 conversion at 623 K and 1.0 bar (see Fig. 1 and Supplementary Table S1). Using density functional theory (DFT) with PBE, the activation barrier was 0.68 eV.",
        },
        {
            "paragraph_id": "p-prim-3",
            "section_title": "2. Results",
            "text": "No statistically significant difference in stability was observed between fresh and aged BatchA (p = 0.42).",
        },
    ],
}

SECONDARY_JSON = {
    "source_id": "secondary",
    "title": "Secondary Synthetic Cobalt Catalysis Study",
    "doi": "10.9999/synth.secondary.2026",
    "authors": ["Maria Gomez", "Kenji Sato"],
    "year": "2026",
    "license": "CC-BY-4.0",
    "paragraphs": [
        {
            "paragraph_id": "p-sec-1",
            "section_title": "2. Methods",
            "text": "We tested Co-Ni-FlowB (5 wt% Co-10 wt% Ni/CeO2) at 573 K and 0.5 bar with residence parameter 10-5 s-1 and obtained 79.1% CO2 conversion (R2 = 0.994).",
        },
        {
            "paragraph_id": "p-sec-2",
            "section_title": "2. Methods",
            "text": "Unpromoted commercial Ni/Al2O3 pellet Lot-Z achieved 61.0% CO2 conversion at 573 K and 0.5 bar. Density functional theory (DFT, RPBE+U) yielded an apparent barrier of 0.81 eV.",
        },
    ],
}

SECONDARY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<article>
  <front>
    <article-meta>
      <article-id pub-id-type="doi">10.9999/synth.secondary.2026</article-id>
      <title-group><article-title>Secondary Synthetic Cobalt Catalysis Study</article-title></title-group>
      <contrib-group>
        <contrib contrib-type="author"><name><surname>Gomez</surname><given-names>Maria</given-names></name></contrib>
        <contrib contrib-type="author"><name><surname>Sato</surname><given-names>Kenji</given-names></name></contrib>
        <contrib contrib-type="editor"><name><surname>WrongEditor</surname><given-names>Eve</given-names></name></contrib>
      </contrib-group>
      <pub-date pub-type="epub"><year>2026</year></pub-date>
    </article-meta>
  </front>
  <body>
    <sec id="s2"><title>2. Methods</title>
      <p id="p-sec-1">We tested Co-Ni-FlowB (5 wt% Co-10 wt% Ni/CeO<sub>2</sub>) at 573 K and 0.5 bar with residence parameter 10<sup>-5</sup> s<sup>-1</sup> and obtained 79.1% CO<sub>2</sub> conversion (R<sup>2</sup> = 0.994).</p>
      <p id="p-sec-2">Unpromoted commercial Ni/Al<sub>2</sub>O<sub>3</sub> pellet Lot-Z achieved 61.0% CO<sub>2</sub> conversion at 573 K and 0.5 bar. Density functional theory (DFT, RPBE+U) yielded an apparent barrier of 0.81 eV.</p>
    </sec>
  </body>
</article>
"""

MARKDOWN_PAPER = """# Synthetic Markdown Validation Study

| Catalyst | Earth Yield | Ocean Yield |
| --- | --- | --- |
| Cat-M1 | 91.5% | 44.2% |
"""


def run_benchmark() -> Dict[str, Any]:
    checks: Dict[str, bool] = {}
    caps = discover_capabilities()

    with tempfile.TemporaryDirectory(prefix="kg_bench_") as tmp:
        root = Path(tmp)
        in_dir, work_dir, out_dir = root / "input", root / "work", root / "output"
        in_dir.mkdir(parents=True)
        out_dir.mkdir(parents=True)

        (in_dir / "case.json").write_text(json.dumps(ROOT_CASE_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
        (in_dir / "task.md").write_text("# Task\nQ1: Compare DFT across papers.\nBuild KG and run 3 queries.", encoding="utf-8")
        p1_path = in_dir / "primary-material.json"
        p2_path = in_dir / "secondary-material.json"
        x2_path = in_dir / "secondary-publisher.xml"
        p1_path.write_text(json.dumps(PRIMARY_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
        p2_path.write_text(json.dumps(SECONDARY_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
        x2_path.write_text(SECONDARY_XML, encoding="utf-8")

        manifest = {
            "papers": [
                {"source_id": "primary", "file": "primary-material.json", "sha256": sha256_file(p1_path), "doi": PRIMARY_JSON["doi"]},
                {"source_id": "secondary", "file": "secondary-material.json", "xml_file": "secondary-publisher.xml", "sha256": sha256_file(p2_path), "doi": SECONDARY_JSON["doi"]},
            ]
        }
        (in_dir / "corpus-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        material_idx = [
            {"source_id": "primary", "paragraph_id": "p-prim-2", "text": PRIMARY_JSON["paragraphs"][1]["text"]},
            {"source_id": "secondary", "paragraph_id": "p-sec-1", "text": SECONDARY_JSON["paragraphs"][0]["text"]},
        ]
        (in_dir / "material.json").write_text(json.dumps(material_idx, ensure_ascii=False, indent=2), encoding="utf-8")

        res_p = out_dir / "result.json"
        brief = run_prep(in_dir, work_dir, init_result=res_p)

        # 1. 验证多文件语料身份解析：paper_count 必须为 2（material.json 和 secondary-publisher.xml 不得算成第3/4篇）
        checks["corpus_dedup_exact_two_papers"] = brief["paper_count"] == 2
        checks["material_index_not_separate_paper"] = (brief.get("material_index_audit") or {}).get("treated_as_separate_paper") is False
        checks["companion_xml_deduped_under_secondary"] = len(brief.get("same_paper_format_dedups") or []) == 1
        checks["manifest_hashes_verified"] = all(h.get("hash_match") is True for h in brief.get("manifest_hash_checks") or [])

        # 2. 验证元数据隔离 + 非PDF无页码 + XML公式差异检测 + 不可用材料边界
        init_res = json.loads(res_p.read_text(encoding="utf-8"))
        sec_meta = init_res["papers"]["secondary"]
        checks["root_doi_not_leaked_to_secondary"] = sec_meta["doi"] == "10.9999/synth.secondary.2026"
        checks["non_pdf_has_no_fake_pages"] = sec_meta["has_pdf_pages"] is False
        sec_diffs = json.loads((work_dir / "secondary" / "formula_diffs.json").read_text(encoding="utf-8"))
        checks["xml_linearized_math_diffs_detected"] = any("10^{-5}" in d["xml_rich_text"] and "R^{2}" in d["xml_rich_text"] for d in sec_diffs)
        checks["unavailable_materials_flagged"] = len(init_res.get("unavailable_materials") or []) >= 1

        # 3. 构造严格合规的 KG 补丁并完成合并、查询执行与导出
        valid_patch = {
            "status": "completed",
            "nodes": [
                {
                    "node_id": "n_001", "canonical_name": "Ni-Cat-BatchA (primary)", "entity_type": "material",
                    "scope_level": "paper_scoped_instance", "source_ids": ["primary"], "provenance_role": "author_self_statement",
                    "aliases": [{"surface_form": "Ni-Cat-BatchA", "source_id": "primary", "paragraph_id": "p-prim-2"}],
                    "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-2", "page": None, "quote": "We prepared Ni-Cat-BatchA (10 wt% Ni/Al2O3) and measured 84.2% CO2 conversion at 623 K and 1.0 bar"}],
                },
                {
                    "node_id": "n_002", "canonical_name": "Co-Ni-FlowB (secondary)", "entity_type": "material",
                    "scope_level": "paper_scoped_instance", "source_ids": ["secondary"], "provenance_role": "author_self_statement",
                    "aliases": [{"surface_form": "Co-Ni-FlowB", "source_id": "secondary", "paragraph_id": "p-sec-1"}],
                    "evidence_anchors": [{"source_id": "secondary", "paragraph_id": "p-sec-1", "page": None, "quote": "We tested Co-Ni-FlowB (5 wt% Co-10 wt% Ni/CeO2) at 573 K and 0.5 bar"}],
                },
                {
                    "node_id": "n_003", "canonical_name": "CO2 conversion", "entity_type": "metric",
                    "scope_level": "cross_paper_concept", "source_ids": ["primary", "secondary"], "provenance_role": "author_self_statement",
                    "evidence_anchors": [
                        {"source_id": "primary", "paragraph_id": "p-prim-2", "page": None, "quote": "measured 84.2% CO2 conversion at 623 K and 1.0 bar"},
                        {"source_id": "secondary", "paragraph_id": "p-sec-1", "page": None, "quote": "obtained 79.1% CO2 conversion (R2 = 0.994)"},
                    ],
                },
                {
                    "node_id": "n_004", "canonical_name": "DFT", "entity_type": "concept",
                    "scope_level": "cross_paper_concept", "source_ids": ["primary", "secondary"], "provenance_role": "author_self_statement",
                    "aliases": [{"surface_form": "density functional theory", "source_id": "primary", "paragraph_id": "p-prim-2"}],
                    "evidence_anchors": [
                        {"source_id": "primary", "paragraph_id": "p-prim-2", "page": None, "quote": "Using density functional theory (DFT) with PBE, the activation barrier was 0.68 eV."},
                        {"source_id": "secondary", "paragraph_id": "p-sec-2", "page": None, "quote": "Density functional theory (DFT, RPBE+U) yielded an apparent barrier of 0.81 eV."},
                    ],
                },
                {
                    "node_id": "n_005", "canonical_name": "Park et al. (2022) Ni/Al2O3 sintering claim", "entity_type": "conclusion",
                    "scope_level": "single_paper_entity", "source_ids": ["primary"], "provenance_role": "cited_or_third_party",
                    "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-1", "page": None, "quote": "Prior studies by Park et al. (2022) reported catalyst sintering above 723 K [1]."}],
                },
            ],
            "edges": [
                {
                    "edge_id": "e_001", "source_node_id": "n_001", "relation_type": "reports_metric", "target_node_id": "n_003",
                    "source_id": "primary", "conditions": "623 K, 1.0 bar", "statistics": "84.2%",
                    "evidence_type": "direct_evidence", "provenance_role": "author_self_statement", "confidence": "high",
                    "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-2", "page": None, "quote": "We prepared Ni-Cat-BatchA (10 wt% Ni/Al2O3) and measured 84.2% CO2 conversion at 623 K and 1.0 bar"}],
                },
                {
                    "edge_id": "e_002", "source_node_id": "n_002", "relation_type": "reports_metric", "target_node_id": "n_003",
                    "source_id": "secondary", "conditions": "573 K, 0.5 bar, 10^{-5} s^{-1}", "statistics": "79.1%, R^2 = 0.994",
                    "evidence_type": "direct_evidence", "provenance_role": "author_self_statement", "confidence": "high",
                    "evidence_anchors": [{"source_id": "secondary", "paragraph_id": "p-sec-1", "page": None, "quote": "We tested Co-Ni-FlowB (5 wt% Co-10 wt% Ni/CeO2) at 573 K and 0.5 bar with residence parameter 10-5 s-1 and obtained 79.1% CO2 conversion (R2 = 0.994)."}],
                },
                {
                    "edge_id": "e_003", "source_node_id": "n_001", "relation_type": "uses", "target_node_id": "n_004",
                    "source_id": "primary", "conditions": "PBE functional slab model", "statistics": "0.68 eV",
                    "evidence_type": "direct_evidence", "provenance_role": "author_self_statement", "confidence": "high",
                    "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-2", "page": None, "quote": "Using density functional theory (DFT) with PBE, the activation barrier was 0.68 eV."}],
                },
            ],
            "entity_alignment": {
                "merged_candidates": [
                    {"canonical_node_id": "n_004", "canonical_name": "DFT", "alignment_type": "abbreviation_fullform", "merge_basis": "Generic acronym 'DFT' for 'density functional theory' aligned at concept level while keeping functional/barrier setups separate."}
                ],
                "non_merge_records": [
                    {
                        "shared_surface_or_term": "Ni/Al2O3 & DFT setup",
                        "candidate_a": {"name": "Ni-Cat-BatchA / PBE slab", "source_id": "primary", "paragraph_id": "p-prim-2"},
                        "candidate_b": {"name": "commercial Ni/Al2O3 Lot-Z / RPBE+U", "source_id": "secondary", "paragraph_id": "p-sec-2"},
                        "rejection_reason": "different_physical_sample",
                        "rationale": "Different catalyst batches, pressures (1.0 vs 0.5 bar), and DFT functionals (PBE vs RPBE+U) must not be merged.",
                    }
                ],
            },
            "conflicts": {
                "intra_source_conflicts": [],
                "cross_paper_conflicts": [{"topic": "activation barrier", "primary": "0.68 eV (PBE)", "secondary": "0.81 eV (RPBE+U)"}],
            },
            "query_specs": [
                {"query_id": "q_001", "question": "What metrics and conditions are bound to Ni-Cat-BatchA?", "query_type": "entity_neighborhood", "query_params": {"term": "Ni-Cat-BatchA"}},
                {"query_id": "q_002", "question": "Compare DFT and Ni/Al2O3 across primary and secondary papers.", "query_type": "cross_paper_comparison", "query_params": {"concept": "DFT"}},
                {"query_id": "q_003", "question": "Which relations are contradictory or unmergeable across the corpus?", "query_type": "conflict_and_uncertainty", "query_params": {}},
                {"query_id": "q_004", "question": "Is there a direct evidenced link between Park et al. and Co-Ni-FlowB?", "query_type": "shortest_path", "query_params": {"source": "Park et al.", "target": "Co-Ni-FlowB", "max_hops": 2}},
            ],
        }
        patch_p = work_dir / "patch_ok.json"
        patch_p.write_text(json.dumps(valid_patch, ensure_ascii=False, indent=2), encoding="utf-8")
        docs = {p["source_id"]: json.loads(Path(p["document_json_path"]).read_text(encoding="utf-8")) for p in brief["papers"]}
        _merge_patch(res_p, patch_p, docs, work_dir=work_dir)
        ok_rep = audit_result(res_p, in_dir / "case.json", work_dir)
        exp_info = export_all(json.loads(res_p.read_text(encoding="utf-8")), out_dir)
        checks["valid_kg_strict_audit_passed"] = ok_rep["passed"] and ok_rep["error_count"] == 0
        checks["graphml_xml_valid"] = exp_info["graphml_valid"]
        checks["executed_queries_ge_3"] = len(json.loads(res_p.read_text(encoding="utf-8"))["queries"]) == 4

        # 4. 负向陷阱拦截测试（证明本 Skill 拦截了基础模型常见错误）
        bad_res_p = out_dir / "bad_result.json"
        bad_obj = json.loads(res_p.read_text(encoding="utf-8"))
        # 陷阱 A: 在非 PDF 语料上伪造 page=1
        bad_obj["nodes"][0]["evidence_anchors"][0]["page"] = 1
        # 陷阱 B: 把 primary DOI 泄漏给 secondary
        bad_obj["papers"]["secondary"]["doi"] = bad_obj["papers"]["primary"]["doi"]
        # 陷阱 C: 使用 co_occurrence 和模糊关系 relates_to
        bad_obj["edges"][0]["relation_type"] = "relates_to"
        bad_obj["edges"][0]["evidence_type"] = "co_occurrence"
        # 陷阱 D: 单条边跨篇混绑（跨篇结果合成一个结论）
        bad_obj["edges"][1]["evidence_anchors"].append(bad_obj["nodes"][0]["evidence_anchors"][0])
        # 陷阱 E: statistics 编造未在引文出现的数值
        bad_obj["edges"][2]["statistics"] = "99.9%"
        bad_res_p.write_text(json.dumps(bad_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        bad_rep = audit_result(bad_res_p, in_dir / "case.json", work_dir)
        errs_txt = "\n".join(bad_rep["errors"])
        warns_txt = "\n".join(bad_rep["warnings"])
        checks["trap_fake_pdf_page_blocked"] = "forbidden page=1" in errs_txt
        checks["trap_metadata_leakage_blocked"] = "metadata leakage" in errs_txt
        checks["trap_cooccurrence_and_vague_rel_blocked"] = "co_occurrence" in errs_txt and "relates_to" in errs_txt
        checks["trap_cross_paper_fusion_blocked"] = "mixes anchors from multiple papers" in errs_txt
        checks["trap_fabricated_stat_flagged"] = "99.9" in warns_txt

        # 4b. 嵌套 JSON 包装 + 字典式 material.json + 自动净化 page=1 与自动补全 3 条查询测试
        wrap_in, wrap_work, wrap_out = root / "wrap_in", root / "wrap_work", root / "wrap_out"
        wrap_in.mkdir(parents=True)
        wrap_out.mkdir(parents=True)
        (wrap_in / "case.json").write_text(json.dumps({"case_id": "WRAP_01", "output_contract": {"required_fields": ["case_id", "status", "graph", "queries"]}}), encoding="utf-8")
        (wrap_in / "primary-material.json").write_text(json.dumps({"article": PRIMARY_JSON}, ensure_ascii=False), encoding="utf-8")
        (wrap_in / "material.json").write_text(json.dumps({"primary": PRIMARY_JSON["paragraphs"]}, ensure_ascii=False), encoding="utf-8")
        w_res_p = wrap_out / "result.json"
        w_brief = run_prep(wrap_in, wrap_work, init_result=w_res_p)
        w_docs = {p["source_id"]: json.loads(Path(p["document_json_path"]).read_text(encoding="utf-8")) for p in w_brief["papers"]}
        w_patch = wrap_work / "w_patch.json"
        w_patch.write_text(json.dumps({
            "status": "completed",
            "nodes": [{"node_id": "w_1", "canonical_name": "Ni-Cat-BatchA", "entity_type": "material", "source_ids": ["primary"], "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-2", "page": 1, "quote": "We prepared Ni-Cat-BatchA (10 wt% Ni/Al2O3)"}]}],
            "edges": [{"edge_id": "we_1", "source_node_id": "w_1", "relation_type": "measured_as", "target_node_id": "w_1", "source_id": "primary", "conditions": "623 K", "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-2", "page": 1, "quote": "We prepared Ni-Cat-BatchA (10 wt% Ni/Al2O3)"}]}],
        }), encoding="utf-8")
        _merge_patch(w_res_p, w_patch, w_docs, work_dir=wrap_work)
        w_rep = audit_result(w_res_p, wrap_in / "case.json", wrap_work)
        w_obj = json.loads(w_res_p.read_text(encoding="utf-8"))
        checks["wrapped_json_and_dict_material_ingested"] = w_brief["paper_count"] == 1 and len(w_docs["primary"]["blocks"]) >= 3
        checks["auto_sanitize_page_and_auto_queries"] = w_rep["passed"] and w_obj["nodes"][0]["evidence_anchors"][0]["page"] is None and len(w_obj["queries"]) == 3 and "graph" in w_obj

        # 5. 增量更新 ID 稳定性测试
        inc_in, inc_work, inc_out = root / "inc_in", root / "inc_work", root / "inc_out"
        inc_in.mkdir(parents=True)
        inc_out.mkdir(parents=True)
        (inc_in / "case.json").write_text(json.dumps({"case_id": "INC_TEST_01", "output_contract": {"required_fields": ["case_id", "status", "nodes", "edges", "incremental_update", "queries"]}}), encoding="utf-8")
        base_g = {
            "nodes": [{"node_id": "n_base_001", "canonical_name": "BaseCatalyst", "entity_type": "material", "scope_level": "single_paper_entity", "source_ids": ["legacy"], "provenance_role": "author_self_statement", "status": "verified", "aliases": [], "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-2", "page": None, "quote": "We prepared Ni-Cat-BatchA (10 wt% Ni/Al2O3)"}]}],
            "edges": [{"edge_id": "e_base_001", "source_node_id": "n_base_001", "relation_type": "uses", "target_node_id": "n_base_001", "source_id": "primary", "conditions": "623 K", "statistics": "unreported", "evidence_type": "direct_evidence", "provenance_role": "author_self_statement", "confidence": "medium", "confidence_basis": "legacy", "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-2", "page": None, "quote": "We prepared Ni-Cat-BatchA (10 wt% Ni/Al2O3)"}]}],
        }
        (inc_in / "base_graph.json").write_text(json.dumps(base_g, ensure_ascii=False, indent=2), encoding="utf-8")
        (inc_in / "primary-material.json").write_text(json.dumps(PRIMARY_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
        inc_res_p = inc_out / "result.json"
        inc_brief = run_prep(inc_in, inc_work, init_result=inc_res_p)
        checks["incremental_mode_auto_detected"] = inc_brief["incremental_mode"]["enabled"] is True
        inc_patch_p = inc_work / "inc_patch.json"
        inc_patch_p.write_text(json.dumps({
            "status": "completed",
            "nodes": [{"node_id": "n_new_001", "canonical_name": "PBE slab", "entity_type": "method", "source_ids": ["primary"], "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-2", "page": None, "quote": "Using density functional theory (DFT) with PBE"}]}],
            "edges": [{"edge_id": "e_new_001", "source_node_id": "n_base_001", "relation_type": "uses", "target_node_id": "n_new_001", "source_id": "primary", "conditions": "623 K", "evidence_anchors": [{"source_id": "primary", "paragraph_id": "p-prim-2", "page": None, "quote": "Using density functional theory (DFT) with PBE"}]}],
            "query_specs": [
                {"query_id": "q1", "query_type": "entity_neighborhood", "query_params": {"term": "BaseCatalyst"}},
                {"query_id": "q2", "query_type": "entity_neighborhood", "query_params": {"term": "PBE slab"}},
                {"query_id": "q3", "query_type": "conflict_and_uncertainty", "query_params": {}},
            ],
        }), encoding="utf-8")
        inc_docs = {p["source_id"]: json.loads(Path(p["document_json_path"]).read_text(encoding="utf-8")) for p in inc_brief["papers"]}
        _merge_patch(inc_res_p, inc_patch_p, inc_docs, work_dir=inc_work)
        inc_rep = audit_result(inc_res_p, inc_in / "case.json", inc_work)
        inc_res_obj = json.loads(inc_res_p.read_text(encoding="utf-8"))
        checks["incremental_ids_preserved_and_audited"] = inc_rep["passed"] and inc_res_obj["incremental_update"]["id_stability_verified"] is True

        # 6. Markdown 表格单元格行列交叉精确核验测试（防邻列串值）
        md_in, md_work, md_out = root / "md_in", root / "md_work", root / "md_out"
        md_in.mkdir(parents=True)
        md_out.mkdir(parents=True)
        (md_in / "case.json").write_text(json.dumps({"case_id": "MD_TBL_01"}), encoding="utf-8")
        (md_in / "paper.md").write_text(MARKDOWN_PAPER, encoding="utf-8")
        md_res_p = md_out / "result.json"
        md_brief = run_prep(md_in, md_work, init_result=md_res_p)
        md_docs = {p["source_id"]: json.loads(Path(p["document_json_path"]).read_text(encoding="utf-8")) for p in md_brief["papers"]}
        md_patch_p = md_work / "md_patch.json"
        md_patch_p.write_text(json.dumps({
            "status": "completed",
            "nodes": [
                {"node_id": "m1", "canonical_name": "Cat-M1", "entity_type": "material", "source_ids": ["paper_1"], "evidence_anchors": [{"source_id": "paper_1", "table_id": "tbl_1", "row_label": "Cat-M1", "col_header": "Earth Yield", "value": "91.5%", "page": None, "quote": "Cat-M1 91.5%"}]},
                {"node_id": "m2", "canonical_name": "Ocean Yield", "entity_type": "metric", "source_ids": ["paper_1"], "evidence_anchors": [{"source_id": "paper_1", "table_id": "tbl_1", "row_label": "Cat-M1", "col_header": "Ocean Yield", "value": "91.5%", "page": None, "quote": "Cat-M1 91.5%"}]},
            ],
            "edges": [],
            "query_specs": [
                {"query_id": "q1", "query_type": "entity_neighborhood", "query_params": {"term": "Cat-M1"}},
                {"query_id": "q2", "query_type": "entity_neighborhood", "query_params": {"term": "Ocean"}},
                {"query_id": "q3", "query_type": "conflict_and_uncertainty", "query_params": {}},
            ],
        }), encoding="utf-8")
        _merge_patch(md_res_p, md_patch_p, md_docs, work_dir=md_work)
        md_rep = audit_result(md_res_p, md_in / "case.json", md_work)
        checks["markdown_table_cell_swap_caught"] = any("not found at cited cell" in e for e in md_rep["errors"])

    passed = all(checks.values())
    summary = {
        "benchmark_passed": passed,
        "checks_passed": sum(1 for v in checks.values() if v),
        "checks_total": len(checks),
        "capabilities": caps["python_modules"],
        "checks": checks,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    res = run_benchmark()
    raise SystemExit(0 if res["benchmark_passed"] else 1)
