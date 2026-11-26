#!/usr/bin/env python3
"""Query extracted document.json / ocr_results.json with exact page, block_id, bbox, and verbatim quote provenance."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.dont_write_bytecode = True

from pdf_common import parse_page_spec


def main() -> int:
    parser = argparse.ArgumentParser(description="Search extracted document.json for blocks, tables, figures, equations, and metadata with provenance.")
    parser.add_argument("--doc", required=True, help="Path to document.json.")
    parser.add_argument("--ocr", default=None, help="Optional path to ocr_results.json.")
    parser.add_argument("--query", default=None, help="Keyword or regex search string (case-insensitive).")
    parser.add_argument("--page", type=int, default=None, help="Filter by 1-based page number.")
    parser.add_argument("--pages", default=None, help="Filter by 1-based page range spec, e.g. '1-3,5'.")
    parser.add_argument("--section", default=None, help="Filter by section title substring.")
    parser.add_argument("--table", default=None, help="Return full table matching table_id (e.g. tbl_1).")
    parser.add_argument("--figures", action="store_true", help="Include matching figures[] entries.")
    parser.add_argument("--equations", action="store_true", help="Include matching equations[] entries.")
    parser.add_argument("--metadata", action="store_true", help="Include metadata, page_label_map, and counts.")
    parser.add_argument("--limit", type=int, default=12, help="Max block hits to return.")
    args = parser.parse_args()

    doc = json.loads(Path(args.doc).read_text(encoding="utf-8"))
    blocks = doc.get("blocks", [])
    sections = doc.get("sections", [])
    tables = doc.get("tables", [])
    merged_tables = doc.get("merged_tables", [])
    figures = doc.get("figures", [])
    equations = doc.get("equations", [])

    page_filter: Optional[Set[int]] = None
    if args.pages:
        total_p = int(doc.get("page_count") or max([b.get("page", 1) for b in blocks] or [1]))
        page_filter = set(parse_page_spec(args.pages, total_p))
    elif args.page is not None:
        page_filter = {args.page}

    # Map block_id -> active section title
    sec_by_block = {s["block_id"]: s["title"] for s in sections if s.get("block_id")}
    current_sec = None
    for b in blocks:
        if b["block_id"] in sec_by_block:
            current_sec = sec_by_block[b["block_id"]]
        b["_section"] = current_sec

    if args.table:
        q_low = args.table.lower()
        matched_tables = [t for t in tables if t.get("table_id") == args.table or q_low in str(t.get("caption") or "").lower()]
        matched_merged = [m for m in merged_tables if m.get("merged_table_id") == args.table or q_low in str(m.get("caption") or "").lower() or args.table in m.get("source_table_ids", [])]
        print(json.dumps({"tables": matched_tables, "merged_tables": matched_merged}, ensure_ascii=False, indent=2))
        return 0

    pat = re.compile(args.query, re.I) if args.query else None
    hits: List[Dict[str, Any]] = []
    for b in blocks:
        if page_filter is not None and b.get("page") not in page_filter:
            continue
        if args.section and args.section.lower() not in str(b.get("_section") or "").lower():
            continue
        hay = f"{b.get('raw_text', '')}\n{b.get('text', '')}"
        if pat and not pat.search(hay):
            continue
        hit_entry: Dict[str, Any] = {
            "page": b["page"],
            "block_id": b["block_id"],
            "section": b.get("_section"),
            "role": b.get("role"),
            "bbox": b.get("bbox"),
            "quote": b.get("raw_text"),
            "script_text": b.get("text") if b.get("text") != b.get("raw_text") else None,
        }
        if b.get("continues_across_break"):
            hit_entry["continues_across_break"] = True
            hit_entry["continued_block_id"] = b.get("continued_block_id")
        hits.append(hit_entry)
        if len(hits) >= args.limit:
            break

    tbl_hits: List[Dict[str, Any]] = []
    for t in tables:
        if page_filter is not None and t.get("page") not in page_filter:
            continue
        hay = f"{t.get('caption') or ''}\n{t.get('markdown') or ''}"
        if pat and not pat.search(hay):
            continue
        tbl_hits.append({
            "table_id": t["table_id"],
            "page": t["page"],
            "caption": t.get("caption"),
            "bbox": t.get("bbox"),
            "markdown": t.get("markdown"),
        })

    ocr_hits: List[Dict[str, Any]] = []
    if args.ocr and Path(args.ocr).exists():
        ocr_data = json.loads(Path(args.ocr).read_text(encoding="utf-8"))
        for item in ocr_data.get("rendered", []):
            pno = item.get("page")
            if page_filter is not None and pno not in page_filter:
                continue
            txt = (item.get("ocr") or {}).get("text") or ""
            if pat and not pat.search(txt):
                continue
            if txt.strip() or not pat:
                ocr_hits.append({
                    "page": pno,
                    "kind": item.get("kind"),
                    "image_path": item.get("image_path"),
                    "mean_confidence": (item.get("ocr") or {}).get("mean_confidence"),
                    "text": txt,
                })

    output: Dict[str, Any] = {"block_hits": hits, "table_hits": tbl_hits, "ocr_hits": ocr_hits}
    if args.figures:
        fig_hits = []
        for fig in figures:
            if page_filter is not None and fig.get("page") not in page_filter:
                continue
            if pat and not pat.search(str(fig.get("caption") or "")):
                continue
            fig_hits.append(fig)
        output["figure_hits"] = fig_hits
    if args.equations:
        eq_hits = []
        for eq in equations:
            if page_filter is not None and eq.get("page") not in page_filter:
                continue
            hay = f"{eq.get('label') or ''}\n{eq.get('raw_text') or ''}\n{eq.get('script_text') or ''}"
            if pat and not pat.search(hay):
                continue
            eq_hits.append(eq)
        output["equation_hits"] = eq_hits
    if args.metadata:
        output["metadata"] = doc.get("metadata")
        output["page_label_map"] = doc.get("page_label_map")
        output["page_numbering_discrepancy"] = doc.get("page_numbering_discrepancy", False)
        output["counts"] = doc.get("counts")

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
