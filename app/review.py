"""Review page for the liquidity answer key.

Shows each drafted value with its verification status and evidence, links
to the quote in the SEC filing, and saves Approve / Correct decisions to
evals/answer_key_liquidity.csv (my_value, verified_by_me, review_reason).

Run:  .venv/bin/streamlit run app/review.py
Run the verifier first:  .venv/bin/python -m evals.verify_key
"""

import csv
import datetime
import html
import os
import re
import tempfile
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
KEY_PATH = Path(os.environ.get("ANSWER_KEY_PATH", ROOT / "evals" / "answer_key_liquidity.csv"))
RESULTS_PATH = ROOT / "evals" / "verification_results.csv"
DEFINITIONS_PATH = ROOT / "evals" / "DEFINITIONS.md"

TICKERS = ["AXP", "COF", "SYF", "V", "MA"]
STATUS_COLORS = {"PASS": "#1a7f37", "FAIL": "#cf222e", "REVIEW": "#9a6700"}
CHECKS = ["quote_in_chunk", "quote_in_filing", "value_in_quote", "math_check", "as_of_check"]
FILTERS = {
    "All": lambda r: True,
    "FAIL": lambda r: r["status"] == "FAIL",
    "REVIEW": lambda r: r["status"] == "REVIEW",
    "Spot-check": lambda r: r["spot_check"] == "yes",
    "FAIL + REVIEW + spot-check": lambda r: r["status"] in ("FAIL", "REVIEW") or r["spot_check"] == "yes",
}
PART_SEP = " [...] "


# ------------------------------------------------------------------ data

def read_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def save_decision(ticker: str, field: str, my_value: str, verified_by_me: str, reason: str) -> None:
    """Write one decision into the answer key (read fresh, write atomically)."""
    rows = read_csv(KEY_PATH)
    cols = list(rows[0].keys())
    if "review_reason" not in cols:
        cols.append("review_reason")
    for r in rows:
        r.setdefault("review_reason", "")
        if r["ticker"] == ticker and r["field"] == field:
            r["my_value"], r["verified_by_me"], r["review_reason"] = my_value, verified_by_me, reason
    fd, tmp = tempfile.mkstemp(dir=KEY_PATH.parent, suffix=".csv")
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, KEY_PATH)


# ------------------------------------------------------------------ rendering helpers

def fmt_value(value: str, unit: str) -> str:
    try:
        n = float(value)
    except ValueError:
        return value or "—"
    if unit.startswith("year"):
        return str(int(n))
    return f"{n:,.0f}"


def pill(status: str) -> str:
    color = STATUS_COLORS.get(status, "#57606a")
    return (f"<span style='background:{color};color:#fff;padding:2px 10px;border-radius:999px;"
            f"font-size:0.8rem;font-weight:600'>{status}</span>")


def highlight(text: str, needles: list[str]) -> str:
    out = html.escape(text)
    for n in needles:
        n = html.escape(n)
        if n:
            out = out.replace(n, f"<mark style='background:#fff3a3;color:#000;padding:0 2px'>{n}</mark>", 1)
    return out


def render_quote(quote: str, needles: list[str]) -> str:
    """The evidence quote as HTML: table rows as a small table, prose as text."""
    blocks = []
    for part in quote.split(PART_SEP):
        lines = [ln for ln in part.split("\n") if ln.strip()]
        if lines and all(ln.lstrip().startswith("|") for ln in lines):
            trs = []
            for ln in lines:
                tds = "".join(f"<td style='padding:2px 8px;border:1px solid #d0d7de'>{highlight(c.strip(), needles)}</td>"
                              for c in ln.strip().strip("|").split("|"))
                trs.append(f"<tr>{tds}</tr>")
            blocks.append(f"<table style='border-collapse:collapse;font-size:0.85rem'>{''.join(trs)}</table>")
        else:
            blocks.append(f"<p style='margin:0'>{highlight(part, needles)}</p>")
    sep = "<p style='margin:4px 0;color:#57606a'>[...]</p>"
    return ("<div style='border-left:3px solid #d0d7de;padding:6px 12px;margin:6px 0'>"
            + sep.join(blocks) + "</div>")


# ------------------------------------------------------------------ page

st.set_page_config(page_title="Answer key review", layout="wide")
st.title("Liquidity answer key review")

if not RESULTS_PATH.exists():
    st.error("No verification results yet. Run `.venv/bin/python -m evals.verify_key` first.")
    st.stop()

key_rows = {(r["ticker"], r["field"]): r for r in read_csv(KEY_PATH)}
results = read_csv(RESULTS_PATH)

with st.sidebar:
    ticker = st.selectbox("Ticker", TICKERS)
    show = st.radio("Show", list(FILTERS), index=0)
    st.divider()
    st.caption("Status counts (all tickers)")
    for s in ("PASS", "FAIL", "REVIEW"):
        st.markdown(f"{pill(s)} &nbsp; {sum(r['status'] == s for r in results)}", unsafe_allow_html=True)
    decided = sum(1 for r in key_rows.values() if r.get("verified_by_me"))
    st.caption(f"Reviewed: {decided} of {len(key_rows)} rows")
    with st.expander("Field definitions"):
        st.markdown(DEFINITIONS_PATH.read_text())

rows = [r for r in results if r["ticker"] == ticker and FILTERS[show](r)]
key_label = KEY_PATH.relative_to(ROOT) if KEY_PATH.is_relative_to(ROOT) else KEY_PATH
st.caption(f"{ticker}: {len(rows)} field(s) shown · decisions save to `{key_label}`")
if not rows:
    st.info("No fields match this filter.")

for res in rows:
    row = key_rows[(res["ticker"], res["field"])]
    k = f"{res['ticker']}_{res['field']}"
    with st.container(border=True):
        head, badge = st.columns([4, 1])
        spot = " &nbsp;<span style='color:#57606a;font-size:0.8rem'>spot-check</span>" if res["spot_check"] else ""
        head.markdown(f"#### `{res['field']}`{spot}", unsafe_allow_html=True)
        badge.markdown(f"<div style='text-align:right'>{pill(res['status'])}</div>", unsafe_allow_html=True)

        st.markdown(
            f"<div style='font-size:1.8rem;font-weight:600;line-height:1.2'>"
            f"{html.escape(fmt_value(row['draft_value'], row['unit']))}</div>"
            f"<div style='color:#57606a'>{html.escape(row['unit'])} · as of {html.escape(row['as_of'])}</div>",
            unsafe_allow_html=True,
        )

        if res["failed_checks"]:
            st.markdown(f"**Failed checks:** {res['failed_checks'].replace(';', ', ')}")

        quote = row["evidence_quote"]
        if quote and not quote.startswith("n/a"):
            st.markdown(render_quote(quote, [h for h in res["highlight"].split(";") if h]), unsafe_allow_html=True)
            url = res["filing_url"].split(";")[0]
            if url:
                link = url + (f"#:~:text={res['text_fragment']}" if res["text_fragment"] else "")
                st.link_button("View in filing", link)
                if res["fragment_unique"] != "yes":
                    st.caption("This quote's text isn't unique in the filing, so the link may land on an "
                               "earlier match; use Find (Cmd+F) for the highlighted number.")
        else:
            st.caption(quote or "No evidence quote (value is not_disclosed).")

        with st.expander("Checks and notes"):
            st.markdown("\n".join(f"- **{c}**: {res[c]}" for c in CHECKS))
            st.markdown(f"**Details:** {res['details'] or '—'}")
            st.markdown(f"**Source chunk:** `{row['source_chunk_id'] or '—'}`")
            st.markdown(f"**Notes:** {row['notes'] or '—'}")

        if row.get("verified_by_me"):
            reason = f" — {row['review_reason']}" if row.get("review_reason") else ""
            st.success(f"Your decision: {row['verified_by_me']} · my_value = {row['my_value']}{reason}")

        a, c, u = st.columns([1, 1, 4])
        if a.button("Approve", key=f"approve_{k}", type="primary"):
            save_decision(res["ticker"], res["field"], row["draft_value"],
                          f"approved {datetime.date.today()}", "")
            st.rerun()
        if c.button("Correct", key=f"correct_{k}"):
            st.session_state[f"correcting_{k}"] = True
        if row.get("verified_by_me") and u.button("Undo decision", key=f"undo_{k}"):
            save_decision(res["ticker"], res["field"], "", "", "")
            st.rerun()

        if st.session_state.get(f"correcting_{k}"):
            with st.form(f"form_{k}"):
                new_value = st.text_input("my_value", value=row.get("my_value") or row["draft_value"])
                reason = st.text_input("Reason")
                ok, cancel = st.columns([1, 5])
                submitted = ok.form_submit_button("Save", type="primary")
                cancelled = cancel.form_submit_button("Cancel")
            if cancelled:
                st.session_state[f"correcting_{k}"] = False
                st.rerun()
            if submitted:
                if not new_value.strip() or not reason.strip():
                    st.error("Enter both my_value and a reason.")
                elif not re.fullmatch(r"-?[\d,]+(\.\d+)?|not_disclosed", new_value.strip()):
                    st.error("my_value must be a number (in $ millions, or a year) or not_disclosed.")
                else:
                    save_decision(res["ticker"], res["field"], new_value.strip().replace(",", ""),
                                  f"corrected {datetime.date.today()}", reason.strip())
                    st.session_state[f"correcting_{k}"] = False
                    st.rerun()
