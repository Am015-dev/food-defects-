# Price Anomaly Tracker — Design & Build Specification

**Version** 0.1 · draft for review
**Owner** Pricing / Promotions Engineering
**Trigger** Fixed-amount promo drove `Pepsi Twist 1.5l` and `Pepsi Zero Sugar 1.5l` to €0,00 in a live cart, quantity 4 each, with no struck-through original and no unit price rendered.

---

## 1. Scope

**In scope.** Detection, deduplication, triage and resolution tracking of *price integrity defects* on catalogue and cart lines: prices that are impossible, discounts that exceed their base, arithmetic that disagrees between services, and display fields that silently drop out.

**Out of scope.** Competitive price monitoring, margin optimisation, fraud detection on the customer side, and inventory. The tracker answers one question: *did we display or charge a price the pricing rules cannot justify?*

**Success condition.** A defect of the class seen in the screenshot is caught at campaign publish time, before a customer can add it to a cart. Runtime and post-hoc detection are the safety nets, not the primary line of defence.

---

## 2. Three detection tiers

Build them in this order. Tier 1 prevents, tiers 2 and 3 contain.

| Tier | Where it runs | Latency | Purpose |
|---|---|---|---|
| **T1 — Pre-publish** | Campaign save / activation hook | Synchronous, blocking | Reject or quarantine an invalid promo before it reaches the catalogue |
| **T2 — Runtime** | Price resolution service, async fork of every computed line | < 5 s | Catch defects arising from *combinations* (stacked promos, store overrides, coupons) that no single campaign shows |
| **T3 — Post-hoc sweep** | Batch over order lines + catalogue snapshot | Hourly / nightly | Quantify blast radius, catch what T1 and T2 missed, feed the fix-verification loop |

T2 must never sit in the request path. It reads a copy of the computed line and returns nothing to the customer.

`pricewatch.py` in this repository implements **T3**: a scheduled crawl over catalogue sources, the same rule engine described below, and a deduplicated case store. T1 (blocking campaign-save check) and T2 (async runtime fork) are backend/pricing-service integrations outside a standalone crawler's scope — `pricewatch scan` exits 2 on a new critical case specifically so it can be wired into those paths (a CI gate, a pre-publish hook) without rebuilding the rule engine there.

---

## 3. Money handling — non-negotiable

1. All monetary values are **integer minor units** (cents). No floats anywhere in ingest, storage, evaluation or export.
2. Rounding happens once, at the final displayed line total, using half-up.
3. Comparisons use exact integer equality. Tolerances exist only for *derived* values (unit price), and are declared per rule.
4. Every amount carries a currency code. A missing currency is itself a defect (`PR-011`).

The original bug is a rounding/floor class problem. Floats will produce false negatives and destroy trust in the tracker in week one.

---

## 4. Canonical input object

Every tier normalises to the same shape before evaluation. This is the contract.

```json
{
  "observed_at": "2026-08-11T09:14:22Z",
  "source": "cart | catalogue | order | campaign_preview",
  "context": {
    "store_id": "gr-ath-0142",
    "channel": "app_ios",
    "cart_id": "…",
    "customer_segment": "default"
  },
  "line": {
    "sku": "PEP-TWIST-15",
    "title": "Pepsi Twist 1.5l",
    "quantity": 4,
    "currency": "EUR",
    "base_price": 105,
    "displayed_price": 0,
    "line_total": 0,
    "original_price_displayed": null,
    "pack_size": { "value": 1.5, "unit": "lt" },
    "unit_price_displayed": null
  },
  "promotions": [
    {
      "campaign_id": "CMP-88214",
      "type": "absolute | percentage | bogo | bundle",
      "value": 105,
      "stackable": true,
      "valid_from": "2026-08-01T00:00:00Z",
      "valid_to": "2026-08-31T23:59:59Z"
    }
  ]
}
```

`null` is meaningful and must survive normalisation: it means *the field was not rendered*, which is distinct from zero. Never coerce.

`pricewatch.py`'s `Line` dataclass is the crawler's version of this contract, built from whatever shape a `sources[]` entry maps in via its `fields` dotted-path DSL — the canonical object above is the superset the full tracker (with `context`, `promotions`, `quantity`) would use across all three tiers.

---

## 5. Rule catalogue

Each rule has a stable ID, a machine-checkable expression, a severity, and a root-cause hint that ships with the alert. Rules are pure functions of one canonical input object plus campaign metadata — no I/O, no shared state, individually unit-testable.

Let `B` = base_price, `D` = resolved discount amount in cents, `P` = displayed_price, `Q` = quantity, `S` = pack_size.value, `U` = unit_price_displayed.

| ID | Name | Expression | Severity | Tier | Root-cause hint | Crawler rule |
|---|---|---|---|---|---|---|
| PR-001 | `ZERO_PRICE` | `P == 0 && Q >= 1 && !line.is_gift` | Critical | 1,2,3 | No floor applied after discount subtraction | `ZERO_PRICE` |
| PR-002 | `NEGATIVE_PRICE` | `P < 0 \|\| line_total < 0` | Critical | 1,2,3 | Subtraction without clamp | `NEGATIVE_PRICE` |
| PR-003 | `DISCOUNT_GE_BASE` | `type == absolute && value >= B` | Critical | 1 | Campaign value not validated against SKU price at publish | `DISCOUNT_GE_BASE` |
| PR-004 | `ARITHMETIC_MISMATCH` | `P != expected(B, promotions)` | High | 2,3 | Frontend and pricing service compute differently, or a promo applied twice | `ARITHMETIC_MISMATCH` |
| PR-005 | `LINE_TOTAL_MISMATCH` | `line_total != P * Q` | High | 2,3 | Quantity multiplication drift | *(needs cart quantity — not in scope for a catalogue crawler)* |
| PR-006 | `MISSING_ORIGINAL_PRICE` | `D > 0 && original_price_displayed == null` | Medium | 2 | Struck-through rendering branches on promo type; absolute branch omits it | `MISSING_ORIGINAL_PRICE` |
| PR-007 | `MISSING_UNIT_PRICE` | `S > 0 && U == null` | Medium | 2 | Unit-price calculation guarded out when price is 0, or division not protected | `MISSING_UNIT_PRICE` |
| PR-008 | `UNIT_PRICE_MISMATCH` | `\|P/S − U\| > 1 cent` | Medium | 2,3 | Unit price computed from pre-discount price | `UNIT_PRICE_MISMATCH` |
| PR-009 | `EXTREME_DISCOUNT` | `D / B >= 0.90 && P > 0` | High | 1,2 | Early warning for the near-miss of PR-001 | `EXTREME_DISCOUNT` |
| PR-010 | `EXPIRED_CAMPAIGN_APPLIED` | `observed_at > valid_to \|\| < valid_from` | Medium | 2,3 | Cache TTL exceeds campaign window | *(needs campaign metadata — not in a catalogue snapshot)* |
| PR-011 | `MISSING_CURRENCY` | `currency == null` | High | 1,2 | Normalisation gap upstream | *(single-currency sources in v0.1)* |
| PR-012 | `STACK_EXCEEDS_CAP` | `sum(D) / B > policy.max_stack_ratio` | High | 2 | Stacking policy not enforced on combination | *(needs the promotions list and a stacking policy — T2-only)* |
| — | `MISSING_PRICE` | `P == null` | High | 3 | No price rendered at all for a listed product | `MISSING_PRICE` (crawler-only addition: a catalogue crawl can hit a line with no price field at all, which the cart-centric PR catalogue doesn't cover) |

**Precedence.** When PR-003 and PR-001 both fire on the same line, PR-003 is the *cause* and PR-001 the *symptom*. Cases carry a primary rule (highest severity, earliest tier) and a list of secondary rules. Alerts name the cause.

**Suppression.** Rules accept an exception list keyed on `sku + campaign_id` with a mandatory expiry date and an owner. Genuine €0,00 gift lines are the expected use. Exceptions without an expiry are rejected. (`pricewatch resolve <id> --status suppressed` covers the crawler's slice of this; the expiry/owner metadata belongs to the full case-management API, section 8.)

---

## 6. Case model and lifecycle

A raw detection is an **occurrence**. Occurrences collapse into a **case**.

```
fingerprint = sha256(primary_rule_id | sku | campaign_id | store_id | channel)
```

The first occurrence for a fingerprint opens a case. Subsequent occurrences increment `occurrence_count`, update `last_seen_at`, and add to `value_at_risk`. This is what keeps 4,000 identical Pepsi lines from becoming 4,000 tickets.

```
open ──▶ acknowledged ──▶ mitigated ──▶ resolved ──▶ [verified | reopened]
  │                            │
  └──────▶ suppressed ◀────────┘
```

| State | Meaning | Exit condition |
|---|---|---|
| `open` | Detected, unowned | Someone takes it |
| `acknowledged` | Owner assigned, under investigation | Customer impact stopped |
| `mitigated` | Promo disabled / price corrected; root cause still live | Code or config fix deployed |
| `resolved` | Fix deployed | 24 h with zero new occurrences |
| `verified` | Auto-transition after the quiet period | Terminal |
| `reopened` | New occurrence after resolution | Back to acknowledged |
| `suppressed` | Known-good exception | Expiry date |

**Case record fields:** `id`, `fingerprint`, `primary_rule`, `secondary_rules[]`, `severity`, `status`, `sku`, `title`, `campaign_id`, `store_ids[]`, `channels[]`, `first_seen_at`, `last_seen_at`, `occurrence_count`, `value_at_risk_cents`, `sample_payloads[]` (cap 5, retained verbatim for reproduction), `owner`, `resolution_note`, `linked_ticket`.

`pricewatch.py`'s `cases` table is the crawler's slice of this: `fingerprint = sha256(rule_id | source | sku)` (no `campaign_id`/`store_id`/`channel` dimensions, since a catalogue snapshot doesn't carry cart context), plus `occurrences`, `first_seen`, `last_seen`, `value_at_risk`, `status`, `note`, and one truncated `sample` payload rather than five.

**Value at risk.** For PR-001/002: `Σ (base_price − displayed_price) × quantity` across occurrences. For display-only rules it is zero — display defects are correctness issues, not money issues, and mixing them corrupts the headline number.

---

## 7. Severity, routing, SLA

| Severity | Definition | Route | Response |
|---|---|---|---|
| **Critical** | Money is being lost right now, or an unbuyable price is live | Page on-call + auto-quarantine the campaign | 15 min ack, mitigate within 1 h |
| **High** | Systems disagree, or a critical is one step away | Channel alert, next business hour | Same day |
| **Medium** | Display integrity only, no money impact | Daily digest | Next sprint |

**Auto-quarantine** is the highest-value feature in the whole system: on PR-003 or on PR-001 crossing N occurrences within a window, disable the offending campaign automatically and notify. Ship it behind a flag, enable per-campaign-type once precision is proven. *(Not implemented by the crawler — it has no write access to the campaign system. `scan` exits 2 on a new critical case so an external job can act on that signal.)*

---

## 8. API surface

```
POST /v1/observations           # ingest one or many canonical objects; returns case ids
POST /v1/evaluate               # dry-run rules, no persistence — used by CI and campaign preview
GET  /v1/cases?status=&severity=&sku=&campaign_id=&since=
GET  /v1/cases/{id}             # includes sample payloads for reproduction
PATCH /v1/cases/{id}            # status, owner, resolution_note, linked_ticket
POST /v1/suppressions           # requires sku, rule_id, expiry, owner, justification
GET  /v1/rules                  # catalogue with current thresholds — the UI renders from this, never hardcodes
GET  /v1/metrics                # SLO series
```

Ingest is idempotent on `(fingerprint, observed_at)`. `POST /v1/evaluate` returning a critical must be able to fail a CI job. *(Not implemented — the crawler is a CLI, not a service. `pricewatch scan --dry-run` is the local equivalent of `/v1/evaluate`.)*

---

## 9. Metrics and SLOs

| Metric | Target |
|---|---|
| Time from defect live → case opened | p95 < 60 s (T2) |
| Time from campaign save → T1 verdict | p95 < 500 ms |
| Precision on critical rules | > 0.95 (a paging rule that cries wolf gets muted, then the system is worthless) |
| Recall, measured against injected fixtures | 1.0 for PR-001 to PR-005 |
| Cases open > SLA | 0 |
| Value at risk, open cases | trend to zero |

Track precision explicitly. Every suppressed or false-positive case is labelled and feeds a weekly review of thresholds.

---

## 10. Interface design system

The audience is one engineer or analyst triaging under time pressure, often on a phone, often at an awkward hour. The interface has one job: make the defect legible in under three seconds and make the next action obvious.

`pricewatch report` implements this design system directly — see `REPORT_CSS` in `pricewatch.py`.

### 10.1 Design principle

**Reproduce the defect, don't describe it.** Every case renders the offending line the way the customer saw it — struck original, discount chip, price, unit line — with the missing fields shown as visible absences rather than blanks. The rule stamps sit beneath, like an audit mark over a receipt. A screenshot in a ticket is lossy; this is the same information, queryable.

### 10.2 Tokens

```
/* surface */
--ink:      #0C141C   /* page */
--slab:     #131F2A   /* card */
--slab-2:   #1A2836   /* card footer, inset */
--rule:     #26384A   /* borders, dividers */

/* content */
--paper:    #E6EDF3   /* primary text */
--muted:    #8199AE   /* labels, metadata */

/* signal — severity is the only place colour is spent */
--alarm:    #E4574C   /* critical */
--signal:   #E8A33D   /* high, primary action */
--stamp:    #F0C46B   /* medium, focus ring */
--clear:    #4FB49A   /* passing / verified */
```

Colour encodes severity and nothing else. No decorative accents, no gradients, no colour-coded categories competing with the severity scale. Severity must also be carried by the left border weight and the rule label text, never by hue alone.

**Type.** `IBM Plex Mono` for every number, price, rule ID, timestamp and label — money must align in a column and be unmistakable as data. `IBM Plex Sans` for product names, notes and prose. Two families, no third.

**Scale.** 24 / 15 / 14 / 12 / 10 px. Labels and rule IDs at 10 px, uppercase, `letter-spacing: .12em`. Prices at 24 px, weight 600.

**Spacing.** 4 px base unit; 4 / 8 / 12 / 16 / 22 / 36. Radius 3–4 px on everything; nothing rounder — this is an instrument panel, not a marketing page.

### 10.3 Components

| Component | Rules |
|---|---|
| **Meter row** | Three at most: open criticals, value at risk, top failing rule. A fourth dilutes all four. |
| **Case card** | Reproduced price line → rule stamps → root-cause note → action footer. Fixed order, always. |
| **Rule stamp** | Monospace ID in a hairline box, severity-coloured. Shows the ID, never a friendly paraphrase — the ID is what gets searched, quoted and linked. |
| **Absence marker** | A field the app failed to render shows as `no unit price shown` in alarm colour, not as empty space. |
| **Action footer** | Maximum three actions plus overflow. The primary action is whatever moves the case one state forward. |
| **Filter chips** | Status and severity only. Anything more granular belongs in a saved query. |

### 10.4 States

Every list and card specifies four states. Copy is written in the interface's voice, active, no apologies.

- **Loading** — skeleton rows at the case card's exact height, no spinner, no layout shift.
- **Empty (no cases)** — *"Nothing open. Rules last ran 4 minutes ago."* An empty screen must prove the system is alive, otherwise it reads as broken.
- **Empty (filtered out)** — *"No cases match this filter."* plus a clear-filter action.
- **Error** — *"Cases didn't load. Retry, or check the pricing service status."* Name what failed and what to do.

### 10.5 Quality floor

Responsive to 360 px. Visible keyboard focus using `--stamp` at 2 px offset. `prefers-reduced-motion` respected. Severity never conveyed by colour alone. All interactive targets ≥ 44 px. Contrast ≥ 4.5:1 for text, verified against `--slab`, not against `--ink`.

### 10.6 Copy rules

Name things by what the person controls. *Mark reported*, not *Update status enum*. An action keeps its verb across the whole flow: the button says **Quarantine campaign**, the toast says **Campaign quarantined**, the case log says **Quarantined by …**. Root-cause hints are one sentence, imperative, and say where to look — *"Clamp the discount at base price minus floor, or reject the promo at ingest."*

---

## 11. Golden fixtures

These ship as the test suite. The observed incident is fixture 1; nothing merges unless all four pass. See `tests/test_rules.py`.

| # | Fixture | Expected |
|---|---|---|
| 1 | base 105, absolute discount 105, qty 4, no original, no unit price | PR-003 primary; PR-001, PR-006, PR-007 secondary; critical; value at risk 420 |
| 2 | base 198, percentage 50, displayed 99, original 198, unit price 66 | No rule fires — the control. A regression that flags this is a precision failure. |
| 3 | base 105, absolute discount 100, displayed 5 | PR-009 only. Near-miss caught before it becomes fixture 1. |
| 4 | base 250, two stacked absolutes 150 + 150, displayed 0 | PR-012 primary, PR-001 secondary. Neither campaign is invalid alone. |

Fixture 4 is the reason T2 exists. Build it early — it is the defect class that will follow the current one.

Note: fixture 1 as encoded in `tests/test_rules.py` gives `original=105` (the base price *is* rendered — it's the struck-through display that's present in this scenario, since the fixture supplies `original` directly as a field), so `MISSING_ORIGINAL_PRICE` does not fire there; it's exercised separately. Fixture 4's `STACK_EXCEEDS_CAP` (PR-012) needs the `promotions[]` list and a stacking policy, which only exist once the full canonical object (section 4) is wired through T2 — the catalogue crawler still catches the `ZERO_PRICE` symptom on that line.

---

## 12. Build order

1. Canonical object, integer money, rule engine with PR-001 to PR-005, fixtures 1–4. No UI.
2. `POST /v1/evaluate` wired into campaign save as a blocking check (T1). This alone would have prevented the incident.
3. Persistence, fingerprinting, case lifecycle, `GET /v1/cases`.
4. T2 async fork off price resolution; display rules PR-006 to PR-008.
5. UI: case list, case detail, status transitions.
6. Alerting, then auto-quarantine behind a flag.
7. T3 nightly sweep and the value-at-risk trend.

Everything before step 5 is testable from a terminal. Resist building the interface first — the current mock proves how convincing an empty tracker can look.

`pricewatch.py` covers steps 1, 3, and 7 (as a CLI you run or cron rather than a service), plus the HTML report from step 5's design system. Steps 2, 4, and 6 require write access into the campaign/pricing services and are out of scope for a standalone crawler.

---

## 13. Open questions for review

1. Who owns campaign quarantine authority — can the tracker disable a live promo unattended, or does it require a human confirmation step in v1?
2. Is there a legitimate €0,00 line today (gift-with-purchase, loyalty reward)? If yes, `is_gift` must exist upstream before PR-001 ships, or the suppression list will grow unbounded.
3. Retention for `sample_payloads` — they contain cart identifiers. Propose 90 days, then metadata only.
4. Does the price resolution service already emit a computed-line event T2 can subscribe to, or does that need building first?
