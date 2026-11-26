#!/usr/bin/env python3
"""Self-contained synthetic benchmark comparing naive PDF text stream vs literature-doc-parser."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

sys.dont_write_bytecode = True

from pdf_common import discover_capabilities, import_pymupdf

SCRIPTS_DIR = Path(__file__).resolve().parent


def _build_synthetic_benchmark_pdf(pdf_path: Path) -> None:
    """Create a multi-feature synthetic PDF using PyMuPDF only (no ReportLab required)."""
    fitz = import_pymupdf()
    doc = fitz.open()

    # Page 1: Title/Abstract + intentionally RIGHT-BEFORE-LEFT two-column stream + hyphenation
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((50, 52), "Journal of Synthetic Benchmarks 2026 | Page 1", fontsize=8)
    p1.insert_text((50, 92), "Benchmarking Layout-Aware Scientific PDF Extraction", fontsize=15)
    p1.insert_text((50, 118), "Abstract", fontsize=11)
    p1.insert_text((50, 134), "We evaluate reading order, three-line tables, equations, and page numbering.", fontsize=10)
    # Right column inserted FIRST in stream
    p1.insert_text((318, 175), "2. Experimental Conditions", fontsize=12)
    p1.insert_text((318, 194), "RIGHT_STREAM_FIRST: Catalyst B operated at 673 K.", fontsize=10)
    # Left column inserted SECOND in stream
    p1.insert_text((50, 175), "1. Introduction", fontsize=12)
    p1.insert_text((50, 194), "LEFT_STREAM_SECOND: Naive extractors scram-", fontsize=10)
    p1.insert_text((50, 208), "ble multi-column manuscripts.", fontsize=10)

    # Page 2: Three-line table + split equation label '(1)' + non-sequential printed page label '10'
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((50, 52), "Journal of Synthetic Benchmarks 2026 | Page 10", fontsize=8)
    p2.insert_text((50, 92), "Table 1. Synthetic catalytic performance.", fontsize=11)
    p2.draw_line((50, 104), (540, 104))
    p2.insert_text((60, 120), "Catalyst", fontsize=10)
    p2.insert_text((210, 120), "Temp (K)", fontsize=10)
    p2.insert_text((360, 120), "Yield (%)", fontsize=10)
    p2.draw_line((50, 128), (540, 128))
    p2.insert_text((60, 146), "Cat-A", fontsize=10)
    p2.insert_text((210, 146), "623", fontsize=10)
    p2.insert_text((360, 146), "71.2", fontsize=10)
    p2.insert_text((60, 166), "Cat-B", fontsize=10)
    p2.insert_text((210, 166), "673", fontsize=10)
    p2.insert_text((360, 166), "84.1", fontsize=10)
    p2.draw_line((50, 176), (540, 176))
    # Split equation body and right-aligned label
    p2.insert_text((110, 240), "r = k * [CO2] / (1 + K * p)", fontsize=10)
    p2.insert_text((515, 240), "(1)", fontsize=10)

    # Page 3: Continued table
    p3 = doc.new_page(width=612, height=792)
    p3.insert_text((50, 52), "Journal of Synthetic Benchmarks 2026 | Page 11", fontsize=8)
    p3.insert_text((50, 92), "Table 1 (continued). Additional runs.", fontsize=11)
    p3.draw_line((50, 104), (540, 104))
    p3.insert_text((60, 120), "Catalyst", fontsize=10)
    p3.insert_text((210, 120), "Temp (K)", fontsize=10)
    p3.insert_text((360, 120), "Yield (%)", fontsize=10)
    p3.draw_line((50, 128), (540, 128))
    p3.insert_text((60, 146), "Cat-C", fontsize=10)
    p3.insert_text((210, 146), "723", fontsize=10)
    p3.insert_text((360, 146), "91.0", fontsize=10)
    p3.draw_line((50, 156), (540, 156))

    doc.save(str(pdf_path))
    doc.close()


def run_benchmark() -> Dict[str, Any]:
    caps = discover_capabilities()
    if not caps["python_modules"].get("pymupdf_fitz"):
        return {"status": "skipped", "reason": "pymupdf not installed"}

    fitz = import_pymupdf()
    with tempfile.TemporaryDirectory(prefix="litdoc_bench_") as tmp:
        tmp_dir = Path(tmp)
        pdf_path = tmp_dir / "synthetic_benchmark.pdf"
        out_dir = tmp_dir / "parse"
        _build_synthetic_benchmark_pdf(pdf_path)

        # Baseline: naive PDF text stream extraction
        doc = fitz.open(str(pdf_path))
        naive_p1 = doc[0].get_text("text")
        doc.close()
        naive_left_before_right = 0 <= naive_p1.find("LEFT_STREAM_SECOND") < naive_p1.find("RIGHT_STREAM_FIRST")
        naive_healed_hyphen = "scramble multi-column" in naive_p1
        naive_stripped_hf = "Journal of Synthetic Benchmarks" not in naive_p1

        # Skill pipeline: pdf_probe + pdf_extract
        subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIR / "pdf_probe.py"), str(pdf_path), "--out", str(tmp_dir / "probe.json")],
            capture_output=True, text=True, check=True,
        )
        subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIR / "pdf_extract.py"), str(pdf_path), "--out-dir", str(out_dir)],
            capture_output=True, text=True, check=True,
        )
        probe = json.loads((tmp_dir / "probe.json").read_text(encoding="utf-8"))
        extracted = json.loads((out_dir / "document.json").read_text(encoding="utf-8"))
        reading_md = (out_dir / "reading_order.md").read_text(encoding="utf-8")

        skill_checks = {
            "left_before_right_order": 0 <= reading_md.find("LEFT_STREAM_SECOND") < reading_md.find("RIGHT_STREAM_FIRST"),
            "healed_hyphenation": "scramble multi-column" in reading_md,
            "stripped_repeating_header_footer": "Journal of Synthetic Benchmarks" not in reading_md and len(extracted["headers_footers"]) >= 3,
            "extracted_metadata_title_and_abstract": bool((extracted.get("metadata") or {}).get("title") and (extracted.get("metadata") or {}).get("abstract")),
            "extracted_and_merged_continued_tables": len(extracted.get("merged_tables", [])) >= 1 and extracted["merged_tables"][0]["num_rows"] >= 4,
            "merged_split_equation_label": any(eq.get("label") == "1" and "CO2" in eq.get("raw_text", "") for eq in extracted.get("equations", [])),
            "detected_printed_page_discrepancy": bool(probe["summary"].get("page_numbering_discrepancy") and extracted.get("page_numbering_discrepancy")),
        }

        baseline_checks = {
            "left_before_right_order": naive_left_before_right,
            "healed_hyphenation": naive_healed_hyphen,
            "stripped_repeating_header_footer": naive_stripped_hf,
            "extracted_metadata_title_and_abstract": False,
            "extracted_and_merged_continued_tables": False,
            "merged_split_equation_label": False,
            "detected_printed_page_discrepancy": False,
        }

        return {
            "notice": "SYNTHETIC_BENCHMARK_ONLY",
            "baseline_passed": sum(1 for v in baseline_checks.values() if v),
            "skill_passed": sum(1 for v in skill_checks.values() if v),
            "total_checks": len(skill_checks),
            "baseline_checks": baseline_checks,
            "skill_checks": skill_checks,
        }


if __name__ == "__main__":
    res = run_benchmark()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("status") == "skipped":
        raise SystemExit(0)
    raise SystemExit(0 if res["skill_passed"] == res["total_checks"] else 1)
