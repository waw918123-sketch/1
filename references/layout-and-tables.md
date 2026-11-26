# Layout Reconstruction & Complex Table Extraction

## 1. Multi-Column Reading Order (`排得对`)

Naive PDF text extractors iterate content streams in PDF drawing order, which frequently interleaves left and right columns or injects running headers mid-sentence.

`scripts/pdf_extract.py` avoids this via a 3-step geometric pipeline:
1. **Header/Footer Suppression**: Edge blocks (`y0 < 9%` or `y1 > 91%` of page height, length `<= 140` chars) whose digit-normalized text repeats across `>= 2` pages are moved to `headers_footers[]` so they never split cross-page paragraphs.
2. **Spanning Band Splitting**: Wide blocks (`width >= 58%` of content width crossing the horizontal midpoint)—such as paper titles, author blocks, abstracts, full-width headings, and double-column captions—act as vertical band dividers.
3. **Column Gutter X-Y Cut**: Within each horizontal band, block `[x0, x1]` intervals are projected onto a 100-bin X-axis occupancy grid. Zero-occupancy interior gaps (`>= 2%` width) partition the band into columns `1, 2, 3`, ordered **left-to-right**, then **top-to-bottom** within each column.

### Cross-Column & Cross-Page Continuity
- When reading `reading_order.md`, verify sentence boundaries at the bottom of `col1` -> top of `col2`, and bottom of page `p` -> top of page `p+1`.
- Keep both block anchors (`p2_b14` and `p2_b15`) when a single sentence spans a column or page break.

## 2. Complex Table Handling (`拆得清`)

Academic PDFs commonly use **three-line tables (booktabs)** with no vertical rules, multi-row headers, merged cells (`colspan`/`rowspan`), and **cross-page continuations** (`Table 2 (continued)`).

`pdf_extract.py` combines:
1. `pdfplumber` bordered-line extraction (`pdfplumber_lines`)
2. `pdfplumber` horizontal-rule + text-column alignment (`pdfplumber_threeline`) when `>= 2` horizontal rules exist
3. `PyMuPDF` `page.find_tables()` as fallback/complement
4. **Unparsed Table Detector**: If `>= 2` wide horizontal vector rules exist in a region not covered by any extracted table, `document.json` logs an `unextracted_table_candidates` entry under `unextracted_regions`.

### What to Do When a Table Is Complex or Unparsed
- Always inspect `tables.md` alongside the surrounding `captions` (`table_caption`) in `document.json`.
- If `continues_from` / `continued_on` is set, inspect both page anchors and note merged vs per-page row provenance.
- If a table is image-based or flagged in `unextracted_regions`, render a 300 DPI crop with `pdf_render_ocr.py --crop "page:x0,y0,x1,y1"` and read the rendered PNG visually rather than guessing column alignment.
- For multi-row headers where `colspan` leaves trailing empty header strings in Markdown, check `tables.json[*].html` (`colspan` attributes) and verify header-to-column ownership visually if ambiguous.
