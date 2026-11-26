#!/usr/bin/env python3
"""Query extracted document.json / ocr_results.json with exact page, block_id, bbox, and verbatim quote provenance."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List


def main() -> int:
    parser = argparse.ArgumentParser(description="Search extracted document.json for blocks, tables, and equations with provenance.")
    parser.add_argument("--doc", required=True, help="Path to document.json.")
    parser.add_argument("--ocr", default=None, help="Optional path to ocr_results.json.")
    parser.add_argument("--query", default=None, help="Keyword or regex search string (case-insensitive).")
    parser.add_argument("--page", type=int, default=None, help="Filter by 1-based page number.")
    parser.add_argument("--section", default=None, help="Filter by section title substring.")
    parser.add_argument("--table", default=None, help="Return full table matching table_id (e.g. tbl_1).")
    parser.add_argument("--limit", type=int, default=12, help="Max block hits to return.")
    args = parser.parse_args()

    doc = json.loads(Path(args.doc).read_text(encoding="utf-8"))
    blocks = doc.get("blocks", [])
    sections = doc.get("sections", [])
    tables = doc.get("tables", [])
    merged_tables = doc.get("merged_tables", [])

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
        if args.page is not None and b.get("page") != args.page:
            continue
        if args.section and args.section.lower() not in str(b.get("_section") or "").lower():
            continue
        hay = f"{b.get('raw_text', '')}\n{b.get('text', '')}"
        if pat and not pat.search(hay):
            continue
        hits.append({
            "page": b["page"],
            "block_id": b["block_id"],
            "section": b.get("_section"),
            "role": b.get("role"),
            "bbox": b.get("bbox"),
            "quote": b.get("raw_text"),
            "script_text": b.get("text") if b.get("text") != b.get("raw_text") else None,
        })
        if len(hits) >= args.limit:
            break

    tbl_hits: List[Dict[str, Any]] = []
    for t in tables:
        if args.page is not None and t.get("page") != args.page:
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
            if args.page is not None and pno != args.page:
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

    print(json.dumps({"block_hits": hits, "table_hits": tbl_hits, "ocr_hits": ocr_hits}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
