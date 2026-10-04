# 308 — `_validity`: the declared `int` contract vs. the values it actually receives

**Target:** `dedup.py:120` · **Lens:** contract · **Companion type:** `BusinessRecord` (`scrapers/base.py:5-28`)

All claims below were verified by importing `dedup`/`enricher` in a throwaway interpreter and
calling the pure functions directly. No network, no scrapers run, no source modified.

---

## Verdict

`_validity` declares itself a per-field plausibility scorer (`dedup.py:121`: *"0-3 bonus for
whether a value is merely present or actually plausible"*), but in practice it is a 5-arm `if` on
a field **name** plus a catch-all `return 1` (`dedup.py:140`), so **18 of the 23 `BusinessRecord`
keys receive a constant**. Worse, the score it returns is placed at `dedup.py:182` — the *second*
element of a lexicographic tuple whose first element is `_trust_for` — so whenever two sources
disagree on trust, `_validity`'s answer is computed, returned, and then **thrown away**. A junk
OSM `website` tag beats a well-formed Google one, and a junk OSM `email` beats a valid Wikidata
one, with no error and no log line. The function also certifies types it never normalises, so a
value it scores `3` can still crash the consumer it was supposed to protect.

**The single most important thing:** reorder the `rank()` tuple so `_validity` precedes
`_trust_for`, and make "invalid" dominate rather than merely score lower.

---

## Declared contract vs. actual contract

```python
# dedup.py:120
def _validity(field: str, value) -> int:
```

| Aspect | Declared | Actual |
|---|---|---|
| `field` | `str` — any string | exact-match dispatch on 5 literals: `email`, `website`, `lat`, `lon`, `rating`; everything else → `1` |
| `value` | unannotated → implicit `Any` under `mypy --strict` (`pyproject.toml:64-68`) | whatever `dict.get` yields: `str`, `float`, `int`, `bool`, `nan`, `inf` |
| return | `int` | observed return set is exactly `{-100, 0, 1, 3}` — **`-100` is outside the docstring's "0-3"** |
| reads | — | exactly one thing: the `value` argument. It never touches a record, a key, or the TypedDict |
| writes | — | nothing. It is pure |

**Direct answer to "every field it reads or writes that is not in the TypedDict":** `_validity`
reads no fields and writes no fields. It has no record access at all. The leak is in its *domain*
— `field` is an open `str` (see S2-b), not a `Literal` — and in its `value` argument, which
`rank()` sources from `a.get(field)` (`dedup.py:167`).

---

## Findings

### S1 — `_validity`'s verdict is discarded whenever two sources have different trust

- **Where:** `dedup.py:179-187` (the tuple) with `dedup.py:97-110` (`_SOURCE_TRUST`) and
  `dedup.py:148-152` (`_trust_for`).
- **Breaks:** `rank()` is lexicographic on `(trust, validity, recency, str(value))`. Trust is
  element **0**, validity element **1**. `_SOURCE_TRUST` deliberately gives OSM a *higher* trust
  than Google on `website` (`3` vs `2`, `dedup.py:104` vs `:100`) and than Wikidata on `email`
  (`3` vs `2`, `dedup.py:103` vs `:107`). So for those two fields a **validity of 0 loses to a
  validity of 3**, and the junk value is what lands in the CSV:
  - `website` → `enricher.check_websites` (`enricher.py:231`) fetches the junk URL, gets
    `UNKNOWN`, sets `website_live=None`; `lead_score` (`enricher.py:300-304`) then awards **0** for
    both website terms, and the row lands in `with_websites.csv` (`main.py:199`) carrying a URL
    that is not a website. A business with a perfectly good Google site is scored as having none.
  - `email` → `completeness_score` (`enricher.py:106`) counts it, `lead_score`
    (`enricher.py:288-289`) awards **+20**, and it is exported as the lead's email address. It is
    not an address; you cannot mail it. Sales-ready filtering (`main.py:137-145`) passes on it.
- **Trigger** (verified, no exotic precondition — a junk OSM URL tag is routine):
  ```python
  a = {"website": "http://example",         "source": "osm",           "scraped_at": "2026-02-15T00:00:00Z"}
  b = {"website": "https://starbakery.com", "source": "google_places", "scraped_at": "2026-02-15T00:00:00Z"}
  _pick("website", a, b)   # -> 'http://example'
  #   a  trust=3 validity=0
  #   b  trust=2 validity=3      <-- better on every other axis, still loses
  ```
  ```python
  a = {"email": "not-an-email",      "source": "osm",      "scraped_at": "2026-02-15T00:00:00Z"}
  b = {"email": "info@starbakery.com","source": "wikidata", "scraped_at": "2026-02-15T00:00:00Z"}
  _pick("email", a, b)      # -> 'not-an-email'
  ```
  For contrast, the fields where OSM has *no* trust entry behave correctly, which is what proves
  the ordering is the cause and not the scorer:
  ```python
  _pick("rating", {"rating": 99, "source": "osm"}, {"rating": 4.4, "source": "google_places"})
  # -> 4.4        (trust 1 vs 3, validity 0 vs 3 — same conclusion, no override)
  ```
- **Fix:** make validity dominate — either swap the tuple to `(validity, trust, recency, str(v))`,
  or better, split the concept: have `_validity` return a hard `0/1` "is this usable at all" and
  gate on that *before* trust, e.g.
  ```python
  return av if (_validity(field, av) > 0, rank_a) >= (_validity(field, bv) > 0, rank_b) else bv
  ```
  Any fix that leaves validity below trust in the tuple leaves this bug intact.

---

### S1 — `_validity` certifies a value's *type* by coercion but never converts it, and the consumer compares the raw value

- **Where:** `dedup.py:128-133` (`lat`/`lon`) and `dedup.py:134-139` (`rating`); consumers at
  `enricher.py:92` and `enricher.py:313`.
- **Breaks:** `float(value)` is used purely as an *acceptance test* — the return value is an `int`
  score, so the original `value` is what propagates. `_validity("lat", "33.893")` returns **3**
  (maximum plausibility). `enricher.infer_region` then does `lat_min <= lat <= lat_max` on that
  same string:
  ```python
  infer_region(None, "33.893", 35.50, country="LB")
  # TypeError: '<=' not supported between instances of 'str' and 'float'
  ```
  `enrich()` is called unguarded at `main.py:187`, so this kills the entire run after scraping.
  `_is_missing` (`dedup.py:79-90`) cannot catch it — a non-blank string is not "missing". Likewise
  `_validity("rating", True)` returns **3**, and `True < 4.0` is a legal-but-meaningless comparison
  at `enricher.py:313`.
- **Trigger — the reachable variant is `nan`:** `main.load_master` casts `lat`/`lon`/`rating`
  through `float()` at `main.py:90-95`, and `float("nan")`, `float("NaN")`, `float("Infinity")`
  all succeed. Separately, `osm.py:68-69` assigns raw Overpass JSON straight into `lat`/`lon` with
  **no cast**, and Python's `json.loads` (which backs `result.json()`) accepts the non-standard
  `NaN` / `Infinity` literals by default. Either path produces a real `float('nan')`:
  ```
  _validity("lat", float("nan"))        -> 3          # maximally valid
  33.845 <= nan <= 33.920               -> False      # every NaN comparison is False
  infer_region(None, float("nan"), 35.5) -> None       # silent, no exception
  ```
  The record keeps a NaN coordinate, `_validity` gave it full marks, and `main.py:229-234` files
  it under `region: Unknown` with no error anywhere.
  **Honest caveat:** I could not confirm that Overpass emits `NaN`, and a *string* `lat` does not
  reach `enricher` today because `main.py:93` normalises it. The defect is that the validity gate
  and the consumer disagree about the type contract; the NaN path needs no string at all.
- **Fix:** have `_validity` return the coerced value or `None` rather than an `int`, and reject
  non-finite floats explicitly (`math.isfinite`), so `dedup` normalises once and every consumer
  downstream is guaranteed a `float | None`.

---

### S2 — `_validity` is a constant `1` for 18 of the 23 `BusinessRecord` keys, and the two lists have already drifted

- **Where:** `dedup.py:120-140`; the TypedDict at `scrapers/base.py:5-28`; `_VOLATILE` at
  `dedup.py:114`.
- **Breaks:** only `email`, `website`, `lat`, `lon`, `rating` are checked. The remaining **18**
  — including `name`, `phone`, `address`, `category`, `review_count`, `website_live`,
  `lead_score`, `completeness_score`, `industry_priority`, `recommended_service` — return `1`
  unconditionally. `field` is typed `str`, not a `Literal`, so **no type checker can catch the
  drift**; `mypy --strict` is on (`pyproject.toml:64-68`) and still sees nothing because
  `value` is implicit `Any` and `field` is just `str`.
  Concrete consequences:
  - **`name` accepts placeholders.** `_validity("name", "N/A")` → `1`. `osm.py:63` only filters
    falsy names, so `name="N/A"` survives, and because `name` + `city` *is* the fallback dedup key
    (`dedup.py:212-214`), every `name="N/A"` record in the same city merges into one lead:
    ```python
    dedup([{"name":"N/A","phone":None,"address":"Main St, Beirut",  ...},
           {"name":"N/A","phone":None,"address":"Rue Verdun, Beirut",...},
           {"name":"N/A","phone":None,"address":"Hamra, Beirut",   ...}])
    # 3 distinct businesses -> 1 record
    ```
    That is silent data loss in the product CSV.
  - **`phone` is never validated** even though `normalize_phone` sits 87 lines above it and proves
    phones are untrusted (`dedup.py:33-63`). Since `phone` is *not* in `_VOLATILE`, both sides
    score `(trust, 1, "", str(value))` and the tiebreak at `dedup.py:184` is a **lexicographic
    string comparison of two phone numbers** — a meaningless ordering:
    ```python
    _pick("phone", {"phone":"+961 1 234 567","source":"google_places","scraped_at":"2026-01-01"},
                  {"phone":"9611234567",    "source":"osm",          "scraped_at":"2026-01-02"})
    # -> '9611234567'   (because '9' > '+')
    _pick("phone", {"phone":"+96112345678","source":"google_places", ...},
                  {"phone":"12345",         "source":"osm",          ...})
    # -> '12345'        (a 5-digit extension wins; main.py:194 later normalises it to '')
    ```
  - **`review_count` is in `_VOLATILE` with no validator**, so its choice is recency alone:
    `_pick` over `{"review_count": 5, source osm, 2026-01-01}` vs
    `{"review_count": "banana", source google, 2026-02-01}` returns `'banana'`.
- **Fix:** declare `_CHECKED_FIELDS: Final[Literal[...]]`, type `field` as
  `Literal["email","website","lat","lon","rating","phone","name"]`, and add a unit test that
  asserts `set(_CHECKED_FIELDS) >= set(_VOLATILE)` so the two can never drift apart again.

---

### S2 — `field` is an open `str`, so non-`BusinessRecord` keys reach the dispatch and lose all validation silently

- **Where:** `dedup.py:192` (`for key in set(a) | set(b)`) → `dedup.py:193` → `dedup.py:182`;
  the loader at `main.py:86-104`; the writer at `main.py:125`.
- **Breaks:** `_merge` iterates the **union of both records' keys**, so the domain of `field` is
  whatever the inputs contain — not `BusinessRecord`. `main.load_master` preserves **every CSV
  header key verbatim** (`main.py:86-104` only casts values; it never filters keys). So any column
  in `data/all_businesses.csv` that is not in `main.FIELDS` (`main.py:33-40`) — a stale schema, a
  hand-added column, a future `place_id`/`osm_id` — is carried into `_merge`, dispatched through
  `_validity`, falls through to the catch-all `1`, is merged, and is then **silently dropped** by
  `write_csv`'s `extrasaction="ignore"` (`main.py:125`).
  ```python
  # a master CSV with extra columns
  name,phone,country,website,place_id,google_rating
  Alpha,0501234567,LB,https://a.com,p1,4.5

  _validity("place_id",       "p1")   -> 1
  _validity("google_rating",  "4.5")  -> 1   # a rating under a different key gets ZERO validation
  # merged record carries place_id='p1', then write_csv drops it with no warning
  ```
  No `KeyError`, no log line, no non-zero exit. Rename `rating` to `google_rating` in the master
  and every rating silently stops being range-checked.
- **Fix:** filter to `set(FIELDS)` at the loader boundary (`main.py:104`) and log any dropped
  column name; and in `_merge`, iterate `set(a) & set(b) | (set(a) ^ set(b))` only over known keys.
  Cheapest durable version: have `dedup.dedup` accept `list[BusinessRecord]` and let mypy catch the
  rest.

---

### S2 — the `-100` sentinel at `dedup.py:122-123` is unreachable, **and would be ineffective if reached**

- **Where:** `dedup.py:122-123`, against the short-circuit at `dedup.py:167-171` and the tuple at
  `dedup.py:179-185`.
- **Breaks, in two independent ways:**
  1. **Unreachable.** `_pick` returns at `dedup.py:168` and `dedup.py:170` whenever either side is
     missing, so by the time `rank()` is constructed both `av` and `bv` are non-missing and
     `_validity`'s `_is_missing` branch can never fire.
     ```python
     _pick("website", {"website": "",  "source":"osm","scraped_at":"2026-01-01"},
                     {"website":"https://cafe.com","source":"google_places","scraped_at":"2026-01-02"})
     # -> 'https://cafe.com'   (short-circuit at dedup.py:168; _validity never called)
     ```
  2. **Ineffective.** Even with the short-circuit removed, `-100` sits at tuple position 1, so a
     trust difference at position 0 dominates it. Verified by stubbing `_is_missing` to always
     return `False`:
     ```
     a = {"website": None,          "source": "osm",           "scraped_at": "2026-01-01"}
     b = {"website": "https://x.com","source": "google_places", "scraped_at": "2026-01-02"}
     # rank(a) = (3, -100, '', 'None')  >  rank(b) = (2, 3, '', 'https://x.com')
     # _pick -> None      <-- the MISSING value wins
     ```
  This is a **latent trap**: `_validity` looks like the owner of the missing-value case, so the
  natural refactor is to delete the now-redundant `_is_missing` short-circuit from `_pick`. Doing
  that turns dead code into a silent "prefer the empty value" bug with no test to catch it.
- **Fix:** delete the `-100` branch (it cannot fire) and let `_is_missing` in `_pick` remain the
  single owner of absence. If a sentinel is genuinely wanted, it must be compared at tuple
  position 0.

---

## Not a bug, but worth knowing

- **`_validity` is unreachable for `source` and `scraped_at`.** `_pick` special-cases both at
  `dedup.py:173-174` and `dedup.py:176-177`, before `rank()` is defined at `dedup.py:179`. Fine,
  but it means the docstring's field-agnostic framing is misleading.
- **`_EMAIL_OK` / `_URL_OK` are prefix matches, and the two modules disagree on what a valid
  email is.** `dedup.py:116-117` uses `.match` with no `fullmatch`, and `_URL_OK` has no `$`:
  `_validity("website", "http://a.b extra")` → **3** and `"http://a.b\njavascript:x"` → **3**.
  `_EMAIL_OK` does anchor with `$`, so `"a@b.com\nc@d.com"` → 0. More importantly `_validity`
  never applies `enricher._EMAIL_BLACKLIST` (`enricher.py:138-141`):
  `info@wixpress.com`, `info@sentry.io`, `info@example.com` all score **3** here while the
  enricher treats them as worthless. Two definitions of "valid email", one pipeline.
- **`osm.py:95-96` and `wikidata.py:98` pre-normalise**, so `_validity`'s email/website checks
  mostly re-validate what the scrapers already guaranteed. The one place it *should* catch raw
  user-typed junk is precisely where trust overrides it — which is S1.
- **`lead_score` / `completeness_score` merges are meaningless but currently harmless.** Both have
  no trust entry and no validator, so they tie and fall to `str(value)`:
  `_pick("lead_score", {"lead_score": 9, "scraped_at":"2026-02-15"}, {"lead_score": 47,
  "scraped_at":"2026-01-15"})` → **9**, because `"9" > "47"` lexicographically. Both are
  recomputed downstream (`enricher.py:349-350`, `main.py:197`), so no wrong value reaches the CSV.
  If anyone ever removes that recomputation, this becomes a live S1.
- **`BusinessRecord` has 23 keys, not 22.** `base.py:6-28` lists 23 and `main.FIELDS`
  (`main.py:33-40`) lists the same 23, in a different order. The sets match exactly today. The
  BRIEF's "22 keys" / "22 columns" is off by one. `pyproject.toml:37` also hints the wheel may be
  missing `main.py`, `dedup.py`, `enricher.py`, `httpclient.py`.
- **`base.py:25` and `base.py:28` declare `lead_score: int` / `completeness_score: int` as
  non-optional**, but every scraper seeds them with `0` and `main.py:96-101` sets them to `None`
  whenever `int(float(cell))` fails. The TypedDict over-promises; `_is_missing` is what actually
  defends the code, and it accepts `None` for these.
- **The master-record ratchet is a `_trust_for` problem, not a `_validity` one**, but it is the
  largest correctness risk in this module and worth flagging: once a value lands in
  `data/all_businesses.csv`, `main.py:179` puts the master *last* in `combined`, and
  `_sources_of` (`dedup.py:143-145`) reads the merged `"a|b"` string, so the accumulated record
  keeps the highest trust forever. Verified: a Google website URL is adopted in run 1; OSM later
  finds the business's real domain and the stale master URL wins again in run 2 *and* run 3. A
  `scraped_at` freshness override for single-source-confirmed fields would break the ratchet.

---

## Recommended order of work

1. **Reorder `rank()` so `_validity` precedes `_trust_for`, and split "valid" from "plausible"**
   (`dedup.py:179-185`). This is the only change that fixes the shipped wrong contact data. Add
   the two regression cases from S1 as tests *first* — they fail today.
2. **Stop the coercion-without-conversion gap** (`dedup.py:128-133`): reject non-finite floats,
   and normalise `lat`/`lon`/`rating` once in `dedup` so `enricher.py:92` and `enricher.py:313`
   can assume `float | None`.
3. **Give `name` and `phone` validators** (`dedup.py:120-140`): reject placeholder names
   (`"N/A"`, `"null"`, `"-"`, `"unknown"`), which also fixes the `name="N/A"` dedup collapse, and
   reuse `normalize_phone` for `phone` so the `str()` tiebreak stops deciding phone merges.
4. **Type `field` as a `Literal` and pin the checked set with a test** asserting
   `set(_checked) >= set(_VOLATILE)` (`dedup.py:114`), so the drift in S2 cannot reopen.
5. **Filter to `set(FIELDS)` in `load_master` and log dropped columns** (`main.py:104`), so a
   renamed column fails loudly instead of silently degrading `_validity` to a no-op.
6. **Delete the unreachable `-100` branch** (`dedup.py:122-123`) so nobody deletes `_pick`'s
   `_is_missing` short-circuit and inherits the position-0 sentinel trap.
7. **Share one email/URL validator between `dedup.py:116-117` and `enricher.py:130-141`**, and
   anchor it with `fullmatch`, so the blacklist is applied in both places.