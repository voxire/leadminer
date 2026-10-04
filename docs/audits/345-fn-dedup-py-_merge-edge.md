# 345 — `_merge` (dedup.py:190): edge & hostile-input audit

**Target:** `dedup._merge` at `dedup.py:190`
**Lens:** edge — boundary and hostile inputs
**Method:** every claim below was executed against the real functions (Python 3.14, `requests` stubbed so `enricher`/`main` import without installing anything). No network, no source modified. Probe scripts live outside the repo; every printed value is quoted verbatim.

---

## Verdict

`_merge` is five lines and has almost no surface of its own — **all of its behaviour is `_pick` (`dedup.py:159-187`)**, so the edge cases that matter are the ones the rank tuple resolves badly. The function is crash-hardy (no reachable traceback) but **not value-hardy**: its final tiebreak is `str(value)` (dedup.py:184), and because `lat`/`lon`/`country`/`region` have no entry in `_SOURCE_TRUST` (dedup.py:97-110) and are absent from `_VOLATILE` (dedup.py:114), **those four fields are frequently decided by a lexicographic string comparison**. The worst consequence is that `lat` and `lon` are resolved by two independent `_pick` calls, so `_merge` emits coordinate pairs that exist nowhere on Earth — 1239 km from both of the real locations — and writes them into the cumulative master CSV where they are never repaired.

**Single most dangerous input:** two records on the same dedup key that disagree about **latitude** — e.g. `lat=33.8876` (Beirut) in one and `lat=24.7136` (Riyadh) in the other. That one disagreement, combined with an independent longitude disagreement, is enough to fabricate `(33.8876, 46.6753)`, a point in the Saudi desert that satisfies neither `country`'s coordinate box nor the other's.

---

## Boundary-input classification

`_merge(a, b)` called directly. `a`/`b` default to two records with `name`, `source="osm"`, `scraped_at` set, unless noted.

| # | Input | Result | Class |
|---|---|---|---|
| 1 | `a=None` / `b=None` | `TypeError: 'NoneType' object is not iterable` (dedup.py:192) | crashes — **contract violation, unreachable** |
| 2 | `a=[("name","x")]` (list of pairs) | `AttributeError: 'list' object has no attribute 'get'` (dedup.py:167) | crashes — contract violation, unreachable |
| 3 | `a="ab"` (str) | `AttributeError: 'str' object has no attribute 'get'` | crashes — contract violation, unreachable |
| 4 | `a={}` , `b={}` | `{}` | correctly handled |
| 5 | `_merge(x, x)` | `x` — idempotent | correctly handled |
| 6 | field value `None` on one side | other side's value returned (dedup.py:168-171) | correctly handled |
| 7 | field value `None` on **both** sides | returns `b`'s, i.e. `None` or `""` depending on argument order | **silently type-churned** (S3) |
| 8 | field value `""` (empty string) | treated as missing; the other side's value survives | correctly handled |
| 9 | field value `"  "` / `"\t"` / `"\xa0"` / `"\u3000"` | all `strip()` to empty → missing | correctly handled |
| 10 | field value `"\u200b"` (ZWSP), `"\ufeff"` (BOM), `"\U0001F600"` | `_is_missing` → **False**: treated as real data | **silently wrong value** (S2) |
| 11 | field value `0` / `0.0` | `_is_missing` → **False**: treated as real data | **silently wrong value** (S2) |
| 12 | field value `False` | `_is_missing` → **False** (only matters for `website_live`; harmless today) | correctly handled by luck |
| 13 | field value `-1` | `_is_missing` → False, validity 1, wins on `str()` | **silently wrong value** |
| 14 | `lat`/`lon` = `nan`, `inf`, `-inf`, `"nan"`, `"Infinity"`, `"1e400"`, `91.0`, `999.0` | validity **3** (full marks); `nan` beats a real coordinate | **silently wrong value** (S1, S2) |
| 15 | `rating` = `nan` | validity **0** — correctly rejected (`0 <= nan <= 5` is False) | correctly handled |
| 16 | `rating` = `0.0` | validity **3**; fresh `0.0` overwrites stale `4.5` | **silently wrong value** (S3, unreachable from Google) |
| 17 | `scraped_at` = str vs str | `max()` picks the later; correct for same-offset ISO | correctly handled |
| 18 | `scraped_at` = str vs int epoch | `TypeError: '>' not supported between instances of 'int' and 'str'` | **crashes — latent run-killer** (S2) |
| 19 | `scraped_at` = str vs `datetime` | `TypeError: '>' not supported between 'str' and 'datetime.datetime'` | crashes — latent (S2) |
| 20 | `scraped_at` = `"…T10:00:00Z"` vs `"…T10:00:00+03:00"` | `max()` returns the `Z` form — 3 h **later** in absolute time | **silently wrong value** (S2) |
| 21 | `scraped_at` = `nan` (a CSV cell literally reading `nan`) | `max()` → `"nan"`, no crash; poisons the `_VOLATILE` tiebreak | **silently wrong value** (S3) |
| 22 | `source` = `0`, `None`, `""`, or `"osm|google_places"` | `_sources_of` (dedup.py:143-145) drops empties, `sorted()` join is stable | correctly handled |
| 23 | Arabic `"مخبز"` vs Latin `"Al Makhbaz"`, both `osm`+`wikidata` (name trust 2 == 2) | Arabic always wins (`U+0600` > `U+0041`) | arbitrary, not wrong (S3) |
| 24 | NFC `"Café"` vs NFD `"Café"` | picks NFC (`U+00E9` > `e`) | benign (S3) |
| 25 | `"Café"` vs `"Café"` (Arabic) | Arabic wins on codepoint order | arbitrary (S3) |
| 26 | `"😀Bakery"` vs `"Zzz Bakery"` | emoji name wins (`U+1F600` > `U+005A`) | arbitrary (S3) |
| 27 | `"\u200bBakery"` vs `"B Bakery"` | ZWSP name wins — **visually empty export cell** | **silently wrong value** (S2) |
| 28 | 5 MB string value | `_merge` 0.1 ms; `_validity("email", …)` 80 ms; `_validity("website", …)` 90–180 ms; **longest string always wins the tiebreak** | no ReDoS; wrong value is possible |
| 29 | extra/unknown keys on either side | union of keys preserved (dedup.py:192) | correctly handled (and moot — main.py:125 drops them) |
| 30 | float `33.9` vs str `"33.9"` on the same field | `_merge(a,b)`→`33.9`, `_merge(b,a)`→`'33.9'` | non-commutative, **unreachable today** (S3) |
| 31 | value whose `__str__` raises | propagates `RuntimeError` out of dedup.py:184 | crashes — unreachable (S3) |
| 32 | `country="LB"` vs `country="SA"` | **always `"SA"`** | **silently wrong value** (S2) |
| 33 | `region="Jeddah"` vs `"Riyadh"` / `"Dammam"` | **always `"Riyadh"` / `"Jeddah"`** | arbitrary, not wrong (S3) |
| 34 | `lat=33.8876,lon=35.5139` vs `lat=24.7136,lon=46.6753` (both `google_places`) | **`(33.8876, 46.6753)`** — spliced, 1239 km from both | **silently wrong value (S1)** |
| 35 | `lat=91.0, lon=999.0` vs `lat=33.8876, lon=35.5139` | **`(91.0, 999.0)`** — corrupt beats real | **silently wrong value (S1)** |
| 36 | `"https://localhost:8000"`, `"javascript:alert(1)"`, `"ftp://x.com"`, `"https://x"` | validity **0** | correctly handled |
| 37 | `"HTTPS://X.COM"`, `" https://x.com "` | validity **3** | correctly handled |
| 38 | `"https://x.com/a b"`, `"https://x.com\n"` | validity **3** (`match` + `^`/`$` don't require full match) | benign (S3) |
| 39 | `"a@b.com"` / `"a@b"` / `"a@@b.com"` / `"a b@c.com"` | 3 / 0 / 0 / 0 | correctly handled |
| 40 | 40,000 identical records on one key | 39 `_merge` calls, all O(1)-ish, no quadratic blowup | correctly handled |

---

## Findings

### S1 — `lat` and `lon` are picked independently and tie-broken by `str()`, so `_merge` fabricates coordinates

- **Where:** `dedup.py:190-194` (`_merge`, one `_pick` call **per key** at dedup.py:193), tiebreak `dedup.py:184` (`str(value)`), the gate that sends `lat`/`lon` down that path at `dedup.py:183` (`_recency(rec) if field in _VOLATILE else ""`), `_VOLATILE` omits both at `dedup.py:114`, `_validity` grants full marks at `dedup.py:128-133`.
- **Breaks:** `rank` is `(trust, validity, recency_or_"", str(value))`. For `lat`/`lon` the recency slot is always `""` (not volatile), so whenever two records share a source and both have parseable floats, the winner is whichever coordinate **string sorts later**. `lat` and `lon` are resolved by two separate calls, so the merged pair can mix record A's latitude with record B's longitude. `_validity` accepts *any* `float()`-able value — `nan`, `inf`, `1e400`, `91.0`, `999.0` all score **3** — so a corrupt coordinate ties with a real one and then wins on the digit comparison.
- **Trigger (verified, end-to-end through `dedup()`):**
  ```python
  {"source":"google_places","country":"LB","address":"Verdun, Beirut",
   "lat":33.8876,"lon":35.5139,"phone":"+961 1 234 567","rating":4.5,
   "scraped_at":"2026-10-01T00:00:00+00:00"}
  {"source":"google_places","country":"SA","address":"Riyadh",
   "lat":24.7136,"lon":46.6753,"phone":"+9611234567","rating":4.5,
   "scraped_at":"2026-10-01T00:00:00+00:00"}
  ```
  Both normalise to key `+9611234567` (dedup.py:205). Output of `dedup()`:
  ```
  1 record: phone=+9611234567  name='Bakery B'  country=SA
            address='Verdun, Beirut'  lat=33.8876  lon=46.6753
            resolve_country -> 'SA'   infer_region(country='SA') -> None
  ```
  The exported row says *Beirut* in `address`, *Saudi Arabia* in `country`, *Beirut* in `lat`, *Riyadh* in `lon`, and `region` is now `None`. The real Beirut point is `(33.8876, 35.5139)`; the emitted point is **1238.9 km away**. Every one of the 22 columns was decided by an independent string comparison, and nothing in the pipeline can detect the incoherence.
  Second verified trigger: `lat=91.0, lon=999.0` vs `lat=33.8876, lon=35.5139` (both `google_places`) → `_merge` returns **`(91.0, 999.0)`**, because `"91.0" > "33.8876"`.
- **Why it compounds:** it does not need two live scrapers. `main.py:179` merges `raw_filtered + master`, and `load_master` re-reads `lat`/`lon` as floats (main.py:43, 90-95) with `source="google_places"` preserved. Verified: master row `(24.7136, 46.6753, scraped_at=2026-06-01)` merged with fresh row `(33.8876, 35.5139, scraped_at=2026-10-01)` yields `(33.8876, 46.6753)`. **Every run re-splices any master row whose listing moved**, and the result is written back to `all_businesses.csv` (main.py:209) where it becomes the next run's input. Google also emits **one `scraped_at` for the whole batch** (google_places.py:173), so the intra-run `google_places`+`google_places` case has no recency tiebreak either — and `seen_ids` only dedups by `place_id` (google_places.py:181-184, 260-266), so two listings of one business under different `place_id`s but one phone number both survive into `dedup`.
- **Fix:** resolve `lat`/`lon` as an indivisible **pair** from a single record (special-case the two keys in `_merge` or pass a bundle to `_pick`), and tighten `_validity` for `lat`/`lon` to require `math.isfinite` and `-90 <= v <= 90` / `-180 <= v <= 180` (0 unless satisfied).

---

### S2 — `scraped_at` is merged with a bare `max()`: the one place `_pick` compares raw values, and the one place a run can die

- **Where:** `dedup.py:176-177`.
- **Breaks:** every other comparator in `_pick` is defensive — `_is_missing` type-checks (dedup.py:86-90), `_validity` catches `TypeError`/`ValueError` (dedup.py:129-139), `_recency` coerces with `str(... or "")` (dedup.py:156), and `str(value)` is total. `max(av, bv)` has **no type guard**. Both values are guaranteed non-`None` (dedup.py:168-171) but not guaranteed `str`.
- **Trigger:** `_merge({"scraped_at": "2026-10-01T00:00:00"}, {"scraped_at": 1757000000})` →
  `TypeError: '>' not supported between instances of 'int' and 'str'`. Same with a `datetime` on either side.
- **Why it is S2 and not S1 today:** nothing in the current pipeline produces a non-`str` `scraped_at`. All three scrapers use `utc_now_iso()` (osm.py:32, wikidata.py:67, google_places.py:173 → verified output `'2026-10-03T19:56:53.502521+00:00'`), and `load_master` has no cast for `scraped_at` (main.py:86-104 casts only `_FLOAT_FIELDS`/`_INT_FIELDS`, main.py:43-44), so it stays a string. **But** `dedup()` is called at main.py:180 **unguarded** — the scraper futures are wrapped in `try/except` at main.py:162-168, but `records = dedup(combined)` is not — so this single exception escapes `main()` and **none of the five CSVs are written** (writes start at main.py:208). That is the difference between a warning and a lost run, so it should be closed before anything starts writing `scraped_at` in another format.
- **Same line, silently wrong:** `max()` is lexicographic on ISO strings, which is only a time comparison when the offsets match. Verified: `max("2026-10-03T10:00:00Z", "2026-10-03T10:00:00+03:00")` returns the **`Z`** form — 10:00 UTC is *later* than 10:00+03:00, so the **earlier** instant wins the "freshest observation wins" rule that `rating`, `review_count`, `website` and `website_live` depend on (dedup.py:114, 183).
- **Fix:** `max(av, bv, key=_parse_iso)` where `_parse_iso` returns a real `datetime` (or `datetime.min` on failure, never raising) — one helper, both problems closed.

---

### S2 — `country` and `region` have no trust model, so they are decided by the alphabet

- **Where:** `_SOURCE_TRUST` (dedup.py:97-110) has **no `"country"` and no `"region"` key for any source**, so `_trust_for` returns `_DEFAULT_TRUST = 1` for all of them (dedup.py:111, 148-152); `country` is not in `_VOLATILE` (dedup.py:114) so the recency slot is `""`; both fall to the `str(value)` tiebreak at dedup.py:184. `region` is additionally pre-populated by Google at google_places.py:275, so it *is* present on both sides of a merge.
- **Breaks:** `country="LB"` vs `country="SA"` resolves to **`"SA"`, always** (`"SA" > "LB"`, verified). This is a systematic bias, not a coin flip. The OSM/Wikidata records that say `LB` are geo-fenced to Lebanon by construction (osm.py:13 `area["ISO3166-1"="LB"]`, wikidata.py:27 `wdt:P17 wd:Q822`), and `dedup.py:5-7` states outright that "data leaks across borders constantly" — yet when an `LB` record and an `SA` record land on one phone key, the merge always sides with `SA`.
- **Damage, verified:** the wrong code is then *locked in*. `resolve_country` (main.py:50-74) short-circuits on any valid code at main.py:59-67 and only reaches its phone-based fallback at main.py:68-73 when `country` is blank — so a merge-chosen `"SA"` is accepted at face value and never re-derived from the phone. `infer_region` then selects the KSA coordinate boxes (enricher.py:90, 70-76) instead of the LB ones (enricher.py:58-68) and skips address-keyword matching entirely (enricher.py:85 is `if address and country == "LB"`). Verified result: `region` → `None`, on a record whose own address and latitude are both Beirut.
- **`region` is arbitrary the same way** (verified): `"Jeddah"` vs `"Riyadh"` → `"Riyadh"`; `"Jeddah"` vs `"Dammam"` → `"Jeddah"`.
- **Fix:** add explicit `country`/`region` entries to `_SOURCE_TRUST` (OSM and Wikidata are geo-fenced to `LB`, so `country` trust should be `"osm": 3, "wikidata": 3, "google_places": 2`), and drop `country` from `_merge`'s decision entirely — derive it once in `resolve_country` from the phone, which already knows how (main.py:68-73).

---

### S2 — `_is_missing` calls `0`, `[]`, `{}` and invisible characters "present"

- **Where:** `dedup.py:79-90`.
- **Breaks, two ways:**
  1. **Falsy-but-real.** `_is_missing(0)`, `_is_missing(0.0)`, `_is_missing(False)`, `_is_missing([])` and `_is_missing({})` all return **False**. `_validity("rating", 0.0)` returns **3** (dedup.py:134-139, `0 <= 0 <= 5`), so a zero rating is a *fully valid* competitor. Verified: a fresh record with `rating=0.0` overwrites a stale `rating=4.5` (both `google_places`, equal trust, equal validity, `rating` is volatile so the newer wins at dedup.py:183) → merged `rating` is `0.0`. `lead_score` then awards +10 "low rating = pain point" (enricher.py:312-314) to a 4.5-star business. Same for `review_count`: verified fresh `review_count=0` overwrites stale `250` → **250 → 0**, a silent loss of a real signal. Reachability is honest-but-partial: Google returns `userRatingCount: 0` when a place has no reviews (google_places.py:298) so the zero is real API data; a genuine `rating: 0.0` is not, since Google omits `rating` entirely for unrated places (google_places.py:297). The `review_count` half is the reachable one.
  2. **Invisible characters.** `_is_missing` uses `str.strip()`, which removes everything where `str.isspace()` is True — verified correct for `\t \n \r \v \f \xa0 \u1680 \u2000 \u2028 \u2029 \u3000` — but **not** for `\u200b` (ZWSP), `\u200c` (ZWNJ) or `\ufeff` (BOM), all of which `isspace()` reports as False. `\u200b` is `U+200B`, above every Latin codepoint, so `"B\u200bBakery"` **beats** `"B Bakery"` on the tiebreak (verified). A `name` consisting only of invisible characters is a present, winning value and reaches the exported CSV.
- **Blast radius note:** a BOM-prefixed `category` is caught upstream — `is_business_category("\ufeffrestaurant")` → `False` (verified) — so it never reaches dedup. `name`, `address`, `website`, `email` and the raw OSM `phone` tag (osm.py:89) are not whitelist-checked and are exposed. This project has already been bitten once by a stray BOM in the master CSV (main.py:82-84), which is why `utf-8-sig` is used there; a BOM *inside* a cell still survives.
- **Fix:** in `_is_missing`, strip `unicodedata.category(c) == "Cf"` (all format/control-invisible characters, which covers `\u200b`, `\u200c`, `\ufeff`) in addition to `.strip()`, and treat numeric `0` as missing for the fields where zero is a sentinel (`rating`, `review_count`, `lat`, `lon`).

---

### S2 — the missing-value shortcut runs *before* the volatility rule, so decaying observations never decay

- **Where:** `dedup.py:168-171` returns early on a missing side, so the `_VOLATILE`/`_recency` logic at dedup.py:183 is never reached for that field.
- **Breaks:** `_pick` conflates *"this source has nothing to say"* with *"keep the older observation."* For accumulating fields (website, email, social) that is the right call and is clearly deliberate. For `rating` and `review_count` it means a business's reputation in the master CSV is **permanent**.
- **Trigger (verified):** master row `rating=2.0, review_count=10, scraped_at=2026-06-01` merged with fresh `rating=None, review_count=None, scraped_at=2026-10-01` → merged `rating=2.0`, `review_count=10`. The four-month-newer observation is discarded and `lead_score` books +10 pain points off the two-year-old rating (verified `lead_score` = 45). `dedup` always merges as `_merge(indexed_so_far, new_record)` (dedup.py:208, 216), so this is the only direction that occurs in practice.
- **Fix:** after the `_is_missing` shortcuts, add a `_VOLATILE` early-out — if `field in _VOLATILE` and `a` is missing but `b` is not, return `b` regardless of which side is newer. Accumulating fields keep the current behaviour.

---

### S3 — `_merge` raises `TypeError`/`AttributeError` on anything that is not a `dict`

- **Where:** `dedup.py:192` `set(a) | set(b)`; `dedup.py:167` `a.get(field)`.
- **Breaks:** `None` → `TypeError: 'NoneType' object is not iterable`; a list of pairs or a string → `AttributeError: '…' object has no attribute 'get'`; a `dict` subclass whose `get` is shadowed → `TypeError: 'NoneType' object is not callable`. A value whose `__str__` raises propagates that exception straight out of dedup.py:184 (verified `RuntimeError`). None of this is wrapped.
- **Not reachable:** the annotation is `a: dict, b: dict` (dedup.py:190), every scraper yields a `BusinessRecord` (base.py:5-28), and a non-dict record already dies at main.py:172 (`r.get("category")`) before `dedup` is reached.
- **Fix:** `if not isinstance(a, dict) or not isinstance(b, dict): raise TypeError(f"_merge expects dicts, got {type(a).__name__}/{type(b).__name__}")`. One line; it buys a real traceback instead of a confusing one.

---

### S3 — `_pick` returns `b`'s value when *both* sides are missing, and turns `""` into `None`

- **Where:** `dedup.py:168-171`. `if _is_missing(av): return bv` — with no check that `bv` is present.
- **Breaks:** verified `_merge({"e": ""}, {"e": None})["e"]` is `None` while `_merge({"e": None}, {"e": ""})["e"]` is `""`. So the merged record's **type** for a field depends on argument order whenever exactly one side is a blank string. Benign today (`write_csv` renders both as an empty cell), but it is the one reachable way `_merge(a,b) != _merge(b,a)`, and it will bite the moment anything downstream does `isinstance` or `float()` on the result.
- **Fix:** `return av if not _is_missing(bv) else bv` — first present wins, else `bv`, so the empty string stays an empty string.

---

## Not a bug, but worth knowing

- **No ReDoS.** `_EMAIL_OK` and `_URL_OK` (dedup.py:116-117) are `^`-anchored with single character-class quantifiers and no nesting, so they are linear. Measured on 5 MB inputs: `_validity("email", 5MB no-@)` 80 ms, `_validity("email", 5MB+@x.com)` 50 ms, `_validity("website", 5MB)` 90–180 ms, `_merge` with a 5 MB value **0.1 ms**. The slow one in this module is `normalize_phone`'s `re.sub(r"\D", "", raw)` at 830 ms for 5 MB (dedup.py:44) — also linear, and not in `_merge`.
- **Long strings still beat short ones on the tiebreak.** Verified: a 5 MB `address` beats `"Beirut"`. No crash, but if a single junk record with a pathological value ever shares a dedup key with a real one, it wins.
- **`_merge` is idempotent and effectively commutative.** Verified `_merge(x, dict(x)) == x`. `_merge(a,b) != _merge(b,a)` only when `str(av) == str(bv)` but `av != bv` (verified `33.9` vs `'33.9'` — a float/str split). Because `main.py:162` collects futures in `as_completed` order, the input order genuinely does vary run to run; what saves it is that the rank tuple's last element is `str(value)`, not the value. **Keep that property if you touch `rank`.**
- **The union of keys is preserved** (verified) — correct, and moot in practice: `write_csv` uses `fieldnames=FIELDS, extrasaction="ignore"` (main.py:125), so any key outside the 22 columns never reaches the product.
- **`_pick("source")` is the most robust branch in the module** (dedup.py:173-174). `_sources_of` (dedup.py:143-145) does `str(record.get("source") or "")` and filters empties, so a `None` or numeric source is dropped instead of crashing, and the `sorted()` join makes it stable across re-merges. This is what keeps `lead_score`'s multi-source bonus (enricher.py:316) correct — it correctly produced `"google_places|osm"` in testing.
- **`normalize_phone` is fully guarded** (dedup.py:33-63): verified `None`/`""`/`0`/`3.5`/`{}`/`nan` → `""`, `int` and `list` accepted via `str()` at dedup.py:42. The **key** side of dedup is robust; all the phone damage in this report is on the **value** side (`_pick("phone")`).
- **`_URL_OK` is doing its job.** Verified validity 0 for `"javascript:alert(1)"`, `"ftp://x.com"`, `"http://"`, `"https://x"` and `"https://localhost:8000"`. It does accept `"https://x.com/a b"` and `"https://x.com\n"` with validity 3 because `match` plus `^`/`$` does not require a full match — harmless, since `.strip()` already ran and the field only affects ranking.
- **`_validity("rating", nan) == 0`** — the `0 <= v <= 5` range check at dedup.py:139 rejects NaN correctly. `_validity("lat"/"lon", nan)` is **3**. That asymmetry is the bug in the S1 above, not a separate one.
- **Unicode handling: the merge does no normalisation at all**, even though `normalize_name` (dedup.py:66-69) does NFKD. Consequences are arbitrary rather than harmful — Arabic beats Latin, emoji beats ASCII, NFC beats NFD — because the only decision being made is which of two spellings to keep. The one case that *is* harmful is the invisible-character case in the S2 above.

---

## Recommended order of work

1. **Make `lat`/`lon` atomic and range-checked** (S1). Single-record provenance for the pair; `math.isfinite` + bounds in `_validity`. Without this the master CSV accumulates impossible coordinates that no later stage can detect.
2. **Close `max()` on `scraped_at`** (S2). `max(av, bv, key=_parse_iso)` with a never-raising parser. Cheap, and it removes the only unguarded comparison in `_pick` before anything starts writing `scraped_at` in a second format.
3. **Give `country` and `region` a real trust model, or drop `country` from `_merge`** (S2). `"SA" > "LB"` is not a country policy.
4. **Let `_VOLATILE` override the missing-value shortcut** (S2) and make `0` missing for `rating`/`review_count`/`lat`/`lon` (S2). Together these stop the master CSV from freezing reputation.
5. **Strip `Cf`-category characters in `_is_missing`** (S2) and `NFC`-normalise in `_pick`.
6. **Boundary guards and tests** (S3): an `isinstance` check in `_merge`, and the `""`/`None` order-dependence fix at dedup.py:168-171.
7. **Property tests worth writing** once 1–3 land:** for every field, `_merge(a, b) == _merge(b, a)`; for every merged record, `lat`/`lon` must be a pair that appeared together in some input; `infer_region` on every output record must not be `None` when a plausible input coordinate existed.