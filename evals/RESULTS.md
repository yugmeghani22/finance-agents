# Liquidity agent v1: eval results

Agent: `agents/liquidity.py` (`claude-opus-5-5`, effort high), with
`evals/DEFINITIONS.md` as the rules. Fields: cash_and_equivalents, total_debt,
net_debt, debt_due_within_12m. All filings are the latest 10-Q (period ending
June 30, 2026). Run date: 2026-10-19.

| Metric | Result | How it's measured |
| --- | --- | --- |
| Tuned set (AXP, COF, SYF, V, MA) | **20/20 = 100%**, all exact | `evals/score_agent.py` vs `answer_key_liquidity.csv` |
| Held-out (PYPL, ALLY, FIS, GPN), raw scorer | **9/15 = 60%**, 1 unscored | `evals/heldout_results_raw.csv` |
| Held-out, corrected scorer | **12/16 = 75%**, 0 unscored | `evals/score_heldout.py --rescore`, same agent outputs; fixes in `SCORER_FIXES.md` |
| Consistency | **20/20 fields identical across 3 runs** (value and as_of); each run 20/20 vs the key | `evals/consistency.py` |
| Leak check | **PASS** (static, runtime, rules) | `evals/leak_check.py` |
| Citation coverage, tuned | **8/12 = 67%** (5 derived values excluded) | `value_in_quote` in `score_agent.py` |
| Citation coverage, held-out | **5/11 = 45%** (5 derived values excluded) | same rule, on the held-out outputs |

## Held-out

The held-out tickers weren't used to write DEFINITIONS.md or the answer key.
Expected values come from each 10-Q's own XBRL facts, built to match
DEFINITIONS.md. Fields XBRL doesn't tag are unscored. Three scorer errors (wrong
tags for PYPL and GPN debt, and rounding precision for GPN's thousands) were
fixed with evidence. The same `DebtCurrent` fix went into
`evals/xbrl_check.py`, and re-running `verify_key` changed no row of the tuned
key's verification. Neither the agent prompt nor DEFINITIONS.md was changed
because of held-out results.

## Leak check

- **Static:** the agent and the data_layer code it imports name no `evals/` path
  except `DEFINITIONS.md` and never import `evals`.
- **Runtime:** an audit hook recorded every file opened during real runs of all
  5 tuned tickers. Under `evals/`, only `DEFINITIONS.md` was opened. Under
  `data/cache/`, each run opened only that ticker's 10-Q folder.
- **Rules:** DEFINITIONS.md contains none of the answer key's values.

## Citation coverage

This is the share of found values whose quote contains the value itself
(read in $m, thousands or billions). Every uncovered value is a sum or
adjustment of lines the agent did quote, with the math in `notes`. In those
cases all the inputs are cited, but not the result:

- Tuned: AXP cash (45,243 − 165 restricted), AXP / V / MA total_debt (short- +
  long-term lines). In an earlier run V and MA quoted the "Total ... debt" row
  instead, so the values were stable across runs but which line the agent
  quotes was not.
- Held-out: PYPL / ALLY / FIS / GPN total_debt and FIS / GPN
  debt_due_within_12m (sums of balance-sheet lines).

## Misses

Tuned set: none.

Held-out, corrected scorer (4):

| Ticker | Field | Agent | Expected (XBRL) | Summary |
| --- | --- | --- | --- | --- |
| ALLY | debt_due_within_12m | not_disclosed | 9,060 | Definition ambiguity: the balance sheet has no current-portion line (the bank rule gives not_disclosed), but XBRL tags `LongTermDebtCurrent` 3,144 from the debt note. |
| GPN | total_debt | 23,555 | 22,418 | Definition ambiguity: the agent counted "Settlement lines of credit" ($1,136.8m); DEFINITIONS.md doesn't say whether settlement-funding lines are debt. |
| GPN | net_debt | 18,146 | 17,009 | Follows from GPN total_debt. |
| GPN | debt_due_within_12m | 2,062 | 925 | Same settlement-lines question (925.0 + 1,136.8). |

Raw-scorer misses that the scorer fixes resolved (2 more, plus GPN cash):

| Ticker | Field | Agent | Raw expected | Summary |
| --- | --- | --- | --- | --- |
| PYPL | total_debt | 13,400 | 10,895 | Scorer dropped `DebtCurrent` 2,505 (balance-sheet "Short-term debt"). |
| PYPL | net_debt | 5,094 | 2,589 | Follows from PYPL total_debt. |
| GPN | cash_and_equivalents | 5,408.962 | 5,409 | Same number; the scorer rounded only the expected value. |

Both open questions are in the KNOWN_ISSUES.md V2 backlog, with proposals.
Testing them needs a fresh held-out set.
