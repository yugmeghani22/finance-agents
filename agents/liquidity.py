"""Liquidity agent: extracts the v1 liquidity fields for one ticker with Claude.

    from agents.liquidity import run
    rows = run("AXP")

Inputs are only that ticker's cached sections (balance sheet, MD&A liquidity,
debt) from the latest 10-Q, falling back to the 10-K for a section the 10-Q
lacks. The rules are evals/DEFINITIONS.md, read by its exact path; nothing else
under evals/ (in particular the answer key) is ever opened.

Run:  .venv/bin/python -m agents.liquidity AXP
"""

import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from data_layer import load_sections

MODEL = "claude-opus-5-5"
FIELDS = ["cash_and_equivalents", "total_debt", "net_debt", "debt_due_within_12m"]
SECTIONS = ["balance_sheet", "liquidity", "debt"]
FORMS = ["10-Q", "10-K"]  # latest 10-Q first; 10-K only for a section the 10-Q lacks
DEFINITIONS = Path(__file__).resolve().parent.parent / "evals" / "DEFINITIONS.md"

FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string", "enum": FIELDS},
        "value": {"type": ["number", "null"]},
        "unit": {"type": "string"},
        "as_of": {"type": "string"},
        "source_chunk_id": {"type": "string"},
        "evidence_quote": {"type": "string"},
        "notes": {"type": "string"},
        "status": {"type": "string", "enum": ["found", "not_disclosed"]},
    },
    "required": ["field", "value", "unit", "as_of", "source_chunk_id", "evidence_quote", "notes", "status"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"fields": {"type": "array", "items": FIELD_SCHEMA}},
    "required": ["fields"],
    "additionalProperties": False,
}

SYSTEM = """You extract liquidity fields from SEC filing excerpts for one company.

The rules below are binding. Follow them exactly, including units ($ millions),
which balance-sheet lines count, what is excluded, and when a field is
not_disclosed.

<definitions>
{definitions}
</definitions>

Use only the filing chunks you are given. Each chunk is wrapped in a <chunk>
tag with its id, form, filing date and source location. Location labels on
fallback excerpts can be wrong (they may name a table caption instead of the
real heading), so judge a passage by its own text.

Return exactly one entry per field: {fields}.
- value: a number in $ millions (negative allowed for net_debt), or null when
  status is not_disclosed.
- unit: "USD millions".
- as_of: the balance-sheet date (YYYY-MM-DD) the value is stated at.
- source_chunk_id: the id of the chunk the value came from (for a derived
  value, the input chunk ids separated by "; "). Empty when not_disclosed.
- evidence_quote: the supporting sentence or table row copied word for word
  from that chunk, at most about 30 words. To quote two separate rows or
  sentences, join them with " [...] ". For a derived value, write
  "n/a - derived". Empty when not_disclosed.
- notes: the math for derived or summed values, and any rounding.
- status: found or not_disclosed. Never estimate."""


def load_chunks(ticker: str) -> list[dict]:
    """Chunks for each liquidity section from the latest filing that has it."""
    chunks = []
    for section in SECTIONS:
        for form in FORMS:
            found = load_sections(ticker, form, section)
            if found:
                chunks.extend(found)
                break
    return chunks


def format_chunk(c: dict) -> str:
    fallback = ' fallback="true"' if c.get("fallback") else ""
    return (f'<chunk id="{c["id"]}" form="{c["form"]}" filing_date="{c["filing_date"]}" '
            f'source="{c["source"]}"{fallback}>\n{c["text"]}\n</chunk>')


def extract(ticker: str, client: anthropic.Anthropic | None = None) -> list[dict]:
    """One dict per field, as returned by Claude (value is a number or None)."""
    ticker = ticker.upper()
    chunks = load_chunks(ticker)
    if not chunks:
        raise RuntimeError(f"no cached sections for {ticker}; run python -m data_layer.fetch {ticker}")
    client = client or anthropic.Anthropic()
    system = SYSTEM.format(definitions=DEFINITIONS.read_text(), fields=", ".join(FIELDS))
    user = f"Company: {ticker}\n\n" + "\n\n".join(format_chunk(c) for c in chunks)

    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=system,
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        messages=[{"role": "user", "content": user}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError(f"{ticker}: model declined ({response.stop_details})")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"{ticker}: response hit max_tokens")
    text = next(b.text for b in response.content if b.type == "text")
    by_field = {r["field"]: r for r in json.loads(text)["fields"]}
    missing = [f for f in FIELDS if f not in by_field]
    if missing:
        raise RuntimeError(f"{ticker}: model returned no entry for {missing}")
    return [by_field[f] for f in FIELDS]


def run(ticker: str, client: anthropic.Anthropic | None = None) -> list[dict]:
    """Rows in the answer-key shape: value is a number or "not_disclosed"."""
    rows = []
    for r in extract(ticker, client):
        disclosed = r["status"] == "found" and r["value"] is not None
        value = r["value"] if disclosed else "not_disclosed"
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        rows.append({"ticker": ticker.upper(), **r, "value": value,
                     "status": "found" if disclosed else "not_disclosed"})
    return rows


if __name__ == "__main__":
    load_dotenv()
    for t in sys.argv[1:] or ["AXP"]:
        print(json.dumps(run(t), indent=2))
