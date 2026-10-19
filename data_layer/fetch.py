"""Download the latest 10-K and 10-Q for each ticker and cache their sections.

Run:  .venv/bin/python -m data_layer.fetch            (all tickers)
      .venv/bin/python -m data_layer.fetch AXP V      (some tickers)
      .venv/bin/python -m data_layer.fetch --refresh  (check SEC for newer filings)
"""

import os
import sys
import warnings

# SEC allows 10 requests/second. edgartools throttles every request it makes
# using this setting, which it reads once at import time.
os.environ["EDGAR_RATE_LIMIT_PER_SEC"] = "8"

from dotenv import load_dotenv
from edgar import Company, set_identity

from data_layer.extract import SECTIONS, chunk, extract_sections, word_count
from data_layer.store import CACHE_ROOT, chunk_id, filing_dirs, form_label, write_filing

TICKERS = ["AXP", "COF", "SYF", "V", "MA"]
FORMS = ["10-K", "10-Q"]

_identity_set = False


def _ensure_identity() -> None:
    global _identity_set
    if _identity_set:
        return
    load_dotenv()
    agent = os.getenv("SEC_USER_AGENT", "")
    if not agent or "example.com" in agent:
        raise RuntimeError('Set SEC_USER_AGENT in .env (format: "Name email@example.com")')
    set_identity(agent)
    _identity_set = True


def fetch_filing(ticker: str, form: str, refresh: bool = False) -> str:
    """Cache the latest `form` for `ticker`. Returns what happened, for logging.

    With a cached copy and refresh=False, no request is made at all. With
    refresh=True, SEC is asked for the latest filing date and the filing is
    downloaded only if it is newer than the cache."""
    cached = filing_dirs(ticker, form)
    if cached and not refresh:
        return f"cached ({cached[0].name})"

    _ensure_identity()
    filing = Company(ticker).get_filings(form=form, amendments=False).latest()
    if filing is None:
        return "no filing found on EDGAR"
    filing_date = str(filing.filing_date)
    folder = CACHE_ROOT / ticker / f"{form_label(form)}_{filing_date}"
    if (folder / "manifest.json").exists():
        return f"cached ({folder.name}, still latest)"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sections = extract_sections(filing.obj(), form, filing.html())

    manifest = {
        "ticker": ticker,
        "form": form,
        "filing_date": filing_date,
        "accession_no": filing.accession_no,
        "url": filing.filing_url,
        "sections": {},
    }
    chunks: dict[str, list[dict]] = {}
    for name in SECTIONS:
        sec = sections[name]
        if not sec.found:
            manifest["sections"][name] = {"status": "missing", "reason": sec.source}
            continue
        pieces = chunk(sec.text)
        chunks[name] = [
            {
                "id": chunk_id(ticker, form, filing_date, name, i),
                "ticker": ticker,
                "form": form,
                "filing_date": filing_date,
                "section": name,
                "chunk_index": i,
                "source": sec.source,
                "fallback": sec.fallback,
                "text": text,
            }
            for i, text in enumerate(pieces, start=1)
        ]
        manifest["sections"][name] = {
            "status": "fallback" if sec.fallback else "found",
            "source": sec.source,
            "words": word_count(sec.text),
            "chunks": len(pieces),
        }
    write_filing(folder, manifest, chunks)
    return f"downloaded ({folder.name})"


def fetch_all(tickers: list[str] = TICKERS, refresh: bool = False) -> None:
    for ticker in tickers:
        for form in FORMS:
            try:
                status = fetch_filing(ticker, form, refresh=refresh)
            except Exception as e:
                status = f"ERROR: {type(e).__name__}: {e}"
            print(f"{ticker:<4} {form:<5} {status}")


if __name__ == "__main__":
    args = sys.argv[1:]
    refresh = "--refresh" in args
    tickers = [a.upper() for a in args if not a.startswith("--")] or TICKERS
    fetch_all(tickers, refresh=refresh)
