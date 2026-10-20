# Held-out scorer fixes

Log of changes to `evals/score_heldout.py` (and the matching rule in
`evals/score_agent.py`) made after the first held-out run. Rule for this log:
the scorer is changed only where it is objectively wrong (wrong tag, wrong
period, wrong unit), with the evidence below. The agent prompt and
`DEFINITIONS.md` were **not** changed on the basis of held-out results.

Both scores are measured on the **same agent outputs** (`--rescore` re-reads
them from the results file; no new API calls):

| | Score | File |
| --- | --- | --- |
| Raw (first run, 2026-10-19) | 9/15 = 60%, 1 unscored | `evals/heldout_results_raw.csv` |
| Corrected scorer | 12/16 = 75%, 0 unscored | `evals/heldout_results.csv` |

## Fix 1: current debt tagged as `DebtCurrent` was dropped (wrong tag)

**Ticker:** PYPL (total_debt, net_debt)

**Evidence:** the 10-Q balance sheet (June 30, 2026, $ millions) shows
"Short-term debt 2,505" and "Long-term debt 10,895". XBRL tags them,
dimensionless, as `DebtCurrent` = 2,505 and `LongTermDebtNoncurrent` = 10,895.
`xbrl_check.total_debt()`, which the scorer reused, only adds `DebtCurrent`
when there is no `LongTermDebtCurrent`/`LongTermDebtNoncurrent`. So the
expected total_debt came out as 10,895, leaving out a balance-sheet debt line.

**Fix:** total_debt = current debt + non-current debt. Current debt is
`DebtCurrent` when tagged (used as is, since it already includes short-term
borrowings), otherwise the current-portion tag plus separately tagged
`ShortTermBorrowings` / `CommercialPaper`.

**Effect:** PYPL total_debt expected 10,895 → 13,400 and net_debt 2,589 →
5,094. Both now match the agent.

## Fix 2: `LongTermDebt` is principal, not carrying value, at GPN (wrong tag)

**Ticker:** GPN (total_debt, net_debt, debt_due_within_12m)

**Evidence:** GPN tags the balance-sheet lines "Current portion of long-term
debt 925,032" and "Long-term debt 21,493,294" ($ thousands) as
`LongTermDebtAndCapitalLeaseObligationsCurrent` = 925.032m and
`LongTermDebtAndCapitalLeaseObligations` = 21,493.294m (total
`LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities` = 22,418.326m,
which equals the debt note's "Total long-term debt 22,418,326"). The scorer
didn't know these tags and fell back to `LongTermDebt` = 22,515.505m. That
figure is exactly the sum of GPN's principal-maturity facts
(`LongTermDebtMaturitiesRepaymentsOfPrincipal…`: 35.872 + 1,377.552 + 3,725.732
+ 3,250.130 + 4,962.467 + 3,663.752 + 5,500.000 = 22,515.505), so it is face
value, not the carrying value on the balance sheet that DEFINITIONS.md uses.

**Fix:** accept `LongTermDebtAndCapitalLeaseObligationsCurrent` and
`LongTermDebtAndCapitalLeaseObligations` as the current and non-current debt
tags (after `LongTermDebtCurrent` / `LongTermDebtNoncurrent`). XBRL facts for
the held-out set are now cached as `xbrl_facts_heldout.json`, so the tuned
set's `xbrl_facts.json` stays untouched.

**Effect:** GPN total_debt expected 22,516 → 22,418, net_debt 17,107 → 17,009.
debt_due_within_12m goes from unscored ("no current-debt tag") to scored, at
925. All three are still misses. The rest of the gap is the settlement lines
of credit (see the misses table below), which is a definition question, not a
scorer error.

## Fix 3: expected rounded to $m but agent value not (wrong unit precision)

**Ticker:** GPN (cash_and_equivalents)

**Evidence:** the balance sheet is in thousands: "Cash and cash equivalents
$5,408,962". The agent returned 5,408.962 ($m, correct). XBRL
`CashAndCashEquivalentsAtCarryingValue` = 5,408.962m, which the scorer rounded
to 5,409 and then compared exactly with the unrounded 5,408.962. The numbers
are the same.

**Fix:** `compare()` in `evals/score_agent.py` compares both values in whole
$ millions (`round(e) == round(g)`), the same rule `evals/xbrl_check.py`
already uses. It has no effect on the tuned set, where every value is a whole
number.

**Effect:** GPN cash miss → exact.

## Not fixed (scorer is not objectively wrong)

| Ticker | Field | Agent | XBRL expected | Why left as a miss |
| --- | --- | --- | --- | --- |
| ALLY | debt_due_within_12m | not_disclosed | 9,060 (`LongTermDebtCurrent` 3,144 + `ShortTermBorrowings` 5,916) | Definition ambiguity. ALLY's balance sheet has only "Short-term borrowings 5,916" and "Long-term debt 17,053", with no current-portion line, so by DEFINITIONS.md's balance-sheet rule the field is not_disclosed. But DEFINITIONS.md also names `LongTermDebtCurrent` as the XBRL equivalent, and ALLY tags it (3,144, from its debt note). The two parts of the rule disagree here. |
| GPN | total_debt | 23,555.09 | 22,418 | Definition ambiguity. The agent added "Settlement lines of credit 1,136,764", a separate balance-sheet line. GPN calls these "net borrowings under our settlement lines of credit", restricted to funding settlement. DEFINITIONS.md says short-term borrowings + long-term debt and doesn't say whether settlement-funding lines count. XBRL doesn't tag them as debt. |
| GPN | net_debt | 18,146.13 | 17,009 | Follows from total_debt. |
| GPN | debt_due_within_12m | 2,061.80 | 925 | Same settlement-lines question (925.032 + 1,136.764). |

## Follow-up: same fix in `evals/xbrl_check.py`

`xbrl_check.total_debt()`, which `verify_key.py` uses for the tuned answer
key, had the Fix 1 logic too. It now uses `DebtCurrent` + `LongTermDebtNoncurrent`
when both are tagged (PYPL: 13,400). Re-running `verify_key` on the tuned key
changed nothing: 0 of 20 rows differ in any column of
`verification_results.csv` (status, xbrl_check or otherwise), and
`answer_key_liquidity.csv` is byte-identical. It still doesn't know the Fix 2
tags; none of the tuned tickers uses them.
