# 200 — `normalize_name` (`dedup.py:66`) under a production cron: the silent-failure surface

## Verdict

`normalize_name` itself is nearly unbreakable: no I/O, no state, no `try/except`, 3.7 µs per call, idempotent, and it correctly strips Arabic harakat (I expected it to fail there — it does not). **The function is fine. The pipeline around it is not.** The problem is that `dedup.py:214` uses its output as an *invisible* identity key and then discards the key, the collisions, and the losers — so every mistake `normalize_name` makes (or fails to prevent) is written into the cumulative master as if it were ground truth, with no counter, no log, and no artifact that would let anyone notice. Two confirmed, scraper-reachable defects in its key are worth fixing first: an OSM record with no `addr:*` tags collapses into `city=""` and merges with every other same-named address-less business in Lebanon (S1), and two genuinely different businesses with the same name in the same city merge into one row whose `address`, `lat/lon`, and `region` each came from a different physical shop (S1).

---

## Direct answers to the operational questions

**What does it print?**
Nothing. `normalize_name` (`dedup.py:66-69`) has no `print`, no logger, and no counter. `dedup.py` imports only `re` and `unicodedata` (`dedup.py:1-2`) — it has *no logging infrastructure at all*, while every scraper has one (`scrapers/osm.py:7`, `scrapers/wikidata.py:7`, both `log = logging.getLogger(__name__)`). The single component whose failure mode is invisible has the least instrumentation.

The only two numbers a human can ever attribute to this function are:

```
main.py:181   print(f"After merge + dedup: {len(records)} unique businesses")
main.py:220   print(f"  Total unique businesses : {len(records)}")
```

One scalar, no denominator. It moves for at least six unrelated reasons (new leads, a source dying, the whitelist shifting, an Overpass query change, `_extract_city` disagreeing with Google's country suffix, and this function). There is no count of records that fell through to the name key (`dedup.py:211-218`), no count of key collisions, no count of records dropped at `dedup.py:229`.

**What does it swallow?**
Nothing directly — it has no `try/except` and cannot swallow an exception. But `dedup()` swallows its *effect*:

- `dedup.py:214-218` — on a key collision the colliding record is merged and the key is discarded. No record of which key, which spellings, or which record won.
- `dedup.py:226-230` — records can be dropped from the output entirely with no counter.
- `main.py:167-168` — scraper exceptions are caught and the run continues, so a run that lost 2 of 3 sources still publishes.

**What happens when the network drops halfway?**
Nothing at all to this function — it performs no I/O and holds no state, and `dedup()` builds fresh dicts (`dict(record)` at `dedup.py:210` and `dedup.py:218`), so a half-finished dedup leaves nothing behind. The silent failure is upstream and it is *inverted from what you'd expect*: when Google Places dies mid-run, `main.py:167-168` swallows it, the batch gets smaller, fewer new name spellings arrive, and `dedup()` merges **more** records into the master. A broken run therefore produces a *better-looking* duplicate rate than a healthy one. Nobody notices a degraded run by looking at the one number that would show it.

**When a dependency returns garbage?**
The only dependency is the stdlib `unicodedata` module — deterministic, no network. The real garbage vector is the input string from the three scrapers, and it splits three ways:

| Garbage input | Result | Silent? |
|---|---|---|
| Falsy (`None`, `""`, `0`, `[]`, `{}`) | Swallowed by `or ""` at `dedup.py:212` → key `("", city)` | **Yes — and destructive** (S1-2) |
| Truthy non-`str` (`12345`, `3.5`, `["Cafe"]`) | `TypeError` out of `dedup()` → `main()` → run dies | No, loud |
| Non-ASCII junk (ZWNJ, LRM, BOM, NUL) | Folded into a key nobody can read | **Yes** (S2) |

Verified on Python 3.14 / UCD 16.0.0 (production is 3.12 per `scrape.yml:34`; the behaviours below are stable across both):

```
normalize_name(12345)      -> TypeError: normalize() argument 2 must be str, not int
normalize_name(['Cafe'])   -> TypeError: normalize() argument 2 must be str, not list
normalize_name(3.5)        -> TypeError: normalize() argument 2 must be str, not float
```

The `TypeError` propagates out of `dedup()` uncaught and terminates `main()` before any CSV is written. Loud, and `scrape.yml:69` fails the step, so nothing is uploaded. This is the one garbage case that behaves correctly.

**When it is called with an empty list?**
`normalize_name([])` → `TypeError` (same as above). But the question that matters is `dedup([])`, and that is genuinely safe:

```
dedup([]) -> []
```

`main.py` then writes a header-only `all_businesses.csv`, and `scrape.yml:78-83` computes `ROWS = $(wc -l < ...) - 1`, sees `0`, and `exit 1`s. This path is covered. Say so plainly: **no finding here.**

**When it is interrupted?**
Safe, with one caveat. `dedup()` mutates no input record and writes no file, so a SIGINT/SIGTERM mid-dedup destroys nothing; `write_csv` is atomic (`main.py:124-130`: temp file → `flush` → `os.fsync` → `os.replace`, with `BaseException` cleanup at `main.py:131-133`). The step exits non-zero, so `scrape.yml:85` never uploads. The master on Drive keeps its previous complete version. This is correct and should not be changed.

The caveat is `scrape.yml:91-98`: `if: always()` uploads `data/` as the `run-output` artifact. On a failed run that directory still holds the **downloaded master from `scrape.yml:56`**, byte-for-byte. An operator debugging a dead run opens a healthy-looking `all_businesses.csv` with last week's row count and can burn hours concluding the scrapers worked. Also worth knowing: `scrape.yml:25` is `timeout-minutes: 300` — **5 hours**, not 6 — so a six-hour expectation does not exist; the job is SIGKILLed at 300 minutes and takes the fail-closed path described above.

---

## Silent-failure ranking, longest time-to-notice first

| # | Failure | Anchor | Detectability today | Time to notice |
|---|---|---|---|---|
| 1 | The identity key is never persisted anywhere; the output CSV cannot distinguish a correct dedup from a broken one | `dedup.py:214`, `main.py:33-40` | Zero. No `dedup_key` column, no alias list, no collision log | **Never.** Multiplies every other row |
| 2 | Two real businesses merge into one row whose fields each came from a different physical shop | `dedup.py:214` + `dedup.py:159-187` | Zero. The composite reads as a plausible business | **Never** — until a prospect says "that's not our address" |
| 3 | Address-less same-name businesses across Lebanon collapse into one row | `dedup.py:213` + `scrapers/osm.py:87` | Zero | Months, if ever; needs someone to notice a plausible row is missing |
| 4 | Fixing `normalize_name` retroactively re-splits the whole master, and reads as a successful harvest | `dedup.py:66-69`, `main.py:181` | Zero — no key version is stored | Days, and misread as good news |
| 5 | Invisible Unicode (ZWNJ/ZWJ/LRM/RLM/ZWSP/BOM/NUL) is preserved inside keys | `dedup.py:67-69` | Zero | Months |
| 6 | Nameless **and** phoneless records collapse into one per city, permanently | `dedup.py:212-214`, `main.py:87-89` | Zero | Weeks after someone edits the master in Sheets |
| 7 | Under-merge on punctuation / word order / `ة`-`ه` / `ى`-`ي` / tatweel inflates the master | `dedup.py:69` | Zero | Months — looks like a good week's harvest |
| 8 | The published CSV ships the raw, un-normalized name | `dedup.py:214` (key only), `main.py:33-40` | Visible, cosmetic | Minutes — but nobody acts on it |

Rows 1–4 are why this is an S1-adjacent observability problem and not just a normalization problem.

---

## Findings

### S1 — Over-merge: `city=""` collapses distinct businesses nationwide

- **Where:** `dedup.py:212-214`, enabled by `dedup.py:75-76` (`_extract_city` returns `""` for a falsy address) and `scrapers/osm.py:87`.
- **Breaks:** `scrapers/osm.py:87` sets `address = ", ".join(...) or None` — a record with no `addr:*` tags gets `address=None`, so `_extract_city` returns `""` and the dedup key becomes `(name, "")`. Every address-less business in the whole country that shares a name then shares one key. The OSM query has no address requirement, `scrapers/osm.py:63-65` only requires a `name` and a category tag, and generic names survive the whitelist (`"bakery"` is in `ADJACENT_BUSINESSES`, `scrapers/whitelist.py:56`). The survivors are then field-merged by `_pick`, and because every source ties on trust and validity, the `lat`/`lon` winner is decided by the final tiebreak `str(value)` at `dedup.py:184` — i.e. lexicographically. Two businesses are gone and the one that remains is placed at an arbitrary one of their coordinates.
- **Trigger** (verified, running the real `dedup.py`):

  ```python
  dedup([
    {"name": "Bakery", "category": "bakery", "address": None, "lat": 33.89, "lon": 35.50, "source": "osm"},
    {"name": "bakery", "category": "bakery", "address": None, "lat": 34.44, "lon": 35.83, "source": "osm"},
    {"name": "BAKERY", "category": "bakery", "address": None, "lat": 33.51, "lon": 36.28, "source": "osm"},
  ])
  # in=3  out=1
  # [{'name': 'bakery', 'address': None, 'lat': 34.44, 'lon': 35.83}]
  ```

  Beirut, Tripoli and Damascus collapsed into one row pinned to Tripoli. This is not hypothetical: OSM nodes are routinely missing `addr:*`.
- **Why silent:** no collision counter, and the merged row is *counted* in the `main.py:181` total, so the run's headline number goes **down**. A run that loses leads reports fewer unique businesses, which reads as a cleaner dataset.
- **Also:** once merged, the row is written to the master and re-merged next week. The loss is permanent.
- **Fix:** treat an empty city as non-mergeable — fall back to `lat`/`lon` rounded to ~4 decimals as the second key component, and only merge on `(name, city)` when city is non-empty:
  ```python
  city = _extract_city(record.get("address"))
  if not city:
      lat, lon = record.get("lat"), record.get("lon")
      city = f"{lat:.3f},{lon:.3f}" if lat is not None and lon is not None else f"\x00nogeo:{id(record)}"
  ```

### S1 — Over-merge: same name + same city produces a fabricated composite record

- **Where:** `dedup.py:214` + `_pick` at `dedup.py:159-187`.
- **Breaks:** two distinct businesses 800 m apart merge, and each field is won independently. The result is a record whose `address` comes from one shop, whose `lat`/`lon` come from the other, and whose `website`/`email`/`instagram` come from both. `enricher.infer_region` (`enricher.py:85-94`) tries `address` **before** `lat`/`lon`, so `region` is derived from the address while the coordinates say otherwise — the inconsistency is then baked into `lead_score` and `recommended_service` at `main.py:190-197` and shipped in `sales_ready.csv`. A human emails this lead and the reply is "that's not our shop".
- **Trigger** (verified):

  ```python
  a = {"name": "Cafe Arz", "category": "cafe", "address": "Zokak el-Blat, Beirut",
       "lat": 33.8938, "lon": 35.5018, "phone": "", "website": "https://cafearz.com",
       "email": "info@cafearz.com", "source": "osm"}
  b = {"name": "Cafe  Arz", "category": "cafe", "address": "Mar Mikhael, Beirut",
       "lat": 33.8955, "lon": 35.5090, "phone": "", "website": "https://cafearz.co",
       "instagram": "@cafearz", "source": "google_places"}
  dedup([a, b])
  # in=2  out=1
  # name='Cafe  Arz'  address='Mar Mikhael, Beirut'  lat=33.8955 (Zokak el-Blat's lat)
  # website='https://cafearz.com'  email='info@cafearz.com'  instagram='@cafearz'
  ```

  Note the two spellings differ only by a **doubled space**, which `normalize_name` *does* fold (`dedup.py:69`), and by source. `_pick` also picked the *worse-looking* name: `google_places` scores higher on `name` trust (`dedup.py:100`), so `Cafe  Arz` with a double space is what lands in the published CSV.
- **Why silent:** the merged row is individually well-populated and passes `scrape.yml:71-83` (row count only). The internal contradiction is visible only by cross-reading `address` against `lat`/`lon`, which nothing does.
- **Fix:** never merge two records whose coordinates are mutually inconsistent, and add the resulting contradiction as a merge guard: reject a candidate when `haversine(a, b) > 500 m` regardless of name/city match.

### S2 — Zero observability: the key is computed and thrown away

- **Where:** `dedup.py:214` (key built), `dedup.py:215-218` (collision, key discarded), `main.py:33-40` (`FIELDS` has no key or alias column).
- **Breaks:** nothing in the entire product records *why* two records were considered the same business, or how many collisions occurred. `dedup()` returns records, not keys. `source` is unioned (`dedup.py:174`) so you can tell that two sources contributed — but not which spelling lost, and not whether a collision was correct. There is no golden-file test (`tests/` has no coverage for this) and the CI gate at `scrape.yml:74-83` checks only that the file is non-empty and has ≥100 rows. A `normalize_name` regression that halves or doubles the merge rate passes every gate in the repository.
- **Trigger:** change `dedup.py:69` `.lower()` → `.casefold()` and nothing on any dashboard moves. Change it to `return name.strip().lower()` (drop NFKD) and `main.py:220` reports ~200 more rows, which is indistinguishable from a good week.
- **Fix:** emit one `dedup_diagnostics.json` per run — records in, records keyed by phone, records keyed by name, distinct name keys, collision count, mean/max key length, and the 20 most frequent normalized keys with their variant spellings. Print the summary at `main.py:181` and upload it at `scrape.yml:91-98`. Ten lines, and it converts rows 2–8 of the ranking table from "never" to "one run".

### S2 — `normalize_name` is unversioned, so fixing it re-splits the master and looks like success

- **Where:** `dedup.py:66-69`, `main.py:150` (master reload), `main.py:181`.
- **Breaks:** the master stores `name`, never the key. Every run re-derives keys from the full master (`main.py:179-180`), so any change to `normalize_name` is applied retroactively to months of accumulated rows. Records that the old key merged now diverge into two rows, and records the old key kept apart now merge. `main.py:181` prints a much larger number and nobody — including the engineer who just shipped the fix — can tell a corrected key from a record explosion.
- **Trigger:** apply audit 058's per-script fix and the next weekly run reports `After merge + dedup: N+1800 unique businesses` with no indication that ~1500 of those are a previously-over-merged pair being split by an unrelated name change.
- **Why silent:** no key version is stored in any column and no key column exists at all, so there is no way to diff two runs' identity decisions.
- **Fix:** add `dedup_key_version` as a constant string stamped into a new CSV column (or a sidecar `data/run_manifest.json`), and record per-run `records_in` / `records_out` / `collision_count` so a key change is visible as a delta rather than as growth.

### S2 — Invisible code points survive into the key

- **Where:** `dedup.py:67-69`.
- **Breaks:** `unicodedata.normalize` preserves default-ignorable and formatting code points, and `re.sub(r"\s+", " ", ...)` does not match any of them. Verified — each of these produces a key that differs from the same name without the character:
  ```
  'caf\u200ce'  -> 'c a f ‌ e'   ZWNJ U+200C   ≠ 'cafe'
  'cafe\u200d'  -> trailing ZWJ U+200D        ≠ 'cafe'
  '\u200ecafe'  -> leading LRM U+200E          ≠ 'cafe'
  'cafe\u200f'  -> trailing RLM U+200F        ≠ 'cafe'
  'ca\u200bfe'  -> ZWSP U+200B                ≠ 'cafe'
  '\ufeffcafe'  -> BOM U+FEFF                 ≠ 'cafe'
  'caf\x00e'    -> NUL U+0000                 ≠ 'cafe'
  ```
  ZWNJ and ZWJ are pervasive in Arabic and Persian text (Persian shop names transliterated into OSM, Persian-language sites scraped for KSA leads). A BOM *inside a cell* survives `main.py:85`'s `utf-8-sig` — that only strips a BOM at the start of the file. Note the asymmetry: this makes keys *worse*, while the same function silently *improves* others (see "not a bug").
- **Trigger:** the same Jeddah pharmacy recorded as `دار\u200cالصيدلة` by one source and `دار الصيدلة` by another produces two rows.
- **Fix:** strip `Cc`, `Cf`, `Zs`-except-space and `Mn` by *category*, not by combining class: `"".join(c for c in name if unicodedata.category(c) not in ("Cc", "Cf", "Co", "Cs"))`, and map `\u00a0`→space before the `\s+` collapse (NBSP already works — verified — but be explicit).

### S2 — Nameless **and** phoneless records collapse into one row per city, irreversibly

- **Where:** `dedup.py:212-214`, reachable via `main.py:87-89`.
- **Breaks:** `normalize_name("") == normalize_name(None) == ""`, so every such record keys to `("", city)` and all of them merge into one. This needs **both** fields empty — if either has a phone the record goes to `phone_index` instead — and all three scrapers skip nameless records (`scrapers/osm.py:64`, `scrapers/wikidata.py:92`, `scrapers/google_places.py:269`), so it is not scraper-reachable today. But `main.py:87-89` converts every `""` cell in the master to `None`, and the master is a Google-Drive CSV that sales staff open in Sheets (`scrape.yml:88-89`). One cleared name cell in a row that also has a blank phone detonates it.
- **Trigger** (verified — note this needed `phone: None`, my first attempt with phones set correctly did *not* collapse):

  ```python
  dedup([
    {"name": "",    "category": "cafe", "address": "Downtown, Beirut", "phone": None, "source": "wikidata"},
    {"name": None,   "category": "cafe", "address": "Hamra, Beirut",     "phone": None, "source": "osm"},
  ])
  # in=2  out=1
  # [{'name': None, 'category': 'cafe', 'address': 'Hamra, Beirut', 'source': 'osm|wikidata'}]
  ```
- **Why silent:** the merged row reports `name=None`, which `write_csv` writes as an empty cell — visually indistinguishable from a row that simply lacks a name. It is written back to the master and re-collapses every subsequent run.
- **Fix:** refuse to build a name key from an empty name — route such records to their own bucket keyed by `lat/lon`, or emit them with a synthetic `name` of `f"(unnamed {category} {city})"` so the loss is visible in the CSV rather than invisible in a count.

### S2 — `normalize_name` raises `TypeError` on any truthy non-`str` name, killing the whole run

- **Where:** `dedup.py:67` (`unicodedata.normalize("NFKD", name)`), reached via `dedup.py:212-214`.
- **Breaks:** `dedup.py:212`'s `or ""` absorbs every *falsy* non-string, so only truthy non-strings crash. Verified for `int`, `float`, `list`, `dict`, `bytes`, and `bool`. The exception propagates out of `dedup()` and terminates `main()` before any CSV is written; `scrape.yml:69` fails the step so nothing is uploaded. Loud, and the atomic write means the Drive master is untouched — the blast radius is one wasted run.
- **Trigger:** `dedup([{"name": 12345}])` → `TypeError: normalize() argument 2 must be str, not int`.
- **Reachability today:** low. The typed contract is `name: str | None` (`scrapers/base.py:6`), all three scrapers build `name` from a JSON string and `continue` when falsy, and `load_master` only ever produces `str`. This becomes live the moment a source is added, a scraper is refactored to stop normalizing its payload, or an enrichment step writes a non-string into `name`.
- **Fix:** `if not isinstance(name, str): return ""` as the first line of `normalize_name`. One line, closes the whole class.

### S2 — Under-merge: punctuation, word order, and Arabic letter variants all split one business into several rows

- **Where:** `dedup.py:69`.
- **Breaks:** `normalize_name` folds whitespace and case (correctly) but not punctuation or word order. Eight spellings of one Beirut restaurant produce **four** distinct keys (verified):

  | input | key |
  |---|---|
  | `Al Salam Restaurant` | `al salam restaurant` |
  | `Al  Salam Restaurant` | `al Salam`→`al salam restaurant` ✅ merges |
  | `AL SALAM RESTAURANT` | `al salam restaurant` ✅ merges |
  | `Al-Salam Restaurant` | `al-salam restaurant` ❌ |
  | `Alsalam Restaurant` | `alsalam restaurant` ❌ |
  | `Restaurant Al Salam` | `restaurant al salam` ❌ |

  Plus two Arabic-specific splits I confirmed are *not* folded (audit 003 lists these; audit 058 does not):
  - `ة` U+0629 vs `ه` U+0647 — `مطعم` → `مطعمه` ❌ (verified `False`)
  - `ى` U+0649 vs `ي` U+064A — `على` vs `علي` ❌ (verified `False`)
  - tatweel `ـ` U+0640 — `مـطعم` vs `مطعم` ❌ (verified `False`); U+0640 is `Lm`, has `ccc=0`, so the `combining()` filter at `dedup.py:68` cannot remove it
- **Trigger:** OSM's Lebanese names are heavily hyphenated (`Al-Salam`); Google appends qualifiers; Lebanon's `S.A.L.` legal suffix is pervasive (audit 003). Each variant is one extra row in the cumulative master.
- **Why silent:** extra rows look like new leads. `main.py:220` goes **up**, which reads as a good harvest.
- **Fix:** strip punctuation to space (not to nothing, so `Al-Salam` → `al salam`, not `alsalam`), drop `S.A.L.`/`SARL`/`LLC`/`Co.` tokens, map `ة→ه` and `ى→ي`, drop U+0640, and add a city-blocked similarity pass on top. Audit 058 §Design spec already specifies this — reuse it rather than reinventing.

### S3 — The published CSV ships the raw, un-normalized name

- **Where:** `dedup.py:214` builds a key but never rewrites `record["name"]`; `main.py:33-40` writes `name` as-is.
- **Breaks:** confirmed above — the merged row's `name` is `Cafe  Arz`, double space and all, because `_pick` chose Google on trust. The master therefore accumulates cosmetic damage (`Cafe  Arz`, `Al-Salam`, `café` vs `cafe`) that a rep sees in Sheets. Normalizing the display name (not just the key) costs nothing and makes the sheet look like a product.
- **Fix:** after a successful key match, store the variants on a new `name_variants` column rather than silently discarding them; that also gives you the audit trail S2 asks for.

### S3 — The only undocumented function in the file

- **Where:** `dedup.py:66-69`.
- **Breaks:** `normalize_phone` (8-line docstring naming the exact `+` bug it fixed), `_is_missing` (5 lines), `_pick` (7 lines), `write_csv` (12 lines), `resolve_country` (8 lines) — every neighbouring function documents the historical bug that motivated its current form. `normalize_name` has no docstring, no comment, and — unlike `scrapers/osm.py:63` — no note that it is the *sole* identity signal for every phone-less record. The comment at `dedup.py:220-221` explains the *ordering* of the two output lists but never says what the key means or that NFKD is destructive for Arabic.
- **Fix:** four lines of docstring stating the input contract (`str`, `""` allowed), the three transformations, and the explicit warning that line 68 deletes hamza/madda from Arabic.

---

## Not a bug, but worth knowing

- **Harakat are stripped correctly — I was wrong to suspect otherwise.** `unicodedata.combining()` returns non-zero for every Arabic short-vowel mark, so `dedup.py:68` removes them. Verified: `مطعم` and `مَطْعَم` produce an identical key. Measured ccc values: `U+064B`=27, `U+064E`=30, `U+064F`=31, `U+0651`=33, `U+0652`=34, `U+0670`=35 — all non-zero, all stripped. The marks that *must* survive (hamza `U+0654`=230, madda `U+0653`=230, hamza-below `U+0655`=220) are precomposed letters, which is exactly the collision audit 058 S1 identifies. **The Latin path is correct as written.**
- **Whitespace and case folding work.** `re.sub(r"\s+", " ", ...)` handles interior runs, tabs, newlines, and non-breaking space (U+00A0) correctly — verified `café\xa0bar` → `cafe bar`. Only the default-ignorable code points listed in S2 evade it. `.strip()` at `dedup.py:69` is redundant (the `\s+` collapse already reduced ends to a single space) but harmless.
- **Not a performance problem.** `normalize_name` costs **3.70 µs/name** measured over 500,000 calls (1.85 s total). At any master size this pipeline will reach, it is invisible next to the 40-thread HTTP fan-out in `enricher.py`. Do not spend time optimizing it.
- **Idempotent** on a 13-case adversarial set (`Café`, `CAFÉ`, `İstanbul`, `Ǻpple`, `ﬁne`, `Ångström`, `ＮＯＲＭＡ`, `Ⅻ`, `ẞ`, `école`, decomposed `cafe\u0301`) — `normalize_name(normalize_name(x)) == normalize_name(x)` for all 13. No CSV round-trip drift.
- **Short common names do not over-merge when addresses differ.** Four records named `Ali` in Beirut / Tripoli / Saida / no-address produce four output rows. `_extract_city` (`dedup.py:75-76`) is doing its job here. The failure mode is specifically the *empty* city (S1-1), not short names.
- **`dedup()`'s `continue` at `dedup.py:229` is dead code.** A record only reaches `name_index` when `normalize_phone` already returned `""` (`dedup.py:205-206`), and `_merge` can only choose among phones that were themselves `""`-normalizing, so the re-check at line 228 always yields `""`, which is never in `captured_phones` (all keys in `phone_index` are non-empty). Harmless today — the dead branch happens to skip nothing — but it advertises a phone↔name cross-merge that does not exist, which is how audit 003's "make the cross-dedup actually run" recommendation was mis-scoped. Delete it or implement it; do not leave it implying a guarantee that isn't there.
- **The empty-run path is properly guarded.** `dedup([])` → `[]`, and `scrape.yml:78-83` rejects a header-only CSV via `ROWS < 100`. No finding.
- **The run is genuinely interrupt-safe.** Pure function, no mutation of input records, atomic CSV replace at `main.py:124-133`, and `scrape.yml:85` never uploads after a non-zero exit. This part is right; leave it alone.
- **Type contract is already `str | None`** (`scrapers/base.py:6`), so the S2 `TypeError` is a contract violation rather than a documented hazard — which is why it is S2 and not S1 despite killing the run when it fires.

---

## Recommended order of work

1. **Add `dedup_diagnostics.json`** (S2, observability). ~10 lines at `dedup.py:197-232`. Do this *first* and unconditionally — it is the difference between every other fix on this list being verifiable and being another silent change. Emit records-in, keyed-by-name count, distinct keys, collision count, and the 20 most-collided keys with their variant spellings.
2. **Stop merging on an empty city** (S1-1). Fall back to rounded `lat/lon` when `_extract_city` returns `""`. Three-line change, removes a nationwide silent data-loss path.
3. **Guard the merge on coordinate distance** (S1-2). Reject any candidate pair more than ~500 m apart. Also fixes the existing cross-source address disagreement (audit 003 S1: `"…, Beirut"` vs `"…, Beirut, Lebanon"`) as a side effect.
4. **Never build a name key from an empty name** (S2). One line at `dedup.py:214`; prevents the Sheets-edit detonation.
5. **Harden `normalize_name` itself** (S2 ×3 + S3). `isinstance` guard, category-based invisible-char strip, punctuation-to-space, `ة→ه`/`ى→ي`, drop U+0640 — then apply audit 058's per-script NFKC branch. Ship 4 and 5 together, with a `dedup_key_version` stamp, so the retroactive re-split is visible as a delta rather than as growth.
6. **Delete or implement `dedup.py:229`** (S3), and add a `name_variants` column so merge decisions are auditable from the CSV alone.