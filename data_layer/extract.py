"""Pull the sections our agents need out of a parsed 10-K / 10-Q.

Everything here works on text edgartools already parsed; nothing is fetched.
Every finder returns either a found section or a reason it is missing, so a
gap is recorded as a gap and never papered over.
"""

import re
from dataclasses import dataclass

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
MIN_MDNA_WORDS = 1000
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


def item_from_document(report, form: str, section: str) -> str | None:
    """Find an item in the full document text by its own "Item N." heading.

    Used when edgartools' section map comes back too short, which happens when
    a filer puts items out of the usual order (Capital One's 10-Q places MD&A
    before the financial statements). The table of contents also lists every
    heading, so the occurrence with the most text before the next item wins."""
    start = ITEM_HEADING[form][section]
    try:
        paras = paragraphs(clean(report.document.text()))
    except Exception:
        return None
    best: str | None = None
    for begin, p in enumerate(paras):
        if not start.match(p):
            continue
        end = next((i for i in range(begin + 1, len(paras)) if ANY_ITEM_HEADING.match(paras[i])), len(paras))
        body = "\n\n".join(paras[begin:end])
        if best is None or word_count(body) > word_count(best):
            best = body
    return best


def item_text(report, form: str, section: str) -> tuple[str | None, str]:
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
        fallback = item_from_document(report, form, section)
        if fallback and word_count(fallback) > word_count(found[0] if found else ""):
            label = ITEM_KEYS[form][section][0]
            return fallback, f"{label} (located in full document text)"
    if found:
        return found
    return None, " / ".join(ITEM_KEYS[form][section])


def find_note(report, title: re.Pattern, exclude: re.Pattern | None = None) -> Section:
    try:
        notes = list(report.notes)
    except Exception as e:
        return Section(None, f"could not read XBRL notes: {e}")
    if not notes:
        return Section(None, "filing has no XBRL footnotes")
    for note in notes:
        if title.search(note.title) and not (exclude and exclude.search(note.title)):
            text = clean(note.text or "")
            if text:
                return Section(text, f"Note: {note.title}")
            return Section(None, f"note '{note.title}' exists but has no text")
    return Section(None, "no footnote with a matching title")


def extract_sections(report, form: str) -> dict[str, Section]:
    out: dict[str, Section] = {}

    mdna, mdna_key = item_text(report, form, "mdna")
    out["mdna"] = Section(mdna, mdna_key) if mdna else Section(None, f"{mdna_key} not found")

    if mdna:
        liq = subsection(mdna, LIQUIDITY_START, LIQUIDITY_STOP)
        out["liquidity"] = (
            Section(liq[1], f"{mdna_key} > {liq[0]}") if liq
            else Section(None, "no liquidity / capital resources heading in MD&A")
        )
    else:
        out["liquidity"] = Section(None, "MD&A not found")

    mr, mr_key = item_text(report, form, "market_risk")
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
    """Paragraphs from other footnotes and items that discuss `section`.

    Every excerpt starts with a "[FALLBACK ...]" tag naming where it came
    from, so a chunk stays traceable even when cited on its own. Footnotes
    come first because they carry the hard numbers."""
    terms = FALLBACK_TERMS[section]
    excerpts: list[tuple[str, str]] = []
    try:
        notes = list(report.notes)
    except Exception:
        notes = []
    for note in notes:
        for p in paragraphs(clean(note.text or "")):
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
    text = "\n\n".join(f"[FALLBACK - from {where}] {p}" for where, p in kept)
    source = f"FALLBACK ({missing_reason}) - excerpts from: " + "; ".join(locations)
    if truncated:
        source += f" (first {FALLBACK_MAX_WORDS:,} words of {len(excerpts)} matching paragraphs)"
    return Section(text, source, fallback=True)


def chunk(text: str, target_words: int = 1500) -> list[str]:
    """Split at paragraph breaks into pieces of about `target_words`.
    A single paragraph longer than the target is split on word boundaries."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for p in paragraphs(text):
        n = word_count(p)
        if n > target_words:
            if current:
                pieces.append("\n\n".join(current))
                current, size = [], 0
            words = p.split()
            pieces.extend(" ".join(words[i:i + target_words]) for i in range(0, n, target_words))
            continue
        if current and size + n > target_words:
            pieces.append("\n\n".join(current))
            current, size = [], 0
        current.append(p)
        size += n
    if current:
        pieces.append("\n\n".join(current))
    return pieces
