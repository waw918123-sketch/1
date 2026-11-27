#!/usr/bin/env python3
"""Low-token navigator & evidence retriever for ingested scientific documents."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.dont_write_bytecode = True

from doc_common import parse_page_spec


def _load_docs(work_dir: Optional[Path], doc_path: Optional[Path], doc_id: Optional[str]) -> List[Dict[str, Any]]:
    loaded: List[Dict[str, Any]] = []
    if doc_path and doc_path.exists():
        d = json.loads(doc_path.read_text(encoding="utf-8"))
        d.setdefault("doc_id", doc_id or "doc_1")
        d["_dir"] = str(doc_path.parent)
        loaded.append(d)
        return loaded
    if work_dir and work_dir.exists():
        for dp in sorted(work_dir.glob("*/document.json")):
            d = json.loads(dp.read_text(encoding="utf-8"))
            if doc_id and d.get("doc_id") != doc_id:
                continue
            d["_dir"] = str(dp.parent)
            loaded.append(d)
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description="Query sections, chunks, tables, cues, and verbatim evidence from ingested documents.")
    parser.add_argument("--work-dir", default="/app/work", help="Work directory containing doc_*/document.json.")
    parser.add_argument("--doc", default=None, help="Direct path to a specific document.json.")
    parser.add_argument("--doc-id", default=None, help="Filter by doc_id (e.g. doc_1).")
    parser.add_argument("--query", default=None, help="Keyword or regex search.")
    parser.add_argument("--section", default=None, help="Filter by section_id or section title substring.")
    parser.add_argument("--chunk", default=None, help="Return a specific section chunk (e.g. chunk_001).")
    parser.add_argument("--pages", default=None, help="Filter by 1-based page range, e.g. '1-3,5'.")
    parser.add_argument("--table", default=None, help="Return matching table_id or caption substring.")
    parser.add_argument("--conditions", action="store_true", help="Include pre-extracted condition/assumption cues.")
    parser.add_argument("--limitations", action="store_true", help="Include pre-extracted limitation cues.")
    parser.add_argument("--terms", action="store_true", help="Include pre-extracted abbreviation/term candidates.")
    parser.add_argument("--figures", action="store_true", help="Include figures[].")
    parser.add_argument("--equations", action="store_true", help="Include equations[].")
    parser.add_argument("--limit", type=int, default=14)
    args = parser.parse_args()

    docs = _load_docs(Path(args.work_dir), Path(args.doc) if args.doc else None, args.doc_id)
    if not docs:
        print(json.dumps({"error": "No document.json found. Run prep_notes.py first."}))
        return 1

    if args.chunk:
        for d in docs:
            cp = Path(d["_dir"]) / "section_chunks" / f"{args.chunk}.json"
            if cp.exists():
                print(cp.read_text(encoding="utf-8")[:24000])
                return 0
        print(json.dumps({"error": f"Chunk {args.chunk!r} not found."}))
        return 1

    pat = re.compile(args.query, re.I) if args.query else None
    out: Dict[str, Any] = {"documents_queried": [d.get("doc_id") for d in docs], "block_hits": [], "table_hits": []}
    if args.conditions:
        out["condition_cues"] = []
    if args.limitations:
        out["limitation_cues"] = []
    if args.terms:
        out["abbreviations"] = []
    if args.figures:
        out["figure_hits"] = []
    if args.equations:
        out["equation_hits"] = []

    for d in docs:
        did = d.get("doc_id", "doc_1")
        page_labels = d.get("page_label_map") or {}
        total_p = int(d.get("page_count") or 1)
        p_filter: Optional[Set[int]] = set(parse_page_spec(args.pages, total_p)) if args.pages else None

        if args.table:
            ql = args.table.lower()
            for t in d.get("tables", []):
                if t.get("table_id") == args.table or ql in str(t.get("caption") or "").lower():
                    out["table_hits"].append({"doc_id": did, **t})
            continue

        for b in d.get("blocks", []):
            if p_filter is not None and b.get("page") not in p_filter:
                continue
            if args.section:
                sl = args.section.lower()
                if sl != str(b.get("section_id") or "").lower() and sl not in str(b.get("section_title") or "").lower():
                    continue
            hay = f"{b.get('raw_text', '')}\n{b.get('text', '')}"
            if pat and not pat.search(hay):
                continue
            if args.query or args.section or args.pages:
                out["block_hits"].append({
                    "doc_id": did,
                    "page": b["page"],
                    "printed_page": page_labels.get(b["page"]) or page_labels.get(str(b["page"])),
                    "section_id": b.get("section_id"),
                    "section": b.get("section_title"),
                    "block_id": b["block_id"],
                    "role": b.get("role"),
                    "bbox": b.get("bbox"),
                    "quote": b.get("raw_text"),
                })
                if len(out["block_hits"]) >= args.limit:
                    break

        for t in d.get("tables", []):
            if p_filter is not None and t.get("page") not in p_filter:
                continue
            hay = f"{t.get('caption') or ''}\n{t.get('markdown') or ''}"
            if pat and not pat.search(hay):
                continue
            if args.query or args.section or args.pages:
                out["table_hits"].append({
                    "doc_id": did, "table_id": t["table_id"], "page": t["page"],
                    "section_id": t.get("section_id"), "caption": t.get("caption"), "markdown": t.get("markdown"),
                })

        cues_p = Path(d["_dir"]) / "terminology_candidates.json"
        if cues_p.exists() and (args.conditions or args.limitations or args.terms):
            cues = json.loads(cues_p.read_text(encoding="utf-8"))
            if args.conditions:
                out["condition_cues"].extend(cues.get("condition_cues", []))
            if args.limitations:
                out["limitation_cues"].extend(cues.get("limitation_cues", []))
            if args.terms:
                out["abbreviations"].extend(cues.get("abbreviations", []))

        if args.figures:
            out["figure_hits"].extend({"doc_id": did, **f} for f in d.get("figures", []) if not pat or pat.search(str(f.get("caption") or "")))
        if args.equations:
            out["equation_hits"].extend({"doc_id": did, **e} for e in d.get("equations", []) if not pat or pat.search(str(e.get("raw_text") or "")))

    txt = json.dumps(out, ensure_ascii=False, indent=2)
    while len(txt) > 24000 and len(out["block_hits"]) > 1:
        out["block_hits"].pop()
        out["truncated"] = True
        txt = json.dumps(out, ensure_ascii=False, indent=2)
    print(txt[:24000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
