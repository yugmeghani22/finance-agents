"""Tool-using agent: answers a question with a skill, tools and Claude.

    from agents.agentic import answer, run
    result = answer("What's JPMorgan's liquidity?")   # routes to a skill itself
    rows = run("AXP")                                  # liquidity skill, eval row shape

Flow: a router call picks the skill (skills/*/SKILL.md) and names the company;
the agent loop then works with the tools in agents/tools.py (at most
MAX_TOOL_CALLS calls) and ends by calling submit_answer with one entry per
skill field. Confidence is computed here, not taken from the model: the
entry's xbrl_expression is recomputed from the filing's real XBRL facts.

  high    the XBRL expression equals the reported value
  medium  no XBRL expression (text only), or not_disclosed
  low     the XBRL expression differs from the reported value, or names tags
          the filing doesn't have, or a not_disclosed field cites XBRL anyway
          (the model's notes explain)

Every run is logged to runs/{ticker}_{timestamp}.json: the routing decision,
each turn's reasoning summary and text, every tool call with its input,
output and duration, and the final answer.

Nothing here reads evals/: the rules come from the skill file.
"""

import datetime
import json
import re
import time
from pathlib import Path

import anthropic

from agents import tools
from agents.skills import Skill, list_skills, load_skill
from data_layer import period_of_report
from data_layer.store import filing_dirs, read_manifest
from data_layer.xbrl import filing_facts

MODEL = "claude-opus-5-5"
MAX_TOOL_CALLS = 15
MAX_SUBMIT_ATTEMPTS = 3
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
LOG_OUTPUT_CHARS = 20_000
TOLERANCE_M = 0.5  # $m; XBRL and text agree when equal in whole millions

AGENT_SYSTEM = """You are a financial analyst agent. You answer questions about US public
companies from their SEC 10-Q and 10-K filings, using only the tools you are
given. Follow the skill below exactly: its field rules are binding.

Work step by step with the tools. Base every value on what the tools return,
never on memory. When you have the answer, call submit_answer once.

<skill name="{name}">
{body}
</skill>"""

ROUTER_PROMPT = """Pick the skill that answers this question and name the company it asks about.

Skills:
{skills}

Question: {question}

If no skill fits, answer "none"."""


# ------------------------------------------------------------------ schemas

def submit_tool(skill: Skill) -> dict:
    entry = {
        "type": "object",
        "properties": {
            "field": {"type": "string", "enum": skill.fields},
            "value": {"anyOf": [{"type": "number"}, {"type": "null"}],
                      "description": f"In {skill.unit}; null when not_disclosed"},
            "unit": {"type": "string"},
            "as_of": {"type": "string", "description": "Balance-sheet date, YYYY-MM-DD"},
            "source_chunk_id": {"type": "string"},
            "evidence_quote": {"type": "string"},
            "notes": {"type": "string"},
            "status": {"type": "string", "enum": ["found", "not_disclosed"]},
            "xbrl_expression": {"type": "string", "description": "XBRL tags combined with + / -, or empty"},
        },
        "required": ["field", "value", "unit", "as_of", "source_chunk_id", "evidence_quote",
                     "notes", "status", "xbrl_expression"],
        "additionalProperties": False,
    }
    return {
        "name": "submit_answer",
        "description": "Submit the final answer: exactly one entry per field. Ends the task.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {"answers": {"type": "array", "items": entry}},
                         "required": ["answers"], "additionalProperties": False},
    }


# ------------------------------------------------------------------ routing

def route(question: str, client: anthropic.Anthropic) -> dict:
    skills = list_skills()
    schema = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "enum": [s.name for s in skills] + ["none"]},
            "company": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["skill", "company", "reason"],
        "additionalProperties": False,
    }
    listing = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": ROUTER_PROMPT.format(skills=listing, question=question)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"router declined: {response.stop_details}")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


# ------------------------------------------------------------------ confidence

def _facts_for(ticker: str, as_of: str, form: str) -> list[dict]:
    """XBRL facts of the cached filing whose period is `as_of` (default: `form`)."""
    folders = [d for f in tools.FORMS for d in filing_dirs(ticker, f)[:1]]
    match = [d for d in folders if read_manifest(d).get("period_of_report") == as_of]
    folder = match[0] if match else next(iter(filing_dirs(ticker, form)), None)
    return filing_facts(folder) if folder else []


def evaluate_xbrl(expression: str, value, facts: list[dict]) -> tuple[float | None, str]:
    """(XBRL total or None, problem). Each tag must exist in `facts`; a tag with
    several values is tried with each, keeping the total closest to `value`."""
    parts = re.findall(r"([+-]?)\s*([A-Za-z][A-Za-z0-9_]*)", expression or "")
    if not parts:
        return None, "no XBRL expression"
    by_tag: dict[str, list[float]] = {}
    for f in facts:
        by_tag.setdefault(f["tag"], []).append(f["value_m"])
    missing = [t for _, t in parts if t not in by_tag]
    if missing:
        return None, f"tag(s) not in the filing's XBRL: {', '.join(missing)}"
    totals = [0.0]
    for sign, tag in parts:
        s = -1 if sign == "-" else 1
        totals = [t + s * v for t in totals for v in by_tag[tag]]
    target = value if isinstance(value, (int, float)) else 0
    return min(totals, key=lambda t: abs(t - target)), ""


def confidence(entry: dict, facts: list[dict]) -> tuple[str, float | None, str]:
    total, problem = evaluate_xbrl(entry["xbrl_expression"], entry["value"], facts)
    if entry["status"] != "found" or entry["value"] is None:
        if total is not None:
            return "low", total, f"not_disclosed, but XBRL {entry['xbrl_expression']} = {total:,.3f}"
        return "medium", None, "not_disclosed"
    if problem == "no XBRL expression":
        return "medium", None, "text only: no matching XBRL fact"
    if total is None:
        return "low", None, problem
    if abs(total - entry["value"]) < TOLERANCE_M:
        return "high", total, f"XBRL {entry['xbrl_expression']} = {total:,.3f} agrees"
    return "low", total, f"XBRL {entry['xbrl_expression']} = {total:,.3f} differs from the text value"


# ------------------------------------------------------------------ agent loop

def _block_log(block) -> dict | None:
    if block.type == "thinking":
        return {"type": "reasoning", "text": block.thinking} if block.thinking else None
    if block.type == "text":
        return {"type": "text", "text": block.text}
    return None


def _validate(answers: list[dict], skill: Skill) -> str:
    fields = [a["field"] for a in answers]
    missing = [f for f in skill.fields if f not in fields]
    dupes = sorted({f for f in fields if fields.count(f) > 1})
    problems = []
    if missing:
        problems.append(f"missing fields {missing}")
    if dupes:
        problems.append(f"duplicate fields {dupes}")
    return "; ".join(problems)


def answer(question: str, skill_name: str | None = None, client: anthropic.Anthropic | None = None,
           log: bool = True) -> dict:
    client = client or anthropic.Anthropic()
    started = datetime.datetime.now()
    record: dict = {"question": question, "started_at": started.isoformat(timespec="seconds"),
                    "model": MODEL, "max_tool_calls": MAX_TOOL_CALLS, "steps": []}
    if skill_name is None:
        decision = route(question, client)
        record["route"] = decision
        if decision["skill"] == "none":
            record["error"] = f"no skill fits this question ({decision['reason']})"
            return _finish(record, None, log)
        skill_name = decision["skill"]
    skill = load_skill(skill_name)
    record["skill"] = skill.name

    system = AGENT_SYSTEM.format(name=skill.name, body=skill.body)
    all_tools = tools.TOOLS + [submit_tool(skill)]
    messages = [{"role": "user", "content": question}]
    calls, submits, resolved = 0, 0, None
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    nudged = False

    while True:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            tools=all_tools,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "high"},
            cache_control={"type": "ephemeral"},
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        for k in usage:
            usage[k] += getattr(response.usage, k, 0) or 0
        step = {"turn": len(record["steps"]) + 1, "stop_reason": response.stop_reason,
                "content": [x for x in map(_block_log, response.content) if x], "tool_calls": []}
        record["steps"].append(step)
        if response.stop_reason == "refusal":
            record["error"] = f"model declined: {response.stop_details}"
            break
        messages.append({"role": "assistant", "content": response.content})
        uses = [b for b in response.content if b.type == "tool_use"]

        if not uses:
            if response.stop_reason == "max_tokens" or nudged:
                record["error"] = f"stopped without submit_answer ({response.stop_reason})"
                break
            nudged = True
            messages.append({"role": "user", "content": "Call submit_answer now with what you have found."})
            continue

        results, final = [], None
        for use in uses:
            entry = {"id": use.id, "name": use.name, "input": use.input}
            t0 = time.monotonic()
            if use.name == "submit_answer":
                submits += 1
                problem = _validate(use.input.get("answers", []), skill)
                if problem and submits < MAX_SUBMIT_ATTEMPTS:
                    entry["error"] = problem
                    results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                    "content": f"{problem}. Submit again with one entry per field."})
                else:
                    final = use.input
                    entry["output"] = "accepted" + (f" (with problems: {problem})" if problem else "")
            elif calls >= MAX_TOOL_CALLS:
                entry["error"] = "tool budget exhausted"
                results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                "content": f"Tool budget of {MAX_TOOL_CALLS} calls is used up. "
                                           "Call submit_answer now with what you have."})
            else:
                calls += 1
                try:
                    output = tools.call(use.name, use.input)
                    if use.name == "resolve_ticker" and resolved is None:
                        resolved = output
                    text = json.dumps(output, ensure_ascii=False)
                    results.append({"type": "tool_result", "tool_use_id": use.id, "content": text})
                    entry["output"] = text[:LOG_OUTPUT_CHARS]
                except tools.ToolError as e:
                    results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                    "content": str(e)})
                    entry["error"] = str(e)
            entry["seconds"] = round(time.monotonic() - t0, 2)
            step["tool_calls"].append(entry)
        if final is not None:
            record["submitted"] = final
            break
        messages.append({"role": "user", "content": results})

    record.update(tool_calls_used=calls, usage=usage, resolved=resolved)
    if "submitted" in record:
        record["answers"] = _finalize(record["submitted"]["answers"], skill, resolved)
    return _finish(record, resolved, log)


def _finalize(answers: list[dict], skill: Skill, resolved: dict | None) -> list[dict]:
    """Answers in skill field order, with confidence and a stable as_of."""
    ticker = resolved["ticker"] if resolved else None
    period = period_of_report(ticker, skill.form) if ticker else None
    by_field = {a["field"]: a for a in answers}
    out = []
    for field in skill.fields:
        a = dict(by_field.get(field) or {"field": field, "value": None, "unit": skill.unit, "as_of": "",
                                         "source_chunk_id": "", "evidence_quote": "", "status": "not_disclosed",
                                         "notes": "no entry submitted", "xbrl_expression": ""})
        if a["status"] != "found" or a["value"] is None:
            a["status"], a["value"] = "not_disclosed", None
            if period:
                a["as_of"] = period
        facts = _facts_for(ticker, a["as_of"], skill.form) if ticker else []
        a["confidence"], a["xbrl_value"], a["confidence_reason"] = confidence(a, facts)
        out.append(a)
    return out


def _finish(record: dict, resolved: dict | None, log: bool) -> dict:
    record["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    if log:
        RUNS_DIR.mkdir(exist_ok=True)
        ticker = resolved["ticker"] if resolved else "unresolved"
        stamp = record["started_at"].replace(":", "").replace("-", "")
        path = RUNS_DIR / f"{ticker}_{stamp}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str))
        record["log_path"] = str(path.relative_to(RUNS_DIR.parent))  # repo-relative: runs/...
    return record


# ------------------------------------------------------------------ eval adapter

def run(ticker: str, client: anthropic.Anthropic | None = None) -> list[dict]:
    """Liquidity rows for a ticker in the shape the eval scorers expect."""
    record = answer(f"What is the liquidity position of the company with ticker {ticker.upper()}?",
                    skill_name="liquidity", client=client)
    if "answers" not in record:
        raise RuntimeError(f"{ticker}: {record.get('error', 'no answer')} (log: {record.get('log_path')})")
    rows = []
    for a in record["answers"]:
        value = a["value"] if a["status"] == "found" else "not_disclosed"
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        rows.append({"ticker": ticker.upper(), **a, "value": value,
                     "tool_calls": record["tool_calls_used"], "log_path": record.get("log_path", "")})
    return rows
