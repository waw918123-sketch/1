#!/usr/bin/env python3
"""Unified multi-format document ingestor for Nanopublication extraction.

Supports PDF, JATS/PLOS XML (preserving full <front> PubInfo while filtering editors),
Markdown/TXT/CSV/JSON, DOCX, XLSX, and PPTX.
Key design choices for Harbor budget conservation:
  - Chunks <= 8,000 chars aligned to native sections.
  - Pre-extracts candidate claim/stat/study-design cues without expanding large tables.
  - Records compact table index (table_id, page, shape, caption, headers ONLY).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.dont_write_bytecode = True

from doc_common import DOI_PATTERN, detect_encoding_defects, normalize_ligatures, sha256_file
from pdf_layout import HEADING_RE, extract_pdf, probe_pdf

CLAIM_CUE_RE = re.compile(
    r"\b(?:increas(?:e|ed|es|ing)|decreas(?:e|ed|es|ing)|reduc(?:e|ed|es|ing)|"
    r"elevat(?:e|ed|es)|suppress(?:ed|es)?|inhibit(?:s|ed)?|activat(?:e|ed|es)|"
    r"correlat(?:e|ed|es|ion)|associat(?:ed|ion)\s+with|predict(?:s|ed|ive)|"
    r"prognostic|significant(?:ly)?|no\s+(?:statistically\s+)?significant\s+difference|"
    r"higher\s+than|lower\s+than|outperform(?:s|ed)?|exceed(?:s|ed)?|induc(?:e|ed|es)|"
    r"caus(?:e|ed|es)|demonstrat(?:e|ed|es)|estimated|extrapolat(?:e|ed|ion))\b|"
    r"(?:显著(?:提高|降低|增加|减少|差异)|无显著差异|正相关|负相关|抑制|激活|预测|导致)",
    re.I,
)
STAT_CUE_RE = re.compile(
    r"(?:\b2?\s*p\s*[<>=≤≥,]\s*0?\.\d+|\bp\s*=\s*0?\.\d+|95%\s*CI|±\s*\d+(?:\.\d+)?|"
    r"\b(?:SE|SD|SEM|IQR|OR|HR|RR|AUC|r|R2|R²|χ²|F|t)\s*[=<>]\s*-?\d+(?:\.\d+)?)",
    re.I,
)
DESIGN_CUE_RE = re.compile(
    r"\b(?:randomized|double[- ]blind|placebo[- ]controlled|observational|retrospective|prospective|"
    r"cross[- ]sectional|cohort|case[- ]control|higher[- ]taxon\s+extrapolation|"
    r"extrapolat(?:e|ed|ion)|in\s+vitro|in\s+vivo|simulation|meta[- ]analysis|n\s*=\s*\d+)\b|"
    r"(?:随机对照|双盲|观察性|回顾性|前瞻性|队列|外推|体外|体内|样本量)",
    re.I,
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


def _extract_xml_pubinfo(root: ET.Element, xml_path: Path) -> Dict[str, Any]:
    """Extract verified PubInfo from JATS/PLOS <front>, strictly excluding editors/reviewers."""
    title_el = next((el for el in root.iter() if _strip_ns(el.tag).lower() == "article-title"), None)
    title = _serialize_xml_inner(title_el) if title_el is not None else "unreported"

    authors: List[str] = []
    for el in root.iter():
        if _strip_ns(el.tag).lower() == "contrib":
            ctype = (el.attrib.get("contrib-type") or "").strip().lower()
            if ctype and ctype != "author":
                continue
            name_el = next((c for c in el if _strip_ns(c.tag).lower() in ("name", "string-name", "collab")), None)
            if name_el is not None:
                sn = next((_serialize_xml_inner(c) for c in name_el if _strip_ns(c.tag).lower() == "surname"), "")
                gn = next((_serialize_xml_inner(c) for c in name_el if _strip_ns(c.tag).lower() == "given-names"), "")
                full = f"{gn} {sn}".strip() if (gn or sn) else _serialize_xml_inner(name_el)
                if full:
                    authors.append(full)

    journal_el = next((el for el in root.iter() if _strip_ns(el.tag).lower() == "journal-title"), None)
    journal = _serialize_xml_inner(journal_el) if journal_el is not None else "unreported"

    doi = "unreported"
    pmid = "unreported"
    for el in root.iter():
        if _strip_ns(el.tag).lower() == "article-id":
            pid = (el.attrib.get("pub-id-type") or "").lower()
            val = _serialize_xml_inner(el)
            if pid == "doi" and val:
                doi = val
            elif pid == "pmid" and val:
                pmid = val

    year = "unreported"
    pub_dates = [el for el in root.iter() if _strip_ns(el.tag).lower() == "pub-date"]
    for pref in ("epub", "ppub", "collection", ""):
        match = next(
            (pd for pd in pub_dates if not pref or (pd.attrib.get("pub-type") or pd.attrib.get("date-type") or "").lower() == pref),
            None,
        )
        if match is not None:
            yr_el = next((c for c in match if _strip_ns(c.tag).lower() == "year"), None)
            if yr_el is not None and _serialize_xml_inner(yr_el):
                year = _serialize_xml_inner(yr_el)
                break

    lic_el = next((el for el in root.iter() if _strip_ns(el.tag).lower() == "license"), None)
    if lic_el is not None:
        lic = lic_el.attrib.get("{http://www.w3.org/1999/xlink}href") or _serialize_xml_inner(lic_el)[:120] or "unreported"
    else:
        lic = "unreported"

    return {
        "title": title,
        "authors": authors or ["unreported"],
        "year": year,
        "journal": journal,
        "doi": doi,
        "pmid": pmid,
        "version": "unreported",
        "license": lic or "unreported",
        "source_file": str(xml_path),
        "file_sha256": sha256_file(xml_path),
    }


def _ingest_xml(xml_path: Path) -> Dict[str, Any]:
    """Ingest JATS/PLOS XML preserving PubInfo, exact section hierarchy, tables, figures, and locators."""
    root = ET.fromstring(xml_path.read_bytes())
    pubinfo = _extract_xml_pubinfo(root, xml_path)
    blocks: List[Dict[str, Any]] = []
    sections: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    figures: List[Dict[str, Any]] = []
    equations: List[Dict[str, Any]] = []
    references: List[Dict[str, Any]] = []

    b_idx = sec_idx = tbl_idx = fig_idx = eq_idx = ref_idx = 0

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
            t_el = next((c for c in el if _strip_ns(c.tag).lower() == "title"), None)
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
            lbl_el = next((c for c in el if _strip_ns(c.tag).lower() == "label"), None)
            lbl = _serialize_xml_inner(lbl_el) if lbl_el is not None else f"Table {tbl_idx}"
            cap_el = next((c for c in el if _strip_ns(c.tag).lower() == "caption"), None)
            cap = _serialize_xml_inner(cap_el) if cap_el is not None else ""
            full_cap = f"{lbl}. {cap}".strip(". ")
            rows: List[List[str]] = []
            for tr in el.iter():
                if _strip_ns(tr.tag).lower() == "tr":
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
            lbl_el = next((c for c in el if _strip_ns(c.tag).lower() == "label"), None)
            lbl = _serialize_xml_inner(lbl_el) if lbl_el is not None else f"Fig {fig_idx}"
            cap_el = next((c for c in el if _strip_ns(c.tag).lower() == "caption"), None)
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
    return {
        "file": str(xml_path), "format": "xml", "backend": "jats_xml_etree", "status": "ok",
        "page_count": 1, "extracted_pages": [1],
        "metadata": pubinfo,
        "page_label_map": {}, "page_numbering_discrepancy": False,
        "sections": sections, "tables": tables, "merged_tables": [], "figures": figures,
        "captions": [], "footnotes": [], "equations": equations, "references": references,
        "headers_footers": [], "unextracted_regions": [], "blocks": blocks,
    }


def _ingest_text_or_office(file_path: Path) -> Dict[str, Any]:
    """Ingest Markdown, plain text, CSV/TSV, JSON, DOCX, XLSX, or PPTX cleanly."""
    ext = file_path.suffix.lower()
    raw_lines: List[str] = []
    if ext in (".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml"):
        raw_lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    elif ext in (".html", ".htm"):
        raw_html = file_path.read_text(encoding="utf-8", errors="replace")
        raw_html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_html)
        raw_html = re.sub(r"(?i)<title[^>]*>(.*?)</title>", lambda m: f"# {re.sub(r'<[^>]+>', ' ', m.group(1)).strip()}\n\n", raw_html)
        raw_html = re.sub(r"(?i)<h[1-6][^>]*>(.*?)</h[1-6]>", lambda m: f"\n\n# {re.sub(r'<[^>]+>', ' ', m.group(1)).strip()}\n\n", raw_html)
        raw_html = re.sub(r"(?i)</?(?:p|div|li|tr|section|article|br)\b[^>]*>", "\n", raw_html)
        raw_html = re.sub(r"(?i)</t[dh]>", "\t", raw_html)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", raw_html))
        raw_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in plain.splitlines()]
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

    full_head = "\n".join(raw_lines[:30])
    dm = DOI_PATTERN.search(full_head)
    ym = re.search(r"\b((?:19|20)\d{2})\b", full_head)
    return {
        "file": str(file_path), "format": ext.lstrip("."), "backend": "text_or_office_xml", "status": "ok",
        "page_count": 1, "extracted_pages": [1],
        "metadata": {
            "title": sections[0]["title"] if sections else file_path.stem,
            "authors": ["unreported"],
            "year": ym.group(1) if ym else "unreported",
            "journal": "unreported",
            "doi": dm.group(1).rstrip(".,;)]") if dm else "unreported",
            "pmid": "unreported",
            "version": "unreported",
            "license": "unreported",
            "source_file": str(file_path),
            "file_sha256": sha256_file(file_path),
        },
        "page_label_map": {}, "page_numbering_discrepancy": False,
        "sections": sections, "tables": [], "merged_tables": [], "figures": [],
        "captions": [], "footnotes": [], "equations": [], "references": [],
        "headers_footers": [], "unextracted_regions": [], "blocks": blocks,
    }


def _enrich_section_tree(doc_data: Dict[str, Any]) -> None:
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
        s["block_ids"], s["table_ids"], s["figure_ids"], s["equation_ids"] = [], [], [], []
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
        sid = bid_to_sec.get(fig.get("caption_block_id") or "") or next((s["section_id"] for s in reversed(sections) if s["page_start"] <= fig["page"]), None)
        fig["section_id"] = sid
        if sid in sec_by_id:
            sec_by_id[sid]["figure_ids"].append(fig["figure_id"])
    for tbl in tables:
        sid = bid_to_sec.get(tbl.get("caption_block_id") or "") or next((s["section_id"] for s in reversed(sections) if s["page_start"] <= tbl["page"]), None)
        tbl["section_id"] = sid
        if sid in sec_by_id:
            sec_by_id[sid]["table_ids"].append(tbl["table_id"])


def extract_claim_cues(doc_data: Dict[str, Any], doc_id: str) -> Dict[str, Any]:
    """Deterministically index candidate claim, statistical, and study-design cues WITHOUT expanding table cells."""
    blocks = doc_data.get("blocks", [])
    page_labels = doc_data.get("page_label_map") or {}
    claim_cues: List[Dict[str, Any]] = []
    stat_cues: List[Dict[str, Any]] = []
    design_cues: List[Dict[str, Any]] = []
    stat_anomalies: List[Dict[str, Any]] = []

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
        if CLAIM_CUE_RE.search(txt) and len(claim_cues) < 60:
            claim_cues.append(anchor)
        if STAT_CUE_RE.search(txt) and len(stat_cues) < 50:
            stat_cues.append(anchor)
        if DESIGN_CUE_RE.search(txt) and len(design_cues) < 40:
            design_cues.append(anchor)
        anom = (b.get("defects") or {}).get("stat_glyph_anomalies") or []
        if anom:
            stat_anomalies.append({**anchor, "anomalies": anom})

    return {
        "doc_id": doc_id,
        "claim_cues": claim_cues,
        "stat_cues": stat_cues,
        "design_cues": design_cues,
        "stat_anomalies": stat_anomalies,
    }


def _build_section_chunks(doc_data: Dict[str, Any], doc_id: str, max_chars: int = 8000) -> List[Dict[str, Any]]:
    """Partition document into budget-safe (<=8,000 chars) chunks; table index only (no giant cell expansion)."""
    blocks = doc_data.get("blocks", [])
    tables_by_sec: Dict[str, List[Dict[str, Any]]] = {}
    for t in doc_data.get("tables", []):
        tables_by_sec.setdefault(t.get("section_id") or "sec_frontmatter", []).append({
            "table_id": t["table_id"], "page": t["page"], "caption": t.get("caption"),
            "num_rows": t.get("num_rows"), "num_cols": t.get("num_cols"), "headers": t.get("headers"),
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
        chunks.append({
            "chunk_id": f"chunk_{len(chunks) + 1:03d}",
            "doc_id": doc_id,
            "section_ids": list(dict.fromkeys(cur_secs)),
            "section_titles": list(dict.fromkeys(b["section_title"] for b in cur_blocks)),
            "pages": sorted({int(b["page"]) for b in cur_blocks}),
            "total_chars": cur_chars,
            "blocks": cur_blocks,
            "table_index": chunk_tables,
        })
        cur_blocks, cur_chars, cur_secs = [], 0, []

    for b in blocks:
        if b.get("role") == "reference":
            continue
        txt = b.get("text") or b.get("raw_text") or ""
        if not txt.strip():
            continue
        is_new_sec = cur_blocks and b.get("role") == "heading" and cur_chars >= 2200
        if cur_blocks and (is_new_sec or cur_chars + len(txt) > max_chars or len(cur_blocks) >= 50):
            _flush()
        cur_blocks.append({
            "block_id": b["block_id"], "page": b["page"],
            "printed_page": page_labels.get(b["page"]) or page_labels.get(str(b["page"])),
            "section_id": b.get("section_id"), "section_title": b.get("section_title"),
            "role": b.get("role"), "bbox": b.get("bbox"), "text": txt,
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
        f"- PubInfo: title={meta.get('title')!r} | authors={meta.get('authors')} | year={meta.get('year')} | journal={meta.get('journal')} | doi={meta.get('doi')}",
    ]
    if doc_data.get("page_numbering_discrepancy"):
        lines.append(f"- **Page Numbering Discrepancy**: `page_label_map={json.dumps(doc_data.get('page_label_map', {}), ensure_ascii=False)}`")
    lines.extend(["", "## Section Tree"])
    for s in doc_data.get("sections", []):
        indent = "  " * max(0, int(s.get("level", 1)) - 1)
        lines.append(f"{indent}- [{s['section_id']}] {s['title']} (p.{s['page_start']}-{s['page_end']}, blocks={len(s.get('block_ids', []))}, tables={s.get('table_ids')})")
    if not doc_data.get("sections"):
        lines.append("- (no headings detected; inspect section_chunks/)")
    lines.extend(["", "## Tables (Compact Index — query individually with query_nanopub.py --table <id>)"])
    for t in doc_data.get("tables", []):
        lines.append(f"- `{t['table_id']}` (p.{t['page']}, {t['num_rows']}x{t['num_cols']}, headers={t.get('headers')}): {t.get('caption') or '(no caption)'}")
    lines.extend(["", "## Pre-Extracted Nanopub Cues"])
    lines.append(f"- Claim cues: {len(cues['claim_cues'])} | Stat cues: {len(cues['stat_cues'])} | Design cues: {len(cues['design_cues'])} | Stat glyph anomalies: {len(cues['stat_anomalies'])}")
    if doc_data.get("unextracted_regions"):
        lines.extend(["", "## Unextracted / Defect / Stat-Anomaly Regions"])
        for u in doc_data["unextracted_regions"][:15]:
            lines.append(f"- p.{u['page']} `{u.get('block_id')}` reason=`{u.get('reason')}` bbox=`{u.get('bbox')}`")
    return "\n".join(lines) + "\n"


def ingest_document(file_path: Path, out_dir: Path, doc_id: str = "doc_1", max_chunk_chars: int = 8000) -> Dict[str, Any]:
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
    cues = extract_claim_cues(doc_data, doc_id)
    chunks = _build_section_chunks(doc_data, doc_id, max_chars=max_chunk_chars)

    (out_dir / "document.json").write_text(json.dumps(doc_data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "claim_cues.json").write_text(json.dumps(cues, ensure_ascii=False, indent=2), encoding="utf-8")
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
        "pubinfo": doc_data.get("metadata"),
        "counts": {
            "blocks": len(doc_data.get("blocks", [])),
            "sections": len(doc_data.get("sections", [])),
            "tables": len(doc_data.get("tables", [])),
            "figures": len(doc_data.get("figures", [])),
            "equations": len(doc_data.get("equations", [])),
            "chunks": len(chunks),
            "claim_cues": len(cues["claim_cues"]),
            "stat_cues": len(cues["stat_cues"]),
            "design_cues": len(cues["design_cues"]),
            "stat_anomalies": len(cues["stat_anomalies"]),
            "unextracted_regions": len(doc_data.get("unextracted_regions", [])),
        },
        "probe_summary": probe.get("summary", {}),
        "document_json_path": str(out_dir / "document.json"),
        "outline_path": str(out_dir / "outline.md"),
        "cues_path": str(out_dir / "claim_cues.json"),
        "chunk_paths": [str(chunk_dir / f"{ch['chunk_id']}.json") for ch in chunks],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest PDF/XML/Text/Office into compact chunks and claim cues.")
    parser.add_argument("input_path")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--doc-id", default="doc_1")
    parser.add_argument("--max-chunk-chars", type=int, default=8000)
    args = parser.parse_args()
    res = ingest_document(Path(args.input_path), Path(args.out_dir), args.doc_id, args.max_chunk_chars)
    print(json.dumps({k: v for k, v in res.items() if k != "chunk_paths"}, ensure_ascii=False, indent=2)[:16000])
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
