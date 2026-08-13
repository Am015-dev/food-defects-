"""Παρατηρητήριο Τιμών: pricing bugs and verified deals across the
supermarkets around Chalandri on e-food.gr, backed by daily snapshots in
the database (see ingest.py / db.py). Templates live in templates/,
styles in static/ -- this module is routes and filter parsing only.
"""

import csv
import io
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func

from charts import line_chart
from db import ItemPrice, SessionLocal, Snapshot, init_db
from efood_client import fetch_menu_item, menu_item_url
from price_utils import get_price_comparison_info
from queries import (
    compare_across_shops,
    get_categories,
    get_deals_page,
    get_flagged_items_filtered,
    get_history,
    get_item_history_by_code,
    get_latest_snapshot,
    get_latest_snapshots_for_all_shops,
    get_product_across_shops,
    get_trend,
    iter_all_sales,
)
from shops import SHOP_URLS, SHOPS

app = Flask(__name__)
try:
    init_db()
except Exception as exc:  # noqa: BLE001
    # Don't let a still-unreachable database at boot time take the whole
    # process down -- /healthz should still come up, and DB-backed routes
    # recover on their own once the database is reachable.
    print(f"WARNING: init_db() failed at startup, continuing anyway: {exc}")

SHOP_LABELS = {s["id"]: s["label"] for s in SHOPS}

CHART_SERIES = {
    "zero": {"label": "Μηδενικές", "color": "var(--chart-red)", "dash": ""},
    "deals": {"label": "Προσφορές", "color": "var(--chart-green)", "dash": "6 3"},
    "placeholder": {"label": "Πλασματικές", "color": "var(--chart-amber)", "dash": "2 3"},
}


# ---------- Jinja helpers ----------


@app.template_filter("eur")
def format_eur(value):
    if value is None:
        return "—"
    text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} €"


@app.template_filter("grdate")
def format_gr_date(value):
    if not value:
        return "—"
    parts = str(value).split("-")
    if len(parts) == 3:
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    return str(value)


@app.context_processor
def inject_globals():
    def page_url(page):
        args = request.args.to_dict()
        args["page"] = page
        return url_for(request.endpoint, **args)

    return {"shop_nav": SHOPS, "page_url": page_url}


# ---------- Self-population -------------------------------------------
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
    """Shops whose stored data for today (UTC) is missing or unusable.

    A snapshot also counts as stale if its rows carry no item code. That
    happens after the schema gains a column an older ingest didn't fill:
    the data looks present, so a plain date check would never re-fetch
    it, and features depending on the new column would stay broken until
    the next calendar day. Checking usability instead of mere existence
    lets the dashboard heal itself on the next page load.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session = SessionLocal()
    try:
        usable_ids = set()
        for shop_id, snapshot_id in session.query(Snapshot.shop_id, Snapshot.id).filter(
            Snapshot.snapshot_date == today
        ):
            has_code = (
                session.query(ItemPrice.id)
                .filter(ItemPrice.snapshot_id == snapshot_id, ItemPrice.code.isnot(None))
                .first()
            )
            if has_code:
                usable_ids.add(shop_id)
    finally:
        session.close()
    return [s for s in SHOPS if s["id"] not in usable_ids]


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


# ---------- Per-shop views ----------


def view_from_snapshot(session, shop_id, label, snapshot, q=None, bug_type=None):
    # Only the bug/deal rows -- a shop's full catalog can be 10,000+ rows,
    # and loading all of that as ORM objects just to filter down to a
    # handful in Python is what once pushed memory over Render's 512MB cap.
    items = get_flagged_items_filtered(session, snapshot.id, q=q, bug_type=bug_type)
    return {
        "ok": True,
        "id": shop_id,
        "label": label,
        "snapshot_date": snapshot.snapshot_date,
        "title": snapshot.store_title,
        "address": snapshot.store_address,
        "is_open": snapshot.is_open,
        "total_items": snapshot.total_items,
        "total_categories": snapshot.total_categories,
        "zero_price_bugs": [
            {"name": it.name, "category": it.category, "code": it.code, "shop_id": shop_id}
            for it in items
            if it.is_zero_price_bug
        ],
        "placeholder_bugs": [
            {
                "name": it.name,
                "category": it.category,
                "price": it.price,
                "code": it.code,
                "shop_id": shop_id,
            }
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
                    "code": it.code,
                    "shop_id": shop_id,
                }
                for it in items
                if it.is_verified_deal
            ),
            key=lambda d: d["pct"],
            reverse=True,
        ),
    }


def empty_view(shop_id, label):
    """Shown when a shop has no stored snapshot yet. Deliberately does
    NOT fall back to fetching the catalog live -- that once cost ~460MB
    on a single request and kept OOM-killing this 512MB service."""
    return {
        "ok": True,
        "no_data": True,
        "id": shop_id,
        "label": label,
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


def get_shop_view(shop_id, label, q=None, bug_type=None):
    # Each call gets its own session -- SQLAlchemy sessions aren't
    # thread-safe, and this runs concurrently across shops.
    session = SessionLocal()
    try:
        snapshot = get_latest_snapshot(session, shop_id)
        if snapshot is None:
            return empty_view(shop_id, label)
        return view_from_snapshot(session, shop_id, label, snapshot, q=q, bug_type=bug_type)
    finally:
        session.close()


def get_all_shop_views(shop_ids=None, q=None, bug_type=None):
    selected = [s for s in SHOPS if shop_ids is None or s["id"] in shop_ids]
    results = [None] * len(selected)
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_to_index = {
            pool.submit(get_shop_view, shop["id"], shop["label"], q, bug_type): i
            for i, shop in enumerate(selected)
        }
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()
    return results


# ---------- Charts ----------


def build_counts_charts(rows):
    """rows: [{date, zero, placeholder, deals}] -> {"bugs": svg, "deals": svg}.

    Two separate charts on purpose: deal counts run in the thousands
    while bug counts run in the tens, and a shared y-scale would flatten
    the bug series into an unreadable baseline (the classic
    two-measures-one-axis mistake)."""
    if len(rows) < 2:
        return None

    def series_for(keys):
        return [
            {
                "label": CHART_SERIES[key]["label"],
                "color": CHART_SERIES[key]["color"],
                "dash": CHART_SERIES[key]["dash"],
                "points": [(format_gr_date(r["date"]), r[key]) for r in rows],
            }
            for key in keys
        ]

    return {
        "bugs": line_chart(series_for(["zero", "placeholder"]), height=160, y_zero=True),
        "deals": line_chart(series_for(["deals"]), height=160, y_zero=True, area=True),
    }


# ---------- Routes ----------


@app.route("/")
def dashboard():
    maybe_start_refresh()

    shop_filter = request.args.get("shop", type=int)
    q = (request.args.get("q") or "").strip() or None
    bug_type = request.args.get("type") or None
    if bug_type not in (None, "zero", "placeholder", "deal"):
        bug_type = None

    shop_ids = [shop_filter] if shop_filter in SHOP_LABELS else None
    results = get_all_shop_views(shop_ids=shop_ids, q=q, bug_type=bug_type)
    ok_results = [r for r in results if r["ok"]]

    bugs = {
        "zero_price": [
            {**it, "shop_label": r["label"]} for r in ok_results for it in r["zero_price_bugs"]
        ],
        "placeholder": [
            {**it, "shop_label": r["label"]} for r in ok_results for it in r["placeholder_bugs"]
        ],
    }

    session = SessionLocal()
    try:
        bargains, total_deals = get_deals_page(
            session,
            SHOP_LABELS,
            shop_id=shop_filter if shop_filter in SHOP_LABELS else None,
            q=q,
            per_page=30,
        )
        trend_rows = get_trend(session)
    finally:
        session.close()

    last_updated = max((r["snapshot_date"] for r in ok_results if r.get("snapshot_date")), default=None)

    return render_template(
        "dashboard.html",
        shops=results,
        bugs=bugs,
        bargains=bargains,
        total_deals=total_deals,
        refresh=refresh_status(),
        trend_charts=build_counts_charts(trend_rows),
        trend_days=len(trend_rows),
        last_updated=last_updated,
        shop_filter=shop_filter,
        q=q,
        bug_type=bug_type,
    )


@app.route("/deals")
def deals():
    shop_filter = request.args.get("shop", type=int)
    if shop_filter not in SHOP_LABELS:
        shop_filter = None
    category = (request.args.get("category") or "").strip() or None
    q = (request.args.get("q") or "").strip() or None
    min_pct = request.args.get("min_pct", type=float)
    sort = request.args.get("sort") or "pct"
    page = max(1, request.args.get("page", default=1, type=int))
    per_page = 50

    session = SessionLocal()
    try:
        latest = get_latest_snapshots_for_all_shops(session, list(SHOP_LABELS))
        categories = get_categories(session, [s.id for s in latest.values()])
        rows, total = get_deals_page(
            session,
            SHOP_LABELS,
            shop_id=shop_filter,
            category=category,
            q=q,
            min_pct=min_pct,
            sort=sort,
            page=page,
            per_page=per_page,
        )
    finally:
        session.close()

    pages = max(1, math.ceil(total / per_page))
    return render_template(
        "deals.html",
        rows=rows,
        total=total,
        page=min(page, pages),
        pages=pages,
        categories=categories,
        shop_filter=shop_filter,
        category=category,
        q=q,
        min_pct=int(min_pct) if min_pct else None,
        sort=sort,
    )


@app.route("/compare")
def compare():
    q = (request.args.get("q") or "").strip() or None
    category = (request.args.get("category") or "").strip() or None
    min_spread = request.args.get("min_spread", default=5, type=int)
    page = max(1, request.args.get("page", default=1, type=int))
    per_page = 25

    session = SessionLocal()
    try:
        latest = get_latest_snapshots_for_all_shops(session, list(SHOP_LABELS))
        categories = get_categories(session, [s.id for s in latest.values()])
        groups = compare_across_shops(
            session, SHOP_LABELS, min_spread_pct=min_spread, q=q, category=category
        )
    finally:
        session.close()

    # Add normalized price info to each row for comparison
    for group in groups:
        for row in group["rows"]:
            comparison_info = get_price_comparison_info(row["price"], row.get("size_info"))
            row.update(comparison_info)

    total = len(groups)
    pages = max(1, math.ceil(total / per_page))
    page = min(page, pages)
    visible = groups[(page - 1) * per_page : page * per_page]

    return render_template(
        "compare.html",
        groups=visible,
        total=total,
        page=page,
        pages=pages,
        categories=categories,
        q=q,
        category=category,
        min_spread=min_spread,
    )


@app.route("/history")
def history_jump():
    shop_id = request.args.get("shop", type=int)
    if shop_id not in SHOP_LABELS:
        return redirect(url_for("dashboard"))
    return redirect(url_for("history", shop_id=shop_id))


@app.route("/history/<int:shop_id>")
def history(shop_id):
    if shop_id not in SHOP_LABELS:
        abort(404)
    session = SessionLocal()
    try:
        rows = get_history(session, shop_id)
    finally:
        session.close()

    charts = build_counts_charts(
        [
            {
                "date": s.snapshot_date,
                "zero": s.zero_price_bug_count or 0,
                "placeholder": s.placeholder_bug_count or 0,
                "deals": s.verified_deal_count or 0,
            }
            for s in rows
        ]
    )
    return render_template(
        "history.html", label=SHOP_LABELS[shop_id], rows=list(reversed(rows)), charts=charts
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
    "verify_url",
]


@app.route("/download/sales.csv")
def download_sales_csv():
    shop_id = request.args.get("shop_id", type=int)
    q = (request.args.get("q") or "").strip() or None
    category = (request.args.get("category") or "").strip() or None
    # Read anything request-scoped BEFORE the generator runs: Flask tears
    # the request context down once it starts streaming, so touching
    # `request` inside generate() raises and truncates the download.
    base_url = request.url_root.rstrip("/")

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
            for r in iter_all_sales(session, SHOP_LABELS, shop_id=shop_id, q=q, category=category):
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
                        f"{base_url}/item/{r['shop_id']}/{r['code']}" if r.get("code") else "",
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


def _live_l30d(live):
    for tag in live.get("tags") or []:
        if tag.startswith("l30d:"):
            try:
                return float(tag.split(":", 1)[1])
            except ValueError:
                return None
    return None


@app.route("/item/<int:shop_id>/<code>")
def verify_item(shop_id, code):
    """Prove a finding: re-query e-food for this one product and show what
    they say right now, side by side with what we recorded."""
    if shop_id not in SHOP_LABELS:
        abort(404)

    session = SessionLocal()
    try:
        # First, try to find the product in the latest snapshot. If not found,
        # search back through recent snapshots in case a new snapshot was created
        # after the dashboard link was clicked.
        snapshot = get_latest_snapshot(session, shop_id)
        stored = None
        snapshot_date = None
        if snapshot is not None:
            snapshot_date = snapshot.snapshot_date
            stored = (
                session.query(ItemPrice)
                .filter(ItemPrice.snapshot_id == snapshot.id, ItemPrice.code == code)
                .first()
            )
            # If not in latest, search back 7 snapshots
            if stored is None:
                recent_snapshots = (
                    session.query(Snapshot)
                    .filter(Snapshot.shop_id == shop_id)
                    .order_by(Snapshot.snapshot_date.desc())
                    .limit(7)
                    .all()
                )
                for snap in recent_snapshots:
                    stored = (
                        session.query(ItemPrice)
                        .filter(ItemPrice.snapshot_id == snap.id, ItemPrice.code == code)
                        .first()
                    )
                    if stored is not None:
                        snapshot_date = snap.snapshot_date
                        break
        history_rows = get_item_history_by_code(session, shop_id, code)
    finally:
        session.close()

    shop_comparison = []

    price_chart = ""
    if len(history_rows) >= 2:
        price_chart = line_chart(
            [
                {
                    "label": "Τιμή",
                    "color": "var(--chart-blue)",
                    "dash": "",
                    "points": [(format_gr_date(d), p) for d, p, _ in history_rows],
                },
                {
                    "label": "Αρχική",
                    "color": "var(--chart-orange)",
                    "dash": "6 3",
                    "points": [(format_gr_date(d), fp) for d, _, fp in history_rows],
                },
            ],
            height=150,
            y_zero=False,
            value_suffix="€",
            area=True,
        )

    live, live_error, live_l30d = None, None, None
    verdict, verdict_ok = "", False
    try:
        live = fetch_menu_item(shop_id, code)
        live_l30d = _live_l30d(live)
        price = live.get("price")
        if price is not None and price <= 0:
            verdict = "Το e-food εξακολουθεί να επιστρέφει τιμή 0,00 € για αυτό το προϊόν."
            verdict_ok = True
        elif live_l30d is not None and live_l30d <= 0.05 and price:
            verdict = (
                f"Το e-food εξακολουθεί να δηλώνει κατώτατη 30 ημερών {live_l30d:.2f} € "
                f"ενώ χρεώνει {price:.2f} €."
            )
            verdict_ok = True
        elif stored is not None and stored.price is not None and price is not None:
            if abs(stored.price - price) < 0.005:
                verdict = "Η ζωντανή τιμή συμφωνεί με το στιγμιότυπό μας."
                verdict_ok = True
            else:
                verdict = (
                    f"Η τιμή άλλαξε από το στιγμιότυπο: {stored.price:.2f} € τότε, "
                    f"{price:.2f} € τώρα."
                )
        else:
            verdict = "Οι ζωντανές τιμές φαίνονται παραπάνω."
            verdict_ok = True
    except Exception as exc:  # noqa: BLE001
        live_error = str(exc)

    return render_template(
        "verify.html",
        code=code,
        stored=stored,
        snapshot_date=snapshot_date,
        shop_label=SHOP_LABELS[shop_id],
        shop_url=SHOP_URLS.get(shop_id, "https://www.e-food.gr/"),
        api_url=menu_item_url(shop_id, code),
        live=live,
        live_l30d=live_l30d,
        live_error=live_error,
        verdict=verdict,
        verdict_ok=verdict_ok,
        price_chart=price_chart,
        history_points=len(history_rows),
        shop_comparison=shop_comparison,
    )


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
