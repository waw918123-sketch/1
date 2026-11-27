---
case_id: example-notes-synthetic-001
status: completed
tags: [literature-notes, structured-summary, evidence-linked]
note_type: ILLUSTRATIVE SYNTHETIC EXAMPLE ONLY
---

# Structured Literature Notes (example-notes-synthetic-001)

## 1. Paper Cards (Comparable Matrix)

| Doc | Title | Research Question | Data / Materials | Method & Preconditions | Key Findings | Limitations |
| --- | --- | --- | --- | --- | --- | --- |
| doc_1 | Synthetic CO2 Hydrogenation over MOF-74 | Quantify selectivity and Turnover Frequency of MOF-74 under mild pressure. | MOF-74 catalyst; reactor wall thickness unreported. | Steady-state fixed-bed flow at 673 K and 1.0 bar. | Table 1 reports 91.8% selectivity at 673 K (Abstract states 92.4%). | Did not evaluate long-term fouling beyond 12 h. |

## 2. Section-Aligned Summary (Paper Skeleton)

### [sec_1] 1. Introduction
- **Purpose**: Establish Turnover Frequency (TOF) kinetics motivation.
  - Anchor: `[doc_1|p.1|p1_b4]` — "Turnover Frequency (TOF) governs CO2 hydrogenation kinetics."

### [sec_2] 2. Experimental Setup
- **Transition from previous**: Specifies operating conditions to test the kinetic hypothesis from Section 1.
- **Methods & Conditions**: Steady-state flow at 673 K and 1.0 bar.
- **Limitations**: unreported
  - Anchor: `[doc_1|p.1|p1_b6]` — "Tests were performed at 673 K and 1.0 bar assuming steady-state flow."

## 3. Atomic Knowledge Points (Searchable Index)

- **`Metal-Organic Framework`** (aliases: MOF-74) [finding | `deterministic`]: Exhibits 91.8% selectivity in Table 1.
  - Preconditions: 673 K, 1.0 bar, steady-state flow
  - Source: `[doc_1|p.2|tbl_1]` — "MOF-74 673 91.8"
