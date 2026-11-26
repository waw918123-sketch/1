# XML & PDF Locator Conventions and Scope Filtering

## JATS / Publisher XML Locators
`xml_pdf_extract.py` builds deterministic structural paths such as:
- `article-title`
- `abstract/p[1]`
- `body/sec[1]/title[1]`, `body/sec[1]/p[2]`
- `body/fig[1]/label[1]`, `body/fig[1]/caption[1]/title[1]`, `body/fig[1]/caption[1]/p[1]`
- `body/supplementary-material[1]/label[1]`, `body/supplementary-material[1]/caption[1]/p[1]`

Each extracted unit gets a stable ID `P00001`..`Pnnnnn`, and every sentence receives `P00001.S001`..`P00001.Smmm`.

## Independent Raw Census (Preventing Silent Drops)
Never compute coverage as `translated / extracted_by_subset`. `xml_pdf_extract.py` runs an independent raw walk counting every text-bearing `<article-title>`, `<title>`, `<label>`, `<p>`, and unnested `<caption>`/`<supplementary-material>` node in scope (including `S1–S14 Fig`, `S1 Text`, `S1–S4 Table`).

## Scope Filtering vs Full Context
When `task.md` or `case.json` limits translation to specific IDs (e.g. `g001`, `g002`) or page ranges:
- Only `in_scope: true` units appear in `units[]` and `batches/`.
- Out-of-scope body/Methods paragraphs stay available in `all_units_context` and `glossary.json` (`context_disambiguations`) so statistical/domain distinctions (like `credible interval` vs `confidence interval`) remain visible during translation.
