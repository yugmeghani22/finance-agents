"""Deterministic checks on the drafted answer key (no LLM).

For every row of evals/answer_key_liquidity.csv:

  quote_in_chunk   evidence_quote appears verbatim in the cited cached chunk
  quote_in_filing  the quote appears in the original SEC filing text
  value_in_quote   draft_value appears in the quote after unit conversion
  math_check       net_debt = total_debt - cash; revolver_undrawn <= revolver_size
  as_of_check      as_of equals the cited filing's period of report

Each check is PASS, FAIL or N/A. A row's status is REVIEW if its notes say
UNSURE or nothing could be checked, FAIL if any check failed, else PASS.
Three random PASS rows per ticker are marked for a manual spot check.

Run:  .venv/bin/python -m evals.verify_key
Writes evals/verification_results.csv.
"""

import csv
import itertools
import json
import random
import re
import urllib.parse
import warnings
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from data_layer.extract import SECTIONS
from data_layer.store import filing_dirs, read_manifest

EVALS = Path(__file__).resolve().parent
KEY_PATH = EVALS / "answer_key_liquidity.csv"
OUT_PATH = EVALS / "verification_results.csv"

CHECKS = ["quote_in_chunk", "quote_in_filing", "value_in_quote", "math_check", "as_of_check"]
PASS, FAIL, NA = "PASS", "FAIL", "N/A"
SPOT_CHECKS_PER_TICKER = 3
SPOT_CHECK_SEED = 42  # fixed so the spot-check sample is reproducible
PART_SEP = " [...] "  # separates the parts of a multi-part quote
TABLE_WINDOW = 3000  # squashed characters that one table row's cells must fall within
ROW_SPAN_CHARS = 400  # rendered characters one table row's cells may span
FRAGMENT_BLOCK_TAGS =["p", "div", "td", "th", "tr", "li", "br", "table", "h1", "h2", "h3", "h4", "h5", "h6"]

NUMBER = re.compile(r"\(?\$?\s?(\d[\d,]*(?:\.\d+)?)\)?(?:\s*(billion|million))?", re.I)
NOTHING_DRAWN = re.compile(r"\bno (amount|amounts|borrowings)\b.*\b(drawn|outstanding|under)\b|\bundrawn\b", re.I)


# ------------------------------------------------------------------ loading

def load_chunks() -> tuple[dict[str, dict], dict[str, dict]]:
    """All cached chunks by id, and each filing's manifest by chunk id."""
    chunks, manifests = {}, {}
    for ticker_dir in (EVALS.parent / "data" / "cache").iterdir():
        for folder in filing_dirs(ticker_dir.name):
            manifest = read_manifest(folder)
            manifest["_folder"] = str(folder)
            for section in SECTIONS:
                path = folder / f"{section}.json"
                if not path.exists():
                    continue
                for rec in json.loads(path.read_text()):
                    chunks[rec["id"]] = rec
                    manifests[rec["id"]] = manifest
    return chunks, manifests


def filing_source(manifest: dict) -> dict:
    """The original filing's text and period of report, cached next to the
    extracted sections so the filing is downloaded at most once."""
    folder = Path(manifest["_folder"])
    html_path, meta_path = folder / "primary.html", folder / "filing_meta.json"
    if not (html_path.exists() and meta_path.exists()):
        from edgar import get_by_accession_number  # noqa: PLC0415 - only needed on a cache miss

        from data_layer.fetch import _ensure_identity  # noqa: PLC0415 - sets SEC identity + rate limit
        _ensure_identity()
        filing = get_by_accession_number(manifest["accession_no"])
        html_path.write_text(filing.html())
        meta_path.write_text(json.dumps({"period_of_report": str(filing.period_of_report)}))
    meta = json.loads(meta_path.read_text())
    with warnings.catch_warnings():
        # Inline-XBRL filings are XHTML; the HTML parser reads them fine.
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html_path.read_text(), "lxml")
    text = soup.get_text(" ")
    return {"squashed": squash(text), "blocks": rendered_blocks(soup),
            "period_of_report": meta["period_of_report"]}


def rendered_blocks(soup: BeautifulSoup) -> str:
    """The filing's text roughly as a browser lays it out: one line per block
    or table cell, inline tags joined without extra spaces ("$" + "500" stays
    "$500"). Text-fragment terms are matched against this."""
    for tag in soup.find_all(FRAGMENT_BLOCK_TAGS):
        tag.insert_after("\n")
    lines = (re.sub(r"\s+", " ", ln.replace("\xa0", " ")).strip() for ln in soup.get_text("").split("\n"))
    return "\n" + "\n".join(ln for ln in lines if ln) + "\n"


# ------------------------------------------------------------------ helpers

def squash(text: str) -> str:
    """Lowercase, unify quotes and dashes, drop table pipes and all whitespace,
    so the same words match whether they came from a table cell or a span."""
    text = text.lower().replace("\xa0", " ")
    text = re.sub("[‘’ʼ]", "'", text)
    text = re.sub("[“”]", '"', text)
    text = re.sub("[–—−]", "-", text)
    return re.sub(r"[\s|]+", "", text)


def quote_parts(quote: str) -> list[str]:
    return [p for p in quote.split(PART_SEP) if p.strip()]


def is_table_row(part: str) -> bool:
    return part.lstrip().startswith("|")


def cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def to_number(token: str) -> float:
    return float(token.replace(",", ""))


def table_multiplier(chunk_text: str, row: str) -> int:
    """1000 if the table holding `row` is stated in billions, else 1."""
    for block in re.split(r"\n\s*\n", chunk_text):
        if row.strip() in block:
            header = block.split("\n")[0]
            return 1000 if re.search(r"billion", header, re.I) else 1
    return 1


def parse_draft(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


# ------------------------------------------------------------------ checks

def check_quote_in_chunk(row, chunks) -> tuple[str, str]:
    ids = row["_ids"]
    if not row["_quote_parts"]:
        return NA, "no quote"
    missing = [i for i in ids if i not in chunks]
    if missing:
        return FAIL, f"unknown chunk id {missing}"
    for part in row["_quote_parts"]:
        if not any(part in chunks[i]["text"] for i in ids):
            return FAIL, f"not in cited chunk: {part[:60]!r}"
    return PASS, ""


def check_quote_in_filing(row, filings) -> tuple[str, str]:
    if not row["_quote_parts"]:
        return NA, "no quote"
    if not row["_filings"]:
        return FAIL, "cited filing not found"
    modes = []
    for part in row["_quote_parts"]:
        mode = _find_in_filings(part, [filings[k] for k in row["_filings"]])
        if not mode:
            return FAIL, f"not in filing text: {part[:60]!r}"
        modes.append(mode)
    return PASS, "; ".join(sorted(set(modes)))


def _find_in_filings(part: str, sources: list[dict]) -> str | None:
    target = squash(part)
    for src in sources:
        if target in src["squashed"]:
            return "exact"
    if not is_table_row(part):
        return None
    # A table row whose cells sit in separate HTML cells: every cell must
    # appear, in order, within a short stretch of the filing text.
    wanted = [squash(c) for c in cells(part) if squash(c)]
    for src in sources:
        text = src["squashed"]
        for start in (m.start() for m in re.finditer(re.escape(wanted[0]), text)):
            pos, ok = start, True
            for cell in wanted[1:]:
                found = text.find(cell, pos, start + TABLE_WINDOW)
                if found < 0:
                    ok = False
                    break
                pos = found + len(cell)
            if ok:
                return "table cells in order"
    return None


def check_value_in_quote(row, chunks, key_rows) -> tuple[str, str, str]:
    """Returns (status, detail, matched text to highlight)."""
    draft = row["draft_value"]
    target = parse_draft(draft)
    if target is None:
        return NA, f"draft_value is {draft!r}", ""
    if not row["_quote_parts"]:
        return NA, "no quote (derived value; see math_check)", ""
    quote = row["evidence_quote"]

    # Undrawn = size when the quote says nothing is drawn.
    if row["field"] == "revolver_undrawn" and NOTHING_DRAWN.search(quote):
        size = parse_draft(key_rows.get((row["ticker"], "revolver_size"), {}).get("draft_value", ""))
        if size is not None and size == target:
            return PASS, "quote says nothing drawn; undrawn = revolver_size", ""

    # A maturity year can sit anywhere, including a table row's label cell.
    if row["field"] == "nearest_maturity_date":
        m = re.search(rf"\b{int(target)}\b", quote)
        if m:
            return PASS, f"year {m.group(0)} in quote", m.group(0)
        return FAIL, f"year {draft} not in quote", ""

    chunk_text = "\n\n".join(chunks[i]["text"] for i in row["_ids"] if i in chunks)
    # 1. A single number in the quote (prose "billion"/"million" or table units).
    for part in row["_quote_parts"]:
        if is_table_row(part):
            mult = table_multiplier(chunk_text, part)
            for cell in cells(part)[1:]:
                for m in NUMBER.finditer(cell):
                    if round(to_number(m.group(1)) * mult, 3) == target:
                        return PASS, f"{m.group(0).strip()} x{mult}", m.group(0).strip()
        else:
            for m in NUMBER.finditer(part):
                mult = 1000 if (m.group(2) or "").lower() == "billion" else 1
                if round(to_number(m.group(1)) * mult, 3) == target:
                    return PASS, f"{m.group(0).strip()} x{mult}", m.group(0).strip()
    # 2. A sum of the same column across the quoted table rows.
    rows = [p for part in row["_quote_parts"] for p in part.split("\n") if is_table_row(p)]
    if len(rows) >= 2:
        mult = table_multiplier(chunk_text, rows[0])
        width = min(len(cells(r)) for r in rows)
        for col in range(1, width):
            nums = []
            for r in rows:
                m = NUMBER.search(cells(r)[col])
                if m:
                    nums.append(m)
            for k in range(2, len(nums) + 1):
                for combo in itertools.combinations(nums, k):
                    if round(sum(to_number(m.group(1)) for m in combo) * mult, 3) == target:
                        shown = " + ".join(m.group(0).strip() for m in combo)
                        return PASS, f"sum {shown} x{mult}", ";".join(m.group(0).strip() for m in combo)
    return FAIL, f"{draft} not found in quote (or as a column sum)", ""


def check_math(row, key_rows) -> tuple[str, str]:
    t = row["ticker"]
    val = lambda f: parse_draft(key_rows.get((t, f), {}).get("draft_value", ""))  # noqa: E731
    if row["field"] == "net_debt":
        net, debt, cash = val("net_debt"), val("total_debt"), val("cash_and_equivalents")
        if None in (net, debt, cash):
            return NA, "net_debt, total_debt or cash is not numeric"
        if net == debt - cash:
            return PASS, f"{debt:,.0f} - {cash:,.0f} = {net:,.0f}"
        return FAIL, f"{debt:,.0f} - {cash:,.0f} = {debt - cash:,.0f}, not {net:,.0f}"
    if row["field"] in ("revolver_undrawn", "revolver_size"):
        undrawn, size = val("revolver_undrawn"), val("revolver_size")
        if None in (undrawn, size):
            return NA, "revolver values not numeric"
        if undrawn <= size:
            return PASS, f"undrawn {undrawn:,.0f} <= size {size:,.0f}"
        return FAIL, f"undrawn {undrawn:,.0f} > size {size:,.0f}"
    return NA, ""


def check_as_of(row, filings) -> tuple[str, str]:
    if not row["_filings"]:
        return NA, "no cited filing"
    periods = {filings[k]["period_of_report"] for k in row["_filings"]}
    if periods == {row["as_of"]}:
        return PASS, ""
    return FAIL, f"as_of {row['as_of']} but cited filing period is {', '.join(sorted(periods))}"


# ------------------------------------------------------------------ "View in filing" links

def _hits(term: str, blocks: str) -> list[int]:
    """Positions where `term` occurs as whole words within one line of `blocks`."""
    pattern = r"(?<![\w$])" + re.escape(term) + r"(?![\w])"
    return [m.start() for m in re.finditer(pattern, blocks, re.I)]


def _in_order(terms: list[str], text: str) -> bool:
    pos = 0
    for t in terms:
        h = _hits(t, text[pos:])
        if not h:
            return False
        pos += h[0] + len(t)
    return True


def _encode(term: str) -> str:
    # The text-fragment spec reserves "-", "," and "&" inside terms.
    return urllib.parse.quote(term, safe="").replace("-", "%2D")


def build_fragment(quote: str, sources: list[dict]) -> tuple[str, str]:
    """A `:~:text=` directive that jumps to the quote, and whether it is unique.

    Each term must sit inside one rendered block (a paragraph or table cell),
    so prose uses the shortest unique run of its opening words plus its last
    few words, and a table row uses its first unique cell through its last
    cell. Returns ("", "no") when no term from the quote is in the filing."""
    if not quote or quote.startswith("n/a"):
        return "", ""
    part = quote_parts(quote)[0].split("\n")[0].strip()
    blocks = "\n".join(s["blocks"] for s in sources)
    if is_table_row(part):
        terms = [re.sub(r"[$()%]", "", c).strip() for c in cells(part)]
        terms = [t for t in terms if t and t not in ("-", "—")]
        found = [t for t in terms if _hits(t, blocks)]
        if not found:
            return "", "no"
        end = found[-1]
        directive = lambda s: _encode(s) if s == end else f"{_encode(s)},{_encode(end)}"  # noqa: E731
        # A browser jumps to the first match of the start term. Use the first
        # cell whose first match is in this row: the row's other cells sit
        # around it in order (earlier cells just before, later cells just after).
        if len(found) >= 2:
            for i, start in enumerate(found):
                hit = _hits(start, blocks)[0]
                if _in_order(found[i + 1:], blocks[hit + len(start):hit + len(start) + ROW_SPAN_CHARS]) and \
                        _in_order(found[:i], blocks[max(0, hit - ROW_SPAN_CHARS):hit]):
                    return directive(start), "yes"
        return directive(found[0]), "no"
    words = part.split()
    for n in range(min(5, len(words)), len(words) + 1):
        start = " ".join(words[:n])
        hits = _hits(start, blocks)
        if not hits:
            return "", "no"
        if len(hits) == 1:
            if n >= len(words) - 4:
                return _encode(part if n == len(words) else start), "yes"
            end = " ".join(words[-4:])
            return f"{_encode(start)},{_encode(end)}", "yes"
    return _encode(" ".join(words[:12])), "no"


# ------------------------------------------------------------------ main

def verify() -> list[dict]:
    chunks, manifests = load_chunks()
    with open(KEY_PATH, newline="") as f:
        key = list(csv.DictReader(f))
    key_rows = {(r["ticker"], r["field"]): r for r in key}

    filings: dict[str, dict] = {}
    results = []
    for row in key:
        row["_ids"] = [x.strip() for x in row["source_chunk_id"].split(";") if x.strip()]
        quote = row["evidence_quote"]
        row["_quote_parts"] = [] if not quote or quote.startswith("n/a") else quote_parts(quote)
        row["_filings"] = []
        for i in row["_ids"]:
            if i in manifests:
                acc = manifests[i]["accession_no"]
                if acc not in filings:
                    filings[acc] = filing_source(manifests[i])
                if acc not in row["_filings"]:
                    row["_filings"].append(acc)

        status = {}
        detail = {}
        status["quote_in_chunk"], detail["quote_in_chunk"] = check_quote_in_chunk(row, chunks)
        status["quote_in_filing"], detail["quote_in_filing"] = check_quote_in_filing(row, filings)
        status["value_in_quote"], detail["value_in_quote"], highlight = check_value_in_quote(row, chunks, key_rows)
        status["math_check"], detail["math_check"] = check_math(row, key_rows)
        status["as_of_check"], detail["as_of_check"] = check_as_of(row, filings)

        failed = [c for c in CHECKS if status[c] == FAIL]
        ran = [c for c in CHECKS if status[c] != NA]
        if "UNSURE" in row["notes"]:
            overall = "REVIEW"
        elif not ran:
            overall = "REVIEW"
            detail["as_of_check"] = (detail["as_of_check"] + "; nothing could be checked").strip("; ")
        elif failed:
            overall = "FAIL"
        else:
            overall = "PASS"

        urls = sorted({manifests[i]["url"] for i in row["_ids"] if i in manifests})
        fragment, fragment_unique = build_fragment(quote, [filings[a] for a in row["_filings"]])
        results.append({
            "ticker": row["ticker"],
            "field": row["field"],
            "draft_value": row["draft_value"],
            "status": overall,
            "failed_checks": ";".join(failed),
            **status,
            "details": " | ".join(f"{c}: {detail[c]}" for c in CHECKS if detail[c]),
            "highlight": highlight,
            "filing_url": ";".join(urls),
            "text_fragment": fragment,
            "fragment_unique": fragment_unique,
            "spot_check": "",
        })

    rng = random.Random(SPOT_CHECK_SEED)
    for ticker in dict.fromkeys(r["ticker"] for r in results):
        passed = [r for r in results if r["ticker"] == ticker and r["status"] == PASS]
        for r in rng.sample(passed, min(SPOT_CHECKS_PER_TICKER, len(passed))):
            r["spot_check"] = "yes"
    return results


def main() -> None:
    results = verify()
    with open(OUT_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    counts = {s: sum(r["status"] == s for r in results) for s in ("PASS", "FAIL", "REVIEW")}
    print(f"wrote {OUT_PATH.relative_to(EVALS.parent)}: {counts}")
    for r in results:
        mark = " *spot*" if r["spot_check"] else ""
        print(f"  {r['ticker']:<4}{r['field']:<24}{r['status']:<7}{r['failed_checks']}{mark}")


if __name__ == "__main__":
    main()
