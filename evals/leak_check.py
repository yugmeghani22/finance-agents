"""Check that the liquidity agent can't see the answer key.

Three checks:

  static    agents/liquidity.py and the data_layer modules it imports never
            name a path under evals/ except evals/DEFINITIONS.md, and never
            import from evals.
  runtime   during real agent runs, every file the process opens is recorded
            with an audit hook; any open under evals/ other than
            DEFINITIONS.md is a leak.
  rules     DEFINITIONS.md (the one evals/ file the agent does read) holds
            none of the answer key's values.

Run:  .venv/bin/python -m evals.leak_check [TICKER ...]   (default: the 5 key tickers)
"""

import csv
import re
import sys
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
DEFINITIONS = EVALS / "DEFINITIONS.md"
KEY = EVALS / "answer_key_liquidity.csv"
AGENT_CODE = [ROOT / "agents" / "liquidity.py", *sorted((ROOT / "data_layer").glob("*.py"))]


@contextmanager
def trace_opens():
    """Yields a set that collects the resolved path of every file opened.
    Audit hooks can't be removed, so the hook just stops recording on exit."""
    opened: set[Path] = set()
    active = [True]

    def hook(event, args):
        if active[0] and event == "open" and isinstance(args[0], (str, Path)):
            opened.add(Path(args[0]).resolve())

    sys.addaudithook(hook)
    try:
        yield opened
    finally:
        active[0] = False


def evals_leaks(opened: set[Path]) -> list[Path]:
    """Opened files under evals/ other than DEFINITIONS.md."""
    return sorted(p for p in opened if p.is_relative_to(EVALS) and p != DEFINITIONS)


def static_check() -> list[str]:
    problems = []
    for path in AGENT_CODE:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"^\s*(from|import)\s+evals\b", code):
                problems.append(f"{path.relative_to(ROOT)}:{n} imports evals: {line.strip()}")
            for m in re.finditer(r"""["']([^"']*)["']""", code):
                s = m.group(1)
                if ("evals" in s or "answer_key" in s) and s not in ("evals", "DEFINITIONS.md"):
                    problems.append(f"{path.relative_to(ROOT)}:{n} names {s!r}")
    return problems


def rules_check() -> list[str]:
    """Answer-key numbers (written as in filings, e.g. 45,078) found in DEFINITIONS.md."""
    text = DEFINITIONS.read_text()
    with KEY.open() as f:
        values = {r["value"] for r in csv.DictReader(f) if r["value"].lstrip("-").isdigit()}
    found = []
    for v in sorted(values):
        n = int(v)
        for form in {v, f"{n:,}", f"{abs(n):,}"}:
            if abs(n) >= 100 and re.search(rf"(?<![\d,.]){re.escape(form)}(?![\d,])", text):
                found.append(form)
    return found


def main() -> None:
    load_dotenv()
    from agents.liquidity import run  # noqa: PLC0415 - imported here so the trace covers its file reads too

    with KEY.open() as f:
        default = list(dict.fromkeys(r["ticker"] for r in csv.DictReader(f)))
    tickers = [t.upper() for t in sys.argv[1:]] or default

    static = static_check()
    rules = rules_check()
    with trace_opens() as opened:
        for t in tickers:
            run(t)
    leaks = evals_leaks(opened)
    under_evals = sorted(p.relative_to(ROOT) for p in opened if p.is_relative_to(EVALS))
    data_files = sorted({str(p.relative_to(ROOT).parent) for p in opened if p.is_relative_to(ROOT / "data")})

    print(f"static:  {'PASS' if not static else 'FAIL'}")
    for p in static:
        print(f"         {p}")
    print(f"rules:   {'PASS' if not rules else 'FAIL'} (answer-key values in DEFINITIONS.md: {rules or 'none'})")
    print(f"runtime: {'PASS' if not leaks else 'FAIL'} ({len(tickers)} tickers: {', '.join(tickers)})")
    print(f"         files opened under evals/: {', '.join(map(str, under_evals)) or 'none'}")
    print(f"         cache folders read: {', '.join(data_files)}")
    for p in leaks:
        print(f"         LEAK: {p.relative_to(ROOT)}")
    sys.exit(1 if static or rules or leaks else 0)


if __name__ == "__main__":
    main()
