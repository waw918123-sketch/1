#!/usr/bin/env python3
"""One-shot Harbor orchestrator: discovers inputs, probes PDFs, extracts layout/tables, runs targeted OCR, and writes case_brief.json."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List

sys.dont_write_bytecode = True

SCRIPTS_DIR = Path(__file__).resolve().parent


def _inspect_non_pdf(path: Path, work_dir: Path) -> Dict[str, Any]:
    """Extract lightweight text preview for non-PDF inputs (.txt, .md, .csv, .json, .docx, .xlsx, .pptx)."""
    ext = path.suffix.lower()
    info: Dict[str, Any] = {"file": str(path), "ext": ext, "size_bytes": path.stat().st_size}
    if ext in (".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml"):
        txt = path.read_text(encoding="utf-8", errors="replace")
        info["line_count"] = len(txt.splitlines())
        info["preview"] = txt[:400]
    elif ext in (".docx", ".pptx", ".xlsx"):
        try:
            texts: List[str] = []
            with zipfile.ZipFile(path, "r") as zf:
                xml_names = sorted(
                    n for n in zf.namelist()
                    if n.endswith(".xml") and (
                        n.startswith("word/document")
                        or n.startswith("word/footnotes")
                        or n.startswith("word/header")
                        or n.startswith("word/footer")
                        or n.startswith("ppt/slides/slide")
                        or n.startswith("xl/sharedStrings")
                        or n.startswith("xl/worksheets/sheet")
                    )
                )
                for xname in xml_names[:20]:
                    raw_xml = zf.read(xname)
                    try:
                        root = ET.fromstring(raw_xml)
                        texts.append(" ".join(t.strip() for t in root.itertext() if t and t.strip()))
                    except ET.ParseError:
                        # Fallback text extraction if XML fragment has unbound namespace prefixes
                        stripped = re.sub(r"<[^>]+>", " ", raw_xml.decode("utf-8", errors="replace"))
                        texts.append(stripped)
            joined = re.sub(r"\s+", " ", "\n".join(texts)).strip()
            out_txt = work_dir / f"{path.stem}_{ext.lstrip('.')}.txt"
            out_txt.write_text(joined, encoding="utf-8")
            info["extracted_text_path"] = str(out_txt)
            info["char_count"] = len(joined)
        except Exception as exc:
            info["extract_error"] = str(exc)
    elif ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
        info["kind"] = "image_asset"
    return info


def _discover_case_paths(obj: Any, base_dir: Path) -> List[Path]:
    """Recursively discover existing input file paths declared inside case.json."""
    allowed_exts = {
        ".pdf", ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml",
        ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
    }
    found: List[Path] = []
    seen: set[str] = set()

    def _walk(val: Any) -> None:
        if isinstance(val, str):
            s = val.strip()
            if not s or len(s) > 300 or "\n" in s:
                return
            p = Path(s)
            if p.suffix.lower() not in allowed_exts:
                return
            cands = [p] if p.is_absolute() else [base_dir / p, base_dir.parent / p]
            for cand in cands:
                try:
                    if cand.exists() and cand.is_file():
                        r = str(cand.resolve())
                        if r not in seen:
                            seen.add(r)
                            found.append(cand)
                        break
                except OSError:
                    continue
        elif isinstance(val, list):
            for item in val:
                _walk(item)
        elif isinstance(val, dict):
            for k, item in val.items():
                if k in ("output_contract", "output_schema", "expected_output", "result_schema"):
                    continue
                _walk(item)

    _walk(obj)
    return found


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

    declared_paths = _discover_case_paths(case_data, input_dir) if case_data else []
    pdfs: List[Path] = [Path(p) for p in args.pdf if Path(p).exists()]
    if not pdfs:
        seen_pdfs: set[str] = set()
        cand_pdfs = (sorted(input_dir.rglob("*.pdf")) if input_dir.exists() else []) + [
            p for p in declared_paths if p.suffix.lower() == ".pdf"
        ]
        for p in cand_pdfs:
            rp = str(p.resolve())
            if rp not in seen_pdfs:
                seen_pdfs.add(rp)
                pdfs.append(p)

    other_files = []
    seen_other: set[str] = set()
    other_root = work_dir / "other_inputs"
    cand_others = (
        [p for p in sorted(input_dir.rglob("*")) if p.is_file() and p.suffix.lower() != ".pdf" and p.name not in ("case.json", "task.md")]
        if input_dir.exists()
        else []
    ) + [p for p in declared_paths if p.suffix.lower() != ".pdf" and p.name not in ("case.json", "task.md")]
    for p in cand_others:
        rp = str(p.resolve())
        if rp not in seen_other:
            seen_other.add(rp)
            other_root.mkdir(parents=True, exist_ok=True)
            other_files.append(_inspect_non_pdf(p, other_root))

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
            entry["metadata"] = doc_obj.get("metadata")
            entry["page_numbering_discrepancy"] = doc_obj.get("page_numbering_discrepancy", False)
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

        entry["gain_diagnostics"] = {
            "multi_column_pages_reordered": len(summ.get("multi_column_pages") or []),
            "headers_footers_stripped": (ext_res.get("counts") or {}).get("headers_footers", 0),
            "tables_extracted": (ext_res.get("counts") or {}).get("tables", 0),
            "merged_cross_page_tables": (ext_res.get("counts") or {}).get("merged_tables", 0),
            "figures_detected": (ext_res.get("counts") or {}).get("figures", 0),
            "equations_detected": (ext_res.get("counts") or {}).get("equations", 0),
            "unextracted_regions_flagged": (ext_res.get("counts") or {}).get("unextracted_regions", 0),
            "ocr_rendered_items": entry.get("ocr_rendered_count", 0),
        }
        entry["status"] = "ok" if ext_res.get("status") == "ok" else "partial"
        docs_summary.append(entry)

    task_preview = None
    if task_path.exists():
        try:
            task_preview = task_path.read_text(encoding="utf-8", errors="replace")[:1000]
        except Exception:
            task_preview = None

    brief = {
        "case_id": case_data.get("case_id"),
        "has_case_json": case_path.exists(),
        "has_task_md": task_path.exists(),
        "task_md_preview": task_preview,
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
