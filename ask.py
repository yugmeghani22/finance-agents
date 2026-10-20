"""Ask a question about a US public company; the agent picks a skill and answers.

Run:  .venv/bin/python ask.py "What's JPMorgan's liquidity?"
      .venv/bin/python ask.py --json "How much debt does Ford have?"

Prints each field with its value, as_of, confidence, source chunk and quote,
and the path of the run log (runs/{ticker}_{timestamp}.json) holding every
tool call the agent made.
"""

import json
import sys

from dotenv import load_dotenv

from agents.agentic import answer


def fmt(value, unit: str) -> str:
    if value is None:
        return "not_disclosed"
    return f"{value:,.3f}".rstrip("0").rstrip(".") + (f" ({unit})" if unit else "")


def main() -> None:
    load_dotenv()
    args = sys.argv[1:]
    as_json = "--json" in args
    question = " ".join(a for a in args if a != "--json").strip()
    if not question:
        sys.exit(__doc__)
    result = answer(question)
    if as_json:
        print(json.dumps({k: result.get(k) for k in ("question", "skill", "resolved", "answers", "error",
                                                     "tool_calls_used", "log_path")}, indent=2, default=str))
        return
    if "answers" not in result:
        print(f"No answer: {result.get('error')}")
    else:
        r = result["resolved"] or {}
        print(f"\n{r.get('name', '?')} ({r.get('ticker', '?')}), skill: {result['skill']}, "
              f"{result['tool_calls_used']} tool calls\n")
        for a in result["answers"]:
            print(f"{a['field']}: {fmt(a['value'], a['unit'])}  as of {a['as_of']}  [{a['confidence']}]")
            print(f"    confidence: {a['confidence_reason']}")
            if a["status"] == "found":
                print(f"    source: {a['source_chunk_id']}")
                print(f"    quote:  {a['evidence_quote']}")
            if a["notes"]:
                print(f"    notes:  {a['notes']}")
    print(f"\nRun log: {result.get('log_path')}")


if __name__ == "__main__":
    main()
