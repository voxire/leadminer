# 356 — `dedup()` contract: declared `list[dict]`, relied on for an enum no caller guarantees

**Target:** `dedup` at `dedup.py:197`
**Lens:** contract

## Verdict

`dedup` is annotated `list[dict] -> list[dict]` (`dedup.py:197`), so `BusinessRecord`
(`scrapers/base.py:5`) is enforced **nowhere**: not by the signature, not by
`main.load_master` (`main.py:77`, also `-> list[dict]`), not by `main.main` at the
call site (`main.py:180`), and not by CI — `[tool.mypy] strict = true`
(`pyproject.toml:57`) exists but `scrape.yml` installs only `requirements.txt`
(`scrape.yml:39`) and has no test or typecheck step. The gap that actually bites is
not cosmetic: `dedup` builds its **primary key** from `normalize_phone(phone,
country)`, and `country` is typed `str | None` (`base.py:9`) while `normalize_phone`
silently coerces every value it does not recognise to Lebanon (`dedup.py:48`).
`country` on a Google record is a guess derived from the *search query string*
(`google_places.py:127-136`), the request carries no location bias
(`google_places.py:222`), and which query wins a `place_id` is decided by thread
arrival order (`google_places.py:190, 196, 263-266`). Result: the same Saudi
business gets two different dedup keys in two runs, is emitted as two rows, and
because the master is cumulative (`main.py:150, 179, 209`) the duplicate is
**permanent**. Fix the ordering first (`resolve_country` must run *before* `dedup`)
and annotate the boundary.

---

## Findings

### S1 — `country` is a load-bearing half of the primary dedup key, but it is a query-string guess that `resolve_country` only fixes *after* dedup runs

- **Where:** key built at `dedup.py:205` (and again at `dedup.py:228`);
  silent coercion at `dedup.py:48`; the canonicaliser runs too late at
  `main.py:183-184`, after the `dedup` call at `main.py:180`.
- **Declared contract:** `normalize_phone(phone: str, country: str = "LB")`
  (`dedup.py:33`) and `country: str | None` (`base.py:9`). Both read as "any string,
  default Lebanon". The *actual* contract is an unstated two-value enum
  `{"LB", "SA"}` — `_COUNTRY_CODES` (`dedup.py:23-26`) has exactly those keys and
  `dedup.py:48` does `_COUNTRY_CODES.get(country, "961")`, so **every other string
  silently becomes Lebanon**.
- **Breaks:** the country code is *the only* country signal in the key for any
  national-format phone, and Google's phone field is national-format by
  construction — the field mask asks for `places.nationalPhoneNumber`
  (`google_places.py:38`) and maps it straight to `phone` (`google_places.py:289`),
  deliberately *not* `places.internationalPhoneNumber`. So for every Google record
  the key is `country_code + lstrip("0", phone)` (`dedup.py:63`) and a wrong
  `country` yields a wrong key. The same business then lands in two different
  buckets and `dedup` returns it twice.
- **The wrong `country` is not hypothetical — it is nondeterministic by
  construction:**
  - `country` comes from `_country_from_query(query)` (`google_places.py:187, 285`),
    a keyword match on the query text (`google_places.py:127-136`). Verified:
    `_country_from_query("hotels in Lebanon") == "LB"` while
    `_country_from_query("boutique hotels in Saudi Arabia") == "SA"`.
  - The request body is `{"textQuery": q}` only (`google_places.py:222`) — no
    `locationBias`, no `regionCode` — so `textSearch` may return a Jeddah hotel for
    `"hotels in Lebanon"` and a Beirut hotel for a KSA query.
  - The field mask (`google_places.py:32-43`) requests no `addressComponents` and
    no `plusCode`, so the response carries nothing that could correct the guess.
  - When the same `place_id` is returned by two queries, `google_places.py:263-266`
    keeps whichever of the 5 worker threads (`google_places.py:194`) reaches it
    first, under a lock, with `as_completed` yielding in completion order
    (`google_places.py:196`). The `country` label is therefore
    **thread-scheduling dependent**.
  - 33 LB-labelled and 34 SA-labelled queries share 25 category nouns (`hotels`,
    `restaurants`, `dental clinics`, `real estate`, `boutique`, `law firms`, …),
    i.e. **1122 cross-label query pairs** that can each return one place under two
    different `country` labels.
- **Trigger (verified end-to-end, real API shapes, no network used):**
  ```python
  master = {"name": "Al Noor Clinic", "country": "KSA", "phone": "0501234567",
            "address": "Riyadh", "source": "google_places"}
  fresh  = {"name": "Al Noor Clinic", "country": "SA",  "phone": "0501234567",
            "address": "Riyadh, Saudi Arabia", "rating": 4.5, "review_count": 88,
            "source": "google_places"}
  dedup([master, fresh])
  ```
  ```
  master country 'KSA' -> key +961501234567
  fresh  country 'SA'  -> key +966501234567
  dedup -> 2 rows for 1 business:
     {'name': 'Al Noor Clinic', 'country': 'KSA', 'rating': None, 'review_count': None}
     {'name': 'Al Noor Clinic', 'country': 'SA',  'rating': 4.5,  'review_count': 88}
  ```
  Both rows are written to `all_businesses.csv` (`main.py:209`), read back on the
  next run (`main.py:150`), re-merged into `combined` (`main.py:179`) and emitted
  again (`main.py:180`). The split is permanent and grows every week. Every derived
  number is wrong: `qualified_businesses.csv`, `sales_ready.csv` and the summary
  counts at `main.py:220-226`.
- **A same-shape failure with no malformed input at all:** because
  `resolve_country` runs after dedup, a master cell reading `KSA`, `KSA `, `sa`,
  `SAU`, `Jordan` — *all values the repo's own `resolve_country` explicitly
  handles at `main.py:64-67`* — is keyed as Lebanon by `dedup`. `main.py:50-74` is
  written specifically because "`load_master` turns blank cells into `None`, and
  `None` is a *present* key, so the default never fires"; that fix lands at
  `main.py:184`, four lines too late.
- **Explicitly *not* claimed:** a mislabel cannot make two *unrelated* real
  businesses share a key. Verified digit arithmetic: LB national numbers yield
  `+961` + 7–8 digits and SA yield `+966` + 9 digits, and
  `{normalize_phone(n,"LB") for real LB numbers} & {normalize_phone(n,"SA") for
  real SA numbers} == set()`. So the damage is **duplicate rows, not a wrong
  merge**. That is still S1 — silent, permanent corruption of the product — but it
  is a different bug from the one a first reading suggests, and the fix is
  ordering, not collision-proofing.
- **Fix:** move `for r in records: r["country"] = resolve_country(r)` to
  immediately before `main.py:180`, and add a `"LB" | "SA"` alias map plus a
  hard reject (not a silent `961` default) at `dedup.py:48`.

---

### S2 — `dedup` returns records whose `country` was chosen by an alphabetical tiebreak, so its output can be internally inconsistent

- **Where:** `dedup.py:179-187` `rank()`; `_SOURCE_TRUST` (`dedup.py:97-110`)
  has **no `country` entry** (verified), so `_trust_for("country", …)` returns
  `_DEFAULT_TRUST = 1` (`dedup.py:111, 148-152`) for every source; `country` is not
  in `_VOLATILE` (`dedup.py:114`) so recency contributes `""`; `_validity` falls
  through to `1` (`dedup.py:140`). The only discriminator left is
  `str(value)` at `dedup.py:184`.
- **Breaks:** `country` on a merged record is the lexicographically largest code
  any contributor supplied. Verified order-independence and the resulting
  precedence:
  ```
  ('LB','SA')     -> 'SA'      ('LB','KSA')    -> 'LB'
  ('SA','LB')     -> 'SA'      ('KSA','LB')    -> 'LB'
                              ('LB','Jordan')  -> 'LB'
  ```
  So `SA` beating `LB` is a fact about the alphabet, not about trust. Because
  `country` and `phone` are ranked *independently*, the merged record's `phone`
  can come from a source that knew the country and `country` from one that
  guessed.
- **Trigger (verified):** a Beirut clinic seen by OSM (`country="LB"`,
  `osm.py:103`) and by a Google query whose text said Saudi Arabia
  (`country="SA"`), both with `phone="01234567"` and Beirut address/coordinates:
  ```
  merged country = SA   (picked because 'SA' > 'LB' as a string)
  merged phone   = 01234567  -> re-keyed at main.py:194 to +9661234567
  infer_region("Hamra, Beirut", 33.89, 35.50, country='SA') = None
  ```
  `enricher.py:85` skips address matching unless `country == "LB"` and
  `enricher.py:90` then selects the KSA coordinate boxes, so a Beirut business is
  emitted with `country=SA` and a blank `region`, and is miscounted in the
  `By region` summary at `main.py:232-234`. Its phone is also rewritten to a Saudi
  E.164 number at `main.py:194` and persisted.
- **Fix:** treat `country` as a key field, not a survivorship field — resolve it
  once via `resolve_country` before dedup and exclude it from `_merge`'s union, or
  give it an explicit `_SOURCE_TRUST` entry and a real tiebreak.

---

### S2 — `dedup` writes back every key it is handed; a `csv.DictReader` restkey yields a `None`-keyed, column-shifted record that passes through silently

- **Where:** `dedup.py:190-194` — `for key in set(a) | set(b): merged[key] =
  _pick(key, a, b)`. No allowlist, no key normalisation, no rejection.
- **Input source:** `main.load_master` uses `csv.DictReader(f)` (`main.py:86`)
  with the stdlib defaults `restkey=None, restval=None`. A row with more cells
  than the header puts the surplus under the literal key `None`.
- **Breaks:** two distinct harms, and the visible one is the smaller.
  1. The output record carries a key that is `None`, not `str` — `dict[NoneType,
     list[str]]` inside a value the rest of the pipeline believes is
     `dict[str, str | float | int | bool | None]`.
  2. **The surplus cells shift nothing in Python but the *last* real columns are
     overwritten by earlier cells.** With a 23-column header and a 25-cell row,
     `scraped_at` receives the `source` cell's value and the real timestamp lands
     in the restkey list. `scraped_at` is the recency input for every volatile
     field (`dedup.py:114, 183`), so `rating`, `review_count`, `website_live` and
     `website` survivorship is decided by a corrupted timestamp.
- **Trigger (verified):** 25 cells against a 23-cell header — the shape produced
  by a hand-edited or Sheets-round-tripped master, which is a live premise: the
  master is downloaded from a shared Drive folder (`scrape.yml:56`) and the five
  CSVs are the human-facing product.
  ```
  cell count: 25 vs header 23
  keys of row[0]: [None] -> ['2026-01-01T00:00:00+00:00', 'SURPLUS']
  after _merge,  None key survives: True
  after dedup,   None key in output: True
  key type: NoneType   value type: list
  write_csv survives it (extrasaction='ignore' drops it silently):
    305 bytes -> SURPLUS present in file? False
  ```
  The last line is the detection problem: `csv.DictWriter(..., extrasaction="ignore")`
  at `main.py:125` discards the evidence, and the only CI gate is a row count
  (`scrape.yml:74-83`).
- **Fix:** in `load_master`, `restkey="__extra__"` and log/raise when it fires;
  in `_merge`, iterate `FIELDS`-style allowlist (or at minimum reject non-`str`
  keys) instead of `set(a) | set(b)`.

---

### S2 — Nothing types the `BusinessRecord` boundary: the TypedDict is total, every actual producer is partial, and `dedup`'s output key sets are heterogeneous

- **Where:** `base.py:5` `class BusinessRecord(TypedDict)` — no `total=False`
  (verified), so all 23 keys are required; 4 are non-`Optional`
  (`base.py:25-28`: `lead_score: int`, `source: str`, `scraped_at: str`,
  `completeness_score: int`). `dedup.py:197` takes and returns bare `dict`.
- **Declared vs actual:**

  | | Declared | Actual in practice |
  |---|---|---|
  | `dedup` parameter | `list[dict]` | `list[BusinessRecord]` ∪ `list[dict]` from `load_master` (`main.py:77`) — a heterogeneous union, statically unrepresentable |
  | `dedup` return | `list[dict]` | `list[dict]` with **per-element key sets that differ** (verified) and arbitrary extra/`None` keys |
  | `country` | `str \| None` | effectively the enum `{"LB","SA"}`; anything else silently means Lebanon (`dedup.py:48`) |
  | `scraped_at` | `str` | `str` that must additionally be a *fixed-offset ISO instant* for `max()` at `dedup.py:177` and `_recency` at `dedup.py:156` to be correct |
  | `source` | `str` (non-optional) | `str` from a scraper, but `load_master` can hand `dedup` `None` (verified) |
  | `completeness_score` | `int` (non-optional) | `None` after a failed `int(float(...))` cast at `main.py:96-101`, until `enricher.py:349` overwrites it |

- **Breaks:** the totality claim is false in both directions and the type system
  is not in a position to notice, because the annotation is `dict`.
  - `dedup` **reads** only `.get()` (`dedup.py:202, 205, 212, 213, 227`), so missing
    keys are tolerated — but a *wrong-typed present* key is not.
  - `dedup` **writes** only the union of the input keys, so it neither adds nor
    validates. Verified: a 3-key master record yields a 3-key output record, 20 of
    23 TypedDict keys absent; merging with one fresh scraper record only reaches 8
    keys. `enricher.py:340-348` later `setdefault`s 8 of them, but nothing
    backfills `category`, `address`, `source`, `scraped_at`,
    `industry_priority`, `recommended_service`, `lead_score` or
    `completeness_score`.
  - Consequence at `main.py:190-197`: `industry_priority`, `recommended_service`
    and `lead_score` are written *after* `dedup` returns, so even a perfectly
    typed input record cannot satisfy `base.py:23-25, 28` at the `dedup` boundary.
    The TypedDict describes the *end* of the pipeline while being applied to its
    middle.
- **Verified round-trip that manufactures `country=None`:** `write_csv` fills
  absent fieldnames with `csv.DictWriter`'s default `restval=""` (`main.py:125`),
  and `load_master` converts `""` to `None` (`main.py:87-89`):
  ```
  write_csv(p, [{"name":"X","phone":"0501234567"}])  ->  load_master(p)
  [{'name':'X', ..., 'country': None, ..., 'source': None, 'scraped_at': None}]
  dedup key for phone 0501234567 with country=None -> +961501234567
  ```
- **Fix:** `def dedup(records: Sequence[BusinessRecord]) -> list[BusinessRecord]:`,
  make `BusinessRecord` reflect its real lifecycle (`total=False` for the six
  enrich-time fields, `NotRequired` where a source genuinely cannot supply one),
  and add a `mypy` step plus `tests/` execution to `scrape.yml`.

---

### S2 — `normalize_phone`'s `""` return erases a phone that `completeness_score` has already banked

- **Where:** `dedup.py:45-46` returns `""` for junk; `main.py:194`
  `r["phone"] = normalize_phone(raw_phone, ...)` writes it back.
- **Ordering:** `enrich()` scores completeness at `enricher.py:349` (which awards
  +1 for any truthy `phone`, `enricher.py:103-104`), which runs at `main.py:187`.
  `main.py:194` then normalises the phone *after* that. `lead_score` is
  recomputed afterwards (`main.py:197`) but `completeness_score` is not.
- **Breaks:** a record can ship to `qualified_businesses.csv`
  (`main.py:206`, `completeness_score >= 1`) with `completeness_score=1` earned
  entirely by a phone string that the same run then deleted from the row.
- **Trigger (verified):**
  ```
  phone before       : 'ext 4'
  completeness_score : 2   <= scored the junk phone
  phone after        : ''
  completeness_score : 2   <= now stale; CSV shows a blank phone
  lands in qualified_businesses.csv: True
  ```
- **Fix:** normalise the phone before `enrich()`, or recompute
  `completeness_score` at `main.py:197` next to the existing `lead_score`
  recomputation.

---

### S3 — `scraped_at` is typed `str` but consumed as an instant, and `max()` at `dedup.py:177` does not coerce

- **Where:** `base.py:27` `scraped_at: str`; consumed at `dedup.py:156`
  (`str(...)` — coerced) and `dedup.py:177` (`max(av, bv)` — **not** coerced) and
  `dedup.py:183` via `_recency`.
- **Two distinct defects from one root cause** (no coercion to a real instant):
  1. **Wrong answer, verified.** ISO-8601 offsets make lexicographic order differ
     from chronological order, and `_pick` uses lexicographic order to decide which
     observation of a `_VOLATILE` field survives:
     ```
     a = 2026-09-01T12:00:00+00:00  (12:00Z)  rating 4.0
     b = 2026-09-01T09:00:00-04:00  (13:00Z)  rating 4.8
     b is FRESHER: True
     max() string compare picked: a   -> rating taken from: 4.0   (WRONG)
     ```
     `httpclient.utc_now_iso` (`httpclient.py:220-229`) emits `+00:00`, but the
     master is a shared, hand-editable artifact, and `tests/test_lead_signal.py:204`
     itself writes a `Z`-suffixed fixture — so mixed spellings are already in the
     project's own vocabulary.
  2. **Hard crash, latent.** `max()` compares raw values, so two non-`str`
     `scraped_at` values raise and there is no `except` anywhere on the path:
     ```
     _merge({... "scraped_at": 1767225600.0}, {... "scraped_at": "2026-01-01T00:00:00+00:00"})
       -> TypeError: '>' not supported between instances of 'str' and 'float'
     _merge({... "scraped_at": b"2026-01-01"}, {... "scraped_at": "2026-01-01T00:00:00+00:00"})
       -> TypeError: '>' not supported between instances of 'str' and 'bytes'
     ```
     Today only `main.main` calls `dedup` (`grep` over the repo: the sole call is
     `main.py:180`) and it only ever passes CSV strings, so this is unreachable
     today — which is exactly the point: the `list[dict]` signature is the *only*
     thing standing between a future caller and a dead run.
- **Fix:** parse once into an aware `datetime` at the boundary and compare
  datetimes; or normalise every `scraped_at` to UTC `Z` on write
  (`httpclient.utc_now_iso` plus a read-side normaliser in `load_master`).

---

### S3 — Helper preconditions are narrower than their annotations suggest

- **`normalize_name(name: str)` (`dedup.py:66`) raises on `None`**, verified:
  `normalize(None)` → `TypeError: normalize() argument 2 must be str, not None`.
  `dedup` is safe today only because `dedup.py:212` guards with
  `record.get("name") or ""`; any other caller of this public function is one
  `None` away from a traceback. Worth a `str | None` signature or an internal
  guard, given `base.py:6` declares `name: str | None`.
- **`_extract_city` returns the country, not the city, for the most common
  address shape** (`dedup.py:76` takes `parts[-1]`), verified:
  ```
  'Hamra, Beirut, Lebanon' -> 'lebanon'
  'Beirut, Lebanon'       -> 'lebanon'
  'Hamra, Beirut'          -> 'hamra, beirut'
  ```
  Every OSM address built at `osm.py:80-87` ends in `addr:city` then
  `addr:district`, and every Google `formattedAddress`
  (`google_places.py:286`) ends in the country. So the `dedup.py:214` name key
  `(normalize_name(name), city)` frequently degenerates to
  `(name, "lebanon")` — shared across every Lebanese business in that city,
  which is a *correctness* issue rather than a contract one, but it is caused by
  the same "declared `str | None`, actually a structured value" gap.
- **Fix:** widen `normalize_name`'s signature; give `_extract_city` a documented
  `parts[-2]`-vs-`parts[-1]` rule and drop a known-country suffix.

---

## Declared vs actual, condensed

| Aspect | Declared | Actual | Enforced? |
|---|---|---|---|
| `dedup` input | `list[dict]` (`dedup.py:197`) | `list[BusinessRecord]` ∪ `load_master`'s partial dicts (`main.py:77`) | No |
| `dedup` output | `list[dict]` | `list[dict]`, heterogeneous key sets, possible `None` key | No |
| Record keys | 23, all required (`base.py:5`, total) | 3–23 depending on provenance; union-only at `dedup.py:192` | No |
| `country` | `str \| None` (`base.py:9`) | enum `{"LB","SA"}`; else Lebanon (`dedup.py:48`) | No |
| `scraped_at` | `str` (`base.py:27`) | fixed-offset ISO instant; compared as text (`dedup.py:177`) | No |
| `phone` | `str \| None` (`base.py:13`) | national **or** international format; may be multi-number (`osm.py:89` `tags["phone"]` can be `"+961…;+961…"`) | Partially (`normalize_phone`) |
| `source` | `str`, non-optional (`base.py:26`) | `str` from scrapers; `None` from `load_master` (verified) | No |
| `lead_score`, `completeness_score` | `int`, non-optional (`base.py:25, 28`) | written *after* `dedup` (`main.py:190-197`) / `None` until `enricher.py:349` | No |

## Every field `dedup` reads or writes, vs the TypedDict

**Reads — by name, all inside `BusinessRecord`; zero out-of-schema reads:**

| Field | Where read | Declared |
|---|---|---|
| `phone` | `dedup.py:202, 205, 227, 228` | `str \| None` |
| `country` | `dedup.py:205, 228` | `str \| None` |
| `name` | `dedup.py:212` | `str \| None` |
| `address` | `dedup.py:213` | `str \| None` |
| `source` | `dedup.py:144, 173` | `str` |
| `scraped_at` | `dedup.py:156, 176` | `str` |
| `email` | `dedup.py:124` (validity bonus only) | `str \| None` |
| `website` | `dedup.py:126` (validity bonus only) | `str \| None` |
| `lat`, `lon` | `dedup.py:128` (validity bonus only) | `float \| None` |
| `rating` | `dedup.py:134` (validity bonus only) | `float \| None` |

**Writes — unbounded, and this is the answer that matters.** `dedup.py:192`
iterates `set(a) | set(b)`, so `dedup` re-emits every key it is handed:
- keys that are **not** in `BusinessRecord` at all — anything a future scraper
  adds and any legacy column in an old master CSV;
- the literal key `None` with a `list[str]` value, from `csv.DictReader`'s default
  `restkey` (verified, see S2 above);
- keys whose values are of a type no `BusinessRecord` field permits.

There is no allowlist, no key normalisation and no rejection anywhere in
`_merge`, `dedup`, `enrich`, or `write_csv`. `write_csv`'s
`extrasaction="ignore"` (`main.py:125`) converts every one of these into silent
discard rather than an error.

## Return-value consumers, audited for "breaks on an unexpected shape"

| Consumer | Shape assumption | Verdict |
|---|---|---|
| `main.py:183-184` `r["country"] = resolve_country(r)` | subscripts to **write** | Safe on any dict. But ordering is wrong — see S1. |
| `main.py:187` `enrich(records)` → `enricher.py:231` `r["website"]` | subscripts after `.get("website")` truthiness | Safe: filtered, so the key exists. |
| `enricher.py:330-335` `r.get("address"/"lat"/"lon"/"country")` | `.get` only | Safe. `r.get("country", "LB")` returns `None` for a `None` value — the pre-`resolve_country` bug `main.py:53-58` documents; masked today only because `main.py:184` runs first. |
| `enricher.py:340-348` `r.setdefault(...)` | idempotent | Safe, and the de facto key backfill for 8 fields. |
| `main.py:206` `r.get("completeness_score", 0) >= 1` | numeric | Safe *only* because `enricher.py:349` overwrites it with an `int`. Would `TypeError` on a `str`/`None`. |
| `main.py:215-217` `r.get("website_live") is True/False/is None` | real `bool` or `None` | Safe: `enricher.py:250` writes real bools; `main.py:103` maps CSV `"True"/"False"` and `check_websites` returns early at `enricher.py:232-233` only when no record has a website. |
| `main.py:229-231` `by_region[r.get("region") or "Unknown"]` | hashable `str` | Safe for `str`/`None`. **Breaks** on a list/dict `region` (the restkey path): `TypeError: unhashable type`. |
| `main.py:234` `f"{reg:<20}"` | `str`/`int`/`float` | Verified: `int`/`float`/`bool`/`str` format fine; `None`/`list`/`dict` raise `TypeError: unsupported format string`. Guarded only by `or "Unknown"`, which does **not** catch a list. |
| `main.py:237-242` `svc = r.get("recommended_service") or "Unknown"` then `f"{svc:<55}"` | `str` | Safe — `recommend_service` always returns a `str` (`pitch_recommender.py:99`). |
| `main.py:209-213` → `write_csv` → `csv.DictWriter(..., extrasaction="ignore")` | dict | Extra/`None` keys silently dropped; missing keys filled with `restval=""`. No error either way. |
| `main.py:181` `print(f"... {len(records)} ...")` | `list` | Safe. `dedup([]) == []` (verified); a non-dict element raises at `dedup.py:202` (`AttributeError`) but only `main.py` calls it and it always passes dicts. |

## Not a bug, but worth knowing

- **`dedup` does not mutate its caller's input dicts.** Verified: input dicts are
  byte-identical after the call, and every returned object is a distinct copy
  (`dedup.py:210, 218` use `dict(record)`; `dedup.py:191-194` builds fresh dicts).
  This is load-bearing — `main.py:183-197` and `enricher.py:249-259` mutate
  `dedup`'s output in place, and if `dedup` aliased its inputs the raw scraper
  batches would be corrupted as a side effect. Worth a regression test.
- **Shallow copy only.** `dict(record)` (`dedup.py:210, 218`) copies the mapping,
  not the values. If any source ever stores a mutable value (the restkey `list`
  is one today), it is shared between the caller's record and the merged output.
- **Output ordering is deterministic but undocumented.** `dedup.py:232` returns
  `phone_records + name_records`, where each half is in `dict` insertion order
  (`dedup.py:207-218`), which follows input order. Nothing in `main.py` depends on
  it, but a stable, documented order would make CSV diffs between runs readable.
- **The `name_index` filter at `dedup.py:227-228` re-normalises the phone using
  the *merged* `country`.** This is a second, independent read of the same
  unenforced enum, so it inherits the S1 failure mode; and because `_merge` may
  take `phone` from one source and `country` from another, the re-normalisation
  can disagree with the key the record was originally indexed under.
- **`pyproject.toml` ships a `[tool.ruff]` `line-length = 100` but `ruff` runs
  nowhere** (`scrape.yml` has no lint step), and `dedup.py`'s `[tool.mypy]
  strict = true` (`pyproject.toml:57`) is dead configuration for the same reason.
  The TypedDict was clearly written to be checked; nothing checks it.

## Recommended order of work

1. **Move `resolve_country` before `dedup`** (`main.py:183-184` → above
   `main.py:180`) and stop defaulting to `"961"` silently at `dedup.py:48` —
   reject or alias unknown codes. Fixes S1 and half of S2-#2. One-line diff,
   largest correctness win, and it makes the master self-healing since
   `main.py:209` writes the canonical value back.
2. **Stop deriving `country` from the query string.** Add
   `places.addressComponents` / `places.plusCode` to `FIELD_MASK`
   (`google_places.py:32-43`) and set `country` from the response, keeping
   `_country_from_query` only as a fallback. Removes the thread-order
   nondeterminism at `google_places.py:263-266` at the source.
3. **Type the boundary.** `dedup(records: Sequence[BusinessRecord]) ->
   list[BusinessRecord]`, make `BusinessRecord` honest about its lifecycle
   (`total=False` / `NotRequired` for the six enrich-time fields), and add a
   `mypy --strict` step to `scrape.yml`. This is what would have caught S1, S2-#2
   and S3-#2 at review time instead of in production data.
4. **Allowlist keys in `_merge`** (`dedup.py:192`) and set
   `restkey="__extra__"` in `load_master` (`main.py:86`) so a malformed master row
   is reported rather than shifted-and-dropped.
5. **Give `country` a trust entry and a real tiebreak** in `_SOURCE_TRUST`
   (`dedup.py:97-110`), or exclude it from the merge union entirely.
6. **Coerce `scraped_at` to an aware UTC `datetime`** at the `load_master`
   boundary and compare datetimes in `_pick` (`dedup.py:176-177`), instead of
   `max()` on raw strings.
7. **Normalise `phone` before `enrich()`**, or recompute `completeness_score`
   beside the existing `lead_score` recomputation at `main.py:197`.

### Verification method

All behavioural claims above were reproduced by importing the real modules with
only `requests` stubbed (no network, no scraper execution, no source modified):
`dedup`, `_merge`, `_pick`, `normalize_phone`, `normalize_name`,
`_extract_city`, `main.load_master`, `main.write_csv`, `main.resolve_country`,
`enricher.completeness_score`, `enricher.infer_region`,
`scrapers.google_places._country_from_query`, `scrapers.base.BusinessRecord`.
`git status` confirms zero modifications to tracked `.py` files. Concrete input
dictionaries and CSV row shapes are quoted inline in each finding.