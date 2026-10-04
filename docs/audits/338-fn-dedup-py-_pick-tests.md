# 338 — `_pick` at `dedup.py:159` is nearly unpinned, and the four tests it needs all fail against current code

## Verdict

`_pick` is the whole survivorship engine — `_merge` (`dedup.py:190-194`) calls nothing
else — yet `tests/test_lead_signal.py` never imports it and tests only `_merge` through
8 examples, all of which use the same `source` on both sides. That single choice hides
the four defects that matter: **trust outranks validity** (`dedup.py:180-182`), the
`source` union **launders provenance into trust** (`dedup.py:173-174` +
`dedup.py:148-152`), `_merge` is **commutative but not associative** so `dedup()` is
order-dependent, and `website`'s recency tiebreak is **unreachable** because trust
outranks it. I reproduced all four; three of them change a value in `sales_ready.csv`.
Nine property tests and eleven example tests are specified below; four of the examples
fail today.

---

## What the current suite already pins

| Test | Line | What it actually covers |
|---|---|---|
| `test_trust_is_per_field_not_per_record` | `tests/test_lead_signal.py:278-284` | OSM beats Google on `website`/`email` when OSM is *fresher*. Trust and recency agree, so the priority order is never observed. |
| `test_empty_string_does_not_beat_a_real_value` | `:286-290` | `_is_missing` (pinned, good). |
| `test_merge_is_order_independent` | `:292-295` | Swaps one `_merge`'s two arguments. **Passes for the wrong reason** — neither record has `scraped_at`, so `_recency` is `""` for both (`dedup.py:156`), and the `str(value)` tiebreak (`dedup.py:184`) makes the ranks *unequal* (`"https://b.example" > "https://a.example"`), so `>=` at `:187` never fires. It proves nothing about commutativity and cannot see order dependence. |
| `test_invalid_value_loses_to_valid_one` | `:297-301` | Both records are `source: "osm"` → trust is 3 vs 3 → validity decides. The trust-vs-validity inversion is invisible. |
| `test_volatile_field_prefers_the_fresher_observation` | `:303-309` | `rating` only, same source, 1 day apart. |
| `test_out_of_range_rating_is_rejected` | `:311-314` | Both `google_places` → trust 3 vs 3. Same blind spot. |
| `test_sources_are_unioned_and_ordered` | `:316-319` | Well-formed sources only (`"osm"`, `"google_places"`). |
| `test_no_field_is_lost` | `:321-326` | Complementary (non-conflicting) fields only. |

Nothing touches `_pick`'s `scraped_at` branch (`dedup.py:176-177`), nothing merges
three records, and nothing merges a record with a record that was *itself* merged.

---

## Findings

### S1 — Trust is ranked above validity, so garbage from the trusted source wins

- **Where:** `dedup.py:180-182` — `rank()` returns `(trust, validity, recency, str(value))`
  and tuple comparison is lexicographic, so `rank[0]` dominates `rank[1]`.
- **Breaks:** `_validity` exists specifically to reject implausible values
  (`dedup.py:120-140`), but it can only ever break a tie between equally trusted
  sources. `_SOURCE_TRUST` (`dedup.py:97-110`) gives OSM `website=3, email=3` against
  Google `website=2` and Wikidata `email=2`, so an OSM value that fails validation
  beats a *valid* value from either other source. This is the exact bug the commit
  `99493b9` set out to kill ("a low-quality value in a rich record beat a high-quality
  value in a sparse one") — reintroduced one level down, inside the ranking itself.
- **Trigger:**
  ```python
  _pick("website",
        {"website": "cafebistro.com.lb",  "source": "osm"},          # validity 0
        {"website": "https://real.example", "source": "google_places"})  # validity 3
  # -> 'cafebistro.com.lb'   (both orders agree)
  ```
  `_URL_OK` (`dedup.py:117`) requires a scheme, and OSM's website values come from
  `tags.get("website") or tags.get("contact:website") or tags.get("url")`
  (`scrapers/osm.py:91`) — a scheme-less bare domain is common in OSM. Same for email:
  `_pick("email", {"email": "info@@cafebistro.com.lb", "source": "osm"},
                 {"email": "info@cafebistro.com",      "source": "wikidata"})`
  → `'info@@cafebistro.com.lb'`.
- **Fix:** compare validity before trust (or gate trust on validity being non-zero).

### S1 — The `source` union launders provenance into per-field trust, and the ratchet never releases

- **Where:** `dedup.py:173-174` (union) + `dedup.py:148-152` (`max` over the union)
  + `dedup.py:176-177` (`scraped_at = max`).
- **Breaks:** After `_merge`, the survivor's `source` lists *both* sources, so
  `_trust_for` grants the merged record the **maximum** trust of any constituent for
  **every** field — including fields only one source ever contributed. Trust is
  record-level where it must be field-level. The merged record also inherits the
  **newest** `scraped_at` of the pair, so it asserts its own freshness while carrying
  the older record's value.
- **Trigger:**
  ```python
  _merge({"name":"Cafe","phone":"+96170123456","source":"google_places",
          "address":"12 Old Google St, Beirut, Lebanon",
          "scraped_at":"2025-01-01T00:00:00+00:00"},
         {"name":"Cafe","phone":"+96170123456","source":"osm",
          "address":"New Address, Verdun 1003, Beirut, Lebanon",
          "scraped_at":"2026-09-01T00:00:00+00:00"})
  # source -> 'google_places|osm', scraped_at -> '2026-09-01T00:00:00+00:00'
  # address -> '12 Old Google St, Beirut, Lebanon'   # the STALE google value
  ```
  `_trust_for("address", master)=3` vs `_trust_for("address", fresh_osm)=2`, so the
  year-old Google address beats a nine-month-old fresh OSM one. Measured ratchet after
  one merge: `address 2→3`, `category 2→3`, `name 2→3`, `rating 1→3`, `email 1→3`,
  `website 2→3`. It is monotonic — trust never decreases again.
  This compounds across runs: `main.py:179-180` merges `raw_filtered + master`, and
  `main.py:209` writes the merged `source` back to `all_businesses.csv`, which
  `.github/workflows/scrape.yml:56` re-downloads from Drive every run. Once a row has
  been merged even once, every future fresh observation loses to it on trust.
- **Fix:** carry per-field provenance (the surviving source for that field), or key
  `_trust_for` on the source that supplied the candidate value rather than on the union.

### S1 — `_merge` is commutative but not associative, so `dedup()` is order-dependent

- **Where:** `dedup.py:176-177` (timestamp max) feeding `dedup.py:183`
  (`_recency(rec) if field in _VOLATILE`); folded left-to-right at `dedup.py:208`.
- **Breaks:** The accumulator's `scraped_at` becomes the max of the pair, so on the
  next merge `_recency` judges the *older* member's value using the *newer* member's
  timestamp. Combined with the trust ratchet above, whichever record merges first
  absorbs the other's authority and then wins against the third. Exhaustive search
  over 7 fields × 3 values³ × 3 sources³ × 3 timestamps³ found **1,536
  non-associative triples**.
- **Trigger** (all six input orders through `dedup()`, one phone key):
  ```python
  A = {"name":"Cafe","phone":"+96170123456","rating":4.0,
       "scraped_at":"2026-06-01T00:00:00+00:00","source":"osm"}
  B = {"name":"Cafe","phone":"+96170123456","rating":4.0,
       "scraped_at":"2026-01-01T00:00:00+00:00","source":"google_places"}
  C = {"name":"Cafe","phone":"+96170123456","rating":4.5,
       "scraped_at":"2026-01-01T00:00:00+00:00","source":"google_places"}

  dedup([A, B, C])["rating"] -> 4.0
  dedup([A, C, B])["rating"] -> 4.5     # same three records, different answer
  ```
  Trace: `_merge(A,B)` takes B's value (trust 3 > 1), takes A's timestamp
  (`2026-06-01`), and ratchets `source` to `google_places|osm` (trust 3). Now
  `_trust_for` ties at 3, validity ties at 3, and `_recency(_merge(A,B))` =
  `2026-06-01` — A's timestamp on B's value — so the accumulator beats C.
  With four records the effect is not marginal: over all 24 input orderings,
  `rating` takes the value of the *fresher* Google record in **8** and the other in
  **16**. `main.py:162` collects scrapers via `as_completed`, so delivery order is
  nondeterministic across runs — the master CSV is not reproducible, which is the
  precise defect commit `99493b9` claimed to have eliminated.
- **Fix:** make the fold order-independent (sort candidates by a total key before
  reducing, or track a per-field `(value, source, scraped_at)` triple so recency is
  never borrowed from a sibling).

### S1 — `website`'s recency tiebreak is unreachable, so a stale URL drives a wrong pitch

- **Where:** `dedup.py:114` (`website` ∈ `_VOLATILE`) vs `dedup.py:100,103`
  (OSM `website=3`, Google `website=2`).
- **Breaks:** `_VOLATILE` lists `website` specifically so recency is consulted
  (`dedup.py:183`), but trust is compared first (`dedup.py:180-181`), so a one-year-old
  OSM website beats today's Google website every time and the recency rule never fires.
  `enricher.check_websites` runs *after* dedup (`main.py:187`) and probes whatever URL
  survived (`enricher.py:250`), so `website_live=False` is then recorded for a URL that
  was never the business's site, and `recommend_service` returns the top-tier pitch
  `"Website rebuild + maintenance"` (`pitch_recommender.py:57-58`). The lead ships in
  `sales_ready.csv` (`main.py:202-213`) pitching a rebuild to a business that already
  has a working site.
- **Trigger:**
  ```python
  _merge({"name":"Cafe Beirut","phone":"+96170123456",
          "website":"https://www.facebook.com/oldcafe", "source":"osm",
          "scraped_at":"2024-01-01T00:00:00+00:00"},
         {"name":"Cafe Beirut","phone":"+96170123456",
          "website":"https://cafebeirut.example", "source":"google_places",
          "scraped_at":"2026-10-03T00:00:00+00:00"})["website"]
  # -> 'https://www.facebook.com/oldcafe'   (both argument orders)
  ```
  The existing `test_trust_is_per_field_not_per_record` (`:278-284`) uses the
  *opposite* ordering (OSM fresher), which is the one case where OSM winning is right.
- **Fix:** order the tuple validity → trust → recency, or drop `website` from
  `_VOLATILE` and stop pretending the rule applies.

### S2 — `max()` on `scraped_at` raises `TypeError` on a non-string, killing the run

- **Where:** `dedup.py:177` — `return max(av, bv)`, unguarded.
- **Breaks:** `load_master` casts floats, ints and the `website_live` triple-state
  (`main.py:90-103`) but **never** `scraped_at`, and `cli.py:259` is the only other
  reader. `all_businesses.csv` is a Google-Drive-hosted file that a human can open and
  edit between runs (`.github/workflows/scrape.yml:56` downloads it, `:89` re-uploads
  it). Any non-string in that cell raises inside `dedup`, i.e. after the full scrape.
  `_recency` at `dedup.py:156` does guard with `str(...)`; the two adjacent lines are
  inconsistent, which reads as an oversight rather than a decision.
- **Trigger:** a master cell containing a bare `2026` (what a spreadsheet leaves behind
  when it reinterprets `2026-10-03T09:00:00+00:00` as a date and re-serialises it):
  `_pick("scraped_at", {"scraped_at": "2026-01-01T00:00:00+00:00"},
                      {"scraped_at": 2026})` →
  `TypeError: '>' not supported between instances of 'str' and 'int'`.
  *Severity defended as S2, not S1:* it needs an out-of-band human edit of the Drive
  master, not ordinary operation. The rubric reserves S1 for runs that die, and this
  one does — but the precondition is not guaranteed, and the guard is one line.
- **Fix:** `return max(str(av), str(bv))`, or validate in `load_master`.

### S2 — The `str(value)` tiebreak is lexicographic, and ties are the *normal* case in-run

- **Where:** `dedup.py:184` — `str(value),  # deterministic final tiebreak`.
- **Breaks:** Deterministic, yes; correct, no. For any field with no validity rule
  (`review_count`, `address`, `name`, `region`, `category`, `completeness_score`,
  `lead_score`), the tiebreak compares string representations. `review_count` 9 vs 10
  yields 9. Crucially this is not a rare corner: each scraper computes `scraped_at`
  **once per scrape run** and stamps every record with it (`google_places.py:173`
  → `:190` → `:303`; `osm.py:32` → `:120`; `wikidata.py:67` → `:128`), so two Google
  observations of one business from two of the 68 overlapping queries always tie on
  trust, validity *and* recency, and land on the tiebreak. Of five realistic
  `review_count` pairs measured, **four return the smaller value**:
  `9 vs 10 → 9`, `99 vs 100 → 99`, `12 vs 9 → 9`, `100 vs 99 → 99`
  (only `3 vs 30 → 30` is right). `review_count` then gates
  `recommend_service` tier 3 (`pitch_recommender.py:70-71`), so a business with 100
  reviews is pitched `"SEO audit + visibility upgrade"` instead of the tier below.
  Coordinates hit it too: `_pick("lat", 9.5, 12.0)` → `9.5`.
- **Fix:** make the tiebreak type-aware (numeric compare for numeric fields,
  `casefold()` for text) instead of `str()`.

### S3 — `_sources_of` does not strip or casefold, silently degrading trust to the default

- **Where:** `dedup.py:143-145` (`{s for s in raw.split("|") if s}`) and
  `dedup.py:151` (`_SOURCE_TRUST.get(src, {})`).
- **Breaks:** `_SOURCE_TRUST` lookups are exact, so `"OSM"`, `" osm"` or `"osm "`
  miss every key and fall to `_DEFAULT_TRUST = 1`. Measured:
  `trust(address)` 2 → 1, `trust(email)` 3 → 1. A master row hand-edited in Drive with
  a trailing space silently loses all its authority on the next run, and the union
  writes the damage back: `_pick("source", {"source":" osm"}, {"source":"google_places"})`
  → `' osm|google_places'`. Note `main.resolve_country` *does* strip and upper-case
  (`main.py:60-63`), so the codebase is already inconsistent about this.
- **Fix:** `{s.strip().lower() for s in raw.split("|") if s.strip()}`.

### S3 — The missing-value early return bypasses `source` normalisation

- **Where:** `dedup.py:168-171` returns `bv` before the `source` union at `:173-174`.
- **Breaks:** A malformed `source` on the surviving side is passed through verbatim:
  `_pick("source", {"source": ""}, {"source": "osm|osm"})` → `'osm|osm'` (duplicate
  parts), `'osm|'` → `'osm|'`, `'|'` → `'|'`. These land in the master CSV and are
  re-read every run. Downstream `_sources_of` de-duplicates, so the impact is
  cosmetic plus the S3 above — but the field the commit describes as "unioned and
  ordered" is not always unioned.
- **Fix:** move the `source` branch above the missing-value returns.

### S3 — `_pick` is not commutative when two values stringify identically but differ in type

- **Where:** `dedup.py:187` — `av if rank(a, av) >= rank(b, bv) else bv`. On an exact
  rank tie `>=` returns `av`, so the winner is whichever argument came first.
- **Breaks:** `_pick("rating", {"rating": 4.5, …}, {"rating": "4.5", …})` → `4.5`
  forward and `'4.5'` reversed. Same for `lat`. Exhaustive search: **150**
  non-commutative pairs, **all** of them type-heterogeneity (`4.5` vs `"4.5"`).
  *Currently unreachable end-to-end*: `load_master` casts `rating`/`lat`/`lon` to
  `float` (`main.py:90-95`), `review_count` to `int` (`:96-99`), `website_live` to
  `bool` (`:103`), and every scraper emits `None` for the rest — so no producer
  supplies two types for one field. It becomes live the moment a fourth source or a
  non-`load_master` ingest path is added. This directly contradicts the docstring at
  `dedup.py:165-166` ("a deterministic tiebreak so the merge is commutative").
- **Fix:** compare on a canonical key (`str` for the tiebreak is already close;
  normalise types before ranking) and return the value, not the winner-by-position.

---

## The test cases this function still needs

Place them in a new `class TestPickSurvivorship(unittest.TestCase)` in
`tests/test_lead_signal.py` (import `_pick` at `:65`). The file is stdlib-only by
design (`tests/test_lead_signal.py:4-6`, `python3 -m unittest discover -s tests`), so
every "property" below is written as an **exhaustive `itertools.product` sweep**, not
`@given`. That keeps the bare-checkout promise; adopting `hypothesis` (as
`docs/audits/031-test-suite-design.md:387` recommends) is a separate decision.

### Tier 0 — properties to pin as invariants (all currently pass; keep them that way)

| # | Method name | Property | Pins |
|---|---|---|---|
| P1 | `test_pick_never_returns_a_missing_value` | For all 17 identity fields, over `{None, "", "   ", "\t", "x", 0, 0.0, False, "0"}`² and 4 source pairings: if either side is non-missing, the result is non-missing. **Measured 0 violations.** | the `_is_missing` fix at `dedup.py:79-90` |
| P2 | `test_pick_never_synthesises_a_value` | For all non-`source`/non-`scraped_at` fields, over `{None,"","a","b",1,2,3.5,True,False,0,"https://x.example","a@b.com"}`² × 16 source pairings: result ∈ {`av`, `bv`} by identity or equality. **Measured 0 violations.** | no field can gain a value no observation carried |
| P3 | `test_merge_keys_are_the_union_of_input_keys` | `_merge(a,b).keys() == set(a) \| set(b)` over 7 shape pairs. **Measured 0 violations.** | `dedup.py:192`; the shape half of `docs/audits/024-dedup-merge-data-loss.md:16-21` |
| P4 | `test_merge_is_idempotent` | `_merge(x, dict(x)) == x` over 10 fields × 9 values × 3 sources. **Measured 0 violations.** | keeps the ratchet from becoming value drift |
| P5 | `test_pick_is_commutative` | `_pick(f,a,b) == _pick(f,b,a)` — currently **fails**, see X4; keep as a target assertion | `dedup.py:165-166` docstring claim |
| P6 | `test_source_union_is_a_superset_and_duplicate_free` | `set(result.split("\|")) == _sources_of(a) \| _sources_of(b)` and no repeated part, for all pairs of `{"", "osm", "google_places", "osm\|google_places", "osm\|osm", "wikidata\|osm\|google_places", None, "\|", "osm\|"}`. **Fails** whenever either side is missing — see X6 | `dedup.py:173-174` |
| P7 | `test_missing_covers_none_and_blank_only` | `_is_missing` is True for `{None, "", " ", "\t\n"}` and False for `{"x", 0, 0.0, False, [], {}}` | `dedup.py:86-90` — note `False`/`0` are **not** missing, which is correct for `website_live` and `review_count` and must stay pinned |

Literal input for P1:

```python
def test_pick_never_returns_a_missing_value(self):
    MUT = [None, "", "   ", "\t", "x", 0, 0.0, False, "0"]
    STAMPS = {"scraped_at": "2026-01-01T00:00:00+00:00"}
    for f in ["name", "category", "region", "address", "lat", "lon", "phone",
              "email", "website", "website_live", "facebook", "instagram",
              "whatsapp", "linkedin", "rating", "review_count",
              "completeness_score", "lead_score", "industry_priority",
              "recommended_service"]:
        for av, bv in itertools.product(MUT, repeat=2):
            for sa, sb in [("osm", "osm"), ("osm", "google_places"),
                           ("google_places", "osm"), (None, None)]:
                got = _pick(f, {"source": sa, **STAMPS, f: av},
                               {"source": sb, **STAMPS, f: bv})
                real = lambda v: not (v is None or
                                      (isinstance(v, str) and not v.strip()))
                self.assertFalse(real(got) is False and (real(av) or real(bv)),
                                 f"{f}: {av!r} vs {bv!r} -> {got!r} lost a real value")
```

### Tier 1 — the exact cases, in priority order

**X1 — `test_invalid_value_from_trusted_source_loses_to_valid_value` · S1 · FAILS today**
- Input: `_pick("website", {"website": "cafebistro.com.lb", "source": "osm"}, {"website": "https://real.example", "source": "google_places"})`
- Assert: `== "https://real.example"`, and the same for the reversed argument order.
- Email variant in the same test: `_pick("email", {"email": "info@@cafebistro.com.lb", "source": "osm"}, {"email": "info@cafebistro.com", "source": "wikidata"})` → `"info@cafebistro.com"`.
- Pins: **S1 trust-above-validity**. Closes the blind spot in `tests/test_lead_signal.py:297-301` and `:311-314`, which both use one source on each side. Returns `"cafebistro.com.lb"` / `"info@@cafebistro.com.lb"` today.

**X2 — `test_merged_record_does_not_inherit_max_trust_of_its_constituents` · S1 · FAILS today**
- Input:
  ```python
  master = {"name": "Cafe", "phone": "+96170123456", "source": "google_places",
            "address": "12 Old Google St, Beirut, Lebanon",
            "scraped_at": "2025-01-01T00:00:00+00:00"}
  fresh  = {"name": "Cafe", "phone": "+96170123456", "source": "osm",
            "address": "New Address, Verdun 1003, Beirut, Lebanon",
            "scraped_at": "2026-09-01T00:00:00+00:00"}
  ```
- Assert: `_merge(master, fresh)["address"] == "New Address, Verdun 1003, Beirut, Lebanon"`; and `_trust_for("address", _merge(master, fresh)) <= _trust_for("address", fresh)`.
- Pins: **S1 trust ratchet**. Returns `"12 Old Google St, Beirut, Lebanon"` today, and `_trust_for` goes 2 → 3.

**X3 — `test_merge_is_associative` / `test_dedup_is_independent_of_record_order` · S1 · FAILS today**
- Input: the `A`/`B`/`C` triple from the S1 finding above, driven through all 6 permutations of `dedup()`.
- Assert: `{_pick("rating", …) for each order}` has length 1; specifically `4.5` (the value with Google trust) for all orders.
- Pins: **S1 non-associativity / `scraped_at` laundering**. Measured today: `dedup([A,B,C])→4.0` vs `dedup([A,C,B])→4.5`. Extend to the 4-record case: assert the fresher Google value wins in all 24 orderings (it wins in 8 today).
- Note this is the test `test_merge_is_order_independent` (`:292-295`) *cannot* be: swapping two arguments of one `_merge` is not the same operation as reordering a left fold.

**X4 — `test_stale_trusted_website_does_not_beat_fresher_untrusted_website` · S1 · FAILS today**
- Input:
  ```python
  stale = {"name": "Cafe Beirut", "phone": "+96170123456",
           "website": "https://www.facebook.com/oldcafe", "source": "osm",
           "scraped_at": "2024-01-01T00:00:00+00:00"}
  fresh = {"name": "Cafe Beirut", "phone": "+96170123456",
           "website": "https://cafebeirut.example", "source": "google_places",
           "scraped_at": "2026-10-03T00:00:00+00:00"}
  ```
- Assert: `_merge(stale, fresh)["website"] == "https://cafebeirut.example"`, both argument orders.
- Pins: **S1 unreachable `website` recency** (`_VOLATILE` at `dedup.py:114` vs trust at `:180`). Returns `"https://www.facebook.com/oldcafe"` today. Pair it with an assertion that the merged `scraped_at` still equals the max — otherwise the fix trades one lie for another.

**X5 — `test_scraped_at_max_does_not_raise_on_non_string` · S2 · FAILS today**
- Input: `_pick("scraped_at", {"scraped_at": "2026-01-01T00:00:00+00:00"}, {"scraped_at": 2026})`
- Assert: returns a `str`; assert no exception. Add a round-trip: `write_csv` → `load_master` → `_merge` with the fresh record, asserting no `TypeError` (covers `main.py:77-105` leaving `scraped_at` uncast, reachable via the Drive-hosted master, `.github/workflows/scrape.yml:56`).
- Pins: **S2 `TypeError`**. Raises today: `'>' not supported between instances of 'str' and 'int'`.

**X6 — `test_source_union_normalises_malformed_input` · S3 · FAILS today**
- Inputs: `_pick("source", {"source": ""}, {"source": "osm|osm"})` → assert `"osm"`; `_pick("source", {"source": None}, {"source": "osm|"})` → assert `"osm"`; `_pick("source", {"source": "|"}, {"source": "|"})` → assert `""`.
- Pins: **S3 early-return bypass** at `dedup.py:168-171`.

**X7 — `test_larger_review_count_wins_over_smaller` · S2 · FAILS today**
- Inputs: both records `source: "google_places"`, **identical** `scraped_at` (which is what a real same-run duplicate looks like — `google_places.py:173` stamps one timestamp per run):
  ```python
  A = {"phone": "+96170123456", "review_count": 9,  "source": "google_places",
       "scraped_at": "2026-10-03T09:00:00+00:00"}
  B = {"phone": "+96170123456", "review_count": 10, "source": "google_places",
       "scraped_at": "2026-10-03T09:00:00+00:00"}
  ```
- Assert: `10`, both orders. Table-drive the pairs `[(9,10), (99,100), (12,9), (100,99)]` → assert `max(pair)`. Today all four return the smaller value.
- Coordinate sibling in the same test: `_pick("lat", {…"lat": 9.5}, {…"lat": 12.0})` → `12.0`.
- Pins: **S2 lexicographic tiebreak**.

**X8 — `test_source_whitespace_and_case_do_not_degrade_trust` · S3 · FAILS today**
- Input: `_trust_for("address", {"source": s})` for `s` in `["osm", "OSM", " osm", "osm ", "Osm"]`
- Assert: all `== 2` (and `_trust_for("email", …) == 3`). Plus
  `_pick("source", {"source": " osm"}, {"source": "google_places"})` → `"google_places|osm"`.
- Pins: **S3 `_sources_of` non-normalisation**. Measured today: `1` for every variant but bare `"osm"`, and the union yields `' osm|google_places'`.

**X9 — `test_pick_is_commutative_across_types` · S3 · FAILS today (latent)**
- Inputs: `_pick("rating", {"rating": 4.5, "source": "osm"}, {"rating": "4.5", "source": "osm"})`; same for `lat` with `33.9` / `"33.9"`.
- Assert: both orders agree, including on `type()`. Today: `4.5` forward, `'4.5'` reversed.
- Pins: the `>=`-on-tie hole at `dedup.py:187` and the docstring claim at `:165-166`. Mark it `expectedFailure` with a comment that `load_master`'s casting currently makes it unreachable end-to-end, so the failure documents the latent contract rather than a live bug.

**X10 — `test_pick_falls_through_when_neither_side_has_the_field` · S3 · passes today**
- Assert: `_pick("email", {"name": "X"}, {"name": "X"}) is None`; `_merge({"name": "A"}, {}) == {"name": "A"}`; `_merge({"name": "A"}, {"rating": None}) == {"name": "A", "rating": None}`.
- Pins: P3 and the documented None-vs-absent shape from `docs/audits/024-dedup-merge-data-loss.md:16-21`, so a future refactor cannot silently start dropping or inventing keys.

**X11 — `test_scraped_at_z_suffix_and_offsets_order_chronologically` · S3 · documents a dependency**
- Inputs: `("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00Z")` → assert the `+00:00` form wins (identical instants, so the `Z` form is not "fresher"); `("2026-10-03T08:00:00+03:00", "2026-10-03T06:30:00+00:00")` → assert the `+00:00` form wins (05:00Z vs 06:30Z).
- Pins: the lexical comparison at `dedup.py:177` and `:183`. *Honest caveat:* `httpclient.utc_now_iso` (`httpclient.py:220-229`) always emits `+00:00`, so no producer creates mixed offsets today — but the test file itself mixes the two forms (`tests/test_lead_signal.py:204` uses `Z`, `:305-307` use `+00:00`), and a Drive-edited master can. Today `+` (0x2B) sorts below `Z` (0x5A), so the `Z` form wins regardless of date.

### What the current suite should *stop* implying

`test_merge_is_order_independent` (`tests/test_lead_signal.py:292-295`) currently passes
for an incidental reason and gives false coverage of commutativity. Either rename it to
what it tests (`test_two_way_tiebreak_is_order_symmetric`) or replace it with P5/X3. As
written it is the only test whose name claims a property the function does not have.

---

## What genuinely cannot be tested here, and why

- **Whether the surviving `website` is the business's real current site.** That needs
  the network. A unit test can only pin the *rule*, and the rule is what is broken
  (X4). Proving the business moved requires a live fetch — a `vcrpy` cassette of two
  scrapes months apart (`docs/audits/031-test-suite-design.md:17`), which `tests/` has
  no fixture infrastructure for today.
- **Whether `website_live` describes the surviving `website`.** `enricher.check_websites`
  runs *after* dedup (`main.py:187`), so the coupling is temporal, not visible from
  `_pick`'s arguments. Needs an integration test with a stubbed fetch, not a unit test.
  The unit-testable half is X4.
- **Whether the `_SOURCE_TRUST` numbers are the *right* numbers.** `osm.website = 3` vs
  `google_places.website = 2` is a product judgement. A test can pin the literals, which
  would freeze an arbitrary decision and make any future retuning look like a
  regression. Test the *ordering* semantics (X1, X4), not the constants.
- **Field-level provenance for a discarded value.** `_pick` returns one value and drops
  the other; there is nothing to assert about the loser. Audit 024 flagged this as
  "no per-field provenance or conflict record"
  (`docs/audits/024-dedup-merge-data-loss.md:12`); it is a design gap, not a test gap.
- **Output ordering of `dedup()`** (`phone_records + name_records`, `dedup.py:222-232`).
  It is deterministic (dict insertion order) but unspecified and incidental. Asserting
  it would ossify an accident. Assert set-of-keys or per-key contents instead.
- **Reachability of X9 through the real pipeline.** `load_master` (`main.py:90-103`)
  normalises every field where two types could collide, so the type-heterogeneity
  commutativity hole is latent. Only a deliberate unit test on `_pick` will hold the
  contract; an end-to-end test cannot reach it without inventing a fourth source.
- **Ratcheting across runs.** X2 pins one merge. That the damage compounds through
  Drive (`main.py:209` → `.github/workflows/scrape.yml:56`) needs two synthetic
  `write_csv`/`load_master` cycles in a temp dir. Doable offline, and worth adding once
  X2 is fixed — but the single-merge assertion is the one that fails today.

---

## Not a bug, but worth knowing

- **`phone` is decided by string comparison, and the raw national form wins.** `_pick`
  between `"+96170123456"` and `"70123456"` returns `"70123456"`, because `+` is 0x2B
  and `7` is 0x37. Harmless in practice: `main.py:194` re-normalises the phone after
  the merge. But it means `_pick` is choosing on a field where no validity rule exists
  and the comparison carries no meaning. Covered incidentally by P2.
- **Two different phones on one business are silently collapsed to one**
  (`_merge({"phone": "+96170123456"}, {"phone": "+96170123457"})` → `"+96170123457"`).
  For an outbound sales team a second number is a real asset, so this is lost revenue,
  not just lost fidelity. It is a schema limitation (one `phone` column at
  `main.py:36`), not a `_pick` bug — flag it for `docs/audits/034-data-model.md`, do not
  fix it here.
- **`website_live` is in `_VOLATILE` but can only ever come from the master.** Every
  scraper emits `website_live=None` (`osm.py:109`, `google_places.py:292`,
  `wikidata.py:117`), and `enrich` runs after dedup, so at merge time the field is
  either `None` (fresh record, early return) or a previous run's verdict (master).
  The recency rule for it (`dedup.py:183`) is therefore only ever exercised by two
  master rows colliding on one key. Low value to test; low value in the code.
- **`_is_missing` correctly treats `False` and `0` as present.** Verified: `_pick`
  between `website_live=False` and `website_live=True` returns `True`
  (`"True" > "False"`), and between `review_count=0` and `3` returns `3`. This is right
  — `False` is a real observation for `website_live`. Pin it as P7 so a future
  "falsy means missing" refactor does not turn every dead site into an unknown one and
  silently demote the top-tier pitch (`pitch_recommender.py:57-58`).

---

## Recommended order of work

1. **X1, X2, X3** — the three S1s. They are three facets of one defect: the rank tuple
   at `dedup.py:180-185` puts record-level proxies (union-derived trust, borrowed
   timestamps) above the per-value checks. Fixing the tuple order and giving the
   survivor field-level provenance fixes all three plus S2 X7. Land the tests first,
   confirm red, then fix.
2. **X4** — separate S1, separate fix: reorder or drop `website` from `_VOLATILE`
   (`dedup.py:114`). Money impact is direct (wrong Tier-1 pitch in `sales_ready.csv`).
3. **P1-P4, P6, P7** — the invariants that currently hold. Cheap, and they are what
   stops the fix above from introducing new drift. Add them in the same commit as 1-2.
4. **X5** — one-line guard at `dedup.py:177`, plus a `scraped_at` cast in
   `load_master`. Removes a whole-run failure mode from a Drive-hosted file.
5. **X6, X8** — normalise `source` in `_sources_of` (`dedup.py:143-145`) and hoist the
   union branch above the missing-value returns. Together they stop whitespace damage
   from compounding across runs.
6. **X9-X11** — latent-contract and dependency-documentation tests. X9 is cheap and
   stops the next new source from turning a documented lie into a real one.
7. **Re-export `_pick`** from the test module's import at `tests/test_lead_signal.py:65`
   and add `_validity` / `_trust_for` where a test needs to assert the ranking inputs
   rather than the ranking outcome — asserting on the tuple is what makes X2 legible.