# 314 — `_sources_of` (dedup.py:143) test gap

## Verdict

`_sources_of` is three lines, is correct on every input I could construct, and has
**zero direct tests**. `tests/test_lead_signal.py` covers it exactly once, indirectly,
via `_merge(GOOGLE, OSM)["source"] == "google_places|osm"`
(`tests/test_lead_signal.py:316-319`) — the single friendliest case. Underneath that,
two live defects live in code `_sources_of` feeds and nothing guards them:
`lead_score` re-parses `source` with a substring test and awards the +5
multi-source bonus to records that have one source (`enricher.py:316`), and
`_trust_for`'s `max()` over the *accumulated* source set permanently freezes `website`
— the one field that is both `_VOLATILE` and has a cross-source trust conflict
(`dedup.py:148-152`, `dedup.py:114`). Both are silent wrong-results bugs in the two
columns sales ranks by. The tests below are written to be pasted into
`tests/test_lead_signal.py`; **two of them fail against the current code** and are
marked `xfail`-style with the exact reason.

Suite state: 28 tests, `python3 -m unittest discover -s tests` → `OK` (0.021s).

---

## Findings

### S1 — `lead_score`'s multi-source bonus is a substring test, not a parse, so single-source records score as corroborated

- **Where:** `enricher.py:316` (`if "|" in str(record.get("source") or ""): score += 5`)
  versus the correct parser it ignores, `dedup.py:143-145`.
- **Breaks:** The codebase has two independent parsers for the same field and they
  disagree. `"|" in s` is true for any string containing a pipe, including strings
  with **zero or one** real source. Verified:

  | `source` | `_sources_of` | `lead_score` delta |
  |---|---|---|
  | `"osm"` | `{'osm'}` | **0** |
  | `"\|osm"` | `{'osm'}` | **+5** |
  | `"\|"` | `set()` | **+5** |
  | `"\|\|"` | `set()` | **+5** |
  | `"n/a\|"` | `{'n/a'}` | **+5** |

- **Trigger:** `"\|osm"` is not hypothetical — it is exactly what the pre-`99493b9`
  `_merge` wrote. That version ran `merged[key] = "|".join(sorted(set(av.split("|")) | set(bv.split("|"))))`
  with no empty-segment filter (`git show 99493b9^:dedup.py`, lines 92-94), and the
  empty string sorts first, so merging `{"source":"osm"}` with `{"source":""}`
  produced `"|osm"`. Those cells are in the cumulative master right now, and
  `main.py:150` reloads that master into every subsequent run.
- **Fix:** `enricher.py:316` → `if len(_sources_of(record)) > 1: score += 5`.

### S1 — Accumulated sources escalate `_trust_for` and permanently freeze `website`, the field the whole pitch depends on

- **Where:** `dedup.py:148-152` (`best = max(best, ...)` over `_sources_of(record)`),
  `dedup.py:114` (`_VOLATILE`), `dedup.py:174` (per-field provenance collapsed into one
  record-level string).
- **Breaks:** `_pick` serialises the whole record's source set into a single
  `source` string. `_trust_for` then reads that set as if it described *where the
  current value came from*. Trust is therefore **stamped permanently onto the merged
  record** and can never be un-learned. `website` is the only field exposed, because
  it is the only field that is both `_VOLATILE` (so recency is supposed to decide) and
  has a non-equal cross-source trust. Verified across all 12 trust fields:

  ```
  field           google osm wiki  conflict?  volatile?
  website             2    3    2  YES       volatile     <-- unique
  rating              3    1    1  -          volatile
  review_count        3    1    1  -          volatile
  ```

- **Trigger:** once OSM has been seen even once, `website` trust is `max(2,3)=3`
  forever, so no later Google observation can displace it regardless of freshness or
  validity. Verified:

  ```
  _merge(
    _merge({"website":"https://maps.example", "source":"google_places", "scraped_at":"2026-01-01T00:00:00"},
           {"website":"http://osm-old.example", "source":"osm",          "scraped_at":"2026-01-01T00:00:00"}),
    {"website":"https://new-real.example", "source":"google_places",     "scraped_at":"2026-09-01T00:00:00"},
  )["website"]  ==  "http://osm-old.example"
  ```

  It compounds because the master is reloaded and re-merged every run
  (`main.py:150`, `main.py:179-180`), so the stale value is re-asserted each time.
- **Blast radius:** `website` + `website_live` drive the Tier-1
  `"Website rebuild + maintenance"` pitch (`pitch_recommender.py:31-36`, `:57-58`)
  and the +20 dead-site `lead_score` (`enricher.py:303-304`). Both columns sales
  sorts by.
- **Test gap that let it through:** `test_volatile_field_prefers_the_fresher_observation`
  (`tests/test_lead_signal.py:303-309`) exercises `rating` with
  `google_places` on **both** sides, where trust ties at 3 and the recency
  mechanism is trivially reachable. No test asserts that `_VOLATILE` can ever win
  for `website` — because it cannot.
- **Fix:** `_trust_for` needs the *value's* origin, not the record's accumulated set
  (audits `024:26`, `095:53`, `033:131` already require per-field provenance). Cheap
  interim: make `_VOLATILE` recency outrank trust in `rank()` (`dedup.py:179-185`).

### S2 — The empty-segment filter at `dedup.py:145` is load-bearing for data already in the field, and has no coverage

- **Where:** `dedup.py:145` — `return {s for s in raw.split("|") if s}`.
- **Breaks:** Not on today's code — it is correct. The point is that it is
  **undefended**. Audit `028:153` and audit `021:70` both describe the un-filtered
  form (`set(raw.split("|"))`) as the thing to watch out for. Reverting to it is a
  one-token "simplification" that silently re-corrupts every legacy `"|osm"` cell in
  a cumulative master that cannot be rebuilt without re-scraping.
- **Trigger:** today `_merge({"source":"osm"}, {"source":""})["source"] == "osm"`
  (correct) but the old expression `"|".join(sorted({"osm"} | {""})) == "|osm"`.
- **Test gap:** `test_empty_string_does_not_beat_a_real_value`
  (`tests/test_lead_signal.py:286-290`) covers the `_is_missing` short-circuit for
  `website` — a *different* mechanism at `dedup.py:168-171`. It never touches
  `source`, so the `if s` filter has zero assertions pointing at it.

### S2 — `_sources_of` is not a normaliser; it is only reachable on the union path

- **Where:** `dedup.py:167-171` returns `av`/`bv` **verbatim** when `_is_missing`, and
  `dedup.py:210` / `dedup.py:218` store `dict(record)` on first sighting with no
  `_merge` at all.
- **Breaks:** the parser and the serialiser are alternatives, not inverses. A dirty
  string is cleaned only if it happens to be unioned with another `source` in the
  same merge. Verified:

  ```
  _merge({"source":"osm|"}, {"name":"A"})["source"]  ==  "osm|"      # passthrough, unnormalised
  _merge({"source":"osm|"}, {"source":"|"})["source"] ==  "osm"      # union path, cleaned
  dedup([<single record, source="osm|">])[0]["source"] == "osm|"      # never reaches _sources_of
  ```

  Association breaks in exactly this degenerate corner — over the 6-value domain
  `{"osm","google_places","wikidata","","|","osm|"}`, 2 of 216 triples are
  non-associative (`src(src("|","|"),"osm|") == "osm|"` vs
  `src("|",src("|","osm|")) == "osm"`).
- **Test gap:** the fixed-point property `"|".join(sorted(_sources_of(m))) == m["source"]`
  is not asserted anywhere, and **it fails today**.

### S2 — No whitespace or case normalisation, so a hand-edited cell silently collapses every field to `_DEFAULT_TRUST`

- **Where:** `dedup.py:144-145`.
- **Breaks:** `_sources_of({"source":"osm | google_places"})` returns
  `{'osm ', ' google_places'}`. Neither matches a `_SOURCE_TRUST` key
  (`dedup.py:97-110`), so `_trust_for` returns `_DEFAULT_TRUST = 1` for **every**
  field — including `email`, `whatsapp`, `instagram`, `facebook`, where OSM is the
  *best* source at 3. Verified: `_trust_for("email", {"source":"OSM"}) == 1` vs
  `_trust_for("email", {"source":"osm"}) == 3`. Re-serialised output is
  `' google_places|osm |wikidata'` — padded cells, and a violation of the declared
  contract `osm|google_places` (audit `049:50`).
- **Trigger:** one space typed into the `source` column of `all_businesses.csv`.
  These files are opened and edited by the sales team (audit `063`), and
  `cli.py:120` already does an exact-match `Counter` over the raw string.
- **Precedent:** `resolve_country` (`main.py:50-74`) has exactly this normalisation
  — strip, uppercase, alias table — and **is** tested
  (`tests/test_lead_signal.py:135-141`). `_sources_of` has none.

### S3 — `_SOURCE_TRUST`'s keys and the three scraper constants never meet in code

- **Where:** `dedup.py:97-110` vs `scrapers/osm.py:119` (`source="osm"`),
  `scrapers/google_places.py:302` (`source="google_places"`),
  `scrapers/wikidata.py:127` (`source="wikidata"`).
- **Breaks:** they agree today. A typo or a fourth scraper on either side silently
  degrades that record to `_DEFAULT_TRUST = 1` for all fields and **no test fails**,
  because a test written from the same constant repeats the same typo. This is the
  one gap a normal unit test structurally cannot close; it needs a test that reads
  the scraper modules (E6 below).

---

## The test cases this function still needs

All go in `tests/test_lead_signal.py` as a new class. The existing file is stdlib
`unittest` with a `requests` stub (`tests/test_lead_signal.py:6`, `:17`, `:23-63`);
add to that import line at `tests/test_lead_signal.py:65`:

```python
from dedup import (  # noqa: E402
    _DEFAULT_TRUST, _SOURCE_TRUST, _merge, _sources_of, _trust_for,
    dedup, normalize_phone,
)
```

### Properties (preferred — these exist and are cheap to state exhaustively)

**P1 — `test_sources_union_is_never_lost`** · *passes today; the core guard*
- **Property:** `_sources_of(_merge(a, b)) == _sources_of(a) | _sources_of(b)` for every
  pair of `source` values, including degenerate ones.
- **Input:** exhaustive `itertools.product` over
  `["osm","google_places","wikidata","","||","|osm","osm|","osm|osm","OSM",None,123]`
  (11 values → 121 pairs), omitting the key entirely when the value is `None`.
- **Assert:** the set equality. I ran this domain: **0 violations.**
- **Pins:** provenance loss — audit `002:336`, `024:26`, `034:33`. This is the single
  invariant that says "merging never erases the fact that a source saw this business."

**P2 — `test_source_field_is_a_fixed_point_of_the_parser`** · **FAILS today**
- **Property:** for every merge output `m`, `"|".join(sorted(_sources_of(m))) == m["source"]`.
- **Input:** `_merge({"name": "A", "source": "osm|"}, {"name": "A"})`.
- **Assert (wanted):** `m["source"] == "osm"`. **Assert (actual):** `"osm|"`.
- **Pins:** S2 — `dedup.py:167-171` bypasses the normaliser.
- **Also assert the first-sighting path:** `dedup([{"name":"Solo","address":"A, Beirut",
  "country":"LB","phone":"70123456","source":"osm|","scraped_at":"2026-01-01T00:00:00"}])[0]["source"]`
  should be `"osm"`; today it is `"osm|"` (`dedup.py:210`).

**P3 — `test_source_merge_is_commutative_and_idempotent_across_runs`** · *passes today*
- **Property A (commutative):** `_merge(a,b)["source"] == _merge(b,a)["source"]`.
- **Property B (idempotent):** `_merge(m, a)["source"] == m["source"]` where
  `m = _merge(a, b)["source"]`.
- **Input:** `a={"source":"google_places"}`, `b={"source":"osm"}`;
  assert `_merge(m, {"source":"osm"})["source"] == "google_places|osm"`.
- **Pins:** audit `013` (idempotency), `028:45`, `032:63`. Property B is *the*
  cumulative-master property: `main.py:150` + `main.py:179-180` re-merge the master
  into itself every single run. A naive `str.split`-without-set implementation fails B
  with `"google_places|osm|osm"`.
- **Note:** I verified A holds and B holds over the degenerate domain; **A does not
  imply associativity** — 2/216 triples fail, both involving `"|"`. Add
  `test_source_merge_is_associative` over
  `["osm","google_places","wikidata","","|","osm|"]` if you want that pinned too;
  it will fail until P2 is fixed.

**P4 — `test_degenerate_source_never_emits_an_empty_segment`** · *passes today; defends the `if s` filter*
- **Property:** for every pair in `["", None, "|", "||", "|osm", "osm|", "osm||google_places"]`,
  `"".split("|") == []` and `"||" not in _merge(a,b)["source"]`.
- **Assert the exact old regression** as a named case:
  `_merge({"name":"A","source":"osm"}, {"name":"A","source":""})["source"] == "osm"`,
  with `assertNotEqual(..., "|osm")`.
- **Pins:** S2 — the pre-`99493b9` bug. `git show 99493b9^:dedup.py` lines 92-94
  produced `"|osm"`; `"|".join(sorted({"osm",""}))` is `"|osm"` because `""` sorts
  first. Also pins audits `028:153`, `021:70`.

**P5 — `test_unknown_source_name_cannot_inflate_trust`** · *passes today; hardening*
- **Property:** if `src not in _SOURCE_TRUST`, then `_trust_for(field, {"source": src}) == _DEFAULT_TRUST`.
- **Input:** loop `src` over `["OSM","osm ","Goggle_Places","legacy_csv","osm,google_places",""]`,
  `field` over `sorted({f for s in _SOURCE_TRUST.values() for f in s})`.
- **Assert:** every result equals `_DEFAULT_TRUST` (1). Verified: `"OSM"` yields 1
  where `"osm"` yields 3.
- **Pins:** S2 (whitespace/case). This is the "a human typo cannot promote a record
  to OSM-grade trust" invariant. It passes today only because
  `_SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST)` (`dedup.py:151`) has a
  default — nothing asserts that default exists.

**P6 — `test_volatile_recency_can_override_accumulated_source_trust`** · **FAILS today**
- **Property:** for a `_VOLATILE` field, a strictly fresher observation from a
  lower-trust source must be able to win.
- **Input:**
  ```python
  g_old  = {"name":"A", "website":"https://maps.example", "source":"google_places", "scraped_at":"2026-01-01T00:00:00"}
  o      = {"name":"A", "website":"http://osm-old.example", "source":"osm",         "scraped_at":"2026-01-01T00:00:00"}
  g_new  = {"name":"A", "website":"https://new-real.example", "source":"google_places", "scraped_at":"2026-09-01T00:00:00"}
  ```
- **Assert (wanted):** `_merge(_merge(g_old, o), g_new)["website"] == "https://new-real.example"`.
  **Assert (actual):** `"http://osm-old.example"`.
- **Pins:** S1 — the frozen-website bug. Companion assertion documenting the cause:
  `_trust_for("website", {"source":"google_places|osm"}) == 3` while
  `_trust_for("website", {"source":"google_places"}) == 2`, so `rank()` never
  reaches the recency term at `dedup.py:183`.

**P7 — `test_multi_source_bonus_requires_two_real_sources`** · **FAILS today**
- **Property:** the `lead_score` corroboration bonus is awarded iff
  `len(_sources_of(record)) > 1`.
- **Input:** `base = {"name":"A", "industry_priority":"low"}` (zero other score
  contributions), then `source` over `["osm","google_places|osm","|osm","|","||","n/a|"]`.
- **Assert:** `lead_score(dict(base, source="google_places|osm")) == 5` and
  `lead_score(dict(base, source=s)) == 0` for every `s` in
  `["osm","|osm","|","||","n/a|"]`.
- **Pins:** S1 — the substring-vs-parse divergence. Verified actuals:
  `"|osm"→5`, `"|"→5`, `"||"→5`, `"n/a|"→5`, `"osm"→0`.

### Examples (exact strings; no clean property to state)

**E1 — `test_legacy_corrupted_master_row_self_heals`**
- **Input:** `_merge({"name":"A","source":"|osm"}, {"name":"A","source":"google_places"})`.
- **Assert:** `["source"] == "google_places|osm"`; and a second merge with
  `{"source":"google_places"}` still yields `"google_places|osm"` (stable).
- **Pins:** the exact string the shipped pre-`99493b9` code wrote into the
  cumulative master. This is the migration test for data already in the field.
  Verified: passes today.

**E2 — `test_source_survives_a_merge_against_a_record_with_no_source_key`**
- **Input:** `_merge({"name":"A","source":"google_places|osm"}, {"name":"A"})`.
- **Assert:** `["source"] == "google_places|osm"`.
- **Pins:** the `_is_missing` short-circuit at `dedup.py:167-171`. Passes today, but
  only as a side effect of a branch written for `website` — nothing states that a
  record lacking `source` must not erase one that has it. Audit `031` test #9
  (`test_merge_source_non_string_attribute_error`) asked for this and it still does
  not exist.

**E3 — `test_source_names_are_the_three_scraper_constants`**
- **Input:** literal, deliberately *not* derived from `_SOURCE_TRUST`:
  `assertEqual(sorted(_SOURCE_TRUST), ["google_places", "osm", "wikidata"])`.
- **Pins:** S3. A fourth scraper (`cli.py:56` builds a `registry`, so adding one is a
  one-liner) must fail this until its trust row is written. Pair with the three
  `file:line` constants in the assertion message.

**E4 — `test_source_merge_does_not_prefix_a_pipe`** (the named instance of P4)
- **Input:** `_merge({"name":"A","source":"osm"}, {"name":"A","source":""})`.
- **Assert:** `["source"] == "osm"`; explicitly `assertNotEqual(..., "|osm")`.
- **Pins:** the single most likely regression, because `"|osm"` looks plausible.

**E5 — `test_dedup_first_sighting_is_not_normalised`** (characterisation test)
- **Input:** `dedup([{"name":"Solo","address":"A, Beirut","country":"LB",
  "phone":"70123456","source":"osm|","scraped_at":"2026-01-01T00:00:00"}])`.
- **Assert:** `out[0]["source"] == "osm|"` **today**, with a comment naming
  `dedup.py:210`. Flip to `"osm"` once P2 is fixed.
- **Why keep it:** a record that is its own dedup key never reaches `_sources_of` at
  all. Without this test the hole is invisible; with it, the hole is documented and
  greppable.

**E6 — `test_scraper_source_literals_match_the_trust_table`** (the only test that closes S3)
- **Method:** read the three scraper files from disk and regex the emitted constants;
  do not import them (importing `google_places.py` pulls in `requests`).
  ```python
  import re
  from pathlib import Path
  root = Path(__file__).resolve().parent.parent
  found = set()
  for rel in ("scrapers/osm.py", "scrapers/google_places.py", "scrapers/wikidata.py"):
      found |= set(re.findall(r'source="([a-z_]+)"', (root / rel).read_text()))
  self.assertEqual(found, set(_SOURCE_TRUST))
  ```
- **Assert:** the emitted set equals the trust-table keys exactly — no more, no fewer.
- **Pins:** S3. Verified to pass today: emitted `{osm, google_places, wikidata}` ==
  `_SOURCE_TRUST` keys.

---

## What genuinely cannot be tested here, and why

1. **Whether `osm|google_places` means two independent confirmations of one business.**
   `_sources_of` counts strings. It cannot know whether the merge that produced them
   was correct. Two branches of a chain sharing a chain hotline merge on phone
   (`dedup.py:207`) and union their sources, earning a +5 "corroborated" bonus for two
   locations that are one brand — and per audit `057` chain-hotline collapsing is a
   known false-merge class. This needs audit `095`'s pair-scoring plus a stable
   `entity_id` before it is a fixture assertion, not a unit test. **Do not write a
   test that asserts `_sources_of` produces "correct" provenance — it cannot.**
2. **Whether the trust numbers (3/3/2/1) are correct.** `dedup.py:97-110` is an
   assertion about Google's and OSM's relative data quality, not a computation. The
   only honest test is calibration against labelled sales outcomes (audit `060`), which
   needs a feedback loop that does not exist. P5 pins only the *floor* (unknown source
   → 1), which is the part that is checkable.
3. **Set iteration order.** `_sources_of` returns an unordered `set` (`:145`). No test
   may assert anything about its iteration order; `"|".join(sorted(...))` at
   `dedup.py:174` is the only stable serialisation. Stated here so nobody writes an
   order-dependent assertion and then "fixes" the function by returning a list.
4. **Concurrency / thread safety.** `_sources_of` is pure, has no state, and is called
   only from `_pick` and `_trust_for`. `dedup()` runs single-threaded at `main.py:180`,
   strictly before `enrich()`'s 40-thread pool at `main.py:187`. There is no
   concurrency property to assert here; a race test would be testing nothing.
5. **Scale.** `k <= 3` sources and an `O(k log k)` sort. A 500k-record performance test
   measures `write_csv`, not this function.
6. **Per-source freshness.** `_recency` (`dedup.py:155-156`) is per-*record*, so nothing
   can express "OSM saw this in 2024, Google saw it last week". Not a `_sources_of`
   defect — it is the absence of per-source timestamps, i.e. the migration in audits
   `033:131` and `095:99`. Untestable until that lands; asserting it now would be
   asserting a field that does not exist.

---

## Recommended order of work

1. **P7** — one-line fix at `enricher.py:316` (`len(_sources_of(record)) > 1`), then
   the test. Cheapest real wrong-results fix in this file.
2. **P1, P3, P4, E1, E4** — all pass today. Add them unchanged; they are the regression
   net for the `99493b9` rewrite and they cost nothing.
3. **E3, E6** — the two tests that make S3 detectable, then S3's fix if they go red.
4. **P6** — needs a design decision before a test can be written honestly: either
   reorder `rank()` (`dedup.py:179-185`) to put recency above trust for `_VOLATILE`
   fields, or implement per-field provenance as audits `024`/`095`/`033` require.
   Whichever is chosen, land the test in the same commit as the fix so the frozen
   `website` cannot come back.
5. **P2, E5, P5** — normalise on every path, not just the union path. Make `_pick` run
   `_sources_of` on its `_is_missing` short-circuit results too, and route
   `dedup.py:210`/`218` first-sightings through the same serialiser. Land P2 and E5
   together so the passthrough hole is closed by the same change that documents it.
6. **S2 whitespace/case** — mirror `resolve_country` (`main.py:60-67`): `.strip()` and
   `.lower()` each segment in `_sources_of`, then lowercase-normalise. Add the
   `'OSM'`/`'osm | google_places'` cases to P5.
