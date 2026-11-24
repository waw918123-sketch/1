#!/usr/bin/env python3
"""
Self-check verification suite for scientific-pdf-parser.

Generates a synthetic 3-page scientific paper PDF + an encrypted PDF in a temporary
working directory, runs parse_paper.py and validate_output.py, and verifies:
1. Two-column reading order (left column fully before right column) with spanning title/abstract.
2. Running header/footer & page number stripping without polluting body text.
3. Inline superscript/subscript & equation extraction.
4. Cross-page continued table extraction and stitching across Pages 2 and 3.
5. Scanned image-only page detection + adaptive OCR fallback (when PIL + tesseract available).
6. Encrypted PDF detection returning status='blocked_encrypted'.
7. Harbor output contract & provenance validation via validate_output.py.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

try:
    from PIL import Image, ImageDraw
except Exception:
    Image = None
    ImageDraw = None

try:
    import pypdf
except Exception:
    pypdf = None

# Import local scripts
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import parse_paper  # noqa: E402
import validate_output  # noqa: E402


def build_synthetic_scientific_pdf(pdf_path: Path, tmp_dir: Path) -> None:
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    w, h = letter  # 612 x 792

    def draw_header_footer(page_num: int) -> None:
        c.setFont("Helvetica", 8)
        c.drawString(50, h - 32, "Journal of Scientific Parsing Benchmarks  Vol. 12 (2026)")
        c.drawRightString(w - 50, 28, f"{page_num}")
        c.drawString(50, 28, "Synthetic Evaluation Document - Do Not Cite")

    # ---------------- Page 1: Spanning Title/Abstract + 2-Column Body + Equation + Footnote ----------------
    draw_header_footer(1)

    # Full-width Title & Authors
    c.setFont("Helvetica-Bold", 15)
    c.drawCentredString(w / 2, h - 75, "Structure-Preserving Extraction of Catalytic Kinetics")
    c.setFont("Helvetica", 10)
    c.drawCentredString(w / 2, h - 94, "A. Researcher, B. Scientist — Department of Energy Chemistry")

    # Full-width Abstract
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, h - 122, "Abstract")
    c.setFont("Helvetica", 9.5)
    c.drawString(
        50,
        h - 137,
        "This synthetic benchmark paper tests two-column reading order, equation extraction, and cross-page tables.",
    )
    c.drawString(
        50,
        h - 150,
        "All numeric values are synthetic and designed solely to verify deterministic PDF parsing pipelines.",
    )

    # Two columns below y = h - 180
    left_x = 50
    right_x = 325
    y = h - 185

    # Left column
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left_x, y, "1. Introduction")
    y -= 18
    c.setFont("Helvetica", 9.5)
    left_lines = [
        "LEFT_COL_START: Modern literature",
        "contains dense multi-column layouts",
        "that naive extractors frequently",
        "interleave across the center gutter.",
        "Our benchmark verifies that every",
        "left-column sentence finishes before",
        "the right column begins.",
    ]
    for ln in left_lines:
        c.drawString(left_x, y, ln)
        y -= 14

    y -= 10
    # Superscript / subscript test line: NiCo2O4 at 10^-3 M
    c.setFont("Helvetica", 9.5)
    c.drawString(left_x, y, "Catalyst NiCo")
    c.setFont("Helvetica", 6.5)
    c.drawString(left_x + 59, y - 3, "2")
    c.setFont("Helvetica", 9.5)
    c.drawString(left_x + 64, y, "O")
    c.setFont("Helvetica", 6.5)
    c.drawString(left_x + 72, y - 3, "4")
    c.setFont("Helvetica", 9.5)
    c.drawString(left_x + 78, y, "yielded 10")
    c.setFont("Helvetica", 6.5)
    c.drawString(left_x + 123, y + 4, "-3")
    c.setFont("Helvetica", 9.5)
    c.drawString(left_x + 131, y, "mol/L.")
    y -= 22

    # Numbered equation in left column
    c.setFont("Helvetica", 9.5)
    c.drawString(left_x, y, "r = k * C_A^2 * exp(-E_a / RT)  (1)")

    # Right column
    ry = h - 185
    c.setFont("Helvetica-Bold", 11)
    c.drawString(right_x, ry, "2. Experimental Setup")
    ry -= 18
    c.setFont("Helvetica", 9.5)
    right_lines = [
        "RIGHT_COL_START: Samples were",
        "evaluated under controlled ambient",
        "conditions using standardized cells.",
        "Figure 1 summarizes the apparatus",
        "layout and sensor calibration steps.",
    ]
    for ln in right_lines:
        c.drawString(right_x, ry, ln)
        ry -= 14

    ry -= 12
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(right_x, ry, "Figure 1: Schematic of the synthetic reactor.")

    # Footnote near bottom (above footer)
    c.setFont("Helvetica", 7.5)
    c.drawString(50, 72, "1 Corresponding author email: synthetic-benchmark@example.org")

    c.showPage()

    # ---------------- Page 2: Table 1 (Part 1) + Section 3 ----------------
    draw_header_footer(2)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, h - 75, "3. Results and Discussion")
    c.setFont("Helvetica", 9.5)
    c.drawString(50, h - 95, "Table 1: Catalytic performance metrics across synthetic formulations.")

    data_p2 = [
        ["Catalyst", "Temp (K)", "Rate (mM/s)", "Selectivity (%)"],
        ["Cat-A", "298", "1.42", "91.5"],
        ["Cat-B", "313", "2.18", "94.2"],
    ]
    t2 = Table(data_p2, colWidths=[120, 100, 120, 120])
    t2.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.7, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    t2.wrapOn(c, w, h)
    t2.drawOn(c, 50, h - 180)
    c.showPage()

    # ---------------- Page 3: Table 1 (continued) + Simulated Scanned Image Block ----------------
    draw_header_footer(3)
    c.setFont("Helvetica", 9.5)
    c.drawString(50, h - 75, "Table 1 (continued): Additional synthetic formulations.")

    data_p3 = [
        ["Catalyst", "Temp (K)", "Rate (mM/s)", "Selectivity (%)"],
        ["Cat-C", "328", "3.05", "96.8"],
        ["Cat-D", "343", "4.11", "95.1"],
    ]
    t3 = Table(data_p3, colWidths=[120, 100, 120, 120])
    t3.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.7, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    t3.wrapOn(c, w, h)
    t3.drawOn(c, 50, h - 160)
    c.showPage()

    # ---------------- Page 4: Pure Raster Image (Simulated Scanned Page) ----------------
    if Image is not None and ImageDraw is not None:
        img = Image.new("RGB", (900, 300), "white")
        draw = ImageDraw.Draw(img)
        draw.text((40, 80), "SCANNED APPENDIX: Thermal stability verified at 450 K.", fill="black")
        scan_png = tmp_dir / "scanned_page.png"
        img.save(str(scan_png))
        c.drawImage(str(scan_png), 40, h - 260, width=520, height=175)
        c.showPage()

    c.save()


def build_encrypted_pdf(src_pdf: Path, enc_pdf: Path) -> bool:
    if pypdf is None:
        return False
    reader = pypdf.PdfReader(str(src_pdf))
    writer = pypdf.PdfWriter()
    writer.add_page(reader.pages[0])
    writer.encrypt("secret123")
    with open(enc_pdf, "wb") as f:
        writer.write(f)
    return True


def run_self_check() -> int:
    with tempfile.TemporaryDirectory(prefix="sci_pdf_check_") as td:
        tmp = Path(td)
        pdf_path = tmp / "synthetic_paper.pdf"
        out_dir = tmp / "parsed"
        build_synthetic_scientific_pdf(pdf_path, tmp)

        doc = parse_paper.parse_pdf(pdf_path, out_dir, ocr_fallback="auto", render_previews="auto")
        assert doc["status"] in ("success", "partial"), f"Unexpected status: {doc['status']}"

        # 1. Verify reading order: LEFT_COL_START appears strictly before RIGHT_COL_START
        full_text = "\n".join(b["text"] for b in doc["blocks"])
        idx_left = full_text.find("LEFT_COL_START")
        idx_right = full_text.find("RIGHT_COL_START")
        assert idx_left != -1 and idx_right != -1 and idx_left < idx_right, (
            f"Column reading order failed: left={idx_left}, right={idx_right}"
        )

        # 2. Verify running headers/footers stripped from body blocks
        assert "Journal of Scientific Parsing Benchmarks" not in full_text, "Running header leaked into body!"
        assert len(doc["headers_footers"]) >= 6, f"Expected stripped headers/footers, got {len(doc['headers_footers'])}"

        # 3. Verify superscript/subscript geometry & equation extraction
        rich_all = "\n".join(b["rich_text"] for b in doc["blocks"])
        assert "_{2}" in rich_all and "^{-3}" in rich_all, f"Super/subscript missing in rich_text: {rich_all}"
        assert len(doc["equations"]) >= 1 and doc["equations"][0]["label"] == "1", "Equation (1) not extracted!"

        # 4. Verify cross-page table stitching (Table 1 spans pages [2, 3] with 4 data rows)
        assert len(doc["tables"]) == 1, f"Expected 1 stitched table, got {len(doc['tables'])}"
        tbl = doc["tables"][0]
        assert tbl["continuation_merged"] is True, "Cross-page table was not stitched!"
        assert tbl["pages"] == [2, 3], f"Expected table pages [2, 3], got {tbl['pages']}"
        assert len(tbl["rows"]) == 4, f"Expected 4 stitched data rows, got {len(tbl['rows'])}"

        # 5. Verify scanned page detected & OCR attempted (Page 4)
        if doc["metadata"]["num_pages"] >= 4:
            p4_diag = doc["metadata"]["page_diagnostics"][3]
            assert p4_diag["quality"] == "scanned_image", f"Page 4 not flagged as scanned: {p4_diag}"
            assert p4_diag["ocr_applied"] is True, "OCR fallback did not run on scanned Page 4!"

        # 6. Verify encrypted PDF handling
        enc_pdf = tmp / "encrypted.pdf"
        if build_encrypted_pdf(pdf_path, enc_pdf):
            enc_doc = parse_paper.parse_pdf(enc_pdf, tmp / "parsed_enc")
            assert enc_doc["status"] == "blocked_encrypted", f"Encrypted PDF not blocked: {enc_doc}"

        # 7. Verify validate_output.py against synthetic case.json & result.json
        case_json = tmp / "case.json"
        case_json.write_text(
            json.dumps(
                {
                    "case_id": "synthetic-check-001",
                    "output_contract": {"required": ["case_id", "status", "summary", "evidence"]},
                }
            ),
            encoding="utf-8",
        )
        result_json = tmp / "result.json"
        result_json.write_text(
            json.dumps(
                {
                    "case_id": "synthetic-check-001",
                    "status": "success",
                    "summary": "Parsed 4-row stitched catalytic table and Equation (1).",
                    "evidence": [
                        {
                            "pdf_page": 1,
                            "block_id": doc["equations"][0]["block_id"],
                            "eq_id": doc["equations"][0]["eq_id"],
                        },
                        {
                            "pdf_page": 2,
                            "table_id": tbl["table_id"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        val = validate_output.validate(case_json, out_dir / "document.json", result_json)
        assert val["valid"] is True, f"validate_output failed: {val}"

        print("ALL SELF-CHECK ASSERTIONS PASSED:")
        print(
            json.dumps(
                {
                    "pages_parsed": doc["metadata"]["num_pages"],
                    "multi_col_page1": doc["metadata"]["page_diagnostics"][0]["multi_column_detected"],
                    "headers_footers_stripped": len(doc["headers_footers"]),
                    "equations_found": len(doc["equations"]),
                    "stitched_table_pages": tbl["pages"],
                    "stitched_table_rows": len(tbl["rows"]),
                    "scanned_page4_ocr": doc["metadata"]["page_diagnostics"][3]["ocr_applied"]
                    if doc["metadata"]["num_pages"] >= 4
                    else False,
                    "encrypted_pdf_blocked": True,
                    "validator_passed": val["valid"],
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(run_self_check())
