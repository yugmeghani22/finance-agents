# Liquidity field definitions

v1 measures near-term liquidity: cash, debt, net debt and debt due within 12 months.

These rules apply to `answer_key_liquidity.csv` and to any agent that extracts the
same fields. When the answer key and an agent disagree, check both against this file
first.

## General rules

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

## Fields

| Field | Rule |
| --- | --- |
| `cash_and_equivalents` | Cash and cash equivalents as reported. **Excludes restricted cash.** |
| `total_debt` | Short-term borrowings + long-term debt. **Exclude customer deposits.** |
| `net_debt` | `total_debt` − `cash_and_equivalents`. Show the math in `notes`. |
| `debt_due_within_12m` | Debt due within 12 months, from the latest 10-Q balance sheet, with the same `as_of` as `cash_and_equivalents`. Take the balance sheet's current-debt line: the current portion of long-term debt, under whatever name the filer uses (for example "Current maturities of debt" or "Short-term debt"; XBRL `LongTermDebtCurrent` or `DebtCurrent`). If that line already includes short-term borrowings or commercial paper, use it as is. Add short-term borrowings only when they are a separate balance-sheet line that `total_debt` counts. If the balance sheet doesn't split out a current portion of long-term debt (an unclassified balance sheet, as banks usually present), use `not_disclosed`; don't rebuild it from maturity schedules or notes. Must not exceed `total_debt`. |

## Out of scope for v1

- `net_leverage`: banks don't report debt/EBITDA-style leverage, so it would be
  mostly `not_disclosed`.
- `covenant_terms`: free text, hard to score objectively.
- `buyback_remaining`: the data layer doesn't extract 10-Q Part II, Item 2, where
  remaining repurchase authorization is disclosed.
- `revolver_size`, `revolver_undrawn`: revolvers don't apply to bank funding
  models and are rarely XBRL-tagged.
- `nearest_maturity_date`, `nearest_maturity_amount`: annual maturity buckets go
  stale between filings.
