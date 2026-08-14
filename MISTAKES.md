# MISTAKES.md

A running log of mistakes made while working on this repo, and what
fixed or would have prevented them. Not a changelog — only things that
were actually wrong at some point and had to be caught or fixed.

Check this file before touching an area listed below. Add an entry
whenever you catch yourself in a real mistake (not a typo you fixed
before running anything) — something a test, a lint pass, a second look,
or the user caught. Skip anything that was just an unfinished draft.

## Entry format

```
## YYYY-MM-DD — short title
**Where:** file(s)/area
**What happened:** the actual wrong behavior
**Root cause:** why it happened, not just what
**Fix:** what changed
**Lesson:** the general rule to carry forward
```

---

## 2026-08-13 — fuzzy product matching used the wrong rapidfuzz scorer, verified only at small scale

**Where:** `product_matching.py`, `best_match` (the product-identity layer)

**What happened:** Built a fuzzy matcher for linking the same product
across shops/chains using `rapidfuzz.fuzz.token_set_ratio`, and verified
it against a handful of hand-picked example pairs — all looked correct.
Only when tested against the real ~7,300-item catalog did the real
failure surface: 1,822 of 7,353 items in a *single* shop's own catalog
got merged into the wrong product. `token_set_ratio` treats one name's
tokens being a full subset of another's as a perfect (100) match, which
is exactly the shape of a supermarket catalog full of long descriptive
names sharing brand/generic words — it merged "Coca-Cola Zero" with
"Coca-Cola Zero Χωρίς Καφεΐνη" (with/without caffeine), different soda
brands sharing common descriptive words ("Fanta" with "Έψα"/"Ήβη"), and
more.

**Root cause:** Hand-picked test cases were all either "clearly the same
product, reordered" or "clearly different products" — never the
realistic middle case (a long, mostly-shared name with one differing but
significant word), which is what actually dominates a real catalog. A
handful of examples can't reveal a scorer's systematic bias; only
running against real, full-scale data did.

**Fix:** Switched to `token_sort_ratio` (order-independent but sensitive
to extra/missing words, unlike set_ratio's subset-tolerance). That alone
cut the false-merge rate from 1,822/7,353 to ~600/7,353. The remaining
cases were long near-identical names differing in exactly one embedded
number (fat %, diaper/shoe size, candle/balloon number) — diluted below
the threshold by the surrounding shared text. Added an explicit guard:
reject a match if the leftover embedded numbers (after size-stripping)
differ at all, regardless of text score. Verified the threshold itself
has no better setting — measured that legitimate cross-chain matches
(different unit abbreviation, punctuation) score as low as 87.8, which
overlaps the residual false positives' 90.2–92.7 range, so raising the
threshold trades one error type for the other rather than fixing it.

**Lesson:** A fuzzy/heuristic matcher's hand-picked test cases can look
perfect while hiding a systematic bias that only shows up at real scale
and real data density. Before trusting a similarity scorer's behavior,
run it against the full real dataset (or as much of it as available) and
inspect the highest-count/most-confident groupings by hand — that's
where a systematic bias concentrates and becomes obvious, not in curated
examples.

---

## 2026-08-14 — assumed a CI runner had an apt repo configured because a preinstalled package carried its version string

**Where:** `.github/workflows/daily-ingest.yml`, the backup step

**What happened:** The nightly backup failed with a pg_dump version
mismatch (runner's pg_dump 16 vs Render's Postgres 18 server). The
first fix changed `apt-get install postgresql-client` to
`postgresql-client-18`, reasoning that the runner must have the PGDG
apt repository configured because its preinstalled pg_dump reported
version "16.14-1.pgdg24.04+1" — a PGDG package string. The next run
failed with "Unable to locate package postgresql-client-18": the
image bakes in a PGDG-built package but does not keep the PGDG apt
source configured, so versioned client packages aren't installable
without adding the repo first.

**Root cause:** Inferred available apt packages from a version string
on an already-installed binary instead of from what the image's apt
sources actually serve. A baked-in package proves only that the repo
was reachable at image-build time, not that it is configured now.
The fix was shipped without any way to verify it short of a full
40-minute production run, so the wrong assumption cost a whole cycle.

**Fix:** The workflow now adds the PGDG apt repository itself (keyring
to /usr/share/postgresql-common/pgdg, signed-by entry in
sources.list.d) before installing `postgresql-client-18`.

**Lesson:** Before pinning a versioned package in CI, verify the repo
that serves it is actually in the image's apt sources (runner-images
docs, or an `apt-cache policy` step) — a pgdg/ppa-flavored version
string on a preinstalled tool is not evidence. When a fix can only be
validated by a slow production run, spend the extra minute checking
the premise up front.

---

## 2026-08-13 — string replace mangled a literal period in a unit suffix

**Where:** `price_utils.py`, `format_normalized_price`

**What happened:** Formatted a price as `f'{value:.2f}€/τεμ.'` then called
`.replace('.', ',')` on the whole string to turn the decimal point into a
comma (Greek number formatting). The blind replace also caught the
literal trailing dot in the Greek abbreviation "τεμ.", producing
`"0,06€/τεμ,"` instead of `"0,06€/τεμ."`.

**Root cause:** `.replace('.', ',')` was applied to the fully-assembled
string, not just the numeric part. Any other literal `.` character
composed into the string is fair game for the same bug — trailing
periods, abbreviations, etc.

**Fix:** Format the decimal separator on the number alone
(`f'{value:.2f}'.replace('.', ',')`) *before* concatenating the unit
suffix, never after.

**Lesson:** Never call a blind find/replace on a string after other
literal text has already been appended to it. Do the replace on the
narrowest possible substring, first.

---

## 2026-08-13 — test stub dict missing keys caused a Jinja `Undefined` crash

**Where:** `tests/test_routes.py`, stubbing `webapp.fetch_menu_item`

**What happened:** A test monkeypatched `fetch_menu_item` to return
`{"price": 1.68, "is_available": True, "tags": []}` — missing
`full_price` and `calculated_price`. The template did
`live.full_price|eur`, which raised `TypeError: unsupported format
string passed to Undefined.__format__`.

**Root cause:** In Jinja, dict-style access via dot notation
(`live.full_price`) on a dict *missing that key* returns the special
`Undefined` sentinel, not `None`. A key that exists with value `None`
formats fine; a key that's simply absent does not. Test stubs built by
guessing "the fields the code reads" instead of mirroring the real
API's response shape will miss this distinction.

**Fix:** Stub the full field set the real API actually returns
(`full_price`, `calculated_price`, `is_available`, `tags`, even when
`None`), not just the fields the specific test cares about.

**Lesson:** When stubbing an external API response for a test, include
every key the code path touches with an explicit value (even `None`) —
don't rely on `.get()`-like leniency you haven't verified the consuming
code actually has.

---

## 2026-08-13 — a test triggered a real background network call and leaked global state across the suite

**Where:** `tests/test_routes.py`, POST `/refresh` tests; `webapp.py`
module-level `_refresh_state`

**What happened:** A test posted to `/refresh`, which called the real
`_shops_needing_refresh()` — this found the ~11 tracked shops the test
fixture hadn't seeded and started a real background thread trying to
fetch them from the live e-food API. That thread left the module-level
`_refresh_state["running"]` stuck `True`, which caused an *unrelated*
later test (the dashboard staleness banner) to fail, because the
dashboard template shows the "refresh running" banner ahead of the
"stale" banner.

**Root cause:** `_refresh_state` is process-global, not per-test-request
state, so one test's side effect silently changed another test's
observed behavior — classic shared mutable state between tests. The
route under test had a real, non-mocked side effect (network I/O) that
had nothing to do with what the test was actually checking (response
shape).

**Fix:** Added an autouse `monkeypatch` fixture in `conftest.py` that
forces `_shops_needing_refresh()` to return `[]` for every test, so
`/refresh` never has anything to do and never spawns a thread.

**Lesson:** Before writing a test against a route, check whether it has
side effects beyond the response (background threads, outbound network
calls, module-level globals) and neutralize them explicitly — don't
assume a fresh DB per test also means fresh process state.

---

## 2026-08-13 — picked a lint config that would have required reflowing normal prose

**Where:** `pyproject.toml` (ruff config), first pass

**What happened:** Ran `ruff check` with the default 88-char line length
against the whole repo before adding a config, and got dozens of hits
that were just normal-length Greek UI strings and docstrings, not real
problems.

**Root cause:** Adopted a lint default without first checking whether it
fit the codebase's actual content — a UI string in Greek (or any
non-Latin script with mixed byte-width considerations) commonly runs
longer than 88 columns for a perfectly reasonable sentence.

**Fix:** Set `line-length = 110` in `pyproject.toml`, then fixed the 3
genuine remaining hits (two ambiguous `l` variable names, one function
signature) instead of suppressing rules or reflowing prose.

**Lesson:** When introducing a linter to an existing codebase, run it
unconfigured first to see what the *real* signal-to-noise ratio is
before deciding between "fix everything," "adjust the threshold," or
"suppress the rule" — don't default to the tool's out-of-the-box
settings without checking they fit the content.
