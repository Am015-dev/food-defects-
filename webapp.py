"""Dashboard: pricing bugs and verified deals across several Masoutis
storefronts on e-food.gr, backed by daily snapshots in the database
(see ingest.py / db.py), with historical and cross-shop comparison views.
"""

import csv
import io
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from flask import Flask, Response, abort, jsonify, render_template_string, request

from db import SessionLocal, Snapshot, init_db
from queries import (
    compare_across_shops,
    get_flagged_items,
    get_history,
    get_latest_snapshot,
    iter_all_sales,
)
from shops import SHOPS

app = Flask(__name__)
try:
    init_db()
except Exception as exc:  # noqa: BLE001
    # Don't let a still-unreachable database at boot time take the whole
    # process down -- /healthz should still come up, and DB-backed routes
    # recover on their own once the database is reachable.
    print(f"WARNING: init_db() failed at startup, continuing anyway: {exc}")

SHOP_LABELS = {s["id"]: s["label"] for s in SHOPS}


# --- Self-population -------------------------------------------------
#
# The dashboard keeps itself current without any external scheduler.
# Render's free tier has no cron service, so when a page load notices
# today's snapshots are missing it kicks off a background refresh.
#
# The refresh walks the shops STRICTLY ONE AT A TIME. That constraint is
# the whole point: fetching all 12 catalogs at once peaked at ~460MB and
# repeatedly got this 512MB instance OOM-killed, whereas sequential
# fetching measures around 160MB. Each shop is committed as it finishes,
# so if the worker is recycled or the instance sleeps mid-refresh, the
# work already done persists and the next page load resumes the rest.

_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "done": 0, "total": len(SHOPS), "error": None}
_last_refresh_attempt = None

# Don't re-attempt more often than this. Without it, a shop that keeps
# failing would be re-fetched on every page load (and the page
# auto-reloads while a refresh is in flight), which would mean hammering
# e-food's API. This is meant to be a low-frequency, personal-use tool.
REFRESH_COOLDOWN_SECONDS = 600


def refresh_status():
    with _refresh_lock:
        return dict(_refresh_state)


def _shops_needing_refresh():
    """Shops with no snapshot for today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session = SessionLocal()
    try:
        fresh_ids = {
            row[0]
            for row in session.query(Snapshot.shop_id).filter(
                Snapshot.snapshot_date == today
            )
        }
    finally:
        session.close()
    return [s for s in SHOPS if s["id"] not in fresh_ids]


def _run_refresh(pending):
    from ingest import fetch_and_store_shop  # lazy: keeps request path light

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for shop in pending:
        try:
            result = fetch_and_store_shop(shop["id"], shop["label"], today)
            if not result.get("ok"):
                print(f"refresh: {shop['label']} failed: {result.get('error')}")
        except Exception as exc:  # noqa: BLE001 - one shop must not stop the rest
            print(f"refresh: {shop['label']} raised: {exc}")
            with _refresh_lock:
                _refresh_state["error"] = str(exc)
        with _refresh_lock:
            _refresh_state["done"] += 1
    with _refresh_lock:
        _refresh_state["running"] = False
    print("refresh: finished")


def maybe_start_refresh():
    """Start a background refresh if today's data is incomplete, one
    isn't already in flight, and we're outside the cooldown. Returns True
    if a refresh is running."""
    global _last_refresh_attempt

    now = datetime.now(timezone.utc)
    with _refresh_lock:
        if _refresh_state["running"]:
            return True
        if (
            _last_refresh_attempt is not None
            and (now - _last_refresh_attempt).total_seconds() < REFRESH_COOLDOWN_SECONDS
        ):
            return False

    try:
        pending = _shops_needing_refresh()
    except Exception as exc:  # noqa: BLE001 - never let this break a page render
        print(f"refresh: could not determine staleness: {exc}")
        return False

    if not pending:
        return False

    with _refresh_lock:
        if _refresh_state["running"]:
            return True
        _last_refresh_attempt = now
        _refresh_state.update(running=True, done=0, total=len(pending), error=None)

    threading.Thread(target=_run_refresh, args=(pending,), daemon=True).start()
    print(f"refresh: started for {len(pending)} shop(s)")
    return True


def view_from_snapshot(session, shop_id, label, snapshot):
    # Only the bug/deal rows -- a shop's full catalog can be 10,000+ rows,
    # and loading all of that as ORM objects just to filter down to a
    # handful in Python is what was pushing memory over Render's 512MB cap.
    items = get_flagged_items(session, snapshot.id)
    return {
        "ok": True,
        "id": shop_id,
        "label": label,
        "source": "db",
        "snapshot_date": snapshot.snapshot_date,
        "title": snapshot.store_title,
        "address": snapshot.store_address,
        "is_open": snapshot.is_open,
        "total_items": snapshot.total_items,
        "total_categories": snapshot.total_categories,
        "zero_price_bugs": [
            {"name": it.name, "category": it.category} for it in items if it.is_zero_price_bug
        ],
        "placeholder_bugs": [
            {"name": it.name, "category": it.category, "price": it.price}
            for it in items
            if it.is_placeholder_bug
        ],
        "verified_deals": sorted(
            (
                {
                    "name": it.name,
                    "category": it.category,
                    "price": it.price,
                    "full_price": it.full_price,
                    "pct": it.deal_pct,
                }
                for it in items
                if it.is_verified_deal
            ),
            key=lambda d: d["pct"],
            reverse=True,
        ),
    }


def empty_view(shop_id, label):
    """Shown when a shop has no stored snapshot yet.

    This deliberately does NOT fall back to fetching the shop's catalog
    live. Doing that used up ~460MB on a single request with an empty
    database (12 shops x a ~15MB JSON catalog, several times that once
    parsed, all fetched concurrently) and is what kept getting this
    service OOM-killed on a 512MB instance. The web service now only
    ever reads pre-digested rows out of the database; fetching catalogs
    is the ingest job's business, and it runs elsewhere.
    """
    return {
        "ok": True,
        "no_data": True,
        "id": shop_id,
        "label": label,
        "source": "none",
        "snapshot_date": None,
        "title": label,
        "address": None,
        "is_open": None,
        "total_items": 0,
        "total_categories": 0,
        "zero_price_bugs": [],
        "placeholder_bugs": [],
        "verified_deals": [],
    }


def get_shop_view(shop_id, label):
    # Each call gets its own session -- SQLAlchemy sessions aren't
    # thread-safe, and this runs concurrently across shops.
    session = SessionLocal()
    try:
        snapshot = get_latest_snapshot(session, shop_id)
        if snapshot is None:
            return empty_view(shop_id, label)
        return view_from_snapshot(session, shop_id, label, snapshot)
    finally:
        session.close()


def get_all_shop_views():
    # Bounded pool: these are small indexed reads now, but one DB
    # connection per shop simultaneously is still needless pressure on a
    # free-tier Postgres connection limit.
    results = [None] * len(SHOPS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_to_index = {
            pool.submit(get_shop_view, shop["id"], shop["label"]): i
            for i, shop in enumerate(SHOPS)
        }
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()
    return results


BASE_STYLE = """
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #262b36;
    --text: #e8eaed; --muted: #9aa4b2;
    --bad: #ff6b6b; --bad-bg: #2a1518;
    --warn: #ffb454; --warn-bg: #2a2015;
    --good: #4ade80; --good-bg: #12261a;
    --accent: #7dd3fc;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header { padding: 28px 24px 8px; max-width: 1100px; margin: 0 auto; }
  header h1 { margin: 0 0 6px; font-size: 22px; }
  header p { margin: 0; color: var(--muted); font-size: 13.5px; }
  nav { max-width: 1100px; margin: 16px auto 0; padding: 0 24px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  nav a {
    color: var(--accent); text-decoration: none; font-size: 13px;
    border: 1px solid var(--border); padding: 5px 10px; border-radius: 999px;
  }
  nav a:hover { border-color: var(--accent); }
  nav .sep { color: var(--border); }
  main { max-width: 1100px; margin: 0 auto; padding: 16px 24px 60px; }
  .shop, .panel {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px 22px; margin: 18px 0;
  }
  .shop h2, .panel h2 { margin: 0 0 2px; font-size: 18px; }
  .panel > p.meta { margin: 0 0 12px; color: var(--muted); font-size: 13px; }
  .btn { display: inline-block; background: var(--good-bg); color: var(--good); border: 1px solid var(--good);
    padding: 8px 14px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 13.5px; }
  .btn:hover { filter: brightness(1.15); }
  .shop .meta { color: var(--muted); font-size: 13px; margin-bottom: 14px; }
  .stats { display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 14px; font-size: 13px; color: var(--muted); }
  .stats b { color: var(--text); }
  .error { background: var(--bad-bg); border: 1px solid var(--bad); color: var(--bad);
    padding: 10px 12px; border-radius: 8px; font-size: 13.5px; }
  .source-tag { display: inline-block; font-size: 11px; color: var(--muted); border: 1px solid var(--border);
    border-radius: 999px; padding: 1px 8px; margin-left: 6px; }
  .section { margin-top: 14px; }
  .section h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
    margin: 0 0 8px; color: var(--muted); }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .badge.bad { background: var(--bad-bg); color: var(--bad); }
  .badge.warn { background: var(--warn-bg); color: var(--warn); }
  .badge.good { background: var(--good-bg); color: var(--good); }
  ul.items { list-style: none; margin: 0; padding: 0; }
  ul.items li { padding: 6px 0; border-top: 1px solid var(--border); font-size: 13.5px; }
  ul.items li:first-child { border-top: none; }
  .item-cat { color: var(--muted); font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  table th { text-align: left; color: var(--muted); font-weight: 500; font-size: 12px;
    padding: 4px 8px; border-bottom: 1px solid var(--border); }
  table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
  table tr:last-child td { border-bottom: none; }
  .pct { color: var(--good); font-weight: 700; }
  .price-was { color: var(--muted); text-decoration: line-through; }
  .price-now { font-weight: 700; }
  .empty { color: var(--muted); font-size: 13px; font-style: italic; }
  footer { max-width: 1100px; margin: 0 auto; padding: 0 24px 40px; color: var(--muted); font-size: 12px; }
  footer a { color: var(--accent); }
</style>
"""

NAV = """
<nav>
  <a href="/">Dashboard</a>
  <a href="/compare">Compare across shops</a>
  <span class="sep">|</span>
  {% for s in shop_nav %}
  <a href="/history/{{ s.id }}">{{ s.label }} history</a>
  {% endfor %}
</nav>
"""

DASHBOARD_TEMPLATE = (
    """
<!doctype html>
<html lang="el">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Masoutis price-defect dashboard</title>
"""
    + BASE_STYLE
    + """
</head>
<body>
<header>
  <h1>\U0001F6D2 Masoutis price-defect dashboard</h1>
  <p>Backed by daily snapshots across {{ shops|length }} storefronts. Shops with no saved snapshot yet show live data instead.</p>
</header>
"""
    + NAV
    + """
<main>
  {% if refresh.running %}
  <section class="panel" style="border-color: var(--warn);">
    <h2>⏳ Fetching fresh data&hellip;</h2>
    <p class="meta">
      {{ refresh.done }} of {{ refresh.total }} shops done. Shops are fetched one at a
      time to stay inside this instance's memory limit, so a full refresh takes a
      minute or two. This page reloads itself until it's finished.
    </p>
  </section>
  <meta http-equiv="refresh" content="15">
  {% endif %}

  <section class="panel" id="bugs-summary">
    <h2>\U0001F41B Bugs found right now</h2>
    <p class="meta">Across all {{ shops|length }} shops' latest saved snapshot.</p>

    <div class="section">
      <h3><span class="badge bad">Bug</span> Zero-priced listings ({{ bugs.zero_price|length }})</h3>
      {% if bugs.zero_price %}
      <ul class="items">
        {% for it in bugs.zero_price %}
        <li>{{ it.name }} <span class="item-cat">&middot; {{ it.shop_label }} &middot; {{ it.category }}</span></li>
        {% endfor %}
      </ul>
      {% else %}
      <div class="empty">None found.</div>
      {% endif %}
    </div>

    <div class="section">
      <h3><span class="badge warn">Bug</span> Placeholder 30-day-low reference price (&euro;0.01) ({{ bugs.placeholder|length }})</h3>
      {% if bugs.placeholder %}
      <ul class="items">
        {% for it in bugs.placeholder[:20] %}
        <li>{{ it.name }} <span class="item-cat">&middot; {{ it.shop_label }} &middot; {{ it.category }} &middot; now {{ "%.2f"|format(it.price) }}&euro;</span></li>
        {% endfor %}
      </ul>
      {% if bugs.placeholder|length > 20 %}
      <div class="empty">&hellip; and {{ bugs.placeholder|length - 20 }} more</div>
      {% endif %}
      {% else %}
      <div class="empty">None found.</div>
      {% endif %}
    </div>
  </section>

  <section class="panel" id="bargains">
    <h2>\U0001F4B0 Really good bargains right now</h2>
    <p class="meta">Fixed-size items verified 20%+ below their real 30-day low (not just the "was" price), ranked across all shops.</p>
    {% if bargains %}
    <table>
      <tr><th>Item</th><th>Shop</th><th>Category</th><th>Now</th><th>Was</th><th>Below 30d-low</th></tr>
      {% for d in bargains %}
      <tr>
        <td>{{ d.name }}</td>
        <td>{{ d.shop_label }}</td>
        <td class="item-cat">{{ d.category }}</td>
        <td class="price-now">{{ "%.2f"|format(d.price) }}&euro;</td>
        <td class="price-was">{{ "%.2f"|format(d.full_price) }}&euro;</td>
        <td class="pct">-{{ "%.0f"|format(d.pct) }}%</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">None found yet.</div>
    {% endif %}
  </section>

  <section class="panel" id="export">
    <h2>\U0001F4E5 Export</h2>
    <p class="meta">Every item currently showing a discount badge, across all shops -- broader than the verified bargains above, with both the official % off and the % vs the real 30-day low so you can judge which are genuine.</p>
    <a class="btn" href="/download/sales.csv">Download all sale cases (CSV)</a>
  </section>

  {% for r in shops %}
  <section class="shop" id="shop-{{ r.id }}">
    {% if r.no_data %}
      <h2>{{ r.label }} <span class="source-tag">no data yet</span></h2>
      <div class="empty">No snapshot stored for this shop yet. Run the "Daily catalog ingest" workflow to populate it.</div>
    {% elif r.ok %}
      <h2>{{ r.title }} <span class="meta">&mdash; {{ r.label }}</span>
        <span class="source-tag">{{ r.snapshot_date }}</span>
      </h2>
      <div class="meta">{{ r.address }} &middot; {{ "Open now" if r.is_open else "Closed" }}</div>
      <div class="stats">
        <span><b>{{ r.total_categories }}</b> categories</span>
        <span><b>{{ r.total_items }}</b> products</span>
        <span><b>{{ r.zero_price_bugs|length }}</b> zero-price bugs</span>
        <span><b>{{ r.placeholder_bugs|length }}</b> placeholder-price bugs</span>
        <span><b>{{ r.verified_deals|length }}</b> verified deals &ge;20%</span>
      </div>

      <div class="section">
        <h3><span class="badge bad">Bug</span> Zero-priced listings</h3>
        {% if r.zero_price_bugs %}
        <ul class="items">
          {% for it in r.zero_price_bugs %}
          <li>{{ it.name }} <span class="item-cat">&middot; {{ it.category }}</span></li>
          {% endfor %}
        </ul>
        {% else %}
        <div class="empty">None found.</div>
        {% endif %}
      </div>

      <div class="section">
        <h3><span class="badge warn">Bug</span> Placeholder 30-day-low reference price (&euro;0.01)</h3>
        {% if r.placeholder_bugs %}
        <ul class="items">
          {% for it in r.placeholder_bugs[:10] %}
          <li>{{ it.name }} <span class="item-cat">&middot; {{ it.category }} &middot; now {{ "%.2f"|format(it.price) }}&euro;</span></li>
          {% endfor %}
        </ul>
        {% if r.placeholder_bugs|length > 10 %}
        <div class="empty">&hellip; and {{ r.placeholder_bugs|length - 10 }} more</div>
        {% endif %}
        {% else %}
        <div class="empty">None found.</div>
        {% endif %}
      </div>

      <div class="section">
        <h3><span class="badge good">Verified deal</span> Genuinely priced below the real 30-day low</h3>
        {% if r.verified_deals %}
        <table>
          <tr><th>Item</th><th>Category</th><th>Now</th><th>Was</th><th>Below 30d-low</th></tr>
          {% for d in r.verified_deals[:15] %}
          <tr>
            <td>{{ d.name }}</td>
            <td class="item-cat">{{ d.category }}</td>
            <td class="price-now">{{ "%.2f"|format(d.price) }}&euro;</td>
            <td class="price-was">{{ "%.2f"|format(d.full_price) }}&euro;</td>
            <td class="pct">-{{ "%.0f"|format(d.pct) }}%</td>
          </tr>
          {% endfor %}
        </table>
        {% if r.verified_deals|length > 15 %}
        <div class="empty">&hellip; and {{ r.verified_deals|length - 15 }} more</div>
        {% endif %}
        {% else %}
        <div class="empty">None found.</div>
        {% endif %}
      </div>
    {% else %}
      <h2>{{ r.label }}</h2>
      <div class="error">Failed to fetch this store: {{ r.error }}</div>
    {% endif %}
  </section>
  {% endfor %}
</main>
<footer>
  Personal price-tracking tool against e-food.gr's public consumer API. Not affiliated with e-food or Masoutis.
  Source: <a href="https://github.com/am015-dev/food-defects-">food-defects-</a>
</footer>
</body>
</html>
"""
)

HISTORY_TEMPLATE = (
    """
<!doctype html>
<html lang="el">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ label }} - history</title>
"""
    + BASE_STYLE
    + """
</head>
<body>
<header>
  <h1>{{ label }} &mdash; daily history</h1>
  <p>One row per day this shop was ingested.</p>
</header>
"""
    + NAV
    + """
<main>
  <div class="panel">
    {% if rows %}
    <table>
      <tr><th>Date</th><th>Products</th><th>Categories</th><th>Zero-price bugs</th><th>Placeholder-price bugs</th><th>Verified deals</th></tr>
      {% for s in rows %}
      <tr>
        <td>{{ s.snapshot_date }}</td>
        <td>{{ s.total_items }}</td>
        <td>{{ s.total_categories }}</td>
        <td>{{ s.zero_price_bug_count }}</td>
        <td>{{ s.placeholder_bug_count }}</td>
        <td>{{ s.verified_deal_count }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">No snapshots stored yet for this shop. Run the "Daily catalog ingest" workflow from the repository's Actions tab to populate it.</div>
    {% endif %}
  </div>
</main>
<footer>
  Source: <a href="https://github.com/am015-dev/food-defects-">food-defects-</a>
</footer>
</body>
</html>
"""
)

COMPARE_TEMPLATE = (
    """
<!doctype html>
<html lang="el">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Compare across shops</title>
"""
    + BASE_STYLE
    + """
</head>
<body>
<header>
  <h1>Same product, different price</h1>
  <p>Products found (by exact name match) in at least 2 shops' latest snapshot, where price differs by 5%+ between the cheapest and priciest branch.</p>
</header>
"""
    + NAV
    + """
<main>
  <div class="panel">
    {% if groups %}
    {% for g in groups[:100] %}
    <div class="section">
      <h3>{{ g.name }} <span class="item-cat">&middot; {{ g.category }}</span>
        <span class="pct" style="margin-left:8px">+{{ "%.0f"|format(g.spread_pct) }}% spread</span>
      </h3>
      <table>
        <tr><th>Shop</th><th>Price</th></tr>
        {% for row in g.rows %}
        <tr>
          <td>{{ row.shop_label }}</td>
          <td class="{{ 'price-now' if row.price == g.low else '' }}">{{ "%.2f"|format(row.price) }}&euro;</td>
        </tr>
        {% endfor %}
      </table>
    </div>
    {% endfor %}
    {% else %}
    <div class="empty">Not enough saved snapshots yet across shops to compare. Run the "Daily catalog ingest" workflow from the repository's Actions tab first.</div>
    {% endif %}
  </div>
</main>
<footer>
  Source: <a href="https://github.com/am015-dev/food-defects-">food-defects-</a>
</footer>
</body>
</html>
"""
)


@app.route("/")
def dashboard():
    # Top up today's data in the background if it's missing or partial.
    maybe_start_refresh()

    # Each shop's items are already fetched once here; derive the
    # cross-shop bugs/bargains summaries from that instead of re-querying
    # the database per shop again (that redundancy was the dashboard's
    # main cost once tracking more than a handful of shops).
    results = get_all_shop_views()
    ok_results = [r for r in results if r["ok"]]

    bugs = {
        "zero_price": [
            {**it, "shop_label": r["label"]} for r in ok_results for it in r["zero_price_bugs"]
        ],
        "placeholder": [
            {**it, "shop_label": r["label"]} for r in ok_results for it in r["placeholder_bugs"]
        ],
    }
    bargains = sorted(
        ({**d, "shop_label": r["label"]} for r in ok_results for d in r["verified_deals"]),
        key=lambda d: d["pct"],
        reverse=True,
    )[:30]

    return render_template_string(
        DASHBOARD_TEMPLATE,
        shops=results,
        bugs=bugs,
        bargains=bargains,
        shop_nav=SHOPS,
        refresh=refresh_status(),
    )


@app.route("/refresh", methods=["POST", "GET"])
def refresh():
    """Kick off (or report on) the background data refresh."""
    running = maybe_start_refresh()
    status = refresh_status()
    status["running"] = running or status["running"]
    return jsonify(status)


CSV_HEADER = [
    "shop",
    "category",
    "product",
    "price",
    "full_price",
    "pct_off_full_price",
    "30_day_low_price",
    "pct_below_30_day_low",
    "verified_real_deal",
    "snapshot_date",
]


@app.route("/download/sales.csv")
def download_sales_csv():
    shop_id = request.args.get("shop_id", type=int)

    def generate():
        # Streamed a row at a time: the full export is ~10k rows / ~3MB,
        # and buffering all of it as one string before responding is
        # memory this instance doesn't have to spare.
        buf = io.StringIO()
        writer = csv.writer(buf)

        def flush():
            value = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            return value

        writer.writerow(CSV_HEADER)
        yield flush()

        session = SessionLocal()
        try:
            for r in iter_all_sales(session, SHOP_LABELS, shop_id=shop_id):
                writer.writerow(
                    [
                        r["shop_label"],
                        r["category"],
                        r["name"],
                        f'{r["price"]:.2f}',
                        f'{r["full_price"]:.2f}',
                        f'{r["pct_off_full"]:.1f}',
                        f'{r["l30d_price"]:.2f}' if r["l30d_price"] else "",
                        f'{r["pct_vs_l30d"]:.1f}' if r["pct_vs_l30d"] is not None else "",
                        "yes" if r["is_verified_deal"] else "no",
                        r["snapshot_date"],
                    ]
                )
                yield flush()
        finally:
            session.close()

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=masoutis_sale_cases.csv"},
    )


@app.route("/history/<int:shop_id>")
def history(shop_id):
    if shop_id not in SHOP_LABELS:
        abort(404)
    session = SessionLocal()
    try:
        rows = get_history(session, shop_id)
    finally:
        session.close()
    return render_template_string(
        HISTORY_TEMPLATE, label=SHOP_LABELS[shop_id], rows=rows, shop_nav=SHOPS
    )


@app.route("/compare")
def compare():
    session = SessionLocal()
    try:
        groups = compare_across_shops(session, SHOP_LABELS)
    finally:
        session.close()
    return render_template_string(COMPARE_TEMPLATE, groups=groups, shop_nav=SHOPS)


# NOTE: ingestion and the bug-report email deliberately do NOT live here
# as HTTP endpoints any more. Both need to hold whole shop catalogs in
# memory, which repeatedly OOM-killed this 512MB service. They now run as
# plain scripts on GitHub Actions runners (see .github/workflows/), which
# have gigabytes of RAM and connect straight to the database. This web
# service stays read-only and small.


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
