"""Export cached liquidity, debt, balance sheet and fallback sections to markdown.

Writes evals/reading/{ticker}.md with every chunk under a heading of its id,
so a value found while reading can be cited by that id in the answer key.

Run:  .venv/bin/python -m evals.export_reading
"""

import re
from pathlib import Path

from data_layer import SECTIONS, list_available, load_sections
from data_layer.fetch import TICKERS

OUT_DIR = Path(__file__).resolve().parent / "reading"
ALWAYS = ["liquidity", "debt", "balance_sheet"]

# Cached text is already markdown (tables included); only make the
# fallback source tags stand out.
FALLBACK_TAG = re.compile(r"^(\[FALLBACK - from [^\]]*\])$", re.M)


def format_text(text: str) -> str:
    return FALLBACK_TAG.sub(r"**\1**", text)


def sections_to_export(manifest: dict) -> list[str]:
    status = manifest["sections"]
    return [s for s in SECTIONS
            if status[s]["status"] != "missing" and (s in ALWAYS or status[s]["status"] == "fallback")]


def render(ticker: str) -> str:
    filings = list_available(ticker)  # newest first
    out = [f"# {ticker}: liquidity, debt, balance sheet and fallback sections", ""]
    out.append("Each `####` heading is a chunk id - cite it in `answer_key_liquidity.csv`.")
    out.append("")
    for m in filings:
        out.append(f"- {m['form']} filed {m['filing_date']}: {m['url']}")
    for m in filings:
        out += ["", f"## {m['form']} filed {m['filing_date']}", ""]
        for name, info in m["sections"].items():
            if info["status"] == "missing":
                out.append(f"- **{name}: MISSING** - {info['reason']}")
        for section in sections_to_export(m):
            info = m["sections"][section]
            label = "FALLBACK - " if info["status"] == "fallback" else ""
            out += ["", f"### {section} ({label}{info['words']:,} words)", "", f"Source: {info['source']}"]
            if info["status"] == "fallback":
                out += ["", "> No dedicated footnote in this filing. These are excerpts from other parts of",
                        "> the filing; each paragraph says where it came from."]
            for chunk in _chunks_for(m, section):
                out += ["", f"#### {chunk['id']}", "", format_text(chunk["text"])]
    return "\n".join(out) + "\n"


def _chunks_for(manifest: dict, section: str) -> list[dict]:
    # load_sections returns the newest filing of a form; that is the one listed.
    chunks = load_sections(manifest["ticker"], manifest["form"], section)
    assert all(c["filing_date"] == manifest["filing_date"] for c in chunks)
    return chunks


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for ticker in TICKERS:
        path = OUT_DIR / f"{ticker}.md"
        path.write_text(render(ticker))
        print(f"wrote {path.relative_to(OUT_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
