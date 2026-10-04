# 200 — `dedup._is_missing`: the missing-value contract has no test that can fail

**Target:** `dedup.py:79-90` (`_is_missing`) · **Callers:** `dedup.py:122` (`_validity`), `dedup.py:168` and `dedup.py:170` (`_pick`) · **Existing tests:** `tests/test_lead_signal.py`

## Verdict

`_is_missing` is the definition of "this field carries no information" for the whole
merge layer, and it is used at every survivorship decision. It has **zero direct
tests** — it is not even imported (`tests/test_lead_signal.py:65` imports only
`_merge, normalize_phone`) — and the single test that targets its headline
regression (`tests/test_lead_signal.py:286-290`) **cannot fail**: I ran its exact
input against a faithful reconstruction of the pre-fix `_merge` and it passes.

Re-verified against commit `99493b9^`: the suite is 28 tests, all green, and
`test_empty_string_does_not_beat_a_real_value` returns `'https://real.example'`
under the old code too. The `.strip()` branch (`dedup.py:89`) is exercised by
nothing in the repo. Meanwhile I reproduced two live defects that only exist
because this contract is untested: whitespace-only values are classified as
missing but propagated verbatim into output records, and a truthiness rewrite of
this one function would silently re-invert the lead signal with the suite
still green.

---

## Findings

### S1 — The only test that pins the `_is_missing` regression cannot fail

- **Where:** `tests/test_lead_signal.py:286-290` vs `dedup.py:80-84`
- **Breaks:** The existing test's orientation is the one the old code already
  got right. Input: `rich` (6 populated keys) as side `a`, `blank` (3 keys,
  `website=""`) as side `b`. Under `99493b9^`'s rule
  `merged[key] = av if _field_count(a) >= _field_count(b) else bv`
  (`git show 99493b9^:dedup.py`, `_merge`), `6 >= 3`, so side `a` wins and the
  assertion holds. The bug in `docs/audits/002-dedup-merge-data-loss.md:92` is
  the *opposite* orientation: the record padded with empty strings must be the
  one that wins the whole-record count, and only then does `""` overwrite a real
  value. Verified:

  | input | old `_merge` | new `_merge` |
  |---|---|---|
  | `_merge(rich, blank)` — the test as written | `'https://real.example'` | `'https://real.example'` |
  | `_merge(blank_heavy, real)` — the actual bug | `''` | `'sales@agency.com'` |

- **Trigger:** `blank_heavy = {"name":"Agency","phone":"70123456","source":"osm","email":"","facebook":"","instagram":"","whatsapp":""}`
  and `real = {"name":"Agency","phone":"70123456","source":"osm","email":"sales@agency.com"}`.
  Old code: `_field_count(blank_heavy)=7 >= _field_count(real)=4`, so
  `email` becomes `''` — the only email the business had, gone.
- **Why S1:** the data destroyed is the entire reason for the merge, and it is
  the exact failure mode `docs/audits/002-dedup-merge-data-loss.md` and `024`
  were filed for. Nothing in the repository now prevents it from returning.
- **Fix:** replace the input with the blank-dominant orientation and assert both
  merge orders (test T1 below); the current test can stay as a second case.

For scale, I replayed all eight `TestMergeSurvivorship` tests against the old
`_merge`. Six genuinely fail on it (`test_trust_is_per_field_not_per_record`,
`test_merge_is_order_independent`, `test_invalid_value_loses_to_valid_one`,
`test_volatile_field_prefers_the_fresher`, `test_out_of_range_rating_is_rejected`,
`test_no_field_is_lost`). Two are vacuous — the one above, plus
`test_sources_are_unioned_and_ordered` (`tests/test_lead_signal.py:316-319`),
which unions sources identically under both rules.

### S2 — Whitespace-only values are called missing but `_pick` propagates them verbatim into scored output

- **Where:** `dedup.py:168-171` (propagation) against `dedup.py:88-89`
  (classification), consumed by `enricher.py:103-116`, `enricher.py:288-295`,
  `enricher.py:231`, gated at `main.py:206`
- **Breaks:** `_is_missing("  ")` is `True`, so `_is_missing` correctly refuses
  to let whitespace *win* a field. But `_pick` returns the rejected value
  unchanged rather than a normalized `None`:

  ```
  _merge(master,  scraped) -> email='   ', website='  ', phone='\t'
  _merge(scraped, master) -> email=None,   website=None, phone=None
  ```

  Same pair, opposite order, different output. This is reachable through the
  public entry point — verified end to end:

  ```
  dedup([{"name":"Ghost Cafe","phone":"70123456","website":None,"email":None,"source":"google_places"},
         {"name":"Ghost Cafe","phone":"70123456","website":"  ","email":"   ","source":"osm"}])
  -> 1 row, website='  ', email='   ', completeness_score=3
  ```

  The leak happens whenever the `None`-bearing record is the one already in the
  index at `dedup.py:208`/`dedup.py:216`. `main.py:179` builds
  `combined = raw_filtered + master`, so which side leaks is decided by arrival
  order across a cumulative master — not by anything deterministic.

  Then every truthiness-based consumer treats the whitespace as real data:
  `enricher.completeness_score` returns **3** (`enricher.py:103-116`), which
  clears the `>= 1` gate at `main.py:206` and lands the row in
  `qualified_businesses.csv`; `lead_score` returns **40**
  (`enricher.py:288-295`), putting it in the sales funnel; and
  `enricher.py:231` (`if r.get("website")`) schedules an HTTP GET for the
  literal URL `"  "` — a guaranteed-failing request occupying one of 40 workers.
- **Why S2 and not S1:** the scrapers pass API strings through verbatim
  (`scrapers/google_places.py:286-291` uses `place.get(...)` with no
  normalisation), so this needs a whitespace-only value to *enter* the pipeline.
  `main.load_master` demonstrably preserves such cells (`main.py:88-89` rewrites
  only exactly-`""` to `None`), so a hand-edited or previously-corrupted master
  CSV is enough to trigger it, and once triggered it recurs every run. Real
  but input-dependent.
- **Fix:** `_pick` should return `None` rather than the losing side's value when
  both sides are missing (`dedup.py:168-171`), or `_merge` should normalize.
  Then pin it with property T5/T6.

### S2 — No test routes `website_live` through `_merge`, so a truthiness rewrite of `_is_missing` resurrects the DEAD/LIVE inversion with a green suite

- **Where:** `dedup.py:79-90`, `dedup.py:167-171`, `enricher.py:250`,
  `tests/test_lead_signal.py:76-96`
- **Breaks:** `_is_missing(False) is False` is load-bearing. `False` is the
  server-confirmed-dead verdict (`enricher.py:250`), and it is a *volatile* field
  (`dedup.py:114`) so `_pick` ranks it against competing observations. I
  monkeypatched `_is_missing` to the obvious "simplification" `not value` and
  re-ran the merge:

  ```
  dead = {..., "website_live": False, "scraped_at": "2026-09-01..."}
  live = {..., "website_live": True,  "scraped_at": "2026-01-01..."}

  as written        _merge(dead, live)["website_live"] -> False   (correct)
  truthiness rewrite                             -> True             (dead site erased)
  ```

  The fresher, authoritative DEAD verdict is discarded in favour of a
  month-old LIVE one, which turns a rebuild pitch back into a "you have a site"
  row — precisely the inversion `docs/audits/043-scoring-upgrade.md` and
  `TestLeadSignalIsNotInverted` exist to prevent. But those tests call
  `lead_score`/`recommend_service` on single records, and **no test in the repo
  puts `website_live` through `_merge` at all** (`GOOGLE`/`OSM` fixtures at
  `tests/test_lead_signal.py:271-276` have no `website_live` key). The rewrite
  passes all 28 tests.
- **Trigger:** any future refactor that writes `_is_missing` as
  `return not value` or `return not str(value).strip() if value is not None else True`.
- **Why S2:** it does not corrupt anything today; it removes the tripwire on the
  pipeline's worst historical bug.
- **Fix:** add T4 — a direct assertion plus a merge-level assertion that a
  fresher `False` beats an older `True`.

### S3 — `_is_missing` itself is never called by a test

- **Where:** `tests/test_lead_signal.py:65`
- **Breaks:** `from dedup import _merge, normalize_phone` — `_is_missing`,
  `_pick` and `_validity` are unreachable from the test module. Every assertion
  about the missing-value definition is therefore indirect, and each indirect
  path can be satisfied by the *wrong* half of the definition.
- **Trigger:** n/a — this is a structure gap.
- **Fix:** add the imports and the direct unit tests T2/T3.

### S3 — The `0` sentinel no longer wins, but only by accident of `str()` tiebreak ordering

- **Where:** `dedup.py:184` (`str(value)`), `dedup.py:180-185`
- **Breaks:** `docs/audits/002-dedup-merge-data-loss.md:46` documented that
  freshly scraped `lead_score=0` / `completeness_score=0` sentinels
  (`scrapers/google_places.py:301,304`, `scrapers/wikidata.py:126,129`,
  `scrapers/osm.py:118,121`) overwrite the master's computed scores. That is
  genuinely fixed — verified: `0` loses to `4`, `9`, `10` and `100` in both
  merge orders. But `_validity` returns a flat `1` for these fields
  (`dedup.py:140`) and `_trust_for` returns `_DEFAULT_TRUST` for both sides, so
  the outcome rests entirely on `"0" < "4"` being lexicographic. The same
  tiebreak is numerically wrong in general: `9` beats `10`, `99` beats `100`
  (verified). Harmless for a monotonic score, but the protection is incidental
  and undocumented, so a future "make the tiebreak numeric" change can silently
  put the sentinel back.
- **Trigger:** `_merge({"lead_score":0,...}, {"lead_score":9,...})` → `9`. Correct
  outcome, wrong reason.
- **Fix:** state the intent in a test (T7) and treat the sentinel in `_validity`
  rather than relying on string ordering.

---

## Tests this function still needs

All go in a new class in `tests/test_lead_signal.py`. Requires widening the
import at `tests/test_lead_signal.py:65` to
`from dedup import _is_missing, _merge, _pick, _validity, dedup` and adding
`completeness_score` to the `enricher` import at line 66. Properties are
preferred; examples appear only where the property is already covered elsewhere.

```python
import itertools  # add at the top of the test module


class TestIsMissingContract(unittest.TestCase):
    """dedup._is_missing defines what 'no information' means for every
    survivorship decision in _pick and _validity. These pin the contract
    itself, not just one observable consequence of it."""

    BLANKS = [None, "", "   ", "\t", "\n", " \t\n ", "\u00a0"]
    VALUES = [None, "", "   ", "\t\n", "\u00a0", "x", "https://a.example",
              0, 1, False, True]

    def _pair(self, av, bv, field="website"):
        return ({"name": "X", "source": "osm", field: av},
                {"name": "X", "source": "osm", field: bv})
```

### T1 — `test_blank_heavy_record_cannot_erase_a_real_value` *(fixes S1)*

Replaces `tests/test_lead_signal.py:286-290`. Literal input: the blank-dominant
pair from the finding above, both merge orders. Asserted output:
`_merge(a, b)["email"] == "sales@agency.com"` and
`_merge(b, a)["email"] == "sales@agency.com"`.
**Pins:** `docs/audits/002-dedup-merge-data-loss.md:92` (empty string counted as
present, real email wiped). Verified: fails on `99493b9^` with `''`, passes now.

### T2 — `test_only_none_and_blank_strings_are_missing` *(property; fixes S3)*

For every `v` in `self.VALUES`, assert
`_is_missing(v) is (v is None or (isinstance(v, str) and not v.strip()))`.
**Pins:** the definition written in the `dedup.py:80-84` docstring, which no
test states today. This is the exact `is None` check the fix replaced, so it is
the direct tripwire for that regression.

### T3 — `test_strip_invariance` *(property)*

For `s` in `("", "   ", "\t\n", "\u00a0", " x ", "https://a.example",
"\u00a0x\u00a0")`, assert `_is_missing(s) is _is_missing(s.strip())`. Verified
to hold. **Pins:** the `.strip()` branch at `dedup.py:89`, currently exercised
by nothing in the repository — commit `99493b9`'s message claims whitespace is
treated as missing and nothing checks it.

### T4 — `test_fresher_dead_verdict_beats_older_live_verdict` *(fixes S2)*

Two assertions. First, literal: `self.assertIs(_is_missing(False), False)`.
Second, literal input

```
dead = {"name":"C","source":"google_places","website":"https://a.example",
        "website_live":False,"scraped_at":"2026-09-01T00:00:00+00:00"}
live = {"name":"C","source":"google_places","website":"https://a.example",
        "website_live":True,"scraped_at":"2026-01-01T00:00:00+00:00"}
```

asserted output: `_merge(dead, live)["website_live"] is False` and
`_merge(live, dead)["website_live"] is False`. Verified both hold now; the
second fails the instant `_is_missing` becomes truthiness-based.
**Pins:** `docs/audits/043-scoring-upgrade.md` / the DEAD-LIVE-UNKNOWN tri-state
(`enricher.py:250`), which today has no coverage through `_merge`.

Complementary case, same property: `website_live=None` **is** missing
(`enricher.py:298-304` — we never reached the host), so
`_merge({"website_live":None,...}, {"website_live":True,...})["website_live"] is True`.

### T5 — `test_missing_iff_both_inputs_missing` *(property)*

For all 121 pairs from `itertools.product(self.VALUES, repeat=2)`, assert
`_is_missing(_merge(a, b)[field]) == (_is_missing(av) and _is_missing(bv))`.
Verified: zero violations across 121 pairs. **Pins:** the formal statement of
what `_is_missing` buys `_merge` — the merge may never manufacture or destroy
information, only choose between two values. This subsumes the two currently
implicit paths (`dedup.py:168` `av is None`, `dedup.py:170` `bv == ""`).

### T6 — `test_merge_is_commutative_up_to_missingness` *(property)*

For all 121 pairs, for every field in `set(_merge(a,b)) | set(_merge(b,a))`,
assert `_is_missing(forward[field]) == _is_missing(reverse[field])`.
Verified: zero violations across 121 pairs.
**Pins:** consequence #3 of commit `99493b9` (non-reproducible master). The
existing `test_merge_is_order_independent`
(`tests/test_lead_signal.py:292-295`) compares raw values for one fully-populated
field only; this states the weaker-but-always-true invariant that also covers the
missing branches. Note the raw values are *not* commutative today — T5/T6 are
the honest formulation, and T7 below is where the remaining gap bites.

### T7 — `test_no_merged_record_carries_a_value_the_merge_calls_missing` *(property; fails today)*

For all 121 pairs, for every field in `_merge(a, b)`:
`assertFalse(_is_missing(value) and value not in (None, ""))`.
Verified **failing** on `("", None)` and `(None, "  ")` style pairs: `_pick`
returns `bv` at `dedup.py:169` when `bv` is whitespace-only, so `'   '` reaches
the output record.
**Pins:** the S1 already fixed at `dedup.py:88-89` reappearing one layer down in
`_pick`, with the S2 consequence in `enricher.completeness_score` /
`enricher.py:231`.

### T8 — `test_whitespace_never_reaches_the_enrichment_gate` *(end-to-end; fails today)*

Literal input: the two-record `dedup()` call from the S2 finding. Asserted
outputs: `len(rows) == 1`; `_is_missing(rows[0]["website"])` and
`_is_missing(rows[0]["email"])` are `True` **and**
`completeness_score(rows[0]) == 0`. Verified today: `len(rows) == 1`,
`website='  '`, `completeness_score == 3`. This is the test that turns the S2
from "the merge leaked" into "the row is not scored as a lead".

### T9 — `test_blank_values_get_the_missing_validity_penalty` *(property)*

For every `field` in `("email","website","lat","lon","rating","review_count","name")`
and every `blank` in `self.BLANKS`, assert `_validity(field, blank) == -100`;
and assert the positive anchors `_validity("email","a@b.com") == 3`,
`_validity("website","https://a.example") == 3`, `_validity("rating",4.2) == 3`.
**Pins:** the `_is_missing` call at `dedup.py:122`. The `-100` must stay below
every real value's `0..3` (`dedup.py:125-139`) or a blank could outrank a
plausible value in the rank tuple at `dedup.py:180-185`. Nothing tests this
today.

### T10 — `test_zero_sentinel_never_overwrites_an_enriched_score` *(S3)*

For both fields and both merge orders, assert
`_merge({"lead_score":0,"source":"osm",...}, {"lead_score":N,"source":"osm",...})["lead_score"] == N`
for `N` in `(4, 9, 10, 100)`, and the same for `completeness_score` with
`(3, 9, 10, 100)`. Verified: all hold now.
**Pins:** `docs/audits/002-dedup-merge-data-loss.md:46` — the `lead_score=0`
sentinel emitted by all three scrapers (`scrapers/google_places.py:301`,
`scrapers/wikidata.py:126`, `scrapers/osm.py:118`) overwriting the master's
computed score. Today it is prevented only by `"0" < "4"` being lexicographic
in the `str(value)` tiebreak at `dedup.py:184`; this test documents the
requirement so a numeric-tiebreak refactor cannot quietly undo it.

### T11 — `test_missing_verdict_survives_a_csv_round_trip` *(property)*

Write a merged record via `main.write_csv`, read it back via `main.load_master`
(both already imported at `tests/test_lead_signal.py:67`), assert
`_is_missing(merged[f]) == _is_missing(reloaded[f])` for every field.
Verified to hold today: `load_master` maps `""` → `None` (`main.py:88-89`) but
leaves `"  "` as `"  "`, and both are missing under `_is_missing`, so the verdict
is stable. **Pins:** the cumulative-master invariant — because
`main.py:150` reloads the master every run, any value that is "present" to
`_is_missing` on write but "missing" after `load_master` would silently change
which record wins a field on the next run.

---

## Not a bug, but worth knowing

- **Nothing has ever audited this function.** `rg _is_missing docs/audits/` →
  0 hits across 100+ reports. The function that defines the pipeline's notion of
  "no information" is invisible to the audit corpus.
- **`_is_missing` and `main.resolve_country` implement the same predicate twice.**
  `main.py:59-60` does `isinstance(value, str) and value.strip()` — the same
  logic as `dedup.py:88-89`, in a different file, with no shared helper. They
  agree today; nothing enforces that. `resolve_country` should call
  `_is_missing`.
- **`"N/A"` and `"null"` are "present".** `_is_missing("N/A") is False`; it is
  ranked present-but-invalid by `_validity` (`_EMAIL_OK`/`_URL_OK` at
  `dedup.py:116-117` reject it) so it loses to a real value — correct outcome by
  a different mechanism than the missing-value path. Same class as the whitespace
  leak: a placeholder that is neither `None` nor blank.
- **`_is_missing([])` is `False`.** Harmless only because
  `BusinessRecord` (`scrapers/base.py:5-29`) declares every field scalar. If a
  future scraper returns a list for any field, `[]` would rank as an observation.
- **`_is_missing` is pure and single-threaded in effect.** It is called from
  `_validity` inside `_pick`, which runs only inside `dedup`'s serial loop
  (`dedup.py:201-218`). No concurrency property to test.
- **`_merge` distinguishes key-absent from key-explicit-`None` only in the
  output, never in the decision** (`dedup.py:192`, `_pick` uses `.get()` at
  `dedup.py:167`). `_merge({}, {}) == {}` and `_merge({"f": None}, {})["f"] is None`.
  Fine, but note it in the T5 docstring so nobody relies on the difference.

## What genuinely cannot be tested here, and why

- **Whether an accepted value is *true*.** `_is_missing("https://maps.example")`
  is `False`, yet that is a Google redirect, not a site. Presence and
  plausibility are different questions; plausibility lives in `_URL_OK`
  (`dedup.py:117`) and `_validity`. Judging which URLs are real needs product
  knowledge and a URL-blocklist fixture, not a unit test on `_is_missing`.
- **Whether `False` for `website_live` is the *right* verdict.** The offline
  half of T4 (`False` is an observation, not an absence) is fully testable and I
  verified it. The other half — that `False` means DEAD rather than UNKNOWN —
  is decided by `_fetch_website` over the network (`enricher.py:161-222`) and
  cannot be exercised without HTTP, which BRIEF.md §Rule 2 forbids. The existing
  `test_unreachable_site_gets_no_dead_credit` tests the *consequence*, not the
  fetch.
- **Whether whitespace actually enters the pipeline in production.** T8 proves
  the merge leaks it and that it scores as a lead. Sizing it needs
  `data/all_businesses.csv`, which is gitignored (BRIEF.md §Infrastructure) and
  only exists after a network run. The honest test is a `cli.py cmd_validate`
  fixture over a hand-built CSV, which is out of this lens.
- **Dedup-key effects of whitespace.** `_extract_city` (`dedup.py:75`) and
  `normalize_name` (`dedup.py:69`) do their own `.strip()`/`.lower()`;
  `_is_missing` never sees `name` or `address`. "Does a padded business name
  still merge with its unpadded twin" is a `dedup()`/`normalize_name` test, not
  an `_is_missing` test.
- **Non-`str`, non-`None` container values** (`[]`, `{}`, `bytes`) are
  unreachable given `BusinessRecord`. Testing them would pin behaviour that no
  caller can produce.

## Recommended order of work

1. **T1**, replacing `tests/test_lead_signal.py:286-290`. One-line change, turns a
   vacuous test into a real tripwire on an S1 data-loss path.
2. **T4**, then **T2/T3/T5/T6**. These are pure additions with no source changes
   and they close the whole direct-coverage gap on `_is_missing`.
3. **T7 + T8**, which will fail. Fix by normalizing in `_pick`
   (`dedup.py:168-171`) — return `None`, not the losing side's raw value — then
   the tests pass and the S2 goes with them. Do T7 before T8: T7 is the
   contract, T8 is the consequence.
4. **T9, T10, T11**, cheap properties that pin `_validity`'s `-100` floor, the
   `0`-sentinel intent, and the CSV-round-trip invariant.
5. Optionally, have `main.resolve_country` (`main.py:59-60`) call `_is_missing`
   so the blank definition stops being duplicated.