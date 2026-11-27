#!/usr/bin/env python3
"""Self-contained synthetic benchmark comparing naive extraction vs nanopublication-claims."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

sys.dont_write_bytecode = True

from doc_common import discover_capabilities, import_pymupdf
from nanopub_audit import _enrich_and_merge_patch, audit_result
from nanopub_export import export_all
from prep_nanopub import run_prep

SYNTHETIC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<article>
  <front>
    <journal-meta><journal-title-group><journal-title>Journal of Synthetic Nanopubs</journal-title></journal-title-group></journal-meta>
    <article-meta>
      <article-id pub-id-type="doi">10.1234/synth.np.2026</article-id>
      <title-group><article-title>Catalyst Selectivity and Observational Survival Analysis</article-title></title-group>
      <contrib-group>
        <contrib contrib-type="author"><name><surname>Smith</surname><given-names>Alice</given-names></name></contrib>
        <contrib contrib-type="author"><name><surname>Chen</surname><given-names>Bob</given-names></name></contrib>
        <contrib contrib-type="editor"><name><surname>WrongEditor</surname><given-names>Eve</given-names></name></contrib>
      </contrib-group>
      <pub-date pub-type="epub"><year>2026</year></pub-date>
    </article-meta>
  </front>
  <body>
    <abstract><p>CA-1 achieves 92.4% selectivity at 673 K and 1.0 bar.</p></abstract>
    <sec id="s1"><title>1. Results</title>
      <p>CA-1 increases CO2 conversion to 88.5% and reduces CO selectivity to 4.1% under 673 K and 1.0 bar.</p>
    </sec>
  </body>
</article>
"""


def _build_synthetic_pdf(pdf_path: Path) -> None:
    fitz = import_pymupdf()
    doc = fitz.open()
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((50, 48), "PLoS ONE 2026 | Page 1 | doi:10.1371/journal.pone.0099999", fontsize=8)
    p1.insert_text((50, 88), "Prognostic Value of Alpha Synchrony and Global Biomass Estimates", fontsize=13)
    p1.insert_text((50, 110), "Jane Doe, John Roe", fontsize=10)
    p1.insert_text((50, 126), "Academic Editor: Prof. NotAnAuthor", fontsize=9)
    # Right column BEFORE left column in stream (wrapped neatly within x=318..565)
    p1.insert_text((318, 170), "2. Observational Cohort Results", fontsize=11)
    p1.insert_text((318, 190), "RIGHT_COL: There were no statistically", fontsize=9)
    p1.insert_text((318, 203), "significant differences in age (p = 0.33)", fontsize=9)
    p1.insert_text((318, 216), "across 21 patients.", fontsize=9)
    p1.insert_text((318, 236), "Alpha synchrony differed between survival", fontsize=9)
    p1.insert_text((318, 249), "groups (p ,0.03 2p ,0.001, Fig. 2.A).", fontsize=9)
    # Left column AFTER right column (wrapped neatly within x=50..290)
    p1.insert_text((50, 170), "1. Introduction", fontsize=11)
    p1.insert_text((50, 190), "LEFT_COL: Previous models relied on", fontsize=9)
    p1.insert_text((50, 203), "higher-taxon extrapolation.", fontsize=9)

    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((50, 48), "PLoS ONE 2026 | Page 2", fontsize=8)
    p2.insert_text((50, 88), "Table 2. Eukaryotic species richness via higher-taxon extrapolation.", fontsize=10)
    p2.draw_line((50, 102), (540, 102))
    p2.insert_text((60, 118), "Group", fontsize=10)
    p2.insert_text((200, 118), "Earth", fontsize=10)
    p2.insert_text((360, 118), "Ocean", fontsize=10)
    p2.draw_line((50, 126), (540, 126))
    p2.insert_text((60, 144), "Total", fontsize=10)
    p2.insert_text((200, 144), "8,740,000", fontsize=10)
    p2.insert_text((360, 144), "2,210,000", fontsize=10)
    p2.draw_line((50, 154), (540, 154))
    doc.save(str(pdf_path))
    doc.close()


def run_benchmark() -> Dict[str, Any]:
    caps = discover_capabilities()
    has_fitz = bool(caps["python_modules"].get("pymupdf_fitz"))

    with tempfile.TemporaryDirectory(prefix="nanopub_bench_") as tmp:
        root = Path(tmp)
        in_dir, work_dir, out_dir = root / "input", root / "work", root / "output"
        in_dir.mkdir(parents=True)
        out_dir.mkdir(parents=True)

        (in_dir / "paper2.xml").write_text(SYNTHETIC_XML, encoding="utf-8")
        if has_fitz:
            _build_synthetic_pdf(in_dir / "paper1.pdf")

        case_obj = {
            "case_id": "SYNTHETIC_NANOPUB_BENCH_01",
            "output_contract": {
                "required_fields": [
                    "case_id", "status", "pubinfo", "nanopublications",
                    "conflicts", "human_review_items", "sources", "uncertainty", "exports",
                ]
            },
        }
        (in_dir / "case.json").write_text(json.dumps(case_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        (in_dir / "task.md").write_text("# Synthetic Task\nExtract atomic Nanopublications and export TriG.", encoding="utf-8")

        res_p = out_dir / "result.json"
        brief = run_prep(in_dir, work_dir, init_result=res_p)

        xml_info = next(d for d in brief["documents"] if d["format"] == "xml")
        xml_doc = json.loads(Path(xml_info["document_json_path"]).read_text(encoding="utf-8"))
        xml_authors = xml_doc["metadata"]["authors"]

        checks: Dict[str, bool] = {
            "xml_editor_excluded_from_authors": ("Alice Smith" in xml_authors and "Bob Chen" in xml_authors and all("WrongEditor" not in a for a in xml_authors)),
            "xml_doi_and_year_locked": (xml_doc["metadata"]["doi"] == "10.1234/synth.np.2026" and xml_doc["metadata"]["year"] == "2026"),
        }

        if has_fitz:
            pdf_info = next(d for d in brief["documents"] if d["format"] == "pdf")
            pdf_doc = json.loads(Path(pdf_info["document_json_path"]).read_text(encoding="utf-8"))
            pdf_cues = json.loads(Path(pdf_info["cues_path"]).read_text(encoding="utf-8"))
            fitz = import_pymupdf()
            d = fitz.open(str(in_dir / "paper1.pdf"))
            naive_txt = d[0].get_text("text")
            d.close()
            naive_order_ok = 0 <= naive_txt.find("LEFT_COL") < naive_txt.find("RIGHT_COL")
            full_pdf_txt = "\n".join(b["raw_text"] for b in pdf_doc["blocks"])
            checks["pdf_column_order_reconstructed"] = 0 <= full_pdf_txt.find("LEFT_COL") < full_pdf_txt.find("RIGHT_COL")
            checks["pdf_editor_excluded_and_doi_extracted"] = (
                pdf_doc["metadata"]["doi"] == "10.1371/journal.pone.0099999"
                and all("NotAnAuthor" not in a for a in pdf_doc["metadata"]["authors"])
            )
            checks["stat_glyph_anomaly_p_comma_detected"] = len(pdf_cues["stat_anomalies"]) >= 1

            age_blk = next(b for b in pdf_doc["blocks"] if "p = 0.33" in b["raw_text"])
            tbl2 = pdf_doc["tables"][0]

            # Valid patch
            patch_ok = work_dir / "patch_ok.json"
            patch_ok.write_text(json.dumps({
                "status": "completed",
                "pubinfo": {"doc_1": {"title": "SHOULD BE IGNORED BY LOCK"}},
                "nanopublications": [
                    {
                        "assertion": {
                            "subject": "Patient age (survived vs deceased)",
                            "predicate": "no_significant_difference",
                            "object": "age distribution across groups",
                            "assertion_text": "Patient age shows no significant difference between survival groups.",
                            "polarity": "no_difference",
                            "conditions": "n = 21 patients",
                            "statistics": "p = 0.33",
                            "study_design": "observational",
                        },
                        "provenance": {
                            "evidence_type": "statistical_comparison",
                            "extraction_method": "between_group_test",
                            "confidence": "high",
                            "confidence_basis": "Verbatim p = 0.33 quote.",
                            "evidence_anchors": [{"doc_id": pdf_doc["doc_id"], "page": 1, "block_id": age_blk["block_id"], "quote": age_blk["raw_text"]}],
                        },
                    },
                    {
                        "assertion": {
                            "subject": "Global terrestrial eukaryotic species",
                            "predicate": "measured_as",
                            "object": "8,740,000 species",
                            "assertion_text": "Earth eukaryotic species richness is estimated at 8,740,000.",
                            "polarity": "neutral",
                            "conditions": "Global Earth domain",
                            "study_design": "extrapolation",
                        },
                        "provenance": {
                            "evidence_type": "extrapolation",
                            "extraction_method": "higher_taxon_extrapolation",
                            "confidence": "medium",
                            "confidence_basis": "Medium due to higher-taxon extrapolation.",
                            "evidence_anchors": [{"doc_id": pdf_doc["doc_id"], "page": 2, "table_id": tbl2["table_id"], "row_label": "Total", "col_header": "Earth", "value": "8,740,000"}],
                        },
                    },
                ],
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            docs_map = {pdf_doc["doc_id"]: pdf_doc, xml_doc["doc_id"]: xml_doc}
            merged = _enrich_and_merge_patch(res_p, patch_ok, docs_map)
            rep_ok = audit_result(res_p, in_dir / "case.json", work_dir)
            exp_ok = export_all(merged, out_dir)
            checks["locked_pubinfo_protected_and_trig_valid"] = (
                rep_ok["passed"]
                and exp_ok["trig_valid"]
                and merged["pubinfo"][pdf_doc["doc_id"]]["doi"] == "10.1371/journal.pone.0099999"
            )

            # Negative traps:
            # 1) Row/col swap (Ocean 2,210,000 attributed to Earth)
            # 2) Confidence 'high' on higher_taxon_extrapolation
            # 3) Statistical overreach ('identical' on p = 0.33)
            # 4) Causal overreach ('causes' on observational design)
            bad_res = json.loads(json.dumps(merged))
            bad_res["nanopublications"][0]["assertion"]["assertion_text"] = "Survived and deceased patients have identical age."
            bad_res["nanopublications"][0]["assertion"]["predicate"] = "causes"
            bad_res["nanopublications"][1]["assertion"]["object"] = "2,210,000 species"
            bad_res["nanopublications"][1]["provenance"]["confidence"] = "high"
            bad_p = out_dir / "bad_result.json"
            bad_p.write_text(json.dumps(bad_res, ensure_ascii=False, indent=2), encoding="utf-8")
            rep_bad = audit_result(bad_p, in_dir / "case.json", work_dir)
            checks["adversarial_traps_caught"] = (not rep_bad["passed"]) and rep_bad["error_count"] >= 4
        else:
            naive_order_ok = False

        # Corrupt PDF blocked check
        blk_in, blk_work, blk_out = root / "blk_in", root / "blk_w", root / "blk_o" / "result.json"
        blk_in.mkdir()
        (blk_in / "bad.pdf").write_bytes(b"NOT_A_PDF")
        (blk_in / "case.json").write_text(json.dumps({"case_id": "BLK_01", "output_contract": {"required_fields": ["case_id", "status", "sources", "uncertainty"]}}), encoding="utf-8")
        run_prep(blk_in, blk_work, init_result=blk_out)
        checks["corrupt_pdf_honestly_blocked"] = json.loads(blk_out.read_text(encoding="utf-8")).get("status") == "blocked"

        return {
            "notice": "SYNTHETIC_BENCHMARK_ONLY",
            "has_pymupdf": has_fitz,
            "naive_pdf_column_order_ok": naive_order_ok,
            "skill_passed": sum(1 for v in checks.values() if v),
            "total_checks": len(checks),
            "checks": checks,
        }


if __name__ == "__main__":
    res = run_benchmark()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    raise SystemExit(0 if res["skill_passed"] == res["total_checks"] else 1)
