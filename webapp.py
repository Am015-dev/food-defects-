"""Dashboard: pricing bugs and verified deals across several Masoutis
storefronts on e-food.gr, backed by daily snapshots in the database
(see ingest.py / db.py), with historical and cross-shop comparison views.
"""

import csv
import hmac
import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, Response, abort, jsonify, render_template_string, request

from db import SessionLocal, init_db
from efood_client import fetch_restaurant
from price_analysis import analyze
from queries import (
    compare_across_shops,
    get_all_sales,
    get_flagged_items,
    get_history,
    get_latest_snapshot,
)
from shops import SHOPS

app = Flask(__name__)
try:
    init_db()
except Exception as exc:  # noqa: BLE001
    # Don't let a still-unreachable database at boot time take the whole
    # process down -- /healthz should still come up, and DB-backed routes
    # will surface their own errors (or fall back to a live fetch, for
    # the per-shop dashboard cards) once the database is reachable.
    print(f"WARNING: init_db() failed at startup, continuing anyway: {exc}")

SHOP_LABELS = {s["id"]: s["label"] for s in SHOPS}
INGEST_SECRET = os.environ.get("INGEST_SECRET", "")


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


def view_from_live_fetch(shop_id, label):
    try:
        data = fetch_restaurant(shop_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "id": shop_id, "label": label, "error": str(exc)}

    a = analyze(data)
    info = a["information"]
    return {
        "ok": True,
        "id": shop_id,
        "label": label,
        "source": "live",
        "snapshot_date": None,
        "title": info["title"],
        "address": info["address"]["description"],
        "is_open": info["is_open"],
        "total_items": a["total_items"],
        "total_categories": a["total_categories"],
        "zero_price_bugs": [
            {"name": it["name"], "category": it["_category"]} for it in a["zero_price_bugs"]
        ],
        "placeholder_bugs": [
            {"name": it["name"], "category": it["_category"], "price": it["price"]}
            for it in a["placeholder_reference_bugs"]
        ],
        "verified_deals": [
            {
                "name": d["item"]["name"],
                "category": d["item"]["_category"],
                "price": d["item"]["price"],
                "full_price": d["item"]["full_price"],
                "pct": d["pct"],
            }
            for d in a["verified_deals"]
        ],
    }


def get_shop_view(shop_id, label):
    # Each call gets its own session -- SQLAlchemy sessions aren't
    # thread-safe, and this runs concurrently across shops.
    session = SessionLocal()
    try:
        snapshot = get_latest_snapshot(session, shop_id)
        if snapshot is not None:
            return view_from_snapshot(session, shop_id, label, snapshot)
        return view_from_live_fetch(shop_id, label)
    finally:
        session.close()


def get_all_shop_views():
    results = [None] * len(SHOPS)
    with ThreadPoolExecutor(max_workers=len(SHOPS)) as pool:
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
    {% if r.ok %}
      <h2>{{ r.title }} <span class="meta">&mdash; {{ r.label }}</span>
        <span class="source-tag">{{ r.snapshot_date if r.source == 'db' else 'live, unsaved' }}</span>
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
    <div class="empty">No snapshots stored yet for this shop -- the daily ingest job hasn't run yet. Run <code>python ingest.py</code> to backfill one now.</div>
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
    <div class="empty">Not enough saved snapshots yet across shops to compare. Run <code>python ingest.py</code> first.</div>
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
        DASHBOARD_TEMPLATE, shops=results, bugs=bugs, bargains=bargains, shop_nav=SHOPS
    )


@app.route("/download/sales.csv")
def download_sales_csv():
    shop_id = request.args.get("shop_id", type=int)
    session = SessionLocal()
    try:
        rows = get_all_sales(session, SHOP_LABELS, shop_id=shop_id)
    finally:
        session.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
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
    )
    for r in rows:
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

    return Response(
        buf.getvalue(),
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


@app.route("/ingest", methods=["POST"])
def trigger_ingest():
    """Runs the daily snapshot fetch. Called by the scheduled GitHub
    Actions workflow (Render's free tier has no free cron job type), so
    it's guarded by a shared secret rather than being open to the world.
    """
    provided = request.headers.get("X-Ingest-Secret", "")
    if not INGEST_SECRET or not hmac.compare_digest(provided, INGEST_SECRET):
        abort(403)

    from ingest import run_ingestion  # imported lazily: pulls in fetch/DB deps only when needed

    summary = run_ingestion()
    return jsonify(summary)


@app.route("/notify/bugs", methods=["POST"])
def notify_bugs():
    """Emails a digest of every bug (zero-price, placeholder 30-day-low)
    currently found across all tracked shops. Manually triggered only --
    see .github/workflows/notify-bugs.yml -- guarded by the same shared
    secret as /ingest.
    """
    provided = request.headers.get("X-Ingest-Secret", "")
    if not INGEST_SECRET or not hmac.compare_digest(provided, INGEST_SECRET):
        abort(403)

    results = get_all_shop_views()
    bugs_by_shop = [
        {"label": r["label"], "zero_price": r["zero_price_bugs"], "placeholder": r["placeholder_bugs"]}
        for r in results
        if r["ok"]
    ]

    from notify import send_bug_email  # imported lazily: pulls in smtplib config only when needed

    try:
        summary = send_bug_email(bugs_by_shop)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, **summary})


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
