# 337 — `_pick` (dedup.py:159) edge / hostile-input audit

## Verdict

`_pick` is a clean, deterministic, order-independent tiebreaker — and it handles
`None`, `""`, whitespace, `0`, `False`, emoji, Arabic, French, and 5 MB strings
correctly. It crashes on exactly **one** input class (a non-`str` `scraped_at`,
`dedup.py:177`) and silently returns wrong values on **three**: fields absent from
`_SOURCE_TRUST` are decided by code-point order (`country` → always `"SA"`),
`_trust_for` outranks `_validity` in the rank tuple so high-trust junk permanently
beats low-trust valid data, and `_is_missing` is used only as a *comparison*
predicate while `_pick` returns the *raw* value, so `"  "` survives as a truthy
"has a website". The two most dangerous are silent and both **compound**, because
`_merge` mutates the surviving record in place (`dedup.py:208`) and the poisoned
row is written to `data/all_businesses.csv`, which is the input to the next run.

Note for the implementer: `_pick` is **new code**. Findings 002/024/057/090/095 all
describe a `_field_count`-based `_merge` at `dedup.py:45-61` that no longer exists.
Their line numbers are dead; the analysis below is against the current file.

---

## The single most dangerous input

**A `scraped_at` value that is not a `str` — e.g. an epoch `int` — on either side of
a merge.**

It is the only input in the whole enumerated set that turns a run into a traceback:

```
_pick("scraped_at", {"scraped_at": "2026-10-03T00:00:00Z"}, {"scraped_at": 1750000000})
-> TypeError: '>' not supported between instances of 'int' and 'str'      # dedup.py:177
```

Why this one and not the silent ones:

1. **It is an inconsistency, not a design choice.** The two helpers `_pick` calls to
   *read* record metadata are both type-hardened — `_recency` wraps in
   `str(... or "")` (`dedup.py:156`), `_sources_of` wraps in `str(... or "")`
   (`dedup.py:144`). Only `max(av, bv)` at `dedup.py:177` touches the raw values.
   The author already assumed these fields are untrusted; one line was missed.
2. **It is the innermost shared primitive.** `_pick` is called from exactly one
   place (`dedup.py:193`), which is called from two places (`dedup.py:208`,
   `dedup.py:216`), both inside `dedup()` at `main.py:180`. The raise therefore
   happens after the entire 3-scraper parallel phase (Overpass POST + 68 Google
   queries + 5 000 Wikidata rows) has been paid for, and *before* any
   `write_csv` (`main.py:209-213`) — so the run produces **zero** output files and
   the master is never updated. Every other hostile input in this report degrades
   the data; this one deletes the run.
3. **It is one in-flight refactor away from live.** `dedup.py`'s callers are being
   retyped: `docs/audits/045-scraper-layer-refactor.md:159` proposes
   `scraped_at: datetime.datetime = Field(...)`. That variant already raises
   `TypeError: '>' not supported between instances of 'str' and
   'datetime.datetime'` (verified). Today it is latent only because
   `scrapers/base.py:27` pins `scraped_at: str` and `httpclient.py:220-229`
   returns `datetime.now(timezone.utc).isoformat()`. Both are conventions, not
   enforced by the type checker (`BusinessRecord` is a `TypedDict`, never
   validated at runtime).
4. **`dedup.py:177` is also the only spot in `_pick` with no `try`.** Everything
   else — `_validity` (129-139), `float()` on `lat`/`lon`/`rating` — already
   catches `(TypeError, ValueError)`.

Runner-up, and the worst thing that happens *without* a traceback: a `country`
conflict between `"LB"` and `"SA"` (S1-1 below). It pins the wrong country
permanently and is not repairable downstream.

---

## Hostile-input enumeration

`crash` = raises; `wrong` = returns a silently incorrect value; `ok` = correctly
handled. All rows verified by executing `dedup._pick` / `dedup._merge` directly
(no network, no scrapers, nothing installed).

| # | Input | Class | Evidence |
|---|---|---|---|
| 1 | `None` / key absent | ok | `dedup.py:168` → returns `bv` |
| 2 | `""` | ok | `_is_missing("")` → `True` (`dedup.py:89`) |
| 3 | `" "`, `"\t"`, `"\n"` | **wrong** | treated missing for comparison but **returned raw** → S1-3 |
| 4 | `"\xa0"` NBSP, `"\u3000"` ideographic space, `"\u2028"` | ok (as missing) | `str.strip()` is Unicode-aware; all → `True` |
| 5 | `"\ufeff"` BOM-only | ok | `"\ufeff".strip()` is truthy → treated as a real value (see S3-2) |
| 6 | `0`, `0.0`, `False` | ok | `_is_missing` returns `False` for all non-`str` (`dedup.py:90`) — the fix for the old `is None` bug holds; `rating=0` and `lat=0.0` survive |
| 7 | `"0"` | ok | not blank |
| 8 | `scraped_at` = epoch `int` vs `str` | **crash** | `TypeError` at `dedup.py:177` |
| 9 | `scraped_at` = `datetime` vs `str` | **crash** | `TypeError` at `dedup.py:177` |
| 10 | `scraped_at` both `int` | ok | `max()` returns `1750000001`; then leaks an `int` into the CSV |
| 11 | `scraped_at` both `str` but different precision/offset | **wrong** | lexical `max()` — S3-2 |
| 12 | `scraped_at` `"  "` vs real | ok | miss short-circuit |
| 13 | `email` = `["a@b.com"]` (list) | **wrong** | `_validity` → `3`; `str()` coercion of a repr passes `_EMAIL_OK` (`dedup.py:125`) — S3-1 |
| 14 | `email` = `{"k": "a@b.com"}` / `website` = tuple | ok | scores `0`, no crash |
| 15 | `lat`/`lon` = `True` | **wrong** (harmless) | `float(True)==1.0` → validity `3` |
| 16 | `rating` = `"nan"` / `float("nan")` / `99` / `"4.9"` | ok | `0, 0, 0, 3` — nan/inf correctly rejected by the `0<=v<=5` guard (`dedup.py:139`) |
| 17 | `website` = `"  https://x.example  "` | ok | `_validity` strips before matching (`dedup.py:127`) |
| 18 | `review_count` 9 vs 10, same source, same run | **wrong** | returns `9` — `str()` tiebreak, S2-1 |
| 19 | `lead_score` 9 vs 100 | **wrong** | returns `9` — S2-1 |
| 20 | `completeness_score` 0 vs 7 | ok | `0` wins the string compare by accident (`"0"` < `"7"`), see S2-1 |
| 21 | `country` `"LB"` vs `"SA"` | **wrong** | returns `"SA"` always — S1-1 |
| 22 | `region` `"Beirut"` vs `"Mount Lebanon"` | **wrong** | returns `"Mount Lebanon"` — S1-1 |
| 23 | `region` `"South Lebanon"` vs `"North Lebanon"` | **wrong** | returns `"South Lebanon"` — S1-1 |
| 24 | junk email on a trust-3 record vs valid email on a trust-2 record | **wrong** | returns the junk — S1-2 |
| 25 | master `source="google_places|osm"` vs fresh `wikidata` | **wrong** | returns master's older value — S1-2 (laundering) |
| 26 | `phone` `"٠٩٦١٧٦٨٩٠٠٠"` vs `"+96176890000"` | **wrong** | returns the Arabic-Indic string — S2-2 |
| 27 | `name` `"Café Deux Rives"` vs `"Zeta"` | wrong but harmless | `"Zeta"` — accent collation is code-point order |
| 28 | `address` two Arabic strings | wrong but harmless | code-point order, not Arabic collation |
| 29 | `email` `"🍔@x.example"` | ok | no crash; `_EMAIL_OK` accepts it (validity `3`) — noted, not fixed here |
| 30 | `name` `"🍔"` | ok | survives `_pick` correctly |
| 31 | 2 × 100 000-char `website` | ok | 1.92 ms; no truncation, no blowup |
| 32 | 5 MB `name` | ok | returned intact, no recursion/slice |
| 33 | ragged CSV row → `row[None] = [extras]` | **wrong** | `field=None` reaches `_pick`; a `None` key holding a list enters the merged dict — S3-1 |
| 34 | `source` = `["osm","google_places"]` (list) | **wrong** | `_merge` emits `"['osm', 'google_places']|wikidata"` — S3-1 |
| 35 | `website` `"   "` vs `"  "` (both blank) | **wrong** | returns `"  "`, whitespace survives — S1-3 |
| 36 | `website` `"   "` (high trust) vs `"N/A"` (low trust) | **wrong** | returns `"N/A"` — the miss short-circuit bypasses `_validity` entirely (this half is correct behaviour: fill-with-something beats nothing) |
| 37 | `website_live` `False` vs `True` | ok | `False` survives `_is_missing`; `True` wins |
| 38 | `website_live` `"False"` vs `True` | ok | `True` wins (`str("True") > str("False")`) |
| 39 | order swapped on any of the above | ok | `_merge` is order-independent — verified on 15/17, 18/19, 21, 24, 25 |

Nothing above is a DoS, a regex-backtrack, or an unbounded-allocation problem.
`_pick` has no regex, no recursion, no I/O, and no unbounded growth.

---

## Findings

### S1 — `country` and `region` have identical trust for every source and are non-volatile, so ASCII code-point order decides them — and `"SA"` always beats `"LB"`

- **Where:** `dedup.py:181-184`, with `_SOURCE_TRUST` (`dedup.py:97-110`) and
  `_VOLATILE` (`dedup.py:114`).
- **Breaks:** `country` and `region` appear in **no** `_SOURCE_TRUST` entry, so
  `_trust_for` returns `_DEFAULT_TRUST = 1` for every record regardless of source
  (verified). `_validity` falls through to `return 1` (`dedup.py:140`) because
  neither field is in the email/website/lat/lon/rating special cases. Neither is in
  `_VOLATILE`, so line 183 substitutes `""` for recency. That leaves
  `str(value)` (line 184) as the sole discriminator — a Unicode code-point compare.
  `"SA" > "LB"` and `"Riyadh" > "Beirut"`, so **Saudi attribution wins every tie**,
  with zero evidence involved.
- **Trigger:** master row (written by a previous run, `country` already resolved to
  `"SA"` at `main.py:184`) merged with a fresh `google_places` record for the same
  place returned this run by a different query with `_country_from_query ==
  "LB"` (`google_places.py:127-136`):

  ```
  master = {country:"SA", region:"Riyadh", address:"King Fahd Rd, Riyadh",
            lat:24.7136, lon:46.6753, phone:"+966112345678",
            source:"google_places|osm", scraped_at:"2026-09-01T08:00:00.123456+00:00"}
  fresh  = {country:"LB", region:"Beirut",  address:"Hamra, Beirut",
            lat:33.8938, lon:35.5018, phone:"+9611123456",
            source:"google_places",        scraped_at:"2026-10-03T08:00:00.123456+00:00"}

  _merge(master, fresh) ->
    country 'SA'   region 'Riyadh'   address 'King Fahd Rd, Riyadh'
    lat/lon  33.8938, 46.6753        <- Beirut coords, Riyadh everything else
    phone    '+966112345678'         phone normalized from the master's own value
  ```

  The merged row is internally contradictory: Beirut coordinates with a Riyadh
  address, region, and country. `_merge(fresh, master)` returns the same thing, so
  this is not order-dependent — it is deterministic and always wrong.
- **Why it compounds:** `resolve_country` cannot repair it — `country="SA"`
  short-circuits at `main.py:61-62` before the phone is ever inspected, so the
  `+961` evidence at `main.py:72-73` is dead code for this row. `enricher.enrich`
  cannot repair `region` either: `if not r.get("region")` (`enricher.py:331`)
  sees a truthy `"Riyadh"` and skips `infer_region`. And because `_pick` again
  prefers `"SA"` next run, the row is written back to `all_businesses.csv`
  (`main.py:209`) and re-poisons every future merge. The same tiebreak then hits
  `lat`/`lon`, where the master's Google-trust 3 ties the fresh record's 3 and
  `"33.8938" > "24.7136"` picks the Beirut pin for a Riyadh-address row.
- **Also hit:** the shared-hotline chain collapse already documented at
  `docs/audits/057-chain-detection.md:14-15`. A Beirut branch and a Riyadh branch
  merging on one chain phone now additionally inherit each other's country by
  alphabet, not by evidence.
- **Fix:** `country` needs real evidence, not a string compare — resolve it from
  `normalize_phone`'s detected calling code (`dedup.py:58-60`), which is the only
  field in the record that actually knows the country. Add `region` to
  `_SOURCE_TRUST` (or make `infer_region` authoritative and run it *after* the
  country is settled at `main.py:184`). As a minimum, add an explicit branch in
  `_pick` next to `scraped_at` so the choice is deliberate instead of accidental.

### S1 — `_trust_for` outranks `_validity` in the rank tuple, and `_pick` launders the source union into every field's trust — so junk from a high-trust source permanently beats valid data from a lower-trust source

- **Where:** `dedup.py:181-182` (tuple order), `dedup.py:173-174` (writes the
  union), `dedup.py:148-152` (`_trust_for` takes `max` over the source set).
- **Breaks:** the rank tuple is compared lexicographically and `_trust_for` is
  element 0, so a trust gap of 1 makes element 1 (`_validity`) irrelevant. Higher
  trust therefore *strictly dominates* validity: an unparseable `"n/a"` from an
  OSM record (trust 3) beats a perfect `clean@example.com` from a Wikidata record
  (trust 2). Verified:

  ```
  _pick("email", {"email":"n/a",          "source":"osm"},
                {"email":"clean@example.com","source":"wikidata"})
    -> 'n/a'          # trust 3 > 2, validity 0 < 3 never consulted
  ```

  Worse, line 174 manufactures `"google_places|osm"`, and line 181 feeds that
  string straight back into `_trust_for`, which takes the **`max`** over all
  sources (`dedup.py:151`). A record's trust for *every* field is therefore
  upgraded to the best source that ever contributed to *any* field:

  ```
  master (source "google_places|osm", value actually from osm) : _trust_for("address") = 3
  fresh  (source "wikidata")                                    : _trust_for("address") = 2
  _pick("address", master, fresh) -> 'OLD osm address text'   # in BOTH argument orders
  ```

  The master claims Google-grade authority over an address Google never supplied,
  and beats every future Wikidata address forever. The ratchet saturates at 3
  after the first merge (verified over 4 sequential merges) and **never decays**,
  because `all_businesses.csv` is the next run's input.
- **Why it is silent data loss, not just a bad pick:** `_merge` mutates the
  surviving record in place (`dedup.py:208`, `dedup.py:216`), so the losing value
  is not deferred — it is gone from the only record that will ever be written.
  There is no per-field provenance to recover it (`docs/audits/095-dedup-strategy-v2.md:17`).
- **Trigger:** the table above; no exotic input required, just a business that two
  sources both saw and one of which got a junk value.
- **Fix:** reorder the tuple so validity outranks trust —
  `(validity, trust, recency, type-stable key)` — or, better, make `_validity`
  a hard gate (`if _validity(...) == 0` never beats a value with `> 0`) and keep
  trust only as a tiebreak among equally-valid values. Separately, pass the
  *originating* source to `rank()` instead of the merged record, so the union
  string stops inflating trust.

### S1 — `_is_missing` is a comparison predicate, but `_pick` returns the raw value, so blank-but-truthy strings survive into the CSVs and are misfiled as "has a website"

- **Where:** `dedup.py:168-171` (the short-circuit returns `bv`/`av`
  unfiltered), `dedup.py:89` (`not value.strip()`).
- **Breaks:** `_is_missing("  ")` is `True`, so `"  "` is correctly ignored *when
  comparing* — but line 169 then `return bv`, handing the caller the untouched
  whitespace string. When **both** sides are blank, the function returns one of
  the two blank strings verbatim:

  ```
  _pick("website", {"website": "  "}, {"website": "  "})      -> '  '
  _pick("website", {"website": "  "}, {"website": "\t\n"})    -> '\t\n'
  ```

  Downstream, nothing strips it. `enricher.py:231` and `main.py:199` both test
  bare truthiness, and `"  "` is truthy:

  ```
  bool("  ")                              -> True
  main.py:199  r.get("website")           -> row goes to with_websites.csv
  main.py:200  not r.get("website")       -> so it CANNOT go to without_websites.csv
  completeness_score({... "website": "  "}) -> 1   (enricher.py:107)
  main.py:206  completeness_score >= 1    -> row qualifies
  ```

  A business with **no website** is exported into `with_websites.csv` — the file
  whose entire purpose is "sites that need building" — and is pushed over the
  `completeness_score >= 1` bar into `qualified_businesses.csv` by a value that
  carries no information. `check_websites` then spends a worker on
  `_fetch_website("  ")` (`enricher.py:231`), which raises `MissingSchema` inside
  `requests`, is swallowed by the bare `except Exception` at `enricher.py:176`,
  and records `website_live = None` — indistinguishable from a real unreachable
  site.
- **Trigger:** `data/all_businesses.csv` is synced to and from Google Drive by
  rclone (`BRIEF.md:37`) and is routinely opened in Sheets. A stray space or tab
  in the `website` cell survives `load_master`, which only nulls the **exactly
  empty** string (`main.py:88` `if row[k] == "": row[k] = None`). Note OSM's own
  `website` path is mostly immune (`osm.py:95-96` prefixes `https://` to any
  non-empty tag), so this arrives from human-edited master rows and from any new
  scraper.
- **Fix:** at the top of `_pick`, normalise the returned value —
  `return None if _is_missing(av) else av` — or coerce blank strings to `None` at
  the single load boundary in `main.py:88` (`if not (row[k] or "").strip()`).
  Cheapest correct place is `load_master`, but `_pick` should not be the thing
  that lets a blank through either.

### S1 — non-`str` `scraped_at` raises `TypeError` at `dedup.py:177` and kills the run after the entire scrape phase

- **Where:** `dedup.py:176-177`.
- **Breaks:** `max(av, bv)` compares the raw values with no coercion, unlike
  `_recency` (`dedup.py:156`) and `_sources_of` (`dedup.py:144`), which both wrap
  in `str()`. Unhandled → propagates through `_merge` → `dedup()` →
  `main.py:180`, which has no `try`. The run dies **after** the Overpass POST, the
  68 Google queries and the 5 000-row Wikidata pull, and **before** any
  `write_csv`, so `all_businesses.csv` is never refreshed and the Drive upload
  never runs.
- **Trigger:** any one of these, all verified to raise:

  ```python
  _pick("scraped_at", {"scraped_at": "2026-10-03T00:00:00Z"}, {"scraped_at": 1750000000})
  # TypeError: '>' not supported between instances of 'int' and 'str'
  _pick("scraped_at", {"scraped_at": 1750000000}, {"scraped_at": "2026-10-03T00:00:00Z"})
  # TypeError: '>' not supported between instances of 'str' and 'int'
  _pick("scraped_at", {"scraped_at": datetime.datetime(2026,10,3)}, {"scraped_at": "2026-10-03T00:00:00+00:00"})
  # TypeError: '>' not supported between instances of 'str' and 'datetime.datetime'
  ```

  **Reachability today: latent.** `scrapers/base.py:27` pins `scraped_at: str`,
  `httpclient.py:220-229` returns `datetime.now(timezone.utc).isoformat()`, and
  every CSV cell read by `load_master` is a `str` (it casts only
  `main.py:43-44`'s float/int fields). Rated S1 anyway because (a) it is a
  run-dies with zero partial output, (b) `docs/audits/045-scraper-layer-refactor.md:159`
  is proposing `scraped_at: datetime.datetime` at exactly these call sites, which
  makes it live, and (c) `docs/audits/020-error-handling-partials.md:27` and
  `docs/audits/031-test-suite-design.md:147` (test
  `test_merge_scraped_at_mixed_type_comparison`) already flag it against the old
  line numbers — the test was written, the code path is still unhardened.
- **Fix:** `return max(str(av), str(bv))` — one token, and it also makes the
  result type match the `BusinessRecord` contract. Parse to `datetime` if the
  format ever drifts (see S3-2).

### S2 — `str(value)` as the final tiebreak is type-blind, so integers compare lexicographically

- **Where:** `dedup.py:184`.
- **Breaks:** the comment says "deterministic final tiebreak", which it is — it is
  just not a *correct* one for numbers. For most fields it never fires (same
  source → same trust; `_validity` returns the constant `1` at `dedup.py:140`;
  non-volatile → recency is `""`), so whenever it *does* fire it is the sole
  decision. Verified:

  ```
  _pick("review_count", {9, "google_places","T"}, {10,"google_places","T"})  -> 9
  _pick("review_count", {99,...}, {100,...})                                -> 99
  _pick("lead_score",  {9}, {10})   -> 9     # 10 silently downgraded to 9
  _pick("lead_score",  {9}, {100})  -> 9     # 100 silently downgraded to 9
  ```

  `str(9) > str(10)` because `'9' > '1'`. The inversion repeats at every power of
  ten.
- **Reachability today: latent, and narrowly so.** Every scraper stamps one
  `scraped_at` per run (`osm.py:32`, `google_places.py:173`, `wikidata.py:67`) and
  Google dedups by `place_id` within a run (`google_places.py:264`), so two
  `google_places` records for one place cannot coexist in a single batch. And
  `completeness_score`/`lead_score` are `0` in every scraper record
  (`osm.py:118,121`; `google_places.py:301,304`), so the only live comparison is
  `0` vs a real score — and `"0"` is the lowest digit character, so the master
  value wins **by accident**. `completeness_score` also caps at 7
  (`enricher.py:101-117`), so it can never cross a power of ten.
  This becomes live the moment `dedup` sees two *enriched* records — precisely
  what `docs/audits/037-cli-design.md:508` proposes
  (`leadminer dedup data/sa_raw.csv -m data/all_businesses.csv -o ...`).
- **Fix:** make the tiebreak type-aware and exclude numerics from string
  comparison — `str(value)` only for `str` values, otherwise the raw value, and
  for the known numeric fields (`lat`, `lon`, `rating`, `review_count`,
  `lead_score`, `completeness_score`) use `max()`.

### S2 — non-ASCII values win every `str()` tie, and Arabic-Indic digits produce undialable phone numbers

- **Where:** `dedup.py:184`, interacting with `dedup.py:44` and `dedup.py:63`.
- **Breaks:** Python compares `str` by code point, and every Arabic-Indic digit
  (U+0660-0669) is numerically above ASCII. So on a tie, an Arabic-Indic value
  beats *any* ASCII value:

  ```
  _pick("phone", {"phone": "٠٩٦١٧٦٨٩٠٠٠", "source": "osm"},
                {"phone": "+96176890000",  "source": "osm"})  -> '٠٩٦١٧٦٨٩٠٠٠'
  ```

  Worse, `re.sub(r"\D", "", raw)` at `dedup.py:44` is Unicode-aware, so
  Arabic-Indic digits are *kept* as "digits", and `digits.lstrip("0")` at
  `dedup.py:63` strips only ASCII `0`:

  ```
  normalize_phone("٠٩٦١٧٦٨٩٠٠٠")   -> '+961٠٩٦١٧٦٨٩٠٠٠'   # undialable, and a unique
  normalize_phone("+96176890000")  -> '+96176890000'        # dedup key, so it never
                                                           # matches the ASCII record
  ```

  `main.py:194` then writes the survivor into `phone`, so `sales_ready.csv` — the
  actionable file — ships a phone number no human can dial, on a record that
  passed `has_any_contact` (`main.py:137-145`, which also only counts `\D`).
- **Trigger:** OSM `phone`/`contact:phone` tags or a Sheets-edited master cell
  containing Arabic-Indic digits. Both records must reach the same merge bucket,
  which happens naturally on the `(normalize_name, city)` name key
  (`dedup.py:214`).
- **Fix:** transliterate Arabic-Indic (and Eastern Arabic) digits to ASCII in
  `normalize_phone` before the `\D` strip, and normalise `NFKC` there. In
  `_pick`, add `unicodedata.normalize("NFKC", str(value))` to the tiebreak so
  equivalent spellings compare equal.

### S3 — a ragged CSV row injects a `None` field key and a list value into the merged record

- **Where:** `dedup.py:192-193` (`for key in set(a) | set(b)`), reached from
  `main.py:86` (`csv.DictReader` with the default `restkey=None`).
- **Breaks:** a row with more cells than headers makes `DictReader` park the
  surplus under the key `None`. `_merge` iterates it, so `_pick` is called with
  `field=None`. No crash — every comparison is type-safe — but the merged dict
  gains a `None` key holding a `list`:

  ```
  _merge({"name":"X","phone":"+9611234567", None:["extra","cells"]}, ok)
    -> {None: ['extra','cells'], 'name':'X', 'source':'osm', ...}
  ```

  `write_csv`'s `extrasaction="ignore"` (`main.py:125`) silently drops it, so the
  damage is contained to a transient dict — but any future consumer that does
  `for k, v in record.items()` (the CSV writers, the SQLite migration in
  `docs/audits/033-storage-sqlite-migration.md:131`, an API layer) inherits it.
- **Trigger:** any hand-edited or externally-synced `all_businesses.csv` row with
  one extra comma-separated cell. Plausible given the rclone↔Drive workflow.
- **Fix:** in `load_master`, reject or truncate rows where `None in row`; or in
  `_merge`, skip non-`str` keys: `for key in set(a) | set(b): if not isinstance(key, str): continue`.

### S3 — two `str()` coercions at the boundary turn arbitrary objects into plausible-looking scores and strings

- **Where:** `dedup.py:125` and `dedup.py:127` (`str(value)` before regex match),
  `dedup.py:144` (`str(record.get("source") or "")`).
- **Breaks:** `_validity` scores the *repr* of an arbitrary object. A list scores
  as a valid email:

  ```
  _validity("email", ["a@b.com"])  -> 3     # str() gives "['a@b.com']", which matches _EMAIL_OK
  _validity("email", {"k":"a@b.com"}) -> 0  # dict repr has quotes/spaces, fails
  ```

  A non-`str` `source` is stringified into the CSV column and then re-split on
  `"|"` forever:

  ```
  _merge({"source": ["osm","google_places"]}, {"source":"wikidata"})
    -> source = "['osm', 'google_places']|wikidata"
  ```

  That garbage token never matches a key in `_SOURCE_TRUST`, so the record
  silently drops to `_DEFAULT_TRUST` for every field — while still looking
  populated to `is_business_category` and the CSV filters.
- **Fix:** in `_validity`, `if not isinstance(value, str): return 0` before the
  regex branches; in `_sources_of`, the same, or `return set()` for non-str.

### S3 — `scraped_at` is compared as a raw string, so format drift silently reorders time

- **Where:** `dedup.py:177`.
- **Breaks:** `max()` over ISO strings is only correct if every writer uses the
  same format. `utc_now_iso` (`httpclient.py:229`) currently emits
  `...isoformat()`, i.e. `+00:00`. Verified misorderings:

  ```
  max('2026-10-03T00:00:00Z','2026-10-03T00:00:00+00:00')   -> '...Z'   # 'Z'(0x5A) > '+'(0x2B)
  max('2026-10-03T02:00:00+03:00','2026-10-02T23:30:00Z')   -> the +03:00 one,
        which is 23:30Z — 30 minutes OLDER — and wins anyway
  ```

  So the moment anyone applies the `.replace("+00:00", "Z")` normalisation already
  proposed in `docs/audits/065-python-312-modernisation.md:32`, records written by
  the two formatters interleave incorrectly and a **stale** master row can claim
  to be fresh. That in turn corrupts `_recency` (`dedup.py:156`), which is
  element 2 of the rank tuple for `website`, `rating`, `review_count` and
  `website_live` (`dedup.py:114`) — the only freshness signal `_pick` has.
  Flagged previously against the old line numbers at
  `docs/audits/028-csv-roundtrip-fidelity.md:154` and
  `docs/audits/032-fixtures-offline-replay.md:63`; the mechanism is unchanged.
- **Fix:** `max(av, bv, key=_parse_iso)` with a `try/except` fallback to the raw
  string, plus a one-line assertion in `utc_now_iso` that the format is fixed.

---

## Not a bug, but worth knowing

- **The `is None` → `_is_missing` fix works and is the strongest part of this
  rewrite.** `0`, `0.0`, `False`, `[]`, `{}` all correctly return `False`
  (`dedup.py:90`), so `rating=0.0` and a legitimately-zero `lat` are not
  mistaken for absence. `_pick("rating", {"rating":0}, {"rating":4.9}) -> 4.9`
  and `_pick("lat", {"lat":0.0}, {"lat":33.89}) -> 33.89` both behave.
  `website_live=False` also survives, which matters because `enricher.py:250`
  encodes UNKNOWN as `None` and DEAD as `False`.
- **Zero is treated as a real value, not a sentinel — including in `rating`.**
  `_validity` rejects out-of-range values (`dedup.py:139`), and correctly rejects
  `nan`, `inf`, and `99`: `_validity("rating", "nan") -> 0`.
- **No denial-of-service surface.** No regex, no recursion, no I/O, no unbounded
  allocation. Two 100 000-char websites compare in 1.92 ms; a 5 MB name is
  returned intact in one pass. There is no length cap anywhere in `_pick`, but
  there is also nothing for a long string to trigger.
- **Unicode is handled without erroring everywhere.** Emoji, Arabic script,
  French accents, NBSP (U+00A0), ideographic space (U+3000) and U+2028 all pass
  through without a traceback. `_is_missing` gets the whitespace set right
  (`.strip()` is Unicode-aware). The problems with non-ASCII are *ordering*
  (S1-1, S2-2), never crashes.
- **`_merge` is genuinely order-independent**, which is the property the old
  `_field_count` rule lacked. Verified by swapping arguments on every wrong-value
  finding above: `country`/`region`/`address`, the 9-vs-10 `review_count`, the
  trust-laundered `address`, and the junk-vs-valid `email` all return the same
  answer both ways. That makes the bugs *reproducible* — which is exactly why they
  will persist in the master indefinitely.
- **`_pick` correctly handles both-missing** by returning `bv` (`None`), which
  `_merge` then writes as an explicit `None`. `_merge` materialises the union of
  keys (`dedup.py:192`), so merged records always carry the full 23-key shape —
  that is intentional and correct.
- **`_validity` strips before matching** (`dedup.py:125,127`), so
  `"  https://x.example  "` correctly scores `3`.
- **`docs/audits/002`, `024`, `057`, `090`, `095` describe a `_field_count`-based
  `_merge` at `dedup.py:45-61` that no longer exists.** Their conclusions about
  order-dependence and whole-record field counting are resolved; their line
  numbers are dead. Do not re-fix them. The stale-line problem is itself the
  argument for the machine-checked invariants in
  `docs/audits/031-test-suite-design.md:147`.

---

## Recommended order of work

1. **`scraped.py:177` → `max(str(av), str(bv))`.** One token. Removes the only
   traceback in `_pick` and stops `docs/audits/045`'s proposed
   `datetime.datetime` model from killing every run.
2. **Give `country`/`region` real evidence, not a string compare (S1-1).** Derive
   `country` from `normalize_phone`'s detected calling code; move `region`
   inference after `main.py:184` and stop it being a merge output. Until then,
   every cross-border row in the master is wrong and will stay wrong.
3. **Flip the rank tuple so `_validity` gates `_trust_for`, and stop feeding the
   merged `source` union back into trust (S1-2).** This is the difference between
   "a bad tiebreak" and "data that is destroyed and unrecoverable".
4. **Coerce blank strings to `None` at the single load boundary, `main.py:88`,
   and have `_pick` return `None` rather than a blank (S1-3).** Otherwise
   `with_websites.csv` and `qualified_businesses.csv` — the two CSVs that define
   the pitch — both contain phantom websites.
5. **Type-aware tiebreak** (S2-1) and **`NFKC` + Arabic-Indic digit folding in
   `normalize_phone`** (S2-2). Both are needed the moment `dedup` gains the CLI
   proposed in `docs/audits/037-cli-design.md:508`.
6. **Harden the remaining boundary coercions** (S3-1, S3-2, S3-3): non-`str`
   `source`, non-`str` in `_validity`, non-`str` `_merge` keys, ISO parsing for
   `scraped_at`.
7. **Write the tests first** — `docs/audits/031-test-suite-design.md:236-238`
   already specifies `test_merge_scraped_at_mixed_type_comparison`. Add four
   more, each one line of assertion, all verified in this report:
   `test_pick_country_prefers_calling_code_not_alphabet`,
   `test_pick_validity_outranks_trust`,
   `test_pick_never_returns_blank_string`,
   `test_pick_numeric_tiebreak_is_numeric`.

**Verification method for this report:** every "→" value above is real output
from `dedup._pick` / `dedup._merge` / `dedup.normalize_phone` executed directly on
CPython 3.14.8 against `/Users/mhomsi/dev/dummy/leadminer/dedup.py` at the line
numbers cited. No scraper was run, no network call was made, no package was
installed, and no repository file other than this report was created or modified.
`enricher.py` could not be imported (`requests` is absent), so
`completeness_score` was re-implemented verbatim from `enricher.py:101-117` for
the S1-3 threshold check.