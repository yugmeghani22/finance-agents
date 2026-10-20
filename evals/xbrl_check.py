"""Cross-check answer-key values against the filing's own XBRL facts.

Each row is compared with the XBRL fact for the same filing and balance-sheet
date (the row's as_of). Tags are tried in the order listed; the tag that
matched is recorded. Only facts with no dimension (company-wide figures) are
used.

xbrl_check values:
  MATCH          XBRL equals draft_value
  MATCH_ROUNDED  within ROUNDING_TOLERANCE, and the draft is a whole $0.1bn (so it
                 may come from a rounded "$X.X billion" in the text)
  MISMATCH       XBRL has a different value
  XBRL_ONLY      draft is not_disclosed, but the filing tags a value
  NOT_FOUND      no usable fact
  N/A            field has no XBRL mapping
"""

import json
import re
from pathlib import Path

ROUNDING_TOLERANCE = 0.005  # 0.5%: covers figures given to $0.1bn in the text
ROUNDED_TO = 100  # $m; a draft that is a multiple of this may come from a rounded "$X.X billion"

CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
DEBT_PARTS = ["ShortTermBorrowings", "CommercialPaper", "LongTermDebtCurrent", "LongTermDebtNoncurrent"]
DEBT_FALLBACK = ["LongTermDebt", "DebtCurrent"]
CURRENT_DEBT_TAGS = ["LongTermDebtCurrent", "DebtCurrent"]
ALL_TAGS = set(CASH_TAGS + DEBT_PARTS + DEBT_FALLBACK + CURRENT_DEBT_TAGS)



# ------------------------------------------------------------------ facts

def load_facts(folder: Path, accession_no: str, tags: set[str] = ALL_TAGS,
               cache_name: str = "xbrl_facts.json") -> list[dict]:
    """Numeric facts for `tags` (default: the mapped tags), cached as `cache_name`."""
    path = folder / cache_name
    if not path.exists():
        from edgar import get_by_accession_number  # noqa: PLC0415 - only on a cache miss

        from data_layer.fetch import _ensure_identity  # noqa: PLC0415 - SEC identity + rate limit
        _ensure_identity()
        facts = []
        for f in get_by_accession_number(accession_no).xbrl().facts.get_facts():
            tag = f["concept"].split(":")[-1]
            if tag in tags and f.get("numeric_value") is not None and f.get("unit_ref", "").lower() == "usd":
                facts.append({
                    "tag": tag,
                    "period_key": f.get("period_key"),
                    "value_m": f["numeric_value"] / 1e6,
                    "dims": {k[4:]: v for k, v in f.items() if k.startswith("dim_") and v},
                })
        path.write_text(json.dumps(facts, indent=1))
    return json.loads(path.read_text())


def _values(facts: list[dict], tag: str, period: str) -> list[float]:
    """Distinct dimensionless values of `tag` at the instant `period`."""
    return sorted({f["value_m"] for f in facts
                   if f["tag"] == tag and f["period_key"] == f"instant_{period}" and not f["dims"]})


def _single(facts, tags, period) -> tuple[list[float], str]:
    for tag in tags:
        vals = _values(facts, tag, period)
        if vals:
            return vals, tag
    return [], ""


def total_debt(facts, period) -> tuple[float | None, str, str]:
    """Short-term borrowings + long-term debt from tagged parts; never Liabilities."""
    parts = {t: _values(facts, t, period) for t in DEBT_PARTS + DEBT_FALLBACK}
    for t, v in parts.items():
        if len(v) > 1:
            return None, t, f"{t} has several values {v}"
    got = {t: v[0] for t, v in parts.items() if v}
    if "LongTermDebtCurrent" in got or "LongTermDebtNoncurrent" in got:
        used = [t for t in DEBT_PARTS if t in got]
    elif "LongTermDebt" in got:
        short = [t for t in ("ShortTermBorrowings", "CommercialPaper") if t in got]
        if not short and "DebtCurrent" in got:
            short = ["DebtCurrent"]
        used = short + ["LongTermDebt"]
    else:
        found = ", ".join(f"{t} {v:,.0f}" for t, v in got.items()) or "none"
        return None, "", f"no long-term debt tag (found: {found})"
    total = sum(got[t] for t in used)
    detail = " + ".join(f"{t} {got[t]:,.0f}" for t in used)
    return total, "+".join(used), detail


# ------------------------------------------------------------------ comparison

def _compare(draft: str, candidates: list[float], tag: str, detail: str) -> dict:
    shown = ", ".join(f"{v:,.0f}" for v in candidates)
    out = {"xbrl_value": shown, "xbrl_tag": tag, "xbrl_detail": detail}
    try:
        d = float(draft)
    except ValueError:
        out["xbrl_check"] = "XBRL_ONLY" if candidates else "NOT_FOUND"
        if candidates:
            out["xbrl_detail"] = (f"draft is {draft!r} but the filing tags {tag} = {shown}. " + detail).strip()
        return out
    if not candidates:
        out["xbrl_check"] = "NOT_FOUND"
        return out
    best = min(candidates, key=lambda v: abs(v - d))
    if len(candidates) > 1:
        out["xbrl_detail"] = (f"several values tagged ({shown}); compared with the closest. " + detail).strip()
    if round(best) == round(d):
        out["xbrl_check"] = "MATCH"
    elif d % ROUNDED_TO == 0 and abs(best - d) <= ROUNDING_TOLERANCE * max(abs(best), 1):
        # Only a figure that looks rounded (whole $0.1bn) can differ by rounding;
        # an exact-looking draft that differs is a real difference.
        out["xbrl_check"] = "MATCH_ROUNDED"
        out["xbrl_detail"] = (f"draft {d:,.0f} vs XBRL {best:,.0f} (diff {d - best:+,.0f}, within rounding). "
                              + out["xbrl_detail"]).strip()
    else:
        out["xbrl_check"] = "MISMATCH"
        out["xbrl_detail"] = f"draft {d:,.0f} vs XBRL {best:,.0f} (diff {d - best:+,.0f}). " + out["xbrl_detail"]
    return out


def cross_check(field: str, draft: str, facts: list[dict], period: str) -> dict:
    """XBRL comparison for one answer-key row (facts from the row's filing)."""
    if field == "cash_and_equivalents":
        vals, tag = _single(facts, CASH_TAGS, period)
        note = "this tag includes restricted cash" if tag == CASH_TAGS[1] else ""
        return _compare(draft, vals, tag, note)
    if field == "total_debt":
        total, tag, detail = total_debt(facts, period)
        return _compare(draft, [total] if total is not None else [], tag, detail)
    if field == "net_debt":
        debt, debt_tag, debt_detail = total_debt(facts, period)
        cash, cash_tag = _single(facts, CASH_TAGS, period)
        if debt is None or len(cash) != 1:
            why = debt_detail if debt is None else "no single cash value"
            return {"xbrl_value": "", "xbrl_tag": "", "xbrl_check": "NOT_FOUND", "xbrl_detail": f"can't compute: {why}"}
        detail = f"({debt_detail}) - {cash_tag} {cash[0]:,.0f}"
        return _compare(draft, [debt - cash[0]], f"total_debt - {cash_tag}", detail)
    if field == "debt_due_within_12m":
        vals, tag = _single(facts, CURRENT_DEBT_TAGS, period)
        return _compare(draft, vals, tag, "")
    return {"xbrl_value": "", "xbrl_tag": "", "xbrl_check": "N/A", "xbrl_detail": "no XBRL mapping for this field"}
