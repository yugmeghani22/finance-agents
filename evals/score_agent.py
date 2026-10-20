"""Score the liquidity agent against the answer key.

Runs agents/liquidity.py on every ticker in the key and compares each field to
the key's `value` column:

  exact      same number in whole $ millions, or both not_disclosed
  rounding   within the rounding DEFINITIONS.md allows: the agent's evidence
             is stated in billions (rounded to $0.1bn = ±$50m), so a value
             within $50m matches (±$100m for net_debt, which subtracts two
             rounded inputs)
  miss       anything else

Two citation checks, reported alongside the score (they don't change it):

  quote_in_chunk   the agent's evidence_quote appears verbatim in the chunk it
                   cites
  citation         how a found value traces to the agent's quotes:
                     direct      the value itself appears in its quote, read in
                                 $ millions, thousands (5,408,962 = 5,408.962m)
                                 or billions (within $0.1bn rounding)
                     traceable   a sum / difference of 2-3 numbers in its own
                                 quote (2,032 + 57,017 = 59,049), or, for a
                                 derived value (quote "n/a - derived"), of the
                                 ticker's other direct / traceable values
                                 (59,049 - 45,078 = 13,971)
                     untraced    neither
                   citation_trace records the expression that matched. The
                   search is kept this narrow on purpose: allowing any mix of
                   every number in every quote would find a chance match for
                   almost any value.
                   Direct % = direct / found values; traceable % = (direct +
                   traceable) / found values.

With --agentic the tool-using agent (agents/agentic.py) is scored instead of
the v1 single-call agent, and each row also gets its confidence (high /
medium / low) and the XBRL value it was checked against. Add --no-xbrl to
run it with get_xbrl_facts disabled (text only, the XBRL ablation).

Run:  .venv/bin/python -m evals.score_agent [--agentic]
      .venv/bin/python -m evals.score_agent [--agentic] --rescore
          re-scores the agent outputs already in the results file
          (no API calls)
Writes evals/agent_results.csv (--agentic: evals/agent_results_agentic.csv;
--agentic --no-xbrl: evals/agent_results_agentic_textonly.csv).
"""

import csv
import itertools
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agents.liquidity import FIELDS
from agents.liquidity import run as run_v1
from data_layer.extract import SECTIONS
from data_layer.store import load_sections

HERE = Path(__file__).resolve().parent
KEY = HERE / "answer_key_liquidity.csv"
OUT = HERE / "agent_results.csv"
OUT_AGENTIC = HERE / "agent_results_agentic.csv"
ROUNDING_M = 50  # $0.1bn rounding, per DEFINITIONS.md
BILLIONS = re.compile(r"billion|\(billions\)", re.I)
QUOTE_NUMBER = re.compile(r"(?<![\w.])\d(?:[\d,]*\d)?(?:\.\d+)?(?:\s*(billion|million))?", re.I)
MAX_TERMS = 3  # numbers combined from one quote

COLUMNS = ["ticker", "field", "expected", "got", "match", "agent_status", "unit", "as_of",
           "source_chunk_id", "quote_in_chunk", "citation", "citation_trace", "confidence", "confidence_reason", "xbrl_value",
           "tool_calls", "evidence_quote", "notes", "error", "log_path"]
AGENT_EXTRA = ["confidence", "confidence_reason", "xbrl_value", "tool_calls", "log_path"]


TEXT_ONLY = ("get_xbrl_facts",)


def agent_runner(agentic: bool, text_only: bool = False):
    """The agent's run(ticker, client). text_only: the agentic agent without
    get_xbrl_facts (the XBRL ablation)."""
    if agentic:
        from agents.agentic import run  # noqa: PLC0415
        if text_only:
            return lambda ticker, client=None: run(ticker, client, disabled_tools=TEXT_ONLY)
        return run
    return run_v1


def citable_chunks(ticker: str) -> dict[str, str]:
    """Text of every id an agent can cite: section chunks of both forms, and
    (for the agentic agent) the full-filing blocks search_filing returns."""
    from agents import tools  # noqa: PLC0415

    out = {}
    for form in ("10-Q", "10-K"):
        for section in SECTIONS:
            out.update({c["id"]: c["text"] for c in load_sections(ticker, form, section)})
        try:
            out.update(tools.filing_blocks(ticker, form))
        except tools.ToolError:
            pass
    return out


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


def quote_numbers(quote: str) -> list[tuple[float, str, bool]]:
    """(amount, text, in_billions) for each number in a quote, years skipped."""
    out = []
    for m in QUOTE_NUMBER.finditer(quote or ""):
        if re.fullmatch(r"(19|20)\d\d", m.group(0)):
            continue  # a year ("2026, $ 165"), not an amount; filings write 2,026
        n = float(m.group(0).split()[0].replace(",", ""))
        billions = (m.group(1) or "").lower() == "billion"
        out.append((n * 1000 if billions else n, m.group(0), billions))
    return out


def same(a: float, v: float, thousands: bool) -> bool:
    """Equal in whole $m; from thousands, equal to the agent's decimals too."""
    return abs(a - v) < 0.002 if thousands else round(a) == round(v)


def value_in_quote(value, quote: str) -> bool:
    v = as_number(value)
    if v is None:
        return False
    for n, _, billions in quote_numbers(quote):
        if billions and abs(n - abs(v)) <= ROUNDING_M:
            return True
        if not billions and (same(n, abs(v), False) or (n >= 1000 and same(n / 1000, abs(v), True))):
            return True
    return False


def find_combination(v: float, terms: list[tuple[float, str]], max_terms: int,
                     thousands: bool = False) -> str:
    """An expression of 2..max_terms signed `terms` equal to `v`, or ""."""
    for k in range(2, max_terms + 1):
        for combo in itertools.combinations(terms, k):
            for signs in itertools.product((1, -1), repeat=k):
                if same(sum(s * x for s, (x, _) in zip(signs, combo)), v, thousands):
                    ordered = sorted(zip(signs, combo), key=lambda p: -p[0])  # positive terms first
                    expr = "".join(f"{' + ' if s > 0 else ' - '}{t}" for s, (_, t) in ordered)
                    return expr[3:] if expr.startswith(" + ") else "-" + expr[3:]
    return ""


def add_citations(rows: list[dict]) -> None:
    """Set citation / citation_trace on each row (rows of one or more tickers)."""
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    for ticker_rows in by_ticker.values():
        derived = []
        for r in ticker_rows:
            r["citation"], r["citation_trace"] = "N/A", ""
            v = as_number(r["got"])
            if r["agent_status"] != "found" or v is None:
                continue
            quote = r["evidence_quote"] or ""
            if quote.startswith("n/a") or not quote:
                derived.append(r)
                continue
            if value_in_quote(v, quote):
                r["citation"] = "direct"
                continue
            nums = [(n, t) for n, t, billions in quote_numbers(quote) if not billions]
            expr = find_combination(v, nums, MAX_TERMS)
            if not expr:  # a filer reporting in thousands
                expr = find_combination(v, [(n / 1000, t) for n, t in nums if n >= 1000], MAX_TERMS, True)
            r["citation"], r["citation_trace"] = ("traceable", expr) if expr else ("untraced", "")
        for r in derived:
            v = as_number(r["got"])
            inputs = [(as_number(o["got"]), f"{o['field']} {o['got']}") for o in ticker_rows
                      if o is not r and o["citation"] in ("direct", "traceable")]
            expr = find_combination(v, inputs, 2, thousands=not float(v).is_integer())
            r["citation"], r["citation_trace"] = ("traceable", expr) if expr else ("untraced", "")


def citation_coverage(rows: list[dict]) -> tuple[int, int, int, list[dict]]:
    """(direct, direct + traceable, found values, the untraced rows)."""
    found = [r for r in rows if r["citation"] != "N/A"]
    direct = sum(r["citation"] == "direct" for r in found)
    traceable = sum(r["citation"] in ("direct", "traceable") for r in found)
    return direct, traceable, len(found), [r for r in found if r["citation"] == "untraced"]


def print_citations(rows: list[dict]) -> None:
    direct, traceable, found, untraced = citation_coverage(rows)
    print(f"\nCitation coverage ({found} found values):")
    print(f"  direct quote  {direct}/{found} = {direct / found:.0%}")
    print(f"  traceable     {traceable}/{found} = {traceable / found:.0%}")
    for r in rows:
        if r["citation"] == "traceable":
            print(f"    {r['ticker']}/{r['field']} = {r['got']}: {r['citation_trace']}")
    for r in untraced:
        print(f"  untraced: {r['ticker']}/{r['field']} = {r['got']}")


def agent_rows_from_csv(path: Path) -> dict[str, dict[str, dict]]:
    """Agent outputs saved by an earlier run: ticker -> field -> agent row."""
    out: dict[str, dict[str, dict]] = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            value = r["got"]
            if value not in ("", "not_disclosed"):
                n = float(value)
                value = int(n) if n.is_integer() else n
            out.setdefault(r["ticker"], {})[r["field"]] = {
                "value": value, "status": r["agent_status"], "unit": r.get("unit", ""), "as_of": r["as_of"],
                "source_chunk_id": r["source_chunk_id"], "evidence_quote": r["evidence_quote"],
                "notes": r["notes"], "error": r["error"],
                **{k: r.get(k, "") for k in AGENT_EXTRA}}
    return out


def score_ticker(ticker: str, key: dict, client: anthropic.Anthropic | None,
                 saved: dict | None = None, agentic: bool = False, text_only: bool = False) -> list[dict]:
    if saved is not None:
        agent_rows = saved.get(ticker, {})
        error = next((r["error"] for r in agent_rows.values() if r.get("error")), "")
    else:
        try:
            agent_rows = {r["field"]: r for r in agent_runner(agentic, text_only)(ticker, client)}
            error = ""
        except Exception as e:  # one ticker failing shouldn't sink the run
            agent_rows, error = {}, f"{type(e).__name__}: {e}"
    chunks = citable_chunks(ticker)
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
            **{k: r.get(k, "") for k in AGENT_EXTRA},
        })
    add_citations(out)
    return out


def print_confidence(rows: list[dict]) -> None:
    rated = [r for r in rows if r.get("confidence")]
    if not rated:
        return
    print("\nConfidence:")
    for level in ("high", "medium", "low"):
        rs = [r for r in rated if r["confidence"] == level]
        scored = [r for r in rs if r["match"] not in ("unscored",)]
        right = sum(r["match"] not in ("miss", "unscored") for r in scored)
        print(f"  {level:<7}{len(rs):>3} fields, {right}/{len(scored)} correct")
    for r in rated:
        if r["confidence"] == "low":
            print(f"    low: {r['ticker']}/{r['field']}: {r['confidence_reason']}")


def print_report(rows: list[dict]) -> None:
    hits = [r for r in rows if r["match"] != "miss"]
    rounded = sum(r["match"] == "rounding" for r in rows)
    print(f"\nAccuracy: {len(hits)}/{len(rows)} = {len(hits) / len(rows):.0%}"
          f"  ({len(hits) - rounded} exact, {rounded} within rounding)")
    for field in FIELDS:
        fr = [r for r in rows if r["field"] == field]
        print(f"  {field:<22} {sum(r['match'] != 'miss' for r in fr)}/{len(fr)}")
    print_citations(rows)
    print_confidence(rows)
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
    agentic = "--agentic" in sys.argv[1:]
    text_only = agentic and "--no-xbrl" in sys.argv[1:]
    out = (HERE / "agent_results_agentic_textonly.csv") if text_only else (OUT_AGENTIC if agentic else OUT)
    if "--rescore" in sys.argv[1:]:
        saved = agent_rows_from_csv(out)
        results = [score_ticker(t, key, None, saved, agentic) for t in tickers]
    else:
        client = anthropic.Anthropic()
        with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
            results = list(pool.map(lambda t: score_ticker(t, key, client, agentic=agentic,
                                                           text_only=text_only), tickers))
    rows = [r for ticker_rows in results for r in ticker_rows]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print_report(rows)
    print(f"\nWrote {out.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
