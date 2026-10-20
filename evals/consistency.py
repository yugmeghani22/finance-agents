"""Run the liquidity agent several times per ticker and report value changes.

Run:  .venv/bin/python -m evals.consistency [RUNS] [TICKER ...]
      (default: 3 runs of the 5 answer-key tickers)
Writes evals/consistency_results.csv: one row per ticker and field, with the
value from each run and whether they all agree.
"""

import csv
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agents.liquidity import FIELDS, run

HERE = Path(__file__).resolve().parent
OUT = HERE / "consistency_results.csv"
DEFAULT_TICKERS = ["AXP", "COF", "SYF", "V", "MA"]


def main() -> None:
    load_dotenv()
    args = sys.argv[1:]
    runs = int(args.pop(0)) if args and args[0].isdigit() else 3
    tickers = [t.upper() for t in args] or DEFAULT_TICKERS
    client = anthropic.Anthropic()
    jobs = [(t, i) for i in range(runs) for t in tickers]

    def one(job):
        t, i = job
        try:
            return job, {r["field"]: r for r in run(t, client)}
        except Exception as e:
            return job, {"error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        results = dict(pool.map(one, jobs))

    rows, changed = [], []
    for t in tickers:
        for f in FIELDS:
            outs = [results[(t, i)] for i in range(runs)]
            values = [o[f]["value"] if f in o else f"ERROR {o.get('error', '')}" for o in outs]
            as_ofs = [o[f]["as_of"] if f in o else "" for o in outs]
            stable = len({str(v) for v in values}) == 1 and len(set(as_ofs)) == 1
            rows.append({"ticker": t, "field": f, "stable": stable,
                         **{f"run{i + 1}": v for i, v in enumerate(values)},
                         **{f"as_of_run{i + 1}": a for i, a in enumerate(as_ofs)}})
            if not stable:
                changed.append(rows[-1])

    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{runs} runs x {len(tickers)} tickers x {len(FIELDS)} fields: "
          f"{len(rows) - len(changed)}/{len(rows)} fields identical across runs")
    for r in changed:
        print(f"  CHANGED {r['ticker']} {r['field']}: " +
              ", ".join(f"{r[f'run{i + 1}']} ({r[f'as_of_run{i + 1}']})" for i in range(runs)))
    print(f"Wrote {OUT.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
