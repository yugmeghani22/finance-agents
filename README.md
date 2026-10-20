# finance-agents

An LLM agent that reads SEC 10-Q / 10-K filings and extracts a company's
near-term liquidity position (cash, total debt, net debt and debt due within
12 months), with a citation to the exact filing passage for every number. It
is measured against a hand-verified answer key for five card issuers and
networks (AXP, COF, SYF, V, MA). It is also tested on four held-out companies
(PYPL, ALLY, FIS, GPN), scored automatically against each filing's own XBRL
facts. Field rules live in one file, [`evals/DEFINITIONS.md`](evals/DEFINITIONS.md),
which the agent and the answer key both follow.

## How it works

```
SEC EDGAR ──► data_layer ──► data/cache/{ticker}/{form}_{date}/   (sections, chunked)
                                   │
                                   ▼
             DEFINITIONS.md ──► agents/liquidity.py ──► JSON per field + citation
                                   │
                                   ▼
        evals/answer_key_liquidity.csv ◄── verify_key.py + xbrl_check.py ◄── review app
                                   │
                                   ▼
     score_agent.py (tuned key)   score_heldout.py (XBRL)   consistency.py   leak_check.py
```

**Data layer** ([`data_layer/`](data_layer)). Downloads the latest 10-K and
10-Q per ticker with [edgartools](https://github.com/dgunning/edgartools) and
extracts the sections the agents need: MD&A, the liquidity subsection, market
risk, the debt and derivatives footnotes, and the consolidated balance sheet.
SEC print-layout tables are rebuilt into markdown tables
([`html_text.py`](data_layer/html_text.py)), so numbers stay in the right
columns. A section the filing lacks is recorded as missing with a reason, or
assembled from tagged fallback excerpts, never silently filled. Sections are
chunked and cached with stable chunk ids (`AXP_10Q_2026-07-24_balance_sheet_01`)
that citations point to.

**Agent** ([`agents/liquidity.py`](agents/liquidity.py)). Given a ticker, it
loads only that ticker's balance sheet, liquidity and debt chunks and calls
Claude (`claude-opus-5-5`) with DEFINITIONS.md as the rules and a JSON schema
for the output. Each field comes back with `value` ($ millions), `unit`,
`as_of`, `source_chunk_id`, `evidence_quote` (verbatim), `notes` (the math for
sums and derived values) and `status` (`found` / `not_disclosed`).

**Verifier** ([`evals/verify_key.py`](evals/verify_key.py)). Deterministic
checks (no LLM) on the drafted answer key:
- the quote is in the cited chunk and in the original filing
- the value appears in the quote after unit conversion
- `net_debt = total_debt − cash`, and debt due within 12 months ≤ total debt
- `as_of` equals the filing's period of report

Rows go to a [Streamlit review page](app/review.py) for approve / correct
decisions. That page links each quote to its place in the SEC filing.

**XBRL cross-check** ([`evals/xbrl_check.py`](evals/xbrl_check.py)). Compares
every value with the filing's own dimensionless XBRL facts at the same
balance-sheet date, for example `CashAndCashEquivalentsAtCarryingValue`, or
short-term borrowings + long-term debt. Rows that XBRL confirms are marked
`AUTO_VERIFIED`; mismatches are left for a human. The held-out scorer
([`evals/score_heldout.py`](evals/score_heldout.py)) uses the same facts as
ground truth for tickers that have no hand-built key.

## Results

From [`evals/RESULTS.md`](evals/RESULTS.md) (run 2026-10-19, filings for the
period ending June 30, 2026):

| Metric | Result |
| --- | --- |
| Tuned set (AXP, COF, SYF, V, MA) | **20/20 = 100%**, all exact |
| Held-out (PYPL, ALLY, FIS, GPN), raw scorer | **9/15 = 60%**, 1 unscored |
| Held-out, corrected scorer | **12/16 = 75%**, 0 unscored |
| Consistency | **20/20 fields identical across 3 runs** (value and as_of) |
| Leak check | **PASS** (static, runtime, rules) |
| Citations, tuned (17 found values) | **direct 8/17 = 47%, traceable 17/17 = 100%** |
| Citations, held-out (15 found values) | **direct 5/15 = 33%, traceable 15/15 = 100%** |

- **Held-out scores.** "Corrected" fixes three scorer errors (wrong XBRL tags,
  rounding precision), each with evidence in
  [`evals/SCORER_FIXES.md`](evals/SCORER_FIXES.md), measured on the same agent
  outputs. Neither the agent prompt nor DEFINITIONS.md was changed in response
  to held-out results.
- **Remaining misses.** All four held-out misses are open definition questions,
  not arithmetic errors.
- **Citations.** "Direct" means the number appears in the agent's quote.
  "Traceable" means it equals a sum or difference of quoted numbers (or, for
  net_debt, of the other fields).

## How to run

Requires Python 3.10+, an [Anthropic API key](https://console.anthropic.com/)
and an SEC user agent (`"Your Name your.email@example.com"`, as
[SEC requires](https://www.sec.gov/os/accessing-edgar-data)).

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Fill in `ANTHROPIC_API_KEY` and `SEC_USER_AGENT` in `.env`, then:

```bash
.venv/bin/python test_setup.py
```

```bash
.venv/bin/python check_data.py
```

```bash
.venv/bin/python -m agents.liquidity AXP
```

`test_setup.py` checks that the Claude API and EDGAR are reachable.
`check_data.py` downloads and caches the filings into `data/cache/` (not
committed). The last command runs the agent on one ticker.

Evals (each writes a CSV in `evals/`):

| Command | What it does |
| --- | --- |
| `.venv/bin/python -m evals.score_agent` | Runs the agent on the 5 tuned tickers and scores it against the answer key (`--rescore` re-scores saved outputs without API calls) |
| `.venv/bin/python -m data_layer.fetch PYPL ALLY FIS GPN` | Caches the held-out filings |
| `.venv/bin/python -m evals.score_heldout` | Runs the agent on the held-out tickers and scores it against XBRL (`--rescore` likewise) |
| `.venv/bin/python -m evals.consistency 3` | Runs the agent 3 times per ticker and reports any value that changes |
| `.venv/bin/python -m evals.leak_check` | Checks the agent can't read the answer key |
| `.venv/bin/python -m evals.verify_key` | Runs the deterministic checks and XBRL cross-check on the answer key |
| `.venv/bin/streamlit run app/review.py` | Opens the review page for the answer key |

## Scope

**V1 fields:** `cash_and_equivalents` (excluding restricted cash),
`total_debt` (short-term borrowings + long-term debt, excluding customer
deposits), `net_debt`, `debt_due_within_12m` (the balance sheet's current-debt
line; `not_disclosed` for unclassified bank balance sheets). Values are in
$ millions from the latest 10-Q.

**Out of scope for V1:** net leverage, covenant terms, buyback authorization,
revolver size, and nearest maturities. See DEFINITIONS.md for why.

**V2 backlog** ([`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)):
- **Settlement lines of credit (GPN).** Proposed: exclude credit lines
  restricted to funding merchant settlement from debt.
- **Current-debt precedence for banks (ALLY).** Proposed: when the balance
  sheet has no current-debt line, the field is `not_disclosed` even if a note
  or an XBRL tag gives a current portion.
- **Data-layer labeling.** Fallback excerpts can carry a wrong location label,
  and text-only tables are flattened to one line per row.

Both definition changes need a fresh held-out set to test, since the current
four held-out tickers have now been seen.
