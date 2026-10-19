# Liquidity field definitions

v1 measures near-term liquidity: cash, debt, committed backup facilities and the next maturity.

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

## Fields

| Field | Rule |
| --- | --- |
| `cash_and_equivalents` | Cash and cash equivalents as reported. |
| `total_debt` | Short-term borrowings + long-term debt. **Exclude customer deposits.** |
| `net_debt` | `total_debt` − `cash_and_equivalents`. Show the math in `notes`. |
| `revolver_size` | Committed bank credit facilities named as such in the filing; exclude secured/securitization facilities and subsidiary credit lines. |
| `revolver_undrawn` | Size minus amounts drawn, for the same facilities as `revolver_size`. Use the filing's stated available amount if given. Subtract commercial paper outstanding only if the filing explicitly says it reduces availability. |
| `revolver_*` (both) | If a facility isn't mentioned, use `not_disclosed`. If the filing explicitly says there is no facility, use `0`. |
| `nearest_maturity_date` | Year of the earliest annual long-term debt maturity bucket disclosed (`YYYY`). |
| `nearest_maturity_amount` | Amount in that bucket, in $ millions. `as_of` is the date of the filing that gives the schedule. |

## Out of scope for v1

- `net_leverage`: banks don't report debt/EBITDA-style leverage, so it would be
  mostly `not_disclosed`.
- `covenant_terms`: free text, hard to score objectively.
- `buyback_remaining`: the data layer doesn't extract 10-Q Part II, Item 2, where
  remaining repurchase authorization is disclosed.
