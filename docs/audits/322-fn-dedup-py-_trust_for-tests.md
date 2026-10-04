# 322 — `_trust_for` at `dedup.py:148`: test specification

## Verdict

`_trust_for` has **zero direct tests**. The whole per-field source-trust mechanism —
the thing commit `99493b9` ("fix: per-field merge survivorship instead of whole-record
richness") was written to introduce — is exercised by exactly one indirect assertion
(`tests/test_lead_signal.py:278`), which only checks the positive direction (OSM beats
Google on `website`). Writing that test today requires **26 failures** if stated as the
property it is supposed to implement, because `_trust_for` reads the *whole record's*
source union instead of the source that contributed *that field*. The test cases below
are the ones this function still needs; nine of the fifteen fail against the current
code.

---

## Coverage today

| | |
|---|---|
| Direct tests of `_trust_for` | **0** |
| Indirect tests | `tests/test_lead_signal.py:278` (`test_trust_is_per_field_not_per_record`) |
| Import of `_trust_for` anywhere | none — `grep -rn "_trust_for"` matches `dedup.py` only |
| Suite baseline | `python3 -m unittest discover -s tests` → `Ran 28 tests ... OK` |
| Callers | exactly one: `_pick` → `rank` at `dedup.py:181` |
| Test deps available | `pytest` **ABSENT**, `hypothesis` **ABSENT**, `requests` **ABSENT** |

That last row matters: `pyproject.toml:24` lists `hypothesis>=6.100` under
`[project.optional-dependencies] dev`, but the suite's own contract
(`tests/test_lead_signal.py:1-11`: *"Run with the stdlib only, no pytest needed"*) means
every property below must be written as an **exhaustive loop over a closed domain** (3
sources × 13 tabled fields × 8 subsets = 312 assertions, ~0 ms), not as a Hypothesis
test. Writing Hypothesis tests would silently break `python3 -m unittest discover -s tests`
in a bare checkout, which is the command the file's own docstring tells you to run.

---

## Findings

### S1 — A merged row is credited with the *maximum* trust of *any* of its sources, so provenance is laundered

- **Where:** `dedup.py:148-152`, reading `_sources_of` (`dedup.py:143-145`) and the union
  written at `dedup.py:173-174`; consumed at `dedup.py:181`.
- **Breaks:** `max` over the pipe-separated union means a value is scored as if the
  highest-trust source in the row had vouched for it. After `_merge`, `source` is a union,
  so a Google-supplied `website` (trust 2, `dedup.py:100`) is reported at OSM's trust 3
  (`dedup.py:103`) purely because an OSM row also landed in the bucket. Because trust is
  element 0 of `rank`'s tuple (`dedup.py:180-185`) and `website` is in `_VOLATILE`
  (`dedup.py:114`), trust **outranks recency** — so a stale Google Maps redirect defeats
  the merchant's real website scraped months later.
- **Trigger** (verified end-to-end through `dedup()`, input verbatim):

  ```python
  [
    {"name":"Cafe Habib","phone":"+96170123456","source":"osm",
     "email":"info@cafehabib.example","scraped_at":"2026-01-01T00:00:00+00:00"},
    {"name":"Cafe Habib","phone":"+96170123456","source":"google_places",
     "website":"https://maps.example/Cafe-Habib","scraped_at":"2026-01-01T00:00:00+00:00"},
    {"name":"Cafe Habib","phone":"+96170123456","source":"google_places",
     "website":"https://cafehabib.example","scraped_at":"2026-06-01T00:00:00+00:00"},
  ]
  ```

  Actual: 1 row, `source == "google_places|osm"`,
  `website == "https://maps.example/Cafe-Habib"` (the **2026-01** placeholder).
  The contributor's true trust is 2; `_trust_for("website", merged)` returns 3.
  `without_websites.csv` / `with_websites.csv` (`main.py:210-211`) are built from this
  value — the product column is wrong and nothing logs it.
- **Quantified:** of 78 `(field, donor, other)` combinations, **26 overstate trust and 0
  understate**. `max` is one-directional, so the bug is systematically optimistic.
- **Amplifier — the enrichment pipeline makes the provenance actively false.**
  `check_websites` regexes `whatsapp` / `instagram` / `linkedin` out of the merchant's
  website HTML (`enricher.py:209-220`, written at `enricher.py:254-259`) and leaves
  `source` untouched. `main.py:209` writes that to `all_businesses.csv`; `main.py:150`
  reloads it next run as a master row. So a WhatsApp number scraped from a web page is
  persisted with `source="google_places"` and, once that row also acquires an OSM source,
  is scored at `_SOURCE_TRUST["osm"]["whatsapp"] == 3` (`dedup.py:103`) though no OSM tag
  ever supplied it.
- **Fix:** carry per-field provenance. Have `_merge` record `_field_source[field] = <the
  source that won>`, and make `_trust_for` read `record["_field_source"].get(field,
  record["source"])`. This is the change `docs/audits/095-dedup-strategy-v2.md` already
  calls for ("retain field-level provenance and a calibrated confidence"), and the gap
  `docs/audits/024-dedup-merge-data-loss.md:26` already named ("this does not retain
  per-field source attribution for other conflicts").

### S2 — Any stray whitespace or case in `source` silently zeroes all trust for the row

- **Where:** `dedup.py:145` — `return {s for s in raw.split("|") if s}`; no `.strip()`,
  no `.casefold()`. `_SOURCE_TRUST.get(src, ...)` at `dedup.py:151` then misses.
- **Breaks:** the row falls to `_DEFAULT_TRUST` (`dedup.py:111`, = 1) for *every* field,
  so a record that genuinely holds the better value loses to a worse one from a
  lower-trust source. `_pick`'s last tiebreak is `str(value)` (`dedup.py:184`), i.e.
  alphabetical order becomes the real ranking.
- **Trigger** (verified):

  ```python
  _trust_for("website", {"source": "osm"})  # -> 3
  _trust_for("website", {"source": "osm "}) # -> 1
  _trust_for("website", {"source": "OSM"})  # -> 1
  _trust_for("website", {"source": "osm | google_places"})  # -> 1
  ```

  Full pipeline, literal input (real OSM website loses to a Wikidata one):

  ```python
  a = {"name":"Cafe","phone":"+96170123456","website":"https://cafe.example",
       "source":"osm ","scraped_at":"2026-01-01T00:00:00+00:00"}
  b = {"name":"Cafe","phone":"+96170123456","website":"https://zzz-wiki.example",
       "source":"wikidata","scraped_at":"2026-01-01T00:00:00+00:00"}
  dedup([a, b])[0]["website"]  # -> 'https://zzz-wiki.example'  (OSM value lost)
  ```
- **Reachable without hand-editing:** `load_master` (`main.py:86-104`) maps only `""` to
  `None` and never calls `.strip()`, so any master CSV touched by Excel, Sheets, or a
  human gains a trailing space. Verified: a two-line CSV written with `source="osm "`
  loads as `['osm', 'osm ']` → trust `[3, 1]`. The same class is already a pinned bug for
  `resolve_country` (`tests/test_lead_signal.py:132-133`, `main.py:60-61`).
- **Fix:** `dedup.py:145` → `{s.strip().casefold() for s in raw.split("|") if s.strip()}`.

### S2 — `country` and `region` have no trust entry for any source, so ASCII collation picks a country's nationality

- **Where:** `_SOURCE_TRUST` (`dedup.py:97-110`) covers 13 fields. `country` and `region`
  are in `BusinessRecord` (`scrapers/base.py:10-11`) and in `FIELDS` (`main.py:34-35`),
  and appear in **no** source's table — so `_trust_for` returns 1 for both sides and
  `str(value)` at `dedup.py:184` decides.
- **Breaks:** `"SA" >= "LB"` is always true, so a merged cross-border record is *always*
  stamped Saudi. `main.py:184` then runs `resolve_country(r)`, which returns the valid
  code `"SA"` at `main.py:62` and never reaches its phone fallback — so dedup's
  alphabetical guess becomes authoritative, feeding `normalize_phone` (`main.py:194`) and
  `infer_region` (`enricher.py:334`). `enricher.py:331` only fills `region` when it is
  falsy, so a wrong region is permanent too.
- **Trigger** (verified end-to-end). Both rows land on phone key `+96170123456` because
  `normalize_phone` trusts the number's own country code (`dedup.py:57-60`) — precisely
  the leak `dedup.py:5-7` warns about:

  ```python
  {"name":"Al Manar Trading","phone":"+96170123456","country":"LB","region":"Beirut",
   "source":"osm","scraped_at":"2026-01-01T00:00:00+00:00"}
  {"name":"Al Manar Trading","phone":"+96170123456","country":"SA","region":"Riyadh",
   "source":"google_places","scraped_at":"2026-01-01T00:00:00+00:00"}
  ```

  Actual: 1 row, `country == "SA"`, `region == "Riyadh"` — the Lebanese row is relabelled,
  and identically in both argument orders.
- **Fix:** never let `_pick` arbitrate `country`/`region`. They are derived: compute them
  after the merge from the merged phone + address. (The deeper cause — a single key
  merging across country lines — is `docs/audits/003-dedup-identity-model.md` and
  `095-dedup-strategy-v2.md`, outside this function.)

### S3 — `linkedin` is the only contact channel no source is trusted for; unknown source names are indistinguishable from "no opinion"

- **Where:** `dedup.py:97-110` (`_SOURCE_TRUST`), `dedup.py:151`.
- **Breaks (a):** `facebook`, `instagram`, `whatsapp` all get 3 from OSM (`dedup.py:103`);
  `linkedin` gets nothing from anyone. `_validity` falls through to `return 1`
  (`dedup.py:140`), so both sides tie on trust *and* validity and `str()` decides.
  Verified: merging OSM `https://linkedin.com/in/aaa` against Google
  `https://linkedin.com/in/zzz` yields `.../zzz` in both argument orders — a contact
  channel chosen alphabetically. (`main.py:137-145` `has_any_contact` also ignores
  `linkedin` entirely, so `sales_ready.csv` never counts it — a `main.py` bug, noted only
  because it makes the missing trust entry unobservable in output.)
- **Breaks (b):** `_SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST)` collapses
  "this source has no opinion about this field" and "this source name is unknown" into
  the same `1`. A fourth scraper (the refactor in `045-scraper-layer-refactor.md`) or one
  typo in a `source=` literal silently ranks at the floor with no error. Verified:
  `_trust_for("rating", {"source": "facebook_places"}) == 1`.
- **Fix:** add `linkedin` to the OSM row; distinguish the two cases — raise (or warn) on a
  source name absent from `_SOURCE_TRUST`.

---

## The exact test cases this function still needs

All are stdlib `unittest`, class `TestTrustFor`, to be added to
`tests/test_lead_signal.py`. Import change at `tests/test_lead_signal.py:65`:

```python
from dedup import _DEFAULT_TRUST, _SOURCE_TRUST, _merge, _pick, _trust_for  # noqa: E402
```

Importing `_trust_for` is consistent with existing practice — the file already reaches for
the private `_merge` at `tests/test_lead_signal.py:65`.

### Properties (closed-domain exhaustive; `hypothesis` is unavailable)

#### P1 — `test_trust_is_invariant_to_source_order` — **passes today**
- **Input:** `for f in <the 13 keys of _SOURCE_TRUST>` × `for perm in
  itertools.permutations(("google_places", "osm", "wikidata"))`: `_trust_for(f,
  {"source": "|".join(perm)})`.
- **Assert:** the set of results for each `f` has length 1.
- **Pins:** order-dependence, the past bug named in the `_pick` docstring
  (`dedup.py:160-165`) and in `tests/test_lead_signal.py:292-295`. Necessary rather than
  decorative: `_sources_of` returns a **set** (`dedup.py:145`), so iteration order is
  `PYTHONHASHSEED`-dependent. A future rewrite to a list or a short-circuiting loop would
  make output vary between processes. Verified: no violations across 13 × 6.

#### P2 — `test_trust_is_monotone_in_the_source_set` — **passes today**
- **Input:** all 8 subsets `S` of the 3 sources and each `x not in S`; `_trust_for(f,
  {"source": "|".join(sorted(S | {x}))})` vs `_trust_for(f, {"source": "|".join(sorted(S))})`.
- **Assert:** `trust(S) <= trust(S ∪ {x})` for every `f`.
- **Pins:** documents the exact mechanism of S1 — `max` (`dedup.py:151`) means a row's
  trust is monotone in how long it has been in the pipeline, because `main.py:179-180`
  re-dedups the whole accumulated master every run. It should keep passing; if a fix makes
  it fail, provenance-aware trust is not the fix that landed.

#### P3 — `test_trust_equals_the_trust_of_the_source_that_supplied_the_value` — **FAILS, 26 of 78**
- **Input:** `for field in <13 tabled fields>` × `for donor in SOURCES` ×
  `for other in SOURCES if other is not donor`, where `donor` supplies the value and
  `other` deliberately supplies nothing for that field:

  ```python
  a = {"name":"Cafe","phone":"+96170123456","source":other,
       "scraped_at":"2026-01-01T00:00:00+00:00"}
  b = {"name":"Cafe","phone":"+96170123456","source":donor,
       field: <plausible value>, "scraped_at":"2026-01-01T00:00:00+00:00"}
  m = _merge(a, b)
  ```
- **Assert:** `_trust_for(field, m) == _trust_for(field, b)`.
- **Pins:** the whole point of commit `99493b9`, the per-field half of
  `docs/audits/002-dedup-merge-data-loss.md` / `024-dedup-merge-data-loss.md:12-14`, and
  the residual `024:26` names. `tests/test_lead_signal.py:278` covers only the happy path
  (donor *is* the most-trusted source); this covers the 26 cases where it is not.
  Representative verified failures:
  | field | donor | other | merged | actual contributor |
  |---|---|---|---|---|
  | `website` | `google_places` | `osm` | 3 | 2 |
  | `email` | `google_places` | `osm` | 3 | 1 |
  | `email` | `wikidata` | `osm` | 3 | 2 |
  | `rating` | `wikidata` | `google_places` | 3 | 1 |
  | `address` | `wikidata` | `google_places` | 3 | 2 |

  Note this test **compiles today and fails 26 times** — it is the specification that
  forces the `_field_source` fix, not a description of current behaviour.

#### P4 — `test_junk_and_missing_source_never_outrank_a_real_one` — **passes today**
- **Input:** `{"source": v}` for `v` in `(None, 0, 123, [], ["osm"], {"a":1}, "", " ", "||",
  "|", "|osm|", "zzz", "OSM")`.
- **Assert:** every result is `== _DEFAULT_TRUST` — **except** `"osm||google_places"`,
  which must still yield 3.
- **Pins:** the "junk used to normalize to `+`, which made unrelated businesses share one
  dedup key" bug (`dedup.py:34-38`, pinned at `tests/test_lead_signal.py:103-108`). Junk
  must land on the floor and stay comparable, never become a privileged value. The
  `"osm||google_places"` exception pins the `if s` filter at `dedup.py:145`: a doubled
  separator is a *malformed-but-honest* value and must not demote the real sources in it.
  Keep as two assertions (`test_hostile_source_values_fall_to_the_floor` and
  `test_repeated_separator_does_not_demote`) so a failure names its own cause.

#### P5 — `test_every_source_emitted_by_a_scraper_is_a_trust_table_key` — **passes today**
- **Input:** `ast.parse` over `scrapers/*.py`; collect every `keyword(arg="source")`
  constant. Today that is exactly `{"osm", "wikidata", "google_places"}`
  (`scrapers/osm.py:119`, `scrapers/wikidata.py:127`,
  `scrapers/google_places.py:302`).
- **Assert:** `emitted <= set(_SOURCE_TRUST)`.
- **Pins:** the S3(b) gap. A fourth scraper that forgets a `_SOURCE_TRUST` row gets no
  warning from the code and none from the suite; this is the cheapest possible guard
  against that, and it is a forward guard for `045-scraper-layer-refactor.md`.

#### P6 — `test_every_output_column_is_either_trusted_or_declared_derived` — **FAILS, 10 columns**
- **Input:** `set(main.FIELDS)` (`main.py:33-40`) minus `{"source", "scraped_at"}`.
- **Assert:** every column is in `_trust_source_fields := set().union(*_SOURCE_TRUST.values())`
  **or** in an explicit `_DERIVED_FIELDS` set the module must declare.
- **Pins:** the CSV contract (`main.py:33-40`, `scrapers/base.py:5-28`). Verified gap —
  10 columns no source is ever trusted for: `country`, `region`, `linkedin`,
  `website_live`, `completeness_score`, `lead_score`, `industry_priority`,
  `recommended_service`. Requiring an explicit *derived* declaration is what forces the
  S2(country) and S3(linkedin) decisions to be made deliberately rather than by omission.
  The five derived ones (`completeness_score`, `lead_score`, `industry_priority`,
  `recommended_service`, `website_live`) are legitimately untrusted — they are recomputed
  after dedup (`enricher.py:349-350`, `main.py:190-197`) — and must be *listed*, not
  merely absent.

#### P7 — `test_source_whitespace_and_case_do_not_change_trust` — **FAILS**
- **Input:** `{"source": v}` for `v` in `("osm", " osm", "osm ", " OSM ", "OSM", " osm",
  "osm|google_places", " osm | google_places ", "\tosm\n")`.
- **Assert:** `_trust_for("website", {"source": v}) == 3` and
  `_trust_for("email", {"source": v}) == 3` for every `v`.
- **Pins:** S2. Also pins the `load_master` contract — it must not be the CSV reader's job
  to clean `source`, but it demonstrably does not (`main.py:86-89`), and the same
  whitespace/blank-cell class is already pinned for `resolve_country` at
  `tests/test_lead_signal.py:132-133`. Add the `load_master` round-trip as a second
  assertion in the same test (see the literal in S2's trigger) so the CSV path is covered,
  not just the function.

### Examples (no clean property exists; these pin output damage)

#### E1 — `test_a_stale_website_loses_to_a_fresher_one_from_the_same_source` — **FAILS**
- **Input:** the three-record list in S1's trigger, verbatim, through `dedup()`.
- **Assert:** `len(rows) == 1` and `rows[0]["website"] == "https://cafehabib.example"`.
- **Pins:** `99493b9` + `024:26`. This is the user-visible shape of P3: `website` is in
  `_VOLATILE` (`dedup.py:114`) precisely so recency decides, but trust is tuple element 0
  (`dedup.py:181`) and outranks it. Currently returns the 2026-01 Maps URL. This is the
  single highest-value test in the file — it is the one a sales rep would notice.

#### E2 — `test_cross_border_merge_does_not_pick_a_country_by_alphabet` — **FAILS**
- **Input:** the two-record list in S2's trigger, through `dedup()`.
- **Assert:** `rows[0]["country"] == "LB"` (the phone is `+961`), and the same for
  `_merge(a, b)` and `_merge(b, a)`.
- **Pins:** the cross-border leak documented at `dedup.py:5-7` and
  `tests/test_lead_signal.py:143-145`. Currently returns `"SA"` in both orders. If the
  eventual fix is "don't merge across countries at all", assert `len(rows) == 2` instead
  and keep this test as the tripwire that the failure mode was understood rather than
  papered over.

#### E3 — `test_a_contact_channel_needs_a_trust_entry_not_a_str_tiebreak` — **FAILS**
- **Input:** `_merge({"name":"X","phone":"1","linkedin":"https://linkedin.com/in/aaa",
  "source":"osm"}, {"name":"X","phone":"1","linkedin":"https://linkedin.com/in/zzz",
  "source":"google_places"})`.
- **Assert:** the result equals `_pick("linkedin", a, b)` and *not* the `str()` winner —
  i.e. the test must assert the OSM value, matching the treatment of `facebook` /
  `instagram` / `whatsapp`.
- **Pins:** the symmetry argument behind `_SOURCE_TRUST`'s own comment (`dedup.py:93-96`,
  "trust therefore has to be per-field"). A channel that `completeness_score`
  (`enricher.py:111-115`) counts is arbitrated alphabetically. Currently returns `/zzz`.

#### E4 — `test_unknown_source_name_is_rejected_not_silently_defaulted` — **FAILS**
- **Input:** `_trust_for("rating", {"source": "facebook_places"})` and
  `_trust_for("rating", {"source": "OSM"})`.
- **Assert:** the first raises (or emits a warning); the second is treated as `"osm"`
  (subsumed by P7). Currently both silently return 1.
- **Pins:** S3(b). Deliberately asserts an *observable* outcome so the implementer picks
  raise-vs-warn deliberately rather than by default.

#### E5 — `test_a_website_scraped_contact_is_not_credited_to_the_scrape_source` — **cannot be written against today's signature**
- **Input:** the master-CSV row `enricher.py:254-259` + `main.py:209` produce for a
  Google-supplied row whose WhatsApp was regexed out of the website, after that row also
  acquires an OSM source.
- **Assert:** the value's effective trust must be the provenance of the *website scrape*,
  not `_SOURCE_TRUST["osm"]["whatsapp"]`.
- **Why it is unwritable now:** `_trust_for(field, record)` takes two arguments and there
  is no third input carrying provenance. This test is the acceptance criterion for the
  S1 fix, not an addition to today's suite — write it in the same commit as
  `_field_source`, not before.

---

## Not a bug, but worth knowing

- **Stale derived columns surviving dedup is currently masked, not harmful.** With no
  trust entries and no `_VOLATILE` membership, `lead_score` is decided by `str()`:
  a 2025 row's `lead_score=90` beats a fresh `lead_score=20` because `"90" > "20"`.
  Verified. It does not reach the output because all of `completeness_score`,
  `lead_score`, `industry_priority`, `recommended_service` are recomputed after dedup
  (`enricher.py:349-350`, `main.py:190-197`). This is a **trap for the pipeline
  reordering** in `044-main-refactor-plan.md` and `099-pipeline-stage-design.md`: add one
  stage that reads dedup output before recomputation and a stale `lead_score` silently
  ships. P6 is what makes that reordering safe.
- **`_trust_for` is unreachable for missing values.** `_pick` early-returns at
  `dedup.py:168-171`, so `_trust_for` only ever sees non-missing values, which makes
  `_validity`'s `return -100` (`dedup.py:122-123`) dead code from this path. No test
  should assert the `-100` branch.
- **The table is frozen to the current three sources, and that is fine.** `lat`/`lon`
  trust 3 for Google (`dedup.py:99`) is defensible only because Places is the sole
  coordinate source in practice (wikidata.py emits `lat`/`lon` too, at trust 1 — see the
  P3 table). Nothing in a unit test can keep that honest; see below.
- **Field 5 of `_SOURCE_TRUST` values: `{1, 2, 3}`.** The scale is consistent and there
  are no inversions (Google ≥ OSM ≥ Wikidata for every shared field), so the table's
  *relative* ordering is defensible. Only its absolute attribution is wrong.
- **Thread-safety is not a concern and not testable-and-needed:** `_SOURCE_TRUST` is
  read-only module state, mutated by nobody. The 40-thread `check_websites`
  (`enricher.py`) never calls `_trust_for`.

## What genuinely cannot be tested here

1. **Whether `3 / 2 / 1` are the correct weights.** Pure domain judgement about Google
   Places, OSM tags and Wikidata. A test can assert self-consistency (Google ≥ OSM for
   every shared field — it does hold) but never correctness. Only a human sign-off or a
   golden fixture over real merged rows settles it.
2. **Whether `max` is the right aggregator across sources.** Provenance-aware is clearly
   better (P3), but "latest source wins" or "count of independent corroborations" are
   also defensible designs. This is a decision to make, then pin — not a fact to test.
3. **The real frequency of each `source` string.** Settling whether any scraper ever emits
   a compound or padded value needs the live Overpass / Wikidata / Places APIs, which
   BRIEF rule 2 forbids and which would be non-deterministic if allowed. Needs a
   recorded fixture (the `032-fixtures-offline-replay.md` design).
4. **Whether the S1 damage fires at production scale.** `data/all_businesses.csv` is
   gitignored and absent, so the frequency of "OSM row without a website + two Google runs
   in one phone bucket" is unmeasurable here. E1 is a demonstration, not an impact
   estimate.
5. **Coordinate quality.** `lat`/`lon` trust 3 asserts Places' coordinates are better than
   Wikidata's, which is only checkable by comparing against ground truth we do not have.
6. **Property-based fuzzing with Hypothesis.** `hypothesis` is absent (verified) and the
   suite promises stdlib-only (`tests/test_lead_signal.py:1-11`). P1–P4 are therefore
   written as exhaustive loops over 3 sources × 13 fields × 8 subsets. If `045` or `038`
   ever adds a CI step that installs the `dev` extra, convert P2 and P4 to Hypothesis and
   widen the source alphabet with untrusted strings — until then, exhaustive is the honest
   choice, and the loops already cover 100% of the reachable domain.

## Recommended order of work

1. **P3 + E1 together**, in one commit: they are the specification for the same S1 fix.
   Add `_field_source` to `_merge`, make `_trust_for` provenance-aware, watch 26 → 0 and
   the Maps URL → the real one.
2. **P7 + S2 fix** (`dedup.py:145`, one line: `.strip().casefold()`). Cheapest
   correctness win in the function; also add the `load_master` round-trip assertion.
3. **E2 + P6**: decide whether `country`/`region` are mergeable at all, then declare the
   derived-field set so the omission is deliberate. P6 then guards `045`'s new sources.
4. **E3 + P4 + P5**: cheap, all passing-or-one-line, and they close the `linkedin` gap
   plus the "fourth scraper with no trust row" gap before `045` lands.
5. **E4**: last, because it is an interface decision (raise vs warn), not a defect.
6. Leave **E5** out of the current suite; it is the acceptance test for step 1's commit.