#!/usr/bin/env python3
"""Unified multi-format document ingestor, section-tree builder, chunker, and cue pre-extractor.

Supports PDF, JATS/PLOS XML, Markdown/TXT, DOCX, XLSX, and PPTX.
Produces:
  - document.json: normalized structure with hierarchical section tree & block-to-section mapping
  - outline.md: compact navigable skeleton (metadata, section tree, tables, figures, equations, defects)
  - section_chunks/: budget-safe (<=9,000 chars) section-aligned reading chunks
  - terminology_candidates.json: abbreviations, condition/assumption cues, limitation cues, numeric claims
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True

from doc_common import detect_encoding_defects, normalize_for_match, normalize_ligatures, normalize_term_key
from pdf_layout import HEADING_RE, extract_pdf, probe_pdf

# Regex patterns for deterministic terminology & precondition/limitation cue surfacing
ABBREV_PATTERNS = [
    re.compile(r"\b([A-Za-z][A-Za-z0-9\-\s/]{2,48}?)\s*\(([A-Z][A-Za-z0-9\-]{1,14})\)"),
    re.compile(r"([\u4e00-\u9fffA-Za-z0-9\-\s]{2,32}?)\s*[（(]([A-Z][A-Za-z0-9\-]{1,14})[）)]"),
]
CONDITION_CUE_RE = re.compile(
    r"\b(?:assum(?:e|ed|es|ing|ption)|subject\s+to|provided\s+that|only\s+valid|valid\s+(?:for|when)|"
    r"under\s+(?:the\s+)?condition|boundary\s+condition|steady[\-\s]state|exclusion\s+criteria|"
    r"inclusion\s+criteria|performed\s+at|measured\s+at|operated\s+at|calcined\s+at|"
    r"at\s+\d+(?:\.\d+)?\s*(?:K|°C|℃|bar|atm|MPa|kPa|Hz|MHz|GHz)|sample\s+size)\b|"
    r"(?:前提|假设|边界条件|稳态|适用条件|仅适用于|实验条件|在.{0,12}(?:下|条件下))",
    re.I,
)
LIMITATION_CUE_RE = re.compile(
    r"\b(?:limitation|limitations|drawback|caveat|shortcoming|weakness|cannot|failed\s+to|"
    r"did\s+not\s+(?:evaluate|test|include|measure)|future\s+work|remain(?:s)?\s+unclear|"
    r"not\s+reported|unreported|underestimate[ds]?|overestimate[ds]?|restricted\s+to)\b|"
    r"(?:局限性?|不足之处|缺陷|未报告|未能|尚未|未考察|未来工作|有待进一步)",
    re.I,
)
NUMERIC_CLAIM_RE = re.compile(
    r"(?:[A-Za-z0-9_\-\u4e00-\u9fff]{1,24}\s*(?:=|≈|<|>|≤|≥|of|was|were|reached|achieved)\s*)?"
    r"-?\d+(?:\.\d+)?(?:\s*[±]\s*\d+(?:\.\d+)?)?\s*(?:%|K|°C|℃|bar|atm|MPa|kPa|mg|g|kg|mol|mM|μM|nm|μm|mm|cm|mL|L|h|min|s|ms|eV|kJ/mol)\b"
)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _serialize_xml_inner(el: ET.Element) -> str:
    parts: List[str] = []
    if el.text:
        parts.append(el.text)
    for ch in el:
        t = _strip_ns(ch.tag).lower()
        inner = _serialize_xml_inner(ch)
        if t in ("sub", "sup", "italic", "bold", "xref"):
            parts.append(f"<{t}>{inner}</{t}>")
        else:
            parts.append(inner)
        if ch.tail:
            parts.append(ch.tail)
    return re.sub(r"\s+", " ", normalize_ligatures("".join(parts))).strip()


def _ingest_xml(xml_path: Path) -> Dict[str, Any]:
    """Ingest JATS/PLOS XML preserving exact section hierarchy, inline markup, tables, figures, and supplementary nodes."""
    root = ET.fromstring(xml_path.read_bytes())
    blocks: List[Dict[str, Any]] = []
    sections: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    figures: List[Dict[str, Any]] = []
    equations: List[Dict[str, Any]] = []
    references: List[Dict[str, Any]] = []

    b_idx = 0
    sec_idx = 0
    tbl_idx = 0
    fig_idx = 0
    eq_idx = 0
    ref_idx = 0

    def _next_bid() -> str:
        nonlocal b_idx
        b_idx += 1
        return f"p1_b{b_idx}"

    def _walk(el: ET.Element, loc: List[str], level: int, in_ref: bool) -> None:
        nonlocal sec_idx, tbl_idx, fig_idx, eq_idx, ref_idx
        tag = _strip_ns(el.tag).lower()
        if tag in ("journal-meta", "contrib-group", "aff", "history", "permissions", "counts"):
            return
        if tag == "ref-list":
            sec_idx += 1
            bid = _next_bid()
            sections.append({"section_id": f"sec_{sec_idx}", "title": "References", "level": max(1, level), "page_start": 1, "block_id": bid, "bbox": [0.0, 0.0, 0.0, 0.0]})
            blocks.append({"block_id": bid, "page": 1, "column": 1, "role": "heading", "heading_level": max(1, level), "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": "References", "text": "References", "locator": "/".join(loc), "defects": detect_encoding_defects("References")})
            for ch in el:
                _walk(ch, loc + [_strip_ns(ch.tag).lower()], level + 1, True)
            return

        if tag in ("sec", "abstract", "app", "supplementary-material"):
            t_el = el.find("title")
            t_txt = _serialize_xml_inner(t_el) if t_el is not None else ("Abstract" if tag == "abstract" else (el.attrib.get("id") or tag.title()))
            sec_idx += 1
            bid = _next_bid()
            sections.append({"section_id": f"sec_{sec_idx}", "title": t_txt, "level": max(1, level), "page_start": 1, "block_id": bid, "bbox": [0.0, 0.0, 0.0, 0.0]})
            blocks.append({"block_id": bid, "page": 1, "column": 1, "role": "heading", "heading_level": max(1, level), "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": t_txt, "text": t_txt, "locator": "/".join(loc), "defects": detect_encoding_defects(t_txt)})
            for ch in el:
                if ch is t_el:
                    continue
                _walk(ch, loc + [_strip_ns(ch.tag).lower()], level + 1, in_ref)
            return

        if tag == "table-wrap":
            tbl_idx += 1
            lbl = _serialize_xml_inner(el.find("label")) if el.find("label") is not None else f"Table {tbl_idx}"
            cap_el = el.find("caption")
            cap = _serialize_xml_inner(cap_el) if cap_el is not None else ""
            full_cap = f"{lbl}. {cap}".strip(". ")
            rows: List[List[str]] = []
            for tr in el.findall(".//tr"):
                row = [_serialize_xml_inner(cell) for cell in tr if _strip_ns(cell.tag).lower() in ("th", "td")]
                if row:
                    rows.append(row)
            bid = _next_bid()
            blocks.append({"block_id": bid, "page": 1, "column": 1, "role": "caption", "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": full_cap, "text": full_cap, "locator": "/".join(loc), "defects": detect_encoding_defects(full_cap)})
            hdrs = rows[0] if rows else []
            md_lines = []
            if rows:
                md_lines = ["| " + " | ".join(hdrs) + " |", "| " + " | ".join(["---"] * len(hdrs)) + " |"] + ["| " + " | ".join(r + [""] * (len(hdrs) - len(r))) + " |" for r in rows[1:]]
            tables.append({
                "table_id": f"tbl_{tbl_idx}", "page": 1, "bbox": [0.0, 0.0, 0.0, 0.0], "method": "jats_xml",
                "header_rows": 1, "headers": hdrs, "num_rows": len(rows), "num_cols": len(hdrs),
                "caption": full_cap, "caption_block_id": bid, "rows": rows, "markdown": "\n".join(md_lines),
            })
            return

        if tag == "fig":
            fig_idx += 1
            lbl = _serialize_xml_inner(el.find("label")) if el.find("label") is not None else f"Fig {fig_idx}"
            cap_el = el.find("caption")
            cap = _serialize_xml_inner(cap_el) if cap_el is not None else ""
            full_cap = f"{lbl}. {cap}".strip(". ")
            bid = _next_bid()
            blocks.append({"block_id": bid, "page": 1, "column": 1, "role": "caption", "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": full_cap, "text": full_cap, "locator": "/".join(loc), "defects": detect_encoding_defects(full_cap)})
            figures.append({"figure_id": f"fig_{fig_idx}", "page": 1, "bbox": [0.0, 0.0, 0.0, 0.0], "caption": full_cap, "caption_block_id": bid})
            return

        if tag == "disp-formula":
            eq_idx += 1
            txt = _serialize_xml_inner(el)
            if txt:
                bid = _next_bid()
                blocks.append({"block_id": bid, "page": 1, "column": 1, "role": "equation", "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": txt, "text": txt, "locator": "/".join(loc), "defects": detect_encoding_defects(txt)})
                equations.append({"equation_id": f"eq_{eq_idx}", "label": el.attrib.get("id"), "page": 1, "block_id": bid, "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": txt, "script_text": txt})
            return

        if tag in ("article-title", "p", "ref") and not any(_strip_ns(c.tag).lower() in ("p", "sec") for c in el):
            txt = _serialize_xml_inner(el)
            if txt:
                bid = _next_bid()
                role = "heading" if tag == "article-title" else ("reference" if in_ref or tag == "ref" else "paragraph")
                blocks.append({"block_id": bid, "page": 1, "column": 1, "role": role, "heading_level": 1 if tag == "article-title" else None, "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": txt, "text": txt, "locator": "/".join(loc), "defects": detect_encoding_defects(txt)})
                if role == "reference":
                    ref_idx += 1
                    references.append({"ref_id": f"ref_{ref_idx}", "page": 1, "block_id": bid, "text": txt})
            return

        cc: Dict[str, int] = {}
        for ch in el:
            ct = _strip_ns(ch.tag).lower()
            cc[ct] = cc.get(ct, 0) + 1
            _walk(ch, loc + [f"{ct}[{cc[ct]}]"], level, in_ref)

    _walk(root, [], 1, False)
    title_b = next((b for b in blocks if b.get("locator", "").endswith("article-title[1]")), None)
    abs_sec = next((s for s in sections if s["title"].lower() == "abstract"), None)
    abs_text = None
    if abs_sec:
        abs_idx = next((i for i, b in enumerate(blocks) if b["block_id"] == abs_sec["block_id"]), -1)
        if abs_idx >= 0:
            abs_parts = []
            for b in blocks[abs_idx + 1:]:
                if b["role"] == "heading":
                    break
                abs_parts.append(b["raw_text"])
            abs_text = " ".join(abs_parts)

    return {
        "file": str(xml_path), "format": "xml", "backend": "jats_xml_etree", "status": "ok",
        "page_count": 1, "extracted_pages": [1],
        "metadata": {"title": title_b["raw_text"] if title_b else None, "title_block_id": title_b["block_id"] if title_b else None, "abstract": abs_text},
        "page_label_map": {}, "page_numbering_discrepancy": False,
        "sections": sections, "tables": tables, "merged_tables": [], "figures": figures,
        "captions": [], "footnotes": [], "equations": equations, "references": references,
        "headers_footers": [], "unextracted_regions": [], "blocks": blocks,
    }


def _ingest_text_or_office(file_path: Path) -> Dict[str, Any]:
    """Ingest Markdown, plain text, JSON, DOCX, XLSX, or PPTX into the unified document schema."""
    ext = file_path.suffix.lower()
    raw_lines: List[str] = []
    if ext in (".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml"):
        raw_lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    elif ext in (".docx", ".pptx", ".xlsx"):
        with zipfile.ZipFile(file_path, "r") as zf:
            xmls = sorted(
                n for n in zf.namelist()
                if n.endswith(".xml") and (
                    n.startswith("word/document") or n.startswith("ppt/slides/slide")
                    or n.startswith("xl/sharedStrings") or n.startswith("xl/worksheets/sheet")
                )
            )
            for xn in xmls[:40]:
                raw_xml = zf.read(xn)
                try:
                    rt = ET.fromstring(raw_xml)
                    raw_lines.extend(t.strip() for t in rt.itertext() if t and t.strip())
                except ET.ParseError:
                    raw_lines.append(re.sub(r"<[^>]+>", " ", raw_xml.decode("utf-8", errors="replace")))

    blocks: List[Dict[str, Any]] = []
    sections: List[Dict[str, Any]] = []
    paras: List[str] = []
    cur: List[str] = []
    for ln in raw_lines:
        s = ln.strip()
        if not s:
            if cur:
                paras.append(" ".join(cur))
                cur = []
        elif s.startswith("#") or HEADING_RE.match(s):
            if cur:
                paras.append(" ".join(cur))
                cur = []
            paras.append(s)
        else:
            cur.append(s)
    if cur:
        paras.append(" ".join(cur))

    sec_c = 0
    for idx, p in enumerate(paras):
        bid = f"p1_b{idx + 1}"
        m_md = re.match(r"^(#{1,6})\s+(.+)$", p)
        if m_md:
            lvl = len(m_md.group(1))
            title = m_md.group(2).strip()
            sec_c += 1
            sections.append({"section_id": f"sec_{sec_c}", "title": title, "level": lvl, "page_start": 1, "block_id": bid, "bbox": [0.0, 0.0, 0.0, 0.0]})
            blocks.append({"block_id": bid, "page": 1, "column": 1, "role": "heading", "heading_level": lvl, "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": title, "text": title, "defects": detect_encoding_defects(title)})
        elif HEADING_RE.match(p) and len(p) <= 120:
            sec_c += 1
            sections.append({"section_id": f"sec_{sec_c}", "title": p, "level": 1, "page_start": 1, "block_id": bid, "bbox": [0.0, 0.0, 0.0, 0.0]})
            blocks.append({"block_id": bid, "page": 1, "column": 1, "role": "heading", "heading_level": 1, "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": p, "text": p, "defects": detect_encoding_defects(p)})
        else:
            blocks.append({"block_id": bid, "page": 1, "column": 1, "role": "paragraph", "bbox": [0.0, 0.0, 0.0, 0.0], "raw_text": p, "text": p, "defects": detect_encoding_defects(p)})

    return {
        "file": str(file_path), "format": ext.lstrip("."), "backend": "text_or_office_xml", "status": "ok",
        "page_count": 1, "extracted_pages": [1],
        "metadata": {"title": sections[0]["title"] if sections else file_path.stem},
        "page_label_map": {}, "page_numbering_discrepancy": False,
        "sections": sections, "tables": [], "merged_tables": [], "figures": [],
        "captions": [], "footnotes": [], "equations": [], "references": [],
        "headers_footers": [], "unextracted_regions": [], "blocks": blocks,
    }


def _enrich_section_tree(doc_data: Dict[str, Any]) -> None:
    """Populate hierarchy (parent_id, prev/next links, block_ids, page_end, table/fig/eq IDs) in-place."""
    sections = doc_data.get("sections", [])
    blocks = doc_data.get("blocks", [])
    tables = doc_data.get("tables", [])
    figures = doc_data.get("figures", [])
    equations = doc_data.get("equations", [])

    sec_by_bid = {s["block_id"]: s for s in sections if s.get("block_id")}
    stack: List[Dict[str, Any]] = []
    for idx, s in enumerate(sections):
        s["order_index"] = idx
        lvl = int(s.get("level") or 1)
        while stack and int(stack[-1].get("level") or 1) >= lvl:
            stack.pop()
        s["parent_id"] = stack[-1]["section_id"] if stack else None
        s["prev_section_id"] = sections[idx - 1]["section_id"] if idx > 0 else None
        s["next_section_id"] = sections[idx + 1]["section_id"] if idx + 1 < len(sections) else None
        s["block_ids"] = []
        s["table_ids"] = []
        s["figure_ids"] = []
        s["equation_ids"] = []
        s["char_count"] = 0
        s["page_end"] = s.get("page_start", 1)
        stack.append(s)

    cur_sec: Optional[Dict[str, Any]] = None
    for b in blocks:
        if b["block_id"] in sec_by_bid:
            cur_sec = sec_by_bid[b["block_id"]]
        b["section_id"] = cur_sec["section_id"] if cur_sec else "sec_frontmatter"
        b["section_title"] = cur_sec["title"] if cur_sec else "Front Matter"
        if cur_sec is not None:
            cur_sec["block_ids"].append(b["block_id"])
            cur_sec["char_count"] += len(b.get("raw_text", ""))
            cur_sec["page_end"] = max(int(cur_sec["page_end"]), int(b.get("page", 1)))

    bid_to_sec = {b["block_id"]: b["section_id"] for b in blocks}
    sec_by_id = {s["section_id"]: s for s in sections}
    for eq in equations:
        sid = bid_to_sec.get(eq.get("block_id", ""))
        eq["section_id"] = sid
        if sid in sec_by_id:
            sec_by_id[sid]["equation_ids"].append(eq["equation_id"])
    for fig in figures:
        sid = bid_to_sec.get(fig.get("caption_block_id") or "")
        if not sid:
            cand = next((s["section_id"] for s in reversed(sections) if s["page_start"] <= fig["page"]), None)
            sid = cand
        fig["section_id"] = sid
        if sid in sec_by_id:
            sec_by_id[sid]["figure_ids"].append(fig["figure_id"])
    for tbl in tables:
        sid = bid_to_sec.get(tbl.get("caption_block_id") or "")
        if not sid:
            sid = next((s["section_id"] for s in reversed(sections) if s["page_start"] <= tbl["page"]), None)
        tbl["section_id"] = sid
        if sid in sec_by_id:
            sec_by_id[sid]["table_ids"].append(tbl["table_id"])


def extract_terminology_and_cues(doc_data: Dict[str, Any], doc_id: str) -> Dict[str, Any]:
    """Deterministically extract abbreviations, precondition cues, limitation cues, and numeric claims."""
    blocks = doc_data.get("blocks", [])
    tables = doc_data.get("tables", [])
    page_labels = doc_data.get("page_label_map") or {}

    abbrev_map: Dict[str, Dict[str, Any]] = {}
    condition_cues: List[Dict[str, Any]] = []
    limitation_cues: List[Dict[str, Any]] = []
    numeric_claims: List[Dict[str, Any]] = []

    for b in blocks:
        txt = b.get("raw_text", "")
        if not txt or b.get("role") == "reference":
            continue
        anchor = {
            "doc_id": doc_id,
            "page": b["page"],
            "printed_page": page_labels.get(b["page"]) or page_labels.get(str(b["page"])),
            "section_id": b.get("section_id"),
            "section": b.get("section_title"),
            "block_id": b["block_id"],
            "bbox": b.get("bbox"),
            "quote": txt[:260],
        }
        for pat in ABBREV_PATTERNS:
            for m in pat.finditer(txt):
                full_form = re.sub(r"\s+", " ", m.group(1)).strip(" ,;:")
                abbr = m.group(2).strip()
                if len(full_form) >= len(abbr) and not abbr.isdigit():
                    key = normalize_term_key(abbr)
                    if key not in abbrev_map:
                        abbrev_map[key] = {
                            "canonical_term": full_form,
                            "abbreviation": abbr,
                            "aliases": [full_form, abbr],
                            "first_anchor": anchor,
                        }
        if CONDITION_CUE_RE.search(txt):
            condition_cues.append(anchor)
        if LIMITATION_CUE_RE.search(txt):
            limitation_cues.append(anchor)
        for nm in NUMERIC_CLAIM_RE.finditer(txt):
            if len(numeric_claims) < 60:
                numeric_claims.append({**anchor, "matched_span": nm.group(0).strip()})

    for tbl in tables:
        headers = tbl.get("headers") or []
        for r_i, row in enumerate(tbl.get("rows", [])[int(tbl.get("header_rows") or 1):]):
            row_lbl = row[0] if row else ""
            for c_i, cell in enumerate(row):
                if NUMERIC_CLAIM_RE.search(cell):
                    numeric_claims.append({
                        "doc_id": doc_id,
                        "page": tbl["page"],
                        "table_id": tbl["table_id"],
                        "section_id": tbl.get("section_id"),
                        "row_label": row_lbl,
                        "col_header": headers[c_i] if c_i < len(headers) else f"col_{c_i}",
                        "value": cell,
                        "quote": " ".join(row)[:240],
                    })

    return {
        "doc_id": doc_id,
        "abbreviations": list(abbrev_map.values()),
        "condition_cues": condition_cues[:40],
        "limitation_cues": limitation_cues[:30],
        "numeric_claims": numeric_claims[:80],
    }


def _build_section_chunks(doc_data: Dict[str, Any], doc_id: str, max_chars: int = 8500) -> List[Dict[str, Any]]:
    """Partition document blocks into section-aligned chunks <= max_chars for bounded context reading."""
    blocks = doc_data.get("blocks", [])
    tables_by_sec: Dict[str, List[Dict[str, Any]]] = {}
    for t in doc_data.get("tables", []):
        tables_by_sec.setdefault(t.get("section_id") or "sec_frontmatter", []).append({
            "table_id": t["table_id"], "page": t["page"], "caption": t.get("caption"), "markdown": t.get("markdown"),
        })

    chunks: List[Dict[str, Any]] = []
    cur_blocks: List[Dict[str, Any]] = []
    cur_chars = 0
    cur_secs: List[str] = []
    seen_tables: set[str] = set()
    page_labels = doc_data.get("page_label_map") or {}

    def _flush() -> None:
        nonlocal cur_blocks, cur_chars, cur_secs
        if not cur_blocks:
            return
        chunk_tables: List[Dict[str, Any]] = []
        for sid in cur_secs:
            for t in tables_by_sec.get(sid, []):
                if t["table_id"] not in seen_tables:
                    seen_tables.add(t["table_id"])
                    chunk_tables.append(t)
        pages = sorted({int(b["page"]) for b in cur_blocks})
        chunks.append({
            "chunk_id": f"chunk_{len(chunks) + 1:03d}",
            "doc_id": doc_id,
            "section_ids": list(dict.fromkeys(cur_secs)),
            "section_titles": list(dict.fromkeys(b["section_title"] for b in cur_blocks)),
            "pages": pages,
            "total_chars": cur_chars,
            "blocks": cur_blocks,
            "tables": chunk_tables,
        })
        cur_blocks, cur_chars, cur_secs = [], 0, []

    for b in blocks:
        if b.get("role") == "reference":
            continue
        txt = b.get("text") or b.get("raw_text") or ""
        if not txt.strip():
            continue
        is_new_sec = cur_blocks and b.get("role") == "heading" and cur_chars >= 2500
        if cur_blocks and (is_new_sec or cur_chars + len(txt) > max_chars or len(cur_blocks) >= 55):
            _flush()
        cur_blocks.append({
            "block_id": b["block_id"],
            "page": b["page"],
            "printed_page": page_labels.get(b["page"]) or page_labels.get(str(b["page"])),
            "section_id": b.get("section_id"),
            "section_title": b.get("section_title"),
            "role": b.get("role"),
            "bbox": b.get("bbox"),
            "text": txt,
        })
        cur_secs.append(b.get("section_id") or "sec_frontmatter")
        cur_chars += len(txt)
    _flush()
    return chunks


def _render_outline(doc_data: Dict[str, Any], cues: Dict[str, Any]) -> str:
    meta = doc_data.get("metadata") or {}
    lines = [
        f"# Document Outline: {Path(doc_data['file']).name}",
        f"- Format: `{doc_data.get('format')}` | Backend: `{doc_data.get('backend')}` | Pages: `{doc_data.get('page_count')}` | Status: `{doc_data.get('status')}`",
    ]
    if doc_data.get("page_numbering_discrepancy"):
        lines.append(f"- **Page Numbering Discrepancy**: `page_label_map={json.dumps(doc_data.get('page_label_map', {}), ensure_ascii=False)}`")
    if meta.get("title"):
        lines.append(f"- Title (`{meta.get('title_block_id')}`): {meta['title']}")
    if meta.get("abstract"):
        lines.append(f"- Abstract: {meta['abstract'][:320]}{'...' if len(meta['abstract']) > 320 else ''}")
    lines.extend(["", "## Section Tree (Original Order)"])
    for s in doc_data.get("sections", []):
        indent = "  " * max(0, int(s.get("level", 1)) - 1)
        lines.append(f"{indent}- [{s['section_id']}] {s['title']} (p.{s['page_start']}-{s['page_end']}, blocks={len(s.get('block_ids', []))}, tables={s.get('table_ids')}, `{s['block_id']}`)")
    if not doc_data.get("sections"):
        lines.append("- (no headings detected; inspect section_chunks/)")
    lines.extend(["", "## Tables"])
    for t in doc_data.get("tables", []):
        lines.append(f"- `{t['table_id']}` (p.{t['page']}, {t['num_rows']}x{t['num_cols']}, sec={t.get('section_id')}): {t.get('caption') or '(no caption)'}")
    lines.extend(["", "## Pre-Extracted Cues"])
    lines.append(f"- Abbreviations: {len(cues.get('abbreviations', []))} | Condition cues: {len(cues.get('condition_cues', []))} | Limitation cues: {len(cues.get('limitation_cues', []))}")
    if doc_data.get("unextracted_regions"):
        lines.extend(["", "## Unextracted / Defect Regions"])
        for u in doc_data["unextracted_regions"][:15]:
            lines.append(f"- p.{u['page']} `{u.get('block_id')}` reason=`{u.get('reason')}` bbox=`{u.get('bbox')}`")
    return "\n".join(lines) + "\n"


def ingest_document(file_path: Path, out_dir: Path, doc_id: str = "doc_1", max_chunk_chars: int = 8500) -> Dict[str, Any]:
    """Ingest any supported document into normalized JSON, section chunks, outline, and terminology cues."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        probe = probe_pdf(file_path)
        (out_dir / "probe.json").write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8")
        if probe.get("needs_password") or not probe.get("valid_pdf_header", True):
            return {"doc_id": doc_id, "file": str(file_path), "status": "blocked", "probe": probe}
        doc_data = extract_pdf(file_path)
    elif ext in (".xml", ".nxml", ".jats"):
        doc_data = _ingest_xml(file_path)
        probe = {"file": str(file_path), "format": "xml", "summary": {"overall_status": "native_xml"}}
    else:
        doc_data = _ingest_text_or_office(file_path)
        probe = {"file": str(file_path), "format": ext.lstrip("."), "summary": {"overall_status": "native_text"}}

    if doc_data.get("status") != "ok":
        return {"doc_id": doc_id, "file": str(file_path), "status": doc_data.get("status", "failed"), "error": doc_data.get("error"), "probe": probe}

    doc_data["doc_id"] = doc_id
    _enrich_section_tree(doc_data)
    cues = extract_terminology_and_cues(doc_data, doc_id)
    chunks = _build_section_chunks(doc_data, doc_id, max_chars=max_chunk_chars)

    (out_dir / "document.json").write_text(json.dumps(doc_data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "terminology_candidates.json").write_text(json.dumps(cues, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "outline.md").write_text(_render_outline(doc_data, cues), encoding="utf-8")
    chunk_dir = out_dir / "section_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    for ch in chunks:
        (chunk_dir / f"{ch['chunk_id']}.json").write_text(json.dumps(ch, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "doc_id": doc_id,
        "file": str(file_path),
        "format": doc_data.get("format"),
        "backend": doc_data.get("backend"),
        "status": "ok",
        "page_count": doc_data.get("page_count"),
        "page_numbering_discrepancy": doc_data.get("page_numbering_discrepancy", False),
        "counts": {
            "blocks": len(doc_data.get("blocks", [])),
            "sections": len(doc_data.get("sections", [])),
            "tables": len(doc_data.get("tables", [])),
            "figures": len(doc_data.get("figures", [])),
            "equations": len(doc_data.get("equations", [])),
            "chunks": len(chunks),
            "abbreviations": len(cues["abbreviations"]),
            "condition_cues": len(cues["condition_cues"]),
            "limitation_cues": len(cues["limitation_cues"]),
            "unextracted_regions": len(doc_data.get("unextracted_regions", [])),
        },
        "metadata": doc_data.get("metadata"),
        "probe_summary": probe.get("summary", {}),
        "document_json_path": str(out_dir / "document.json"),
        "outline_path": str(out_dir / "outline.md"),
        "cues_path": str(out_dir / "terminology_candidates.json"),
        "chunk_paths": [str(chunk_dir / f"{ch['chunk_id']}.json") for ch in chunks],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest PDF/XML/Text/Office into section-aligned chunks and cues.")
    parser.add_argument("input_path")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--doc-id", default="doc_1")
    parser.add_argument("--max-chunk-chars", type=int, default=8500)
    args = parser.parse_args()
    res = ingest_document(Path(args.input_path), Path(args.out_dir), args.doc_id, args.max_chunk_chars)
    print(json.dumps({k: v for k, v in res.items() if k != "chunk_paths"}, ensure_ascii=False, indent=2))
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
