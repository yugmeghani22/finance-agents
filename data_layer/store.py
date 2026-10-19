"""Local cache of extracted filing sections.

Layout:
    data/cache/{ticker}/{form}_{filing_date}/
        manifest.json      filing metadata + found/missing status per section
        {section}.json     list of chunk records (only for sections found)
"""

import json
from pathlib import Path

from data_layer.extract import SECTIONS

CACHE_ROOT = Path(__file__).resolve().parent.parent / "data" / "cache"


def form_label(form: str) -> str:
    """'10-K' -> '10K' (also accepts '10K')."""
    return form.upper().replace("-", "")


def chunk_id(ticker: str, form: str, filing_date: str, section: str, index: int) -> str:
    return f"{ticker}_{form_label(form)}_{filing_date}_{section}_{index:02d}"


def filing_dirs(ticker: str, form: str | None = None) -> list[Path]:
    """Cached filing folders for a ticker, newest first. Only folders with a
    manifest count, so a half-written download is never treated as cached."""
    root = CACHE_ROOT / ticker.upper()
    if not root.is_dir():
        return []
    prefix = f"{form_label(form)}_" if form else ""
    dirs = [d for d in root.iterdir() if d.is_dir() and d.name.startswith(prefix) and (d / "manifest.json").exists()]
    return sorted(dirs, key=lambda d: d.name.split("_", 1)[1], reverse=True)


def write_filing(folder: Path, manifest: dict, chunks: dict[str, list[dict]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for section, records in chunks.items():
        (folder / f"{section}.json").write_text(json.dumps(records, indent=2, ensure_ascii=False))
    # Manifest last: its presence marks the folder complete.
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))


def read_manifest(folder: Path) -> dict:
    return json.loads((folder / "manifest.json").read_text())


def load_sections(ticker: str, form: str, section: str) -> list[dict]:
    """Chunks for one section of the latest cached filing of `form`.
    Returns [] when the section was recorded as missing or nothing is cached."""
    if section not in SECTIONS:
        raise ValueError(f"unknown section {section!r}; expected one of {SECTIONS}")
    dirs = filing_dirs(ticker, form)
    if not dirs:
        return []
    path = dirs[0] / f"{section}.json"
    return json.loads(path.read_text()) if path.exists() else []


def list_available(ticker: str) -> list[dict]:
    """One entry per cached filing: form, filing_date and each section's status."""
    return [read_manifest(d) for d in filing_dirs(ticker)]
