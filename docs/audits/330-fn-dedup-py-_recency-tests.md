# 330 — `_recency` (dedup.py:155) test-coverage audit

**Lens:** tests. **Tree:** `HEAD=99493b9`, `dedup.py`/`main.py`/`tests/` unmodified vs HEAD.
**Sibling lenses on the same function:** `328` (correctness), `329` (edge cases), `331`
(concurrency), `332` (contract), `334` (deps), `335` (ops). This report does not restate
their findings; it covers **what tests are missing**, and adds two failure modes they did
not reach (mixed *suffix conventions* in the same second, and the `str(value)` tiebreak
column that makes recency untestable as currently written).

---

## Verdict

`_recency` has **zero effective test coverage**. Proven by mutation, not by inspection:
replacing its body with `lambda rec: ""`, or **deleting it from the `rank()` tuple at
`dedup.py:183` altogether**, leaves all 28 tests in `tests/test_lead_signal.py` green.
The single test that looks like it covers recency —
`TestMergeSurvivorship.test_volatile_field_prefers_the_fresher_observation`
(`tests/test_lead_signal.py:303-309`) — is **vacuous**: its fixture is arranged so the
`str(value)` tiebreak at `dedup.py:184` independently produces the asserted answer, so
recency is never the deciding column. The remedy is a **fixture redesign, not more
assertions**: any test of `_recency` must choose ratings whose `str()` ordering points
*opposite* to their recency ordering (`4.9` old vs `1.0` new). That single change takes
the kill rate from 0/2 mutants to 4/4.

---

## Coverage ledger

`_recency` appears **0 times** in `tests/`. Its only indirect exercise is
`tests/test_lead_signal.py:303-309`.

### Mutation evidence

| Mutation to `_recency` | Result on the whole suite |
|---|---|
| `lambda rec: ""` (no-op) | **28 passed, 0 failed** |
| `lambda rec: "MUTANT"` (constant) | **28 passed, 0 failed** |
| `dedup.py:183` → `_recency(rec) if … else ""` replaced by literal `""` (function deleted) | **28 passed, 0 failed** |

Per-test, isolating `test_volatile_field_prefers_the_fresher_observation`:

| Mutant | existing fixture (`tests/test_lead_signal.py:304-307`) | redesigned fixture (below) |
|---|---|---|
| `→ ""` | SURVIVED | **KILLED** |
| `→ "z"` (inverted) | SURVIVED | **KILLED** |
| `→ min(key, "0000")` | — | **KILLED** |
| `→ str(rec.get("rating"))` | — | **KILLED** |

The existing fixture cannot distinguish recency from the tiebreak because
`str(3.0) < str(4.8)`, i.e. the tiebreak column and the recency column **agree**.

---

## Findings

### S1 — The one recency test cannot fail; the ranking column can be deleted and CI stays green

- **Where:** `dedup.py:183` (consumed), `dedup.py:179-187` (the `rank` tuple),
  `tests/test_lead_signal.py:303-309` (the vacuous test).
- **Breaks:** `rank()` is a 4-tuple ordered `trust > validity > recency > str(value)`
  (`dedup.py:180-185`). A test that only sets trust/validity equal and varies recency is
  *not* testing recency unless the last column is forced to disagree. In
  `tests/test_lead_signal.py:303-309` all four columns happen to point the same way:
  trust `3`=`3` (`_SOURCE_TRUST`, `dedup.py:99`), validity `3`=`3`
  (`_validity("rating", 3.0)` and `(…, 4.8)` both `3`, `dedup.py:134-139`), recency
  `2026-09…` > `2026-01…`, and `str("4.8") > str("3.0")`. Deleting the recency column
  changes nothing. So the suite certifies a guarantee — "volatile fields prefer the
  fresher observation" — that it does not actually verify, which is exactly the guarantee
  a future simplification of `rank()` will be assumed to preserve.
- **Trigger (verified):** patch `dedup.py:183` to `"" if field in _VOLATILE else ""`.
  Result: `ran=28 failures=0 errors=0`.
- **Fix:** rebuild the fixture so the tiebreak column is *provably* not the decider, and
  assert recency in both merge directions. See **T-P1** / **T-E1** below. Add a comment
  naming the invariant so the next author does not re-introduce agreeing values.

### S2 — `dedup.py:184`'s `str(value)` tiebreak is a lexicographic compare on non-strings, and on non-volatile fields it is the *only* decider

- **Where:** `dedup.py:183-185`. Recency is consulted **only** when
  `field in _VOLATILE` (`dedup.py:114`), so for every other field both sides receive the
  literal `""` and the `str(value)` column decides unconditionally.
- **Breaks (two concrete effects, both verified):**

  1. **Numerically smaller wins on numeric fields.** `review_count` is volatile, so
     recency is consulted but *ties* whenever the two stamps are equal — and
     `utc_now_iso()` (`httpclient.py:220-229`) stamps **once per scraper per run**
     (`scrapers/osm.py:32`, `scrapers/wikidata.py:67`,
     `scrapers/google_places.py:173`), so every record from one source in one run shares a
     byte-identical stamp. Then `str("9") > str("10")` decides:
     `_merge({… "review_count":10 …}, {… "review_count":9 …})["review_count"] == 9`.

  2. **Alphabetical collapse on enum fields, always.** `industry_priority` is not
     volatile, so recency is `""` on both sides unconditionally and the merged value is
     the alphabetically greatest one: `high` vs `low` → **`low`**; `high` vs `medium` →
     **`medium`**. `main.py:204` gates `sales_ready.csv` on
     `industry_priority in ("high", "medium")`, so a reclassification that would promote a
     lead is silently reverted.
- **Why S2 and not S1, stated honestly:** the derived enums are recomputed before any CSV
  is written (`main.py:190-191`, `main.py:197`, `enricher.py:349`), so effect (2) is
  **inert in today's five outputs** and effect (1) needs an exact recency tie, which
  requires two records from one source in one run — blocked inside `google_places` by
  `seen_ids` (`scrapers/google_places.py:181-182`) and not produced by `osm`, which
  hard-sets `rating=None`/`review_count=None`. Both become live the moment a source emits
  a rating without deduping its own duplicates, or the moment `--replay` fixtures are
  added. Rated S2 because it is a real correctness defect in a load-bearing column that
  is currently masked downstream, not a proven wrong number in a shipped file.
- **Trigger:** `_merge` of two master rows for one business, `industry_priority="high"`
  (newer) and `"low"` (older) → `"low"`.
- **Fix:** make the tiebreak type-aware (`(0, float(v))` for numeric fields,
  `(1, str(v))` otherwise), or extend `_VOLATILE` to the fields whose value changes
  between runs. **Test first** — this is the one defect in the file that a green suite
  would otherwise let through.

### S2 — `scraped_at` is merged by one rule (`max`, `dedup.py:177`) and ranked by a different rule (`_recency`, `dedup.py:156`); the two disagree on blanks and one of them raises

- **Where:** `dedup.py:156` (`or ""`, no `.strip()`) vs `dedup.py:88-89`
  (`_is_missing` **does** `.strip()`) vs `dedup.py:176-177` (`max(av, bv)` on raw values,
  no `str()` coercion).
- **Breaks (verified, three distinct symptoms):**

  1. **The surviving value's timestamp is thrown away.** `_is_missing(" ")` is `True`
     (`dedup.py:88-89`), so at `dedup.py:168-171` the `" "` stamp is treated as *absent*
     and the other record's `""` is written to `scraped_at`. But `_recency(" ")` is
     `" "`, which beats `""`, so the `" "` record wins the volatile `rating`. Result:
     `{"rating": 4.9, "scraped_at": ""}` — the output claims the rating was never
     observed. `load_master` preserves the space: it maps only `""` → `None`
     (`main.py:87-89`), and `scraped_at` is in neither `_FLOAT_FIELDS` nor `_INT_FIELDS`
     (`main.py:43-44`), so no cast normalises it.
  2. **The recency key is not round-trip stable.** `_merge({…"scraped_at": 0…},
     {…"scraped_at": None…})` stores the **int** `0` (because `_is_missing(0)` is `False`,
     `dedup.py:86-90`); `_recency` then reads that cell as `""`. After
     `write_csv` → `load_master` the same record's key is the **string** `"0"`. So the
     ranking key for one record changes between run N and run N+1 with no input change:
     `"" → "0"`.
  3. **The `max()` arm raises on types `_recency` swallows.** `_recency` coerces via
     `str()`; `dedup.py:177` does not. Verified `TypeError: '>' not supported between
     instances of 'str' and 'int'` for `scraped_at=0` and `=1767225600`, and
     `…'str' and 'bytes'` for `scraped_at=b"2026-10-03T12:00:00+00:00"`. This is the
     crash `020-error-handling-partials.md:27` predicted and
     `031-test-suite-design.md:236` already specified as
     `test_merge_scraped_at_mixed_type_comparison`; it has still never been written.
- **Trigger:** a master CSV cell containing a space, `0`, or a bytes-typed value.
  `0` and `bytes` are not reachable from `main.py` today (`load_master` yields strings) —
  they are armed for the first writer that stores an epoch integer, which is what audits
  033/034 propose. The **space** case *is* reachable today via a hand-edited Drive cell.
- **Fix:** route `dedup.py:177` through the same normaliser as `dedup.py:156`, and make
  both use `isinstance(str)` + `.strip()`. **Tests: T-P3, T-P4, T-E4.**

### S2 — Order-isomorphism breaks on mixed *suffix conventions* in the same second (not covered by `329`, which tested mixed *offsets*)

- **Where:** `dedup.py:156` → `dedup.py:183` → `dedup.py:177`.
- **Property:** when `_recency` is used as an ordering key, its string order must equal
  chronological order. This holds only within one fixed textual format. `329` S2 showed
  it failing for `+03:00` vs `+00:00`. It **also** fails for `Z` vs `+00:00` when the
  `Z` form has no fractional part — and `httpclient.py:229` emits a
  no-fractional-part stamp whenever `microsecond == 0`, because `datetime.isoformat()`
  omits the fraction then.
- **Trigger (verified):**

  ```
  old = "2026-10-03T12:34:56Z"                 # 12:34:56.000000Z
  new = "2026-10-03T12:34:56.789012+00:00"     # 12:34:56.789012Z  (0.79 s NEWER)
  _merge(...)["rating"] -> 4.9                  # the OLDER record won
  _merge(...)["scraped_at"] -> "2026-10-03T12:34:56Z"
  ```

  `'.'` (0x2E) < `'Z'` (0x5A) at index 19, so the `Z` stamp outranks every fractional
  stamp in its own second. The result is **order-independent** (both merge directions
  give `4.9`), so this is a *deterministic* wrong answer, not a race.
- **Do not over-fix this one:** with a single suffix convention, variable width is
  **correct** — `'.'` (0x2E) > `'+'` (0x2B), so
  `"…T12:34:56+00:00"` < `"…T12:34:56.789012+00:00"` and the fractional stamp wins as it
  should. `329`'s "Not a bug" note is right; the hazard is the **suffix**, not the width.
  This is why the test must pin both, and why a blanket "normalise the width" fix would
  be wrong.
- **Reachability:** dormant while every writer uses `utc_now_iso()`. Armed by the `Z`
  stamps the repo's own fixtures still emit (`tests/test_lead_signal.py:204`), by any
  pre-`065` master CSV, and by any spreadsheet that reformats dates.
- **Trigger (mixed offset, confirmed at `_merge` level for completeness):** `old =
  "2026-10-03T12:00:00+03:00"` (09:00Z), `new = "2026-10-03T11:00:00+00:00"` (11:00Z) →
  `merged["rating"] == 4.9`, i.e. the 2-hour-older record wins.
- **Fix:** compare parsed instants, never strings. **Tests: T-P2, T-E2, T-E3.** Guard the
  naive-vs-aware trap: `datetime.fromisoformat("2026-10-03T12:00:00") <
  datetime.fromisoformat("2026-10-03T11:00:00+03:00")` raises
  `TypeError: can't compare offset-naive and offset-aware datetimes`.

### S3 — The coercion contract is undocumented and unpinned

- **Where:** `dedup.py:156`.
- **Breaks:** `str()` makes `_recency` type-safe, which is exactly what hides the fact
  that nothing constrains the *content*. Full verified coercion table:

  | input | `_recency` output | note |
  |---|---|---|
  | `None` / `""` | `''` | correct |
  | `0`, `0.0`, `False` | `''` | falsy sentinel erased (`329` S3) |
  | `b"2026-10-03T12:00:00+00:00"` | `"b'2026-10-03T12:00:00+00:00'"` | Python repr leaks `b` and quotes into a data column |
  | `3.5` | `'3.5'` | |
  | `float("nan")` | `'nan'` | `'n'` > `'2'`, so it outranks every real stamp |
  | `datetime(2026,10,3,12,0,tzinfo=utc)` | `'2026-10-03 12:00:00+00:00'` | **space** separator, not `T` |

- **The `datetime` row is a live trap, not a curiosity.** `str(dt)` uses `' '` (0x20)
  where `.isoformat()` uses `'T'` (0x54), so a `datetime` object and its *own* isoformat
  string do not compare equal, and the object sorts **below** its own string. Today
  `scrapers/base.py:27` declares `scraped_at: str` and all three scrapers pass a `str`, so
  this is unreachable — but `045-scraper-layer-refactor.md:159` proposes
  `scraped_at: datetime.datetime`, at which point the type annotation alone silently
  breaks merge ordering. Write the test **now**, as a refactor tripwire.
- **Trigger:** any future writer storing a `datetime`, a `float` epoch, or `bytes`.
- **Fix:** `isinstance(raw, str)` + `.strip()`, and reject everything else to `""`.
  **Tests: T-P5, T-E5.**

---

## The tests `_recency` still needs

Harness constraints, verified: `pytest` and `hypothesis` are **declared** in
`pyproject.toml:20-28` (`[project.optional-dependencies] dev`) but **not installed** in
this environment (`ModuleNotFoundError` for both). The existing suite is stdlib-`unittest`
by explicit convention (`tests/test_lead_signal.py:4-6`) and any test importing
`enricher` needs `_stub_requests()` (`tests/test_lead_signal.py:23-63`) first. So the
properties below are written stdlib-only with **fixed seeds**; each carries a one-line
`@given` equivalent for when `.[dev]` is installed.

**Fixture rule (the whole point of this section).** Neutralise columns 1, 2 and 4 of
`rank()` so column 3 is the only decider:
1. omit `source` entirely → `_trust_for` returns `_DEFAULT_TRUST = 1` on both sides
   (`dedup.py:148-152`, `dedup.py:111`);
2. use two *valid* ratings, both in `[0,5]` → `_validity` returns `3` on both sides
   (`dedup.py:134-139`);
3. use `4.9` for the older record and `1.0` for the newer one → `str("4.9") > str("1.0")`,
   so the tiebreak column points **away** from the recency answer;
4. assert both `_merge(a,b)` and `_merge(b,a)`.

```python
def _r(rating, stamp):
    """Fixture: trust and validity tied at 1/3, and str(4.9) > str(1.0) so the
    tiebreak column at dedup.py:184 cannot produce the expected answer."""
    return {"name": "X", "phone": "1", "rating": rating, "scraped_at": stamp}
```

### Properties (preferred — these exist, so pin the property, not a list of samples)

#### T-P1 — `test_recency_order_is_isomorphic_to_chronological_order`
- **Property:** for stamps in the one canonical format (`httpclient.py:229`),
  `a < b` (string) ⟺ `parse(a) < parse(b)` (instant), for every pair.
- **Input:** `random.Random(20261003)`, 300 instants in
  `[2024-01-01, 2026-06-19)`, formatted `"%Y-%m-%dT%H:%M:%S.%f+00:00"`.
- **Assert:** zero out-of-order adjacent pairs. **Verified today: 0 violations — this
  property holds.** It is a guard, not a bug report: it is what a fix for S2 must not
  break, and it is the test that will catch the naive-vs-aware `TypeError` when the fix
  lands.
- **Pins:** `065-python-312-modernisation.md:551` ("lexicographical timestamp sort
  invariant"); `013-main-state-idempotency.md` S3 (precision edge case).
- **Mutation value:** this is the property whose absence let S1 stand.

#### T-P2 — `test_recency_order_survives_mixed_suffix_conventions`  *(expected to FAIL today)*
- **Property:** the same isomorphism must hold when the two stamps use different suffix
  conventions, because both conventions are already in circulation.
- **Input:** every pair drawn from a generator that mixes
  `…%H:%M:%S.%f+00:00`, `…%H:%M:%SZ`, `…%H:%M:%S+00:00`, and `+03:00`.
- **Assert:** zero violations. **Verified today: fails.** Minimal witness —
  `old="2026-10-03T12:34:56Z"`, `new="2026-10-03T12:34:56.789012+00:00"`.
- **Pins:** `329` S1/S2 (unvalidated master text as a total order; mixed-offset
  inversion); `032-fixtures-offline-replay.md:62` ("`scraped_at` merge depends on a single
  string format").
- **Note:** add a **second** assertion that mixed *width with a single suffix* stays
  correct (`"…T12:34:56+00:00" < "…T12:34:56.789012+00:00"`). Without it, a lazy fix that
  pads the fraction everywhere would also pass T-P2 and would be hiding `329`'s correct
  "Not a bug" note.

#### T-P3 — `test_merge_scraped_at_key_is_one_of_the_input_keys`
- **Property:** the recency key of the merged record is exactly the recency key of the
  record whose `scraped_at` cell survived — the output's ranking key must be traceable to
  an input.
- **Input:** a table of `(a_scraped_at, b_scraped_at)` pairs including
  `(None, "2026-01-01T00:00:00+00:00")`, `(" ", "")`, `(0, None)`,
  `("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")`.
- **Assert:** `_recency(_merge(a, b)) in (_recency(a), _recency(b))`.
- **Pins:** the `scraped_at` rule asymmetry at `dedup.py:177` vs `dedup.py:156`.
  **Verified today: passes for the string cases; the `(" ", "")` case passes only by
  coincidence** — the surviving cell is `""` while the surviving `rating` came from the
  `" "` record, which is what T-E4 catches directly. Keep both.

#### T-P4 — `test_merge_never_raises_on_scraped_at`
- **Property:** `_merge` is total over `scraped_at`; no input type kills the run at
  `main.py:180`.
- **Input:** `scraped_at` drawn from
  `[None, "", 0, 0.0, False, 1767225600, b"2026-10-03T12:00:00+00:00", 3.5, "  ", float("nan")]`
  paired against `"2026-01-01T00:00:00+00:00"`.
- **Assert:** no exception, and the merged cell is a `str` or `None`.
- **Pins:** `020-error-handling-partials.md:27`;
  `031-test-suite-design.md:236` (`test_merge_scraped_at_mixed_type_comparison`, specified
  in 2025-era audit 031 and still unwritten).
- **Verified today: FAILS** with `TypeError: '>' not supported between instances of 'str'
  and 'int'` (`0`, `1767225600`) and `…'str' and 'bytes'`. This is the one test in the
  list that legitimately asserts on an exception — and it is asserting that there is
  **none**.

#### T-P5 — `test_recency_is_identity_on_strings_and_empty_on_everything_else`
- **Property:** `_recency` is the identity on `str` and maps every non-`str`/falsy value
  to `""` (the contract the S3 fix should adopt).
- **Input:** the coercion table in S3, verbatim.
- **Assert:** `_recency({"scraped_at": s}) == s` for `s` in
  `["", "2026-10-03T12:00:00+00:00", " ", "\ufeff2026-01-01T00:00:00+00:00"]`;
  `_recency({"scraped_at": x}) == ""` for `x` in `[None, 0, 0.0, False, b"…", 3.5,
  float("nan"), datetime(…)]`; and `_recency({}) == ""`.
- **Pins:** `329` S3 (`or ""` collapses falsy values; `str()` leaks reprs); the
  `datetime`-object trap from `045-scraper-layer-refactor.md:159`.
- **Verified today: the `str`-identity half passes, the "empty on everything else" half
  fails** (`bytes` → `"b'…'"`, `3.5` → `'3.5'`, `nan` → `'nan'`, `datetime` →
  `'2026-10-03 12:00:00+00:00'`).

### Examples (no property exists; pin the literal)

#### T-E1 — `test_volatile_field_tracks_the_recency_key_not_the_tiebreak`  *(replaces `tests/test_lead_signal.py:303-309`)*
- **Input:** `a = _r(4.9, "2026-01-01T00:00:00+00:00")`,
  `b = _r(1.0, "2026-09-01T00:00:00+00:00")`.
- **Assert:** `_merge(a, b)["rating"] == 1.0` **and** `_merge(b, a)["rating"] == 1.0`.
- **Pins:** the intended purpose of `_recency`; and the old whole-record-field-count
  merge from `024-dedup-merge-data-loss.md`. **Verified: kills 4/4 mutants, where the
  current fixture kills 0/2.** Add the `_r` docstring as a comment so the fixture is not
  "simplified" back into agreeing values.

#### T-E2 — `test_same_second_z_suffix_does_not_outrank_a_fractional_stamp`  *(expected to FAIL today)*
- **Input:** `a = _r(4.9, "2026-10-03T12:34:56Z")`,
  `b = _r(1.0, "2026-10-03T12:34:56.789012+00:00")`.
- **Assert:** `1.0`, both directions, **and** `_merge(a, b)["scraped_at"] ==
  "2026-10-03T12:34:56.789012+00:00"`.
- **Pins:** S2 above. **Verified today: yields `4.9` and the `Z` stamp, order-independently.**

#### T-E3 — `test_mixed_utc_offsets_compare_by_instant`  *(expected to FAIL today)*
- **Input:** `a = _r(4.9, "2026-10-03T12:00:00+03:00")` (09:00Z),
  `b = _r(1.0, "2026-10-03T11:00:00+00:00")` (11:00Z).
- **Assert:** `1.0`, both directions.
- **Pins:** `329` S2, promoted from `_pick` to `_merge` level. **Verified today: `4.9`.**

#### T-E4 — `test_whitespace_stamp_does_not_win_the_value_and_lose_the_timestamp`
- **Input:** `a = {"name":"X","phone":"1","rating":4.9,"scraped_at":" "}`,
  `b = {"name":"X","phone":"1","rating":1.0,"scraped_at":""}`.
- **Assert (the invariant):** `merged["rating"]` came from `a` ⟹ `merged["scraped_at"]`
  is not `""`. Concretely today: `merged["rating"] == 4.9` while
  `merged["scraped_at"] == ""` — assert the pair **and** assert
  `_is_missing(" ") == _recency(a) == ""` once fixed.
- **Pins:** S2 symptom 1. This is the only case in the file that is **reachable from
  `main.py` today**, because `main.py:87-89` preserves a space in the cell.

#### T-E5 — `test_datetime_scraped_at_ranks_identically_to_its_isoformat`
- **Input:** `d = datetime(2026,10,3,12,0,tzinfo=timezone.utc)`;
  pairs `({"scraped_at": d}, {"scraped_at": d.isoformat()})` in both orders.
- **Assert:** `_merge(a, b)["rating"] == _merge(b, a)["rating"]` and both `scraped_at`
  cells compare equal after the fix. **Verified today:** `str(d) == "2026-10-03
  12:00:00+00:00"` sorts **below** its own `.isoformat()`, so order-independence fails.
- **Pins:** S3 datetime row; the tripwire for `045-scraper-layer-refactor.md:159`.

#### T-E6 — `test_junk_stamp_cannot_pin_stale_data_across_three_runs`  *(expected to FAIL today)*
- **Input:** master `all_businesses.csv` with
  `scraped_at = "﻿2026-10-01T09:00:00+00:00"`, `rating=3.1`, `review_count=2`,
  `website="https://dead-2024.example"`, `source="google_places"`; each run merges a
  fresh `{"rating":4.8,"review_count":900,"website":"https://cafe-beirut.example",
  "scraped_at":"2026-10-03T12:34:56.123456+00:00"}` in `main.py:179` order
  (`raw_filtered + master`), then `write_csv` → `load_master` → merge again, 3 times.
- **Assert:** after every run, `rating == 4.8` and `review_count == 900`.
- **Pins:** `329` S1 in its test form — this is the regression test `329`'s *Recommended
  order of work* item 1 asks for, expressed against the real
  `load_master`/`write_csv` round trip instead of a hand-rolled in-memory loop.
  **Verified today: `rating=3.1, reviews=2, website=https://dead-2024.example` on all
  three runs** — the junk stamp wins its own replacement at `dedup.py:177` and is written
  back out at `main.py:209`, so it is self-perpetuating. This is the single highest-value
  test in the list: it exercises the real persistence path, needs no network, and fails
  today for the reason that matters.

#### T-E7 — `test_recency_key_is_stable_across_the_csv_round_trip`
- **Input:** `m = _merge({"name":"X","phone":"1","scraped_at":0}, {"name":"X","phone":"1","scraped_at":None})`;
  `write_csv` → `load_master`.
- **Assert:** `_recency(m) == _recency(reloaded)`.
- **Pins:** S2 symptom 2. **Verified today: `''` → `'0'`, not stable.**

#### T-E8 — `test_non_volatile_enum_fields_do_not_merge_alphabetically`
- **Input:** newer `industry_priority="high"`, older `"low"`; and newer `"high"`, older
  `"medium"`; both with `scraped_at` present and valid.
- **Assert:** `merged["industry_priority"] == "high"` in both.
- **Pins:** S2 effect 2. **Verified today: `low` and `medium`.** Add the honesty note in
  the docstring that `main.py:190-197` and `enricher.py:349` currently recompute this
  field, so the test guards the merge contract rather than a shipped number — and will
  start guarding a real number the moment the recompute moves earlier.

---

## Genuinely not testable here, and why

1. **Whether any real provider returns a well-formed stamp.** Overpass, Wikidata and
   Places responses cannot be exercised (no network; `BRIEF.md` rule 2). The oracle for
   "is this ISO-8601?" is `datetime.fromisoformat`, which tests *our* handling, not
   theirs. The right substitute is an offline replay fixture (`032-fixtures-offline-replay.md`),
   not a unit test.
2. **Whether the master CSV is corrupted on its way through Google Drive.** The file
   leaves the program (`.github/workflows/scrape.yml:56`, `:89`) and returns as a human-
   editable sheet. T-E6 proves our *handling* of a corrupt cell; it cannot prove the cell
   arrives corrupt. Only an end-to-end run can, and it costs a Drive round trip.
3. **Whether the recency key reflects the wall clock.** `utc_now_iso()`
   (`httpclient.py:220-229`) calls `datetime.now()` directly and takes **no injectable
   clock**; none of the three scrapers accept a `now=` parameter. So "a stamp from this
   run is newer than a stamp from last run" cannot be asserted without either freezing
   time globally or monkeypatching `dedup`/`httpclient` internals. `032-fixtures-offline-replay.md:10`
   already proposes the `now=` seam; **add it before writing any test that needs it.**
   NTP steps and DST are likewise out of reach.
4. **That `_recency` never raises.** `329` established there is no reachable crash
   surface: the only caller reaches `dedup.py:167` (`a.get(field)`) and early-returns at
   `dedup.py:168-171` before `rank()` runs. Do **not** write
   `assertRaises` tests against `_recency` itself — they would test an unreachable path.
   Test the *precondition* instead (T-P4), and pin the early-return at `dedup.py:168-171`
   with a test that a missing value is never selected.
5. **Whether `trust` and `validity` are the *right* policy.** `_SOURCE_TRUST`
   (`dedup.py:97-110`) and `_validity` (`dedup.py:120-140`) encode product judgements. A
   test can pin that `osm` beats `google_places` for `website` (already done at
   `tests/test_lead_signal.py:278-284`), but "should OSM win?" has no oracle and must not
   be encoded as a test.
6. **Timing.** Any assertion about `_recency` or `rank()` cost at 500k rows is flaky on
   shared CI, and `329` already measured comparison cost at ~28 µs for a 2 MB stamp —
   not a problem worth a benchmark test.
7. **The `_validity` `-100` branch (`dedup.py:122-123`) is unreachable from `_pick`.**
   `_pick` short-circuits on missing values at `dedup.py:168-171`, so the `-100` sentinel
   only fires from a direct `_validity` call. A test asserting
   `_validity("rating", None) == -100` passes but proves nothing about `_merge`; assert
   instead that `_merge` never selects a missing value.

---

## Recommended order of work

1. **T-E6** (`test_junk_stamp_cannot_pin_stale_data_across_three_runs`) and **T-E1**
   (replace `tests/test_lead_signal.py:303-309` with the `_r` fixture). Highest value per
   line: T-E6 is the regression test `329` asks for and it fails today; T-E1 is the only
   change that makes `_recency` testable at all.
2. **T-P4 + T-P2** as the executable spec for the S2 fix in `329`. T-P4 is the
   `031-test-suite-design.md:236` test, three audits overdue. Write them before touching
   `dedup.py:177`/`183` so the fix is driven red-to-green, and keep T-P2's
   single-suffix-width assertion so the `329` "Not a bug" is not regressed.
3. **T-E4, T-E7, T-E5, T-P1** — the consistency and round-trip invariants. Each is 3-6
   lines; together they are the difference between "the ranking key is a timestamp" and
   "the ranking key is whatever was in the cell".
4. **T-P3, T-P5, T-E3, T-E2, T-E8** — the remaining property/coercion pins. Low
   urgency individually; they exist so the next refactor of `rank()` cannot silently
   change semantics.
5. **Add a mutation smoke check to CI** (`dedup.py:183` with `_recency` stubbed to a
   constant must fail the suite). The evidence above is that the suite does not currently
   distinguish the two, and that gap is what allowed every S1/S2 in this file and in
   `329` to reach production undetected.