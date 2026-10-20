"""Score the liquidity agent on held-out tickers, with XBRL as ground truth.

These tickers were not used to write DEFINITIONS.md or the answer key. Each
field's expected value comes from the latest 10-Q's own XBRL facts at its
period of report (dimensionless facts only), built to match DEFINITIONS.md:

  cash_and_equivalents  CashAndCashEquivalentsAtCarryingValue. A filer that
                        only tags cash including restricted cash is unscored,
                        since that tag doesn't match the definition.
  total_debt            current debt + non-current debt, from the tags filers
                        use for those balance-sheet lines (never Liabilities):
                          current      DebtCurrent as is; else LongTermDebtCurrent
                                       or LongTermDebtAndCapitalLeaseObligationsCurrent,
                                       plus separately tagged ShortTermBorrowings /
                                       CommercialPaper (or, if neither is tagged,
                                       NotesAndLoansPayable)
                          non-current  LongTermDebtNoncurrent or
                                       LongTermDebtAndCapitalLeaseObligations
                        A filer with no current / non-current split falls back to
                        evals/xbrl_check.total_debt (LongTermDebt plus short-term
                        tags).
  net_debt              total_debt - cash_and_equivalents, from the two above.
  debt_due_within_12m   the current part above.

See evals/SCORER_FIXES.md for why the debt tags are what they are.

A field with no usable XBRL is "unscored" and left out of the score. Matching
is the same as evals/score_agent.py (exact, or within $0.1bn rounding when the
agent's evidence is in billions). An agent not_disclosed where XBRL tags a
value is a miss.

Run:  .venv/bin/python -m evals.score_heldout [TICKER ...]   (default: PYPL ALLY FIS GPN)
      .venv/bin/python -m evals.score_heldout --rescore
          re-scores the agent outputs already in the results file
          (no API calls), so a scorer change is measured on the same answers
Options: --agentic scores the tool-using agent (agents/agentic.py) and adds
its confidence; --no-xbrl (with --agentic) disables its get_xbrl_facts tool,
the XBRL ablation; --out=NAME.csv writes evals/NAME.csv instead of
evals/heldout_results.csv (heldout_results_agentic.csv with --agentic,
heldout_results_agentic_textonly.csv with --no-xbrl).
"""

import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agents.liquidity import FIELDS
from data_layer.store import filing_dirs, read_manifest
from evals.score_agent import (AGENT_EXTRA, add_citations, agent_rows_from_csv, agent_runner, compare,
                               print_citations, print_confidence)
from evals.xbrl_check import ALL_TAGS, _values, load_facts, total_debt

HERE = Path(__file__).resolve().parent
OUT = HERE / "heldout_results.csv"
HELD_OUT = ["PYPL", "ALLY", "FIS", "GPN"]
CASH_TAG = "CashAndCashEquivalentsAtCarryingValue"
CASH_WITH_RESTRICTED = "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
CURRENT_TAGS = ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"]
NONCURRENT_TAGS = ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"]
SHORT_TERM_TAGS = ["ShortTermBorrowings", "CommercialPaper"]
# Used only when neither short-term tag is present: KO tags its balance-sheet
# "Loans and notes payable" line this way (SCORER_FIXES.md, fix 4). The
# taxonomy lets this tag include non-current notes, hence fallback only.
SHORT_TERM_FALLBACK = "NotesAndLoansPayable"
TAGS = ALL_TAGS | set(CURRENT_TAGS + NONCURRENT_TAGS + [SHORT_TERM_FALLBACK])
FACTS_CACHE = "xbrl_facts_heldout.json"

COLUMNS = ["ticker", "field", "expected", "got", "match", "xbrl_tag", "xbrl_detail", "period",
           "agent_status", "as_of", "source_chunk_id", "citation", "citation_trace", "confidence", "confidence_reason", "xbrl_value",
           "tool_calls", "evidence_quote", "notes", "error", "log_path"]


def period_of_report(folder: Path, accession_no: str) -> str:
    """Cached in filing_meta.json, the same file evals/verify_key.py writes."""
    path = folder / "filing_meta.json"
    if not path.exists():
        from edgar import get_by_accession_number  # noqa: PLC0415 - only on a cache miss

        from data_layer.fetch import _ensure_identity  # noqa: PLC0415 - SEC identity + rate limit
        _ensure_identity()
        filing = get_by_accession_number(accession_no)
        path.write_text(json.dumps({"period_of_report": str(filing.period_of_report)}))
    return json.loads(path.read_text())["period_of_report"]


def one(facts, tag, period) -> tuple[float | None, str]:
    vals = _values(facts, tag, period)
    if len(vals) > 1:
        return None, f"{tag} has several values {vals}"
    return (vals[0], "") if vals else (None, "")


def first(facts, tags, period) -> tuple[float | None, str, str]:
    """(value, tag, problem) for the first of `tags` with a single value."""
    for tag in tags:
        v, why = one(facts, tag, period)
        if why:
            return None, tag, why
        if v is not None:
            return v, tag, ""
    return None, "", ""


def current_debt(facts, period) -> tuple[float | None, str, str]:
    """The balance sheet's current-debt amount: (value, tags, detail)."""
    v, why = one(facts, "DebtCurrent", period)
    if v is not None or why:
        return v, "DebtCurrent", why or f"DebtCurrent {v:,.0f}"
    v, tag, why = first(facts, CURRENT_TAGS, period)
    if v is None:
        return None, "", why or "no current-debt tag"
    parts = {tag: v}
    for t in SHORT_TERM_TAGS:
        sv, _ = one(facts, t, period)
        if sv is not None:
            parts[t] = sv
    if len(parts) == 1:
        sv, _ = one(facts, SHORT_TERM_FALLBACK, period)
        if sv is not None:
            parts[SHORT_TERM_FALLBACK] = sv
    return sum(parts.values()), "+".join(parts), " + ".join(f"{t} {x:,.3f}" for t, x in parts.items())


def debt_total(facts, period) -> tuple[float | None, str, str]:
    current, ctag, cdetail = current_debt(facts, period)
    noncurrent, ntag, why = first(facts, NONCURRENT_TAGS, period)
    if current is not None and noncurrent is not None:
        return current + noncurrent, f"{ctag}+{ntag}", f"{cdetail} + {ntag} {noncurrent:,.3f}"
    if why:
        return None, ntag, why
    return total_debt(facts, period)  # no current / non-current split


def xbrl_truth(facts: list[dict], period: str) -> dict[str, tuple[float | None, str, str]]:
    """field -> (expected value or None if unscored, tag(s) used, detail)."""
    out = {}
    cash, why = one(facts, CASH_TAG, period)
    if cash is None and not why:
        restricted, _ = one(facts, CASH_WITH_RESTRICTED, period)
        why = (f"only {CASH_WITH_RESTRICTED} tagged ({restricted:,.0f}), which includes restricted cash"
               if restricted is not None else "no cash tag")
    out["cash_and_equivalents"] = (cash, CASH_TAG if cash is not None else "",
                                   why or f"{CASH_TAG} {cash:,.3f}")

    debt, debt_tag, debt_detail = debt_total(facts, period)
    out["total_debt"] = (debt, debt_tag, debt_detail)

    if debt is not None and cash is not None:
        out["net_debt"] = (debt - cash, f"total_debt - {CASH_TAG}", f"({debt_detail}) - {cash:,.3f}")
    else:
        out["net_debt"] = (None, "", "needs both total_debt and cash from XBRL")

    out["debt_due_within_12m"] = current_debt(facts, period)
    return out


def score_ticker(ticker: str, client: anthropic.Anthropic | None, saved: dict | None = None,
                 agentic: bool = False, text_only: bool = False) -> list[dict]:
    error = ""
    if saved is not None:
        agent_rows = saved.get(ticker, {})
        error = next((r["error"] for r in agent_rows.values() if r.get("error")), "")
    else:
        # The agent runs before the cache is read: the agentic agent fetches a
        # new ticker's filings itself.
        try:
            agent_rows = {r["field"]: r for r in agent_runner(agentic, text_only)(ticker, client)}
        except Exception as e:  # one ticker failing shouldn't sink the run
            agent_rows, error = {}, f"{type(e).__name__}: {e}"
    if not filing_dirs(ticker, "10-Q"):
        from data_layer.fetch import backfill, fetch_filing  # noqa: PLC0415
        fetch_filing(ticker, "10-Q")
        backfill(ticker)
    folder = filing_dirs(ticker, "10-Q")[0]
    manifest = read_manifest(folder)
    period = period_of_report(folder, manifest["accession_no"])
    truth = xbrl_truth(load_facts(folder, manifest["accession_no"], TAGS, FACTS_CACHE), period)
    for r in agent_rows.values():
        if r["status"] == "not_disclosed":
            r["as_of"] = period  # same rule as the agents' run()
    rows = []
    for field in FIELDS:
        value, tag, detail = truth[field]
        r = agent_rows.get(field, {})
        got = r.get("value", "")
        if value is None:
            expected, match = "unscored", "unscored"
        else:
            expected = round(value)
            match = compare(field, str(expected), got, r.get("evidence_quote", "")) if r else "miss"
        rows.append({
            "ticker": ticker, "field": field, "expected": expected, "got": got, "match": match,
            "xbrl_tag": tag, "xbrl_detail": detail, "period": period,
            "agent_status": r.get("status", ""), "as_of": r.get("as_of", ""),
            "source_chunk_id": r.get("source_chunk_id", ""),
            "evidence_quote": r.get("evidence_quote", ""),
            "notes": r.get("notes", ""), "error": error,
            **{k: r.get(k, "") for k in AGENT_EXTRA},
        })
    add_citations(rows)
    return rows


def main() -> None:
    load_dotenv()
    args = sys.argv[1:]
    rescore, agentic = "--rescore" in args, "--agentic" in args
    text_only = agentic and "--no-xbrl" in args
    out_name = next((a.split("=", 1)[1] for a in args if a.startswith("--out=")), None)
    default = "heldout_results_agentic_textonly.csv" if text_only else "heldout_results_agentic.csv"
    out = HERE / out_name if out_name else (HERE / default if agentic else OUT)
    tickers = [a.upper() for a in args if not a.startswith("--")] or HELD_OUT
    if rescore:
        saved = agent_rows_from_csv(out)
        rows = [r for t in tickers for r in score_ticker(t, None, saved)]
    else:
        client = anthropic.Anthropic()
        with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
            rows = [r for rs in pool.map(lambda t: score_ticker(t, client, agentic=agentic, text_only=text_only),
                                         tickers) for r in rs]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    scored = [r for r in rows if r["match"] != "unscored"]
    hits = [r for r in scored if r["match"] != "miss"]
    print(f"\nHeld-out accuracy: {len(hits)}/{len(scored)}"
          + (f" = {len(hits) / len(scored):.0%}" if scored else "")
          + f"  ({len(rows) - len(scored)} unscored)")
    print_citations(rows)
    print_confidence(rows)
    unscored = [r for r in rows if r["match"] == "unscored"]
    if unscored:
        print("\nUnscored (no usable XBRL):")
        for r in unscored:
            print(f"  {r['ticker']:<5}{r['field']:<22} agent: {r['got']}  ({r['xbrl_detail']})")
    misses = [r for r in scored if r["match"] == "miss"]
    print(f"\nMisses ({len(misses)}):" if misses else "\nNo misses.")
    if misses:
        print("| ticker | field | expected (XBRL) | got | XBRL detail | agent's quote |")
        print("| --- | --- | --- | --- | --- | --- |")
        for r in misses:
            quote = (r["error"] or r["evidence_quote"]).replace("|", "\\|")
            print(f"| {r['ticker']} | {r['field']} | {r['expected']} | {r['got']} | {r['xbrl_detail']} | {quote} |")
    print(f"\nWrote {out.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
