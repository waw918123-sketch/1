#!/usr/bin/env python3
"""Self-contained synthetic benchmark comparing naive extraction/summary vs literature-structured-notes."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

sys.dont_write_bytecode = True

from doc_common import discover_capabilities, import_pymupdf
from notes_audit import audit_result, export_markdown_notes
from prep_notes import run_prep

SCRIPTS_DIR = Path(__file__).resolve().parent

SYNTHETIC_XML_PAPER2 = """<?xml version="1.0" encoding="UTF-8"?>
<article>
  <front>
    <article-meta>
      <title-group><article-title>Comparative Synthetic Kinetics over Cu-MOF Catalysts</article-title></title-group>
    </article-meta>
  </front>
  <body>
    <abstract><p>We evaluate Turnover Frequency (TOF) of Cu-MOF at 623 K and 1.0 bar.</p></abstract>
    <sec id="s1"><title>1. Introduction</title>
      <p>Metal-Organic Framework (MOF-74) analogues exhibit tunable active sites under mild pressure.</p>
    </sec>
    <sec id="s2"><title>2. Limitations</title>
      <p>Our batch reactor did not evaluate catalyst deactivation beyond 12 h.</p>
    </sec>
  </body>
</article>
"""


def _build_synthetic_pdf(pdf_path: Path) -> None:
    fitz = import_pymupdf()
    doc = fitz.open()

    # Page 1: Title/Abstract + RIGHT-BEFORE-LEFT two-column stream + nested sections + abbreviations + preconditions
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((50, 48), "Journal of Synthetic Catalysis 2026 | Page 1", fontsize=8)
    p1.insert_text((50, 88), "Structured Synthesis of Metal-Organic Framework Catalysts", fontsize=14)
    p1.insert_text((50, 114), "Abstract", fontsize=11)
    p1.insert_text((50, 130), "We report 92.4% selectivity over Metal-Organic Framework (MOF-74) at 673 K.", fontsize=10)
    # Right column inserted FIRST in stream
    p1.insert_text((318, 175), "2. Experimental Setup", fontsize=12)
    p1.insert_text((318, 195), "2.1 Catalyst Synthesis", fontsize=11)
    p1.insert_text((318, 214), "RIGHT_COL: Tests were performed at 673 K and 1.0 bar assuming steady-state flow.", fontsize=10)
    # Left column inserted SECOND in stream
    p1.insert_text((50, 175), "1. Introduction", fontsize=12)
    p1.insert_text((50, 194), "LEFT_COL: Turnover Frequency (TOF) governs CO2 hydrogenation kinetics.", fontsize=10)

    # Page 2: Results + three-line table (91.8% vs Abstract 92.4% conflict) + Limitations
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((50, 48), "Journal of Synthetic Catalysis 2026 | Page 2", fontsize=8)
    p2.insert_text((50, 88), "3. Results and Discussion", fontsize=12)
    p2.insert_text((50, 108), "Table 1. Catalytic performance at 673 K and 1.0 bar.", fontsize=10)
    p2.draw_line((50, 120), (540, 120))
    p2.insert_text((60, 136), "Catalyst", fontsize=10)
    p2.insert_text((220, 136), "Temp (K)", fontsize=10)
    p2.insert_text((380, 136), "Selectivity (%)", fontsize=10)
    p2.draw_line((50, 144), (540, 144))
    p2.insert_text((60, 162), "MOF-74", fontsize=10)
    p2.insert_text((220, 162), "673", fontsize=10)
    p2.insert_text((380, 162), "91.8", fontsize=10)
    p2.draw_line((50, 172), (540, 172))
    p2.insert_text((50, 210), "4. Limitations", fontsize=12)
    p2.insert_text((50, 230), "A key limitation is that we did not evaluate long-term fouling; reactor wall thickness is unreported.", fontsize=10)

    doc.save(str(pdf_path))
    doc.close()


def run_benchmark() -> Dict[str, Any]:
    caps = discover_capabilities()
    has_fitz = bool(caps["python_modules"].get("pymupdf_fitz"))

    with tempfile.TemporaryDirectory(prefix="litnotes_bench_") as tmp:
        root = Path(tmp)
        in_dir = root / "input"
        work_dir = root / "work"
        out_dir = root / "output"
        in_dir.mkdir(parents=True)
        out_dir.mkdir(parents=True)

        (in_dir / "paper2.xml").write_text(SYNTHETIC_XML_PAPER2, encoding="utf-8")
        if has_fitz:
            _build_synthetic_pdf(in_dir / "paper1.pdf")

        case_obj = {
            "case_id": "SYNTHETIC_READING10_BENCH_01",
            "output_contract": {
                "required_fields": [
                    "case_id", "status", "paper_cards", "section_notes",
                    "knowledge_points", "conflicts", "human_review_items",
                    "sources", "uncertainty", "change_records",
                ]
            },
        }
        (in_dir / "case.json").write_text(json.dumps(case_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        (in_dir / "task.md").write_text("# Synthetic Task\nProduce section-aligned notes, comparable paper cards, normalized knowledge points, and verify anchors.", encoding="utf-8")

        res_p = out_dir / "result.json"
        brief = run_prep(in_dir, work_dir, init_result=res_p)

        # Baseline naive text check (if PDF exists)
        naive_order_ok = False
        if has_fitz:
            fitz = import_pymupdf()
            d = fitz.open(str(in_dir / "paper1.pdf"))
            naive_txt = d[0].get_text("text")
            d.close()
            naive_order_ok = 0 <= naive_txt.find("LEFT_COL") < naive_txt.find("RIGHT_COL")

        # Locate XML doc and PDF doc
        doc_dirs = sorted(work_dir.glob("doc_*"))
        xml_doc_info = next((d for d in brief["documents"] if d["format"] == "xml"), None)
        pdf_doc_info = next((d for d in brief["documents"] if d["format"] == "pdf"), None)

        xml_doc = json.loads(Path(xml_doc_info["document_json_path"]).read_text(encoding="utf-8"))
        xml_cues = json.loads(Path(xml_doc_info["cues_path"]).read_text(encoding="utf-8"))

        checks: Dict[str, bool] = {
            "multi_paper_mode_detected": (brief["document_count"] == (2 if has_fitz else 1)),
            "xml_section_hierarchy_built": len(xml_doc["sections"]) >= 2 and xml_doc["sections"][0]["next_section_id"] == xml_doc["sections"][1]["section_id"],
            "terminology_and_limitation_cues_extracted": any(a["abbreviation"] == "MOF-74" for a in xml_cues["abbreviations"]) and len(xml_cues["limitation_cues"]) >= 1,
        }

        if has_fitz and pdf_doc_info:
            pdf_doc = json.loads(Path(pdf_doc_info["document_json_path"]).read_text(encoding="utf-8"))
            pdf_cues = json.loads(Path(pdf_doc_info["cues_path"]).read_text(encoding="utf-8"))
            full_pdf_txt = "\n".join(b["raw_text"] for b in pdf_doc["blocks"])
            checks["pdf_column_order_reconstructed"] = 0 <= full_pdf_txt.find("LEFT_COL") < full_pdf_txt.find("RIGHT_COL")
            checks["pdf_nested_sections_and_threeline_table"] = len(pdf_doc["sections"]) >= 4 and len(pdf_doc["tables"]) >= 1
            checks["pdf_precondition_and_limitation_cues"] = len(pdf_cues["condition_cues"]) >= 1 and len(pdf_cues["limitation_cues"]) >= 1

            # Build valid structured note patch and verify strict audit passes
            p1_intro = next(b for b in pdf_doc["blocks"] if "LEFT_COL" in b["raw_text"])
            p1_exp = next(b for b in pdf_doc["blocks"] if "RIGHT_COL" in b["raw_text"])
            p2_lim = next(b for b in pdf_doc["blocks"] if "did not evaluate" in b["raw_text"])
            s1 = next(s for s in pdf_doc["sections"] if "Introduction" in s["title"])
            s2 = next(s for s in pdf_doc["sections"] if "2. Experimental" in s["title"])
            s4 = next(s for s in pdf_doc["sections"] if "Limitations" in s["title"])
            tbl1 = pdf_doc["tables"][0]

            valid_res = json.loads(res_p.read_text(encoding="utf-8"))
            valid_res.update({
                "status": "completed",
                "paper_cards": [
                    {
                        "doc_id": pdf_doc["doc_id"],
                        "title": "Structured Synthesis of Metal-Organic Framework Catalysts",
                        "research_question": "Evaluate CO2 hydrogenation selectivity over MOF-74.",
                        "data_or_materials": "MOF-74 catalyst; reactor wall thickness unreported.",
                        "method_and_conditions": "Steady-state flow at 673 K and 1.0 bar.",
                        "key_findings": "Abstract reports 92.4% selectivity; Table 1 reports 91.8% at 673 K.",
                        "limitations": "Did not evaluate long-term fouling.",
                    },
                    {
                        "doc_id": xml_doc["doc_id"],
                        "title": "Comparative Synthetic Kinetics over Cu-MOF Catalysts",
                        "research_question": "Evaluate TOF of Cu-MOF.",
                        "data_or_materials": "Cu-MOF catalyst in batch reactor.",
                        "method_and_conditions": "623 K and 1.0 bar.",
                        "key_findings": "Evaluates TOF of Cu-MOF under mild pressure.",
                        "limitations": "Did not evaluate deactivation beyond 12 h.",
                    },
                ],
                "section_notes": [
                    {
                        "doc_id": pdf_doc["doc_id"], "section_id": s1["section_id"], "section_title": s1["title"],
                        "purpose": "Motivate TOF kinetics.",
                        "evidence_anchors": [{"doc_id": pdf_doc["doc_id"], "page": 1, "block_id": p1_intro["block_id"], "quote": p1_intro["raw_text"]}],
                    },
                    {
                        "doc_id": pdf_doc["doc_id"], "section_id": s2["section_id"], "section_title": s2["title"],
                        "transition_from_prev": "Defines experimental conditions for the kinetic problem introduced in Section 1.",
                        "methods": "Steady-state flow at 673 K and 1.0 bar.",
                        "evidence_anchors": [{"doc_id": pdf_doc["doc_id"], "page": 1, "block_id": p1_exp["block_id"], "quote": p1_exp["raw_text"]}],
                    },
                    {
                        "doc_id": pdf_doc["doc_id"], "section_id": s4["section_id"], "section_title": s4["title"],
                        "limitations": "Long-term fouling not evaluated.",
                        "evidence_anchors": [{"doc_id": pdf_doc["doc_id"], "page": 2, "block_id": p2_lim["block_id"], "quote": p2_lim["raw_text"]}],
                    },
                ],
                "knowledge_points": [
                    {
                        "canonical_term": "Metal-Organic Framework",
                        "aliases": ["MOF-74"],
                        "kind": "finding",
                        "statement": "Achieves 91.8% selectivity in Table 1.",
                        "preconditions": "673 K, 1.0 bar, steady-state flow",
                        "verification_mode": "deterministic",
                        "evidence_anchors": [{"doc_id": pdf_doc["doc_id"], "page": 2, "table_id": tbl1["table_id"], "row_label": "MOF-74", "col_header": "Selectivity (%)", "value": "91.8"}],
                    }
                ],
                "conflicts": [
                    {"topic": "MOF-74 selectivity", "abstract_value": "92.4%", "table_value": "91.8%", "verification_mode": "human_review_required"}
                ],
            })
            res_p.write_text(json.dumps(valid_res, ensure_ascii=False, indent=2), encoding="utf-8")
            rep_ok = audit_result(res_p, in_dir / "case.json", work_dir)
            md_out = out_dir / "notes.md"
            export_markdown_notes(valid_res, md_out)
            checks["strict_audit_passes_valid_notes"] = rep_ok["passed"] and md_out.exists()

            # Negative traps: fabricated quote + wrong table value + scrambled section order
            bad_res = json.loads(json.dumps(valid_res))
            bad_res["section_notes"] = list(reversed(bad_res["section_notes"]))
            bad_res["knowledge_points"][0]["evidence_anchors"][0]["value"] = "99.9"
            bad_res["section_notes"][0]["evidence_anchors"][0]["quote"] = "FABRICATED QUOTE NOT IN PAPER"
            bad_p = out_dir / "bad_result.json"
            bad_p.write_text(json.dumps(bad_res, ensure_ascii=False, indent=2), encoding="utf-8")
            rep_bad = audit_result(bad_p, in_dir / "case.json", work_dir)
            checks["negative_traps_caught"] = (not rep_bad["passed"]) and rep_bad["error_count"] >= 3

        # Blocked invalid PDF header check
        blk_in = root / "blocked_in"
        blk_work = root / "blocked_work"
        blk_out = root / "blocked_out" / "result.json"
        blk_in.mkdir()
        (blk_in / "corrupt.pdf").write_bytes(b"NOT_A_REAL_PDF_FILE")
        (blk_in / "case.json").write_text(json.dumps({"case_id": "SYNTH_BLK_01", "output_contract": {"required_fields": ["case_id", "status", "sources", "uncertainty"]}}), encoding="utf-8")
        run_prep(blk_in, blk_work, init_result=blk_out)
        blk_res = json.loads(blk_out.read_text(encoding="utf-8"))
        checks["corrupt_pdf_honestly_blocked"] = blk_res.get("status") == "blocked"

        return {
            "notice": "SYNTHETIC_BENCHMARK_ONLY",
            "has_pymupdf": has_fitz,
            "naive_pdf_column_order_ok": naive_order_ok,
            "skill_passed": sum(1 for v in checks.values() if v),
            "total_checks": len(checks),
            "checks": checks,
        }


if __name__ == "__main__":
    r = run_benchmark()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    raise SystemExit(0 if r["skill_passed"] == r["total_checks"] else 1)
