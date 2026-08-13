# Παρατηρητήριο Τιμών (Food Defects Dashboard)

Daily price-defect and deal-verification dashboard for supermarkets on
[e-food.gr](https://www.e-food.gr), mostly clustered around Chalandri,
Athens. It exists because of the Greek/EU "Omnibus Directive": any
advertised discount has to be measured against the item's real lowest
price in the last 30 days, and e-food's own pricing data regularly
contradicts itself on that point. This tool catches the contradictions.

It looks for three things, every day, across every tracked shop:

- **Zero-price bugs** — items listed at €0.00.
- **Placeholder reference-price bugs** — items whose declared "30-day low"
  is an implausible near-zero value (e.g. €0.01), which makes any
  discount percentage shown against it meaningless.
- **Verified deep discounts** — items genuinely priced below their real
  30-day low, badge and all, excluding sold-by-weight items where the
  reference price may have been recorded at a different purchase weight.

Every product page also links back to e-food's own live API response, so
any finding can be checked against the source directly.

## Architecture

```
ingest.py        -- fetches every tracked shop's catalog, stores a daily
                     snapshot, runs the bug/deal detection
efood_client.py  -- thin HTTP client for e-food's public consumer API
price_analysis.py-- the bug/deal detection rules themselves
price_utils.py   -- price normalization (per-kg/lt/item) for fair
                     cross-shop, cross-package-size comparison
db.py            -- SQLAlchemy models (Postgres in prod, SQLite locally)
queries.py       -- all read-side queries, kept column-only / LIMIT'd
webapp.py        -- Flask routes; templates/ + static/ hold the UI
notify.py        -- optional email digest of current bugs (SMTP)
shops.py         -- the list of tracked shops (id, label, e-food slug)
```

**Why the split between a GitHub Actions ingest and a web service**: the
web service runs on Render's free 512MB tier, which can't hold a whole
shop catalog (or several) in memory alongside serving traffic without
risking an OOM kill. Ingestion instead runs daily on a GitHub Actions
runner (`.github/workflows/daily-ingest.yml`, plenty of RAM) and writes
straight to the production Postgres database. The web app only ever
reads small, filtered, paginated slices of that data.

**Price normalization**: e-food publishes its own authoritative unit
price on most items (`metric_unit_description`, e.g. `"3,36€ / kg"`),
which the dashboard prefers wherever present. Only for the minority of
items missing that field does it fall back to parsing the package-size
string itself (`price_utils.parse_size_info`).

## Running locally

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python3 ingest.py     # fetches every shop, populates food_defects.db (SQLite)
python3 webapp.py     # http://localhost:5000
```

Set `DATABASE_URL` to point at Postgres instead; it defaults to a local
SQLite file. For the email digest, set `SMTP_USER`, `SMTP_PASSWORD` (a
Gmail [App Password](https://myaccount.google.com/apppasswords)), and
`NOTIFY_EMAIL_TO`, then run `python3 notify.py`.

## Deployment

Deployed on [Render](https://render.com) via `render.yaml`: one free web
service + one free Postgres database. Scheduling lives in GitHub Actions
(`daily-ingest.yml`, 05:00 UTC) since Render's free tier has no cron job
service type.

## Adding a shop

Edit `shops.py`: add `{"id": ..., "label": ..., "slug": ...}`. The `id`
is e-food's restaurant id (visible in the store's e-food URL or its API
response); `slug` is the store's path on e-food.gr.

## Note on scope

Built for low-frequency, personal, non-commercial use against e-food's
public consumer API — see `efood_client.py` for the relevant caveat.
