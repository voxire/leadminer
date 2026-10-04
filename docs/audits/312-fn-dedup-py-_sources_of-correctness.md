# 312 — `_sources_of` (dedup.py:143) — correctness

## Verdict

`_sources_of` is four lines and is *locally* correct — it is deterministic, idempotent,
and handles the three literal source strings the scrapers actually emit. The bug is not
in it, it is in **what the two callers do with the answer**. Both callers read the
returned set as an **assertion set** ("which sources assert this value?") when it is
actually a **contributor set** ("which sources ever returned a hit for this business?").
`_pick` unions it (`dedup.py:174`) and `_trust_for` takes the max over it
(`dedup.py:151`), so a field value inherits the highest trust of **every source that
ever touched the business, including sources that contributed nothing to that field**.

Two consequences, both executed and reproduced below: `dedup()` becomes
**arrival-order dependent** — the exact property the surrounding docstring
(`dedup.py:161`) and the test `test_merge_is_order_independent`
(`tests/test_lead_signal.py:292`) claim to have bought — and trust becomes a
**monotonic ratchet** across runs, because `all_businesses.csv` is cumulative
(`main.py:150,179`) and carries the union forward forever. Seven of the 22 output
columns are exposed.

## How I verified

Imported the module in place (`python3.14`, repo root) and called `_sources_of`,
`_trust_for`, `_merge` and `dedup` directly. No network, no scrapers, no files
written outside this report. Every "exact wrong output" below is copied from a real
run, not reasoned about.

Trust table cross-check used throughout (computed, not assumed):

```
trust(address)  {'google_places': 3, 'osm': 2, 'wikidata': 2}
trust(lat/lon)  {'google_places': 3, 'osm': 1, 'wikidata': 1}
trust(website)  {'google_places': 2, 'osm': 3, 'wikidata': 2}
trust(email)    {'google_places': 1, 'osm': 3, 'wikidata': 2}
trust(category) {'google_places': 3, 'osm': 2, 'wikidata': 1}
```

---

## Findings

### S1 — Provenance laundering: a value wears the trust of a source that never asserted it

- **Where:** `dedup.py:143-145` (`_sources_of`), consumed by `dedup.py:148-152`
  (`_trust_for`, `best = max(...)` over the union) and `dedup.py:173-174`
  (`_pick`, `"|".join(sorted(_sources_of(a) | _sources_of(b)))`), ranked at
  `dedup.py:179-187`.
- **Invariant it violates:** `_trust_for(field, rec)` is only meaningful if every
  source in `_sources_of(rec)` actually asserts a value for `field`. The union at
  `dedup.py:174` destroys that precondition permanently.
- **Breaks (a) — `dedup()` is order-dependent again.** `_pick`'s docstring
  (`dedup.py:160-161`) says "deterministically". It is not, when three sources
  contribute. Exact input, same three records, only order differs:

  ```python
  P, T = "+96170123456", "2026-09-01T00:00:00+00:00"
  G = {"name":"Cafe Habib","phone":P,"scraped_at":T,"source":"google_places",
       "category":"restaurant","lat":None,"lon":None}
  O = {"name":"Cafe Habib","phone":P,"scraped_at":T,"source":"osm",
       "category":"cafe","lat":33.89000,"lon":35.50123}
  W = {"name":"Cafe Habib","phone":P,"scraped_at":T,"source":"wikidata",
       "category":"coffee shop","lat":33.88900,"lon":35.49500}
  ```

  Exact wrong output — all six permutations:

  ```
  [W,O,G]  lat=33.89     source='google_places|osm|wikidata'
  [W,G,O]  lat=33.889    source='google_places|osm|wikidata'   <-- DIFFERENT
  [O,W,G]  lat=33.89     source='google_places|osm|wikidata'
  [G,W,O]  lat=33.889    source='google_places|osm|wikidata'   <-- DIFFERENT
  [G,O,W]  lat=33.89     source='google_places|osm|wikidata'
  [O,G,W]  lat=33.89     source='google_places|osm|wikidata'
  ```

  `33.89` is OSM's rooftop coordinate (`scrapers/osm.py:68-69`). `33.889` is
  Wikidata's P625 **municipality centroid** (`scrapers/wikidata.py:101`), ~120 m
  away — it will fail the sales team's "is this on the right street" check and it
  is silently worse. Mechanism, confirmed step by step:

  ```
  trust(lat)  google_places=3  osm=1  wikidata=1
  _merge(W, G) sources = ['google_places','wikidata']   trust(lat) = 3
  ```

  Google contributed `lat=None`. `_pick` short-circuits on `_is_missing` at
  `dedup.py:169-171` and keeps Wikidata's 33.889 — but the record's `source` is now
  the union, so `rank(a, 33.889)` is `(3, 3, "", "33.889")` while `rank(b, 33.89)`
  is `(1, 3, "", "33.89")`. `_pick` is lexicographic on the tuple
  (`dedup.py:179-185`), trust is element 0, and `33.89`'s *only* chance — winning
  the `str(value)` tiebreak at 33.89 > 33.889 — is unreachable. Google vetoed a
  field it said nothing about.

  Reachability of `lat=None` from Google is documented, not hypothetical:
  `google_places.py:272-274` does `location = place.get("location") or {}` then
  `lat = location.get("latitude")` with no guard, on the `places:searchText`
  endpoint (`google_places.py:30`). Google's own discovery doc states for
  `includePureServiceAreaBusinesses`: *"Places will not return fields including
  `location`, `plus_code`, and other location related fields for these
  businesses"*, and Text Search supports returning them. A pure-service-area
  business (a mobile cleaning service, a plumber with no premises) comes back with
  `lat=None` and is written straight through.

  `main.py:160-166` feeds `dedup()` in `as_completed` order, i.e. the order the
  three network-bound scrapers happened to finish. That order varies run to run, so
  this is a real per-run coin flip, not a theoretical one.

- **Breaks (b) — the ratchet, which needs no optional field at all.** Once *any*
  Google record for a business has been merged, the row written to
  `all_businesses.csv` carries `google_places` in its `source`. Every future run
  re-reads it (`main.py:150`), and every field on it now scores trust 3 forever.
  Exact input — a master row left over from January plus today's fresh OSM record:

  ```python
  master   = {"name":"Cafe Habib","phone":"+96170123456","scraped_at":"2026-01-01T00:00:00+00:00",
              "source":"google_places|osm","lat":33.889,"category":"restaurant"}
  fresh_osm= {"name":"Cafe Habib","phone":"+96170123456","scraped_at":"2026-10-01T00:00:00+00:00",
              "source":"osm","lat":33.89000,"category":"cafe"}
  ```

  Exact wrong output:

  ```
  trust(lat): master_row = 3   fresh_osm = 1
  _merge(master_row, fresh_osm)["lat"] = 33.889      # today's correct value discarded
  _merge(fresh_osm, master_row)["lat"] = 33.889      # symmetric: both orders discard it
  counterfactual, master source='osm': 33.89         # fresh value wins
  ```

  The master row cannot be corrected by OSM or by Wikidata, ever, and the
  row it came from never expires. Fields exposed: the seven Google is trusted 3 on
  at `dedup.py:99-100` — `rating`, `review_count`, `lat`, `lon`, `name`, `address`,
  `category` — because every other source scores ≤ 2 on them.
- **Fix:** rank on the provenance of the record that *supplied the value*, not on
  the union — record `_PROV[field] = winning_record["source"]` in `_merge` and have
  `rank()` at `dedup.py:181` call `_trust_for(field, {"source": _PROV.get(field)})`;
  keep the union only in the public `source` column. (Not literally one line: the
  merged record does not currently carry per-field provenance at all. The one-line
  stopgap, if per-field provenance is too big a change today, is to delete the
  union at `dedup.py:174` and set `source` to the *winner's* source — which costs
  the corroboration signal at `enricher.py:316` but removes the laundering
  entirely.)

---

### S2 — A single space in `source` silently zeroes the entire trust model

- **Where:** `dedup.py:145` — `return {s for s in raw.split("|") if s}`. No
  `.strip()`, no `.lower()`, no membership check against `_SOURCE_TRUST`
  (`dedup.py:97-110`).
- **Breaks:** `"google_places | osm"` parses to `{' osm', 'google_places '}`,
  neither of which is a key in `_SOURCE_TRUST`, so `_trust_for` falls to
  `_DEFAULT_TRUST = 1` (`dedup.py:111,151`) for **every** field. That is
  indistinguishable from having no provenance at all — and from being
  `wikidata`, whose own `category` trust is also 1 (`dedup.py:108`). Every
  contested field then falls through to `_validity` and finally to the
  lexicographic `str(value)` tiebreak at `dedup.py:184`.
- **Trigger (exact input → exact output):**

  ```
  _sources_of({'source': 'google_places|osm'})   = ['google_places', 'osm']
  _sources_of({'source': 'google_places | osm'}) = [' osm', 'google_places ']
  _sources_of({'source': 'OSM'})                 = ['OSM']

  trust(website) for 'google_places|osm' = 3
  trust(website) for 'google_places | osm' = 1     <-- model disabled
  trust(rating)  for 'google_places | osm' = 1     <-- was 3
  ```

  Downstream, two space-dirty rows both claiming `google_places`:

  ```
  _merge({"source":"google_places | osm","website":"https://z.example", ...},
         {"source":"google_places | osm","website":"https://a.example", ...})
    -> website = 'https://z.example'      # z beats a purely on str()
  ```

  `a.example` should win on any reading; `z.example` wins because trust collapsed to
  a tie on element 0, `_validity` tied too, and the lexicographic `str(value)`
  tiebreak at `dedup.py:184` then decided the field.
- **Reachability:** `main.py:86-104` (`load_master`) strips nothing from any column.
  `main.py:118-120` documents that the master CSV is written `utf-8-sig` *"so Excel
  and Google Sheets detect UTF-8 and render Arabic business names correctly"* — the
  file is explicitly designed to be opened, sorted, de-duplicated and re-saved by a
  human in a spreadsheet. One stray space around a pipe is the single most likely
  hand-edit. There is also no canonicalisation pass: `_pick`'s union at
  `dedup.py:174` re-emits whatever it read, dirty or not, so once a master row is
  dirty it stays dirty forever.
- **Fix:** one line — `dedup.py:145` becomes
  `{s.strip().lower() for s in raw.split("|") if s.strip()}`.

---

### S2 — A source missing from `_SOURCE_TRUST` is silently trusted at 1 while still being recorded as provenance

- **Where:** `dedup.py:151` — `_SOURCE_TRUST.get(src, {}).get(field,
  _DEFAULT_TRUST)`; `dedup.py:111` — `_DEFAULT_TRUST = 1`.
- **Breaks:** the table has exactly three keys (`dedup.py:98, 102, 106`). A fourth
  scraper's records get trust 1 on every field, so they lose **every** contested
  merge, forever — but `_pick` still unions their name into `source`
  (`dedup.py:174`), so the output CSV shows them as a contributing source. The
  failure mode is "wired up, contributing provenance, incapable of winning
  anything", and it produces no error, no warning, no log line.
- **Trigger (exact input → exact output):**

  ```python
  reg = {"name":"Registre","phone":"+96170123456","source":"official_registry",
         "website":"https://registre.example","email":"a@b.com"}
  osm = {"name":"Registre","phone":"+96170123456","source":"osm",
         "website":"https://old.example","email":"x@y.com"}
  ```

  ```
  _sources_of(reg) = ['official_registry']
  trust(website): official_registry = 1   osm = 3
  _merge(reg, osm)["website"] = 'https://old.example'   # registry's real site lost
  _merge(reg, osm)["source"]  = 'official_registry|osm' # provenance kept, trust ignored
  ```

  This is not hypothetical for this repo: audits 077 (SerpAPI/Google Maps), 079
  (official registries) and 062 (bulk OSM alternatives) each propose a fourth
  scraper, and `cli.py:56-61` already exposes `--source` as a per-scraper selector.
- **Fix:** one line at `dedup.py:144` — warn (or raise) for any token in
  `_sources_of(record)` that is not a key of `_SOURCE_TRUST`.

---

### S3 — `str()` coerces a non-`str` `source` into a garbage token and writes the repr into the master CSV

- **Where:** `dedup.py:144` — `raw = str(record.get("source") or "")`.
- **Breaks:** `BusinessRecord.source` is declared `str` (`scrapers/base.py:26`) but
  nothing enforces it, and a `TypedDict` is not validated at runtime. A list or set
  becomes a single unusable token, then `_pick` writes that repr straight back out.
- **Trigger (exact input → exact output):**

  ```
  _sources_of({'source': ['osm','wikidata']}) = ["['osm', 'wikidata']"]
  _sources_of({'source': ('osm',)})          = ["('osm',)"]
  _sources_of({'source': 42})                = ['42']
  _sources_of({'source': {'osm': 1}})        = ["{'osm': 1}"]

  _merge({'source': ['osm','wikidata']}, {'source':'google_places'})["source"]
    = "['osm', 'wikidata']|google_places"
  ```

  That last string is then written verbatim into `all_businesses.csv`
  (`main.py:125,209`) and read back forever after.
- **Fix:** one line — `dedup.py:144` raises `TypeError` unless
  `isinstance(record.get("source"), (str, type(None)))`.

---

### S3 — The unbounded `source` union breaks `cli.py stats`

- **Where:** `dedup.py:174` (the union) consumed at `cli.py:119-121`:
  `collections.Counter(r.get("source") for r in rows).most_common(8)`.
- **Breaks:** the operator-facing "by source" breakdown groups by the *composite
  string*, so every corroborated business is its own group and the top-8 slice is
  dominated by near-unique combinations instead of telling the operator which
  scraper is working.
- **Trigger (exact input → exact output):**

  ```
  after folding 5 scrapers into one record:
    'apple_maps|google_places|osm|serpapi|wikidata'
  Counter(['apple_maps|google_places|osm|serpapi|wikidata',
           'osm|google_places|wikidata',
           'osm']).most_common()
    = [(...serpapi|wikidata, 1), ('osm|google_places|wikidata', 1), ('osm', 1)]
  ```

  Nothing above 1 in any bucket.
- **Fix:** tabulate `sorted(_sources_of(r))` per record instead of `r.get("source")`
  at `cli.py:120` — reuse the same helper rather than re-parsing the pipe string a
  third time.

---

## Not a bug, but worth knowing

- **`_sources_of` itself is deterministic and idempotent.** It returns a `set`, so
  iteration order cannot leak: `_pick` guards it with `sorted` (`dedup.py:174`) and
  `_trust_for` with `max` (`dedup.py:151`). Verified over 1000 constructions of
  `{"source":"osm|wikidata"}` — identical set every time — and `_merge` on an
  already-joined value is a fixed point. There is no nondeterminism *inside* the
  function; all of the nondeterminism is in the union feeding `_trust_for`.
- **The three real source strings are handled exactly right.** `"osm"`
  (`scrapers/osm.py:119`), `"wikidata"` (`scrapers/wikidata.py:127`),
  `"google_places"` (`scrapers/google_places.py:302`) contain no whitespace, no
  pipe, and are lowercase. For those inputs `_sources_of` is exact.
- **`enricher.py:315-317` (`if "|" in str(record.get("source") or ""): score += 5`)
  is an intended consumer of the union**, and it is immune to the whitespace bug
  (the pipe survives). It does mean the +5 "multi-source confirmation" bonus is a
  ratchet too — it is granted once, when a second scraper first matches, and never
  revoked even if only one scraper still finds the business years later. Worth a
  deliberate decision, not a fix.
- **`_merge` unions keys (`dedup.py:192`), so `source` survives even when only one
  side has it.** `_is_missing` at `dedup.py:168-171` returns the present side, so a
  sparse record contributes no provenance of its own — correct, and not a source of
  lost data.
- **`record.get` on a non-dict would raise `AttributeError` at `dedup.py:144`.**
  Not reachable: `main.py:172-180` only ever passes dicts from the scrapers and from
  `csv.DictReader`. Not a finding.

## Recommended order of work

1. **S1(b), the ratchet** — cheapest and highest value. It is 100% internally
   reachable (no Google optionality required), it is permanent once written, and it
   silently freezes stale `name` / `address` / `lat` / `lon` / `category` values in
   the cumulative master forever. Do S1(a) with it; the two are one fix.
2. **S2 whitespace** — a one-line change at `dedup.py:145` that removes an entire
   silent-disable class, plus a canonicalisation pass over `load_master`
   (`main.py:86-104`) so already-dirty master rows self-heal.
3. **S2 unknown source** — one warning line; do it *before* any fourth scraper
   lands, not after.
4. **S3s** — bundle into the same edit; they are cheap and both write permanent
   garbage into the master CSV.