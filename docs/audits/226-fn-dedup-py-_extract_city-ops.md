# 200 — `_extract_city` (dedup.py:72) under a live weekly cron

## Verdict

`_extract_city` is a four-line pure string function, so the standard operational
review questions (prints, swallows, retries, interrupts) are almost entirely
inapplicable — and I say so rather than inventing answers. The real defect is
one line: `parts[-1]` at `dedup.py:76`. Because both scrapers that feed it put a
non-city in the **last** comma position (Google puts the country, OSM puts
`addr:district`), the function returns the country for Google records and a
neighbourhood for OSM records. That value is half of the dedup key at
`dedup.py:214`, and `main.py:209` rewrites the cumulative master through that key
every week — so this is **unattended, permanent, and bidirectional: it deletes
real businesses from the all-time master, and it duplicates them.** The cron went
live at `scrape.yml:9` (`0 3 * * 1`), so this is no longer hypothetical.

---

## Lens questions answered literally

The brief asks what the function prints, swallows, and does under failure. Being
precise here matters, because the honest answers are mostly "not applicable" and
that is *not* the same as "safe".

| Question | Answer | Evidence |
|---|---|---|
| What does it print? | **Nothing.** No `print`, no `logging`, no `log` call anywhere in `dedup.py` (verified by grep: the only `try`/`except` in the file is `_validity` at `dedup.py:129-137`). | `dedup.py:72-76` |
| What does it swallow? | **Nothing directly** — there is no `try`/`except`. But it propagates uncaught, and its *result* is swallowed one level up: `city` is a local at `dedup.py:213` and is never stored on the record. | `dedup.py:213-214` |
| Network drops halfway | **No direct effect** — the function does no I/O. Indirect effect is severe: see "Partial scrape changes the key space" below. | — |
| Dependency returns garbage | Non-empty garbage → a valid-looking empty key. Non-`str` garbage → `AttributeError` that kills the whole run. Both detailed in S2-1 / S3-1. | `dedup.py:75` |
| Called with an empty list | **N/A to this function** — it takes one `str \| None`, not a list. `dedup([])` returns `[]` (loop body never runs), so the caller is safe. | verified |
| Interrupted | **N/A** — pure function, no checkpoints, nothing to interrupt. But a *crash* here is maximally expensive (see S3-1). | — |

The one structural fact that makes this worse than a "4-line function" is that
it executes at `main.py:180` — **after** all three scrapers have run
(`main.py:160-168`) and **before** anything is written. There is no checkpoint
between scrape and dedup, so every one of these failures is discovered only after
the paid, six-hour, rate-limited part is already over.

---

## Findings

### S1 — Google addresses make the dedup key *the country*, collapsing every same-named business in the country into one row

- **Where:** `dedup.py:76` (`return parts[-1] if parts else ""`), consumed at `dedup.py:214` (`key = (normalize_name(name), city)`). Source of the bad input: `scrapers/google_places.py:286` sets `address=place.get("formattedAddress")`.
- **Breaks:** Google's `formattedAddress` is `street, city, region, country`. `parts[-1]` is therefore the **country**, not the city. Every Google record in Lebanon shares the city key `"lebanon"`; every KSA record shares `"saudi arabia"`. Two genuinely different branches of the same chain collide, `_merge` picks one survivor, and `main.py:209` writes that survivor back to `all_businesses.csv` — the "cumulative master, all time".
- **Trigger (verified, real `dedup()` call):**

  ```
  input                          _extract_city    key
  Hamra Street, Beirut, Lebanon   'lebanon'
  Rue Monnot, Beirut, Lebanon     'lebanon'
  ABC, Achrafieh, Beirut, Lebanon 'lebanon'
  Starbucks, Adliyah, Riyadh, Saudi Arabia  'saudi arabia'

  dedup() -> 2 rows (was 4). Three Beirut branches became one.
  ```

  The surviving address is `Rue Monnot, Beirut, Lebanon` — chosen by the
  `str(value)` tiebreak at `dedup.py:184` (both records have
  `source=google_places`, so `_trust_for` ties at 3 and `_validity` ties at 1).
  **The address a salesperson reads is decided by alphabetical string
  comparison.** A caller told "we have your Hamra branch" can be handed the
  Monnot address with no indication it is a merge artifact.
- **Why this is data loss, not just a bad key:** the master is not append-only.
  `main.py:150` loads it, `main.py:179-180` feeds it through `dedup()`, and
  `main.py:209` overwrites it. Once two rows merge, the other branch's address
  and phone are gone from the all-time file, and the next run cannot recover
  them because they no longer exist to re-merge.
- **The publish guard does not catch it:** `scrape.yml:78-83` fails the run only
  if `ROWS < 100`. Over-merge makes the row count go *down*, which sails past
  that guard, and `scrape.yml:89` copies the lossy master over the canonical
  `gdrive:leads/` location.
- **Partial mitigation:** `scrape.yml:88` archives each run to
  `gdrive:leads/data/$TIMESTAMP/`, so the pre-merge master is recoverable — but
  only by someone who already knows to go looking.
- **Fix:** stop guessing positionally. Return all tokens and match against a
  known-city set, or drop `address` from the key entirely and key on
  `(normalize_name(name), country)` plus `lat/lon` rounded to ~4dp
  (~11 m), which is already present and consistent across all three scrapers
  (`osm.py:104-105`, `google_places.py:287-288`, `wikidata.py:101`).

---

### S1 — OSM puts `addr:district` last, so the key is a neighbourhood, and the same business accumulates a permanent extra master row every time its tags change

- **Where:** `dedup.py:76`; input assembled at `scrapers/osm.py:80-87`.
- **Breaks:** the OSM address is built in fixed order
  `housenumber, street, suburb, city, district` (`osm.py:81-85`). The **last**
  element is `addr:district` — a neighbourhood, never the city. So `_extract_city`
  is wrong for OSM in the *opposite* direction from Google: it returns an
  over-specific value, which causes **under-merge**.
- **Trigger (verified, three monthly runs of one real business):**

  ```
  run 1  'Rue Monnot, Beirut'                              -> key 'beirut'    master: 1 row
  run 2  '12, Rue Monnot, Beirut'                         -> key 'beirut'    master: 1 row
  run 3  '12, Rue Monnot, Sursock, Beirut, Achrafieh'     -> key 'achrafieh' master: 2 rows
  ```

  When OSM contributors add a `addr:district` tag to an existing node, the key
  changes and the business forks into a second master row. Both rows persist
  forever, because `_merge` only ever runs *after* a key collision
  (`dedup.py:215-216`) and there is no later reconciliation pass. Worst case
  degenerates further: with only `addr:street` tagged, the key is
  `'rue monnot'` — a street name used as a city.
- **Trigger 2 — hard-split:** `scrapers/wikidata.py:99` reads `P6375`, a free-text
  street-address literal. A Wikidata value of `"Beirut"` yields key `'beirut'`
  while the same business from Google yields `'lebanon'`. Cross-source
  under-merge is therefore **structural, not incidental** — the three scrapers
  emit three incompatible address grammars and `parts[-1]` cannot satisfy all
  three.
- **Fix:** see S1-1. Keying on coordinates and country is the only construction
  that is simultaneously correct for all three grammars.

---

### S2 — `if parts else ""` is dead code, and non-empty garbage silently becomes the empty key

- **Where:** `dedup.py:75-76`.
- **Breaks:** after a successful `str.split(",")`, `parts` is **never** empty —
  even `",".split(",")` is `['', '']`, which is truthy. So the `else ""` branch is
  unreachable, and it is unreachable *by construction*: line 73 already returned
  for the only inputs that could produce an empty list. The author clearly
  believed an empty case existed; no test was written for it (there is no test
  referencing `city` anywhere in `tests/`, which contains only
  `test_lead_signal.py`).
- **The consequence is that garbage passes as a valid key.** `_extract_city`
  returns `""` — indistinguishable from "no address at all" — for:
  - `"Hamra St, Beirut,"` (trailing comma) → `''`
  - `","` or `", "` → `''`
  - `"   "` (whitespace only; truthy, so line 73 does not catch it) → `['']`
    → `''`

  All of these are non-empty strings, so they pass `if not address`, and all
  collapse to the key `(name, "")`.
- **Trigger:** Wikidata `P6375` is free text and can carry trailing punctuation.
  A record whose address is `"Rue Monnot, Achrafieh,"` and another whose address
  is `None` both get key `("",)`-city, and merge regardless of actually being in
  different places.
- **Fix:** delete the dead guard, and normalise the result —
  `if not result: return ""` after the strip, so a whitespace/comma-only address
  is treated as genuinely absent rather than as a real city name.

---

### S2 — The name key carries no country, and `dedup()` runs *before* `resolve_country()`

- **Where:** `dedup.py:214` builds `(normalize_name(name), city)` with no
  country component. `main.py:180` calls `dedup()`, but
  `main.py:184` (`r["country"] = resolve_country(r)`) runs **afterwards**.
- **Breaks:** at dedup time the country is whatever the scraper stamped on the
  record (`google_places.py:285`, `osm.py:103`, `wikidata.py:111`) or `None`
  from a master CSV with a blank cell. A country that *would* have separated two
  records is not yet trustworthy at the moment the key is computed — and is not
  in the key at all.
- **Trigger (verified):** two address-less records both named `"Clinic"`,
  `country=LB` and `country=SA`, no phone → `dedup()` returns **1 row**. The
  surviving row carries `source="google_places"` with both sources unioned at
  `dedup.py:174` and a single `country` value — a Saudi clinic is now filed as
  Lebanese.
- **Honesty about likelihood:** this is a *latent* hazard, not the main event.
  In practice Google is the only two-country source and Google always populates
  `formattedAddress`, so cross-country collisions need two address-less
  same-name businesses. The more realistic variant is **intra-country**:
  `"Clinic"` in Beirut and `"Clinic"` in Tripoli, both OSM nodes without `phone`
  and without any address tags, merge into one row. That is common enough in OSM.
- **Fix:** move `main.py:183-184` above `main.py:180`, and include the resolved
  country in the name key.

---

### S3 — No type coercion at `dedup.py:75`: a non-`str` address raises `AttributeError` at `main.py:180` and wastes an entire paid run

- **Where:** `dedup.py:72` annotates `address: str \| None` but line 75 calls
  `address.split(",")` unconditionally once the value is truthy.
- **Breaks (verified):** `123`, `1.5`, `True`, `["a"]`, `{"a": 1}` all raise
  `AttributeError: '<type>' object has no attribute 'split'`. There is no `try`
  in `dedup`, so it propagates to `main()`, which has no top-level handler. The
  run dies **at the dedup step**, after all three scrapers finished and after
  the full Google Places SKU spend — with nothing written, because
  `main.py:209-213` is the first write and it is never reached.
- **Reachability — stated plainly:** I could not construct a path that feeds a
  non-`str` today. `load_master` (`main.py:86-104`) only casts
  `_FLOAT_FIELDS`/`_INT_FIELDS`; `address` comes back from `csv.DictReader` as
  `str` or `None`, and all three scrapers produce `str | None`. So this is a
  latent hazard, not a live bug, and I am not claiming otherwise. It is worth
  fixing anyway because the failure mode is the worst available: a four-line
  function taking down six hours of rate-limited, metered scraping.
- **Fix:** `address = str(address)` at the top of the function, matching the
  defensive `str(phone)` already used at `dedup.py:42`.

---

### S3 — The extracted city is never persisted, so nothing in the output can reveal that it is wrong

- **Where:** `dedup.py:213` assigns to a local; `dedup.py:214` consumes it; the
  local is never written to the record.
- **Breaks:** `city` exists only inside the key. None of the 22 columns in
  `main.py:33-40` records it. An operator auditing `sales_ready.csv` cannot
  determine which key rule fired, cannot see that `"lebanon"` was treated as a
  city, and cannot tell a merged row from a genuinely single-site business
  without re-deriving the key by hand.
- **This is the property that sets the notice times in the table below.** A bug
  whose output is written to the file gets caught by reading the file. This one
  has no output.
- **Fix:** during the fix, emit the resolved city into the record (or at minimum
  `print` the top-20 key components and their collision counts at
  `main.py:180`), so the next person can see the key space.

---

## Every silent failure mode, ranked by time-to-notice

"Notice" = a human looking at logs, the Actions UI, or the CSVs. Not "a customer
complains".

| # | Silent failure | Why it is silent | Time to notice | Severity |
|---|---|---|---|---|
| 1 | **Same-named businesses duplicated in the master** (OSM district key, S1-2) | Row count goes *up*, which reads as healthy growth. Exit code 0. Passes the `ROWS >= 100` guard at `scrape.yml:80`. No error, no warning, no field records the city. | **Months.** Only visible if someone spots the same business twice in one CSV, or if a caller says "you already sent me this". | S1 |
| 2 | **Distinct businesses silently merged; the other branch erased from all-time history** (Google country key, S1-1) | Row count goes *down*, which looks like "fewer new businesses this week" — the expected steady state. `sales_ready.csv` just gets shorter. Nothing prints the merge. | **Weeks-to-months.** The first hard signal is a salesperson reading the wrong street address on a call. `main.py:181`'s count is printed but nobody diffs it against last week, and `scrape.yml` has no regression check on the delta. | S1 |
| 3 | **Cross-country / cross-city merge via the `""` city key** (S2-2) | Identical mechanics to #2 but needs an address-less record, so it hits a smaller population and hides inside it. | **Months.** Surfaces as "this Saudi lead has a Lebanon phone number". | S2 |
| 4 | **A partial scrape silently changes dedup outcomes** | See below. Highest blast radius per unit probability, because it is *caused* by a failure that is logged loudly elsewhere — so attention is already spent on the outage. | **Days**, but only if someone correlates the two. | S2 |
| 5 | **Garbage address treated as a real, empty city** (S2-1) | Indistinguishable from a legitimately absent address. | **Never**, individually. Only visible as an aggregate if key collisions are ever counted — which they are not. | S2 |
| 6 | **Whichever address wins a merge is picked alphabetically** (`dedup.py:184`) | A deterministic tiebreak reads as intentional. No log line records that a merge occurred. | **Weeks-to-months**, on a call. | S2 |
| 7 | **Non-`str` address aborts the run after 6 hours** (S3-2) | *Not* silent — it dies loudly with a traceback. Listed for completeness: it fails the "notice" test in the opposite direction, costing a full metered run plus the Actions 300-minute budget (`scrape.yml:25`). | Seconds. | S3 |

### Partial scrape changes the key space — worth calling out separately

Nothing here observes the network, but the *population* the function runs over is
network-dependent. `main.py:167-168` catches a scraper exception, prints it to
stderr, and continues; `osm.py:47-56` and `wikidata.py:79-85` likewise print
"Continuing without … data" and return an empty generator. So a Google 429 storm
or an Overpass timeout yields a *smaller, differently-shaped* input to
`dedup()`. Fewer Google records means fewer `"lebanon"` keys, which changes which
OSM records collide with which, which changes the master that
`main.py:209` writes and `scrape.yml:89` publishes.

**A partial scrape is therefore not merely "fewer rows this week" — it is a
different key space applied to history.** Combined with #1 and #2, the same
underlying business can be duplicated in one week's run and merged in the next.
Neither run is wrong in isolation; neither prints anything.

---

## Not a bug, but worth knowing

- `dedup([])` returns `[]` correctly — the loop at `dedup.py:201` never executes.
  The caller is safe on an empty master.
- `main.py:209-213` writes through `write_csv`, which is genuinely atomic
  (`main.py:108-134`: temp file, `flush`, `os.fsync`, `os.replace`, cleanup in
  `except BaseException`). `_extract_city` cannot corrupt a CSV by crashing —
  it can only corrupt one by returning a wrong value. Worth saying, because it
  means every finding above is a *content* bug, never a *torn file* bug.
- The 22 columns carry `region` (`main.py:34`), which `enricher.infer_region`
  (`enricher.py:79-88`) already derives correctly from the **whole** address by
  regex against a 50-entry Lebanon keyword table (`enricher.py:10-50`) and from
  coordinate bounding boxes (`enricher.py:58-77`). **The correct city/region
  resolution already exists in this codebase** — it is just applied at
  `main.py:187`, i.e. *after* dedup. `enricher.py:85-88` searching the entire
  address for any known locality is exactly the logic `parts[-1]` should be
  replacing, and it is already written, already tested-adjacent, and already
  handles Arabic (`enricher.py:12`, `"بيروت"`).
- `dedup.py:114` already treats `website` as `_VOLATILE`. `address` is **not** in
  that set, so a merged record's address is decided by trust/validity and then
  the `str()` tiebreak — there is no "freshest observation wins" rule protecting
  a recently corrected address. Relevant to #6.
- Sibling hazard, outside this lens but adjacent at `dedup.py:222-230`: phone-keyed
  records are emitted first and name-keyed records are only checked for phone
  collision, never cross-matched by name. A record with a phone and a record
  without one for the same business can never merge regardless of how
  `_extract_city` behaves. Fixing `_extract_city` will not fix that.

---

## Recommended order of work

1. **Stop using `address` as a key component.** Key the no-phone branch on
   `(normalize_name(name), country, round(lat, 3), round(lon, 3))`, and skip
   records that have neither coordinates nor a country. This one change retires
   S1-1, S1-2 and S2-2 together, because it is the only construction correct
   for all three address grammars at once. Note the deliberate trade: it will
   *reduce* apparent merging versus today for records that today collide on
   `"lebanon"`, so re-baseline the expected row count before shipping.
2. **Move `main.py:183-184` above `main.py:180`** so a trustworthy `country`
   exists at key-computation time. Two lines, and step 1 depends on it.
3. **Add a regression assertion on the row-count delta**, not just the absolute
   floor at `scrape.yml:80`. Fail the run if the master shrinks by more than a
   few percent week-over-week — that single check catches both S1 modes and
   would have caught this on the first scheduled run.
4. **Make the key space observable.** `print` the top-20 key components and their
   collision counts after `main.py:180`, and drop the resolved city onto the
   record. Without this, every future identity bug in this file is invisible by
   construction.
5. **Delete the dead `else ""` at `dedup.py:76`**, add `str()` coercion at
   `dedup.py:75`, and add the four tests that do not exist today: trailing comma,
   whitespace-only, `None`, and a Google `formattedAddress` asserting the key is
   a city rather than a country.