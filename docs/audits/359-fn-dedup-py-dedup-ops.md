# 359 — `dedup()` under production ops

**Target:** `dedup.py:197` · **Lens:** ops (six hours, cron, unattended)

## Verdict

`dedup` is the only stage in the pipeline with **no output surface whatsoever** — no
`print`, no `logging` import, no stats return, no progress line — while being the stage that
touches every record from all three sources *plus* the cumulative master. Every silent-failure
mode below is invisible for the same reason: the function's only output is a `list`, and the
caller (`main.py:181`) counts it. Two concrete defects make that blindness load-bearing: a
**provably unreachable guard** at `dedup.py:226-229` lets the same business be emitted as two
rows, and an **unbounded key collapse** at `dedup.py:206-208` turns N distinct businesses into
one. Both survive `leadminer validate` (which is also blind to them), both survive the
workflow's row-count gate, and both would take a human weeks-to-quarters to notice.

Everything here was reproduced by calling `dedup()` directly with synthetic records. No
scraper was run and no network call was made.

---

## Failure modes, ranked by time-to-notice

The lens asked for this ranking explicitly. Time-to-notice, longest first:

| # | Failure | Noticeable in | Human notices |
|---|---|---|---|
| 1 | Same business emitted twice (`dedup.py:226-229`) | nothing | a rep calls the same café twice |
| 2 | N businesses collapse to 1 row (`dedup.py:206-208`) | nothing | a region goes thin in a summary nobody diffs |
| 3 | `dedup` emits zero diagnostics | — | never; it is the *cause* of 1 and 2 |
| 4 | Stale value permanently locked (`dedup.py:148-152`) | nothing | a rep dials a dead number |
| 5 | `scraped_at` laundered (`dedup.py:177`) | nothing | never — it removes the ability to notice 4 |
| 6 | Cross-source name dedup can't fire (`dedup.py:72-76`) | nothing | duplicate outreach again |
| 7 | One bad record kills a 6h run (`main.py:180`) | job goes red | minutes |

---

## Findings

### S1 — `dedup()` has no observability at all, and its return value structurally cannot signal failure

- **Where:** `dedup.py:197-232` (whole function); contrast `enricher.py:235,262,271`,
  `osm.py:51-56`, `wikidata.py:80-85`, `httpclient.py:208-217`.
- **Breaks:** `dedup.py` is the **only module in the pipeline with no `logging` import and no
  `log` object**. Verified by grep: `scrapers/osm.py:7`, `scrapers/wikidata.py:7` and
  `httpclient.py:45` all define one; `dedup.py` does not. There is no `logging.basicConfig`
  anywhere in the repo, so even those `log.debug` calls are discarded.

  So when `dedup` merges 4,000 records into 1, or drops 49 of 50, or emits the same business
  twice, the run log is byte-identical to a perfect run.

  Worse, the one signal the caller derives — `main.py:181`
  `print(f"After merge + dedup: {len(records)} unique businesses")` — **cannot go down in a
  way that means anything.** The master is a subset of `combined` (`main.py:179`), and every
  input record lands in exactly one index and is emitted exactly once, so
  `len(dedup(x)) <= len(x)` always. On a total scraper outage the log prints
  `After merge + dedup: <master row count>` — the *floor* of every healthy run. Any increase
  is read as "good, more leads". The number is arithmetically incapable of signalling failure.
- **Trigger:** all three scrapers fail. `main.py:167-168` catches the exception and prints to
  stderr; `raw` stays empty; `combined = [] + master`; `dedup` returns exactly the master's
  rows; `main.py:181` prints a completely normal count. The workflow's only automated gate is
  `scrape.yml:80` `if [ "$ROWS" -lt 100 ]` — against a real master of hundreds of thousands,
  that threshold sits ~2400× below normal, so it fires on catastrophe and never on decay.
- **Fix:** return stats and log them — `(records, DedupStats)` with
  `n_in / n_phone_keys / n_name_keys / n_merged / n_dropped / n_collisions` where
  `n_collisions` counts keys where ≥2 records merged, and log the biggest 10 collision sizes.

---

### S1 — The guard at `dedup.py:226-229` is provably unreachable; the same business is emitted twice

- **Where:** `dedup.py:220-230`, comment at `dedup.py:220-221`.
- **Breaks:** the comment claims name-keyed records that share a phone with a captured record
  are dropped. **That branch can never execute.** Proof:

  1. A record reaches `name_index` only via the `else` at `dedup.py:211`, which is reached only
     when `key` is falsy — i.e. `normalize_phone(...)` returned `""` (or `raw_phone` was
     falsy).
  2. `captured_phones = set(phone_index.keys())` (`dedup.py:223`) contains only keys that
     passed `if key:` at `dedup.py:206`, so it can never contain `""`.
  3. Therefore at `dedup.py:228`, if `raw` is truthy then `normalize_phone(raw, ...)` is `""`,
     and `"" in captured_phones` is always `False`.

  Verified empirically — every input that lands in `name_index` re-normalizes to `''` or
  `None`:

  ```
  phone=None            -> key=''          index=name  re-norm=None
  phone=''              -> key=''          index=name  re-norm=None
  phone='123'           -> key=''          index=name  re-norm=''
  phone='12345'         -> key=''          index=name  re-norm=''
  phone='+961 3 123 456'-> key='+9613123456' index=phone re-norm='+9613123456'
  ```

  The real-world case is the common one, not an edge case: **OSM records routinely carry no
  `phone` tag** (`osm.py:89` → `None`) while Google Places always supplies
  `nationalPhoneNumber`. So one café is keyed into `phone_index` by Google and into
  `name_index` by OSM — **two different indexes, and `dedup` has no cross-index merge at
  all.** Both rows survive, and neither is enriched with the other's fields.
- **Trigger** (reproduced):

  ```python
  dedup([
    {"name":"Cafe Al Amal","address":"Beirut, Lebanon","country":"LB",
     "phone":"+961 3 123 456","website":"https://amal.com",
     "source":"google_places","scraped_at":"2026-10-01T00:00:00+00:00"},
    {"name":"Café Al-Amal","address":"Acheh, Beirut","country":"LB",
     "phone":None,"email":"info@amal.com","source":"osm",
     "scraped_at":"2026-10-01T00:00:00+00:00"},
  ])
  # -> 2 records out. One business. The Google row has no email;
  #    the OSM row has no phone and no website.
  ```

  Output:

  ```
  {'name':'Cafe Al Amal','phone':'+961 3 123 456','website':'https://amal.com','email':None,...}
  {'name':'Café Al-Amal','phone':None,'website':None,'email':'info@amal.com',...}
  ```

  Both land in `sales_ready.csv` (`main.py:202-205`). The rep calls it twice.
- **Why nobody notices:** `cli.py:187` computes
  `dupes = total - len({(r.get("name"), r.get("phone")) for r in rows})`. These phantoms have
  *different* `(name, phone)` pairs by construction — that is why they are separate rows — so
  `dupes` is `0`. Reproduced on a 1,060-row input yielding 60 phantoms (6%, well above the
  5% gate at `cli.py:188`): `cli.py:187 dupes count: 0 (threshold is 53)`. The only duplicate
  check in the tool has a fingerprint that is mathematically incapable of seeing this bug.
- **Fix:** merge `name_index` records into `phone_index` by name when they collide, instead of
  testing phone equality. Replace the dead test at `dedup.py:228` with a real cross-index
  lookup keyed on `normalize_name(record["name"])`.

---

### S1 — An unbounded number of distinct businesses collapses into one row

- **Where:** `dedup.py:206-208` combined with `_MIN_DIGITS = 7` at `dedup.py:30`.
- **Breaks:** any phone that normalizes to the same string merges every record carrying it,
  with no bound and no check that the records are the same business. `_merge`
  (`dedup.py:190-194`) then picks exactly one `name`, one `address`, one `lat/lon` — so the
  survivor is a **Frankenstein row**: a name from business A at business B's coordinates with
  business C's website. The 49 deleted businesses are not recoverable, because `all_businesses.csv`
  is rewritten wholesale at `main.py:209` and re-uploaded over Drive at `scrape.yml:89`.
- **Trigger (reproduced):** 50 distinct Beirut parking areas, each tagged with the single
  municipality switchboard number:

  ```python
  dedup([{"name":f"Parking Lot {i}","address":f"Zone {i}, Beirut","category":"parking",
          "country":"LB","phone":"+961 1 999 999","source":"osm", ...} for i in range(50)])
  # -> 1 row. Surviving name: 'Parking Lot 9'. 49 businesses deleted.
  ```

  A worse, sharper case: `normalize_phone("0000000", "LB")` returns **`"+961"`**, not `""` —
  all-zero input produces a *valid-looking shared key*. And any 7-digit junk that survives
  `_MIN_DIGITS` gets a plausible national number:

  ```
  '1234567' -> '+9611234567'   ACCEPTED as a real LB number
  '0000000' -> '+961'          ACCEPTED as a real LB number
  '9631234' -> '+9619631234'   ACCEPTED as a real LB number
  '5551234' -> '+9615551234'   ACCEPTED as a real LB number
  ```

  Note the irony: the docstring at `dedup.py:37-38` says *"Junk used to normalize to `+`,
  which made unrelated businesses share one dedup key."* `"+961"` is that same bug with a
  country code glued on, and it is still reachable.
- **Why nobody notices:** `main.py:232-234` prints a by-region breakdown, but the collapsed
  businesses vanish *silently from the region they belong to*, and the run's headline count
  still looks normal because the master keeps the merged row forever.
- **Fix:** treat a phone key that merges ≥3 records with ≥3 distinct normalized names as a
  suspected shared/facility number and fall back to the `(name, city)` key for all but one of
  them. Log every such key with its record count — this is the one event that must never be
  silent.

---

### S2 — A stale high-trust value is permanently un-replaceable, and self-reinforcing

- **Where:** `_trust_for` `dedup.py:148-152` (max over `source`), `source` accumulation at
  `dedup.py:173-174`, comparison at `dedup.py:187`.
- **Breaks:** `main.py:179` builds `combined = raw_filtered + master`, so a fresh scrape is
  always folded *into* the master row, and the master row's `source` set has already
  accumulated every source that ever contributed. `_SOURCE_TRUST` (`dedup.py:97-110`) gives
  OSM 3 for `website`/`email`, Google 2, wikidata 2. Once OSM has ever supplied an email for a
  business, that master row's `_trust_for("email", row)` is **3 forever**, so no fresher,
  more accurate Google value can ever win — not this run, not next year. The poisoned trust
  propagates into every future merge because `dedup.py:174` unions the sources into the
  output.
- **Trigger (reproduced, three consecutive simulated weekly runs):**

  ```
  run1: email='old@x.com'  source='google_places|osm'  scraped_at=2026-10-01
  run2: email='old@x.com'  source='google_places|osm'  scraped_at=2026-10-01
  run3: email='old@x.com'  source='google_places|osm'  scraped_at=2026-10-01
  => new@x.com NEVER wins.
  ```

  Same result for `website`, where it is commercially expensive: a business rebuilds its site,
  Google updates its URL, and `dedup` keeps the dead OSM URL forever. The row then lands in
  `with_websites.csv` and is reported `website_live=False` by `main.py:215-216` — a pitch for
  a website the business already has.
- **Fix:** break trust ties with recency for `_VOLATILE` fields *before* trust, or drop a
  contributing source from `_sources_of` when its value is superseded. At minimum, when
  `_VOLATILE` and the incoming value is fresher **and** plausibly different, prefer the
  incoming value.

---

### S2 — `scraped_at` is laundered, so the master can never be aged

- **Where:** `dedup.py:176-177`, `return max(av, bv)`.
- **Breaks:** `scraped_at` is the *only* freshness signal in the 22 columns, and `max()` over
  contributors means a single fresh contributor relabels the entire row. A row whose 21 other
  fields are nine months stale reports today's date.
- **Trigger (reproduced):** a January OSM record with no website/email/rating merges with an
  October OSM record that only carries `category`. Result: `scraped_at = 2026-10-01`,
  `website=None`, `email=None`, `rating=None` — all still January data.
- **Why it matters for ops specifically:** you cannot build a staleness alert on this column.
  There is no way to ask "which rows have not been re-confirmed in 30 days", so no such alert
  can ever be written. This finding is what makes S2 above undetectable at scale.
- **Fix:** track a separate `last_seen` = `max(scraped_at)` and a per-row
  `field_freshness` map; or keep `scraped_at` as the *minimum* contributor timestamp so the
  column means "oldest data in this row".

---

### S2 — `_extract_city` takes the last comma segment, so cross-source name dedup cannot fire

- **Where:** `dedup.py:72-76`, feeding the name key at `dedup.py:213-214`.
- **Breaks:** `osm.py:80-87` builds addresses as
  `housenumber, street, suburb, city, district` — **district last**. Google returns
  `street, city, country`. So the same city yields a different key per source:
- **Trigger (reproduced):**

  ```
  OSM   '12, Rue Verd, Achkarieh, Beirut, Beirut District' -> city='beirut district'
  Google 'Rue Verd, Beirut, Lebanon'                      -> city='lebanon'
  Google 'Downtown Beirut'                                -> city='downtown beirut'
  ```

  Two records for the same bakery with junk phone numbers (so both are name-keyed) fail to
  merge:

  ```
  input: 2  output: 2   <- expected 1
  ```

- **Fix:** match the city against a known-city list (`Beirut`, `Riyadh`, `Jeddah`, `Dammam`,
  `Tripoli`, …) by substring over all comma segments, instead of taking `parts[-1]`.

---

### S2 — The widest-blast-radius stage is the only one with no exception guard

- **Where:** `main.py:180` vs `main.py:163-168`.
- **Breaks:** `main.py:163-168` wraps every scraper future in `try/except Exception`, giving
  each scraper an independent failure domain. `dedup(combined)` at `main.py:180` has **no
  guard at all** — and it is the one stage that touches every record from all three sources
  *plus* the master. Any single malformed record raises out of `main()` and the entire run is
  lost. Because `write_csv` (`main.py:209-213`) is never reached, nothing uploads, so Drive
  keeps the previous master — the run is a clean, total no-op after up to six hours of work.
- **Trigger (reproduced):**

  ```
  non-str name (float)    -> TypeError: normalize() argument 2 must be str, not float
  non-str address (int)   -> AttributeError: 'int' object has no attribute 'split'
  record is a str         -> AttributeError: 'str' object has no attribute 'get'
  ```

  (`dedup.py:67` and `dedup.py:75` are the unguarded sites.) I could not construct a non-`str`
  `name`/`address` from the three current scrapers — they are all `str`-typed at the point of
  yield — so this is a low-probability/high-blast-radius pair. The likelier trigger is the
  master itself, a months-accumulated file from previously-buggy scraper versions.
- **Fix:** wrap `dedup` per-record in the same style as `main.py:163-168`, counting and
  logging rejected records rather than aborting the run. Raising loudly is the right instinct
  (`dedup` correctly swallows nothing — see below); the error is that it is the *only* stage
  that must not be allowed to take the run down with it.

---

### S3 — `"20"` (Egypt) swallows any 20-prefixed number

- **Where:** `_KNOWN_COUNTRY_CODES` `dedup.py:20`, loop at `dedup.py:58-60`.
- **Breaks:** the loop returns on the first prefix match, and `"20"` is two digits long, so it
  matches before any of the three-digit codes can. `normalize_phone("20001234", "LB")` →
  `"+20001234"`, an Egyptian key for a record that claims Lebanon. Verified.
- **Fix:** order `_KNOWN_COUNTRY_CODES` longest-first, or require `len(known) == 3` when
  matching.

---

### S3 — OSM semicolon-separated multi-phone numbers produce garbage keys

- **Where:** `osm.py:89`, `dedup.py:44`.
- **Breaks:** `re.sub(r"\D", "", raw)` concatenates every number in the tag into one digit
  string. Verified:

  ```
  '+961 1 123 456; +961 1 654 321' -> '+96111234569611654321'
  '06123456;07123456'              -> '+961612345607123456'
  ```

  These are stable-but-meaningless keys, so the record dedups against nothing and is emitted
  as its own row — inflating all five CSVs and worsening the phantom-duplicate problem above.
- **Fix:** split on `;`/`\/`/`,` in `normalize_phone` and take the first segment that
  normalizes.

---

### S3 — `normalize_phone` is called twice per name-indexed record

- **Where:** `dedup.py:205` and `dedup.py:228`.
- **Breaks:** the second call at `dedup.py:228` is pure waste given it can never be true
  (see S1 above). Measured on 200,000 phone-less records: **8.54s with the duplicate call vs
  2.93s for a mixed 200k set.** Honestly: at production scale `dedup` takes **15.0s for
  500,000 records** — it is *not* a performance problem, and I am not claiming one. This is
  hygiene, and it disappears for free when the dead guard at `dedup.py:228` is replaced.

---

## Not a bug, but worth knowing

Things I checked specifically for this lens and found **correct**. Stating them so nobody
re-litigates them:

- **`dedup([])` returns `[]` cleanly**, and `dedup([{}])` returns `[{}]`. The empty-list case
  is handled without a special case. It is caught downstream: `main.py:181` prints `0`, the
  CSVs are written header-only, and `scrape.yml:80` (`ROWS -lt 100`) fails the job before
  upload. That guard is crude — 100 against a real master in the hundreds of thousands — but
  it fails closed.
- **`dedup` is fast.** 0.20s @ 50k, 2.93s @ 200k, **15.04s @ 500k** records, single-threaded.
  No index to optimise. A 20,000-record single-key hot spot costs 1.49s. Do not let anyone
  "optimise" this before fixing the collapse above — the collapse is a correctness bug that
  happens to be cheap.
- **The merge is a fixed point, so the cumulative master does not churn.** Verified over three
  passes: `dedup` is idempotent on already-deduped input (6000 → 4000 → 4000 → 4000, and
  field-for-field identical), and a simulated week where the scrapers return byte-identical
  data reproduces last week's master exactly. `main.py:179` re-ingesting the master every run
  does **not** cause weekly drift. This matters and is easy to assume wrong.
- **The merge does not mutate caller input.** `dedup.py:210` and `dedup.py:218` both
  `dict(record)`, so the mutation at `main.py:183-197` cannot reach back into `raw` or
  `master`.
- **Exceptions propagate instead of being swallowed.** Given a hard crash at `main.py:180`
  means no upload and a preserved Drive master (`scrape.yml:89` never runs), failing loudly
  is the correct trade. The problem is the missing guard (S2 above), not the absence of one.
- **`_merge` is order-dependent in principle but does not bite in the steady state.** Same
  three records in different orders really do give different results —
  `(W,O,G) → 'w1@x.com'`, `(O,G,W) → 'g@x.com'`, `(O,W,G) → 'w1@x.com'` — because
  `_trust_for` is a `max` over a source set that grows as records accumulate. But once the
  master already carries the union of all sources, trust is saturated and the order stops
  mattering (verified: identical output run-over-run). This would become a live bug the moment
  `_SOURCE_TRUST` is re-tuned or a source is dropped from the table.
- **A 6-hour run exceeds the harness limit.** `scrape.yml:25` sets `timeout-minutes: 300`
  (5h). GitHub kills the job at 300 minutes; `dedup` sits at `main.py:180`, upstream of every
  `write_csv`, so a run that overruns loses everything with only the `if: always()` artifact
  (`scrape.yml:91-98`) to diagnose from. `dedup` holds `phone_index` + `name_index` + the
  input list entirely in memory with no checkpoint and no progress line, so there is nothing
  to resume from and nothing to distinguish "hung" from "working" during the quiet stretch.
- **`csv.DictReader` restkey is survivable.** A ragged master row yields
  `{..., None: ['EXTRA','COLS']}` (a `None` *key* holding a `list`). `dedup` handles it —
  `_is_missing` (`dedup.py:88-90`) returns `False` for a list, `_trust_for` defaults to 1 —
  and `main.py:125`'s `extrasaction="ignore"` drops it on write. Noise, not breakage.

---

## Recommended order of work

1. **Return stats from `dedup` and log them** (`dedup.py:197`). Do this first and in the same
   change as #2 and #3 — it is what makes every other fix verifiable, and without it all three
   land silently. Nothing else on this list can be validated without it.
2. **Replace the dead guard at `dedup.py:226-229`** with a real cross-index merge by
   normalized name. Removes the phantom duplicates; `cli.py:187`'s `(name, phone)`
   fingerprint will then start working, so fix that fingerprint at the same time.
3. **Bound key collisions** (`dedup.py:206-208`): detect keys merging ≥3 distinct names, fall
   back to the name key, and log every one. This is the only unbounded data loss here.
4. **Fix `_extract_city` (`dedup.py:72-76`)** against a known-city list. Small, self-contained,
   and multiplies the value of #2.
5. **Make `scraped_at` honest** (`dedup.py:176-177`) — split `scraped_at` from a
   last-reconfirmed timestamp — then add a staleness alert on it. Without this, no alert on
   lead decay is buildable.
6. **Wrap `dedup` per-record** (`main.py:180`) so one malformed master row cannot cost a
   six-hour run.
7. **Unstick the trust accumulation** (`dedup.py:148-152`, `dedup.py:173-174`) and reorder
   `_KNOWN_COUNTRY_CODES` longest-first. Both are correctness-of-value issues that compound
   weekly; they rank below the above only because they degrade rather than destroy.