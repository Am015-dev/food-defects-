#!/usr/bin/env python3
"""
pricewatch triage - interactive view over the cases store.

    streamlit run streamlit_app.py -- --db pricewatch.db

Read-only. Detection, scanning and status changes stay in pricewatch.py; this
renders what a scan already recorded and never writes to the database.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

import streamlit as st

from pricewatch import DB_DEFAULT, REPORT_CSS, RULES, SEV_ORDER, db_open, eur

# the report's own webfonts, so the dashboard and report.html read as one surface.
# drop this line and the CSS falls back to system mono/sans.
FONTS = ('<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600'
         '&family=IBM+Plex+Sans:wght@400;600;700&display=swap" rel="stylesheet">')

# maps the report's tokens onto streamlit's own containers, plus the §10.5 floor
# (focus ring on --stamp, 44px targets, reduced motion). no new tokens.
APP_CSS = """
.stApp{background:var(--ink);color:var(--paper)}
[data-testid="stHeader"]{background:transparent}
[data-testid="stSidebar"]{background:var(--slab);border-right:1px solid var(--rule)}
[data-testid="stSidebar"] label,[data-testid="stWidgetLabel"] p{font-family:"IBM Plex Mono",monospace;
font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
[data-testid="stSidebar"] [role="radiogroup"] p{font-size:13px;text-transform:none;letter-spacing:0;color:var(--paper)}
[data-testid="stSidebar"] [role="radiogroup"] label{min-height:32px;align-items:center}
.stMainBlockContainer,.block-container{max-width:900px;margin:0 auto;padding-top:22px}
[data-testid="stMarkdownContainer"] p{margin:0}
.meter{margin-bottom:12px}
.stButton>button{background:var(--slab2);color:var(--paper);border:1px solid var(--rule);border-radius:4px;
min-height:44px;font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase}
.stButton>button:hover{border-color:var(--signal);color:var(--signal)}
:focus-visible{outline:2px solid var(--stamp);outline-offset:2px}
.empty.failed{color:var(--alarm);border-color:var(--alarm)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:640px){h1{font-size:22px}.now{font-size:20px}}
"""

ALL = "all"
STATUSES = ["open", "acknowledged", "mitigated", "fixed", "suppressed", "reopened"]
SEVERITIES = sorted(SEV_ORDER, key=SEV_ORDER.get)
FILTERS = {"status_filter": ALL, "severity_filter": ALL}


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def db_arg(argv: list[str]) -> str:
    for i, a in enumerate(argv):
        if a == "--db" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--db="):
            return a.split("=", 1)[1]
    return DB_DEFAULT


def query_cases(con, status=None, severity=None, rule=None):
    sql = "SELECT * FROM cases WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"; params.append(status)
    if severity:
        sql += " AND severity=?"; params.append(severity)
    if rule:
        sql += " AND rule_id=?"; params.append(rule)
    sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, last_seen DESC"
    return con.execute(sql, params).fetchall()


def header(scan) -> str:
    return ('<header><div class="eyebrow">Price integrity · triage</div>'
            '<h1>Defect triage</h1>'
            f'<p class="sub">{esc(scan["finished"]) if scan else "—"} · '
            f'{scan["lines_seen"] if scan else 0} lines checked</p></header>')


def meter(value, label, tone="") -> str:
    return (f'<div class="meter {tone}"><div class="n">{esc(value)}</div>'
            f'<div class="l">{esc(label)}</div></div>')


def case_card(r, hints, sev_of) -> str:
    sec = [s for s in (r["secondary"] or "").split(",") if s]
    flags = "".join(f'<span class="flag {sev_of.get(x, "medium")}">{esc(x)}</span>'
                    for x in [r["rule_id"], *sec])
    note = esc(hints.get(r["rule_id"], ""))
    unit = ""
    if r["unit_size"]:
        up = (f'{eur(r["unit_price"])} / {esc(r["unit_label"] or "unit")}' if r["unit_price"] is not None
              else '<span class="miss">no unit price shown</span>')
        unit = f'<div class="unit">{r["unit_size"]}{esc(r["unit_label"] or "")} · {up}</div>'
    was = f'<span class="was">{eur(r["original"])}</span>' if r["original"] is not None else ""
    zero = " zero" if r["price"] == 0 else ""
    return (f'<article class="case {esc(r["severity"])}">'
            f'<div class="head"><div class="title">{esc(r["title"])}</div>'
            f'<div class="n-occ">{esc(r["occurrences"])}×</div></div>'
            f'<div class="tag"><span class="now{zero}">{eur(r["price"])}</span>{was}{unit}'
            f'<div class="unit">sku {esc(r["sku"])} · {esc(r["source"])}</div></div>'
            f'<div class="flags">{flags}<div class="note">{note}</div></div>'
            f'<div class="foot">{esc(r["status"])} · first seen {esc(r["first_seen"])[:10]}'
            f' · last seen {esc(r["last_seen"])[:10]} · at risk {eur(r["value_at_risk"])}</div>'
            f'</article>')


def notice(msg: str, tone="") -> str:
    return f'<div class="empty {tone}">{esc(msg)}</div>'


def nothing_open(scan) -> str:
    if scan and scan["finished"]:
        return f"Nothing open. Rules last ran {scan['finished']}."
    return "Nothing open. Rules last ran — (no scan yet)."


def clear_filters() -> None:
    for k, v in FILTERS.items():
        st.session_state[k] = v


def main() -> None:
    st.set_page_config(page_title="Price integrity · triage", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown(f"{FONTS}<style>{REPORT_CSS}{APP_CSS}</style>", unsafe_allow_html=True)

    for k, v in FILTERS.items():
        st.session_state.setdefault(k, v)
    st.sidebar.radio("Status", [ALL, *STATUSES], key="status_filter")
    st.sidebar.radio("Severity", [ALL, *SEVERITIES], key="severity_filter")
    status = None if st.session_state.status_filter == ALL else st.session_state.status_filter
    severity = None if st.session_state.severity_filter == ALL else st.session_state.severity_filter

    db_path = db_arg(sys.argv[1:])
    st.sidebar.markdown(f'<div class="unit">{esc(db_path)}</div>', unsafe_allow_html=True)

    if not Path(db_path).exists():
        st.markdown(header(None), unsafe_allow_html=True)
        st.markdown(notice(nothing_open(None)), unsafe_allow_html=True)
        return

    hints = {r.id: r.hint for r in RULES}
    sev_of = {r.id: r.severity for r in RULES}
    try:
        with st.spinner("Reading cases…"):
            con = db_open(db_path)
            scan = con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
            total = con.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
            rows = query_cases(con, status, severity)
            cards = [case_card(r, hints, sev_of) for r in rows]
            openrows = [r for r in rows if r["status"] == "open"]
            risk = sum(r["value_at_risk"] for r in openrows)
            crit = sum(1 for r in openrows if r["severity"] == "critical")
    except Exception:  # noqa: BLE001 - a bad database must not reach the page as a traceback
        st.markdown(header(None), unsafe_allow_html=True)
        st.markdown(notice("Cases didn't load. Retry, or check the pricing service status.",
                           "failed"), unsafe_allow_html=True)
        return

    st.markdown(header(scan), unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    c1.markdown(meter(crit, "Open critical", "alarm"), unsafe_allow_html=True)
    c2.markdown(meter(eur(risk), "Value at risk", "signal"), unsafe_allow_html=True)
    c3.markdown(meter(len(openrows), "Open cases"), unsafe_allow_html=True)

    if not total:
        st.markdown(notice(nothing_open(scan)), unsafe_allow_html=True)
        return
    if not rows:
        st.markdown(notice("No cases match this filter."), unsafe_allow_html=True)
        st.button("Clear filters", on_click=clear_filters)
        return
    for card in cards:
        st.markdown(card, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
