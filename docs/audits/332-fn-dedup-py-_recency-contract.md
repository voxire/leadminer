# 332 — `_recency` contract: honest return type, dishonest clock

## Verdict

`_recency` (`dedup.py:155-156`) reads **exactly one** field, `scraped_at`, and **writes
nothing** — and `scraped_at` *is* declared in `BusinessRecord` (`scrapers/base.py:27`). So the
answer to "what does it touch that isn't in the TypedDict?" is **nothing**, and its declared
return type `str` is *honest*: the `str()` wrapper at `dedup.py:156` makes the function total.
The contract that is broken is the **semantic** one. `_recency` is a *"when was this record
scraped"* clock being consumed at `dedup.py:183` as a *"when was this particular field
observed"* oracle — and `scraped_at` provably is not that clock, because `dedup.py:176-177`
advances it with `max()` on every merge and because every scraper fixes it **once per run**
(`scrapers/google_places.py:173`, `scrapers/osm.py:32`, `scrapers/wikidata.py:67`). The
consequence is a wrong pitch in the shipped CSV (S1), and a *silent* fall-through to a
lexicographic string compare that inverts integer ordering whenever recency ties — which, for
any same-source same-run merge, it always does (S2).

---

## Contract, as declared vs. as implemented

| | Declared | Actual |
|---|---|---|
| parameter | `record: dict` (`dedup.py:155`) | bare `dict` = `dict[Any, Any]`, **not** `BusinessRecord` — no checker guards the key access |
| key read | `scraped_at: str`, **required**, total (`base.py:27`) | **optional** — `.get()` at `dedup.py:156`; key may be absent |
| value read | `str` | `Any`; falsy values (`None`, `0`, `False`, `0.0`) all collapse to `""` |
| return | `str` | always `str` ✅ — the one part of the contract that holds |
| mutation | — | none; pure read |

**Fields read/written that are not in the TypedDict: zero.** Proven by reading the body —
`dedup.py:156` touches `scraped_at` and nothing else, and the record is never mutated.
This is worth stating explicitly because it is a *narrower* contract than the type implies:
the function is written defensively for states (`key absent`, `None`, `int`, `datetime`) that
`BusinessRecord` declares impossible. Every one of those defensive branches is either
unreachable today or a live bug (see S2/S3) — the type is wrong, and the function is
compensating for it in a place where the compensation is incomplete.

**Use sites of the return value: exactly one.** `dedup.py:183`, as element 3 of the `rank()`
tuple, consumed by the tuple comparison at `dedup.py:187`.

---

## Findings

### S1 — `_recency` reads *scrape* time as a *field-observation* clock, so a months-stale `website_live` verdict wins and flips the pitch

- **Where:** `dedup.py:183`, `dedup.py:114` (`_VOLATILE` includes `website_live`), with
  `dedup.py:176-177` (`scraped_at` = `max()` over every merge).
- **Why this field specifically:** `website_live` is the **only** member of `_VOLATILE` for
  which `_recency` is the *sole* deciding axis. No source in `_SOURCE_TRUST`
  (`dedup.py:97-110`) declares `website_live`, so `_trust_for` returns `_DEFAULT_TRUST = 1`
  for both sides; and `_validity` (`dedup.py:120-140`) has no branch for it, returning `1`
  for any non-missing value. Verified:

  ```
  _VOLATILE = ['rating', 'review_count', 'website', 'website_live']
     rating         {'google_places': 3, 'osm': None, 'wikidata': None}
     website        {'google_places': 2, 'osm': 3, 'wikidata': 2}
     website_live   {'google_places': None, 'osm': None, 'wikidata': None}   <-- all default
  ```

  So `website_live` survivorship is decided **entirely** by `_recency`. And `_recency` returns
  `scraped_at`, which `enricher.check_websites` (`enricher.py:250`) writes *after* dedup
  (`main.py:180` → `main.py:187`) and which `_pick` advances to `max()` on every merge — so it
  means "last seen by any scraper", never "when the site was last HTTP-GETted". The two clocks
  are unrelated and drift apart.
- **Breaks:** a `website_live=True` verdict written by an enrich pass in January, on a row that
  has since been re-scraped in June (bumping `scraped_at`), outranks a June `website_live=False`.
  That flips `lead_score` by 10 (`enricher.py:300-304`: dead site = +20 "rebuild pitch") and
  flips `recommended_service` entirely.
- **Trigger** (verified end-to-end against the real `dedup`, `enricher.lead_score`,
  `pitch_recommender.recommend_service`; only `requests`/`urllib3` were stubbed, no network):

  ```python
  BASE = dict(name="Al Mazraa Bakery", category="bakery", address="Main St, Beirut",
              country="LB", website="https://mazraa.example", region="Beirut")
  A = dict(BASE, phone="12345",         website_live=True,  source="osm",
           scraped_at="2026-06-05T00:00:00+00:00")   # verdict is from JANUARY
  B = dict(BASE, phone="+96170123456",  website_live=False, source="google_places",
           scraped_at="2026-06-01T00:00:00+00:00")   # verdict is from JUNE 1 = current
  ```
  Output:
  ```
  survivor website_live = True    <-- 5-month-stale verdict wins
    website_live=True   lead_score=30  service=SEO audit + visibility upgrade
    website_live=False  lead_score=40  service=Website rebuild + maintenance
  ```
  The sales team is pitched *"SEO audit"* at a shop whose site is confirmed dead.
- **Reachability note (be fair to the code):** all three scrapers hardcode `website_live=None`
  (`osm.py:109`, `google_places.py:292`, `wikidata.py:117`), and `enrich` runs *after* `dedup`.
  So a fresh record can never supply a competing bool. Two non-`None` bools only meet inside
  `_merge` when **two master rows merge** — which the cumulative master *does* produce, because
  `dedup` is not idempotent across runs (it splits on phone-index vs name-index at
  `dedup.py:205-231`). Verified:

  ```python
  run1 = [{... "phone":"12345",        "source":"osm"}]            # junk phone -> name-keyed
  master = dedup(run1)                                              # 1 row
  run2 = [{... "phone":"+96170123456", "source":"google_places"}]   # real phone -> phone-keyed
  master2 = dedup(run2 + master)     # 2 rows for ONE business
  ```
  Any business whose phone improves between runs therefore accumulates duplicate master rows,
  and those are exactly the rows that later collide inside `_merge`. This is the same root
  cause flagged in `020-type-contract-import-cycle.md` S8 and `035-incremental-scraping.md` S2;
  `_recency` is where it turns into a wrong answer.
- **Fix:** stop inferring observation time from `scraped_at`. Write
  `r["website_checked_at"] = utc_now_iso()` next to `enricher.py:250`, add it to `_VOLATILE`'s
  companion map, and have `_recency` take the clock *field name* as a parameter:
  `_recency(record, "website_checked_at")`. Until that exists, drop `website_live` from
  `_VOLATILE` so `_recency` is not consulted for a field it cannot date — `str(True)`/`str(False)`
  is at least a stable, documented last resort.

### S2 — Intra-run recency is a *guaranteed* tie, so `_VOLATILE` fields fall through to a lexicographic `str(value)` compare that inverts integers

- **Where:** `dedup.py:184` (`str(value),  # deterministic final tiebreak`), reached whenever
  `dedup.py:183` returns equal values.
- **Breaks:** each scraper computes `scraped_at` **once per run** and threads that single string
  through every record — `google_places.py:173` (shared across 68 queries and 5 workers via
  `:190`), `osm.py:32`, `wikidata.py:67`. So for any merge of two records from the same source
  in the same run, `_recency(a) == _recency(b)` **always**. The tiebreak then compares
  `"9"` against `"100"`, and `"9" > "100"`. `review_count` is an int with variable width, so
  this inverts; `rating` is a float that happens to share a `"4."` prefix and mostly does not.
- **Trigger** (verified):

  ```python
  T = "2026-10-03T12:00:00.123456+00:00"   # one clock for the whole run
  A = dict(name="Cafe Roasters", source="google_places", scraped_at=T,
           phone="+96170123456", review_count=9,   rating=4.6, website="https://cafe.example")
  B = dict(A, review_count=100)                       # second place_id, same phone
  _merge(A, B)["review_count"]  ->  9      # "9" > "100"
  ```
  Two `place_id`s for one chain sharing a phone is not exotic, and `seen_ids`
  (`google_places.py:181`) dedups by `place_id` **only** — it never checks the phone, so both
  records reach `phone_index` and merge. Downstream, `pitch_recommender.py:70` branches on
  `review_count < 20`:
  ```
  survivor=9   -> recommend_service = "SEO audit + visibility upgrade"
  survivor=100 -> recommend_service = "RTYLR commerce OS (POS, online ordering, CRM)"
  ```
  A 100-review cafe is pitched an SEO audit it does not need. Silent, wrong, in a single run.
- **Also reachable with equal recency across runs:** two master rows whose `scraped_at` cells are
  both blank load as `None` (`main.py:88-89`), so `_recency` returns `""` on both sides
  (verified) and the same lexicographic rule applies to `rating`/`review_count`.
- **Fix:** make the final tiebreak type-aware instead of `str(value)` — rank numerics with a
  `(0, float(value))` tuple and strings with a `(1, str(value))` tuple so the two never
  compare across representations. One line:
  `float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)`.

### S2 — `_pick` never calls `_recency` for `scraped_at` itself: two different comparison contracts for one field, 20 lines apart

- **Where:** `dedup.py:155-156` vs `dedup.py:176-177`.
- **Breaks:** `_recency` is careful — `.get()`, `or ""`, `str()` — because its author expected
  `scraped_at` to be missing or non-string. Twenty lines later, `_pick` handles *the same field*
  with `return max(av, bv)` on the **raw, uncoerced** values. The defensive helper is applied
  everywhere except on the field it was written for. Both `_is_missing` guards
  (`dedup.py:168-171`) only catch `None`/`""`; `_is_missing(0)` is `False` (verified), so a
  falsy-but-present value falls straight into `max()`.
- **Trigger** (both verified to raise `TypeError: '>' not supported between...`):

  ```python
  _merge({"scraped_at": 1767225600,          ...}, {"scraped_at": "2026-01-01T00:00:00+00:00", ...})
  _merge({"scraped_at": datetime.datetime(2026,9,1), ...}, {"scraped_at": "2026-01-01T00:00:00+00:00", ...})
  ```

  A crash inside the merge kills the run after scraping has already spent the budget.
- **Reachability today: latent.** `load_master` (`main.py:77-105`) casts only `_FLOAT_FIELDS`
  and `_INT_FIELDS` (`main.py:43-44`), and `scraped_at` is in neither, so a CSV cell stays a
  string; the three scrapers all pass `utc_now_iso()` (`scraped_at: str`) directly. Nothing
  can emit a non-`str` today. That is exactly why it is S2 and not S1 — but it is the cheapest
  fix in this file and it removes a landmine from the one field every future producer must set.
- **Fix:** `return max(_recency(a), _recency(b))` at `dedup.py:177`. One expression, and the
  two halves of the timestamp contract stop disagreeing.

### S2 — `_recency` is unreachable for `website` whenever the two sources disagree on trust, contradicting its own docstring

- **Where:** `dedup.py:113` comment, `dedup.py:180-185` tuple order.
- **Breaks:** the comment at `dedup.py:113` claims "Fields whose value changes over time, so
  the fresher observation wins." The tuple is `(trust, validity, recency, str(value))`
  (`dedup.py:180-185`) — `trust` is element 0, so Python short-circuits and `_recency` is
  **never evaluated** whenever trust differs. For `website`,
  `_SOURCE_TRUST` gives `osm: 3` vs `google_places: 2` (`dedup.py:99-105`), so a fresh Google
  website loses to a 6-year-old OSM one. Verified:

  ```python
  g = {... "website":"https://maps.app.goo.gl/REAL", "source":"google_places",
         "scraped_at":"2026-10-01T00:00:00+00:00"}
  o = {... "website":"http://cafe-real.example",   "source":"osm",
         "scraped_at":"2020-01-01T00:00:00+00:00"}
  _merge(g, o)["website"] -> "http://cafe-real.example"   # 6 years staler, still wins
  ```
- **Assessment:** for OSM-vs-Google that outcome is *probably desirable* (OSM carries the real
  domain, Google a `goo.gl` shortlink). The defect is that membership in `_VOLATILE` silently
  implies recency matters, and it doesn't — so nobody reading `_VOLATILE` can tell which fields
  it actually governs. That is a maintainability trap with a data-quality edge (Google-vs-
  Wikidata `website`, both trust `2`, *is* recency-decided, and picks whichever shortlink is
  newer).
- **Fix:** either reorder to `(validity, trust, recency, ...)` and state the precedence in the
  comment, or split `_VOLATILE` into `_VOLATILE_AT_EQUAL_TRUST` and document that trust wins.

### S3 — `_recency`'s `str()` coercion silently misorders every non-string clock, in both directions

- **Where:** `dedup.py:156`.
- **Breaks:** `str()` normalises the *type* so `dedup.py:183` never raises, but it does not
  normalise the *ordering*, and it conflates three distinct states — absent key, `None`, and any
  falsy value — into one indistinguishable `""`. Verified:

  | input | `_recency` | effect |
  |---|---|---|
  | `1767225600` (epoch int) | `'1767225600'` | `'1' < '2'` → sorts below **every** ISO string → treated as infinitely stale, no warning |
  | `True` (bool) | `'True'` | `'T' > '2'` → sorts above every ISO string → treated as infinitely fresh |
  | `0`, `False`, `0.0` | `''` | identical to "absent" |
  | `'not-a-date'` | `'not-a-date'` | `'n' > '2'` → infinitely fresh |

  No exception, no log, no metric. A misconfigured producer that writes epoch seconds silently
  freezes every volatile field at its oldest value; one that writes a bool silently freezes it
  at its newest.
- **Fix:** validate once at the boundary instead of coercing at every comparison — parse
  `scraped_at` with `datetime.fromisoformat` in `utc_now_iso()`'s callers and reject
  non-ISO input loudly; make `_recency` return `float("-inf")`-equivalent ordering by mapping
  unparseable values to a distinct sentinel so they lose to every real timestamp rather than
  winning by accident.

### S3 — `_VOLATILE` and the clock it reads are declared independently, so they can drift

- **Where:** `dedup.py:114` (`_VOLATILE`, a bare set of field names) vs `dedup.py:183` /
  `dedup.py:156` (the hardcoded literal `"scraped_at"`).
- **Breaks:** `_VOLATILE` says *which fields need a clock*; nothing says *which clock*. Today
  every volatile field borrows `scraped_at`, which is only correct for `rating`/`review_count`
  (produced by `google_places.py:297-298` in the same pass as `scraped_at` at `:303`) and wrong
  for `website_live` (produced by `enricher.py:250`, a different pass, months apart) — that is
  S1. `_merge` also iterates `set(a) | set(b)` (`dedup.py:192`), so any *new* clock column added
  downstream would be merged generically but would neither be `max()`-ed nor be read by
  `_recency`, silently behaving like a non-volatile field. Note that `035-incremental-scraping.md`
  §279 already recommends exactly such a column (`website_checked_at`) — following that
  recommendation without pairing it with a clock map re-creates S1 in a new place.
- **Fix:** replace `_VOLATILE` with an explicit map, `_VOLATILE_CLOCK = {"rating": "scraped_at",
  "review_count": "scraped_at", "website_live": "website_checked_at", "website": "scraped_at"}`,
  and pass the clock name into `_recency`. The compiler then forces every new volatile field to
  declare its clock.

---

## Not a bug, but worth knowing

I checked and **ruled out** four plausible-looking defects. Recording them so nobody re-derives
them:

- **`Z` vs `+00:00` suffix is benign.** The codebase genuinely has two serializations — the
  project's own fixture writes `"2026-01-01T00:00:00Z"` (`tests/test_lead_signal.py:204`) while
  `utc_now_iso` emits `+00:00` (`httpclient.py:229`). But the suffix only decides the comparison
  when the date-time prefix is already identical, i.e. the two strings denote the *same instant*,
  so either winner is chronologically correct. Verified in both directions (`'Z'`=0x5A beats
  `'+'`=0x2D, `'+'` beats `'.'`=0x2E — but only on ties). No fix needed for ordering; the
  representation still should be unified for display (`063-output-contract-for-humans.md`).
- **Variable microsecond precision from `utc_now_iso()` is benign.** `datetime.isoformat()`
  defaults to `timespec="auto"`, so the string is 25 chars when `microsecond == 0` and 32
  otherwise — a non-fixed-width clock, which is normally fatal for lexicographic ordering. It
  happens to work: at the same whole second `'.'`(0x2E) > `'+'`(0x2D), so the fractional form
  (always the later instant) wins. Verified. The uniformity is luck, not design —
  `httpclient.py:225`'s own comment flags the *precision* concern without noting it is currently
  masked by byte ordering.
- **Naive vs offset-aware is benign.** The previous `datetime.utcnow()` produced
  `"2026-10-03T12:00:00.123456"` with no suffix; `utc_now_iso` produces the same string plus
  `"+00:00"`, so the naive form is always a strict prefix and always sorts lower. Fine.
- **The single use site is type-safe.** `dedup.py:183` places `_recency(rec)` at element 3 and
  `""` at the same position for non-volatile fields, and `dedup.py:187` compares tuples
  element-wise with matching positions. Both sides are always `str`. This is exactly what
  `dedup.py:156`'s `str()` wrapper buys, and it means **no crash originates at line 183** — every
  crash path for a malformed `scraped_at` goes through `dedup.py:177` instead (S2 above).
- **Non-UTC offset / space-separated forms *would* invert ordering, but nothing produces them.**
  `"2026-10-03T14:00:00+03:00"` (11:00 UTC) beats `"2026-10-03T12:00:00+00:00"` (12:00 UTC) under
  `max()`, and `"2026-10-03 23:00:00"` — which is what `str(datetime)` yields — loses to
  `"2026-10-03T01:00:00+00:00"` despite being 22 hours later. Both verified. Latent only:
  `utc_now_iso()` (`httpclient.py:227-229`) hardcodes `timezone.utc`. Keep this in mind if the
  clock is ever localized.

---

## Recommended order of work

1. **S2 (tiebreak)** — `dedup.py:184`: make the final tiebreak numeric-aware. One line, fixes a
   demonstrable wrong `review_count` and wrong `recommended_service` within a single run, and
   closes the whole class of "recency tied → string compare" failures.
2. **S1 (stale verdict)** — add `website_checked_at` at `enricher.py:250` and clock
   `website_live` from it. This is the only finding that changes what a salesperson is told.
   Depends on the `_VOLATILE_CLOCK` map from item 3.
3. **S3 (drift)** — replace `_VOLATILE` with an explicit field→clock map and parameterize
   `_recency` by clock name. Do this *with* item 2, not after, or item 2 re-creates S1 elsewhere.
4. **S2 (`max()` bypass)** — `dedup.py:177`: `return max(_recency(a), _recency(b))`. Latent
   today; one expression; removes the crash path and unifies the two contracts.
5. **S2 (trust precedence)** — document or reorder `rank()` at `dedup.py:180-185` so `_VOLATILE`
   membership stops implying something it does not guarantee.
6. **S3 (coercion)** — stop coercing at comparison time; validate `scraped_at` once at the
   boundary and fail loudly on non-ISO input.
7. **Prerequisite, owned by the dedup-identity work, not this function:** make `dedup`
   idempotent so the cumulative master stops accumulating duplicate rows for one business
   (`dedup.py:205-231`). S1 needs that state to exist; eliminating it removes S1's trigger
   while S1's clock confusion stays latent until `website_checked_at` lands.
