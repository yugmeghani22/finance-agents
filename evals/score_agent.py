"""Score the liquidity agent against the answer key.

Runs agents/liquidity.py on every ticker in the key and compares each field to
the key's `value` column:

  exact      same number in whole $ millions, or both not_disclosed
  rounding   within the rounding DEFINITIONS.md allows: the agent's evidence
             is stated in billions (rounded to $0.1bn = ±$50m), so a value
             within $50m matches (±$100m for net_debt, which subtracts two
             rounded inputs)
  miss       anything else

Also records whether the agent's evidence_quote appears verbatim in the chunk
it cites (quote_in_chunk), as a check on its citations; it doesn't affect
the score.

Run:  .venv/bin/python -m evals.score_agent
Writes evals/agent_results.csv.
"""

import csv
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agents.liquidity import FIELDS, load_chunks, run

HERE = Path(__file__).resolve().parent
KEY = HERE / "answer_key_liquidity.csv"
OUT = HERE / "agent_results.csv"
ROUNDING_M = 50  # $0.1bn rounding, per DEFINITIONS.md
BILLIONS = re.compile(r"billion|\(billions\)", re.I)

COLUMNS = ["ticker", "field", "expected", "got", "match", "agent_status", "unit", "as_of",
           "source_chunk_id", "quote_in_chunk", "evidence_quote", "notes", "error"]


def load_key() -> dict[tuple[str, str], str]:
    with KEY.open() as f:
        return {(r["ticker"], r["field"]): r["value"].strip() for r in csv.DictReader(f)}


def as_number(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def compare(field: str, expected: str, got, evidence: str) -> str:
    if expected == "not_disclosed" or got == "not_disclosed":
        return "exact" if expected == got else "miss"
    e, g = as_number(expected), as_number(got)
    if e is None or g is None:
        return "miss"
    # Values are in whole $ millions; a filer reporting in thousands gives the
    # agent decimals (5,408,962k = 5,408.962m), which still equal 5,409.
    if round(e) == round(g):
        return "exact"
    tolerance = ROUNDING_M * (2 if field == "net_debt" else 1)
    if BILLIONS.search(evidence or "") and abs(e - g) <= tolerance:
        return "rounding"
    return "miss"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def quote_in_chunk(row: dict, chunks: dict[str, str]) -> str:
    quote = row.get("evidence_quote") or ""
    if row.get("status") != "found" or not quote or quote.startswith("n/a"):
        return "N/A"
    ids = [i.strip() for i in (row.get("source_chunk_id") or "").split(";") if i.strip()]
    texts = [chunks[i] for i in ids if i in chunks]
    if not texts:
        return "FAIL (unknown chunk)"
    # "[...]" (or "...") joins two pieces of one quote; each must be in the chunk.
    pieces = [normalize(p) for p in re.split(r"\[\.\.\.\]|\.\.\.|…", quote) if p.strip()]
    return "PASS" if all(any(p in normalize(t) for t in texts) for p in pieces) else "FAIL"


def score_ticker(ticker: str, key: dict, client: anthropic.Anthropic) -> list[dict]:
    try:
        agent_rows = {r["field"]: r for r in run(ticker, client)}
        error = ""
    except Exception as e:  # one ticker failing shouldn't sink the run
        agent_rows, error = {}, f"{type(e).__name__}: {e}"
    chunks = {c["id"]: c["text"] for c in load_chunks(ticker)}
    out = []
    for field in FIELDS:
        expected = key[(ticker, field)]
        r = agent_rows.get(field, {})
        got = r.get("value", "")
        out.append({
            "ticker": ticker, "field": field, "expected": expected, "got": got,
            "match": compare(field, expected, got, r.get("evidence_quote", "")) if r else "miss",
            "agent_status": r.get("status", ""), "unit": r.get("unit", ""), "as_of": r.get("as_of", ""),
            "source_chunk_id": r.get("source_chunk_id", ""),
            "quote_in_chunk": quote_in_chunk(r, chunks) if r else "N/A",
            "evidence_quote": r.get("evidence_quote", ""), "notes": r.get("notes", ""), "error": error,
        })
    return out


def print_report(rows: list[dict]) -> None:
    hits = [r for r in rows if r["match"] != "miss"]
    rounded = sum(r["match"] == "rounding" for r in rows)
    print(f"\nAccuracy: {len(hits)}/{len(rows)} = {len(hits) / len(rows):.0%}"
          f"  ({len(hits) - rounded} exact, {rounded} within rounding)")
    for field in FIELDS:
        fr = [r for r in rows if r["field"] == field]
        print(f"  {field:<22} {sum(r['match'] != 'miss' for r in fr)}/{len(fr)}")
    bad_quotes = [r for r in rows if r["quote_in_chunk"].startswith("FAIL")]
    if bad_quotes:
        print(f"\nQuotes not found verbatim in the cited chunk: "
              + ", ".join(f"{r['ticker']}/{r['field']}" for r in bad_quotes))

    misses = [r for r in rows if r["match"] == "miss"]
    if not misses:
        print("\nNo misses.")
        return
    print(f"\nMisses ({len(misses)}):")
    print(f"| ticker | field | expected | got | agent's quote |\n| --- | --- | --- | --- | --- |")
    for r in misses:
        quote = (r["error"] or r["evidence_quote"]).replace("|", "\\|")
        print(f"| {r['ticker']} | {r['field']} | {r['expected']} | {r['got']} | {quote} |")


def main() -> None:
    load_dotenv()
    key = load_key()
    tickers = list(dict.fromkeys(t for t, _ in key))
    client = anthropic.Anthropic()
    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        results = list(pool.map(lambda t: score_ticker(t, key, client), tickers))
    rows = [r for ticker_rows in results for r in ticker_rows]
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print_report(rows)
    print(f"\nWrote {OUT.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
