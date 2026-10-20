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
| Citations, tuned (17 found values) | **direct 8/17 = 47%, traceable 17/17 = 100%** | `citation` in `score_agent.py --rescore` |
| Citations, held-out (15 found values) | **direct 5/15 = 33%, traceable 15/15 = 100%** | same rule, `score_heldout.py --rescore` |

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

## Citations

Each found value is classified (`citation` / `citation_trace` columns in the
results CSVs):

- **Direct:** the value itself appears in its quote, read in $m, thousands or
  billions.
- **Traceable:** the value equals a sum or difference of 2–3 numbers in its own
  quote, or, for a derived value (net_debt, quoted as "n/a - derived"), of the
  ticker's other direct or traceable values.

Direct % = direct / found values. Traceable % = (direct + traceable) / found.

The search is kept narrow on purpose. Any mix of every number in every quote
would find a chance match for almost any value. With this rule, 0 of 2,000
random values traced against a typical quote. Every traced expression agrees
with the math the agent wrote in `notes`:

- **Tuned, not direct (9):** AXP cash 45,243 − 165 restricted; AXP / V / MA
  total_debt as short-term + long-term lines; all five net_debt values as
  total_debt − cash.
- **Held-out, not direct (10):** PYPL / ALLY / FIS / GPN total_debt and FIS /
  GPN debt_due_within_12m as sums of balance-sheet lines (GPN in thousands); all
  four net_debt values as total_debt − cash.

Traceable doesn't mean correct. GPN's total_debt traces exactly (settlement
lines + current portion + long-term debt) and is still a miss, because the
open question is which lines to count, not the arithmetic. Which lines the agent
quotes also varies between runs. In one run V and MA quoted the "Total ...
debt" row (direct), and in another the component lines (traceable), with the
same values.

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
