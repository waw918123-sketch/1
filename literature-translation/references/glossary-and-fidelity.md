# Glossary Locking & Fidelity Checks

## Glossary Lock Hierarchy
1. **Supplied Glossary (`locked: true`)**: Extracted verbatim from `case.json`, `task.md`, or `.csv/.tsv/.json/.xlsx` attachments. Every target translation must match identically across titles, headings, body paragraphs, captions, and conclusions.
2. **Candidate Terms**: Recurring multi-word domain phrases mined from `all_units_context` — commit to one Chinese target per sense across the entire document.

## Preserved Markup & Scientific Tokens
- **Inline XML tags**: Keep `<sub>`, `<sup>`, `<italic>`, `<bold>`, `<xref ...>` verbatim (e.g., `K<sub><italic>m</italic></sub>`, NEVER flatten to `Km`).
- **PDF script notation**: Keep `^{...}` and `_{...}` verbatim.
- **Panel tags**: Keep figure/subfigure labels such as `(A2)`, `(B1)` attached to the corresponding clause.

## Clause-Level Negation & Scope Audit
`translate_audit.py` splits source and target sentences across contrastive boundaries (`,`, `;`, `but`, `whereas`, `however` ↔ `，`, `；`, `但`, `然而`, `却`) and compares per-clause negation polarity. Swapping negation across clauses (`does not increase A, but reduces B` → `增加 A，但不减少 B`) fails `--strict` even when sentence-level negation counts appear equal.
