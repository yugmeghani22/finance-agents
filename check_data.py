"""Fetch anything not yet cached, then print what the data layer holds.

Run:  .venv/bin/python check_data.py
"""

from data_layer import SECTIONS, list_available
from data_layer.fetch import TICKERS, fetch_all

COLUMNS = ["Ticker", "Form", "Filed"] + SECTIONS


def cell(info: dict) -> str:
    if info["status"] == "missing":
        return "MISSING"
    return f"{info['words']:,}w"


def main() -> None:
    fetch_all()
    rows = []
    for ticker in TICKERS:
        for m in list_available(ticker):
            rows.append([ticker, m["form"], m["filing_date"]] + [cell(m["sections"][s]) for s in SECTIONS])

    widths = [max(len(str(r[i])) for r in rows + [COLUMNS]) for i in range(len(COLUMNS))]
    line = "  ".join("{:<%d}" % w for w in widths)
    print()
    print(line.format(*COLUMNS))
    print(line.format(*["-" * w for w in widths]))
    for r in rows:
        print(line.format(*r))

    print("\nWhy sections are missing / where found ones came from:")
    for ticker in TICKERS:
        for m in list_available(ticker):
            for s in SECTIONS:
                info = m["sections"][s]
                detail = info["reason"] if info["status"] == "missing" else info["source"]
                print(f"  {ticker} {m['form']} {s:<12} {info['status']:<8} {detail}")


if __name__ == "__main__":
    main()
