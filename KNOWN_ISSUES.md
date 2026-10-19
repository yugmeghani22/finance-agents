# Known issues

## 1. Fallback excerpts can carry the wrong location label

**Where:** `fallback_excerpts()` in `data_layer/extract.py`, which tracks the
current heading with `is_heading()`.

**What happens:** when a section has no dedicated footnote, each fallback excerpt
is tagged `[FALLBACK - from <item> > <heading>]` using the last paragraph that
looked like a heading. `is_heading()` accepts any short line that doesn't end in
punctuation, so two kinds of line are mistaken for section headings:

- **Table captions**, such as "Table 17: Summary of Customer Deposits and
  Consolidated Debt". The prose after the table gets labeled with the caption.
- **Rows of tables that weren't rebuilt** (see issue 2), such as "Outlook Stable
  Stable Stable" from the credit-ratings table.

**Example:** AXP 10-Q, debt fallback (`AXP_10Q_2026-07-24_debt_02`):

| Excerpt | Labeled as | Actually under |
| --- | --- | --- |
| "Our funding needs are driven by … funding plan for the full year 2026 …" | Table 17: Summary of Customer Deposits and Consolidated Debt | Summary of Consolidated Debt |
| "We issued $6.1 billion of unsecured debt …" | Table 17: Summary of Customer Deposits and Consolidated Debt | Summary of Consolidated Debt |
| "Our equity capital and funding strategies are designed …" | Table 18: Debt Issuances | Summary of Consolidated Debt |
| "Downgrades in the ratings of our unsecured debt …" | Outlook Stable Stable Stable | Summary of Consolidated Debt |

**Impact:** the label only. The excerpt text, chunk id and section are correct,
so citations still point to the right chunk. A reader or agent that trusts the
label could misdescribe where in the filing a passage sits.

**Possible fix:** in `fallback_excerpts()`, don't treat `^Table \d+` captions as
headings. Also don't treat lines produced from a flattened table as headings
(this falls away once issue 2 is fixed).

**Found:** 2026-10-19, while drafting the AXP answer key.

## 2. Tables with no numbers are flattened to one line per row

**Where:** `table_to_blocks()` in `data_layer/html_text.py` (the "layout table"
branch).

**What happens:** a table whose body has no numeric cells is treated as a layout
table holding prose, and each row becomes its own paragraph. AXP's "Unsecured
Debt Ratings" table (10-Q Table 19, 10-K Table 18) comes out as lines like
"American Express Company Long Term A2 A- A" and "Outlook Stable Stable Stable"
instead of a markdown table.

**Impact:** the ratings are still readable but lose their column headers
(Moody's / S&P / Fitch). This is also the cause of the "Outlook Stable Stable
Stable" label in issue 1.

**Possible fix:** only use the layout-table path when rows have one non-empty
cell. Render text-only grids with two or more columns as markdown tables.
