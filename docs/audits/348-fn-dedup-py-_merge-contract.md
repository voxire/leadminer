# 348 — `_merge` contract: declared `dict`, delivered "any keys at all"

## Verdict

`_merge` (`dedup.py:190`) declares `-> dict` and `BusinessRecord` (`scrapers/base.py:5-28`)
is imported by nothing outside the three scrapers, so the 23-column contract is enforced
nowhere — mypy `strict = true` is configured (`pyproject.toml:64-68`) but never run, and
`.github/workflows/scrape.yml` has no test/lint/type step. The one real defect the lens
exposes is that `_merge` **writes `source` as a set encoded in a string (`dedup.py:173-174`)
and `_trust_for` reads it back as a trust oracle taking `max` over all members
(`dedup.py:148-152`)** — so per-field authority silently becomes per-record *history*, which
makes trust ties the normal case and hands every non-volatile field to the `str(value)`
lexicographic tiebreak at `dedup.py:184`. Demonstrated consequence: a stale OSM `name:ar`
permanently beats the fresh Google display name, and a category wikidata was deliberately
down-weighted to trust 1 (`dedup.py:108`) wins anyway once the row has been seen twice.

---

## Findings

### S1 — `_merge` writes `source` as a union and `_trust_for` reads it back as a trust oracle, so field authority becomes record history

- **Where:** `dedup.py:192-193` (writes every key of `set(a) | set(b)`); `dedup.py:173-174`
  (writes the compound `source`); `dedup.py:143-145` (`_sources_of` splits on `|`);
  `dedup.py:148-152` (`_trust_for` takes `max` over all members); `dedup.py:97-110`
  (the table it inflates); `dedup.py:179-187` (trust is the **first** element of the rank).
- **Breaks:** `_SOURCE_TRUST` is documented at `dedup.py:93-96` as
  *"How much each source is trusted per field… Trust therefore has to be per-field, not
  per-record."* The implementation makes it per-record-history, which is strictly weaker.
  After the first merge, a record's `_trust_for(field)` is `max` over every source that has
  ever contributed **any** field to it — not over the sources that supplied **this** value.
  A value's authority is credited to whatever other field it happened to arrive alongside.
  - `dedup.py:151` is `best = max(best, _SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST))`
    inside a loop over `_sources_of(record)`. There is no per-field provenance anywhere in
    the record, so the lookup cannot be narrowed.
  - Verified saturation: for `{"source": "google_places|osm|wikidata"}`,
    `trust(name)=trust(address)=trust(category)=trust(website)=trust(email)=trust(rating)=trust(lat)=trust(review_count)=3`,
    `trust(phone)=2`. The whole table is inert.
  - Verified defeat of a deliberate weight: `_merge(wikidata, google)` on a *fresh* pair
    gives `category == "hair_dresser"` (Google wins, trust 3 > 1 — correct). The **same
    wikidata value** on a row whose `source` is already `"google_places|wikidata"` grades at
    trust 3, ties Google, and `_merge` returns `category == "hairdresser"` — the value
    `dedup.py:108` exists to suppress.
  - `main.py:150` reloads the cumulative master and `main.py:179` feeds master rows through
    the same `phone_index`/`name_index`, so any business seen by two sources saturates in
    run 1 and is saturated forever after. This is the steady state from run 2 onward, not an
    edge case.
- **Trigger:**
  ```python
  _merge(
    {"name": "X", "phone": "p", "source": "google_places|wikidata",
     "category": "hairdresser", "scraped_at": "2026-01-01T00:00:00+00:00"},
    {"name": "X", "phone": "p", "source": "google_places",
     "category": "hair_dresser", "scraped_at": "2026-10-01T00:00:00+00:00"},
  )["category"]   # -> 'hairdresser'  (wikidata's low-trust value wins)
  ```
  `"hairdresser"` is in `ADJACENT_BUSINESSES` (`scrapers/whitelist.py:58`) but
  `"hair_dresser"` is in neither tier and contains none of the `business_keywords` at
  `scrapers/whitelist.py:123-126`, so `is_business_category("hair_dresser")` is `False` and
  `main.py:172` would drop the row. The same business lands in the export or is dropped
  depending on which run happened to observe it first.
- **Fix:** store provenance per field. Keep `source` as the export-facing union string, but
  add a parallel `dict[str, str]` field→source map that `_merge` unions field-by-field, and
  make `_trust_for(field, record)` read `record["_provenance"][field]` instead of
  `max` over `_sources_of(record)`. Alternative: pass the provenance of the *specific*
  candidate value into `_pick` instead of the provenance of the whole record.

### S2 — For 13 of 23 columns the winner is decided by `str(value)`, and trust saturation makes that the normal path

- **Where:** `dedup.py:179-187`; the tiebreak is `str(value)` at `dedup.py:184`;
  `_VOLATILE` at `dedup.py:114` contains only `{"rating", "review_count", "website_live", "website"}`.
- **Breaks:** `rank()` is `(trust, validity, recency_or_empty, str(value))`. For any field not
  in `_VOLATILE` the third element is the constant `""`, so the decision reduces to
  `(trust, validity, str)`. Once S1 saturates trust and both values are plausible strings,
  validity ties too, and the lexicographic order of the two strings picks.
  - The 13 affected columns: `name`, `address`, `category`, `phone`, `email`, `lat`, `lon`,
    `facebook`, `instagram`, `whatsapp`, `linkedin`, `lead_score`, `completeness_score`.
    (`region` and `country` are also unranked but are overwritten at `main.py:184`.)
  - `name` and `address` are the two columns a human reads first. Arabic codepoints
    (U+0600+) sort above Latin, so a stale OSM `name:ar` beats the fresh canonical Google
    display name permanently.
- **Trigger:**
  ```python
  _merge(
    {"name": "مقهى بيروت", "category": "cafe", "address": "Zokak el Blat, Beirut",
     "phone": "+96170123456", "source": "google_places|osm",
     "scraped_at": "2026-01-01T00:00:00+00:00"},
    {"name": "Cafe Beirut", "category": "cafe", "address": "Hamra, Beirut",
     "phone": "+96170123456", "source": "google_places",
     "scraped_at": "2026-10-01T00:00:00+00:00"},
  )
  # -> {"name": "مقهى بيروت", "address": "Zokak el Blat, Beirut"}
  ```
  Google is fresher and holds trust 3 for `name`; the master row also holds trust 3 (via
  `osm`); both validities are 1; `name` is not volatile. Verified order-independent —
  `_merge(google, master)` returns the same Arabic name — so it is deterministic, which
  makes it sticky rather than flaky.
- **Numeric variant (currently masked, will not stay masked):** `lead_score` and
  `completeness_score` are `int` in the TypedDict (`base.py:25`, `base.py:28`), not
  volatile, and unranked by `_validity` (fallthrough `return 1` at `dedup.py:140`), so the
  tiebreak is string comparison on integers: `_merge` of `lead_score=100` against
  `lead_score=30` returns **30**, and `1000` vs `9` returns **9**. This is invisible today
  only because `enricher.py:349-350` and `main.py:197` recompute both before any consumer
  reads them. Nothing records that dependency — see S4 — and `dedup()` otherwise returns
  records it presents as coherent.
- **Fix:** extend `_VOLATILE` to at least `name`, `address`, `category`, `phone`, `email`
  so recency actually breaks the tie, and rank numeric columns on the numeric value
  (`float(value)` guarded) instead of `str(value)`.

### S2 — `_merge` cannot tell a declared column from an unknown key; it unions both, and `write_csv` drops the unknown ones silently

- **Where:** `dedup.py:192` `for key in set(a) | set(b):`; `dedup.py:193`;
  `dedup.py:140` (`_validity` blind `return 1` for any unrecognised field name);
  `dedup.py:151` (`_DEFAULT_TRUST = 1` for any field not in the table);
  `main.py:77-105` (`load_master` never validates the CSV header);
  `main.py:125` (`csv.DictWriter(..., extrasaction="ignore")`).
- **Breaks:** the key set of a merged record is the union of its inputs, so anything the
  master CSV happens to contain is treated as mergeable data, graded, held in memory for
  every row, and then discarded at export with no warning. Two distinct silent-loss classes:
  - **(a) Unknown column.** `{"google_place_id": "ChIJabc123"}` survives `_merge` as
    `merged["google_place_id"]`, was graded `_validity → 1` and `_trust_for → 1`, and is
    dropped at `main.py:125`. `DictWriter` never calls `.keys()` under
    `extrasaction="ignore"` — it looks up `rowdict.get(field, restval)` per `FIELDS`
    entry — so the loss is unobservable. This is precisely the identity column audit 050
    wants (`google_place_id` / OSM id), which is the one thing a dedup function must never
    drop.
  - **(b) Ragged row.** `csv.DictReader`'s default `restkey=None` puts overflow cells under
    a `None` key as a **list**. `load_master` passes it straight through (the coercion
    loops at `main.py:90-101` only touch named columns). `_merge` unions the `None` key,
    `_pick(None, a, b)` runs `_is_missing(['EXTRA1','EXTRA2'])` → `False` (`dedup.py:90`),
    `_validity(None, [...])` → `1` via the `dedup.py:140` fallthrough, `_trust_for(None, …)`
    → `1`, and the list lands in the output record.
    Verified end to end: a master CSV row
    `Cafe,LB,+96170123456,cafe,EXTRA1,EXTRA2` yields
    `rows[0][None] == ['EXTRA1', 'EXTRA2']`, survives `_merge`, and is written back as
    `Cafe,cafe,,LB,,,,+96170123456,,,,,,,,,,,,,,osm,2026-01-01T00:00:00+00:00` — extras gone,
    exit status 0, no warning anywhere.
  - **Heterogeneous output.** `dedup()` returns `dict(record)` for first-sight rows
    (`dedup.py:210`, `dedup.py:218`) and `_merge(...)` output for merged rows. Verified: two
    inputs that differ only in whether they matched produce a 6-key dict and a 7-key dict.
    A 23-column contract that yields 6-key dicts is not a contract.
- **Fix:** make `main.FIELDS` (`main.py:33-40`) the single source of truth in a module both
  sides import; assert `set(record) <= set(FIELDS)` at the `dedup()` entry point; warn once
  per run on unknown keys instead of unioning them; and drop `restkey` handling by
  validating `len(row)` in `load_master`.

### S2 — `BusinessRecord` is enforced on the producer side only; nothing on the consumer side, and no enforcement at all

- **Where:** `base.py:5-28` (the TypedDict) and `base.py:33` (`Iterator[BusinessRecord]`);
  versus `dedup.py:190` (`-> dict`), `dedup.py:197` (`list[dict]`), `dedup.py:159`
  (`_pick -> object`), `enricher.py:101`, `enricher.py:283`, `enricher.py:326`,
  `main.py:50`, `main.py:137`.
- **Breaks:** grep for `BusinessRecord` returns imports in `scrapers/osm.py:5`,
  `scrapers/wikidata.py:5`, `scrapers/google_places.py:28` — and nothing else.
  `dedup.py`, `enricher.py`, `main.py`, `cli.py`, `pitch_recommender.py` never import it.
  `_pick` returns `object` (`dedup.py:159`), so `_merge`'s value type carries no field or
  value information at all: it is effectively `dict[str, object]`.
  - `mypy` is configured `strict = true` (`pyproject.toml:64-68`) and would already reject
    the bare `dict` generics at `dedup.py:190`/`dedup.py:197` (`disallow_any_generics`), but
    mypy is only a dev extra (`pyproject.toml:26`) and `.github/workflows/scrape.yml` runs
    `python main.py` and nothing else — no `pytest`, no `ruff`, no `mypy` (verified by grep).
  - Two declared **non-optional** fields are routinely `None`: `lead_score: int`
    (`base.py:25`) and `completeness_score: int` (`base.py:28`). `main.py:96-101` sets both
    to `None` on any unparseable cell, and `_merge` propagates `None` whenever both sides
    are missing (`dedup.py:168-171`).
  - **The one place that breaks on that shape:** `main.py:206`
    `qualified = [r for r in records if r.get("completeness_score", 0) >= 1]` raises
    `TypeError: '>=' not supported between instances of 'NoneType' and 'int'` when the value
    is `None`. `.get`'s default only covers an **absent** key, not a `None` value — the exact
    trap `main.py:50-58` documents at length for `country`. It is masked today solely because
    `enricher.py:349` unconditionally overwrites the key before `main.py:206` runs. That is a
    two-line ordering coincidence, not a contract.
  - For completeness: every field `_pick`/`_validity`/`_VOLATILE`/`_SOURCE_TRUST` names
    **is** in the TypedDict (`name`, `category`, `address`, `phone`, `email`, `website`,
    `facebook`, `instagram`, `whatsapp`, `rating`, `review_count`, `lat`, `lon`, `source`,
    `scraped_at`, `website_live`; `linkedin` is in the TypedDict but in no trust table).
    So the non-TypedDict fields are not hard-coded — they are **whatever the unvalidated
    master-CSV header contains**, unbounded (S2 above).
- **Fix:** annotate `_merge(a: BusinessRecord, b: BusinessRecord) -> BusinessRecord` and
  `dedup(records: list[BusinessRecord]) -> list[BusinessRecord]`; make the four genuinely
  nullable fields `| None`; change `main.py:206` to `r.get("completeness_score") or 0` so
  `None` is explicit rather than accidental; add `mypy --strict` + `pytest` to CI.

### S3 — `scraped_at` is compared with `max()` on raw values: `TypeError` on mixed types, lexicographic on formats

- **Where:** `dedup.py:176-177`.
- **Breaks:** `_merge({"…","scraped_at": "2026-01-01"}, {"…","scraped_at": 20261001})` raises
  `TypeError: '>' not supported between instances of 'int' and 'str'` (verified). Not
  reachable from the three scrapers — all use `utc_now_iso()` (`httpclient.py:220-231`,
  returning `+00:00`-suffixed ISO) — or from `load_master`, which never coerces
  `scraped_at` and therefore leaves it `str | None`. But `scraped_at` is copied verbatim
  from CSV at `main.py:104`, so any external writer of the master (a spreadsheet edit, a
  re-export, an `audit 032` offline fixture) breaks the whole run at `dedup()`.
- **Format sensitivity:** `max("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00Z")` returns
  the `Z` form for the *same instant*, because `+` (43) < `Z` (90). Verified: with trust
  tied, a `rating` of `4.9` stamped `+00:00` loses to `3.1` stamped `Z`. Every row this
  pipeline writes is `+00:00`, so the hazard is only an inconsistent external writer — but
  `httpclient.py:222-225` already documents awareness of exactly this comparison.
- **Fix:** `scraped_at = max(str(av), str(bv))` at minimum, or parse to `datetime` once in
  `load_master`.

### S3 — `_is_missing(False)` is `False`, so a stale `website_live=False` survives a merge that replaced the URL it referred to

- **Where:** `dedup.py:79-90` (the `return False` at `dedup.py:90` for every non-`str`
  non-`None` value); `dedup.py:167-171`; `dedup.py:114`.
- **Breaks:** `website_live` is in `_VOLATILE`, so `dedup.py:183` *should* prefer the fresher
  observation. It never gets there: with `av=False, bv=None`, `_is_missing(bv)` at
  `dedup.py:170` short-circuits and returns `av` at `dedup.py:171`. `False` is never
  "missing" by the `dedup.py:86-90` definition.
- **Trigger (verified):**
  ```python
  _merge(
    {"name": "X", "phone": "+96170123456", "source": "osm",
     "website": "https://old.example", "website_live": False,
     "scraped_at": "2026-01-01T00:00:00+00:00"},
    {"name": "X", "phone": "+96170123456", "source": "osm",
     "website": "https://new.example", "website_live": None,
     "scraped_at": "2026-10-01T00:00:00+00:00"},
  )
  # -> {"website": "https://new.example", "website_live": False}
  ```
  A URL checked today carries last month's "server said dead" verdict.
- **Masked, but only by luck of ordering:** `enricher.check_websites` re-fetches every record
  that has a website (`enricher.py:231`) and overwrites the verdict at `enricher.py:250`, so
  the shipped path is correct. The safety net is 40 lines away in a different module and is
  conditional on the network; `_merge`'s own contract permits the stale pairing.
- **Downstream miscount if that shape ever reaches disk:** `cli.py:108`
  `dead = sum(1 for r in rows if (r.get('website_live') or '').strip() == 'False')` does not
  require a website, so `leadminer stats` would report rows with no website as "website dead"
  — inflating the number an operator uses to size the rebuild-pitch campaign.
- **Fix:** treat `False` as missing for `website_live` specifically (it is the one field where
  `False` means "no verdict"), or special-case it in `_pick` the way `source` and
  `scraped_at` already are; and require `r.get('website')` in `cli.py:108`.

---

## Not a bug, but worth knowing

- **`_merge` is genuinely order-independent, and that is load-bearing.** `rank()`
  (`dedup.py:179-187`) returns `(int, int, str, str)` — a total order — with `str(value)` as
  the final key, so `_merge(a, b) == _merge(b, a)` for every field. Verified on the
  Arabic/English case, where reversing the arguments changes nothing. Asserted at
  `tests/test_lead_signal.py:292-295` and `:318-319`. Most merge code lacks this. It is
  precisely why S2 hurts: determinism converts a bad tiebreak into a permanent one instead
  of a flaky one.
- **The union-with-missing-shortcircuit base is right.** `set(a) | set(b)` at `dedup.py:192`
  guarantees a field present on only one side is never dropped, and `dedup.py:168-171`
  correctly treats `None`, `""` and `"   "` alike — the regression guarded at
  `tests/test_lead_signal.py:286`.
- **`_merge` never invents a key.** Unlike `enricher.py:340-348` (`setdefault`) and
  `main.py:184-197` (direct assignment), `_merge` only writes keys one of its inputs
  already had. It also does not mutate its inputs, and `dedup` stores `dict(record)` at
  `dedup.py:210`/`dedup.py:218`, so `raw_filtered` and `master` are untouched by the merge.
  That is why `main.py:183-197` mutating the deduped records in place is safe.
- **No unguarded subscript on a `dedup()` output anywhere.** `enricher.py:231`'s `r["website"]`
  is guarded by `r.get("website")` in the same comprehension; `cli.py:167`'s `r["lat"]` is
  inside a `try` that catches `KeyError`. Every other read on the pipeline is `.get`. With
  `write_csv`'s `restval=""` at `main.py:125`, the heterogeneous key sets out of `dedup()`
  crash nothing today. **This is the reason the missing-`BusinessRecord` finding is S2 and not
  S1** — the code is defensively written; it just has no stated invariant to defend.
- **Output key order is hash-seed dependent and that is harmless today.** `dedup.py:192`
  iterates a `set`, so `merged` is populated in `PYTHONHASHSEED` order (verified: three seeds,
  three different orderings). Nothing observable depends on it because every consumer looks
  up by name and `write_csv` passes an explicit `fieldnames=FIELDS` at `main.py:125`. Worth
  one line of comment so nobody later "fixes" it by depending on the order.
- **Minor doc drift, out of scope for this lens:** `BRIEF.md:59-62` and `base.py:5-28` both
  describe a "22-key" / "22 columns" record; the actual count is 23.

---

## Recommended order of work

1. **Per-field provenance (S1).** Make `_trust_for` reflect who supplied the value, not who
   supplied the record. Everything in the second finding is downstream of this: with real
   provenance, trust ties become rare and `str(value)` goes back to being the last-resort
   tiebreak it was designed to be. Highest value, and it is the root cause.
2. **Fix the tiebreak (S2).** Add `name`, `address`, `category`, `phone`, `email` to
   `_VOLATILE`, and rank numeric columns on the number. Smallest diff, immediately visible in
   the two columns humans read first.
3. **`FIELDS` as the single source of truth + a boundary assertion (S2).** Validate the master
   header in `load_master`, assert `set(record) <= set(FIELDS)` at the `dedup()` entry,
   and warn on unknown keys instead of unioning them. Cheapest guard against schema drift,
   and it makes the ragged-row class impossible before an identity column is ever added.
4. **Type the boundary and enforce it (S2).** `BusinessRecord` in and out of `_merge`/`dedup`,
   `| None` on the four nullable fields, `r.get("completeness_score") or 0` at `main.py:206`,
   and a `mypy --strict` + `pytest` step in CI. Until a checker runs, this report's other
   findings can all silently regress.
5. **S3s.** `_is_missing(False)` for `website_live`; require a website in `cli.py:108`'s dead
   count; `str()`-coerce `scraped_at` at `dedup.py:177`.