---
name: liquidity
description: Near-term liquidity of a US public company from its latest 10-Q / 10-K - cash and cash equivalents, total debt, net debt and debt due within 12 months. Use for questions about a company's liquidity, cash position, debt load, net debt or near-term debt maturities.
fields: [cash_and_equivalents, total_debt, net_debt, debt_due_within_12m]
unit: USD millions
form: 10-Q
---

# Liquidity skill

Report the four fields below for one company, each with its value, as_of date,
a word-for-word quote from the filing and the chunk id it came from. The field
rules are binding. They are copied verbatim from `evals/DEFINITIONS.md`
(`evals/leak_check.py` fails if the two drift apart).

## Field rules

<!-- BEGIN DEFINITIONS -->
### General rules

- **Units.** Money values are in **$ millions**. When the source states a figure in
  billions (for example "$2.0 billion" or a table headed "(Billions)"), convert it to
  millions and say in `notes` that the source was rounded (for example "source in
  $bn, rounded to $0.1bn = ±$50m").
- **Missing values.** If a field isn't disclosed in the filing, write
  `not_disclosed`. Never estimate.
- **Evidence.** `source_chunk_id` is the chunk the value came from, and
  `evidence_quote` is the exact sentence or table row that supports it, copied word
  for word (about 30 words at most). Derived values show their math in `notes`.
- **as_of.** The balance-sheet date of the filing the value comes from. Use the
  latest filing that discloses the field. Where that is the 10-K rather than the
  10-Q, `as_of` is the 10-K's date.
- **Balance sheet values.** Balance sheet values (cash, debt) come from the
  latest 10-Q. All inputs to a calculated field (such as `net_debt`) must share
  the same `as_of`.
- **Exact over rounded.** When the text gives a rounded figure (for example
  "$59.0 billion") and XBRL from the same filing gives an exact figure, use the
  exact figure.

### Fields

| Field | Rule |
| --- | --- |
| `cash_and_equivalents` | Cash and cash equivalents as reported. **Excludes restricted cash.** |
| `total_debt` | Short-term borrowings + long-term debt. **Exclude customer deposits.** |
| `net_debt` | `total_debt` − `cash_and_equivalents`. Show the math in `notes`. |
| `debt_due_within_12m` | Debt due within 12 months, from the latest 10-Q balance sheet, with the same `as_of` as `cash_and_equivalents`. Take the balance sheet's current-debt line: the current portion of long-term debt, under whatever name the filer uses (for example "Current maturities of debt" or "Short-term debt"; XBRL `LongTermDebtCurrent` or `DebtCurrent`). If that line already includes short-term borrowings or commercial paper, use it as is. Add short-term borrowings only when they are a separate balance-sheet line that `total_debt` counts. If the balance sheet doesn't split out a current portion of long-term debt (an unclassified balance sheet, as banks usually present), use `not_disclosed`; don't rebuild it from maturity schedules or notes. Must not exceed `total_debt`. **Banks:** AXP, COF and SYF present unclassified balance sheets that don't split current vs long-term debt, so this field is `not_disclosed` for them and is not reconstructed from other disclosures. |

### Out of scope

- `net_leverage`: banks don't report debt/EBITDA-style leverage, so it would be
  mostly `not_disclosed`.
- `covenant_terms`: free text, hard to score objectively.
- `buyback_remaining`: the data layer doesn't extract 10-Q Part II, Item 2, where
  remaining repurchase authorization is disclosed.
- `revolver_size`, `revolver_undrawn`: revolvers don't apply to bank funding
  models and are rarely XBRL-tagged.
- `nearest_maturity_date`, `nearest_maturity_amount`: annual maturity buckets go
  stale between filings.
<!-- END DEFINITIONS -->

## How to work

1. **Resolve and fetch.** `resolve_ticker` with the company the user named,
   then `fetch_filings` for that ticker. If `resolve_ticker` fails (no match,
   or not a 10-K / 10-Q filer), stop and submit every field as
   `not_disclosed`, with the reason in `notes`.
2. **Balance sheet first.** `list_sections` for the 10-Q, then `read_section`
   `balance_sheet`. Cash, the current-debt line and the debt lines usually
   all come from here.
3. **Debt detail.** Read the `debt` section (debt footnote) when the balance
   sheet doesn't split debt into short-term and long-term lines, or to check
   what a line includes (commercial paper, finance leases, securitized debt).
   Read `liquidity` (MD&A) only if you still need it.
4. **Search when a section is missing.** If a section is `missing`, or doesn't
   hold the line you need, use `search_filing` with the line's likely wording
   (for example "current portion of long-term debt", "short-term borrowings",
   "commercial paper"). Cite the returned block id.
5. **Cross-check with XBRL.** `get_xbrl_facts` on the same filing: cash as
   `CashAndCashEquivalentsAtCarryingValue` (a tag named
   `...RestrictedCash...` includes restricted cash, so it isn't the same
   figure), and debt with `keyword` "Debt" or "Borrowings". XBRL is only a
   cross-check. Every value you report must be supported by a quote from the
   filing text, except where the rules say to prefer an exact XBRL figure
   over a rounded one in the text.
6. **Calculate, don't do mental math.** Use `calculate` for every sum or
   difference (total_debt from its lines, net_debt, any exclusion).
7. **Submit.** Call `submit_answer` once, with one entry per field.

## Reporting rules

- **Never guess.** If the filing doesn't disclose a field under these rules,
  report `not_disclosed`. That is a correct answer, not a failure.
- `evidence_quote` is copied word for word from the cited chunk or block (a
  table row is fine). To quote two rows or sentences, join them with " [...] ".
  For a derived value such as net_debt, write "n/a - derived" and put the math
  in `notes`.
- `source_chunk_id`: the chunk or block id the quote came from. For a derived
  value, list the input ids separated by "; ".
- `xbrl_expression`: the XBRL tag(s) whose value should equal the reported
  value, combined with + and -, using exact tag names returned by
  `get_xbrl_facts` (for example "LongTermDebtCurrent + LongTermDebtNoncurrent"
  or "CashAndCashEquivalentsAtCarryingValue"). Leave it empty if XBRL has no
  matching fact. The pipeline recomputes it from the real facts and sets
  confidence: high when it equals your value, medium when it's empty (text
  only), low when it differs. When XBRL disagrees with the text, keep the
  value the rules call for, give the XBRL expression anyway, and explain the
  difference in `notes`.
- You have at most 15 tool calls before `submit_answer`. Read what you need
  and no more.
