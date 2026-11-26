#!/usr/bin/env python3
"""Shared utilities for scientific PDF probing, layout extraction, OCR rendering, and evidence auditing."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple


LIGATURE_MAP = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "ft",
    "\ufb06": "st",
    "\u00ad": "",  # soft hyphen
    "\u200b": "",  # zero-width space
    "\ufeff": "",  # BOM
}

CID_PATTERN = re.compile(r"\(cid:\d+\)")


def normalize_ligatures(text: str) -> str:
    """Normalize typographic ligatures and invisible characters while preserving Greek/math symbols."""
    if not text:
        return ""
    for src, dst in LIGATURE_MAP.items():
        if src in text:
            text = text.replace(src, dst)
    return text


def detect_encoding_defects(text: str) -> Dict[str, Any]:
    """Detect replacement characters, CID placeholders, and PUA glyphs in extracted text."""
    if not text:
        return {
            "total_chars": 0,
            "replacement_chars": 0,
            "cid_tokens": 0,
            "pua_chars": 0,
            "control_chars": 0,
            "defect_rate": 0.0,
            "has_defects": False,
        }
    total = len(text)
    replacement_chars = text.count("\ufffd")
    cid_matches = CID_PATTERN.findall(text)
    cid_tokens = len(cid_matches)
    cid_char_len = sum(len(m) for m in cid_matches)
    pua_chars = 0
    control_chars = 0
    for ch in text:
        code = ord(ch)
        if 0xE000 <= code <= 0xF8FF or 0xF0000 <= code <= 0xFFFFD:
            pua_chars += 1
        elif unicodedata.category(ch) == "Cc" and ch not in ("\n", "\r", "\t"):
            control_chars += 1

    defect_units = replacement_chars + cid_tokens + pua_chars + control_chars
    effective_total = max(1, total - cid_char_len + cid_tokens)
    defect_rate = round(defect_units / effective_total, 4)
    return {
        "total_chars": total,
        "replacement_chars": replacement_chars,
        "cid_tokens": cid_tokens,
        "pua_chars": pua_chars,
        "control_chars": control_chars,
        "defect_rate": defect_rate,
        "has_defects": defect_units > 0,
    }


def import_pymupdf() -> Any:
    """Import PyMuPDF via modern `pymupdf` name first, falling back to `fitz`."""
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:
        import fitz  # type: ignore

        return fitz


def _module_version(name: str) -> Optional[str]:
    if importlib.util.find_spec(name) is None:
        return None
    try:
        mod = __import__(name)
        for attr in ("__version__", "VERSION", "version"):
            val = getattr(mod, attr, None)
            if isinstance(val, str):
                return val
            if isinstance(val, tuple):
                return ".".join(str(x) for x in val)
        return "installed"
    except Exception:
        return "installed"


def _cli_info(cmd: str) -> Optional[str]:
    path = shutil.which(cmd)
    return path


def discover_capabilities() -> Dict[str, Any]:
    """Probe available Python PDF/OCR libraries and system CLI tools without assuming any."""
    py_mods = {
        "pymupdf_fitz": _module_version("pymupdf") or _module_version("fitz"),
        "pdfplumber": _module_version("pdfplumber"),
        "pypdf": _module_version("pypdf"),
        "pypdf2": _module_version("PyPDF2"),
        "pypdfium2": _module_version("pypdfium2"),
        "pillow": _module_version("PIL"),
        "numpy": _module_version("numpy"),
        "pytesseract": _module_version("pytesseract"),
        "rapidocr_onnxruntime": _module_version("rapidocr_onnxruntime"),
    }
    cli_tools = {
        "pdftotext": _cli_info("pdftotext"),
        "pdftoppm": _cli_info("pdftoppm"),
        "pdfinfo": _cli_info("pdfinfo"),
        "pdffonts": _cli_info("pdffonts"),
        "mutool": _cli_info("mutool"),
        "tesseract": _cli_info("tesseract"),
        "soffice": _cli_info("soffice"),
        "fc-list": _cli_info("fc-list"),
    }
    tesseract_langs: List[str] = []
    if cli_tools["tesseract"]:
        try:
            proc = subprocess.run(
                [cli_tools["tesseract"], "--list-langs"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            lines = (proc.stdout or "").splitlines()
            for line in lines:
                s = line.strip()
                if s and not s.lower().startswith("list of available"):
                    tesseract_langs.append(s)
        except Exception:
            pass

    return {
        "python_modules": py_mods,
        "cli_tools": cli_tools,
        "tesseract_langs": tesseract_langs,
    }


def parse_page_spec(spec: Optional[str], total_pages: int) -> List[int]:
    """Parse 1-based page specification like '1,3-5,8' into sorted unique 1-based page numbers."""
    if not spec or spec.strip().lower() == "all":
        return list(range(1, total_pages + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_str, b_str = part.split("-", 1)
            a = max(1, int(a_str.strip()))
            b = min(total_pages, int(b_str.strip()))
            if a <= b:
                pages.update(range(a, b + 1))
        else:
            p = int(part)
            if 1 <= p <= total_pages:
                pages.add(p)
    return sorted(pages)


def bbox_area(b: Sequence[float]) -> float:
    return max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))


def bbox_iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix0 = max(float(a[0]), float(b[0]))
    iy0 = max(float(a[1]), float(b[1]))
    ix1 = min(float(a[2]), float(b[2]))
    iy1 = min(float(a[3]), float(b[3]))
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0.0:
        return 0.0
    union = bbox_area(a) + bbox_area(b) - inter
    return inter / union if union > 0.0 else 0.0


def bbox_contains_center(outer: Sequence[float], inner: Sequence[float], margin: float = 2.0) -> bool:
    cx = 0.5 * (float(inner[0]) + float(inner[2]))
    cy = 0.5 * (float(inner[1]) + float(inner[3]))
    return (
        float(outer[0]) - margin <= cx <= float(outer[2]) + margin
        and float(outer[1]) - margin <= cy <= float(outer[3]) + margin
    )


def merge_bbox(boxes: Sequence[Sequence[float]]) -> List[float]:
    if not boxes:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        round(min(float(b[0]) for b in boxes), 2),
        round(min(float(b[1]) for b in boxes), 2),
        round(max(float(b[2]) for b in boxes), 2),
        round(max(float(b[3]) for b in boxes), 2),
    ]


def normalize_for_match(text: str) -> str:
    """Normalize whitespace, hyphenated linebreaks, and ligatures for evidence matching."""
    text = normalize_ligatures(text)
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_header_footer_key(text: str) -> str:
    """Normalize repeating header/footer string by replacing page numbers with '#'."""
    t = normalize_for_match(text)
    t = re.sub(r"\b\d+\b", "#", t)
    return t.lower()
