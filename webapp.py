"""Dashboard: pricing bugs and verified deals across several Masoutis
storefronts on e-food.gr, refreshed periodically.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template_string

from efood_client import fetch_restaurant
from price_analysis import analyze
from shops import SHOPS

app = Flask(__name__)

CACHE_TTL_SECONDS = 30 * 60
_cache = {}  # shop_id -> {"result": {...}, "fetched_at": float}


def get_shop_result(shop_id, label):
    cached = _cache.get(shop_id)
    if cached and (time.time() - cached["fetched_at"]) < CACHE_TTL_SECONDS:
        return cached["result"]

    try:
        data = fetch_restaurant(shop_id)
        result = {"ok": True, "label": label, "id": shop_id, **analyze(data)}
    except Exception as exc:  # noqa: BLE001 - surface any fetch/parse failure in the UI
        result = {"ok": False, "label": label, "id": shop_id, "error": str(exc)}

    _cache[shop_id] = {"result": result, "fetched_at": time.time()}
    return result


def get_all_results():
    results = [None] * len(SHOPS)
    with ThreadPoolExecutor(max_workers=len(SHOPS)) as pool:
        future_to_index = {
            pool.submit(get_shop_result, shop["id"], shop["label"]): i
            for i, shop in enumerate(SHOPS)
        }
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()
    return results


TEMPLATE = """
<!doctype html>
<html lang="el">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Masoutis price-defect dashboard</title>
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
  nav { max-width: 1100px; margin: 16px auto 0; padding: 0 24px; display: flex; flex-wrap: wrap; gap: 8px; }
  nav a {
    color: var(--accent); text-decoration: none; font-size: 13px;
    border: 1px solid var(--border); padding: 5px 10px; border-radius: 999px;
  }
  nav a:hover { border-color: var(--accent); }
  main { max-width: 1100px; margin: 0 auto; padding: 16px 24px 60px; }
  .shop {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px 22px; margin: 18px 0;
  }
  .shop h2 { margin: 0 0 2px; font-size: 18px; }
  .shop .meta { color: var(--muted); font-size: 13px; margin-bottom: 14px; }
  .stats { display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 14px; font-size: 13px; color: var(--muted); }
  .stats b { color: var(--text); }
  .error { background: var(--bad-bg); border: 1px solid var(--bad); color: var(--bad);
    padding: 10px 12px; border-radius: 8px; font-size: 13.5px; }
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
</head>
<body>
<header>
  <h1>🛒 Masoutis price-defect dashboard</h1>
  <p>Live from e-food.gr's public catalog API, across {{ shops|length }} storefronts. Cached {{ cache_minutes }} min.</p>
</header>
<nav>
  {% for r in shops %}
  <a href="#shop-{{ r.id }}">{{ r.label }}</a>
  {% endfor %}
</nav>
<main>
  {% for r in shops %}
  <section class="shop" id="shop-{{ r.id }}">
    {% if r.ok %}
      <h2>{{ r.information.title }} <span class="meta">&mdash; {{ r.label }}</span></h2>
      <div class="meta">{{ r.information.address.description }} &middot; {{ "Open now" if r.information.is_open else "Closed" }}</div>
      <div class="stats">
        <span><b>{{ r.total_categories }}</b> categories</span>
        <span><b>{{ r.total_items }}</b> products</span>
        <span><b>{{ r.zero_price_bugs|length }}</b> zero-price bugs</span>
        <span><b>{{ r.placeholder_reference_bugs|length }}</b> placeholder-price bugs</span>
        <span><b>{{ r.verified_deals|length }}</b> verified deals &ge;20%</span>
      </div>

      <div class="section">
        <h3><span class="badge bad">Bug</span> Zero-priced listings</h3>
        {% if r.zero_price_bugs %}
        <ul class="items">
          {% for it in r.zero_price_bugs %}
          <li>{{ it.name }} <span class="item-cat">&middot; {{ it._category }}</span></li>
          {% endfor %}
        </ul>
        {% else %}
        <div class="empty">None found.</div>
        {% endif %}
      </div>

      <div class="section">
        <h3><span class="badge warn">Bug</span> Placeholder 30-day-low reference price (&euro;0.01)</h3>
        {% if r.placeholder_reference_bugs %}
        <ul class="items">
          {% for it in r.placeholder_reference_bugs[:10] %}
          <li>{{ it.name }} <span class="item-cat">&middot; {{ it._category }} &middot; now {{ it.calculated_price }}</span></li>
          {% endfor %}
        </ul>
        {% if r.placeholder_reference_bugs|length > 10 %}
        <div class="empty">&hellip; and {{ r.placeholder_reference_bugs|length - 10 }} more</div>
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
            <td>{{ d.item.name }}</td>
            <td class="item-cat">{{ d.item._category }}</td>
            <td class="price-now">{{ d.item.calculated_price }}</td>
            <td class="price-was">{{ "%.2f"|format(d.item.full_price) }}&euro;</td>
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


@app.route("/")
def dashboard():
    results = get_all_results()
    return render_template_string(
        TEMPLATE, shops=results, cache_minutes=CACHE_TTL_SECONDS // 60
    )


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
