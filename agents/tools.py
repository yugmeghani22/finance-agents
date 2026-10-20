"""Tools the agentic pipeline exposes to Claude, and their implementations.

Every tool takes a dict of JSON inputs and returns a JSON-serializable dict.
A tool that can't do what was asked raises ToolError; the agent loop returns
the message to Claude as an error result instead of crashing.

Tools read only SEC data (EDGAR and the local cache under data/cache/).
Nothing here opens evals/.
"""

import ast
import json
import operator
import re
import warnings

from data_layer.extract import SECTIONS, document_blocks
from data_layer.store import chunk_id, filing_dirs, form_label, load_sections, read_manifest
from data_layer.xbrl import filing_facts

FORMS = ["10-Q", "10-K"]
MAX_OUTPUT_CHARS = 40_000
SEARCH_BLOCK_CHARS = 3_000
BLOCKS_FILE = "blocks.json"


class ToolError(Exception):
    pass


def _str(desc):
    return {"type": "string", "description": desc}


def _nullable(schema, desc):
    return {"anyOf": [schema, {"type": "null"}], "description": desc}


def _tool(name, description, properties):
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": properties,
                         "required": list(properties), "additionalProperties": False},
    }


FORM = {"type": "string", "enum": FORMS, "description": "10-Q (latest quarter) or 10-K (latest annual report)"}
TICKER = _str("Ticker symbol as returned by resolve_ticker, e.g. JPM")

TOOLS = [
    _tool("resolve_ticker",
          "Resolve a company name or ticker to its SEC ticker and CIK. Fails if the company "
          "doesn't file 10-K / 10-Q reports with the SEC (e.g. foreign filers using 20-F). "
          "Call this first.",
          {"query": _str("Company name or ticker, e.g. 'JPMorgan' or 'JPM'")}),
    _tool("fetch_filings",
          "Make sure the latest 10-Q and 10-K for a ticker are downloaded and split into "
          "sections (downloads only what isn't cached). Returns each filing's date and "
          "period of report. Call after resolve_ticker, before reading anything.",
          {"ticker": TICKER}),
    _tool("list_sections",
          "List the extracted sections of a filing: balance_sheet, liquidity (MD&A liquidity "
          "and capital resources), debt (debt footnote), mdna, market_risk, derivatives. "
          "Each shows found / fallback / missing and its chunk ids.",
          {"ticker": TICKER, "form": FORM}),
    _tool("read_section",
          "Read a section's text. Tables are markdown. Each chunk has an id to cite as "
          "source_chunk_id. Pass chunk_index to read one chunk of a long section.",
          {"ticker": TICKER, "form": FORM,
           "section": {"type": "string", "enum": SECTIONS, "description": "Section name"},
           "chunk_index": _nullable({"type": "integer"}, "1-based chunk number, or null for all chunks")}),
    _tool("search_filing",
          "Keyword search across the full filing (every paragraph and table), for when a "
          "section is missing or doesn't hold what you need. Returns matching blocks with ids "
          "to cite as source_chunk_id.",
          {"ticker": TICKER, "form": FORM,
           "query": _str("Keywords, e.g. 'commercial paper' or 'current portion of long-term debt'"),
           "max_results": _nullable({"type": "integer"}, "Blocks to return (default 8, max 20)")}),
    _tool("get_xbrl_facts",
          "The filing's XBRL facts: company-wide USD values in $ millions at the period of "
          "report. Filter by exact tag names and/or a keyword in the tag name. Use to "
          "cross-check values found in the text.",
          {"ticker": TICKER, "form": FORM,
           "tags": _nullable({"type": "array", "items": {"type": "string"}},
                             "Exact tag names, e.g. ['CashAndCashEquivalentsAtCarryingValue']"),
           "keyword": _nullable({"type": "string"}, "Case-insensitive substring of the tag name, e.g. 'Debt'")}),
    _tool("calculate",
          "Evaluate an arithmetic expression (+ - * / and parentheses) exactly, e.g. "
          "'2032 + 57017' or '59049 - 45078'. Use for every sum or difference you report.",
          {"expression": _str("Arithmetic expression using numbers only")}),
]


# ------------------------------------------------------------------ helpers

def _latest(ticker: str, form: str):
    dirs = filing_dirs(ticker.upper(), form)
    if not dirs:
        raise ToolError(f"no cached {form} for {ticker}; call fetch_filings first")
    return dirs[0], read_manifest(dirs[0])


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[... truncated, {len(text) - limit:,} more characters]"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# ------------------------------------------------------------------ tools

_tickers = None


def resolve_ticker(query: str) -> dict:
    global _tickers
    from edgar import Company, get_company_tickers  # noqa: PLC0415

    from data_layer.fetch import _ensure_identity  # noqa: PLC0415
    _ensure_identity()
    if _tickers is None:
        _tickers = get_company_tickers()
    df = _tickers
    q = query.strip()
    rows = df[df["ticker"].str.upper() == q.upper().replace(".", "-")]
    if rows.empty:
        name, nq = df["company"].map(_norm), _norm(q)
        if not nq:
            raise ToolError("empty company name")
        for mask in (name == nq, name.str.startswith(nq + " "), name.str.contains(rf"\b{re.escape(nq)}\b")):
            rows = df[mask]
            if not rows.empty:
                break
    if rows.empty:
        raise ToolError(f"no SEC registrant matches {query!r}")
    # Rows are ordered by market value; the first row of a CIK is its main ticker.
    ciks = list(dict.fromkeys(rows["cik"]))
    cik = ciks[0]
    main = df[df["cik"] == cik].iloc[0]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        company = Company(int(cik))
        forms = [f for f in FORMS if company.get_filings(form=f, amendments=False).latest() is not None]
    if not forms:
        raise ToolError(f"{main['company']} (CIK {cik}) doesn't file 10-K or 10-Q reports with the SEC; "
                        "this pipeline only reads those (foreign filers use 20-F / 40-F)")
    others = [f"{df[df['cik'] == c].iloc[0]['ticker']} ({df[df['cik'] == c].iloc[0]['company']})" for c in ciks[1:6]]
    return {"ticker": main["ticker"], "cik": int(cik), "name": main["company"],
            "files": forms, "other_matches": others}


def fetch_filings(ticker: str) -> dict:
    from data_layer.fetch import backfill, fetch_filing  # noqa: PLC0415

    ticker = ticker.upper()
    out = {}
    for form in FORMS:
        try:
            status = fetch_filing(ticker, form)
        except Exception as e:
            raise ToolError(f"{form} fetch failed: {type(e).__name__}: {e}") from e
        out[form] = {"status": status}
    backfill(ticker)
    for form in FORMS:
        dirs = filing_dirs(ticker, form)
        if dirs:
            m = read_manifest(dirs[0])
            out[form].update(filing_date=m["filing_date"], period_of_report=m.get("period_of_report"))
    return out


def list_sections(ticker: str, form: str) -> dict:
    folder, m = _latest(ticker, form)
    sections = {}
    for name in SECTIONS:
        info = m["sections"].get(name, {"status": "missing", "reason": "not extracted"})
        entry = {"status": info["status"], "source": info.get("source") or info.get("reason")}
        if info["status"] != "missing":
            entry["chunk_ids"] = [chunk_id(m["ticker"], m["form"], m["filing_date"], name, i)
                                  for i in range(1, info["chunks"] + 1)]
            entry["words"] = info["words"]
        sections[name] = entry
    return {"filing_date": m["filing_date"], "period_of_report": m.get("period_of_report"), "sections": sections}


def read_section(ticker: str, form: str, section: str, chunk_index: int | None) -> dict:
    _latest(ticker, form)
    chunks = load_sections(ticker.upper(), form, section)
    if not chunks:
        raise ToolError(f"{section} is missing from the {form}; try search_filing")
    if chunk_index is not None:
        chunks = [c for c in chunks if c["chunk_index"] == chunk_index]
        if not chunks:
            raise ToolError(f"no chunk {chunk_index} in {section}")
    out, used, rest = [], 0, []
    for c in chunks:
        if out and used + len(c["text"]) > MAX_OUTPUT_CHARS:
            rest.append(c["chunk_index"])
            continue
        text = _cap(c["text"], MAX_OUTPUT_CHARS)
        out.append({"id": c["id"], "source": c["source"], "fallback": c["fallback"], "text": text})
        used += len(text)
    result = {"chunks": out}
    if rest:
        result["not_shown"] = f"chunk_index {rest}: read them with chunk_index"
    return result


def _blocks(folder) -> list[str]:
    path = folder / BLOCKS_FILE
    if not path.exists():
        html = folder / "primary.html"
        if not html.exists():
            raise ToolError("the filing's HTML isn't cached; call fetch_filings")
        path.write_text(json.dumps(document_blocks(html.read_text()), ensure_ascii=False))
    return json.loads(path.read_text())


def filing_blocks(ticker: str, form: str) -> dict[str, str]:
    """Every block of the latest cached `form`, by the id search_filing cites."""
    folder, m = _latest(ticker, form)
    prefix = f"{m['ticker']}_{form_label(m['form'])}_{m['filing_date']}_doc"
    return {f"{prefix}_{i:04d}": b for i, b in enumerate(_blocks(folder))}


def search_filing(ticker: str, form: str, query: str, max_results: int | None) -> dict:
    folder, m = _latest(ticker, form)
    terms = [t for t in _norm(query).split() if t]
    if not terms:
        raise ToolError("empty query")
    phrase = " ".join(terms)
    scored = []
    for i, block in enumerate(_blocks(folder)):
        text = _norm(block)
        hits = sum(t in text for t in terms)
        if hits:
            scored.append((phrase in text, hits, -i, i, block))
    scored.sort(reverse=True)
    n = min(max_results or 8, 20)
    prefix = f"{m['ticker']}_{form_label(m['form'])}_{m['filing_date']}_doc"
    # Blocks with every keyword, when there are any; otherwise the best partial matches.
    if any(hits == len(terms) for _, hits, *_ in scored):
        scored = [s for s in scored if s[1] == len(terms)]
    results = [{"id": f"{prefix}_{i:04d}", "text": _cap(block, SEARCH_BLOCK_CHARS)}
               for *_, i, block in scored[:n]]
    return {"query": query, "matches": len(scored), "results": results}


def get_xbrl_facts(ticker: str, form: str, tags: list[str] | None, keyword: str | None) -> dict:
    folder, m = _latest(ticker, form)
    facts = filing_facts(folder)
    if tags:
        wanted = set(tags)
        facts = [f for f in facts if f["tag"] in wanted]
    if keyword:
        facts = [f for f in facts if keyword.lower() in f["tag"].lower()]
    facts = sorted(facts, key=lambda f: f["tag"])[:200]
    return {"period": m.get("period_of_report"), "unit": "USD millions",
            "facts": [{"tag": f["tag"], "value": round(f["value_m"], 3)} for f in facts]}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ToolError("only numbers, + - * / and parentheses are allowed")


def calculate(expression: str) -> dict:
    expr = expression.replace(",", "")
    try:
        value = _eval(ast.parse(expr, mode="eval").body)
    except (SyntaxError, ZeroDivisionError) as e:
        raise ToolError(f"can't evaluate {expression!r}: {e}") from e
    return {"expression": expression, "result": round(value, 6)}


IMPLEMENTATIONS = {
    "resolve_ticker": resolve_ticker,
    "fetch_filings": fetch_filings,
    "list_sections": list_sections,
    "read_section": read_section,
    "search_filing": search_filing,
    "get_xbrl_facts": get_xbrl_facts,
    "calculate": calculate,
}


def call(name: str, args: dict) -> dict:
    if name not in IMPLEMENTATIONS:
        raise ToolError(f"unknown tool {name!r}")
    return IMPLEMENTATIONS[name](**args)
