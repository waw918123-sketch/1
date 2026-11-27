#!/usr/bin/env python3
"""Deterministic machine-readable Nanopublication exporters (W3C TriG, JSON-LD, Markdown) + TriG syntax verifier."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from doc_common import discover_capabilities, normalize_term_key, sha256_text


def _iri_slug(text: str, fallback: str = "entity") -> str:
    slug = re.sub(r"[^A-Za-z0-9_\-]+", "_", (text or "").strip()).strip("_")
    return slug[:64] if slug else fallback


def _trig_lit(val: Any) -> str:
    """Escape a Python value as a safe Turtle/TriG double-quoted string literal."""
    s = "" if val is None else str(val)
    s = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    return f'"{s}"'


def _build_trig(result: Dict[str, Any]) -> str:
    """Serialize Nanopublications into valid W3C RDF TriG with Head/assertion/provenance/pubinfo named graphs."""
    case_id = _iri_slug(str(result.get("case_id") or "case"), "case")
    base_ns = f"http://purl.org/nanopub/local/{case_id}/"
    lines: List[str] = [
        "@prefix np: <http://www.nanopub.org/nschema#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix dct: <http://purl.org/dc/terms/> .",
        "@prefix pav: <http://purl.org/pav/> .",
        "@prefix prov: <http://www.w3.org/ns/prov#> .",
        "@prefix cito: <http://purl.org/spar/cito/> .",
        "@prefix schema: <https://schema.org/> .",
        f"@prefix npdoc: <{base_ns}> .",
        "@prefix npprop: <http://purl.org/nanopub/local/vocab/> .",
        "",
    ]

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nanopubs = result.get("nanopublications") or []

    for idx, np_item in enumerate(nanopubs, start=1):
        if not isinstance(np_item, dict):
            continue
        np_id = _iri_slug(str(np_item.get("nanopub_id") or f"np_{idx:03d}"), f"np_{idx:03d}")
        np_uri = str(np_item.get("nanopub_uri") or f"{base_ns}{np_id}")
        head_uri = f"<{np_uri}#Head>"
        ass_uri = f"<{np_uri}#assertion>"
        prov_uri = f"<{np_uri}#provenance>"
        pub_uri = f"<{np_uri}#pubinfo>"
        root_uri = f"<{np_uri}>"

        ass = np_item.get("assertion") or {}
        prov = np_item.get("provenance") or {}
        pub = np_item.get("pubinfo") or {}

        subj_lbl = str(ass.get("subject") or "unreported")
        pred_lbl = str(ass.get("canonical_predicate") or ass.get("predicate") or "not_reported")
        obj_lbl = str(ass.get("object") or "unreported")
        subj_iri = f"npdoc:entity_{_iri_slug(subj_lbl)}_{sha256_text(normalize_term_key(subj_lbl))[:8]}"
        pred_iri = f"npprop:{_iri_slug(pred_lbl, 'predicate')}"
        stmt_iri = f"<{np_uri}#statement>"

        # Head Graph
        lines.extend([
            f"{head_uri} {{",
            f"  {root_uri} a np:Nanopublication ;",
            f"    np:hasAssertion {ass_uri} ;",
            f"    np:hasProvenance {prov_uri} ;",
            f"    np:hasPublicationInfo {pub_uri} .",
            "}",
            "",
        ])

        # Assertion Graph
        lines.extend([
            f"{ass_uri} {{",
            f"  {subj_iri} rdfs:label {_trig_lit(subj_lbl)} ;",
            f"    {pred_iri} {_trig_lit(obj_lbl)} .",
            f"  {stmt_iri} a rdf:Statement ;",
            f"    rdf:subject {subj_iri} ;",
            f"    rdf:predicate {pred_iri} ;",
            f"    rdf:object {_trig_lit(obj_lbl)} ;",
            f"    rdfs:label {_trig_lit(ass.get('assertion_text') or '')} ;",
            f"    npprop:polarity {_trig_lit(ass.get('polarity') or 'unreported')} ;",
            f"    npprop:conditions {_trig_lit(ass.get('conditions') or 'unreported')} ;",
            f"    npprop:statistics {_trig_lit(json.dumps(ass.get('statistics'), ensure_ascii=False) if isinstance(ass.get('statistics'), (dict, list)) else (ass.get('statistics') or 'unreported'))} ;",
            f"    npprop:studyDesign {_trig_lit(ass.get('study_design') or 'unreported')} .",
            "}",
            "",
        ])

        # Provenance Graph
        lines.extend([
            f"{prov_uri} {{",
            f"  {ass_uri} prov:wasGeneratedBy {_trig_lit(prov.get('extraction_method') or 'unreported')} ;",
            f"    npprop:evidenceType {_trig_lit(prov.get('evidence_type') or 'unreported')} ;",
            f"    npprop:confidence {_trig_lit(prov.get('confidence') or 'low')} ;",
            f"    npprop:confidenceBasis {_trig_lit(prov.get('confidence_basis') or 'unreported')} .",
        ])
        for a_i, anc in enumerate(prov.get("evidence_anchors") or [], start=1):
            if not isinstance(anc, dict):
                continue
            anc_iri = f"<{np_uri}#evidence_{a_i}>"
            lines.extend([
                f"  {ass_uri} cito:citesAsEvidence {anc_iri} .",
                f"  {anc_iri} a prov:Entity ;",
                f"    npprop:docId {_trig_lit(anc.get('doc_id') or 'doc_1')} ;",
                f"    npprop:page {int(anc.get('page')) if isinstance(anc.get('page'), int) else 0} ;",
                f"    npprop:printedPage {_trig_lit(anc.get('printed_page') or 'unreported')} ;",
                f"    npprop:sectionId {_trig_lit(anc.get('section_id') or 'unreported')} ;",
                f"    npprop:blockId {_trig_lit(anc.get('block_id') or 'unreported')} ;",
                f"    npprop:tableId {_trig_lit(anc.get('table_id') or 'none')} ;",
                f"    npprop:rowLabel {_trig_lit(anc.get('row_label') or 'none')} ;",
                f"    npprop:colHeader {_trig_lit(anc.get('col_header') or 'none')} ;",
                f"    npprop:cellValue {_trig_lit(anc.get('value') if anc.get('value') is not None else 'none')} ;",
                f"    npprop:quoteSha256 {_trig_lit(anc.get('quote_sha256') or 'none')} ;",
                f"    prov:value {_trig_lit(anc.get('quote') or '')} .",
            ])
        lines.extend(["}", ""])

        # PubInfo Graph
        authors = pub.get("authors") or ["unreported"]
        authors_str = "; ".join(str(a) for a in authors) if isinstance(authors, list) else str(authors)
        doi_str = str(pub.get("doi") or "unreported")
        lines.extend([
            f"{pub_uri} {{",
            f"  {root_uri} dct:created {_trig_lit(pub.get('created_at') or now_iso)}^^xsd:dateTime ;",
            f"    pav:createdBy {_trig_lit(pub.get('generated_by') or 'nanopublication-claims')} ;",
            f"    dct:title {_trig_lit(pub.get('title') or 'unreported')} ;",
            f"    pav:authoredBy {_trig_lit(authors_str)} ;",
            f"    dct:issued {_trig_lit(pub.get('year') or 'unreported')} ;",
            f"    schema:isPartOf {_trig_lit(pub.get('journal') or 'unreported')} ;",
            f"    dct:identifier {_trig_lit(doi_str)} ;",
            f"    dct:license {_trig_lit(pub.get('license') or 'unreported')} ;",
            f"    npprop:sourceFile {_trig_lit(pub.get('source_file') or 'unreported')} ;",
            f"    npprop:fileSha256 {_trig_lit(pub.get('file_sha256') or 'unreported')} .",
            "}",
            "",
        ])

    return "\n".join(lines).rstrip() + "\n"


def validate_trig_syntax(trig_text: str) -> Tuple[bool, List[str]]:
    """Validate TriG syntax via pure-Python balanced-graph parser and optional rdflib cross-check."""
    errors: List[str] = []
    if not trig_text.strip():
        return False, ["TriG output is empty."]

    # Strip quoted strings while respecting escapes before checking graph brace structure
    cleaned: List[str] = []
    i = 0
    n = len(trig_text)
    in_str = False
    while i < n:
        ch = trig_text[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_str = False
                cleaned.append('"')
            elif ch in ("\n", "\r"):
                errors.append("Unescaped newline inside quoted TriG literal.")
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            cleaned.append('"')
        else:
            cleaned.append(ch)
        i += 1
    if in_str:
        errors.append("Unterminated string literal in TriG output.")

    scrub = "".join(cleaned)
    depth = 0
    for ch in scrub:
        if ch == "{":
            depth += 1
            if depth > 1:
                errors.append("Nested '{' detected outside TriG named graph.")
                break
        elif ch == "}":
            depth -= 1
            if depth < 0:
                errors.append("Unmatched '}' in TriG output.")
                break
    if depth != 0:
        errors.append("Unclosed '{' named graph block in TriG output.")

    if not re.search(r"@prefix\s+np:\s*<http://www\.nanopub\.org/nschema#>\s*\.", scrub):
        errors.append("Missing required @prefix np: declaration.")

    caps = discover_capabilities()
    if caps["python_modules"].get("rdflib") and not errors:
        try:
            from rdflib import ConjunctiveGraph  # type: ignore

            g = ConjunctiveGraph()
            g.parse(data=trig_text, format="trig")
        except Exception as exc:
            errors.append(f"rdflib TriG parse failure: {exc}")

    return len(errors) == 0, errors


def _build_jsonld(result: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize Nanopublications into W3C-compatible JSON-LD."""
    graph: List[Dict[str, Any]] = []
    for idx, np_item in enumerate(result.get("nanopublications") or [], start=1):
        if not isinstance(np_item, dict):
            continue
        np_uri = np_item.get("nanopub_uri") or f"urn:nanopub:{idx}"
        graph.append({
            "@id": np_uri,
            "@type": "np:Nanopublication",
            "nanopub_id": np_item.get("nanopub_id"),
            "assertion": np_item.get("assertion"),
            "provenance": np_item.get("provenance"),
            "pubinfo": np_item.get("pubinfo"),
        })
    return {
        "@context": {
            "np": "http://www.nanopub.org/nschema#",
            "prov": "http://www.w3.org/ns/prov#",
            "dct": "http://purl.org/dc/terms/",
            "pav": "http://purl.org/pav/",
            "cito": "http://purl.org/spar/cito/",
        },
        "case_id": result.get("case_id"),
        "status": result.get("status"),
        "@graph": graph,
    }


def _build_markdown_report(result: Dict[str, Any]) -> str:
    cid = result.get("case_id") or "unknown"
    status = result.get("status") or "unknown"
    lines = [
        "---",
        f"case_id: {cid}",
        f"status: {status}",
        "tags: [nanopublication, atomic-claims, provenance]",
        "---",
        "",
        f"# Nanopublication Verification Report ({cid})",
        "",
        "## 1. Publication Info (PubInfo)",
        "",
    ]
    pub_map = result.get("pubinfo") or {}
    if isinstance(pub_map, dict):
        for did, meta in pub_map.items():
            if not isinstance(meta, dict):
                continue
            lines.append(f"- **`{did}`**: {meta.get('title')} | Authors: `{meta.get('authors')}` | Year: `{meta.get('year')}` | Journal: `{meta.get('journal')}` | DOI: `{meta.get('doi')}`")
        lines.append("")

    nps = result.get("nanopublications") or []
    lines.extend([f"## 2. Atomic Nanopublications ({len(nps)})", ""])
    for np_item in nps:
        ass = np_item.get("assertion") or {}
        prov = np_item.get("provenance") or {}
        lines.append(f"### `{np_item.get('nanopub_id')}` — `{ass.get('subject')}` —[`{ass.get('canonical_predicate') or ass.get('predicate')}`]→ `{ass.get('object')}`")
        lines.append(f"- **Statement**: {ass.get('assertion_text')}")
        lines.append(f"- **Conditions**: {ass.get('conditions')} | **Statistics**: {ass.get('statistics') or 'unreported'} | **Design**: {ass.get('study_design') or 'unreported'}")
        lines.append(f"- **Provenance**: `{prov.get('evidence_type')}` via `{prov.get('extraction_method')}` | **Confidence**: `{prov.get('confidence')}` ({prov.get('confidence_basis')})")
        for a in prov.get("evidence_anchors") or []:
            loc = a.get("table_id") or a.get("block_id")
            cell = f" [{a.get('row_label')} | {a.get('col_header')} = {a.get('value')}]" if a.get("table_id") else ""
            lines.append(f"  - Evidence `[{a.get('doc_id')}|p.{a.get('page')}|{loc}{cell}]`: \"{str(a.get('quote', ''))[:180]}\"")
        lines.append("")

    if result.get("conflicts"):
        lines.extend(["## 3. Conflicts & Discrepancies", ""])
        for cf in result["conflicts"]:
            lines.append(f"- {json.dumps(cf, ensure_ascii=False)}")
        lines.append("")

    if result.get("human_review_items"):
        lines.extend(["## 4. Human Review Checklist", ""])
        for hr in result["human_review_items"]:
            lines.append(f"- [ ] {json.dumps(hr, ensure_ascii=False)}")
        lines.append("")

    if result.get("uncertainty"):
        lines.extend(["## 5. Uncertainty & Unreported Limits", ""])
        unc = result["uncertainty"]
        lines.extend([f"- {u}" for u in (unc if isinstance(unc, list) else [unc])])
        lines.append("")

    return "\n".join(lines)


def export_all(result: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    """Write TriG, JSON-LD, and Markdown report to out_dir and validate TriG syntax."""
    out_dir.mkdir(parents=True, exist_ok=True)
    trig_path = out_dir / "nanopublications.trig"
    jsonld_path = out_dir / "nanopublications.jsonld"
    md_path = out_dir / "nanopubs_report.md"

    trig_txt = _build_trig(result)
    trig_ok, trig_errs = validate_trig_syntax(trig_txt)
    trig_path.write_text(trig_txt, encoding="utf-8")

    jsonld_obj = _build_jsonld(result)
    jsonld_path.write_text(json.dumps(jsonld_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path.write_text(_build_markdown_report(result), encoding="utf-8")

    return {
        "trig_path": str(trig_path),
        "trig_valid": trig_ok,
        "trig_errors": trig_errs,
        "jsonld_path": str(jsonld_path),
        "markdown_path": str(md_path),
    }
