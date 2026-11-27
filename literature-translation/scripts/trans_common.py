#!/usr/bin/env python3
"""Shared utilities for evidence-aligned-translation: bytecode guard, inline-tag-safe XML/text helpers, sentence splitting, and normalization."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.dont_write_bytecode = True

LIGATURE_MAP = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "ft",
    "\ufb06": "st",
    "\u00a0": " ",
    "\u200b": "",
}

PRESERVED_INLINE_TAGS = {
    "sub", "sup", "italic", "bold", "sc", "monospace", "underline", "overline", "xref", "ext-link", "tex-math",
}

ABBREV_TAIL_RE = re.compile(
    r"(?:\b(?:Fig|Figs|Eq|Eqs|Tab|Ref|Refs|Vol|No|pp|Dr|Prof|Mr|Ms|vs|etc|al|e\.g|i\.e|ca|approx|est|resp|viz|cf|St)\.|"
    r"\b[A-Z]\.)$",
    re.I,
)

PANEL_RE = re.compile(r"[(（]([A-Za-z]\d{1,2}|\d{1,2}[A-Za-z]|[A-G])[)）]")


def normalize_ligatures(text: str) -> str:
    if not text:
        return ""
    for k, v in LIGATURE_MAP.items():
        if k in text:
            text = text.replace(k, v)
    return text


def detect_encoding_defects(text: str) -> Dict[str, Any]:
    """Detect replacement chars, cid tokens, PUA glyphs, and non-whitespace control chars."""
    if not text:
        return {"total_chars": 0, "has_defects": False, "defect_rate": 0.0}
    total = len(text)
    repl = text.count("\ufffd")
    cids = len(re.findall(r"\(cid:\d+\)|cid:\d+", text, re.I))
    pua = sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF)
    ctrl = sum(1 for ch in text if ord(ch) < 32 and ch not in ("\n", "\r", "\t"))
    bad = repl + cids * 5 + pua + ctrl
    return {
        "total_chars": total,
        "replacement_chars": repl,
        "cid_tokens": cids,
        "pua_chars": pua,
        "control_chars": ctrl,
        "defect_rate": round(bad / max(1, total), 4),
        "has_defects": (repl > 0 or cids > 0 or pua > 0 or ctrl > 0),
    }


def strip_ns(tag: str) -> str:
    """Strip XML namespace URI prefix."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def serialize_xml_inner(elem: ET.Element) -> str:
    """Serialize element content while preserving meaningful inline tags (<sub>, <sup>, <italic>, <xref>, etc.) verbatim."""
    parts: List[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        ctag = strip_ns(child.tag).lower()
        if ctag in PRESERVED_INLINE_TAGS:
            inner = serialize_xml_inner(child)
            attrs = ""
            if ctag == "xref":
                rid = child.attrib.get("rid")
                rtype = child.attrib.get("ref-type")
                if rtype:
                    attrs += f' ref-type="{rtype}"'
                if rid:
                    attrs += f' rid="{rid}"'
            parts.append(f"<{ctag}{attrs}>{inner}</{ctag}>")
        elif ctag == "alternatives":
            # JATS <alternatives> wraps redundant representations (tex-math, mml:math, graphic); keep only best single form
            alt_text = ""
            for alt_ch in child:
                if strip_ns(alt_ch.tag).lower() == "tex-math":
                    cand = serialize_xml_inner(alt_ch)
                    if cand:
                        alt_text = f"<tex-math>{cand}</tex-math>"
                        break
            if not alt_text:
                for alt_ch in child:
                    if strip_ns(alt_ch.tag).lower() in ("inline-graphic", "graphic", "math"):
                        continue
                    cand = serialize_xml_inner(alt_ch)
                    if cand:
                        alt_text = cand
                        break
            if alt_text:
                parts.append(alt_text)
        elif ctag == "break":
            parts.append(" ")
        else:
            parts.append(serialize_xml_inner(child))
        if child.tail:
            parts.append(child.tail)
    raw = "".join(parts)
    # Collapse excess newlines/tabs while keeping single spaces around preserved inline tags
    return re.sub(r"[ \t\r\n]+", " ", normalize_ligatures(raw)).strip()


def split_sentences_preserving_markup(text: str) -> List[str]:
    """Split English/CJK paragraph into sentences without breaking inline XML tags, abbreviations, decimals, or panel labels."""
    st = text.strip()
    if not st:
        return []
    # Protect inline XML tags and numeric patterns cleanly by masking tag blocks during boundary scan
    tag_spans: List[Tuple[int, int]] = []
    for m in re.finditer(r"<([A-Za-z][A-Za-z0-9\-]*)(?:\s[^>]*)?>.*?</\1>|<[A-Za-z][A-Za-z0-9\-]*/?>", st):
        tag_spans.append((m.start(), m.end()))

    def _in_tag(pos: int) -> bool:
        return any(a <= pos < b for a, b in tag_spans)

    cuts: List[int] = []
    n = len(st)
    i = 0
    while i < n:
        ch = st[i]
        if ch in ("。", "！", "？") and not _in_tag(i):
            end_pos = i + 1
            while end_pos < n and st[end_pos] in ('"', "”", "’", ")", "）", "]", " "):
                end_pos += 1
            cuts.append(end_pos)
            i = end_pos
            continue
        if ch in (".", "!", "?") and not _in_tag(i):
            # Ignore decimals (e.g. 2.5), URLs, or abbreviations (Fig., e.g., et al.)
            prev_ch = st[i - 1] if i > 0 else ""
            next_ch = st[i + 1] if i + 1 < n else ""
            if ch == "." and prev_ch.isdigit() and next_ch.isdigit():
                i += 1
                continue
            prefix = st[max(0, i - 16): i + 1]
            if ch == "." and ABBREV_TAIL_RE.search(prefix):
                i += 1
                continue
            # Check trailing closing quotes/parens/panel tags/spaces before next capitalized/CJK sentence
            j = i + 1
            while j < n and st[j] in ('"', "”", "’", ")", "）", "]"):
                j += 1
            # Do not split before inline trailing xref or closing tag if still mid-clause
            if j < n and st[j] == "<" and re.match(r"</|<(?:xref|sup)\b", st[j:], re.I):
                i += 1
                continue
            if j == n or st[j].isspace() or _is_cjk(st[j]):
                # Peek next non-space token
                k = j
                while k < n and st[k].isspace():
                    k += 1
                if k == n or st[k].isupper() or _is_cjk(st[k]) or st[k] in ("(", "（", "[", "\"", "“", "<"):
                    # Guard against 'Fig. 4.' followed by lowercase/punctuation or mid-abbreviation 'e.g. x'
                    if k < n and st[k].islower():
                        i += 1
                        continue
                    cuts.append(k)
                    i = k
                    continue
        i += 1

    if not cuts:
        return [st]
    sents: List[str] = []
    start = 0
    for c in cuts:
        seg = st[start:c].strip()
        if seg:
            sents.append(seg)
        start = c
    tail = st[start:].strip()
    if tail:
        sents.append(tail)
    return sents


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return (0x4E00 <= code <= 0x9FFF) or (0x3400 <= code <= 0x4DBF) or (0x3000 <= code <= 0x303F)


def normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", normalize_ligatures(text or "")).strip().lower()


def import_pymupdf() -> Any:
    try:
        import pymupdf  # type: ignore
        return pymupdf
    except ImportError:
        import fitz  # type: ignore
        return fitz


def discover_capabilities() -> Dict[str, Any]:
    import importlib.util
    import shutil

    mods: Dict[str, Any] = {}
    mods["pymupdf_fitz"] = (importlib.util.find_spec("pymupdf") is not None) or (importlib.util.find_spec("fitz") is not None)
    for pkg, alias in (
        ("pdfplumber", "pdfplumber"),
        ("pypdf", "pypdf"),
        ("pypdfium2", "pypdfium2"),
        ("PIL", "pillow"),
    ):
        mods[alias] = importlib.util.find_spec(pkg) is not None
    clis = {name: shutil.which(name) for name in ("pdftotext", "pdftoppm", "pdfinfo", "tesseract")}
    return {"python_modules": mods, "cli_tools": clis}
