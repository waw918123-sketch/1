#!/usr/bin/env python3
"""One-shot Harbor orchestrator: discovers inputs, probes PDFs, extracts layout/tables, runs targeted OCR, and writes case_brief.json."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

SCRIPTS_DIR = Path(__file__).resolve().parent


def _run_py(script: str, args: List[str]) -> Dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS_DIR / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    out = proc.stdout.strip()
    try:
        data = json.loads(out) if out else {}
    except Exception:
        data = {"raw_stdout": out}
    if proc.returncode != 0 and "error" not in data:
        data["returncode"] = proc.returncode
        if proc.stderr.strip():
            data["stderr"] = proc.stderr.strip()
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Run full literature-doc-parser triage, extraction, and targeted OCR.")
    parser.add_argument("--input-dir", default="/input", help="Input directory containing case.json, task.md, and PDFs.")
    parser.add_argument("--work-dir", default="/app/work", help="Working output directory.")
    parser.add_argument("--pdf", action="append", default=[], help="Optional explicit PDF path(s).")
    parser.add_argument("--max-ocr-pages", type=int, default=12, help="Cap automatic full-page OCR renders per PDF.")
    parser.add_argument("--max-crops", type=int, default=8, help="Cap automatic defect/table crop renders per PDF.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    case_path = input_dir / "case.json"
    task_path = input_dir / "task.md"
    case_data: Dict[str, Any] = {}
    if case_path.exists():
        try:
            case_data = json.loads(case_path.read_text(encoding="utf-8"))
        except Exception as exc:
            case_data = {"parse_error": str(exc)}

    pdfs: List[Path] = [Path(p) for p in args.pdf if Path(p).exists()]
    if not pdfs and input_dir.exists():
        pdfs = sorted(input_dir.rglob("*.pdf"))

    other_files = []
    if input_dir.exists():
        other_files = [
            str(p) for p in sorted(input_dir.rglob("*"))
            if p.is_file() and p.suffix.lower() != ".pdf" and p.name not in ("case.json", "task.md")
        ]

    docs_summary: List[Dict[str, Any]] = []
    for idx, pdf in enumerate(pdfs):
        slug = f"doc_{idx + 1}_{pdf.stem}" if len(pdfs) > 1 else "parse"
        doc_dir = work_dir / slug
        doc_dir.mkdir(parents=True, exist_ok=True)

        probe_out = doc_dir / "probe.json"
        probe = _run_py("pdf_probe.py", [str(pdf), "--out", str(probe_out)])

        entry: Dict[str, Any] = {
            "pdf": str(pdf),
            "work_subdir": str(doc_dir),
            "probe_path": str(probe_out),
            "page_count": probe.get("page_count"),
            "needs_password": bool(probe.get("needs_password")),
            "valid_pdf_header": probe.get("valid_pdf_header", True),
            "summary": probe.get("summary", {}),
        }

        if entry["needs_password"] or not entry["valid_pdf_header"]:
            entry["status"] = "blocked"
            docs_summary.append(entry)
            continue

        ext_res = _run_py("pdf_extract.py", [str(pdf), "--out-dir", str(doc_dir)])
        entry["extract_status"] = ext_res.get("status")
        entry["counts"] = ext_res.get("counts")
        entry["outline_path"] = str(doc_dir / "outline.md")
        entry["reading_order_path"] = str(doc_dir / "reading_order.md")
        entry["tables_path"] = str(doc_dir / "tables.md")
        entry["document_json_path"] = str(doc_dir / "document.json")

        # Targeted OCR on scanned/garbled pages and unextracted defect/table crops
        summ = probe.get("summary") or {}
        need_pages = sorted(set((summ.get("scanned_pages") or []) + (summ.get("garbled_pages") or [])))[: args.max_ocr_pages]

        crops: List[str] = []
        doc_json_path = doc_dir / "document.json"
        if doc_json_path.exists():
            doc_obj = json.loads(doc_json_path.read_text(encoding="utf-8"))
            for reg in doc_obj.get("unextracted_regions", []):
                pno = reg.get("page")
                bb = reg.get("bbox")
                if isinstance(pno, int) and isinstance(bb, list) and len(bb) == 4 and bb != [0.0, 0.0, 0.0, 0.0]:
                    if pno not in need_pages:
                        crops.append(f"{pno}:{bb[0]},{bb[1]},{bb[2]},{bb[3]}")
                if len(crops) >= args.max_crops:
                    break

        if need_pages or crops:
            ocr_dir = doc_dir / "ocr"
            ocr_args = [str(pdf), "--out-dir", str(ocr_dir), "--dpi", "250", "--ocr", "auto"]
            if need_pages:
                ocr_args.extend(["--pages", ",".join(str(p) for p in need_pages)])
            for cr in crops:
                ocr_args.extend(["--crop", cr])
            ocr_res = _run_py("pdf_render_ocr.py", ocr_args)
            entry["ocr_results_path"] = str(ocr_dir / "ocr_results.json")
            entry["ocr_rendered_count"] = ocr_res.get("rendered_count", 0)
            entry["ocr_engine_used"] = ocr_res.get("ocr_engine_used")

        entry["status"] = "ok" if ext_res.get("status") == "ok" else "partial"
        docs_summary.append(entry)

    brief = {
        "case_id": case_data.get("case_id"),
        "has_case_json": case_path.exists(),
        "has_task_md": task_path.exists(),
        "output_contract": case_data.get("output_contract") or case_data.get("output_schema") or case_data.get("expected_output"),
        "pdf_count": len(pdfs),
        "other_input_files": other_files,
        "documents": docs_summary,
    }
    brief_path = work_dir / "case_brief.json"
    brief_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
