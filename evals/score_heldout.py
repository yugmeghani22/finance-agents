"""Score the liquidity agent on held-out tickers, with XBRL as ground truth.

These tickers were not used to write DEFINITIONS.md or the answer key. Each
field's expected value comes from the latest 10-Q's own XBRL facts at its
period of report (dimensionless facts only), built to match DEFINITIONS.md:

  cash_and_equivalents  CashAndCashEquivalentsAtCarryingValue. A filer that
                        only tags cash including restricted cash is unscored,
                        since that tag doesn't match the definition.
  total_debt            short-term borrowings + long-term debt from the tagged
                        parts (evals/xbrl_check.total_debt; never Liabilities).
  net_debt              total_debt - cash_and_equivalents, from the two above.
  debt_due_within_12m   DebtCurrent as is; or LongTermDebtCurrent plus any
                        separately tagged ShortTermBorrowings / CommercialPaper.

A field with no usable XBRL is "unscored" and left out of the score. Matching
is the same as evals/score_agent.py (exact, or within $0.1bn rounding when the
agent's evidence is in billions). An agent not_disclosed where XBRL tags a
value is a miss.

Run:  .venv/bin/python -m evals.score_heldout [TICKER ...]   (default: PYPL ALLY FIS GPN)
Writes evals/heldout_results.csv.
"""

import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agents.liquidity import FIELDS, run
from data_layer.store import filing_dirs, read_manifest
from evals.score_agent import compare
from evals.xbrl_check import _values, load_facts, total_debt

HERE = Path(__file__).resolve().parent
OUT = HERE / "heldout_results.csv"
HELD_OUT = ["PYPL", "ALLY", "FIS", "GPN"]
CASH_TAG = "CashAndCashEquivalentsAtCarryingValue"
CASH_WITH_RESTRICTED = "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"

COLUMNS = ["ticker", "field", "expected", "got", "match", "xbrl_tag", "xbrl_detail", "period",
           "agent_status", "as_of", "source_chunk_id", "evidence_quote", "notes", "error"]


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


def xbrl_truth(facts: list[dict], period: str) -> dict[str, tuple[float | None, str, str]]:
    """field -> (expected value or None if unscored, tag(s) used, detail)."""
    out = {}
    cash, why = one(facts, CASH_TAG, period)
    if cash is None and not why:
        restricted, _ = one(facts, CASH_WITH_RESTRICTED, period)
        why = (f"only {CASH_WITH_RESTRICTED} tagged ({restricted:,.0f}), which includes restricted cash"
               if restricted is not None else "no cash tag")
    out["cash_and_equivalents"] = (cash, CASH_TAG if cash is not None else "", why)

    debt, debt_tag, debt_detail = total_debt(facts, period)
    out["total_debt"] = (debt, debt_tag, debt_detail)

    if debt is not None and cash is not None:
        out["net_debt"] = (debt - cash, f"total_debt - {CASH_TAG}", f"({debt_detail}) - {cash:,.0f}")
    else:
        out["net_debt"] = (None, "", "needs both total_debt and cash from XBRL")

    current, why = one(facts, "DebtCurrent", period)
    if current is not None:
        out["debt_due_within_12m"] = (current, "DebtCurrent", f"DebtCurrent {current:,.0f}")
    else:
        ltdc, why = one(facts, "LongTermDebtCurrent", period)
        if ltdc is None:
            out["debt_due_within_12m"] = (None, "", why or "no current-debt tag")
        else:
            parts = {"LongTermDebtCurrent": ltdc}
            for t in ("ShortTermBorrowings", "CommercialPaper"):
                v, _ = one(facts, t, period)
                if v is not None:
                    parts[t] = v
            out["debt_due_within_12m"] = (sum(parts.values()), "+".join(parts),
                                          " + ".join(f"{t} {v:,.0f}" for t, v in parts.items()))
    return out


def score_ticker(ticker: str, client: anthropic.Anthropic) -> list[dict]:
    folder = filing_dirs(ticker, "10-Q")[0]
    manifest = read_manifest(folder)
    period = period_of_report(folder, manifest["accession_no"])
    truth = xbrl_truth(load_facts(folder, manifest["accession_no"]), period)
    try:
        agent_rows, error = {r["field"]: r for r in run(ticker, client)}, ""
    except Exception as e:
        agent_rows, error = {}, f"{type(e).__name__}: {e}"
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
            "source_chunk_id": r.get("source_chunk_id", ""), "evidence_quote": r.get("evidence_quote", ""),
            "notes": r.get("notes", ""), "error": error,
        })
    return rows


def main() -> None:
    load_dotenv()
    tickers = [t.upper() for t in sys.argv[1:]] or HELD_OUT
    client = anthropic.Anthropic()
    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        rows = [r for rs in pool.map(lambda t: score_ticker(t, client), tickers) for r in rs]
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    scored = [r for r in rows if r["match"] != "unscored"]
    hits = [r for r in scored if r["match"] != "miss"]
    print(f"\nHeld-out accuracy: {len(hits)}/{len(scored)}"
          + (f" = {len(hits) / len(scored):.0%}" if scored else "")
          + f"  ({len(rows) - len(scored)} unscored)")
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
    print(f"\nWrote {OUT.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
