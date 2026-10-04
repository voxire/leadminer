# 328 — `dedup._recency` — correctness

## Verdict

`_recency` (`dedup.py:155-156`) is a two-line function that is **correct in isolation but
structurally inert**. It is consulted in exactly one place — `dedup.py:183`, as the *third*
element of a four-element rank tuple — and for every field where it could change an answer it is
either unreachable or dominated: `website_live` never reaches `rank()` at all, and
`website`/`rating`/`review_count` lose on the `trust` key compared one element earlier
(`dedup.py:181`). The result is that a month-old dead OSM `website` plus `website_live=False`
permanently outranks today's live Google scrape, and `recommend_service` pitches **"Website
rebuild + maintenance"** to a business that launched a site last week. That is S1: it is a wrong,
customer-facing answer in the product's core output.

Second finding, also S1-class: because all records from one source in one run share a single
`scraped_at` (`osm.py:32`, `wikidata.py:67`, `google_places.py:173` each call `utc_now_iso()`
once per `scrape()`), the tie that `_recency` was added to break instead falls through to
`str(value)` at `dedup.py:184` — and lexicographic string comparison of integers is numerically
wrong. `review_count` 120 loses to 9.

---

## Findings

### S1 — `_recency` is third in the tuple, so `trust` decides; a stale OSM `website` + `website_live=False` beats today's scrape forever

- **Where:** `dedup.py:114` (`_VOLATILE` contains `"website"` and `"website_live"`),
  `dedup.py:179-187` (`rank()`), specifically `dedup.py:181` (`_trust_for` compared **before**)
  and `dedup.py:183` (`_recency` compared **after**).
- **Breaks:** the comment at `dedup.py:113-114` states the intent — *"Fields whose value changes
  over time, so the fresher observation wins."* The code does not do that. Because `_trust_for`
  is element 0 of the tuple, recency is only ever reached when trust **and** validity tie.
  `_SOURCE_TRUST` gives `osm["website"] = 3` (`dedup.py:103`) but `google_places["website"] = 2`
  (`dedup.py:100`). So any OSM-sourced website beats any Google-sourced website no matter how old
  it is. Since `all_businesses.csv` is a **cumulative master merged into every run**
  (`main.py:150`, `main.py:179`), the stale row is always present. The same applies to
  `rating`/`review_count` (Google 3 vs OSM/Wikidata default 1, `dedup.py:99-109`).
- **Trigger** (confirmed end-to-end through `dedup(fresh + master)`, the exact `main.py:179`
  ordering):

  ```python
  master = [{"name":"Cafe Maroni","phone":"+96170123456",
             "website":"http://cafe-maroni-2015.example","website_live":False,
             "source":"osm", "scraped_at":"2026-09-01T09:00:00+00:00"}]
  fresh  = [{"name":"Cafe Maroni","phone":"+961 7 012 3456",
             "website":"https://cafemaroni.example","website_live":None,
             "source":"google_places","scraped_at":"2026-10-03T12:00:00.517293+00:00"}]
  ```

  Actual output:

  ```
  rows: 1
    website     -> 'http://cafe-maroni-2015.example'     # dead 2015 URL survives
    website_live-> False                                  # stale liveness survives
    source      -> 'google_places|osm'
    scraped_at  -> '2026-10-03T12:00:00.517293+00:00'    # proof we SAW the new URL
    recommend_service -> 'Website rebuild + maintenance' # pitch_recommender.py:33-35
  ```

  The output row simultaneously records today's timestamp, Google's provenance, *and* the dead
  URL — the record is internally contradictory, and the pitch is wrong. Change `master`'s
  `source` to `"google_places"` (equal trust) and the same input correctly yields
  `'https://cafemaroni.example'` — proving the discriminator is trust, not recency.
- **Fix:** for fields in `_VOLATILE`, compare recency *before* trust —
  `( _recency(rec) if field in _VOLATILE else "", _trust_for(field, rec), _validity(...), ... )`.

---

### S2 — Within a run `_recency` is a constant, so `str(value)` decides; `review_count` 120 loses to 9

- **Where:** `dedup.py:183-184` (the `_recency` element, then `str(value)`),
  combined with `osm.py:32`, `wikidata.py:67`, `google_places.py:173`, which each call
  `utc_now_iso()` **once per `scrape()`** and stamp every record with it
  (`google_places.py:173` → `google_places.py:190` → `google_places.py:303`).
- **Breaks:** all `google_places` records in one run have byte-identical `scraped_at`. When two
  of them collide on the same `normalize_phone` key (duplicate listings for one business with
  distinct `place_id`s — `seen_ids` at `google_places.py:181` dedups by place id, not by phone),
  the rank tuple's element 2 ties, and element 3 becomes the decider. `str(value)` on an `int` is
  lexicographic, so any count ≥ 10 loses to any single-digit count. `_validity` gives
  `review_count` a flat `1` (`dedup.py:140`, it is not in the special-case list at
  `dedup.py:124-139`), so nothing catches this.
- **Trigger** (confirmed):

  ```python
  run_ts = "2026-10-03T12:00:00.517293+00:00"   # the one value both records carry
  a = {..., "phone":"+96170000001", "review_count":120, "rating":4.6, "scraped_at":run_ts, "source":"google_places"}
  b = {..., "phone":"+96170000001", "review_count":  9, "rating":4.2, "scraped_at":run_ts, "source":"google_places"}
  dedup([a, b])[0]
  ```

  ```
  review_count -> 9   (true: 120)
  rating       -> 4.6 (true: 4.6)
  ```

  `_recency(a) == _recency(b)` is `True`, confirming it never fired. This propagates to
  `pitch_recommender.py:70` (`review_count < 20`) and to `main.py:44`
  (`_INT_FIELDS`), so a 120-review prospect is scored as a 9-review prospect. `rating` escapes
  only by luck: single-digit integer parts make `str(float)` ordering coincide with numeric
  ordering.
- **Fix:** insert a numeric key ahead of `str(value)`, e.g.
  `-abs(float(value)) if field in ("rating", "review_count") and isinstance(value, (int, float)) else 0`.

---

### S2 — `_recency` compares raw ISO strings; any non-UTC offset inverts the ordering

- **Where:** `dedup.py:155-156` (the raw `str()` return), `dedup.py:183` (used as an ordering
  key), and the identical flaw at `dedup.py:177` (`return max(av, bv)` for `scraped_at` itself).
- **Breaks:** the function's entire notion of "later" is ASCII ordering of the timestamp *text*.
  That is only chronological when every input is UTC. `utc_now_iso()`
  (`httpclient.py:220-229`) emits `2026-10-03T19:34:06.002882+00:00`, so today's producers are
  fine — but the moment a non-UTC offset appears, wall-clock prefixes sort before the offset is
  ever read.
- **Trigger** (confirmed):

  ```python
  a = {"phone":"+96170123456","review_count":500,
       "scraped_at":"2026-10-01T14:00:00+03:00","source":"google_places"}   # = 11:00Z
  b = {"phone":"+96170123456","review_count":5,
       "scraped_at":"2026-10-01T10:00:00+00:00","source":"google_places"}   # = 10:00Z
  ```

  ```
  a is NEWER (11:00Z > 10:00Z): True
  _recency(a) > _recency(b)   : True      # correct outcome, by luck of magnitude
  a = "...T12:00:00+03:00"  vs b = "...T11:00:00+00:00"
  a is OLDER than b (09:00Z < 11:00Z): True
  _recency(a) > _recency(b)  : True      # WRONG
  merged website -> 'https://new.example'   (expected 'https://old.example')
  ```

  Reachability today is **latent, not live**: all three scrapers route through `utc_now_iso()`.
  But nothing in the pipeline validates the shape of `scraped_at` — `load_master`
  (`main.py:86-104`) casts `rating`/`review_count`/`website_live` and passes `scraped_at`
  through as raw CSV text, and `BusinessRecord.scraped_at: str` (`base.py:27`) is a TypedDict
  annotation, not a runtime check. One hand-edited row, one `cli` import, one future
  `datetime.now().astimezone()` refactor (which is the *correct* refactor) and this becomes live.
- **Fix:** parse and compare aware datetimes —
  `datetime.fromisoformat(v.replace("Z", "+00:00"))`, falling back to `datetime.min` on failure,
  and use the same helper at `dedup.py:177`.

---

### S3 — Mixed `Z` and `+00:00` in the cumulative master inverts ordering inside the same second

- **Where:** `dedup.py:156` and `dedup.py:177`; producer changed in commit `607e729`
  (`datetime.utcnow().isoformat() + "Z"` → `utc_now_iso()` → `+00:00`).
- **Breaks:** `all_businesses.csv` is cumulative and merged every run (`main.py:150`,
  `main.py:179`), so it necessarily contains rows written before and after `607e729`. `Z` is
  `ord 90`, `.` is `ord 46`, so a whole-second `Z` stamp outranks any fractional `+00:00` stamp
  in the same second — including one that is 1µs newer.
- **Trigger** (confirmed):

  ```python
  old = "2026-10-01T12:00:00Z"               # pre-607e729 master row
  new = "2026-10-01T12:00:00.000001+00:00"    # current utc_now_iso(), 1µs later
  # chronologically new > old : True
  # lexicographically new > old: False
  _merge(a, b)["website"] -> 'https://o.example'   # the 1µs-older value won
  ```

  Impact is sub-second, so this is cosmetic on its own. It matters for sequencing the fix: it
  proves that "switch everything to `Z`" is *not* a sufficient remedy on its own, because `'Z'`
  outranks `'.'`.
- **Fix:** fold this into the S2-S2 parse-to-`datetime` fix; do not attempt to patch it by
  string-format normalisation alone.

---

### S3 — `_pick("scraped_at")` at `dedup.py:177` compares raw values, so a non-`str` `scraped_at` raises `TypeError` and kills the run

- **Where:** `dedup.py:176-177`, contrasted with `dedup.py:155-156`.
- **Breaks:** the two code paths disagree. `_recency` stringifies defensively via
  `str(record.get("scraped_at") or "")`; line 177 calls `max(av, bv)` on the raw values. If a
  `scraped_at` is anything other than `str`, line 177 raises before `rank()` — and therefore
  before `_recency` — ever runs.
- **Trigger** (confirmed):

  ```python
  _merge({"scraped_at": datetime(2026,1,1)}, {"scraped_at": "2026-01-02T00:00:00+00:00"})
  # TypeError: '>' not supported between instances of 'str' and 'datetime.datetime'
  ```

  Meanwhile `_recency({"scraped_at": datetime(2026,1,1)})` returns `'2026-01-01 00:00:00'` without
  raising — i.e. the defensive helper exists but is not used on the path that needs it. This
  crashes the whole run (`main.py:180`, uncaught) for every record, not one row.
- **Fix:** `return max(_recency(a), _recency(b))` at `dedup.py:177`, so both paths share one
  normalisation.

---

### S3 — Half of `_VOLATILE` is unreachable, and its comment misdescribes the function

- **Where:** `dedup.py:114`, `dedup.py:168-171`, `dedup.py:113-114` (comment).
- **Breaks (unreachable):** `website_live` is in `_VOLATILE`, but all three scrapers hard-code
  `website_live=None` — `osm.py:109`, `wikidata.py:117`, `google_places.py:292`. `_is_missing`
  at `dedup.py:168-171` short-circuits before `rank()` is ever built, so `_recency` can never
  observe it. Confirmed:

  ```python
  _merge({"phone":"1","website":"https://x.example","website_live":None,"source":"google_places"},
         {"phone":"1","website":"https://x.example","website_live":None,"source":"osm"})
  ["website_live"]  # -> None
  ```

  `website_live` is only ever populated by `enricher.py:250`, which runs *after* dedup
  (`main.py:180` then `main.py:187`). Putting it in `_VOLATILE` is dead configuration.
- **Breaks (misleading comment):** *"Fields whose value changes over time, so the fresher
  observation wins"* is false for 3 of the 4 entries. Measured trust per field (executed):

  | field | google_places | osm | wikidata | cross-source trust differs |
  |---|---|---|---|---|
  | `rating` | 3 | 1 | 1 | yes → trust decides |
  | `review_count` | 3 | 1 | 1 | yes → trust decides |
  | `website` | 2 | 3 | 2 | yes → trust decides |
  | `website_live` | 1 | 1 | 1 | no → unreachable |

  So `_recency` is decisive **only** for two records from the *same* source, and only across
  *different runs* (within a run the timestamp is constant — see S2). That is a far narrower
  contract than the comment advertises, and it is the entire reason S1 exists.
- **Fix:** delete `"website_live"` from `_VOLATILE`, or move the dedup/enrich boundary so
  liveness is mergeable; and correct the comment to say recency is a *last* tiebreak.

---

## Not a bug, but worth knowing

- **`_recency` never raises on absent data, and that part is right.** `{}`,
  `{"scraped_at": None}`, `{"scraped_at": ""}` all return `""`, which sorts below every real
  timestamp — so a record with no observation time always loses ties. Verified for all three.
  `{"scraped_at": " "}` returns `" "` (truthy), which is harmless but is an asymmetry with
  `_pick`, whose `_is_missing(" ")` is `True`.
- **The rank tuple is type-homogeneous — the one thing it got right.** Elements are
  `(int, int, str, str)` (`dedup.py:180-185`), so the `rank(a, av) >= rank(b, bv)` comparison at
  `dedup.py:187` cannot raise `TypeError`, unlike the raw `max(av, bv)` at `dedup.py:177`.
- **The merge is order-independent for these fields.** `_merge(low, high)["review_count"] ==
  _merge(high, low)["review_count"]` returned `True`, so S2 is a wrong answer, not a race.
- **Homogeneous `+00:00` output is lexically ordered correctly.** I verified the invariant that
  *would* make `_recency` correct today: with all inputs in `utc_now_iso()`'s format, lexical
  order equals chronological order in every pair tested, including the whole-second /
  fractional boundary (`.` = 46 > `+` = 43, so the truncated form correctly sorts first). The
  function is not wrong for the format its producers currently emit — it is wrong for every
  other format, and unreachable for the reason in S1.
- **`str(datetime)` uses a space separator.** If `scraped_at` ever holds a `datetime`,
  `_recency` yields `'2026-01-01 00:00:00'`; `' '` (32) loses to `'T'` (84), so such a record
  loses every tie on the same date.
- **`scraped_at` is run-start time, not observation time.** `google_places.py:173` stamps all
  68 queries with one value taken before a multi-minute crawl at
  `_QUERY_BUDGET_SECONDS`. `_recency`'s effective resolution is coarser than its microsecond
  strings suggest, which reinforces S2.
- **Not in scope but adjacent:** `normalize_phone("(01) 70123456")` returns `+961170123456`
  (`dedup.py:53-55` tests `digits.startswith("0" + cc)` = `"0961"`, which a local trunk-prefixed
  `01…` never matches). Two spellings of the same Lebanese number therefore miss each other in
  `dedup()` and produce two rows — observed while building the S1 repro, which is why my first
  attempt at it returned 2 rows instead of 1.

## Recommended order of work

1. **S1** — swap the `rank()` tuple so `_recency` precedes `_trust_for` for `_VOLATILE` fields
   (`dedup.py:179-185`). One-line reorder; immediately stops the dead-website pitch.
2. **S2 (tuple)** — add a numeric key before `str(value)` (`dedup.py:184`) so `review_count`
   and `rating` compare numerically.
3. **S2 (parse)** — introduce a `_parse_ts()` helper, use it in `_recency` (`dedup.py:155-156`)
   **and** in the `scraped_at` branch (`dedup.py:177`), returning `datetime.min` for unparseable
   input. This closes S2-parse, S3-mixed-format and S3-TypeError together.
4. **S3** — remove `"website_live"` from `_VOLATILE` and fix the comment at `dedup.py:113-114`.
5. Add a regression test beside `test_volatile_field_prefers_the_fresher_observation`
   (`tests/test_lead_signal.py:303`) that asserts the S1 case: an older higher-trust `website`
   must lose to a newer lower-trust one. The existing test at
   `tests/test_lead_signal.py:303-309` cannot catch S1 because it uses the *same* source
   (`"google_places"` on both sides), which is the one configuration where `_recency` works.

## Reproduction

All findings were confirmed by importing `dedup` and executing it, not by reading alone. Probe
scripts were written outside the repository (in `$TMPDIR/opencode/probe/`) and imported the
module read-only via `sys.path.insert`; no source file was modified and no network call was
made.