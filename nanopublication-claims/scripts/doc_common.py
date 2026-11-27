#!/usr/bin/env python3
"""Shared utilities for Nanopublication extraction, geometry, symbol repair, hashing, and contract mapping."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

LIGATURE_MAP = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "ft",
    "\ufb06": "st",
    "\u00ad": "",
    "\u200b": "",
    "\ufeff": "",
}

CID_PATTERN = re.compile(r"\(cid:\d+\)")
DOI_PATTERN = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b")
STAT_ANOMALY_RE = re.compile(
    r"(?:\b2?\s*p\s*,\s*0?\.\d+|\bp\s*[?]\s*0?\.\d+|\bCI\s*[:=]?\s*\d+\s*,\s*\d+)",
    re.I,
)

SYMBOL_PUA_MAP = {
    0xF044: "Δ", 0xF046: "Φ", 0xF047: "Γ", 0xF048: "Η", 0xF04C: "Λ", 0xF050: "Π",
    0xF051: "Θ", 0xF053: "Σ", 0xF057: "Ω", 0xF058: "Ξ", 0xF059: "Ψ",
    0xF061: "α", 0xF062: "β", 0xF063: "χ", 0xF064: "δ", 0xF065: "ε", 0xF066: "φ",
    0xF067: "γ", 0xF068: "η", 0xF069: "ι", 0xF06B: "κ", 0xF06C: "λ", 0xF06D: "μ",
    0xF06E: "ν", 0xF06F: "ο", 0xF070: "π", 0xF071: "θ", 0xF072: "ρ", 0xF073: "σ",
    0xF074: "τ", 0xF075: "υ", 0xF077: "ω", 0xF078: "ξ", 0xF079: "ψ", 0xF07A: "ζ",
    0xF0B1: "±", 0xF0A3: "≤", 0xF0B3: "≥", 0xF0B4: "×", 0xF0B8: "÷", 0xF0BB: "≈",
    0xF0B9: "≠", 0xF0A5: "∞", 0xF0B0: "°", 0xF0AE: "→", 0xF0AC: "←", 0xF0D7: "·",
}
SYMBOL_ASCII_MAP = {chr(k - 0xF000): v for k, v in SYMBOL_PUA_MAP.items() if 0xF041 <= k <= 0xF07A}


def is_cjk(ch: str) -> bool:
    """Return True if character is a CJK unified ideograph."""
    return "\u4e00" <= ch <= "\u9fff"


def sha256_text(text: str) -> str:
    """Return hex SHA-256 of UTF-8 normalized text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> Optional[str]:
    """Compute SHA-256 of a file cleanly."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def normalize_ligatures(text: str) -> str:
    """Normalize typographic ligatures and invisible characters while preserving Greek/math symbols."""
    if not text:
        return ""
    for src, dst in LIGATURE_MAP.items():
        if src in text:
            text = text.replace(src, dst)
    return text


def decode_symbol_pua(txt: str, font_name: str) -> str:
    """Decode Windows Symbol PUA codepoints (U+F0xx) and isolated PostScript Symbol letters."""
    if "symbol" not in (font_name or "").lower():
        return txt
    has_pua = any(0xF000 <= ord(ch) <= 0xF0FF for ch in txt)
    allow_ascii = (not has_pua) and (not re.search(r"[A-Za-z]{2,}", txt))
    out: List[str] = []
    for ch in txt:
        code = ord(ch)
        if code in SYMBOL_PUA_MAP:
            out.append(SYMBOL_PUA_MAP[code])
        elif allow_ascii and ch in SYMBOL_ASCII_MAP:
            out.append(SYMBOL_ASCII_MAP[ch])
        else:
            out.append(ch)
    return "".join(out)


def detect_encoding_defects(text: str) -> Dict[str, Any]:
    """Detect replacement chars, CID placeholders, PUA glyphs, control chars, and damaged stat glyphs."""
    if not text:
        return {
            "total_chars": 0, "replacement_chars": 0, "cid_tokens": 0,
            "pua_chars": 0, "control_chars": 0, "stat_glyph_anomalies": [],
            "defect_rate": 0.0, "has_defects": False,
        }
    total = len(text)
    replacement_chars = text.count("\ufffd")
    cid_matches = CID_PATTERN.findall(text)
    cid_tokens = len(cid_matches)
    cid_char_len = sum(len(m) for m in cid_matches)
    stat_anomalies = [m.group(0) for m in STAT_ANOMALY_RE.finditer(text)]
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
    return {
        "total_chars": total,
        "replacement_chars": replacement_chars,
        "cid_tokens": cid_tokens,
        "pua_chars": pua_chars,
        "control_chars": control_chars,
        "stat_glyph_anomalies": stat_anomalies,
        "defect_rate": round(defect_units / effective_total, 4),
        "has_defects": (defect_units > 0) or bool(stat_anomalies),
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


def discover_capabilities() -> Dict[str, Any]:
    """Probe available Python PDF/OCR/RDF libraries and system CLI tools dynamically."""
    py_mods = {
        "pymupdf_fitz": _module_version("pymupdf") or _module_version("fitz"),
        "pdfplumber": _module_version("pdfplumber"),
        "pypdf": _module_version("pypdf"),
        "pypdf2": _module_version("PyPDF2"),
        "pypdfium2": _module_version("pypdfium2"),
        "pillow": _module_version("PIL"),
        "rdflib": _module_version("rdflib"),
        "rapidocr_onnxruntime": _module_version("rapidocr_onnxruntime"),
    }
    cli_tools = {
        cmd: shutil.which(cmd)
        for cmd in ("pdftotext", "pdftoppm", "pdfinfo", "pdffonts", "mutool", "tesseract", "soffice")
    }
    tesseract_langs: List[str] = []
    if cli_tools["tesseract"]:
        try:
            proc = subprocess.run(
                [cli_tools["tesseract"], "--list-langs"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            for line in (proc.stdout or "").splitlines():
                s = line.strip()
                if s and not s.lower().startswith("list of available"):
                    tesseract_langs.append(s)
        except Exception:
            pass
    return {"python_modules": py_mods, "cli_tools": cli_tools, "tesseract_langs": tesseract_langs}


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
    """Normalize whitespace, hyphenated linebreaks, ligatures, and inline XML/script tags for evidence matching."""
    text = normalize_ligatures(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_term_key(term: str) -> str:
    """Canonical key for entity/predicate/term deduplication."""
    t = normalize_ligatures(term).lower().strip()
    t = re.sub(r"[\s\-_/]+", " ", t)
    t = re.sub(r"[^\w\s\u4e00-\u9fffα-ωΑ-Ω]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_header_footer_key(text: str) -> str:
    """Normalize repeating header/footer strings by replacing page numbers with '#'."""
    t = normalize_for_match(text)
    t = re.sub(r"\b\d+\b", "#", t)
    return t.lower()


def load_predicate_vocab(vocab_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load controlled predicate vocabulary and alias index."""
    if vocab_path is None:
        vocab_path = Path(__file__).resolve().parent.parent / "references" / "predicate-vocab.json"
    if not vocab_path.exists():
        return {"predicates": {}, "alias_to_canonical": {}, "vague_predicates": []}
    data = json.loads(vocab_path.read_text(encoding="utf-8"))
    alias_map: Dict[str, str] = {}
    for pname, meta in (data.get("predicates") or {}).items():
        alias_map[normalize_term_key(pname)] = pname
        for al in meta.get("aliases") or []:
            alias_map[normalize_term_key(al)] = pname
    data["alias_to_canonical"] = alias_map
    return data


def collect_contract_keys(contract: Any) -> List[str]:
    """Safely extract expected top-level field names from case.json's output_contract."""
    fields: List[str] = []
    if isinstance(contract, dict):
        if isinstance(contract.get("required_fields"), list):
            fields.extend(str(f) for f in contract["required_fields"])
        elif isinstance(contract.get("fields"), list):
            for item in contract["fields"]:
                if isinstance(item, str):
                    fields.append(item)
                elif isinstance(item, dict) and item.get("name"):
                    fields.append(str(item["name"]))
        elif isinstance(contract.get("properties"), dict):
            req = contract.get("required")
            if isinstance(req, list):
                fields.extend(str(f) for f in req)
            else:
                fields.extend(str(k) for k in contract["properties"].keys())
        elif isinstance(contract.get("result_schema"), dict) and isinstance(contract["result_schema"].get("required"), list):
            fields.extend(str(f) for f in contract["result_schema"]["required"])
        else:
            meta_keys = {
                "format", "description", "notes", "schema_version", "file", "filename",
                "deliverables", "files", "artifacts", "constraints", "evidence_requirements",
                "primary_output", "rules", "example", "title", "type", "output_dir",
            }
            for k, v in contract.items():
                if k not in meta_keys and not isinstance(v, dict):
                    fields.append(str(k))
    elif isinstance(contract, list):
        for item in contract:
            if isinstance(item, str):
                fields.append(item)
            elif isinstance(item, dict) and item.get("name"):
                fields.append(str(item["name"]))
    return list(dict.fromkeys(fields))
