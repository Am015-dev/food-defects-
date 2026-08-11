# food-defects-

**pricewatch** — a price integrity crawler. It crawls a catalogue (JSON API or HTML), normalises every line to integer cents, runs a rule engine over it, and tracks defects as deduplicated cases in SQLite.

It exists because a fixed-amount promo drove two SKUs to €0,00 in a live cart with no struck-through original price and no unit price rendered — see `docs/price-anomaly-tracker-spec.md` for the full design spec this implementation is built against.

## Install

```bash
pip install -r requirements.txt
python pricewatch.py scan --config pricewatch.yaml
```

Runs locally — it needs to reach the site you're monitoring.

## Try it offline first

The shipped config reads `fixture_cart.json`, which reproduces the incident: two SKUs (`PEP-TWIST-15`, `PEP-ZERO-15`) driven to zero by a fixed discount equal to their base price, plus a 90%+ near-miss (`COKE-15`) that should be caught before it becomes the next zero-price defect.

```bash
python pricewatch.py scan --config pricewatch.yaml
python pricewatch.py cases
python pricewatch.py report --out report.html
```

Expected: 1 open critical case, 1 reopenable/open critical case, 1 open high case, 6 lines checked, exit code 2 (a new critical case was found).

## Point it at the real catalogue

To scan live pages you need the actual endpoint, because most shop frontends are JS apps and the prices arrive as JSON, not HTML.

1. Open a shop page in Chrome → DevTools → **Network** → filter **Fetch/XHR**.
2. Reload. Look for the response containing product names and prices (usually a menu/catalogue call).
3. Copy the request URL, and note the JSON shape — where the product array lives, and what the price fields are called.
4. Fill in a source in `pricewatch.yaml`:

```yaml
sources:
  - name: "shop 1234 catalogue"
    type: json
    url: "https://<host>/<the endpoint you copied>"
    items_path: "data.categories[].items[]"   # [] fans out over a list
    page_param: page            # omit if not paginated
    max_pages: 20
    price_scale: euro           # or 'cents' if the API returns integers
    fields:
      sku: id
      title: name
      price: price
      original_price: initial_price
      discount_amount: discount_amount
      discount_pct: discount
      unit_size: quantity
      unit_label: unit
      unit_price: price_per_unit
```

`items_path` and every entry under `fields` are dotted paths into the JSON. Any field the API doesn't have — leave it out. A missing field is treated as *not rendered*, which is what `MISSING_ORIGINAL_PRICE` and `MISSING_UNIT_PRICE` detect.

If a page is server-rendered, use `type: html` with CSS selectors instead:

```yaml
  - name: "category page"
    type: html
    url: "https://<host>/category/soft-drinks"
    selectors:
      item: "[data-testid='product-card']"
      title: ".product-name"
      price: ".price-current"
      original_price: ".price-was"
      unit_price: ".unit-price"
```

**Adding shops:** one source entry per shop or category. Start with three or four while you tune selectors, then widen.

### Live via the official e-food Partner API (dish-level, sanctioned)

This is the **legal, no-bypass route to product-level prices and promotions** — the data the tracker was built for. e-food runs a Partner API under the Delivery Hero developer program ([developer-qc.e-food.gr](https://developer-qc.e-food.gr)) with a Catalog integration (product status + pricing) and a Promotions API.

To use it you must be an **e-food partner**: request a `client_id`/`client_secret` from your e-food Account Manager. The token covers all stores under your chain, so it reads *your* catalogue — exactly where a promo-driven €0 product shows up.

```bash
export EFOOD_CLIENT_ID=...
export EFOOD_CLIENT_SECRET=...
```

```yaml
  - name: "efood partner catalogue"
    type: json
    url: "https://e-food.partner.deliveryhero.io/v2/<catalogue read endpoint>"
    auth:
      type: oauth2_client_credentials
      token_url: "https://e-food.partner.deliveryhero.io/v2/oauth/token"
      client_id_env: EFOOD_CLIENT_ID
      client_secret_env: EFOOD_CLIENT_SECRET
    items_path: "products[]"          # set from your Partner API response shape
    price_scale: euro
    fields:
      sku: sku
      title: name
      price: price
      original_price: original_price
      discount_amount: discount_amount
      discount_pct: discount_percentage
```

The crawler handles the OAuth2 client-credentials exchange itself (`oauth2_token` in `pricewatch.py`), attaches the bearer token, and runs the full rule engine over the returned products — so a fixed-amount promo that drives a product to €0 fires `DISCOUNT_GE_BASE` + `ZERO_PRICE` exactly like the golden fixture. Fill in the exact catalogue read endpoint and field names from the Partner API docs that come with your credentials.

> **Eligibility is the catch, not legality.** This route needs partner credentials, which e-food issues to its vendors/chains. If you have them (or your client/employer does), this is the clean path to dish-level defects. If you don't, there is no *legal automated* route to another shop's dish prices — that data is private to that partner.

### Live via the Apify platform

Some catalogues (including e-food.gr) sit behind a bot-protection wall that refuses anonymous scripts. For those, the crawler can pull data through a **published Apify actor** — a sanctioned, keyed channel that reaches the site's *public* API, rather than defeating its protection. Set your token in the environment (never in the config), then add an `apify` source:

```bash
export APIFY_TOKEN=apify_api_xxx
```

```yaml
  - name: "efood athens stores (apify)"
    type: apify
    actor: "studio-amba/efood-scraper"   # or dataset_id: "abc123"
    token_env: APIFY_TOKEN
    input:
      location: "Athens"
      coverage: "city-wide"
      maxResults: 20000
    items_path: ""                       # actor dataset is a top-level array
    price_scale: euro
    fields:
      sku: efoodId
      title: name
      price: deliveryCost
      unit_size: minimumOrder
```

Run it:

```bash
export APIFY_TOKEN=apify_api_xxx
python3 pricewatch.py scan --config pricewatch.yaml
```

**Scope limit (from the actor's own docs):** the public e-food actor returns *store-level* fields — delivery cost, minimum order, ratings, offers, hours — **not individual dish prices**. Menu-level dish prices require authenticated access to e-food's private API and are not available through this actor, and this crawler does not scrape that private API. So this feed lets pricewatch monitor store-level integrity live and autonomously; it will not surface a defect that lives on a product/dish (e.g. a single item mispriced to €0 inside a shop's menu). For dish-level monitoring you need a sanctioned menu feed.

## Rules

| Rule | Severity | Fires when |
|---|---|---|
| `DISCOUNT_GE_BASE` | critical | fixed discount ≥ base price — the cause |
| `ZERO_PRICE` | critical | price is 0 — the symptom |
| `NEGATIVE_PRICE` | critical | price below zero |
| `ARITHMETIC_MISMATCH` | high | displayed ≠ base − discount |
| `EXTREME_DISCOUNT` | high | ≥90% off — the near-miss, catch it before it hits zero |
| `MISSING_PRICE` | high | product listed with no price |
| `MISSING_ORIGINAL_PRICE` | medium | promo active, no struck-through price |
| `MISSING_UNIT_PRICE` | medium | pack size shown, no €/unit |
| `UNIT_PRICE_MISMATCH` | medium | €/unit ≠ price ÷ pack size |

When several fire on one line, the highest-severity one becomes the case's primary rule and the rest are recorded as secondary — so you get told the *cause*, not just the symptom.

Add a rule by appending to the `RULES` list in `pricewatch.py`. Each is a one-line lambda over a normalised `Line`; no other code changes. Add a golden fixture for it in `tests/test_rules.py`.

## Commands

```bash
python pricewatch.py scan --config pricewatch.yaml       # crawl and record
python pricewatch.py scan --config pricewatch.yaml --dry-run --limit 20
python pricewatch.py cases --status open --severity critical
python pricewatch.py report --out report.html
python pricewatch.py export --format csv --out cases.csv
python pricewatch.py resolve 1 --status mitigated --note "campaign disabled"
```

Statuses: `open → acknowledged → mitigated → fixed`, plus `suppressed` for known-good zero-price lines (gift items). A case that reappears after being marked fixed flips to `reopened` automatically.

`scan` exits **2** when it opens a new critical case — that's the hook for alerting.

## Tests

```bash
pip install pytest
pytest tests/
```

`tests/test_rules.py` runs the rule engine against the golden fixtures from `docs/price-anomaly-tracker-spec.md` §11, plus one fixture per rule not covered there.

## Schedule it

```cron
0 * * * * cd /path/to/food-defects- && /usr/bin/python3 pricewatch.py scan --config pricewatch.yaml >> scan.log 2>&1 || mail -s "New critical price defect" you@example.com < scan.log
```

Hourly is plenty for catalogue drift. The exit code does the alerting; no daemon, no queue.

## Behaviour on the target site

Defaults: 1.5 s between requests, sequential, three retries with backoff, `robots.txt` honoured, identifying User-Agent. Set your contact address in `politeness.user_agent` before running it at any scale, and keep the delay conservative — the tool only reads listing pages, and there's no reason for it to be indistinguishable from a load test.

## Storage

Single file `pricewatch.db`. Table `cases` holds one row per fingerprint (`rule + source + sku`) with `occurrences`, `first_seen`, `last_seen`, `value_at_risk` and a truncated sample of the raw payload for reproduction. Table `scans` records each run. Back it up by copying the file.

## What this crawler is (and isn't)

This is Tier 3 of the three-tier design in `docs/price-anomaly-tracker-spec.md`: a scheduled post-hoc sweep over catalogue sources. It does not block a campaign at publish time (Tier 1) or fork off the live price-resolution service (Tier 2) — those require write access into pricing/campaign systems this crawler doesn't have. `scan`'s exit code 2 on a new critical case is the integration point: wire it into a CI gate or pre-publish hook to get Tier-1-style prevention without duplicating the rule engine there.
