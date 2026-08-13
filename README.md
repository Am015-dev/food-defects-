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

Beyond bug-hunting, it's also a small price-comparison tool:

- **`/search`** — full-catalog search across every tracked shop, sortable
  by price, by unit price, or alphabetically.
- **`/compare`** — the same product at different prices across shops,
  with unit-price normalization so a 500g jar and a 1kg jar of the same
  thing compare fairly.
- **`/drops`** — products that got cheaper since the previous snapshot.

Every product page also links back to e-food's own live API response, so
any finding can be checked against the source directly.

## Architecture

```
ingest.py        -- fetches every tracked shop's catalog, stores a daily
                     snapshot, runs the bug/deal detection
efood_client.py  -- thin HTTP client for e-food's public consumer API
price_analysis.py-- the bug/deal detection rules themselves
price_utils.py   -- price normalization (per-kg/lt/item) and accent-
                     insensitive name folding for search
db.py            -- SQLAlchemy models (Postgres in prod, SQLite locally)
queries.py       -- all read-side queries, kept column-only / LIMIT'd
webapp.py        -- Flask routes; templates/ + static/ hold the UI
retention.py     -- prunes item_prices rows past 90 days
notify.py        -- optional email digest of current bugs (SMTP),
                     plus the ingest-failure alert
shops.py         -- the list of tracked shops (id, label, e-food slug)
tests/           -- pytest: price_utils/price_analysis unit tests,
                     Flask test-client route tests over a seeded SQLite DB
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

Tests (`pip install -r requirements-dev.txt` first):

```
ruff check .
pytest -q
```

## Deployment

Deployed on [Render](https://render.com) via `render.yaml`: one free web
service + one free Postgres database. Scheduling lives in GitHub Actions
(`daily-ingest.yml`, 05:00 UTC) since Render's free tier has no cron job
service type.

**Keeping the schedule alive**: GitHub disables a scheduled workflow
automatically after 60 days with no repository activity (any commit or
manual `workflow_dispatch` run resets that clock). If the daily ingest
ever seems to have stopped, check the Actions tab first -- it may just
need a manual re-enable, not a code fix.

**Backups**: Render's free Postgres has no backups of its own, and price
history can't be re-fetched after the fact (e-food only exposes today's
prices). `daily-ingest.yml` runs a `pg_dump` after every successful
ingest, GPG-encrypts it, and keeps it as a 30-day GitHub Actions
artifact. Requires a `BACKUP_ENCRYPTION_PASSPHRASE` secret -- GitHub
Actions artifacts are downloadable by anyone with repo read access for
the whole retention window (a lower bar than the `DATABASE_URL` secret
itself), so the dump is encrypted rather than uploaded raw. To restore:
download the artifact, then
```
gpg --batch --yes --decrypt --passphrase-fd 0 \
  --output food_defects_backup.dump \
  food_defects_backup.dump.gpg <<< "$BACKUP_ENCRYPTION_PASSPHRASE"
pg_restore --clean --if-exists -d "$DATABASE_URL" food_defects_backup.dump
```
Old per-item rows are also pruned after 90 days (`retention.py`) to stay
within the free tier's 1GB limit; the small per-day summary rows are
kept forever.

## Adding a shop

Edit `shops.py`: add `{"id": ..., "label": ..., "slug": ...}`. The `id`
is e-food's restaurant id (visible in the store's e-food URL or its API
response); `slug` is the store's path on e-food.gr.

## Note on scope

Built for low-frequency, personal, non-commercial use against e-food's
public consumer API — see `efood_client.py` for the relevant caveat.
