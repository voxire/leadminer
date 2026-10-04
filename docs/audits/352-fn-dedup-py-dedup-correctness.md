# 352 — `dedup()` correctness

Target: `dedup.py:197-232` (plus the helpers it composes: `normalize_phone` `:33-63`,
`normalize_name` `:66-69`, `_extract_city` `:72-76`, `_pick` `:159-187`, `_merge` `:190-194`).
Sole caller: `main.py:180` (`records = dedup(combined)` where
`combined = raw_filtered + master`, `main.py:179`).

Every finding below was **executed**, not reasoned about. Harness lives outside the repo
(`/private/var/folders/.../T/opencode/dedup_correctness.py`, `dedup_round2.py`,
`dedup_round3.py`); `requests` is stubbed because `main`/`enricher` import it at module
scope. No repo file was modified.

Repro preamble:

```python
import sys; sys.path.insert(0, "/Users/mhomsi/dev/dummy/leadminer")
from dedup import dedup, normalize_phone, _extract_city
```

---

## Verdict

`dedup()` **silently deletes distinct businesses and silently duplicates single ones**, and
both failure modes are unconditional consequences of the key construction at `dedup.py:214`
and `:205`, not of unlucky data. The name key's second component is the *last comma-separated
token of the address*, which for Google `formattedAddress` is the **country** — so every
phone-less business in a country shares one name key, and 8 branches of one chain in 8
different cities collapse to **1 row**. Separately, `normalize_phone` maps every
all-zero phone to the literal string `"+961"`, which is a **single shared key for the entire
corpus** — the exact bug the docstring at `dedup.py:36-38` claims to have fixed, only
half-fixed.

Not everything is wrong: `_merge` is genuinely order-independent and idempotent, and the
phone index produces **zero** accidental collisions across realistic LB/SA numbers. The
rewrite of the merge rule is sound. The damage is entirely in the *keys*.

---

## Findings

### S1 — Every all-zero phone collapses to the shared key `"+961"`

- **Where:** `dedup.py:30` (`_MIN_DIGITS = 7`), `dedup.py:45-46`, `dedup.py:63`
  (`return "+" + cc + digits.lstrip("0")`), consumed as a phone key at `dedup.py:205-210`.
- **Breaks:** `len(digits) >= 7` passes for `"0000000"`. The `00` branch at `dedup.py:50-52`
  strips it to `"00000"`. No known country code matches. `lstrip("0")` then empties the
  string and the function returns the bare country code as a **shared, dialable-looking
  phone key**. Every all-zero phone in the corpus lands on `phone_index["+961"]` and is
  merged with every other one. This is the regression the docstring at `dedup.py:36-38`
  claims to have closed; the test at `tests/test_lead_signal.py:103-108` only covers
  `"---", "...", "  ", "n/a", "ext 4"`, all of which return `""` correctly, so it misses
  this input entirely.
- **Trigger** (exact input, exact wrong output):

  ```python
  dedup([
    {"name": "B", "address": "Sidon",   "country": "LB", "phone": "0000000",     "source": "osm"},
    {"name": "C", "address": "Byblos",   "country": "LB", "phone": "000-000-0000", "source": "osm"},
  ])
  ```

  → **`1` record**, not `2`:

  ```
  [ {'address': 'Byblos', 'country': 'LB', 'name': 'C', 'phone': '0000000', 'source': 'osm'} ]
  ```

  "B" is gone; the survivor is named **C** because the final tiebreak at `dedup.py:184`
  (`str(value)`) compares `"C" > "B"`. A four-record run collapses to three for the same
  reason. Confirmed normalization outputs:

  ```
  normalize_phone("0000000",           "LB") == "+961"
  normalize_phone("0-000-0000",        "LB") == "+961"
  normalize_phone("000 000 0000",      "LB") == "+961"
  normalize_phone("+0000000000",       "LB") == "+961"
  normalize_phone("00000000000000000", "LB") == "+961"
  ```
- **Fix:** one line — after `lstrip`, bail if nothing is left:
  `dedup.py:63` → `national = digits.lstrip("0"); return "+" + cc + national if national else ""`.

---

### S1 — The name key's "city" is the **country**, so phone-less chain branches collapse to one row

- **Where:** `dedup.py:72-76` (`_extract_city` returns `parts[-1]`), key built at
  `dedup.py:213-214`. Fed by `scrapers/google_places.py:286`
  (`address=place.get("formattedAddress")`).
- **Breaks:** `formattedAddress` ends in the country for essentially every Saudi and
  Lebanese place (real-world shape: `"King Fahd Rd, Al Olaya, Riyadh, Saudi Arabia"`;
  per Google's own address-parsing convention, "the country is the last part of the
  address"). So the `city` discriminator is a **constant per country** and carries zero
  discriminating information. Any two phone-less records with the same normalized name
  merge regardless of how far apart they are, and `_merge` (`dedup.py:190-194`) silently
  drops the loser's address, coordinates and contacts.
- **Trigger** (exact input, exact wrong output):

  ```python
  dedup([{"name": "City Pharmacy", "address": f"{c}, Saudi Arabia",
          "country": "SA", "phone": None, "source": "google_places",
          "scraped_at": "2026-01-01"}
         for c in ["Riyadh","Jeddah","Dammam","Abha","Tabuk","Mecca","Medina","Jizan"]])
  ```
  → **`1` record**, address `"Tabuk, Saudi Arabia"`. The correct answer is `8`.
  Same shape: 7 `Al Baik` branches → 1; 4 `Beko` branches in Beirut/Tripoli/Sidon/Zahle
  (address `"X, Lebanon"`) → 1.
- **Note the precondition is common.** This fires whenever *both* records are phone-less,
  and phones are frequently absent: `scrapers/osm.py:89` reads an optional `phone` tag,
  `scrapers/wikidata.py:29` uses `OPTIONAL { ?item wdt:P1329 ?phone }`, and
  `scrapers/google_places.py:289` reads `nationalPhoneNumber`, which `textSearch` omits for
  a large share of places. Give the same 8 pharmacies per-branch numbers and `dedup`
  correctly returns 8 — which is why this has not been noticed.
- **Fix:** one line — drop a trailing country token before taking the last segment:
  `dedup.py:75-76` → strip `{"lebanon","saudi arabia"}` from `parts`, then `parts[-1]`;
  failing that, key on `parts[-2]` with a city gazetteer.

---

### S1 — The cross-tier reconciliation at `dedup.py:225-230` is unreachable; one business is emitted twice

- **Where:** `dedup.py:220-221` (comment), `dedup.py:225-230` (the loop).
- **Breaks:** the loop reads `record.get("phone")` off `name_index` values. But a record
  only enters `name_index` if `not raw_phone` or `normalize_phone(...) == ""`
  (`dedup.py:205-211`). `normalize_phone` is pure, so line 228 recomputes that same `""`,
  and `""` is never a key in `phone_index`, which holds only truthy keys (`dedup.py:206`).
  Therefore `continue` **never fires**, provably:

  ```
  raw=None       line205 key=''    -> name_index   line228 guard: raw falsy       -> skip check
  raw=''         line205 key=''    -> name_index   line228 guard: raw falsy       -> skip check
  raw='   '      line205 key=''    -> name_index   line228 guard: normalize==''   -> not in captured
  raw='---'      line205 key=''    -> name_index   line228 guard: normalize==''   -> not in captured
  raw='12'       line205 key=''    -> name_index   line228 guard: normalize==''   -> not in captured
  raw='ext 4'    line205 key=''    -> name_index   line228 guard: normalize==''   -> not in captured
  ```
  A business that has a phone in one source and no phone in another therefore produces
  **two rows**, which inflates `all_businesses.csv` and double-counts `sales_ready.csv`
  (`main.py:202-205`). The comment at `dedup.py:220-221` describes behavior that cannot occur.
- **Trigger:**

  ```python
  dedup([{"name": "Falafel Tabbara", "address": "Hamra, Beirut", "country": "LB",
          "phone": "01 234 567", "source": "osm", "scraped_at": "2026-01-01"},
         {"name": "Falafel Tabbara", "address": "Cairo Street, Beirut", "country": "LB",
          "phone": None, "source": "wikidata", "scraped_at": "2026-01-01"}])
  ```
  → **`2` records**. Correct answer: `1` (merged, with the OSM phone plus any Wikidata
  contacts).
- **Fix:** reconcile by identity rather than by the phone the record lacks — after
  building both indexes, for each `name_index` group compute `(normalize_name(name), city)`
  and merge it into any `phone_index` record whose `(normalize_name(name), city)` matches.

---

### S2 — The name key is not comparable across sources, so cross-source duplicates survive

- **Where:** `dedup.py:213`, versus `scrapers/osm.py:80-87`,
  `scrapers/google_places.py:286`, `scrapers/wikidata.py:99`.
- **Breaks:** the three sources build `address` on three different schemas, and
  `_extract_city` takes the last token of each. The `city` half of the key is therefore
  drawn from a different *vocabulary* per source:

  | Source | `address` | `_extract_city` → key half |
  |---|---|---|
  | Google (SA) | `Al Olaya St, Riyadh, Saudi Arabia` | `"saudi arabia"` |
  | Google (LB) | `Hamra, Beirut, Lebanon` | `"lebanon"` |
  | OSM | `12, Rue Verdun, Hamra, Beirut, Ras Beirut` | `"ras beirut"` (`addr:district`, `osm.py:85`) |
  | OSM | `12, Rue Verdun, Beirut` | `"beirut"` |
  | OSM | `12` (`addr:housenumber` only) | `"12"` |
  | Wikidata | `Cairo Street, Beirut` (P6375, `wikidata.py:99`) | `"cairo street, beirut"` |

  OSM's last segment is `addr:district` (`osm.py:85` is the final element of `addr_parts`),
  not `addr:city`. Wikidata's P6375 is a single free-text string with no city slot at all.
  No OSM key ever equals a Google key, so the same phone-less business found by two sources
  is never merged.
- **Trigger:** OSM `{"name": "Cafe Lebanon", "address": "Hamra, Beirut", "phone": None}`
  and Google `{"name": "Cafe Lebanon", "address": "Hamra, Beirut, Lebanon", "phone": None}`
  → **`2` records**; the OSM row keeps `website="https://cafe.example"` and the Google row
  does not, so the enriched contact data is split across two CSV rows.
- **Fix:** replace `_extract_city` with a shared locality extractor fed from Google
  `addressComponents` / OSM `addr:city`, and validate it against a city gazetteer before
  using it as a key half.

---

### S2 — `country` (and `region`, and the surviving `phone`) is chosen by lexicographic string order

- **Where:** `dedup.py:179-187` — the rank tuple's last element is `str(value)`
  (`dedup.py:184`) — combined with `_SOURCE_TRUST` (`dedup.py:97-110`), which has no
  `country` or `region` entry, so `_trust_for` returns `_DEFAULT_TRUST = 1`
  (`dedup.py:111`) for both sides and `_validity` returns `1` for both.
- **Breaks:** when two records share a dedup key but disagree on `country`, the winner is
  the one whose ISO code sorts later. `"SA" > "LB"`, so a Saudi record always wins — and
  `main.py:183-184` runs `resolve_country` **after** `dedup`, accepts `"SA"`/`"LB"`
  verbatim, and the mislabel becomes permanent in the master CSV. This is precisely the
  cross-border case the header comment at `dedup.py:5-7` says the module exists to handle.
- **Trigger:**

  ```python
  _merge({"name": "Al Noor", "phone": "+96170123456", "country": "SA",
          "source": "google_places", "scraped_at": "2026-01-01"},
         {"name": "Al Noor", "phone": "70 123 456",  "country": "LB",
          "source": "osm",          "scraped_at": "2026-01-01"})
  ```
  → `country == "SA"` while the surviving `phone` is `"70 123 456"`, i.e. **Lebanese**.
  Then `main.py:194` normalizes it to `+96170123456`, so the row ships as
  *country=SA, phone=+961…*. Order-independent: `_merge(b, a)["country"]` is also `"SA"`.
  The same mechanism decides `region` (`"Mount Lebanon"` beats `"Beirut"`), and `phone`
  itself (`'+96170123456'` vs `'70 123 456'` → `"70 123 456"` wins, because `'+' < '7'`).
- **Fix:** one line — special-case `country` in `_pick` alongside `source`/`scraped_at` and
  derive it from the surviving normalized phone key rather than from a string comparison.

---

### S2 — `lat` and `lon` are chosen independently, synthesizing a coordinate pair that belongs to no record

- **Where:** `dedup.py:190-194` (`_merge` iterates fields one at a time), tiebreak
  `dedup.py:184`.
- **Breaks:** when two merged records have equal trust and validity for `lat`/`lon` (same
  source, e.g. two OSM rows), each coordinate is decided by its own `str()` comparison. If
  `lat` sorts later for record B and `lon` sorts later for record A, the merged record gets
  A's longitude with B's latitude — a location that is not a real point. That point is fed
  straight into `infer_region` at `enricher.py:331-335`.
- **Trigger:** two `osm` records on the same phone,
  A `lat=33.8938, lon=35.5018` (Beirut) and B `lat=34.5560, lon=31.7784` (far north/east):
  merged `(34.556, 35.5018)`; `infer_region(None, 34.556, 35.5018, country="LB")` returns
  **`"North Lebanon"`** where the real A point yields **`"Beirut"`**. The wrong region then
  survives into `region` (not overwritten later, since `enricher.py:331` only recomputes
  when it is falsy) and into every regional summary at `main.py:228-234`.
- **Fix:** one line — in `_merge`, handle `"lat"`/`"lon"` as a unit: pick the winning
  record on `"lat"` and copy both coordinates from it.

---

### S3 — The final tiebreak compares integers as text, so `review_count` keeps the smaller number

- **Where:** `dedup.py:184` — `str(value),  # deterministic final tiebreak`.
- **Breaks:** for any integer-valued field whose rank ties, the comparison is lexicographic.
  Measured, both argument orders:

  | field | a | b | winner | correct |
  |---|---|---|---|---|
  | `review_count` | 9 | 100 | **9** | 100 |
  | `review_count` | 12 | 8 | **8** | 12 |
  | `lead_score` | 42 | 7 | **7** | 42 |
  | `review_count` | 3 | 30 | 30 | 30 |

  `rating` is **not** affected: Google ratings are 1-decimal floats, and for every one of
  the 1 640 `(x, y)` pairs over `1.0…5.0` step `0.1` (and `0.01` steps vs `{3.0,3.5,4.0,4.5,5.0}`)
  the `str()` order matches numeric order, including across the `4.0` threshold that
  `enricher.py:312-314` keys `lead_score` off. So the damage is confined to
  `review_count`, which no downstream consumer reads.
  Trigger requires equal `scraped_at`, which is the normal intra-run case:
  `scraped_at` is one timestamp for the whole batch (`scrapers/google_places.py:173`), so
  two Google records sharing a phone with review counts 9 and 100 merge to **9**.
- **Fix:** one line — make the tiebreak type-aware, e.g.
  `str(value)` → `(isinstance(value, (int, float)) and not isinstance(value, bool), value, str(value))`.

---

### S3 — `normalize_phone` can return a key longer than E.164 allows

- **Where:** `dedup.py:53-55` — the `0 + cc` branch tests only `cc`, the *record's*
  country, while `dedup.py:58-60` scans for *any* known code after the fact.
- **Breaks:** if the record's `country` disagrees with the number's own prefix and the
  number is written `0<country-code>…`, the `00`-strip at `dedup.py:50-52` does not fire,
  the `0 + cc` branch does not fire, the known-code scan misses because the digits are
  still `0`-prefixed, and `dedup.py:63` prepends `cc` to the whole thing.
  `scrapers/osm.py:103` hardcodes `country="LB"` for every OSM record (as does
  `scrapers/wikidata.py:111`), so any Saudi business in the OSM extract with a `phone` tag
  in that form hits this.
- **Trigger:** `normalize_phone("0966501234567", "LB")` → **`"+961966501234567"`**, 15
  digits after the `+`. E.164 caps the entire number at 15 digits, so no dialer can use it,
  and it is a stable-but-unique key (so it causes no merge damage — only bad data).
- **Fix:** at `dedup.py:50`, strip `00` **or** a leading `0` before *any* known code, not
  just `0 + cc`: `elif digits.startswith("0"): digits = digits[1:]`.

---

### S3 — `dedup()` has no test coverage at all, and the one adjacent test misses the S1 above

- **Where:** `tests/test_lead_signal.py:65` imports `_merge` and `normalize_phone`; no file
  under `tests/` or `scripts/` calls `dedup(`. The nearest test,
  `tests/test_lead_signal.py:103-108`, asserts `normalize_phone` returns falsy for
  `("---", "...", "  ", "n/a", "ext 4")` — all of which are caught by the
  `len(digits) < _MIN_DIGITS` guard at `dedup.py:45-46`. `"0000000"` passes that guard and
  is exactly the shape the test was written to exclude.
- **Fix:** one line each — add `"0000000"`, `"0-000-0000"`, `"+0000000000"` to that
  tuple, and add the three `dedup()` cases from S1-1/S1-2/S1-3 above.

---

## Not a bug, but worth knowing

- **`_merge` really is order-independent and idempotent — this is a genuine improvement.**
  All 720 permutations of a 6-record pool (mixed sources, `scraped_at` spread over six
  days, conflicting `rating`/`review_count`/`lat`/`lon`/`website`/`email`) produced
  byte-identical output. `rank` (`dedup.py:179-185`) is a total order ending in the value
  itself, so it is a `max`, and `max` is associative; `_sources_of` trust
  (`dedup.py:143-152`) takes a max over a growing union, which is also associative.
  Consequence: the `as_completed` nondeterminism at `main.py:162-166` does **not** leak
  into merged field values. Findings 002-S1-3 and 024-S1-1 (arrival-order
  non-determinism) are genuinely fixed by commit `99493b9`.
- **`_merge` never discards a populated value.** `_pick` returns the other side whenever
  one side is missing (`dedup.py:168-171`) and otherwise takes a real winner. Merging a
  record carrying `rating=4.7, review_count=120, website="https://real.example"` with one
  carrying `None`s yields all three populated values back. No S1 here.
- **The phone index itself is sound.** Brute-forcing 16 realistic LB/SA national numbers
  (Lebanese `1x`/`7x`/`8x`, Saudi `11x` landline and `5x` mobile) × 5 format variants each
  (`96170123456`, `+96170123456`, `0096170123456`, `+961 70 123 456`, `07 0123 456`) gave
  14 distinct keys and **0 collisions** between different numbers. All phone-key damage in
  this report comes from junk input (S1-1), not from the normalization rules. This is a
  real strengthening versus the pre-rewrite state described in
  `docs/audits/001-dedup-phone-normalization.md`.
- **The master-CSV round-trip is stable.** `main.py:193-194` writes normalized phones
  (`+966501234567`), and the next run's `dedup` re-keys them identically because the
  early return at `dedup.py:58-60` matches the country prefix before `country` matters.
  Verified for Lebanese and Saudi mobile and landline. Legacy master rows with
  `country=""` → `None` (`main.py:86-89`) fall back to `"LB"` at `dedup.py:205`, which is
  harmless **because** their phones are already in international form.
- **Stale `website_live` after a merge is not a bug.** `_VOLATILE` (`dedup.py:114`) can pair
  a kept website with the other record's liveness flag, but `enricher.check_websites`
  recomputes `website_live` for every record that has a website
  (`enricher.py:231`, `enricher.py:250`) on the same run. I looked for this and it is
  covered downstream.
- **`_extract_city` on a single-part address returns the house number**
  (`osm.py:81` alone → `"12"`). Harmless in practice, but it means a nameless-address OSM
  record keys on `("<name>", "<housenumber>")`.
- **`dedup()` reorders its output** (`return phone_records + name_records`,
  `dedup.py:232`). Every consumer indexes by content, so this is cosmetic — but it means
  row order in `all_businesses.csv` is not input order, and a diff between two runs will
  show spurious reordering.

---

## Recommended order of work

1. **S1 junk-key** — one line at `dedup.py:63`. Highest ratio of blast radius to diff size:
   a single shared key corrupts unrelated businesses across the whole corpus, and it is the
   one S1 whose fix cannot regress anything.
2. **S1 name key** — replace `_extract_city` (`dedup.py:72-76`) with a locality extractor
   that strips the country token and reads OSM `addr:city` rather than `addr:district`.
   Fixing this kills both S1-2 and most of S2-1 at once, since both are symptoms of the
   `parts[-1]` heuristic.
3. **S1 cross-tier reconciliation** — reimplement `dedup.py:225-230` to compare
   `(normalize_name(name), city)` across the two indexes instead of reading a phone the
   name-index record does not have.
4. **S2 `country`** — special-case it in `_pick` and derive it from the surviving phone
   key, and move `main.py:183-184` (`resolve_country`) to **before** `main.py:180` (`dedup`)
   so key construction and dedup see the same resolved country.
5. **S2 lat/lon** — copy both coordinates from one winning record in `_merge`.
6. **S3s** — type-aware tiebreak (`dedup.py:184`), the `0`-strip in
   `normalize_phone` (`dedup.py:50`), and test coverage for `dedup()` itself.

Note that `docs/audits/003-dedup-identity-model.md` already filed S1-2 and S1-3 against
`dedup.py:34-38` and `:90-95`. Both were still present after the
`_pick`/`_merge` rewrite in commit `99493b9`; that commit changed how values are *chosen*
without touching how records are *keyed*, and the keying is where the data loss is.