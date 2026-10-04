# 200 — `_is_missing` (dedup.py:79) correctness audit

**Target:** `dedup.py:79-90` `_is_missing(value) -> bool`
**Lens:** correctness — every input that yields a wrong or surprising output.
**Method:** every claim below was executed against the real module (Python 3.14.8, offline
harness with stubbed `requests`/`urllib3`). Reproduction scripts:
`/private/var/folders/.../T/opencode/ismissing_lens/{p2,p3,p4,p5}.py`.

---

## Verdict

`_is_missing` is a **presence** test, not a **usability** test, and it is the *only* gate
`_pick` consults before short-circuiting past the entire `_validity` scoring apparatus
(`dedup.py:168-171`). Any non-empty string that a source emits as a placeholder therefore
becomes a permanent, load-bearing value: it survives the merge, it suppresses the real
contact address that `enricher.check_websites` scraped off the business's own homepage
(`enricher.py:252-259`), it flips `recommended_service` from *"build me a website"* to
*"overhaul my website"*, and it removes the row from `without_websites.csv` — the product the
brief calls the new-site pitch list. That is S1: silently wrong revenue-facing output.

The whitespace/empty-string half of the function is **correct and important** — do not
"simplify" it (see *Not a bug* below for the exact invariant and why `not value` would break
`website_live=False`).

## Call graph (complete)

```
_is_missing            dedup.py:79
├── _validity          dedup.py:122   → -100 branch (UNREACHABLE, see S3)
└── _pick              dedup.py:168   → return bv     ← short-circuits _validity entirely
                       dedup.py:170   → return av     ← short-circuits _validity entirely
    └── _merge         dedup.py:193
        └── dedup      dedup.py:208 (phone index), dedup.py:216 (name index)
```

`_pick` is reached only via `_merge`, which is reached only from `dedup()` at
`dedup.py:208`/`:216`. The other entry points into the index —
`phone_index[key] = dict(record)` (`dedup.py:210`) and
`name_index[key] = dict(record)` (`dedup.py:218`) — **do not call `_merge` at all**, so a
record that never collides with another record is never seen by `_is_missing`. That is why
finding S1 needs a two-part fix, not just a predicate change.

---

## Findings

### S1 — Placeholder strings count as "present", and `_pick`'s early return bypasses `_validity` entirely

- **Where:** `dedup.py:79-90` (the predicate), `dedup.py:168-171` (the bypass),
  fed by `scrapers/osm.py:90-96` which passes raw tag values through unfiltered.
- **Breaks:** OSM (and scraped web pages) use literal placeholder tokens where the schema
  expects a value. `osm.py:90-93` copies them straight into the record. `_is_missing`
  accepts every one of them as data:

  ```
  _is_missing('no')           = False   _is_missing('none')   = False
  _is_missing('n/a')          = False   _is_missing('N/A')    = False
  _is_missing('unknown')      = False   _is_missing('false')  = False
  _is_missing('not provided') = False   _is_missing('-')      = False
  _is_missing('null')         = False   _is_missing('None')   = False
  ```

  Worse, `_pick` returns `av`/`bv` at `dedup.py:169`/`:171` **before** `_validity` is ever
  consulted, so the one field that *does* know these are junk (`_validity('email','none')`
  = 0 vs `_validity('email','a@b.com')` = 3) is never given a vote whenever the other side
  of the merge is empty — which is the normal case on a first run.

- **Trigger (three concrete chains, all executed):**

  **(a) Real email permanently destroyed.** One OSM node with
  `contact:email=none`, `contact:instagram=no`, `website=https://cafe-khalil.example`.
  The site is live and its homepage contains `mailto:hello@cafe-khalil.example` and
  `instagram.com/cafe_khalil` (simulated by stubbing `check_websites`):

  ```
  _merge(osm_junk, google)['email']     = 'none'      # dedup.py:171 short-circuit
  _merge(osm_junk, google)['instagram'] = 'no'
  enricher.py:252  `if not r.get('email')`  ->  'none' is truthy -> guard is False
  enricher.py:254  `if not r.get('instagram')` -> 'no' is truthy -> guard is False
  ```

  End-to-end through `dedup()` + `enrich()` + `write_csv()` + `load_master()`:

  ```
  run 1: email='none' instagram='no'   master csv cell = 'none'
  run 2: email='none' instagram='no'                      # permanent
  control (no junk tags): email='hello@cafe-khalil.example' instagram='cafe_khalil'
  ```

  `_validity` only saves you if the master *already* held the real address
  (`_merge(fresh_junk, master_real)['email'] = 'hello@cafe-khalil.example'`) — which it
  never will, because run 1 is what poisoned it. Email is worth +20 `lead_score`
  (`enricher.py:288-289`) and is a primary channel in `main.has_any_contact`
  (`main.py:142-143`). This is silent, irreversible data loss on the highest-value field.

  **(b) Pitch target vanishes from the pitch list.** `osm.py:95-96` will even
  *manufacture* a URL from a junk value: an OSM tag `website=no` becomes
  `"https://" + "no"` = `"https://no"`, which is truthy.

  ```
  merged website = 'https://no'
  main.py:199  with_websites    contains it: True
  main.py:200  without_websites contains it: False   <-- the new-site pitch list
  ```

  It is also truthy for `pitch_recommender.recommend_service:31` (`has_website =
  bool(record.get("website"))`) and for `enricher.py:231`, so every such row costs one
  doomed HTTP GET to `https://no` and is routed *away* from every "no website" pitch:

  | Record | `recommended_service` |
  |---|---|
  | clinic, honest (no website, no social) | `Lead-gen website + Google Ads launch` |
  | clinic, same business + `website=no` + `contact:instagram=no` | `Lead-gen overhaul (landing pages + Google Ads + WhatsApp capture)` |
  | bakery, honest (no website, no social) | `Full digital launch (brand + website + social setup)` |
  | bakery, same business + same junk | `Discovery call - scope the right service` |

  A business with **no website at all** is pitched a website *overhaul*, and is dropped
  from the CSV whose purpose is to list exactly those businesses.

  **(c) `facebook`/`instagram` pollution.** `main.py:201`
  `with_social = [r for r in records if r.get("facebook") or r.get("instagram")]`
  reports social presence for a record whose only social "value" is `"no"`.

- **Honest scoping.** I did **not** quantify how often OSM emits these tokens in the
  LB/SA extract (taginfo's API was not reachable during the audit), so treat incidence as
  *unknown but nonzero*. Two things make it worth fixing anyway: the code already knows
  about this class of junk — `enricher.py:138-143` hardcodes `_EMAIL_BLACKLIST`
  (`example.com`, `yourdomain.com`, …) and `_IG_BLACKLIST`, and `cli.py:175-188` adds a
  separate ad-hoc "value looks bogus" validator for `rating`; and the failure is
  *permanent*, so a single occurrence costs a lead forever. `enricher.py:138-143` proves the
  team already treats placeholder strings as "no data" — `dedup.py` is the one place in the
  merge path that does not.

- **Fix (predicate, one line):**
  `_NULL_TOKENS = {"no","none","n/a","n.a.","null","nil","-","--","?","0","false","unknown","not provided"}`
  and at `dedup.py:89` → `return (not value.strip()) or value.strip().lower() in _NULL_TOKENS`.
  *(Caveat: `"0"` must only be a null token for string fields; `_is_missing(0)` — the int —
  must stay `False`, see below. Keeping the token set string-typed handles this.)*

- **Fix (bypass, one line):** at `dedup.py:168-171`, gate the short-circuit on validity —
  `if _is_missing(av) and _validity(field, bv) >= 0: return bv` — or, simpler, delete the
  early returns and let `rank()`'s `_validity == -100` term do its job (which requires
  un-breaking S3 first).

- **Fix (unmerged records, one line):** the two `dict(record)` fast paths at
  `dedup.py:210`/`:218` never call `_merge`, so a lone junk record is never checked at all.
  Sanitise once on entry to `dedup()` instead of inside `_merge`.

---

### S2 — Non-finite floats are "present" and outrank every real coordinate; the poison survives the master round-trip

- **Where:** `dedup.py:88-90` (`_is_missing` returns `False` for any non-`str`,
  non-`None`), consumed by `dedup.py:128-133` (`_validity` gives them the **maximum** bonus
  of 3) and by the `str(value)` tiebreak at `dedup.py:184`.
  Reachable from `main.py:90-94` (`load_master` runs a bare `float()` over every
  `lat`/`lon`/`rating` cell).
- **Breaks:** `float()` accepts `"nan"`, `"NaN"`, `"inf"`, `"Infinity"`. `_is_missing(nan)`
  is `False`, and `_validity('lat', nan)` is **3** — the same maximum a genuine coordinate
  gets. The tie then falls to `str(value)` at `dedup.py:184`, and `"nan"`/`"inf"` sort
  *after* every digit-leading coordinate:

  ```
  str(nan)  = 'nan'   > '33.8938' ? True
  str(inf)  = 'inf'   > '33.8938' ? True
  str(0.0)  = '0.0'   > '33.8938' ? False
  ```

  So a non-finite coordinate in `all_businesses.csv` beats a real one, order-independently:

  ```
  master cell 'nan' -> load_master=nan  _is_missing=False  _validity=3
      dedup([fresh, master]).lat = nan          # main.py:179 puts raw first
      dedup([master, fresh]).lat = nan
  master cell 'inf' -> ... dedup([fresh, master]).lat = inf
  ```

  It then round-trips: `write_csv` (`main.py:127`) stringifies it as `nan`, `load_master`
  (`main.py:93`) parses it straight back, forever.

- **Trigger:** one row in `data/all_businesses.csv` with `lat`/`lon` = `nan` (or `inf`).
- **Scope, stated honestly:** it is **not** immortal. A fresh *Google Places* record for the
  same business repairs it, because `_trust_for('lat', google) = 3` beats the OSM/Wikidata
  default of 1 (`dedup.py:97-110`):

  ```
  dedup([fresh_osm , nan_master]).lat = nan         # still poisoned
  dedup([fresh_goog, nan_master]).lat = 33.8938     # repaired
  ```

  Since OSM and Wikidata are exactly the two sources with no `lat` trust entry, a
  Lebanon-only business that Google Places does not return stays broken indefinitely. The
  one existing detector, `cli.cmd_validate`'s `in_box` (`cli.py:159-172`), would flag these
  rows (`in_box(nan, nan) = False`) but `main.py` never calls it.
- **Fix (one line):** at `dedup.py:90`, `return not (isinstance(value, float) and not
  math.isfinite(value))` — or more simply, tighten `dedup.py:128-133` to
  `return 3 if math.isfinite(float(value)) else 0`, which also removes the bogus bonus from
  `inf`.

---

### S3 — `_validity`'s `-100` branch is unreachable dead code that advertises a safety net that does not exist

- **Where:** `dedup.py:122-123`.
- **Breaks:** nothing at runtime. It is a correctness *hazard*, not an output bug, and the
  hazard is specifically about `_is_missing`:

  ```
  call sites of _validity in dedup.py: [120 (def), 182 (inside rank)]
  call sites of rank(  in _pick:        [187]
  ```

  `rank()` is invoked **only** at `dedup.py:187`, which is textually after the two
  `_is_missing` early returns at `dedup.py:168-171`. Those guarantee both `av` and `bv` are
  non-missing, so `_is_missing(value)` at `dedup.py:122` can never be `True` on the path
  into `rank()`. Confirmed by instrumenting `_validity` and running five merges designed to
  hit it (`email=""`, `email=None`, `lat=None`, `lat="   "`, `rating=99`, `website="no"`):

  ```
  -100 hits during those 5 _merge calls: []
  ```

  A reader auditing this file sees `-100` and concludes "`rank()` strongly penalises
  missing values, so it is safe to route values through it." It is not — and the S1 fix
  depends on *un-breaking* this branch first, or the `-100` will silently never fire.
- **Fix:** either delete `dedup.py:122-123`, or keep it and delete `dedup.py:168-171` so the
  ranking actually owns the decision.

---

### S3 — `_is_missing` never looks inside non-`str` values, so empty containers and `bytes` are "present"

- **Where:** `dedup.py:88-90`. `isinstance(value, str)` is the only type inspection, so
  everything else falls through to `return False`.
- **Breaks:** nothing today — no in-repo producer emits a container into a record field
  (`google_places.py:279` `_pick_category` returns a `str`, not the `types` list). This is
  hardening, listed so a future producer cannot silently reintroduce it:

  ```
  _is_missing(b'')          = False   # empty bytes = "present"
  _is_missing(bytearray())  = False
  _is_missing([])           = False   _is_missing({}) = False
  _is_missing(object())     = False
  ```

  I also confirmed `_is_missing` **never raises** on any built-in value I could construct
  (int, float incl. nan/inf, complex, huge int, bytes, bytearray, `list`, `dict`, `object`,
  `str` with embedded NUL). The only exception was a hand-written `str` subclass overriding
  `strip()`, which no producer creates.
- **Fix:** one line — `return not value` for `Sized` non-`str` containers, or add
  `if not isinstance(value, (int, float, bool, str)) and not value: return True`.

---

## Not a bug, but worth knowing

These are **negative results** — I checked each one specifically because it looked like a
bug, and each is fine. Recording them so nobody "fixes" them.

1. **`_is_missing(False)`, `_is_missing(0)`, `_is_missing(0.0)` must all stay `False`.**
   `dedup.py:90` correctly does *not* reduce to `not value`. `website_live` is tri-state
   (`main.py:103`, `enricher.py:250`) and `False` means *server-confirmed dead* — the
   strongest pitch signal in the whole product (`pitch_recommender.py:57-58`,
   `enricher.py:303-304`). `review_count=0` is real data. Verified:

   ```
   merged review_count=250  website_live=True   # neither 0 nor False was treated as missing
   ```

   Collapsing `dedup.py:86-90` to a truthiness check would make `_pick` discard the dead-site
   flag and silently delete the rebuild pitch. **This is the invariant to protect in any
   refactor.**

2. **`lat=0.0` (Wikidata `Point(0 0)`, reachable via `wikidata.py:60`) does *not*
   currently corrupt output.** This was my leading hypothesis and it is **false**. Both
   `_validity` terms tie at 3, `_recency` is skipped (`lat` is not in `_VOLATILE`), so the
   `str()` tiebreak decides — and `"0.0" < "33.8938"` and `"0.0" < "21.3"` (Jeddah), so
   the real coordinate wins in both directions:

   ```
   _parse_point('Point(0 0)') = (0.0, 0.0)
   osm     real first  -> lat=33.8938     wikidata first  -> lat=33.8938
   google  real first  -> lat=33.8938     wikidata first  -> lat=33.8938
   ```

   `_is_missing(0.0)` being `False` is still *conceptually* wrong (0.0 is the universal
   "unknown coordinate" sentinel), but the damage is currently blocked by an accident of
   string ordering. It would become live the moment `dedup.py:187`'s `>=` became `>`, or a
   market with single-digit latitudes was added. Fix alongside S2.

3. **Whitespace handling is correct.** `_is_missing('   ')` is `True` and
   `_merge({'website':'   '}, {'website':None})` → `None`,
   `_merge({'website':'   '}, {'website':'https://a.example'})` → the real URL. The
   original `is None` bug the docstring at `dedup.py:80-85` describes really is fixed.
   NBSP (`'\xa0'`) and `'\u2028'` also resolve to `True` via `str.strip()`; zero-width
   space (`'\u200b'`) does not, since it is not `isspace()`. Not reachable from any current
   producer.

4. **`completeness_score`/`lead_score` merge on a *string* comparison and invert
   numerically — but it is masked.** `dedup.py:184`'s `str(value)` tiebreak makes
   `_merge(cs=9, cs=14)['completeness_score'] == 9` in both orders. This is harmless *only*
   because `enricher.py:349-350` recomputes both fields unconditionally for every record
   before anything reads them. Worth a regression test so a future "incremental"
   refactor (which `docs/audits/035-incremental-scraping.md` proposes) does not inherit it.
   `review_count` is safe from this because it is in `_VOLATILE` and recency decides:
   `_merge(old@2026-01, new@2026-10) = 1400` in both orders.

5. **The single-record fast path has no gate at all.** `dedup.py:210` and `dedup.py:218` do
   `dict(record)` without calling `_merge`, so `_is_missing` is never consulted:

   ```
   dedup([osm_record]) -> 1 record, _merge never called
   email='none' website='https://no' instagram='no' facebook='no'
   _is_missing was never consulted for any of these fields
   main.py:201 with_social = True
   ```

   This is why the S1 fix needs a sanitiser at the `dedup()` entry point, not only inside
   `_is_missing`.

---

## The invariant that *is* guaranteed

Precisely: **for any field `f` and any two records `a`, `b`, if `_is_missing` reports
that at least one side is missing, `_pick` returns the other side's value verbatim, without
consulting trust, validity, recency, or type.** That is guaranteed structurally by
`dedup.py:167-171`, which run before `rank()` is defined. It is what makes the merge
order-independent for the common case (verified: `_merge(a,b) == _merge(b,a)` for website,
email, rating, `source`, whitespace, and `None`), and it is the property the existing
regression tests at `tests/test_lead_signal.py:286-326` lock in.

The guarantee does **not** extend to "a returned value is usable." That is the gap this
report is about.

## Recommended order of work

1. **S1 predicate** — add the `_NULL_TOKENS` set at `dedup.py:79-90`. Two lines, kills the
   `email=none` / `instagram=no` permanent data loss and the `website=no` → `https://no`
   pitch-list exclusion. Ship this first and alone; it is independently safe.
2. **S1 bypass** — make `dedup.py:168-171` consult `_validity` (or delete them, after
   step 3). Otherwise a source that emits a plausible-but-wrong value with a *non-empty*
   sibling still wins unscored.
3. **S1 entry point** — sanitise each record once on entry to `dedup()` (`dedup.py:201`) so
   the `dict(record)` fast paths at `:210`/`:218` are covered too.
4. **S2** — reject non-finite floats, in `_validity`'s lat/lon branch
   (`dedup.py:128-133`) so the `3` bonus is not awarded to `nan`/`inf`; this also covers
   `lat=0.0` from item 2 above.
5. **S3 dead code** — delete `dedup.py:122-123` or the early returns; do not leave a
   `-100` that can never fire next to code that reads as if it can.
6. **Tests** — the existing `TestMergeSurvivorship` (`tests/test_lead_signal.py:267`) has
   nothing for `_is_missing` itself. Add: a `_is_missing` truth table (including the
   `False`/`0`/`0.0` must-be-`False` cases from item 1, so nobody regresses them), a
   placeholder-token merge case, and a `nan`-coordinate merge case asserting the real
   coordinate wins in **both** argument orders.
