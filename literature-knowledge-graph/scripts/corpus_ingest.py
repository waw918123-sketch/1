#!/usr/bin/env python3
"""统一多格式语料身份解析、段落定位与 XML 公式核对引擎。

核心能力：
1. 识别 corpus-manifest.json、material.json、primary/secondary-material.json、secondary-publisher.xml 与既有基线图谱；
2. 严格区分“同篇多种格式变体”与“独立跨篇论文”，绝不把 secondary-publisher.xml 或 material.json 误算为第三篇论文；
3. 严格隔离根 metadata（case.json 根 DOI/title/authors 仅归属第一篇，绝不套用到第二篇）；
4. 区分 PDF 页码（has_pdf_pages=True）与 JATS paragraph_id（has_pdf_pages=False, page=None），绝不伪造“页面 1”；
5. 对齐 JSON 线性化文本与 XML 原始上下标/行内公式，检出差异并标出未提供图像/附件/底层临床实验数据边界。
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from doc_common import (
    DOI_PATTERN,
    detect_encoding_defects,
    normalize_doi,
    normalize_for_match,
    normalize_ligatures,
    normalize_term_key,
    sha256_file,
)
from pdf_layout import HEADING_RE, extract_pdf, probe_pdf

SUPPORTED_EXTS = {
    ".pdf", ".xml", ".nxml", ".jats", ".md", ".txt", ".csv", ".tsv",
    ".json", ".yaml", ".yml", ".docx", ".pptx", ".xlsx",
}

ENTITY_CUE_RE = re.compile(
    r"\b(?:propos(?:e|ed|es)|introduc(?:e|ed|es)|develop(?:ed|s)?|evaluat(?:e|ed|es)|"
    r"benchmark(?:ed|s)?|outperform(?:s|ed)?|achieves?|yields?|increas(?:e|ed|es)|"
    r"decreas(?:e|ed|es)|reduc(?:e|ed|es)|improves?|correlat(?:e|ed|es)|predicts?|"
    r"contradicts?|supports?|extends?|derived\s+from|synthesiz(?:e|ed)|measured)\b|"
    r"(?:提出|构建|采用|使用|评估|优于|提高|降低|减少|改善|相关|预测|矛盾|支持|证实|测得)",
    re.I,
)
CITED_THIRD_PARTY_RE = re.compile(
    r"(?:\[\d+(?:\s*[-,]\s*\d+)*\]|\([A-Z][A-Za-z\-]+(?:\s+et\s+al\.?)?,?\s*(?:19|20)\d{2}[a-z]?\)|"
    r"\b(?:previously|prior\s+work|earlier\s+stud(?:y|ies)|reported\s+by|et\s+al\.)\b|"
    r"(?:前人研究|已有研究|文献\s*\[?\d+\]?|等人))",
    re.I,
)
SELF_STATEMENT_RE = re.compile(
    r"(?:\b(?:we|our|this\s+(?:study|work|paper)|herein|in\s+this\s+work)\b|"
    r"(?:本文|本研究|我们|本工作))",
    re.I,
)
UNAVAILABLE_MATERIAL_RE = re.compile(
    r"\b(?:Fig(?:ure)?\.?\s*\d+[A-Za-z]?|Supplementary\s+(?:Table|Figure|Fig\.?|Note|File|Data|Information|Material)|"
    r"Additional\s+file\s*\d*|Appendix\s+[A-Z0-9]+|Table\s+S\d+|Fig\.?\s*S\d+|"
    r"(?:raw|underlying|individual\s+patient|clinical|experimental)\s+data\s+(?:are|is)\s+(?:not\s+shown|available|deposited)|"
    r"data\s+not\s+shown)\b|"
    r"(?:附图|附表|补充材料|补充图|补充表|原始数据|底层临床数据|实验原始数据)",
    re.I,
)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _serialize_xml_layers(el: ET.Element) -> Tuple[str, str]:
    """同时生成 XML 节点的线性化文本 (linear_text) 与保留上下标/公式标记的富文本 (rich_text)。"""
    lin_parts: List[str] = []
    rich_parts: List[str] = []
    if el.text:
        lin_parts.append(el.text)
        rich_parts.append(el.text)

    for ch in el:
        t = _strip_ns(ch.tag).lower()
        c_lin, c_rich = _serialize_xml_layers(ch)
        if t == "sub":
            lin_parts.append(c_lin)
            rich_parts.append(f"_{{{c_rich}}}")
        elif t == "sup":
            lin_parts.append(c_lin)
            rich_parts.append(f"^{{{c_rich}}}")
        elif t in ("tex-math", "inline-formula"):
            lin_parts.append(c_lin)
            if t == "tex-math":
                rich_parts.append(f"[tex: {c_rich}]")
            else:
                rich_parts.append(f"<formula>{c_rich}</formula>")
        else:
            lin_parts.append(c_lin)
            rich_parts.append(c_rich)
        if ch.tail:
            lin_parts.append(ch.tail)
            rich_parts.append(ch.tail)

    lin = re.sub(r"\s+", " ", normalize_ligatures("".join(lin_parts))).strip()
    rich = re.sub(r"\s+", " ", normalize_ligatures("".join(rich_parts))).strip()
    return lin, rich


def _extract_xml_pubinfo(root: ET.Element, xml_path: Path) -> Dict[str, Any]:
    """从 JATS/PLOS <front> 提取出版元数据，严格排除 editor/reviewer。"""
    title_el = next((el for el in root.iter() if _strip_ns(el.tag).lower() == "article-title"), None)
    title = _serialize_xml_layers(title_el)[0] if title_el is not None else "unreported"

    authors: List[str] = []
    for el in root.iter():
        if _strip_ns(el.tag).lower() == "contrib":
            ctype = (el.attrib.get("contrib-type") or "").strip().lower()
            if ctype and ctype != "author":
                continue
            name_el = next((c for c in el if _strip_ns(c.tag).lower() in ("name", "string-name", "collab")), None)
            if name_el is not None:
                sn = next((_serialize_xml_layers(c)[0] for c in name_el if _strip_ns(c.tag).lower() == "surname"), "")
                gn = next((_serialize_xml_layers(c)[0] for c in name_el if _strip_ns(c.tag).lower() == "given-names"), "")
                full = f"{gn} {sn}".strip() if (gn or sn) else _serialize_xml_layers(name_el)[0]
                if full:
                    authors.append(full)

    journal_el = next((el for el in root.iter() if _strip_ns(el.tag).lower() == "journal-title"), None)
    journal = _serialize_xml_layers(journal_el)[0] if journal_el is not None else "unreported"

    doi = "unreported"
    pmid = "unreported"
    for el in root.iter():
        if _strip_ns(el.tag).lower() == "article-id":
            pid = (el.attrib.get("pub-id-type") or "").lower()
            val = _serialize_xml_layers(el)[0]
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
            if yr_el is not None and _serialize_xml_layers(yr_el)[0]:
                year = _serialize_xml_layers(yr_el)[0]
                break

    lic_el = next((el for el in root.iter() if _strip_ns(el.tag).lower() == "license"), None)
    if lic_el is not None:
        lic = lic_el.attrib.get("{http://www.w3.org/1999/xlink}href") or _serialize_xml_layers(lic_el)[0][:160] or "unreported"
    else:
        lic = "unreported"

    return {
        "title": title or "unreported",
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


def _parse_jats_table_rows(tbl_el: ET.Element) -> Tuple[List[List[str]], int]:
    """解析 JATS <table>，支持 <thead> 统计与 colspan/rowspan 展平。"""
    trs: List[Tuple[bool, List[Tuple[str, int, int]]]] = []
    for sub in tbl_el.iter():
        st = _strip_ns(sub.tag).lower()
        if st in ("thead", "tbody"):
            in_hdr = st == "thead"
            for tr in sub:
                if _strip_ns(tr.tag).lower() != "tr":
                    continue
                cells = []
                for c in tr:
                    if _strip_ns(c.tag).lower() in ("th", "td"):
                        txt = _serialize_xml_layers(c)[1]
                        cs = max(1, int(c.attrib.get("colspan", "1") or 1))
                        rs = max(1, int(c.attrib.get("rowspan", "1") or 1))
                        cells.append((txt, cs, rs))
                if cells:
                    trs.append((in_hdr, cells))
    if not trs:
        for tr in tbl_el.iter():
            if _strip_ns(tr.tag).lower() == "tr":
                cells = []
                in_hdr = False
                for c in tr:
                    ct = _strip_ns(c.tag).lower()
                    if ct in ("th", "td"):
                        if ct == "th":
                            in_hdr = True
                        txt = _serialize_xml_layers(c)[1]
                        cs = max(1, int(c.attrib.get("colspan", "1") or 1))
                        rs = max(1, int(c.attrib.get("rowspan", "1") or 1))
                        cells.append((txt, cs, rs))
                if cells:
                    trs.append((in_hdr, cells))

    grid: List[List[str]] = []
    span_carry: Dict[Tuple[int, int], str] = {}
    hdr_count = 0
    for r_idx, (in_hdr, cells) in enumerate(trs):
        if in_hdr and hdr_count == r_idx:
            hdr_count += 1
        row_out: List[str] = []
        c_idx = 0
        for txt, cs, rs in cells:
            while (r_idx, c_idx) in span_carry:
                row_out.append(span_carry.pop((r_idx, c_idx)))
                c_idx += 1
            for k in range(cs):
                row_out.append(txt)
                for dr in range(1, rs):
                    span_carry[(r_idx + dr, c_idx + k)] = txt
            c_idx += cs
        while (r_idx, c_idx) in span_carry:
            row_out.append(span_carry.pop((r_idx, c_idx)))
            c_idx += 1
        grid.append(row_out)

    max_cols = max((len(r) for r in grid), default=0)
    for r in grid:
        if len(r) < max_cols:
            r.extend([""] * (max_cols - len(r)))
    return grid, max(1, hdr_count) if grid else 1


def _ingest_xml(xml_path: Path) -> Dict[str, Any]:
    """解析 JATS/PLOS XML，保留原始 @id 作为 paragraph_id 且绝不伪造 PDF 页码。"""
    root = ET.fromstring(xml_path.read_bytes())
    pubinfo = _extract_xml_pubinfo(root, xml_path)
    blocks: List[Dict[str, Any]] = []
    sections: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    figures: List[Dict[str, Any]] = []
    equations: List[Dict[str, Any]] = []
    references: List[Dict[str, Any]] = []

    b_idx = sec_idx = tbl_idx = fig_idx = eq_idx = ref_idx = 0
    seen_bids: Set[str] = set()

    def _alloc_ids(native_id: Optional[str], prefix: str = "xml_p") -> Tuple[str, str, str]:
        nonlocal b_idx
        b_idx += 1
        pid = native_id.strip() if (native_id and native_id.strip()) else f"{prefix}_{b_idx}"
        id_src = "native_jats_id" if (native_id and native_id.strip()) else "synthesized_xml_index"
        bid = pid if pid not in seen_bids else f"{pid}__{b_idx}"
        seen_bids.add(bid)
        return bid, pid, id_src

    def _walk(el: ET.Element, loc: List[str], level: int, in_ref: bool) -> None:
        nonlocal sec_idx, tbl_idx, fig_idx, eq_idx, ref_idx
        tag = _strip_ns(el.tag).lower()
        if tag in ("journal-meta", "contrib-group", "aff", "history", "permissions", "counts"):
            return

        if tag == "ref-list":
            sec_idx += 1
            bid, pid, id_src = _alloc_ids(el.attrib.get("id"), "xml_reflist")
            sections.append({"section_id": f"sec_{sec_idx}", "native_id": el.attrib.get("id"), "title": "References", "level": max(1, level), "page_start": None, "block_id": bid, "paragraph_id": pid})
            blocks.append({"block_id": bid, "paragraph_id": pid, "paragraph_id_source": id_src, "page": None, "role": "heading", "heading_level": max(1, level), "raw_text": "References", "text": "References", "rich_text": "References", "locator": "/".join(loc), "defects": detect_encoding_defects("References")})
            for ch in el:
                _walk(ch, loc + [_strip_ns(ch.tag).lower()], level + 1, True)
            return

        if tag in ("sec", "abstract", "app", "supplementary-material"):
            t_el = next((c for c in el if _strip_ns(c.tag).lower() == "title"), None)
            t_lin, t_rich = _serialize_xml_layers(t_el) if t_el is not None else (("Abstract", "Abstract") if tag == "abstract" else (el.attrib.get("id") or tag.title(), el.attrib.get("id") or tag.title()))
            sec_idx += 1
            bid, pid, id_src = _alloc_ids(el.attrib.get("id"), "xml_sec")
            sections.append({"section_id": f"sec_{sec_idx}", "native_id": el.attrib.get("id"), "title": t_lin, "level": max(1, level), "page_start": None, "block_id": bid, "paragraph_id": pid})
            blocks.append({"block_id": bid, "paragraph_id": pid, "paragraph_id_source": id_src, "page": None, "role": "heading", "heading_level": max(1, level), "raw_text": t_lin, "text": t_rich, "rich_text": t_rich, "locator": "/".join(loc), "defects": detect_encoding_defects(t_lin)})
            for ch in el:
                if ch is t_el:
                    continue
                _walk(ch, loc + [_strip_ns(ch.tag).lower()], level + 1, in_ref)
            return

        if tag == "table-wrap":
            tbl_idx += 1
            lbl_el = next((c for c in el if _strip_ns(c.tag).lower() == "label"), None)
            lbl = _serialize_xml_layers(lbl_el)[0] if lbl_el is not None else f"Table {tbl_idx}"
            cap_el = next((c for c in el if _strip_ns(c.tag).lower() == "caption"), None)
            cap_lin, cap_rich = _serialize_xml_layers(cap_el) if cap_el is not None else ("", "")
            full_lin = f"{lbl}. {cap_lin}".strip(". ")
            full_rich = f"{lbl}. {cap_rich}".strip(". ")
            rows, n_hdr = _parse_jats_table_rows(el)
            native_tid = el.attrib.get("id") or f"tbl_{tbl_idx}"
            bid, pid, id_src = _alloc_ids(el.attrib.get("id"), "xml_tbl")
            blocks.append({"block_id": bid, "paragraph_id": pid, "paragraph_id_source": id_src, "page": None, "role": "caption", "raw_text": full_lin, "text": full_rich, "rich_text": full_rich, "locator": "/".join(loc), "defects": detect_encoding_defects(full_lin)})
            hdrs = rows[0] if rows else []
            md_lines = []
            if rows:
                md_lines = ["| " + " | ".join(hdrs) + " |", "| " + " | ".join(["---"] * len(hdrs)) + " |"] + ["| " + " | ".join(r) + " |" for r in rows[n_hdr:]]
            tables.append({
                "table_id": f"tbl_{tbl_idx}", "native_table_id": native_tid, "paragraph_id": pid, "page": None, "method": "jats_xml",
                "header_rows": n_hdr, "headers": hdrs, "num_rows": len(rows), "num_cols": len(hdrs),
                "caption": full_rich, "caption_block_id": bid, "rows": rows, "markdown": "\n".join(md_lines),
            })
            return

        if tag == "fig":
            fig_idx += 1
            lbl_el = next((c for c in el if _strip_ns(c.tag).lower() == "label"), None)
            lbl = _serialize_xml_layers(lbl_el)[0] if lbl_el is not None else f"Fig {fig_idx}"
            cap_el = next((c for c in el if _strip_ns(c.tag).lower() == "caption"), None)
            cap_lin, cap_rich = _serialize_xml_layers(cap_el) if cap_el is not None else ("", "")
            full_lin = f"{lbl}. {cap_lin}".strip(". ")
            full_rich = f"{lbl}. {cap_rich}".strip(". ")
            bid, pid, id_src = _alloc_ids(el.attrib.get("id"), "xml_fig")
            blocks.append({"block_id": bid, "paragraph_id": pid, "paragraph_id_source": id_src, "page": None, "role": "caption", "raw_text": full_lin, "text": full_rich, "rich_text": full_rich, "locator": "/".join(loc), "defects": detect_encoding_defects(full_lin)})
            figures.append({"figure_id": f"fig_{fig_idx}", "native_figure_id": el.attrib.get("id"), "paragraph_id": pid, "page": None, "caption": full_rich, "caption_block_id": bid})
            return

        if tag == "disp-formula":
            eq_idx += 1
            lin, rich = _serialize_xml_layers(el)
            if lin:
                bid, pid, id_src = _alloc_ids(el.attrib.get("id"), "xml_eq")
                blocks.append({"block_id": bid, "paragraph_id": pid, "paragraph_id_source": id_src, "page": None, "role": "equation", "raw_text": lin, "text": rich, "rich_text": rich, "locator": "/".join(loc), "defects": detect_encoding_defects(lin)})
                equations.append({"equation_id": f"eq_{eq_idx}", "label": el.attrib.get("id"), "paragraph_id": pid, "page": None, "block_id": bid, "raw_text": lin, "script_text": rich})
            return

        if tag in ("article-title", "p", "ref") and not any(_strip_ns(c.tag).lower() in ("p", "sec") for c in el):
            lin, rich = _serialize_xml_layers(el)
            if lin:
                bid, pid, id_src = _alloc_ids(el.attrib.get("id"), "xml_p")
                role = "heading" if tag == "article-title" else ("reference" if in_ref or tag == "ref" else "paragraph")
                blocks.append({
                    "block_id": bid, "paragraph_id": pid, "paragraph_id_source": id_src, "page": None,
                    "role": role, "heading_level": 1 if tag == "article-title" else None,
                    "raw_text": lin, "text": rich, "rich_text": rich,
                    "has_script_or_formula": rich != lin,
                    "locator": "/".join(loc), "defects": detect_encoding_defects(lin),
                })
                if role == "reference":
                    ref_idx += 1
                    references.append({"ref_id": f"ref_{ref_idx}", "paragraph_id": pid, "page": None, "block_id": bid, "text": lin})
            return

        cc: Dict[str, int] = {}
        for ch in el:
            ct = _strip_ns(ch.tag).lower()
            cc[ct] = cc.get(ct, 0) + 1
            _walk(ch, loc + [f"{ct}[{cc[ct]}]"], level, in_ref)

    _walk(root, [], 1, False)
    return {
        "file": str(xml_path), "format": "xml", "backend": "jats_xml_etree", "status": "ok",
        "has_pdf_pages": False, "locator_mode": "jats_paragraph_id",
        "page_count": None, "extracted_pages": [],
        "metadata": pubinfo,
        "page_label_map": {}, "page_numbering_discrepancy": False,
        "sections": sections, "tables": tables, "merged_tables": [], "figures": figures,
        "captions": [], "footnotes": [], "equations": equations, "references": references,
        "headers_footers": [], "unextracted_regions": [], "blocks": blocks,
    }


def _extract_markdown_tables(lines: List[str]) -> List[Dict[str, Any]]:
    """从 Markdown 文本行中解析标准管道表格，便于精确单元格级证据核验。"""
    tables: List[Dict[str, Any]] = []
    i = 0
    n = len(lines)
    t_idx = 0
    sep_re = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
    while i < n - 1:
        ln = lines[i].strip()
        nxt = lines[i + 1].strip()
        if "|" in ln and sep_re.match(nxt):
            cap = None
            if i > 0 and re.match(r"^(?:Table|Tab\.?|表)\s*\d+", lines[i - 1].strip(), re.I):
                cap = lines[i - 1].strip()
            raw_rows = [ln]
            j = i + 2
            while j < n and "|" in lines[j] and lines[j].strip():
                raw_rows.append(lines[j].strip())
                j += 1
            parsed = []
            for rr in raw_rows:
                s = rr.strip().strip("|")
                parsed.append([c.strip() for c in s.split("|")])
            if parsed:
                t_idx += 1
                hdrs = parsed[0]
                max_c = max(len(r) for r in parsed)
                for r in parsed:
                    if len(r) < max_c:
                        r.extend([""] * (max_c - len(r)))
                tables.append({
                    "table_id": f"tbl_{t_idx}",
                    "paragraph_id": f"md_tbl_{t_idx}",
                    "page": None,
                    "method": "markdown_pipe_table",
                    "header_rows": 1,
                    "headers": hdrs,
                    "num_rows": len(parsed),
                    "num_cols": max_c,
                    "caption": cap,
                    "caption_block_id": None,
                    "rows": parsed,
                    "markdown": "\n".join([ln, nxt] + raw_rows[1:]),
                })
            i = j
            continue
        i += 1
    return tables


def _ingest_structured_paper_json(json_path: Path, manifest_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """解析 primary-material.json / secondary-material.json 等结构化论文 JSON，完整保留 paragraph_id。"""
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    manifest_meta = manifest_meta or {}

    meta_obj = raw.get("metadata") if isinstance(raw, dict) and isinstance(raw.get("metadata"), dict) else (raw if isinstance(raw, dict) else {})
    title = manifest_meta.get("title") or meta_obj.get("title") or "unreported"
    authors_raw = manifest_meta.get("authors") or meta_obj.get("authors")
    if isinstance(authors_raw, str):
        authors = [a.strip() for a in re.split(r"[;；]", authors_raw) if a.strip()] or [authors_raw]
    elif isinstance(authors_raw, list):
        authors = [str(a).strip() for a in authors_raw if str(a).strip()]
    else:
        authors = ["unreported"]
    doi = manifest_meta.get("doi") or meta_obj.get("doi") or "unreported"
    year = str(manifest_meta.get("year") or meta_obj.get("year") or "unreported")
    journal = str(manifest_meta.get("journal") or meta_obj.get("journal") or "unreported")
    lic = str(manifest_meta.get("license") or meta_obj.get("license") or "unreported")
    src_id = str(manifest_meta.get("source_id") or (raw.get("source_id") if isinstance(raw, dict) else None) or json_path.stem)

    blocks: List[Dict[str, Any]] = []
    sections: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    seen_bids: Set[str] = set()
    sec_map: Dict[str, str] = {}
    b_idx = sec_idx = 0

    def _add_block(pid: Optional[str], text: str, role: str = "paragraph", sec_id_hint: Optional[str] = None, sec_title: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> None:
        nonlocal b_idx, sec_idx
        txt = re.sub(r"\s+", " ", normalize_ligatures(text or "")).strip()
        if not txt:
            return
        b_idx += 1
        native_pid = str(pid).strip() if (pid is not None and str(pid).strip()) else None
        final_pid = native_pid or f"para_{b_idx}"
        bid = final_pid if final_pid not in seen_bids else f"{final_pid}__{b_idx}"
        seen_bids.add(bid)

        if sec_title:
            skey = normalize_term_key(sec_title)
            if skey not in sec_map:
                sec_idx += 1
                sid = sec_id_hint or f"sec_{sec_idx}"
                sec_map[skey] = sid
                h_bid = f"sec_hdr_{sec_idx}"
                sections.append({"section_id": sid, "title": sec_title, "level": 1, "page_start": None, "block_id": h_bid, "paragraph_id": final_pid})
                blocks.append({
                    "block_id": h_bid, "paragraph_id": final_pid, "paragraph_id_source": "section_heading",
                    "page": None, "role": "heading", "heading_level": 1,
                    "raw_text": sec_title, "text": sec_title, "defects": detect_encoding_defects(sec_title),
                })

        blk = {
            "block_id": bid,
            "paragraph_id": final_pid,
            "paragraph_id_source": "native_jats_id" if native_pid else "synthesized_json_index",
            "page": None,
            "role": role,
            "raw_text": txt,
            "text": txt,
            "defects": detect_encoding_defects(txt),
        }
        if extra:
            blk.update(extra)
        blocks.append(blk)

    if isinstance(raw, dict):
        # 处理摘要
        ab = raw.get("abstract")
        if isinstance(ab, str) and ab.strip():
            _add_block(raw.get("abstract_id") or "abstract-p1", ab, role="paragraph", sec_title="Abstract")
        elif isinstance(ab, list):
            for idx_ab, it in enumerate(ab, 1):
                if isinstance(it, dict):
                    _add_block(it.get("paragraph_id") or it.get("id") or f"abstract-p{idx_ab}", str(it.get("text") or it.get("content") or ""), role="paragraph", sec_title="Abstract")
                elif isinstance(it, str):
                    _add_block(f"abstract-p{idx_ab}", it, role="paragraph", sec_title="Abstract")

        # 处理扁平段落列表
        para_list = None
        for key in ("paragraphs", "blocks", "chunks", "body", "content", "items", "records"):
            if isinstance(raw.get(key), list):
                para_list = raw[key]
                break

        if isinstance(para_list, list):
            for it in para_list:
                if isinstance(it, str):
                    _add_block(None, it)
                elif isinstance(it, dict):
                    txt = str(it.get("text") or it.get("content") or it.get("body") or it.get("raw_text") or "")
                    pid = it.get("paragraph_id") or it.get("id") or it.get("pid") or it.get("para_id") or it.get("jats_id")
                    st = it.get("section_title") or it.get("section") or it.get("heading") or it.get("sec_title")
                    sid_h = it.get("section_id") or it.get("sec_id")
                    role = str(it.get("role") or ("heading" if it.get("is_heading") else "paragraph"))
                    _add_block(pid, txt, role=role, sec_id_hint=sid_h, sec_title=st)

        # 处理分节树
        if isinstance(raw.get("sections"), list):
            for s_idx, sec in enumerate(raw["sections"], 1):
                if not isinstance(sec, dict):
                    continue
                st = str(sec.get("title") or sec.get("section_title") or sec.get("heading") or f"Section {s_idx}")
                sid_h = str(sec.get("section_id") or sec.get("id") or f"sec_{s_idx}")
                sub_paras = sec.get("paragraphs") or sec.get("blocks") or sec.get("content") or []
                if isinstance(sub_paras, str):
                    _add_block(sec.get("paragraph_id") or sec.get("id"), sub_paras, sec_id_hint=sid_h, sec_title=st)
                elif isinstance(sub_paras, list):
                    for it in sub_paras:
                        if isinstance(it, str):
                            _add_block(None, it, sec_id_hint=sid_h, sec_title=st)
                        elif isinstance(it, dict):
                            pid = it.get("paragraph_id") or it.get("id") or it.get("pid") or it.get("jats_id")
                            txt = str(it.get("text") or it.get("content") or it.get("body") or it.get("raw_text") or "")
                            _add_block(pid, txt, sec_id_hint=sid_h, sec_title=st)

        # 处理内置 tables
        if isinstance(raw.get("tables"), list):
            for ti, tb in enumerate(raw["tables"], 1):
                if not isinstance(tb, dict):
                    continue
                rows = tb.get("rows") or []
                hdrs = tb.get("headers") or (rows[0] if rows else [])
                tables.append({
                    "table_id": str(tb.get("table_id") or tb.get("id") or f"tbl_{ti}"),
                    "paragraph_id": str(tb.get("paragraph_id") or tb.get("id") or f"tbl_{ti}"),
                    "page": None,
                    "method": "structured_json",
                    "header_rows": int(tb.get("header_rows") or 1),
                    "headers": hdrs,
                    "num_rows": len(rows),
                    "num_cols": len(hdrs),
                    "caption": tb.get("caption"),
                    "caption_block_id": None,
                    "rows": rows,
                    "markdown": tb.get("markdown") or "",
                })
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                pid = it.get("paragraph_id") or it.get("id") or it.get("pid") or it.get("jats_id")
                txt = str(it.get("text") or it.get("content") or it.get("body") or it.get("raw_text") or "")
                st = it.get("section_title") or it.get("section") or it.get("heading")
                _add_block(pid, txt, sec_title=st)

    return {
        "file": str(json_path),
        "format": "json_material",
        "backend": "structured_corpus_json",
        "status": "ok",
        "source_id": src_id,
        "has_pdf_pages": False,
        "locator_mode": "jats_paragraph_id",
        "page_count": None,
        "extracted_pages": [],
        "metadata": {
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "doi": doi,
            "pmid": str(manifest_meta.get("pmid") or meta_obj.get("pmid") or "unreported"),
            "version": str(manifest_meta.get("version") or meta_obj.get("version") or "unreported"),
            "license": lic,
            "source_file": str(json_path),
            "file_sha256": sha256_file(json_path),
        },
        "page_label_map": {}, "page_numbering_discrepancy": False,
        "sections": sections, "tables": tables, "merged_tables": [], "figures": [],
        "captions": [], "footnotes": [], "equations": [], "references": [],
        "headers_footers": [], "unextracted_regions": [], "blocks": blocks,
    }


def _ingest_text_or_office(file_path: Path) -> Dict[str, Any]:
    """解析 Markdown、TXT、CSV/TSV、DOCX/PPTX/XLSX，保留 Markdown 表格并禁止伪造页码。"""
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

    tables = _extract_markdown_tables(raw_lines) if ext in (".md", ".txt") else []
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
        pid = f"para_{idx + 1}"
        m_md = re.match(r"^(#{1,6})\s+(.+)$", p)
        if m_md:
            lvl = len(m_md.group(1))
            title = m_md.group(2).strip()
            sec_c += 1
            sections.append({"section_id": f"sec_{sec_c}", "title": title, "level": lvl, "page_start": None, "block_id": pid, "paragraph_id": pid})
            blocks.append({"block_id": pid, "paragraph_id": pid, "paragraph_id_source": "synthesized_paragraph_index", "page": None, "role": "heading", "heading_level": lvl, "raw_text": title, "text": title, "defects": detect_encoding_defects(title)})
        elif HEADING_RE.match(p) and len(p) <= 120:
            sec_c += 1
            sections.append({"section_id": f"sec_{sec_c}", "title": p, "level": 1, "page_start": None, "block_id": pid, "paragraph_id": pid})
            blocks.append({"block_id": pid, "paragraph_id": pid, "paragraph_id_source": "synthesized_paragraph_index", "page": None, "role": "heading", "heading_level": 1, "raw_text": p, "text": p, "defects": detect_encoding_defects(p)})
        else:
            blocks.append({"block_id": pid, "paragraph_id": pid, "paragraph_id_source": "synthesized_paragraph_index", "page": None, "role": "paragraph", "raw_text": p, "text": p, "defects": detect_encoding_defects(p)})

    full_head = "\n".join(raw_lines[:30])
    dm = DOI_PATTERN.search(full_head)
    ym = re.search(r"\b((?:19|20)\d{2})\b", full_head)
    return {
        "file": str(file_path), "format": ext.lstrip("."), "backend": "text_or_office_xml", "status": "ok",
        "has_pdf_pages": False, "locator_mode": "paragraph_id",
        "page_count": None, "extracted_pages": [],
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
        "sections": sections, "tables": tables, "merged_tables": [], "figures": [],
        "captions": [], "footnotes": [], "equations": [], "references": [],
        "headers_footers": [], "unextracted_regions": [], "blocks": blocks,
    }


def _enrich_section_tree(doc_data: Dict[str, Any]) -> None:
    sections = doc_data.get("sections", [])
    blocks = doc_data.get("blocks", [])
    tables = doc_data.get("tables", [])
    figures = doc_data.get("figures", [])
    equations = doc_data.get("equations", [])
    has_pdf = bool(doc_data.get("has_pdf_pages"))

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
        s["page_end"] = s.get("page_start") if has_pdf else None
        stack.append(s)

    cur_sec: Optional[Dict[str, Any]] = None
    for b in blocks:
        if b["block_id"] in sec_by_bid:
            cur_sec = sec_by_bid[b["block_id"]]
        b["section_id"] = cur_sec["section_id"] if cur_sec else "sec_frontmatter"
        b["section_title"] = cur_sec["title"] if cur_sec else "Front Matter"
        if "paragraph_id" not in b:
            b["paragraph_id"] = b["block_id"]
        if cur_sec is not None:
            cur_sec["block_ids"].append(b["block_id"])
            cur_sec["char_count"] += len(b.get("raw_text", ""))
            if has_pdf and isinstance(b.get("page"), int):
                pe = cur_sec.get("page_end")
                cur_sec["page_end"] = max(int(pe), int(b["page"])) if isinstance(pe, int) else int(b["page"])

    bid_to_sec = {b["block_id"]: b["section_id"] for b in blocks}
    sec_by_id = {s["section_id"]: s for s in sections}
    for eq in equations:
        sid = bid_to_sec.get(eq.get("block_id", ""))
        eq["section_id"] = sid
        if sid in sec_by_id:
            sec_by_id[sid]["equation_ids"].append(eq["equation_id"])
    for fig in figures:
        sid = bid_to_sec.get(fig.get("caption_block_id") or "")
        if not sid and has_pdf and isinstance(fig.get("page"), int):
            sid = next((s["section_id"] for s in reversed(sections) if isinstance(s.get("page_start"), int) and s["page_start"] <= fig["page"]), None)
        fig["section_id"] = sid
        if sid in sec_by_id:
            sec_by_id[sid]["figure_ids"].append(fig["figure_id"])
    for tbl in tables:
        sid = bid_to_sec.get(tbl.get("caption_block_id") or "")
        if not sid and has_pdf and isinstance(tbl.get("page"), int):
            sid = next((s["section_id"] for s in reversed(sections) if isinstance(s.get("page_start"), int) and s["page_start"] <= tbl["page"]), None)
        tbl["section_id"] = sid
        if sid in sec_by_id:
            sec_by_id[sid]["table_ids"].append(tbl["table_id"])


def extract_kg_cues(doc_data: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    """抽取候选关系线索、作者自述 vs 他人转述标记、缺失材料提示与公式/符号缺陷。"""
    blocks = doc_data.get("blocks", [])
    page_labels = doc_data.get("page_label_map") or {}
    has_pdf = bool(doc_data.get("has_pdf_pages"))

    relation_cues: List[Dict[str, Any]] = []
    cited_cues: List[Dict[str, Any]] = []
    self_cues: List[Dict[str, Any]] = []
    unavailable_refs: List[Dict[str, Any]] = []
    stat_anomalies: List[Dict[str, Any]] = []

    for b in blocks:
        txt = b.get("raw_text", "")
        if not txt or b.get("role") == "reference":
            continue
        p_val = b.get("page") if has_pdf else None
        anchor = {
            "source_id": source_id,
            "paragraph_id": b.get("paragraph_id") or b["block_id"],
            "page": p_val,
            "printed_page": (page_labels.get(p_val) or page_labels.get(str(p_val))) if p_val is not None else None,
            "section_id": b.get("section_id"),
            "section": b.get("section_title"),
            "block_id": b["block_id"],
            "quote": txt[:260],
        }
        if ENTITY_CUE_RE.search(txt) and len(relation_cues) < 60:
            relation_cues.append(anchor)
        if CITED_THIRD_PARTY_RE.search(txt) and len(cited_cues) < 40:
            cited_cues.append(anchor)
        if SELF_STATEMENT_RE.search(txt) and len(self_cues) < 40:
            self_cues.append(anchor)
        um = UNAVAILABLE_MATERIAL_RE.findall(txt)
        if um and len(unavailable_refs) < 30:
            unavailable_refs.append({**anchor, "mentioned_items": list(dict.fromkeys(um))[:6]})
        anom = (b.get("defects") or {}).get("stat_glyph_anomalies") or []
        if anom:
            stat_anomalies.append({**anchor, "anomalies": anom})

    return {
        "source_id": source_id,
        "relation_cues": relation_cues,
        "author_self_cues": self_cues,
        "cited_third_party_cues": cited_cues,
        "unavailable_material_refs": unavailable_refs,
        "stat_anomalies": stat_anomalies,
    }


def cross_check_xml_companion(main_doc: Dict[str, Any], xml_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """用 companion JATS XML（如 secondary-publisher.xml）核对线性化行内公式与上下标，返回差异列表。"""
    diffs: List[Dict[str, Any]] = []
    xml_by_pid: Dict[str, Dict[str, Any]] = {}
    xml_blocks = xml_doc.get("blocks", [])
    for xb in xml_blocks:
        pid = xb.get("paragraph_id")
        if pid:
            xml_by_pid[pid] = xb

    for mb in main_doc.get("blocks", []):
        if mb.get("role") in ("heading", "reference"):
            continue
        pid = mb.get("paragraph_id")
        xb = xml_by_pid.get(pid or "")
        if xb is None:
            mn = normalize_for_match(mb.get("raw_text", ""))
            if len(mn) >= 24:
                m_toks = set(mn.lower().split())
                best, best_sc = None, 0.0
                for cand in xml_blocks:
                    if cand.get("role") in ("heading", "reference"):
                        continue
                    cn = normalize_for_match(cand.get("raw_text", ""))
                    if not cn:
                        continue
                    c_toks = set(cn.lower().split())
                    ov = len(m_toks & c_toks) / max(1, len(m_toks | c_toks))
                    if ov > best_sc:
                        best_sc, best = ov, cand
                if best is not None and best_sc >= 0.72:
                    xb = best
        if xb is not None:
            rich = xb.get("rich_text") or xb.get("text") or ""
            lin = xb.get("raw_text") or ""
            main_txt = mb.get("raw_text") or ""
            if rich and (rich != lin or normalize_for_match(rich) != normalize_for_match(main_txt)):
                if "_{" in rich or "^{" in rich or "<formula>" in rich or "[tex:" in rich:
                    mb["xml_rich_text"] = rich
                    mb["xml_paragraph_id"] = xb.get("paragraph_id")
                    mb["has_linearized_math_diff"] = True
                    diffs.append({
                        "paragraph_id": mb.get("paragraph_id"),
                        "xml_paragraph_id": xb.get("paragraph_id"),
                        "section": mb.get("section_title"),
                        "linearized_text": main_txt[:300],
                        "xml_rich_text": rich[:340],
                    })

    # 若主文档未解析出表格而 XML 存在 table-wrap，则补充挂载
    if not main_doc.get("tables") and xml_doc.get("tables"):
        main_doc["tables"] = xml_doc["tables"]
    if not main_doc.get("equations") and xml_doc.get("equations"):
        main_doc["equations"] = xml_doc["equations"]

    # 仅补充缺失的同篇元数据字段，绝不覆盖已有字段
    m_meta = main_doc.setdefault("metadata", {})
    x_meta = xml_doc.get("metadata") or {}
    for k in ("title", "year", "journal", "doi", "pmid", "license"):
        if (not m_meta.get(k) or m_meta.get(k) == "unreported") and x_meta.get(k) and x_meta.get(k) != "unreported":
            m_meta[k] = x_meta[k]
    if (not m_meta.get("authors") or m_meta.get("authors") == ["unreported"]) and x_meta.get("authors") and x_meta.get("authors") != ["unreported"]:
        m_meta["authors"] = x_meta["authors"]

    return diffs


def _build_section_chunks(doc_data: Dict[str, Any], source_id: str, max_chars: int = 8000) -> List[Dict[str, Any]]:
    """按原生章节切分 <=8,000 字符的预算安全块；大表仅输出紧凑索引。"""
    blocks = doc_data.get("blocks", [])
    has_pdf = bool(doc_data.get("has_pdf_pages"))
    tables_by_sec: Dict[str, List[Dict[str, Any]]] = {}
    for t in doc_data.get("tables", []):
        tables_by_sec.setdefault(t.get("section_id") or "sec_frontmatter", []).append({
            "table_id": t["table_id"], "paragraph_id": t.get("paragraph_id"),
            "page": t["page"] if has_pdf else None,
            "caption": t.get("caption"),
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
        pages_list = sorted({int(b["page"]) for b in cur_blocks if isinstance(b.get("page"), int)}) if has_pdf else []
        chunks.append({
            "chunk_id": f"chunk_{len(chunks) + 1:03d}",
            "source_id": source_id,
            "has_pdf_pages": has_pdf,
            "locator_mode": doc_data.get("locator_mode"),
            "section_ids": list(dict.fromkeys(cur_secs)),
            "section_titles": list(dict.fromkeys(b["section_title"] for b in cur_blocks)),
            "pages": pages_list,
            "total_chars": cur_chars,
            "blocks": cur_blocks,
            "table_index": chunk_tables,
        })
        cur_blocks, cur_chars, cur_secs = [], 0, []

    for b in blocks:
        if b.get("role") == "reference":
            continue
        txt = b.get("raw_text") or b.get("text") or ""
        if not txt.strip():
            continue
        is_new_sec = cur_blocks and b.get("role") == "heading" and cur_chars >= 2200
        if cur_blocks and (is_new_sec or cur_chars + len(txt) > max_chars or len(cur_blocks) >= 50):
            _flush()
        p_val = b.get("page") if has_pdf else None
        entry = {
            "block_id": b["block_id"],
            "paragraph_id": b.get("paragraph_id") or b["block_id"],
            "page": p_val,
            "printed_page": (page_labels.get(p_val) or page_labels.get(str(p_val))) if p_val is not None else None,
            "section_id": b.get("section_id"),
            "section_title": b.get("section_title"),
            "role": b.get("role"),
            "text": txt,
        }
        if b.get("xml_rich_text"):
            entry["xml_rich_text"] = b["xml_rich_text"]
        cur_blocks.append(entry)
        cur_secs.append(b.get("section_id") or "sec_frontmatter")
        cur_chars += len(txt)
    _flush()
    return chunks


def _render_outline(doc_data: Dict[str, Any], cues: Dict[str, Any], formula_diffs: List[Dict[str, Any]]) -> str:
    meta = doc_data.get("metadata") or {}
    has_pdf = bool(doc_data.get("has_pdf_pages"))
    loc_desc = f"Pages={doc_data.get('page_count')}" if has_pdf else f"No PDF pages (locator_mode={doc_data.get('locator_mode')})"
    lines = [
        f"# Paper Outline: `{doc_data.get('source_id')}` ({Path(doc_data['file']).name})",
        f"- Format: `{doc_data.get('format')}` | Backend: `{doc_data.get('backend')}` | {loc_desc} | Status: `{doc_data.get('status')}`",
        f"- Metadata: title={meta.get('title')!r} | authors={meta.get('authors')} | year={meta.get('year')} | journal={meta.get('journal')} | doi={meta.get('doi')} | license={meta.get('license')}",
    ]
    if doc_data.get("companion_formats"):
        lines.append(f"- Companion Format Variants (SAME paper, NOT cross-paper): `{doc_data['companion_formats']}`")
    if doc_data.get("page_numbering_discrepancy"):
        lines.append(f"- **Page Numbering Discrepancy**: `page_label_map={json.dumps(doc_data.get('page_label_map', {}), ensure_ascii=False)}`")
    lines.extend(["", "## Section Tree"])
    for s in doc_data.get("sections", []):
        indent = "  " * max(0, int(s.get("level", 1)) - 1)
        loc_tag = f"p.{s['page_start']}-{s['page_end']}" if has_pdf else f"pid={s.get('paragraph_id')}"
        lines.append(f"{indent}- [{s['section_id']}] {s['title']} ({loc_tag}, blocks={len(s.get('block_ids', []))}, tables={s.get('table_ids')})")
    if not doc_data.get("sections"):
        lines.append("- (no headings detected; inspect section_chunks/)")
    lines.extend(["", "## Tables (Compact Index — inspect via query_kg.py --table <id>)"])
    for t in doc_data.get("tables", []):
        loc_tag = f"p.{t['page']}" if has_pdf else f"pid={t.get('paragraph_id')}"
        lines.append(f"- `{t['table_id']}` ({loc_tag}, {t['num_rows']}x{t['num_cols']}, headers={t.get('headers')}): {t.get('caption') or '(no caption)'}")
    lines.extend(["", "## Pre-Extracted KG Cues & Integrity Signals"])
    lines.append(
        f"- Relation cues: {len(cues['relation_cues'])} | Author self-statements: {len(cues['author_self_cues'])} | "
        f"Cited 3rd-party cues: {len(cues['cited_third_party_cues'])} | Unavailable material mentions: {len(cues['unavailable_material_refs'])} | "
        f"XML linearized math diffs: {len(formula_diffs)} | Stat glyph anomalies: {len(cues['stat_anomalies'])}"
    )
    return "\n".join(lines) + "\n"


def _parse_manifest_entries(manifest_path: Path) -> List[Dict[str, Any]]:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for idx, item in enumerate(raw, 1):
            if isinstance(item, dict):
                e = dict(item)
                e.setdefault("source_id", str(e.get("id") or e.get("paper_id") or e.get("doc_id") or f"paper_{idx}"))
                entries.append(e)
    elif isinstance(raw, dict):
        seq = None
        for k in ("papers", "sources", "documents", "corpus", "entries", "items"):
            if isinstance(raw.get(k), list):
                seq = raw[k]
                break
            if isinstance(raw.get(k), dict):
                seq = [{"source_id": sk, **sv} if isinstance(sv, dict) else {"source_id": sk, "file": sv} for sk, sv in raw[k].items()]
                break
        if seq is not None:
            for idx, item in enumerate(seq, 1):
                if isinstance(item, dict):
                    e = dict(item)
                    e.setdefault("source_id", str(e.get("id") or e.get("paper_id") or e.get("doc_id") or f"paper_{idx}"))
                    entries.append(e)
        else:
            for sk, sv in raw.items():
                if isinstance(sv, dict) and any(x in sv for x in ("doi", "title", "authors", "file", "path", "material_file", "sha256", "source_hash")):
                    e = dict(sv)
                    e.setdefault("source_id", str(e.get("id") or sk))
                    entries.append(e)
    return entries


def _is_base_graph_json(path: Path) -> bool:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("nodes"), list) and isinstance(obj.get("edges"), list):
        return True
    kg = obj.get("knowledge_graph")
    if isinstance(kg, dict) and isinstance(kg.get("nodes"), list) and isinstance(kg.get("edges"), list):
        return True
    return False


def _is_structured_paper_json(path: Path) -> bool:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if isinstance(obj, dict):
        return any(k in obj for k in ("paragraphs", "blocks", "sections", "abstract", "body", "content", "chunks"))
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return any(k in obj[0] for k in ("text", "content", "paragraph_id", "body"))
    return False


def _is_combined_material_index(path: Path) -> Tuple[bool, List[Dict[str, Any]]]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False, []
    items = None
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        for k in ("paragraphs", "items", "entries", "records", "materials"):
            if isinstance(obj.get(k), list):
                items = obj[k]
                break
    if not items or not isinstance(items[0], dict):
        return False, []
    sids = {str(it.get("source_id") or it.get("doc_id") or it.get("paper_id") or "").strip() for it in items if isinstance(it, dict)}
    sids.discard("")
    return (len(sids) >= 2 or path.name.lower() == "material.json"), [it for it in items if isinstance(it, dict)]


def ingest_single_file(file_path: Path, manifest_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """根据扩展名与内容结构解析单个文件，返回统一中间文档结构。"""
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        probe = probe_pdf(file_path)
        if probe.get("needs_password") or not probe.get("valid_pdf_header", True):
            return {"file": str(file_path), "format": "pdf", "status": "blocked", "probe": probe, "has_pdf_pages": True}
        doc = extract_pdf(file_path)
        doc["probe"] = probe
        return doc
    if ext in (".xml", ".nxml", ".jats"):
        doc = _ingest_xml(file_path)
        doc["probe"] = {"file": str(file_path), "format": "xml", "summary": {"overall_status": "native_xml"}}
        return doc
    if ext == ".json" and _is_structured_paper_json(file_path):
        doc = _ingest_structured_paper_json(file_path, manifest_meta=manifest_meta)
        doc["probe"] = {"file": str(file_path), "format": "json_material", "summary": {"overall_status": "structured_json"}}
        return doc
    doc = _ingest_text_or_office(file_path)
    doc["probe"] = {"file": str(file_path), "format": ext.lstrip("."), "summary": {"overall_status": "native_text"}}
    return doc


def _find_manifest_match(fpath: Path, entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    fname = fpath.name.lower()
    fstem = fpath.stem.lower()
    skip_keys = {"source_id", "id", "paper_id", "doc_id", "doi", "title", "authors", "year", "journal", "license", "sha256", "source_hash", "file_sha256", "hash", "role", "abstract"}

    def _walk_vals(v: Any) -> List[str]:
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            res: List[str] = []
            for x in v:
                res.extend(_walk_vals(x))
            return res
        if isinstance(v, dict):
            res = []
            for x in v.values():
                res.extend(_walk_vals(x))
            return res
        return []

    for e in entries:
        for k, val in e.items():
            if k in skip_keys:
                continue
            for s in _walk_vals(val):
                sl = s.strip().lower()
                if sl and (Path(sl).name == fname or sl.endswith("/" + fname) or sl.endswith("\\" + fname)):
                    return e
        sid = str(e.get("source_id") or "").lower()
        if sid and (fstem.startswith(sid + "-") or fstem.startswith(sid + "_") or fstem == sid):
            return e
    return None


def _are_same_paper(doc_a: Dict[str, Any], doc_b: Dict[str, Any]) -> Optional[str]:
    """判断两份解析结果是否属于同一篇论文的不同文件格式，并返回匹配依据。"""
    doi_a = normalize_doi((doc_a.get("metadata") or {}).get("doi"))
    doi_b = normalize_doi((doc_b.get("metadata") or {}).get("doi"))
    if doi_a and doi_b and doi_a == doi_b:
        return f"identical_doi:{doi_a}"

    fa = Path(doc_a["file"]).stem.lower()
    fb = Path(doc_b["file"]).stem.lower()
    for prefix in ("primary", "secondary"):
        if fa.startswith(prefix) and fb.startswith(prefix):
            return f"paired_role_prefix:{prefix}"

    ta = normalize_term_key((doc_a.get("metadata") or {}).get("title") or "")
    tb = normalize_term_key((doc_b.get("metadata") or {}).get("title") or "")
    if ta and tb and ta != "unreported" and ta == tb:
        toks_a = set(" ".join(b.get("raw_text", "") for b in doc_a.get("blocks", [])[:15]).lower().split())
        toks_b = set(" ".join(b.get("raw_text", "") for b in doc_b.get("blocks", [])[:15]).lower().split())
        if toks_a and toks_b:
            jac = len(toks_a & toks_b) / max(1, len(toks_a | toks_b))
            if jac >= 0.45:
                return f"identical_title_and_text_overlap:{jac:.2f}"
    return None


def ingest_corpus(
    input_dir: Path,
    work_dir: Path,
    explicit_files: Optional[List[Path]] = None,
    case_data: Optional[Dict[str, Any]] = None,
    base_graph_path: Optional[Path] = None,
    max_chunk_chars: int = 8000,
) -> Dict[str, Any]:
    """统一摄取语料目录，解析独立论文身份、合并同篇多格式文件并生成紧凑章节块与公式核对记录。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    case_data = case_data or {}

    all_candidates: List[Path] = []
    if explicit_files:
        all_candidates = [p for p in explicit_files if p.exists()]
    elif input_dir.exists():
        all_candidates = [
            p for p in sorted(input_dir.rglob("*"))
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS and p.name not in ("case.json", "task.md")
        ]

    manifest_path = next((p for p in all_candidates if p.name.lower() in ("corpus-manifest.json", "manifest.json")), None)
    manifest_entries = _parse_manifest_entries(manifest_path) if manifest_path else []

    detected_base_graph = base_graph_path
    if detected_base_graph is None:
        for p in all_candidates:
            if p.suffix.lower() == ".json" and p != manifest_path and _is_base_graph_json(p):
                detected_base_graph = p
                break

    material_index_path = next((p for p in all_candidates if p.name.lower() == "material.json"), None)
    per_paper_files = [
        p for p in all_candidates
        if p != manifest_path and p != detected_base_graph and p != material_index_path
    ]

    # 若仅有 material.json 而无独立的 primary/secondary 文件，则按 source_id 拆分为独立论文内存文档
    split_material_docs: List[Dict[str, Any]] = []
    material_index_items: List[Dict[str, Any]] = []
    if material_index_path:
        is_idx, m_items = _is_combined_material_index(material_index_path)
        material_index_items = m_items
        if not per_paper_files and is_idx and m_items:
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for it in m_items:
                sid = str(it.get("source_id") or it.get("doc_id") or it.get("paper_id") or "doc_1")
                grouped.setdefault(sid, []).append(it)
            for sid, items in grouped.items():
                m_meta = next((e for e in manifest_entries if e.get("source_id") == sid), {})
                tmp_json = work_dir / f"_split_{sid}.json"
                tmp_json.write_text(json.dumps({"source_id": sid, "paragraphs": items}, ensure_ascii=False), encoding="utf-8")
                split_material_docs.append(_ingest_structured_paper_json(tmp_json, manifest_meta=m_meta))

    # 排序确保 JSON/PDF 主文本先于 companion XML 载入
    prio = {".json": 0, ".pdf": 1, ".md": 2, ".txt": 3, ".xml": 4, ".nxml": 4, ".jats": 4}
    per_paper_files.sort(key=lambda p: (
        0 if "primary" in p.name.lower() else (1 if "secondary" in p.name.lower() else 2),
        prio.get(p.suffix.lower(), 5),
        p.name.lower(),
    ))

    raw_docs: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = []
    for p in per_paper_files:
        m_entry = _find_manifest_match(p, manifest_entries)
        doc = ingest_single_file(p, manifest_meta=m_entry)
        raw_docs.append((doc, m_entry))
    for sdoc in split_material_docs:
        m_entry = next((e for e in manifest_entries if e.get("source_id") == sdoc.get("source_id")), None)
        raw_docs.append((sdoc, m_entry))

    # 合并同篇多格式变体到同一个 source_id
    canonical_papers: List[Dict[str, Any]] = []
    companion_map: Dict[str, List[Dict[str, Any]]] = {}
    same_paper_dedups: List[Dict[str, Any]] = []

    for doc, m_entry in raw_docs:
        if doc.get("status") == "blocked":
            sid = str((m_entry or {}).get("source_id") or doc.get("source_id") or f"paper_{len(canonical_papers) + 1}")
            doc["source_id"] = sid
            canonical_papers.append(doc)
            continue

        assigned_sid: Optional[str] = None
        dedup_reason: Optional[str] = None
        cand_sid = str((m_entry or {}).get("source_id") or doc.get("source_id") or "").strip()
        if cand_sid and any(cp.get("source_id") == cand_sid for cp in canonical_papers):
            assigned_sid = cand_sid
            dedup_reason = f"manifest_shared_source_id:{cand_sid}"
        else:
            for cp in canonical_papers:
                if cp.get("status") == "blocked":
                    continue
                reason = _are_same_paper(cp, doc)
                if reason:
                    assigned_sid = cp["source_id"]
                    dedup_reason = reason
                    break

        if assigned_sid:
            companion_map.setdefault(assigned_sid, []).append(doc)
            same_paper_dedups.append({
                "source_id": assigned_sid,
                "primary_file": next(cp["file"] for cp in canonical_papers if cp["source_id"] == assigned_sid),
                "companion_file": doc["file"],
                "companion_format": doc.get("format"),
                "reason": dedup_reason,
                "note": "同篇论文的不同文件格式变体，共享同一 source_id，不构成跨篇证据。",
            })
        else:
            if not cand_sid:
                stem_l = Path(doc["file"]).stem.lower()
                if stem_l.startswith("primary"):
                    cand_sid = "primary"
                elif stem_l.startswith("secondary"):
                    cand_sid = "secondary"
                else:
                    cand_sid = f"paper_{len(canonical_papers) + 1}"
            doc["source_id"] = cand_sid
            canonical_papers.append(doc)

    # 根 case.json 元数据严格仅允许兜底第一篇（primary），绝不套用到第二篇
    root_meta_applied_to: Optional[str] = None
    if canonical_papers and any(case_data.get(k) for k in ("doi", "title", "authors", "journal", "year")):
        first = canonical_papers[0]
        if first.get("status") == "ok":
            fmeta = first.setdefault("metadata", {})
            for k in ("doi", "title", "journal", "year"):
                if (not fmeta.get(k) or fmeta.get(k) == "unreported") and case_data.get(k):
                    fmeta[k] = str(case_data[k])
                    root_meta_applied_to = first["source_id"]
            if (not fmeta.get("authors") or fmeta.get("authors") == ["unreported"]) and case_data.get("authors"):
                ca = case_data["authors"]
                fmeta["authors"] = list(ca) if isinstance(ca, list) else [str(ca)]
                root_meta_applied_to = first["source_id"]

    # 核验 manifest 哈希、执行 XML 公式核对并写入工作目录
    manifest_checks: List[Dict[str, Any]] = []
    for e in manifest_entries:
        declared_hash = e.get("sha256") or e.get("source_hash") or e.get("file_sha256")
        if declared_hash:
            matched_file = None
            for p in all_candidates:
                if _find_manifest_match(p, [e]):
                    matched_file = p
                    break
            actual_hash = sha256_file(matched_file) if matched_file else None
            manifest_checks.append({
                "source_id": e.get("source_id"),
                "file": str(matched_file) if matched_file else None,
                "declared_sha256": declared_hash,
                "actual_sha256": actual_hash,
                "hash_match": (str(declared_hash).lower() == str(actual_hash).lower()) if (declared_hash and actual_hash) else None,
            })

    papers_out: List[Dict[str, Any]] = []
    for cp in canonical_papers:
        sid = cp["source_id"]
        p_dir = work_dir / sid
        p_dir.mkdir(parents=True, exist_ok=True)
        if cp.get("status") != "ok":
            papers_out.append({
                "source_id": sid,
                "file": cp["file"],
                "format": cp.get("format"),
                "status": cp.get("status"),
                "has_pdf_pages": bool(cp.get("has_pdf_pages")),
                "probe": cp.get("probe"),
            })
            continue

        companions = companion_map.get(sid, [])
        cp["companion_formats"] = [c["file"] for c in companions]
        formula_diffs: List[Dict[str, Any]] = []
        for comp in companions:
            if comp.get("format") == "xml":
                formula_diffs.extend(cross_check_xml_companion(cp, comp))

        _enrich_section_tree(cp)
        cues = extract_kg_cues(cp, sid)
        chunks = _build_section_chunks(cp, sid, max_chars=max_chunk_chars)

        (p_dir / "document.json").write_text(json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8")
        (p_dir / "kg_cues.json").write_text(json.dumps(cues, ensure_ascii=False, indent=2), encoding="utf-8")
        (p_dir / "formula_diffs.json").write_text(json.dumps(formula_diffs, ensure_ascii=False, indent=2), encoding="utf-8")
        (p_dir / "outline.md").write_text(_render_outline(cp, cues, formula_diffs), encoding="utf-8")
        ch_dir = p_dir / "section_chunks"
        ch_dir.mkdir(parents=True, exist_ok=True)
        for ch in chunks:
            (ch_dir / f"{ch['chunk_id']}.json").write_text(json.dumps(ch, ensure_ascii=False, indent=2), encoding="utf-8")

        papers_out.append({
            "source_id": sid,
            "file": cp["file"],
            "companion_formats": cp["companion_formats"],
            "format": cp.get("format"),
            "backend": cp.get("backend"),
            "status": "ok",
            "has_pdf_pages": bool(cp.get("has_pdf_pages")),
            "locator_mode": cp.get("locator_mode"),
            "page_count": cp.get("page_count"),
            "page_numbering_discrepancy": cp.get("page_numbering_discrepancy", False),
            "metadata": cp.get("metadata"),
            "counts": {
                "blocks": len(cp.get("blocks", [])),
                "sections": len(cp.get("sections", [])),
                "tables": len(cp.get("tables", [])),
                "figures": len(cp.get("figures", [])),
                "equations": len(cp.get("equations", [])),
                "chunks": len(chunks),
                "relation_cues": len(cues["relation_cues"]),
                "author_self_cues": len(cues["author_self_cues"]),
                "cited_third_party_cues": len(cues["cited_third_party_cues"]),
                "unavailable_material_refs": len(cues["unavailable_material_refs"]),
                "formula_diffs": len(formula_diffs),
                "stat_anomalies": len(cues["stat_anomalies"]),
            },
            "probe_summary": (cp.get("probe") or {}).get("summary", {}),
            "document_json_path": str(p_dir / "document.json"),
            "outline_path": str(p_dir / "outline.md"),
            "cues_path": str(p_dir / "kg_cues.json"),
            "formula_diffs_path": str(p_dir / "formula_diffs.json"),
            "chunk_paths": [str(ch_dir / f"{ch['chunk_id']}.json") for ch in chunks],
        })

    # 校验 material.json 汇总索引覆盖情况（不把 material.json 算作第三篇）
    material_index_audit = None
    if material_index_path and material_index_items:
        known_pids = {
            (p["source_id"], b.get("paragraph_id"))
            for p in papers_out if p.get("status") == "ok"
            for b in json.loads(Path(p["document_json_path"]).read_text(encoding="utf-8")).get("blocks", [])
        }
        idx_pairs = [
            (str(it.get("source_id") or it.get("doc_id") or ""), str(it.get("paragraph_id") or it.get("id") or ""))
            for it in material_index_items
        ]
        matched = sum(1 for pr in idx_pairs if pr in known_pids)
        material_index_audit = {
            "material_index_file": str(material_index_path),
            "indexed_entries": len(idx_pairs),
            "matched_in_canonical_papers": matched,
            "treated_as_separate_paper": False,
        }

    return {
        "manifest_file": str(manifest_path) if manifest_path else None,
        "manifest_hash_checks": manifest_checks,
        "material_index_audit": material_index_audit,
        "base_graph_path": str(detected_base_graph) if detected_base_graph else None,
        "root_metadata_isolated_to": root_meta_applied_to,
        "same_paper_format_dedups": same_paper_dedups,
        "paper_count": len(papers_out),
        "papers": papers_out,
    }
