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
"What's JPMorgan's liquidity?"
        │  ask.py: router picks a skill (skills/*/SKILL.md)
        ▼
  agents/agentic.py ──tool calls──► resolve_ticker · fetch_filings · list_sections ·
        │                           read_section · search_filing · get_xbrl_facts · calculate
        ▼                                  │
  4 fields + quote + source + confidence   ▼
  (log: runs/{ticker}_{ts}.json)     SEC EDGAR ──► data_layer ──► data/cache/{ticker}/{form}_{date}/

             DEFINITIONS.md ──► agents/liquidity.py (v1, single call) ──► JSON per field + citation
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

**Tool-using agent** ([`agents/agentic.py`](agents/agentic.py),
[`agents/tools.py`](agents/tools.py)). `ask.py` takes a question in plain
English. A router call picks the skill and names the company. The agent loop
then works with seven tools:
- `resolve_ticker`: name or ticker to CIK; fails for non-10-K/10-Q filers
- `fetch_filings`: download and cache if missing
- `list_sections` / `read_section`: the data layer's extracted sections
- `search_filing`: keyword search across the whole filing when a section is
  missing
- `get_xbrl_facts`: the filing's tagged values
- `calculate`: exact arithmetic

It has at most 15 tool calls and ends with a `submit_answer` call. Confidence
isn't taken from the model. The agent names the XBRL tags its value should
equal, and the pipeline recomputes them from the filing's real facts: high if
they agree, medium if there's no XBRL fact, low if they conflict.

**Skills** ([`skills/`](skills)). Each skill is a `SKILL.md` with front matter
(name, description, fields, unit) and instructions. The router reads the
descriptions and the agent loads the chosen skill's body as its rules, so a
new skill (FX next) is a new folder with no code change.
[`skills/liquidity/SKILL.md`](skills/liquidity/SKILL.md) holds the field
rules copied verbatim from DEFINITIONS.md, plus which tools to use when. The
leak check fails if the two drift apart.

**v1 agent** ([`agents/liquidity.py`](agents/liquidity.py)). Given a ticker, it
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

From [`evals/RESULTS.md`](evals/RESULTS.md) (filings for the period ending
June 30, 2026). The v1 single-call agent was run on 2026-10-19 and the
tool-using agent on 2026-10-20:

| Metric | Result |
| --- | --- |
| v1: tuned set (AXP, COF, SYF, V, MA) | **20/20 = 100%**, all exact |
| v1: held-out (PYPL, ALLY, FIS, GPN), raw scorer | **9/15 = 60%**, 1 unscored |
| v1: held-out, corrected scorer | **12/16 = 75%**, 0 unscored |
| Consistency | **20/20 fields identical across 3 runs** (value and as_of) |
| Leak check | **PASS** (static, runtime, rules) |
| Citations, tuned (17 found values) | **direct 8/17 = 47%, traceable 17/17 = 100%** |
| Citations, held-out (15 found values) | **direct 5/15 = 33%, traceable 15/15 = 100%** |
| Tool-using agent: tuned set | **20/20 = 100%** |
| Tool-using agent: held-out | **15/16 = 94%** |
| Tool-using agent: new tickers (JPM, AAPL, KO, F, DAL) | **10/13 = 77%**, 7 unscored. The 3 misses are a finance-lease ambiguity (DAL, V2 backlog); see [RESULTS.md](evals/RESULTS.md#tool-using-agent-featureagentic-2026-10-20) |
| Ablation: text only vs with XBRL | **Identical scores and values** on all 56 fields; XBRL adds confidence, not accuracy. See [RESULTS.md](evals/RESULTS.md#ablation-text-only-vs-with-xbrl-2026-10-20) |

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

## Quick start

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

Fill in `ANTHROPIC_API_KEY` and `SEC_USER_AGENT` in `.env`, then check that
the Claude API and EDGAR are reachable:

```bash
.venv/bin/python test_setup.py
```

Ask about any US company that files 10-K / 10-Q reports. Filings are fetched
and cached on first use:

```bash
.venv/bin/python ask.py "What's Coca-Cola's liquidity?"
```

The agent picks a skill (here `liquidity`), resolves "Coca-Cola" to KO, and
works through the tools (at most 15 calls). Real output from 2026-10-20:

```
COCA COLA CO (KO), skill: liquidity, 9 tool calls

cash_and_equivalents: 12,907 (USD millions)  as of 2026-07-03  [high]
    confidence: XBRL CashAndCashEquivalentsAtCarryingValue = 12,907.000 agrees
    source: KO_10Q_2026-07-29_balance_sheet_01
    quote:  | Cash and cash equivalents | $12,907 | $10,270 |
    notes:  Excludes short-term investments ($622m) and marketable securities.
total_debt: 43,543 (USD millions)  as of 2026-07-03  [high]
    confidence: XBRL NotesAndLoansPayable + LongTermDebtAndCapitalLeaseObligationsCurrent + LongTermDebtAndCapitalLeaseObligations = 43,543.000 agrees
    source: KO_10Q_2026-07-29_balance_sheet_01
    quote:  | Loans and notes payable | 48 | 1,551 | [...] | Current maturities of long-term debt | 6,494 | 1,822 | [...] | Long-term debt | 37,001 | 42,119 |
    notes:  48 + 6,494 + 37,001 = 43,543. Excludes debt classified within liabilities held for sale (...)
net_debt: 30,636 (USD millions)  as of 2026-07-03  [high]
    confidence: XBRL NotesAndLoansPayable + ... - CashAndCashEquivalentsAtCarryingValue = 30,636.000 agrees
    source: KO_10Q_2026-07-29_balance_sheet_01
    quote:  n/a - derived
    notes:  total_debt 43,543 − cash and cash equivalents 12,907 = 30,636.
debt_due_within_12m: 6,542 (USD millions)  as of 2026-07-03  [high]
    confidence: XBRL NotesAndLoansPayable + LongTermDebtAndCapitalLeaseObligationsCurrent = 6,542.000 agrees
    source: KO_10Q_2026-07-29_balance_sheet_01
    quote:  | Loans and notes payable | 48 | 1,551 | [...] | Current maturities of long-term debt | 6,494 | 1,822 |
    notes:  Current maturities of LTD 6,494 + separate short-term line Loans and notes payable 48 (counted in total_debt) = 6,542.

Run log: runs/KO_20261020T125000.json
```

Each field gets a confidence, computed by the pipeline from the filing's own
XBRL facts rather than taken from the model:

| Confidence | Meaning | Example |
| --- | --- | --- |
| **high** | The text value equals the XBRL tags the agent named, recomputed from the real facts | Every KO field above |
| **medium** | Text only (no matching XBRL fact), or not_disclosed | JPM `total_debt: 532,953 [medium]`, "text only: no matching XBRL fact" (JPM tags no long-term debt total) |
| **low** | Text and XBRL conflict, or the named tags don't exist; the notes explain | None in the evals so far (46 high, 10 medium, 0 low across 14 tickers) |

Add `--json` for machine-readable output. Every tool call, with its input,
output and the model's reasoning summary, is logged to
`runs/{ticker}_{timestamp}.json` (not committed) for replaying a run.

To run the v1 single-call agent instead (reads the cached sections directly,
no tools):

```bash
.venv/bin/python check_data.py
```

```bash
.venv/bin/python -m agents.liquidity AXP
```

Evals (each writes a CSV in `evals/`):

| Command | What it does |
| --- | --- |
| `.venv/bin/python -m evals.score_agent` | Runs the agent on the 5 tuned tickers and scores it against the answer key (`--agentic` for the tool-using agent, `--no-xbrl` for its text-only ablation; `--rescore` re-scores saved outputs without API calls) |
| `.venv/bin/python -m data_layer.fetch PYPL ALLY FIS GPN` | Caches the held-out filings |
| `.venv/bin/python -m evals.score_heldout` | Runs the agent on the held-out tickers (or any tickers given) and scores it against XBRL (`--agentic`, `--no-xbrl`, `--rescore`, `--out=FILE.csv`) |
| `.venv/bin/python -m evals.consistency 3` | Runs the agent 3 times per ticker and reports any value that changes |
| `.venv/bin/python -m evals.leak_check` | Checks the agent can't read the answer key (`--agentic`: nothing under `evals/` at all, and SKILL.md rules match DEFINITIONS.md) |
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
- **Finance leases in debt (DAL).** Proposed: include finance lease
  liabilities in debt; exclude operating leases.
- **Settlement lines of credit (GPN).** Proposed: exclude credit lines
  restricted to funding merchant settlement from debt.
- **Current-debt precedence for banks (ALLY).** Proposed: when the balance
  sheet has no current-debt line, the field is `not_disclosed` even if a note
  or an XBRL tag gives a current portion.
- **Data-layer labeling.** Fallback excerpts can carry a wrong location label,
  and text-only tables are flattened to one line per row.

The definition changes need a fresh ticker set to test, since the nine
held-out and new tickers have now been seen.
