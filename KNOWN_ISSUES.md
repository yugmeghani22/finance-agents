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

## V2 backlog

Definition questions the held-out test (PYPL, ALLY, FIS, GPN) and the
new-ticker test (JPM, AAPL, KO, F, DAL) turned up. They
are deliberately **not** changed in v1: DEFINITIONS.md was not edited on the
basis of held-out or new-ticker results, and those nine tickers now count as
seen. Any change
below needs a **fresh held-out set** (tickers not used for v1 or this
backlog) to measure it.

### Settlement lines of credit (GPN)

**Question:** do borrowings under credit lines restricted to funding merchant
settlement count toward `total_debt` and `debt_due_within_12m`?

**Case:** GPN's 10-Q balance sheet has a separate "Settlement lines of credit"
line ($1,136.8m at June 30, 2026) next to the current portion of long-term
debt and long-term debt. The agent counted it, which gives total_debt 23,555
against XBRL's 22,418 (and debt_due_within_12m 2,062 against 925). XBRL doesn't
tag the line as debt. DEFINITIONS.md ("short-term borrowings + long-term
debt") doesn't say either way.

**Proposed:** exclude them from debt. They fund settlement timing (GPN: "affected
primarily by timing of month-end and transaction volume") and sit with
settlement obligations rather than corporate funding. Add to the
`total_debt` rule: "Exclude settlement lines of credit and other borrowings
restricted to funding settlement."

### Finance leases in debt (DAL)

**Question:** do finance lease liabilities count toward `total_debt` and
`debt_due_within_12m`?

**Case:** DAL's 10-Q balance sheet (June 30, 2026) combines them: "Current
maturities of debt and finance leases" 3,324 and "Debt and finance leases"
9,527 (total 12,851). The debt note's total excluding finance leases is 12,105
(XBRL `LongTermDebtCurrent` 2,981 + `LongTermDebtNoncurrent` 9,124), a $746m
difference. The tool-using agent took the balance-sheet lines, as the
current-debt rule says ("under whatever name the filer uses"). The XBRL scorer
used the debt-only tags, so all three DAL debt fields score as misses.
DEFINITIONS.md doesn't mention leases. In the tuned key, COF's long-term debt
already includes $20m of finance leases.

**Proposed:** include finance leases in debt. They are contractual,
interest-bearing obligations, many filers present them on the same lines, and
it matches the balance-sheet-line rule. Operating leases stay excluded. Add to
the `total_debt` rule: "Include finance lease liabilities; exclude operating
lease liabilities." Then have the XBRL scorer use
`LongTermDebtAndCapitalLeaseObligations*` tags when the balance sheet
presents debt and finance leases together.

**Test on a fresh ticker set.** DAL and the other new tickers (JPM, AAPL, KO,
F) have now been seen, like the held-out four.

### Current-debt precedence for banks (ALLY)

**Question:** when the balance sheet has no current-portion line but XBRL tags
`LongTermDebtCurrent` (from a debt note), which wins?

**Case:** ALLY's balance sheet shows only "Short-term borrowings" (5,916) and
"Long-term debt" (17,053), yet it tags `LongTermDebtCurrent` = 3,144. The agent
followed the balance-sheet rule (`not_disclosed`). The XBRL scorer, following
the tag DEFINITIONS.md names, expected 9,060. The rule's two parts disagree.

**Proposed:** the bank rule wins. A balance sheet with no current-debt line means
`not_disclosed`, even if a note or an XBRL tag gives a current portion. Extend
the **Banks** sentence to say so and to cover bank holding companies beyond
AXP/COF/SYF (ALLY). Have the XBRL scorer use `LongTermDebtCurrent` /
`DebtCurrent` only when the tag is presented on the balance sheet.
