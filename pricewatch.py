#!/usr/bin/env python3
"""
pricewatch - price integrity monitor.

Crawls a catalogue (JSON API or HTML), normalises every line to integer cents,
runs a rule engine over it, and tracks defects as deduplicated cases in SQLite.

    python pricewatch.py scan --config pricewatch.yaml
    python pricewatch.py cases --status open --severity critical
    python pricewatch.py report --out report.html
    python pricewatch.py export --format csv --out cases.csv
    python pricewatch.py resolve 12 --status fixed --note "clamped discount at base"

Exit codes:  0 clean   2 new critical cases found   1 error
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
import yaml

DB_DEFAULT = "pricewatch.db"
UA_DEFAULT = "pricewatch/0.1 (price integrity monitor)"


# --------------------------------------------------------------------------
# money: integer cents everywhere. never float.
# --------------------------------------------------------------------------

def to_cents(value: Any, scale: str = "euro") -> int | None:
    """Parse a price into integer cents. Returns None if absent/unparseable."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value)) if scale == "cents" else int(round(value * 100))
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("€", "").replace("EUR", "").strip()
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or s in {"-", ".", ","}:
        return None
    # 1.234,56 -> 1234.56 ; 1,05 -> 1.05
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        num = float(s)
    except ValueError:
        return None
    return int(round(num)) if scale == "cents" else int(round(num * 100))


def eur(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"{cents / 100:,.2f}€".replace(",", "@").replace(".", ",").replace("@", ".")


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = re.sub(r"[^\d,.\-]", "", str(value)).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# canonical line
# --------------------------------------------------------------------------

@dataclass
class Line:
    source: str
    url: str
    sku: str
    title: str
    price: int | None = None              # displayed price, cents
    original: int | None = None           # struck-through price, cents
    discount_amount: int | None = None    # fixed discount, cents
    discount_pct: float | None = None     # percentage discount
    unit_size: float | None = None        # e.g. 1.5
    unit_label: str | None = None         # "lt", "kg"
    unit_price: int | None = None         # displayed price per unit, cents
    raw: dict = field(default_factory=dict)

    def resolved_discount(self) -> int | None:
        """Discount in cents, however it was expressed."""
        if self.discount_amount is not None:
            return self.discount_amount
        if self.discount_pct is not None and self.original is not None:
            return int(round(self.original * self.discount_pct / 100))
        if self.original is not None and self.price is not None:
            return self.original - self.price
        return None

    def has_promo(self) -> bool:
        return bool(self.discount_amount or self.discount_pct
                    or (self.original is not None and self.price is not None
                        and self.original > self.price))


# --------------------------------------------------------------------------
# rule engine
# --------------------------------------------------------------------------

@dataclass
class Rule:
    id: str
    severity: str
    hint: str
    test: Any

    def fires(self, ln: Line) -> bool:
        try:
            return bool(self.test(ln))
        except Exception:
            return False


def _arithmetic_mismatch(ln: Line) -> bool:
    if ln.price is None or ln.original is None:
        return False
    d = ln.discount_amount
    if d is None and ln.discount_pct is not None:
        d = int(round(ln.original * ln.discount_pct / 100))
    if d is None:
        return False
    return abs((ln.original - d) - ln.price) > 1


def _unit_price_mismatch(ln: Line) -> bool:
    if ln.price is None or ln.unit_price is None or not ln.unit_size:
        return False
    return abs(ln.price / ln.unit_size - ln.unit_price) > 1.5


RULES: list[Rule] = [
    Rule("DISCOUNT_GE_BASE", "critical",
         "Fixed discount is >= the base price. Clamp the discount at base minus floor, or reject at campaign ingest.",
         lambda ln: ln.discount_amount is not None and ln.original is not None
                    and ln.original > 0 and ln.discount_amount >= ln.original),
    Rule("ZERO_PRICE", "critical",
         "A sellable line reached 0. Nothing should be orderable at zero unless flagged as a gift item.",
         lambda ln: ln.price == 0),
    Rule("NEGATIVE_PRICE", "critical",
         "Price is below zero - discount subtracted with no clamp.",
         lambda ln: ln.price is not None and ln.price < 0),
    Rule("ARITHMETIC_MISMATCH", "high",
         "Displayed price does not equal base minus discount. Two services are computing it differently.",
         _arithmetic_mismatch),
    Rule("EXTREME_DISCOUNT", "high",
         "90%+ off. Near-miss for a zero-price defect - verify the campaign is intentional.",
         lambda ln: ln.original and ln.price is not None and ln.price > 0
                    and (ln.original - ln.price) / ln.original >= 0.90),
    Rule("MISSING_ORIGINAL_PRICE", "medium",
         "Promo is active but no struck-through original price is rendered. Check the fixed-amount branch of the price component.",
         lambda ln: ln.has_promo() and ln.original is None),
    Rule("MISSING_UNIT_PRICE", "medium",
         "Pack size shown with no price per unit. Likely a guard that skips the calculation when price is 0.",
         lambda ln: bool(ln.unit_size) and ln.unit_price is None),
    Rule("UNIT_PRICE_MISMATCH", "medium",
         "Price per unit does not match displayed price divided by pack size - probably computed pre-discount.",
         _unit_price_mismatch),
    Rule("MISSING_PRICE", "high",
         "No price rendered at all for a listed product.",
         lambda ln: ln.price is None),
]

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2}


def evaluate(ln: Line) -> list[Rule]:
    hits = [r for r in RULES if r.fires(ln)]
    return sorted(hits, key=lambda r: SEV_ORDER[r.severity])


# --------------------------------------------------------------------------
# extraction: tiny path DSL  ->  "data.categories[].items[]"
# --------------------------------------------------------------------------

def walk(obj: Any, path: str) -> list[Any]:
    """Resolve a dotted path; '[]' fans out over a list."""
    if not path:
        return [obj] if obj is not None else []
    nodes = [obj]
    for part in path.split("."):
        fan = part.endswith("[]")
        key = part[:-2] if fan else part
        nxt = []
        for n in nodes:
            v = n.get(key) if isinstance(n, dict) and key else (n if not key else None)
            if v is None:
                continue
            if fan and isinstance(v, list):
                nxt.extend(v)
            else:
                nxt.append(v)
        nodes = nxt
    return nodes


def pluck(obj: Any, path: str | None) -> Any:
    if not path:
        return None
    vals = walk(obj, path)
    return vals[0] if vals else None


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

class Fetcher:
    def __init__(self, cfg: dict):
        p = cfg.get("politeness", {})
        self.delay = float(p.get("delay_seconds", 1.5))
        self.timeout = int(p.get("timeout", 20))
        self.respect_robots = bool(p.get("respect_robots", True))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": p.get("user_agent", UA_DEFAULT),
            "Accept": "application/json, text/html;q=0.9",
        })
        extra = cfg.get("headers") or {}
        self.session.headers.update(extra)
        self._robots: dict[str, Any] = {}
        self._last = 0.0

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        base = "{0.scheme}://{0.netloc}".format(urlparse(url))
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(urljoin(base, "/robots.txt"))
            try:
                rp.read()
            except Exception:
                rp = None
            self._robots[base] = rp
        if rp is None:
            return True
        return rp.can_fetch(self.session.headers["User-Agent"], url)

    def get(self, url: str, params: dict | None = None) -> requests.Response | None:
        if not self.allowed(url):
            print(f"  robots.txt disallows {url} - skipped", file=sys.stderr)
            return None
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                self._last = time.time()
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2 ** attempt * 2)
                    continue
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                if attempt == 2:
                    print(f"  fetch failed {url}: {e}", file=sys.stderr)
                    return None
                time.sleep(2 ** attempt * 2)
        return None


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def build_line(node: dict, src: dict, url: str) -> Line:
    f = src.get("fields", {})
    scale = src.get("price_scale", "euro")
    return Line(
        source=src["name"],
        url=url,
        sku=str(pluck(node, f.get("sku")) or "").strip() or hashlib.sha1(
            json.dumps(node, sort_keys=True, default=str).encode()).hexdigest()[:12],
        title=str(pluck(node, f.get("title")) or "untitled").strip(),
        price=to_cents(pluck(node, f.get("price")), scale),
        original=to_cents(pluck(node, f.get("original_price")), scale),
        discount_amount=to_cents(pluck(node, f.get("discount_amount")), scale),
        discount_pct=to_float(pluck(node, f.get("discount_pct"))),
        unit_size=to_float(pluck(node, f.get("unit_size"))),
        unit_label=(str(pluck(node, f.get("unit_label")) or "") or None),
        unit_price=to_cents(pluck(node, f.get("unit_price")), scale),
        raw=node if isinstance(node, dict) else {},
    )


def read_source(src: dict, fetcher: Fetcher, limit: int | None) -> Iterable[Line]:
    kind = src.get("type", "json")

    if kind == "file":                       # offline fixture / catalogue export
        data = json.loads(Path(src["path"]).read_text(encoding="utf-8"))
        for node in walk(data, src.get("items_path", "")):
            yield build_line(node, src, src["path"])
        return

    if kind == "json":
        pages = int(src.get("max_pages", 1))
        param = src.get("page_param")
        start = int(src.get("page_start", 1))
        seen = 0
        for i in range(pages):
            params = dict(src.get("query", {}))
            if param:
                params[param] = start + i
            r = fetcher.get(src["url"], params=params or None)
            if r is None:
                return
            try:
                data = r.json()
            except ValueError:
                print(f"  {src['name']}: response is not JSON", file=sys.stderr)
                return
            nodes = walk(data, src.get("items_path", ""))
            if not nodes:
                return
            for node in nodes:
                yield build_line(node, src, r.url)
                seen += 1
                if limit and seen >= limit:
                    return
        return

    if kind == "html":
        from bs4 import BeautifulSoup
        sel = src.get("selectors", {})
        r = fetcher.get(src["url"])
        if r is None:
            return
        soup = BeautifulSoup(r.text, "html.parser")
        for i, card in enumerate(soup.select(sel["item"])):
            if limit and i >= limit:
                return
            def pick(key):
                s = sel.get(key)
                if not s:
                    return None
                el = card.select_one(s)
                return el.get_text(" ", strip=True) if el else None
            node = {k: pick(k) for k in
                    ("sku", "title", "price", "original_price", "discount_amount",
                     "discount_pct", "unit_size", "unit_label", "unit_price")}
            src2 = dict(src)
            src2["fields"] = {k: k for k in node}
            yield build_line(node, src2, r.url)
        return

    raise ValueError(f"unknown source type: {kind}")


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT UNIQUE NOT NULL,
  rule_id TEXT NOT NULL,
  secondary TEXT,
  severity TEXT NOT NULL,
  source TEXT, sku TEXT, title TEXT, url TEXT,
  price INTEGER, original INTEGER, discount INTEGER,
  unit_size REAL, unit_label TEXT, unit_price INTEGER,
  value_at_risk INTEGER DEFAULT 0,
  occurrences INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT,
  status TEXT DEFAULT 'open',
  note TEXT,
  sample TEXT
);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY, started TEXT, finished TEXT,
  lines_seen INTEGER, cases_new INTEGER, cases_seen INTEGER
);
CREATE INDEX IF NOT EXISTS idx_status ON cases(status, severity);
"""


def db_open(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def fingerprint(rule_id: str, ln: Line) -> str:
    return hashlib.sha256(f"{rule_id}|{ln.source}|{ln.sku}".encode()).hexdigest()[:16]


def record(con: sqlite3.Connection, ln: Line, hits: list[Rule], now: str) -> bool:
    """Upsert one case. Returns True if newly opened."""
    primary, secondary = hits[0], [r.id for r in hits[1:]]
    fp = fingerprint(primary.id, ln)
    # money lost is attributed whenever a zero/negative price is present, even when
    # a cause rule (DISCOUNT_GE_BASE) outranks it as primary
    risk = 0
    if any(r.id in ("ZERO_PRICE", "NEGATIVE_PRICE") for r in hits) and ln.original:
        risk = max(ln.original - (ln.price or 0), 0)
    row = con.execute("SELECT id, status FROM cases WHERE fingerprint=?", (fp,)).fetchone()
    if row:
        con.execute(
            """UPDATE cases SET occurrences=occurrences+1, last_seen=?, price=?, original=?,
                   discount=?, unit_price=?, secondary=?, value_at_risk=value_at_risk+?,
                   status=CASE WHEN status IN ('resolved','fixed') THEN 'reopened' ELSE status END
               WHERE id=?""",
            (now, ln.price, ln.original, ln.resolved_discount(), ln.unit_price,
             ",".join(secondary), risk, row["id"]))
        return False
    con.execute(
        """INSERT INTO cases (fingerprint, rule_id, secondary, severity, source, sku, title, url,
               price, original, discount, unit_size, unit_label, unit_price,
               value_at_risk, occurrences, first_seen, last_seen, status, sample)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,'open',?)""",
        (fp, primary.id, ",".join(secondary), primary.severity, ln.source, ln.sku, ln.title, ln.url,
         ln.price, ln.original, ln.resolved_discount(), ln.unit_size, ln.unit_label, ln.unit_price,
         risk, now, now, json.dumps(ln.raw, ensure_ascii=False, default=str)[:4000]))
    return True


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_scan(args) -> int:
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    con = db_open(args.db)
    fetcher = Fetcher(cfg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen = new = flagged = 0
    new_critical = 0

    print(f"scan started {now}")
    for src in cfg.get("sources", []):
        print(f"\n  {src['name']} [{src.get('type','json')}]")
        for ln in read_source(src, fetcher, args.limit):
            seen += 1
            hits = evaluate(ln)
            if not hits:
                continue
            flagged += 1
            ids = " ".join(r.id for r in hits)
            print(f"    {hits[0].severity:8s} {eur(ln.price):>10s}  {ln.title[:44]:<44s} {ids}")
            if not args.dry_run:
                is_new = record(con, ln, hits, now)
                new += is_new
                if is_new and hits[0].severity == "critical":
                    new_critical += 1

    if not args.dry_run:
        con.execute("INSERT INTO scans (started, finished, lines_seen, cases_new, cases_seen)"
                    " VALUES (?,?,?,?,?)",
                    (now, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     seen, new, flagged))
        con.commit()

    print(f"\n{seen} lines checked · {flagged} flagged · {new} new cases · {new_critical} new critical")
    if args.dry_run:
        print("dry run - nothing written")
    return 2 if new_critical else 0


def _query(con, args):
    sql = "SELECT * FROM cases WHERE 1=1"
    params: list = []
    if getattr(args, "status", None):
        sql += " AND status=?"; params.append(args.status)
    if getattr(args, "severity", None):
        sql += " AND severity=?"; params.append(args.severity)
    if getattr(args, "rule", None):
        sql += " AND rule_id=?"; params.append(args.rule)
    sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, last_seen DESC"
    return con.execute(sql, params).fetchall()


def cmd_cases(args) -> int:
    con = db_open(args.db)
    rows = _query(con, args)
    if not rows:
        print("no cases match")
        return 0
    risk = sum(r["value_at_risk"] for r in rows if r["status"] == "open")
    print(f"{'ID':>4} {'SEV':<9}{'RULE':<24}{'PRICE':>9} {'WAS':>9} {'N':>4}  TITLE")
    print("-" * 100)
    for r in rows:
        print(f"{r['id']:>4} {r['severity']:<9}{r['rule_id']:<24}{eur(r['price']):>9} "
              f"{eur(r['original']):>9} {r['occurrences']:>4}  {r['title'][:38]}")
    print("-" * 100)
    print(f"{len(rows)} cases · value at risk on open cases: {eur(risk)}")
    return 0


def cmd_resolve(args) -> int:
    con = db_open(args.db)
    cur = con.execute("UPDATE cases SET status=?, note=? WHERE id=?",
                      (args.status, args.note, args.id))
    con.commit()
    print(f"case {args.id} -> {args.status}" if cur.rowcount else f"no case {args.id}")
    return 0


def cmd_export(args) -> int:
    con = db_open(args.db)
    rows = [dict(r) for r in _query(con, args)]
    out = Path(args.out)
    if args.format == "json":
        out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["id"])
            w.writeheader(); w.writerows(rows)
    print(f"wrote {len(rows)} cases to {out}")
    return 0


REPORT_CSS = """
:root{--ink:#0C141C;--slab:#131F2A;--slab2:#1A2836;--rule:#26384A;--paper:#E6EDF3;
--muted:#8199AE;--alarm:#E4574C;--signal:#E8A33D;--stamp:#F0C46B;--clear:#4FB49A}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--paper);font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif;padding:0 16px 60px}
.wrap{max-width:900px;margin:0 auto}
header{border-bottom:1px solid var(--rule);padding:24px 0 18px;margin-bottom:22px}
.eyebrow{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--signal)}
h1{font-size:26px;letter-spacing:-.02em;margin:6px 0 4px}
.sub{color:var(--muted);font-size:14px;margin:0}
.meters{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}
.meter{flex:1 1 150px;background:var(--slab);border:1px solid var(--rule);border-radius:4px;padding:12px 14px}
.meter .n{font-family:"IBM Plex Mono",monospace;font-size:24px;font-weight:600}
.meter .l{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-top:6px}
.meter.alarm .n{color:var(--alarm)}.meter.signal .n{color:var(--signal)}
.case{background:var(--slab);border:1px solid var(--rule);border-left:3px solid var(--rule);border-radius:4px;margin-bottom:12px}
.case.critical{border-left-color:var(--alarm)}.case.high{border-left-color:var(--signal)}.case.medium{border-left-color:var(--stamp)}
.head{display:flex;justify-content:space-between;gap:12px;padding:14px 16px 0}
.title{font-weight:600}
.n-occ{font-family:"IBM Plex Mono",monospace;background:var(--slab2);border-radius:4px;padding:6px 12px;height:fit-content}
.tag{padding:6px 16px 14px}
.now{font-family:"IBM Plex Mono",monospace;font-size:24px;font-weight:600}
.now.zero{color:var(--alarm)}
.was{font-family:"IBM Plex Mono",monospace;font-size:15px;color:var(--muted);text-decoration:line-through;margin-left:10px}
.unit{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted);margin-top:4px}
.unit .miss{color:var(--alarm)}
.flags{border-top:1px dashed var(--rule);padding:12px 16px;display:flex;flex-wrap:wrap;gap:6px}
.flag{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;border:1px solid currentColor;border-radius:2px;padding:4px 7px}
.flag.critical{color:var(--alarm)}.flag.high{color:var(--signal)}.flag.medium{color:var(--stamp)}
.note{width:100%;font-size:12px;color:var(--muted);margin-top:4px}
.foot{padding:9px 16px;border-top:1px solid var(--rule);background:var(--slab2);font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.empty{border:1px dashed var(--rule);border-radius:4px;padding:40px;text-align:center;color:var(--muted)}
"""


def cmd_report(args) -> int:
    con = db_open(args.db)
    rows = _query(con, args)
    hints = {r.id: r.hint for r in RULES}
    sev_of = {r.id: r.severity for r in RULES}
    openrows = [r for r in rows if r["status"] == "open"]
    risk = sum(r["value_at_risk"] for r in openrows)
    crit = sum(1 for r in openrows if r["severity"] == "critical")
    scan = con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()

    def block(r):
        sec = [s for s in (r["secondary"] or "").split(",") if s]
        flags = "".join(f'<span class="flag {sev_of.get(x,"medium")}">{x}</span>'
                        for x in [r["rule_id"], *sec])
        note = hints.get(r["rule_id"], "")
        unit = ""
        if r["unit_size"]:
            up = (f'{eur(r["unit_price"])} / {r["unit_label"] or "unit"}' if r["unit_price"] is not None
                  else '<span class="miss">no unit price shown</span>')
            unit = f'<div class="unit">{r["unit_size"]}{r["unit_label"] or ""} · {up}</div>'
        was = f'<span class="was">{eur(r["original"])}</span>' if r["original"] is not None else ""
        zero = " zero" if r["price"] == 0 else ""
        return f"""<article class="case {r['severity']}">
  <div class="head"><div class="title">{r['title']}</div><div class="n-occ">{r['occurrences']}×</div></div>
  <div class="tag"><span class="now{zero}">{eur(r['price'])}</span>{was}{unit}
    <div class="unit">sku {r['sku']} · {r['source']}</div></div>
  <div class="flags">{flags}<div class="note">{note}</div></div>
  <div class="foot">{r['status']} · first seen {r['first_seen'][:10]} · last seen {r['last_seen'][:10]}
   · at risk {eur(r['value_at_risk'])}</div>
</article>"""

    body = "".join(block(r) for r in rows) or '<div class="empty">No cases. Rules ran clean.</div>'
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Price integrity report</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>{REPORT_CSS}</style></head><body><div class="wrap">
<header><div class="eyebrow">Price integrity · automated scan</div>
<h1>Defect report</h1>
<p class="sub">{scan['finished'] if scan else '—'} · {scan['lines_seen'] if scan else 0} lines checked</p>
<div class="meters">
  <div class="meter alarm"><div class="n">{crit}</div><div class="l">Open critical</div></div>
  <div class="meter signal"><div class="n">{eur(risk)}</div><div class="l">Value at risk</div></div>
  <div class="meter"><div class="n">{len(openrows)}</div><div class="l">Open cases</div></div>
</div></header>{body}</div></body></html>"""
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"wrote {args.out} ({len(rows)} cases)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="pricewatch", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DB_DEFAULT)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="crawl sources and record defects")
    s.add_argument("--config", required=True)
    s.add_argument("--limit", type=int, help="max items per source")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_scan)

    c = sub.add_parser("cases", help="list tracked cases")
    c.add_argument("--status"); c.add_argument("--severity"); c.add_argument("--rule")
    c.set_defaults(fn=cmd_cases)

    r = sub.add_parser("report", help="write an HTML report")
    r.add_argument("--out", default="report.html")
    r.add_argument("--status"); r.add_argument("--severity"); r.add_argument("--rule")
    r.set_defaults(fn=cmd_report)

    e = sub.add_parser("export", help="export cases")
    e.add_argument("--format", choices=["csv", "json"], default="csv")
    e.add_argument("--out", required=True)
    e.add_argument("--status"); e.add_argument("--severity"); e.add_argument("--rule")
    e.set_defaults(fn=cmd_export)

    v = sub.add_parser("resolve", help="change a case status")
    v.add_argument("id", type=int)
    v.add_argument("--status", default="fixed",
                   choices=["open", "acknowledged", "mitigated", "fixed", "suppressed"])
    v.add_argument("--note", default="")
    v.set_defaults(fn=cmd_resolve)

    args = p.parse_args()
    try:
        return args.fn(args)
    except FileNotFoundError as ex:
        print(f"error: {ex}", file=sys.stderr); return 1
    except Exception as ex:  # noqa: BLE001
        print(f"error: {type(ex).__name__}: {ex}", file=sys.stderr); return 1


if __name__ == "__main__":
    sys.exit(main())
