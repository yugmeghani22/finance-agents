"""Convert SEC filing HTML to plain paragraphs, with tables as markdown tables.

SEC tables are laid out for print: values sit in cells spanning several grid
columns, with "$", "%" and ")" in cells of their own and empty spacer cells
between them. Converting cell-by-cell leaves numbers in the wrong columns, so
this module rebuilds each table from its grid:

1. Expand colspans so every cell has a [start, end) span on one grid.
2. Glue "$" onto the value after it and "%" / ")" onto the value before it.
3. Key each value by the grid column it ends at (numbers are right-aligned,
   so values of one column share an end), and give each key the header text
   whose span covers it.
4. Merge neighbouring keys that never hold values in the same row and share
   a header, which absorbs rows laid out with slightly different spans.
"""

import re

from bs4 import BeautifulSoup, NavigableString, Tag

BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "section", "article", "body", "html", "center", "blockquote"}
SKIP_TAGS = {"script", "style", "head", "title"}
HIDDEN = re.compile(r"display\s*:\s*none", re.I)

CURRENCY = re.compile(r"^[$€£¥]$")
TRAILING_SYMBOL = re.compile(r"^[%)]+$")
NUMERIC = re.compile(r"^[$€£¥]?\s*\(?[-–—]?\s*[\d,]*\.?\d+\s*\)?\s*%?$|^[-–—]+%?$")
YEAR = re.compile(r"^(19|20)\d\d$")


def is_value(text: str) -> bool:
    """A number in the table body. Bare years are column headers, not values."""
    return bool(NUMERIC.match(text)) and not YEAR.match(text)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def html_to_blocks(html: str) -> list[str]:
    """Paragraphs in reading order; each table is one block of markdown."""
    soup = BeautifulSoup(html, "lxml")
    blocks: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        text = normalize("".join(buffer))
        buffer.clear()
        if text:
            blocks.append(text)

    def walk(el: Tag) -> None:
        for child in el.children:
            if isinstance(child, NavigableString):
                if child.__class__.__name__ in ("Comment", "Doctype", "ProcessingInstruction"):
                    continue
                buffer.append(str(child))
                continue
            if not isinstance(child, Tag) or child.name in SKIP_TAGS:
                continue
            if HIDDEN.search(child.get("style", "")):
                continue
            if child.name == "table":
                flush()
                blocks.extend(table_to_blocks(child))
            elif child.name == "br":
                buffer.append(" ")
            elif child.name in BLOCK_TAGS:
                flush()
                walk(child)
                flush()
            else:
                walk(child)

    walk(soup)
    flush()
    return blocks


def _cell_text(cell: Tag) -> str:
    return normalize(cell.get_text(" ")).replace("|", "\\|")


def table_to_blocks(table: Tag) -> list[str]:
    # Rows of [start, end, text] on the expanded grid (nested tables ignored).
    rows: list[list[list]] = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        col, cells = 0, []
        for cell in tr.find_all(["td", "th"], recursive=False):
            span = int(cell.get("colspan", 1) or 1)
            text = _cell_text(cell)
            if text:
                cells.append([col, col + span, text])
            col += span
        rows.append(_glue_symbols(cells))
    rows = [r for r in rows if r]
    if not rows:
        return []

    value_rows = [r for r in rows if len(r) >= 2 and any(is_value(c[2]) for c in r[1:])]
    if not value_rows:
        # A layout table holding prose: one paragraph per row.
        return [" ".join(c[2] for c in r) for r in rows]
    # The label column ends where the second cell of a value row begins.
    label_end = min(r[1][0] for r in value_rows)

    def split(row):
        label = " ".join(c[2] for c in row if c[0] < label_end)
        values = [c for c in row if c[0] >= label_end]
        return label, values

    first_numeric = next(i for i, r in enumerate(rows) if any(is_value(c[2]) for c in split(r)[1]))
    header_rows, body_rows = [], []
    for i, row in enumerate(rows):
        label, values = split(row)
        (header_rows if i < first_numeric and values else body_rows).append((label, values))

    keys = sorted({c[1] for _, values in body_rows for c in values})
    headers = {k: _header_for(k, header_rows) for k in keys}
    keys = _merge_keys(keys, body_rows, headers)

    label_header = " ".join(label for label, _ in header_rows if label)
    out = ["| " + " | ".join([label_header] + [_header_text(k, headers) for k in keys]) + " |",
           "| " + " | ".join(["---"] * (len(keys) + 1)) + " |"]
    for label, values in body_rows:
        cells = {}
        for start, end, text in values:
            k = next(k for k in keys if end in k)
            cells[k] = f"{cells[k]} {text}" if k in cells else text
        out.append("| " + " | ".join([label] + [cells.get(k, "") for k in keys]) + " |")
    return ["\n".join(out)]


def _glue_symbols(cells: list[list]) -> list[list]:
    out: list[list] = []
    pending = None  # a currency symbol waiting for its value
    for start, end, text in cells:
        if CURRENCY.match(text):
            pending = [start, end, text]
            continue
        if TRAILING_SYMBOL.match(text) and out:
            out[-1][1] = end
            out[-1][2] += text
            continue
        if pending:
            start, text = pending[0], pending[2] + text
            pending = None
        out.append([start, end, text])
    if pending:
        out.append(pending)
    return out


def _header_for(key: int, header_rows) -> list[str]:
    parts = []
    for _, values in header_rows:
        for start, end, text in values:
            if start < key <= end and (not parts or parts[-1] != text):
                parts.append(text)
    return parts


def _merge_keys(keys: list[int], body_rows, headers) -> list[frozenset]:
    groups = [frozenset([k]) for k in keys]
    used = [{c[1] for c in values} for _, values in body_rows]
    merged = True
    while merged:
        merged = False
        for i in range(len(groups) - 1):
            a, b = groups[i], groups[i + 1]
            clash = any(row & a and row & b for row in used)
            same_header = headers[min(a)] == headers[min(b)]
            if not clash and same_header:
                groups[i:i + 2] = [a | b]
                merged = True
                break
    return groups


def _header_text(group: frozenset, headers) -> str:
    return " ".join(headers[min(group)]) or ""


def blocks_to_text(blocks: list[str]) -> str:
    return "\n\n".join(blocks)
