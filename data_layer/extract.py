"""Pull the sections our agents need out of a parsed 10-K / 10-Q.

Everything here works on text edgartools already parsed; nothing is fetched.
Every finder returns either a found section or a reason it is missing, so a
gap is recorded as a gap and never papered over.
"""

import re
from dataclasses import dataclass

from data_layer.html_text import html_to_blocks

SECTIONS = ["mdna", "liquidity", "market_risk", "debt", "derivatives"]

# Item names edgartools uses for each form.
ITEM_KEYS = {
    "10-K": {"mdna": ["Item 7"], "market_risk": ["Item 7A"]},
    "10-Q": {"mdna": ["Part I, Item 2", "Item 2"], "market_risk": ["Part I, Item 3", "Item 3"]},
}

# Footnote titles. "Debt Securities" is an investment holding, not borrowings.
DEBT_NOTE = re.compile(r"\b(debt|borrowings?)\b", re.I)
NOT_DEBT_NOTE = re.compile(r"debt securities|investment", re.I)
DERIVATIVES_NOTE = re.compile(r"derivative|hedg", re.I)

ITEM_HEADING = {
    "10-K": {"mdna": re.compile(r"^item\s*7\.?\s+management", re.I)},
    "10-Q": {"mdna": re.compile(r"^item\s*2\.?\s+management", re.I)},
}
ANY_ITEM_HEADING = re.compile(r"^item\s*\d+[a-c]?\.", re.I)
TABLE_ITEM_HEADING = re.compile(r"^\|\s*item\s*\d+[a-c]?\.", re.I | re.M)
# A period, optionally followed by closing quotes or parentheses.
SENTENCE_END = re.compile("\\.[\u201d\u2019\"')]*$")
MIN_MDNA_WORDS = 1000
# Matching edgartools' item text back to the table-preserving blocks.
ANCHOR_CHARS = 120
MAX_TRAILING_BLOCKS = 40
MAX_LENGTH_GAP = 0.5  # rebuilt span may differ from edgartools' by this fraction
MIN_SUBSECTION_WORDS = 150

LIQUIDITY_START = re.compile(r"liquidity|capital resources", re.I)
# Headings that mean the liquidity discussion is over.
LIQUIDITY_STOP = re.compile(
    r"risk management|critical accounting|other matters|accounting (standards|pronouncements|developments)"
    r"|recent(ly)? (adopted|issued)|forward-looking|cautionary|glossary|market risk|credit risk"
    r"|operational risk|regulation|non-gaap|results of operations"
    # Results-of-operations headings: a liquidity "heading" followed by these
    # was a summary-highlights label, not the start of the liquidity section.
    r"|interest income|provision for (credit|income)|other income|other expense|delinquenc"
    r"|charge-offs|loan receivables|average balance sheet|platform analysis",
    re.I,
)
MARKET_RISK_START = re.compile(r"market risk", re.I)
# Headings that mean the market-risk discussion is over (interest rate / FX
# risk headings are sub-parts of it and are deliberately not listed).
MARKET_RISK_STOP = re.compile(
    r"critical accounting|other matters|accounting (standards|pronouncements|developments)"
    r"|recent(ly)? (adopted|issued)|forward-looking|cautionary|glossary|liquidity|operational risk"
    r"|compliance risk|credit risk|strategic risk|reputation|model risk|capital",
    re.I,
)
# A market-risk item this short that points elsewhere is a cross-reference.
XREF_MAX_WORDS = 150
XREF_HINT = re.compile(r"MD&A|management.s discussion", re.I)

NOISE_LINE = re.compile(r"^\s*(table of contents|\d{1,3}|page \d+)\s*$", re.I)

# Fallbacks: when a footnote is missing, collect the paragraphs elsewhere in
# the filing (other footnotes, MD&A, market-risk item) that discuss the topic.
FALLBACK_TERMS = {
    "debt": re.compile(
        r"long-term debt|short-term (debt|borrowings)|\bborrowings\b|senior (unsecured )?notes"
        r"|commercial paper|unsecured debt|debt (issuance|maturit|outstanding)|credit facilit",
        re.I,
    ),
    "derivatives": re.compile(r"derivative|hedg|interest rate swap", re.I),
}
FALLBACK_SKIP_HEADINGS = re.compile(r"glossary|forward-looking|cautionary", re.I)
FALLBACK_MIN_WORDS = 8
FALLBACK_MAX_WORDS = 4000
FALLBACK_TAG = "[FALLBACK - from {where}]"
FALLBACK_TAG_LINE = re.compile(r"^\[FALLBACK - from [^\]]*\]$")


@dataclass
class Section:
    text: str | None
    source: str  # where in the filing it came from, or why it is missing
    fallback: bool = False  # True when assembled from other parts of the filing

    @property
    def found(self) -> bool:
        return bool(self.text)


def clean(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not NOISE_LINE.match(ln)]
    text = "\n".join(lines).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def word_count(text: str) -> int:
    return len(text.split())


def is_heading(p: str) -> bool:
    return (
        word_count(p) <= 12
        and p[0].isalpha()
        and not p.endswith((".", ":", ";", ","))
        and "$" not in p
    )


def subsection(text: str, start: re.Pattern, stop: re.Pattern) -> tuple[str, str] | None:
    """A subsection running from a heading matching `start` up to the next
    heading matching `stop` (and not `start`). Returns (heading, text) or None.

    Filings mention liquidity or market risk under several headings (summary
    highlights, risk-type lists, regulatory rules), so every candidate is
    measured and the longest one wins. A span over half the text means its
    end was never found, so it is not a real subsection."""
    paras = paragraphs(text)
    total = word_count(text)
    best: tuple[int, str, str] | None = None
    for begin, p in enumerate(paras):
        if not (is_heading(p) and start.search(p)):
            continue
        end = len(paras)
        for i in range(begin + 1, len(paras)):
            if is_heading(paras[i]) and stop.search(paras[i]) and not start.search(paras[i]):
                end = i
                break
        body = "\n\n".join(paras[begin:end])
        words = word_count(body)
        if words < MIN_SUBSECTION_WORDS or words > total * 0.5:
            continue
        if best is None or words > best[0]:
            best = (words, p, body)
    return (best[1], best[2]) if best else None


def document_blocks(html: str) -> list[str]:
    """The whole filing as paragraphs, tables as markdown, page furniture dropped."""
    return [b for b in html_to_blocks(html) if not NOISE_LINE.match(b)]


def is_table(block: str) -> bool:
    return block.startswith("|")


def is_item_heading(block: str) -> bool:
    """An "Item N." heading, as a paragraph or as the first row of a table
    (some filers put the heading in an index table, e.g. COF's 10-Q)."""
    if ANY_ITEM_HEADING.match(block):
        return True
    if is_table(block):
        first_rows = "\n".join(block.split("\n")[:4])
        return bool(TABLE_ITEM_HEADING.search(first_rows))
    return False


def item_from_document(blocks: list[str], form: str, section: str) -> str | None:
    """Find an item in the full document by its own "Item N." heading.

    Used when edgartools' section map comes back too short, which happens when
    a filer puts items out of the usual order (Capital One's 10-Q places MD&A
    before the financial statements). The table of contents also lists every
    heading, so the occurrence with the most text before the next item wins."""
    start = ITEM_HEADING[form][section]
    best: str | None = None
    for begin, p in enumerate(blocks):
        if not start.match(p):
            continue
        end = next((i for i in range(begin + 1, len(blocks)) if is_item_heading(blocks[i])), len(blocks))
        body = "\n\n".join(blocks[begin:end])
        if best is None or word_count(body) > word_count(best):
            best = body
    return best


def _anchor_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())[:ANCHOR_CHARS]


def _is_prose(p: str) -> bool:
    return (word_count(p) >= 8 and SENTENCE_END.search(p.rstrip())
            and not re.search(r" {3,}|[│─┼]", p) and not is_table(p))


def locate_in_blocks(plain: str, blocks: list[str]) -> str | None:
    """The run of `blocks` holding the same item as edgartools' `plain` text.

    edgartools decides where an item starts and ends but flattens its tables;
    `blocks` has the tables intact. The item's first and last prose paragraphs
    are found in `blocks`, and the span is widened to take in the item heading
    before and any tables after. When a paragraph occurs more than once (the
    business overview often repeats), the span closest in length wins."""
    prose = [p for p in paragraphs(plain) if _is_prose(p)]
    if not prose:
        return None
    keys = [_anchor_key(b) if not is_table(b) else "" for b in blocks]
    first, last = _anchor_key(prose[0]), _anchor_key(prose[-1])
    starts = [i for i, k in enumerate(keys) if k and k.startswith(first)]
    ends = [i for i, k in enumerate(keys) if k and k.startswith(last)]
    target = word_count(plain)
    best: tuple[float, int, int] | None = None
    for i in starts:
        # Widen backwards over the headings just before the first paragraph.
        begin = i
        while begin > 0 and i - begin < 3 and word_count(blocks[begin - 1]) <= 12 \
                and not is_table(blocks[begin - 1]) and not is_item_heading(blocks[begin - 1]):
            begin -= 1
        if begin > 0 and ANY_ITEM_HEADING.match(blocks[begin - 1]):
            begin -= 1
        for j in ends:
            if j < i:
                continue
            # Widen forwards to the next item heading, if it is close.
            end = next((k for k in range(j + 1, min(len(blocks), j + 1 + MAX_TRAILING_BLOCKS))
                        if is_item_heading(blocks[k])), j + 1)
            words = sum(word_count(b) for b in blocks[begin:end])
            gap = abs(words - target) / target
            if best is None or gap < best[0]:
                best = (gap, begin, end)
    if best is None or best[0] > MAX_LENGTH_GAP:
        return None
    return "\n\n".join(blocks[best[1]:best[2]])


def item_text(report, form: str, section: str, blocks: list[str]) -> tuple[str | None, str]:
    found: tuple[str, str] | None = None
    for key in ITEM_KEYS[form][section]:
        try:
            raw = report.get(key)
        except Exception:
            raw = None
        if raw and raw.strip():
            found = (clean(raw), key)
            break
    # A suspiciously short MD&A usually means the section map is wrong.
    if section == "mdna" and (found is None or word_count(found[0]) < MIN_MDNA_WORDS):
        fallback = item_from_document(blocks, form, section)
        if fallback and word_count(fallback) > word_count(found[0] if found else ""):
            label = ITEM_KEYS[form][section][0]
            return fallback, f"{label} (located in full document text)"
    if not found:
        return None, " / ".join(ITEM_KEYS[form][section])
    plain, key = found
    with_tables = locate_in_blocks(plain, blocks)
    if with_tables:
        return with_tables, key
    if re.search(r"[│─┼]| {3,}\S+ {3,}", plain):
        return plain, f"{key} (plain text: tables could not be rebuilt)"
    return plain, key


def find_note(report, title: re.Pattern, exclude: re.Pattern | None = None) -> Section:
    try:
        notes = list(report.notes)
    except Exception as e:
        return Section(None, f"could not read XBRL notes: {e}")
    if not notes:
        return Section(None, "filing has no XBRL footnotes")
    for note in notes:
        if title.search(note.title) and not (exclude and exclude.search(note.title)):
            text = note_text(note)
            if text:
                return Section(text, f"Note: {note.title}")
            return Section(None, f"note '{note.title}' exists but has no text")
    return Section(None, "no footnote with a matching title")


def note_text(note) -> str:
    """A footnote's text with its tables as markdown (plain text if no HTML)."""
    html = note.html
    if html:
        return "\n\n".join(document_blocks(html))
    return clean(note.text or "")


def extract_sections(report, form: str, html: str) -> dict[str, Section]:
    """`html` is the filing's primary document; tables are rebuilt from it."""
    out: dict[str, Section] = {}
    blocks = document_blocks(html)

    mdna, mdna_key = item_text(report, form, "mdna", blocks)
    out["mdna"] = Section(mdna, mdna_key) if mdna else Section(None, f"{mdna_key} not found")

    if mdna:
        liq = subsection(mdna, LIQUIDITY_START, LIQUIDITY_STOP)
        out["liquidity"] = (
            Section(liq[1], f"{mdna_key} > {liq[0]}") if liq
            else Section(None, "no liquidity / capital resources heading in MD&A")
        )
    else:
        out["liquidity"] = Section(None, "MD&A not found")

    mr, mr_key = item_text(report, form, "market_risk", blocks)
    if not mr:
        out["market_risk"] = Section(None, f"{mr_key} not found")
    elif word_count(mr) <= XREF_MAX_WORDS and XREF_HINT.search(mr) and mdna:
        # The item only points into MD&A: keep the pointer and follow it.
        sub = subsection(mdna, MARKET_RISK_START, MARKET_RISK_STOP)
        if sub:
            out["market_risk"] = Section(
                f"{mr}\n\n{sub[1]}", f"{mr_key} (cross-reference) + {mdna_key} > {sub[0]}"
            )
        else:
            out["market_risk"] = Section(mr, f"{mr_key} (cross-reference only; MD&A target not located)")
    else:
        out["market_risk"] = Section(mr, mr_key)

    sources = [(mdna_key, mdna), (mr_key, mr)]
    for name, note in (("debt", find_note(report, DEBT_NOTE, NOT_DEBT_NOTE)),
                       ("derivatives", find_note(report, DERIVATIVES_NOTE))):
        out[name] = note if note.found else fallback_excerpts(report, name, note.source, sources)
    return out


def fallback_excerpts(report, section: str, missing_reason: str,
                      item_sources: list[tuple[str, str | None]]) -> Section:
    """Paragraphs and tables from other footnotes and items that discuss `section`.

    Every excerpt is preceded by a "[FALLBACK ...]" tag naming where it came
    from (on its own line, so a table under it stays a table); chunk() keeps
    each tag with its excerpt, so a chunk stays traceable when cited on its
    own. Footnotes come first because they carry the hard numbers."""
    terms = FALLBACK_TERMS[section]
    excerpts: list[tuple[str, str]] = []
    try:
        notes = list(report.notes)
    except Exception:
        notes = []
    for note in notes:
        for p in paragraphs(note_text(note)):
            if word_count(p) >= FALLBACK_MIN_WORDS and terms.search(p):
                excerpts.append((f"Note: {note.title}", p))
    for key, text in item_sources:
        if not text:
            continue
        heading = None
        for p in paragraphs(text):
            if is_heading(p):
                heading = p
                continue
            if heading and FALLBACK_SKIP_HEADINGS.search(heading):
                continue
            if word_count(p) >= FALLBACK_MIN_WORDS and terms.search(p):
                excerpts.append((f"{key} > {heading}" if heading else key, p))

    if not excerpts:
        return Section(None, f"{missing_reason}; no discussion of it found elsewhere in the filing")

    kept, total, truncated = [], 0, False
    for where, p in excerpts:
        n = word_count(p)
        if kept and total + n > FALLBACK_MAX_WORDS:
            truncated = True
            break
        kept.append((where, p))
        total += n
    locations = list(dict.fromkeys(where for where, _ in kept))
    text = "\n\n".join(f"{FALLBACK_TAG.format(where=where)}\n\n{p}" for where, p in kept)
    source = f"FALLBACK ({missing_reason}) - excerpts from: " + "; ".join(locations)
    if truncated:
        source += f" (first {FALLBACK_MAX_WORDS:,} words of {len(excerpts)} matching paragraphs)"
    return Section(text, source, fallback=True)


def _units(text: str) -> list[str]:
    """Paragraphs, with each fallback tag kept together with the excerpt after it."""
    units: list[str] = []
    glue = False
    for p in paragraphs(text):
        if glue:
            units[-1] += "\n\n" + p
        else:
            units.append(p)
        glue = bool(FALLBACK_TAG_LINE.match(p))
    return units


def _split_oversized(unit: str, target_words: int) -> list[str]:
    """Break a unit bigger than a chunk: tables by rows (header repeated on
    every piece), prose on word boundaries."""
    lines = unit.split("\n")
    table_at = next((i for i, ln in enumerate(lines) if ln.startswith("|")), None)
    if table_at is not None and all(ln.startswith("|") for ln in lines[table_at:]):
        lead = "\n".join(lines[:table_at]).strip()
        header, body = lines[table_at:table_at + 2], lines[table_at + 2:]
        pieces, rows, size = [], [], 0
        for row in body:
            n = word_count(row)
            if rows and size + n > target_words:
                pieces.append(rows)
                rows, size = [], 0
            rows.append(row)
            size += n
        if rows:
            pieces.append(rows)
        out = []
        for k, rows in enumerate(pieces):
            note = f"(table continued, part {k + 1} of {len(pieces)})" if len(pieces) > 1 else ""
            prefix = "\n\n".join(x for x in (lead, note) if x)
            out.append((prefix + "\n\n" if prefix else "") + "\n".join(header + rows))
        return out
    words = unit.split()
    return [" ".join(words[i:i + target_words]) for i in range(0, len(words), target_words)]


def chunk(text: str, target_words: int = 1500) -> list[str]:
    """Split at paragraph breaks into pieces of about `target_words`.
    Tables are never cut mid-row; a unit longer than the target is split
    on its own (see _split_oversized)."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for p in _units(text):
        n = word_count(p)
        if n > target_words:
            if current:
                pieces.append("\n\n".join(current))
                current, size = [], 0
            pieces.extend(_split_oversized(p, target_words))
            continue
        if current and size + n > target_words:
            pieces.append("\n\n".join(current))
            current, size = [], 0
        current.append(p)
        size += n
    if current:
        pieces.append("\n\n".join(current))
    return pieces
