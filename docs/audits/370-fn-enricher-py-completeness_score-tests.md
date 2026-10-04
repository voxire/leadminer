# 370 — Test completeness for `completeness_score` (`enricher.py:101`)

**Lens:** tests. Read the function, its callers, and `tests/test_lead_signal.py`.
Every expected value below was executed against the working tree (`99493b9`, clean) with
`requests`/`urllib3` stubbed; runs marked ✅ are observed, not inferred.

---

## Verdict

`completeness_score()` has **zero tests** — it is the only function in the codebase with no
coverage at all *and* no single obvious home for its logic, and it is the sole gate on a
shipped deliverable (`qualified_businesses.csv`, `main.py:206`). Worse, its output is
**demonstrably wrong today**: any truthy junk in `phone`/`email`/`website`/`address` earns a
point, and `main.py:194` then normalises the phone away three lines later, so the CSV ships
rows with `completeness_score=2` and an empty `phone` cell (✅ `{"phone":"private"}` →
score 1, `normalize_phone("private","LB")` → `""`).

The single highest-value test in this whole file is not an example at all — it is the
invariant `record["completeness_score"] == completeness_score(record)` **after `enrich()`
returns**. That one assertion pins the ordering bug that shipped from commit `f2f9d11`
through `f0f1e39` and was only fixed in `14cbbe9`, and it keeps holding after `docs/audits/044`
moves the assignment out of `enrich()`.

---

## The contract surface (read this before writing the tests)

```python
# enricher.py:101-117
def completeness_score(record: dict) -> int:
    score = 0
    if record.get("phone"):     score += 1   # :103
    if record.get("email"):     score += 1   # :105
    if record.get("website"):   score += 1   # :107
    if record.get("address"):   score += 1   # :109
    if record.get("facebook") or record.get("instagram"): score += 1  # :111  <-- one point, two fields
    if record.get("whatsapp"):  score += 1   # :113
    if record.get("linkedin"):  score += 1   # :115
    return score
```

Who reads the answer:

| Caller | Line | What it actually consumes |
|---|---|---|
| `enricher.enrich` | `enricher.py:349` | assigns `r["completeness_score"]` |
| `main.main` | `main.py:206` | `r.get("completeness_score", 0) >= 1` → `qualified_businesses.csv` |
| `pitch_recommender.recommend_service` | `pitch_recommender.py:48-52` | **dead** — `completeness` is coerced and then never read again in the `if`-chain (`pitch_recommender.py:55-99`) |
| `leadminer score` | `cli.py:247-277` | never recomputes it (only `lead_score`, `cli.py:269`) |

**Consequence for test design:** the only *product* meaning is the boolean `0` vs `≥ 1`.
Values 2–7 exist in the CSV and in `README.md:64` / `docs/audits/049-data-dictionary.md:282`,
but nothing in the pipeline branches on them. So the test budget should go to
(a) the 0/≥1 boundary, (b) the invariants that make the number trustworthy, and
(c) the ordering that decides *when* the number is computed — not to 256 example rows.

**Coverage today:** `tests/test_lead_signal.py` is the only test file. It imports
`DEAD, LIVE, UNKNOWN, infer_region, lead_score` from `enricher` (`:66`) — `completeness_score`
is never imported. The single occurrence of the string in the suite is
`tests/test_lead_signal.py:202`, a fixture CSV cell `"completeness_score": "2"`. **0 tests.**

---

## Findings

### S1 — The function that gates a shipped CSV has no test at all

- **Where:** `enricher.py:101-117`; gate at `main.py:206`; suite at `tests/test_lead_signal.py`
- **Breaks:** nothing crashes when this function is wrong — it just quietly reshapes
  `qualified_businesses.csv`, the file the sales team opens first. Every other predicate in
  the pipeline (`lead_score` tri-state, `normalize_phone`, `resolve_country`, atomic write,
  CLI exit codes) has a regression test pinning the exact bug it was written for; this one
  has none, and it has never been modified for correctness either — `git show f2f9d11:enricher.py:86-98`
  and `git show 14cbbe9:enricher.py:101-117` are byte-identical except for the two arms added
  in `14cbbe9` (`whatsapp`, `linkedin`).
- **Trigger:** delete `enricher.py:103-117` and replace the body with `return 7`. The suite
  still passes 20/20.
- **Fix:** add `tests/test_enricher_pure.py` (the filename `docs/audits/032-fixtures-offline-replay.md:84`
  already reserves) with the 11 tests specified below.

**The tests** (all in one new class; every literal input below was executed):

```python
FIELDS = ("phone", "email", "website", "address",
          "facebook", "instagram", "whatsapp", "linkedin")
```

| # | Method name | Literal input | Asserted output | Past bug pinned |
|---|---|---|---|---|
| 1.1 | `TestCompletenessScoreProperties::test_absent_key_equals_explicit_none` | property over all 8 keys: `full = dict.fromkeys(FIELDS, "x")`; for each `f`, `{k:v for k,v in full.items() if k != f}` and `dict(that, **{f: None})` | equal scores (✅ `phone`: 6 vs 6; `facebook`: 7 vs 7) | guards the `record.get(k)` contract for the `enrich()` `setdefault` path (`enricher.py:340-348`) and for `csv.DictWriter`'s missing-key fill (`main.py:125`) |
| 1.2 | `test_score_is_monotone_in_presence` | property: for each `f` and each `(absent, present)` in `(None,"x"), (None," "), ("","x")` | `score(base) <= score(base \| {f: present})` | structural: any future re-weighting must not invert the direction of a point |
| 1.3 | `test_score_is_pure_and_ignores_its_own_output` | `{"phone": "+96170123456", "completeness_score": 7}` | returns `1`; record unchanged (`rec == snapshot`) ✅ | `enricher.py:349` writes the key the function reads — a self-referential read would compound across runs |
| 1.4 | `test_score_stays_within_documented_ceiling` | property: exhaust all `2**8 = 256` presence masks | every result is `int`, in `[0, 7]`; reachable set is exactly `{0..7}` ✅ | the invariant `docs/audits/048-data-quality-validation.md:47` requires and `cli.py:135-201` never checks |
| 1.5 | `test_facebook_and_instagram_share_one_point` | `{"facebook": "fb", "instagram": "ig"}` → `1`; `dict.fromkeys(FIELDS, "x")` → `7` ✅ | exactly as stated | the deliberate-but-undocumented `or` at `enricher.py:111`; `README.md:64` says "0–7 count of filled contact fields" for **8** fields |
| 1.6 | `TestGate::test_identity_fields_alone_never_qualify` | `{"name":"Acme","category":"restaurant","region":"Beirut","country":"LB","lat":33.89,"lon":35.50,"rating":4.9,"review_count":312,"source":"osm\|google_places","scraped_at":"2026-10-03","website_live":False}` | `0` ✅ (a 4.9★, 312-review, well-located business with no contact channel is *not* qualified) | `docs/audits/020-type-contract-import-cycle.md:108` — derived fields must not be counted as completeness |
| 1.7 | `test_every_single_contact_field_qualifies_a_row` | `{"<f>": "x"}` for each `f` in `FIELDS` | all `≥ 1` ✅ | the `main.py:206` predicate; also pins that `address` alone qualifies (a deliberate choice that needs a comment, see §Not a bug) |
| 1.8 | `test_dead_and_unreachable_websites_still_earn_the_website_point` | `{"website":"https://x.example","website_live":False}` → `1` ✅; `…,"website_live":None}` → `1` ✅ | both `1` | pins the asymmetry with `lead_score` (`enricher.py:300-304` awards **+20** for a dead site). Two functions, opposite opinions on the same row; make it deliberate or change it |

---

### S1 — Junk, placeholder and whitespace values all score as complete

- **Where:** `enricher.py:103, 105, 107, 109` (bare truthiness); normalisation happens *after*
  scoring at `main.py:192-194`
- **Breaks:** the number in the CSV is a count of *non-empty strings*, not of *contact
  channels*. Verified live today:

  | Input | `completeness_score` ✅ | What ships |
  |---|---|---|
  | `{"phone": "private"}` | `1` | `main.py:194` rewrites `phone` to `""`; row says score 1, phone column blank |
  | `{"phone": "---"}` / `"n/a"` / `"yes"` / `"12345"` | `1` each ✅ | same |
  | `{"phone": "   "}` | `1` ✅ | whitespace-only cell, scored as a contact |
  | `{"email": "  "}` | `1` ✅ | ditto |
  | `{"email": "a@b"}` | `1` ✅ | `has_any_contact` (`main.py:137-145`) says `False` → the two gates disagree |
  | `{"email": "noreply@"}` | `1` ✅ | **passes both gates** — `@` present, `len>5` (`main.py:143`) |
  | `{"website": "n/a"}` | `1` ✅ | also lands in `with_websites.csv` (`main.py:199`) and is handed to `check_websites` as a URL (`enricher.py:231`) |

  This is `docs/audits/084-SYNTH-adversarial-fix-review.md:94` ("Completeness score distortion"),
  filed as **S1** and never fixed. It is the same class as
  `docs/audits/087-SYNTH-test-gap.md:150-157`, where `dedup._field_count` had to be taught
  that `"   "` is empty (`dedup.py:79-90`, `_is_missing`) — `completeness_score` was never
  given the same treatment.
- **Trigger:** `{"name":"Acme","phone":"private","address":"Hamra, Beirut","category":"cafe","country":"LB"}`
  → ✅ `enrich()` returns `completeness_score = 2`; row enters `qualified_businesses.csv`
  with an empty phone cell.
- **Fix:** reuse the validators that already exist — `dedup._is_missing` (`dedup.py:79-90`) for
  blank/whitespace, `dedup._validity` (`dedup.py:120-140`) for `email`/`website` shape, and
  `normalize_phone` (`dedup.py:33-63`) for `phone`; then normalise the phone **before**
  scoring.

**The tests** (all currently **fail**; that is the point — they are the spec):

```python
JUNK = ("private", "---", "n/a", "N/A", "yes", "unknown", "12345", "   ", "")

class TestCompletenessRejectsJunk(unittest.TestCase):
    def test_junk_phone_does_not_earn_a_point(self):
        for junk in JUNK:
            self.assertEqual(completeness_score({"phone": junk}), 0,
                             f"phone={junk!r} is not dialable but scored a point")

    def test_whitespace_only_values_are_not_contacts(self):
        for f in FIELDS:
            self.assertEqual(completeness_score({f: "   "}), 0, f)

    def test_placeholder_website_does_not_earn_a_point(self):
        for w in ("n/a", "N/A", "-", "--", "none", "http://"):
            self.assertEqual(completeness_score({"website": w}), 0, w)

    def test_placeholder_email_does_not_earn_a_point(self):
        for e in ("a@b", "@", "mailto:", "noreply@", "user@", "  "):
            self.assertEqual(completeness_score({"email": e}), 0, e)
```
Pins: 084's S1; 087's `_field_count` whitespace regression; the `N/A` shapes
`docs/audits/084:88` calls out for `scrapers/base.py:13`.

---

### S1 — No regression test for the ordering bug that shipped for four commits

- **Where:** `enricher.py:337` (`records = check_websites(records)`) vs
  `enricher.py:349` (`r["completeness_score"] = completeness_score(r)`)
- **Breaks:** this exact inversion was live for the entire life of the feature. `git show`
  proves it: at `f2f9d11:enricher.py:155` the score was computed, and `:157` fetched
  websites; still inverted at `02e0c3e:155/157` and `f0f1e39:175/177`; reordered in
  `14cbbe9` (`:334` fetch, `:347` score). Every contact the crawler scraped from a homepage
  was invisible to the score for that whole period. Verified magnitude on one record with a
  LIVE site yielding `mailto:` + `wa.me` + `instagram.com/`: ✅ correct order `5`, historical
  order `2`. It is also the exact scenario `docs/audits/087-SYNTH-test-gap.md:427-441`
  (test 5.2) asks for, and `docs/audits/044-main-refactor-plan.md:73` proposes *moving* the
  line again — so the guard is needed now, not later.
- **Trigger:** any `LIVE` website whose HTML carries contact links.
- **Fix:** the test. No source change.

**The tests:**

```python
class TestEnrichScoresAfterFetching(unittest.TestCase):
    # NB: put the instagram link BEFORE the mailto. With the mailto first,
    # _INSTAGRAM_RE (enricher.py:131) matches the "@" branch inside the email
    # address and reports instagram="acme.example" -- verified. Asserting that
    # would bake a live bug into the fixture.
    LIVE_HTML = (b'<a href="https://instagram.com/acme_cafe">ig</a>'
                 b'<a href="https://wa.me/96170123456">wa</a>'
                 b'<a href="mailto:hello@acme.example">x</a>')

    def _fake_session(self, status=200, body=LIVE_HTML):
        import enricher
        raw, resp = type("R", (), {"__init__": lambda s, b: setattr(s, "_b", b),
                                   "read": lambda s, n, decode_content=False: s._b}), None
        class _Resp:
            def __init__(self): self.status_code, self.encoding = status, "utf-8"
            @property
            def raw(self): return type("Raw", (), {"read": lambda s, n, d=False: body})()
            def close(self): pass
        class _Session:
            def get(self, url, **kw): return _Resp()
        self.addCleanup(setattr, enricher, "get_session", enricher.get_session)
        enricher.get_session = lambda: _Session()

    def test_score_equals_a_fresh_recomputation_after_enrich(self):
        """THE invariant. Fails on any reordering of enricher.py:337 vs :349."""
        import contextlib, io
        from enricher import completeness_score, enrich
        self._fake_session()
        rec = {"name": "Acme", "category": "restaurant", "country": "LB",
               "address": "Hamra, Beirut", "website": "https://acme.example",
               "source": "osm", "scraped_at": "2026-01-01"}
        with contextlib.redirect_stdout(io.StringIO()):
            out = enrich([rec])[0]
        self.assertEqual(out["email"], "hello@acme.example")
        self.assertEqual(out["completeness_score"], 5)          # addr+site+email+ig+wa  ✅
        self.assertEqual(out["completeness_score"],
                         completeness_score(out),
                         "enrich() must score strictly after check_websites()")

    def test_unreachable_site_does_not_invent_contacts(self):
        """No stub needed: with the suite's existing requests stub, get() raises,
        so _fetch_website takes the UNKNOWN path (enricher.py:176-178)."""
        import contextlib, io
        from enricher import enrich
        with contextlib.redirect_stdout(io.StringIO()):
            out = enrich([{"name": "Acme", "country": "LB",
                           "website": "https://acme.example", "source": "osm"}])[0]
        self.assertIsNone(out["website_live"])                  # ✅
        self.assertEqual(out["completeness_score"], 1)           # website only  ✅

    def test_enrich_is_idempotent_on_completeness(self):
        rec = {"name": "Clinic", "phone": "+9611222333", "category": "clinic",
               "address": "Hamra", "source": "osm"}
        with contextlib.redirect_stdout(io.StringIO()):
            once, twice = enrich([dict(rec)])[0], None
        with contextlib.redirect_stdout(io.StringIO()):
            twice = enrich([dict(once)])[0]
        self.assertEqual(once["completeness_score"], twice["completeness_score"])  # ✅ 2 == 2
```

Also pin the sentinel-zero merge case (087 test 5.1) **at the enrich boundary**, not at
`_merge`:

```python
    def test_stale_master_score_is_always_overwritten(self):
        fresh  = {"name": "Clinic", "phone": "+9611222333", "category": "clinic",
                  "address": "Hamra", "lead_score": 0, "completeness_score": 0,
                  "source": "osm", "scraped_at": "2026-10-03"}
        master = dict(fresh, lead_score=85, completeness_score=5, scraped_at="2026-09-01")
        merged = _merge(fresh, master)
        self.assertEqual(merged["completeness_score"], 5)        # ✅ (but see note)
        self.assertEqual(enrich([merged])[0]["completeness_score"], 2)   # ✅ recomputed
```
**Do not** write this as a `_merge` contract. ✅ `_merge` returns `5` only because
`dedup._pick`'s final tiebreak is `str(value)` (`dedup.py:184`) and `"5" > "0"`
lexicographically; `_merge(0, 7)` → `7` ✅ and `_merge(0, 9)` → `9` ✅. That is an accident
of lexicographic ordering, not a design. The guarantee that actually protects the output is
`enricher.py:349` overwriting unconditionally — pin *that*.

---

### S2 — Two different "is this lead reachable?" gates, and no test that they agree

- **Where:** `enricher.py:103` (truthiness) vs `main.py:137-145` (`has_any_contact`, which
  requires ≥7 digits, an `@` plus `len>5`, or an Instagram handle `len>3`)
- **Breaks:** the two disagree, so a single row can be simultaneously in
  `qualified_businesses.csv` and absent from `sales_ready.csv` for no reason a salesperson
  can see. ✅ verified disagreements on `phone`: `"private"`, `"---"`, `"12345"`, `"n/a"` all
  score `≥1` while `has_any_contact` returns `False`. On `email`: `"a@b"` scores `1` and
  `has_any_contact` returns `False`. On the other side, ✅ `{"email":"noreply@"}` passes
  *both* gates.
- **Trigger:** `{"phone": "private", "address": "Hamra, Beirut"}` → qualified, never
  sales-ready.
- **Fix:** make `completeness_score` consume the same predicates `has_any_contact` uses
  (`main.py:192-194` already has `normalize_phone` in hand), or export one shared
  `reachable_channels(record)` and have both call it.

**The test** (this is a genuine property, so write it as one):

```python
class TestReachabilityGatesAgree(unittest.TestCase):
    PHONE_CASES = ["+96170123456", "05 1234567", "70123456", "private", "---",
                   "12345", "n/a", "yes", "  +96170123456  ", ""]
    EMAIL_CASES = ["a@b.com", "sales@acme.example", "a@b", "@", "noreply@",
                   "mailto:", "user@"]

    def test_phone_gate_is_identical(self):
        for p in self.PHONE_CASES:
            rec = {"phone": p}
            self.assertEqual(completeness_score(rec) >= 1, has_any_contact(rec),
                             f"phone={p!r}: qualified_businesses and sales_ready disagree")
    # ✅ fails today on "private", "---", "12345", "n/a", "yes"

    def test_email_gate_is_identical(self):
        for e in self.EMAIL_CASES:
            rec = {"email": e}
            self.assertEqual(completeness_score(rec) >= 1, has_any_contact(rec),
                             f"email={e!r}: gates disagree")
    # ✅ fails today on "a@b", "@", "mailto:", "user@"
```

`{"address": "Hamra, Beirut"}` scores `1` while `has_any_contact` is `False`. That one is a
**spec decision, not a bug** — see §Not a bug.

---

### S2 — The documented ceiling, the field count, and the range gate are all unchecked

- **Where:** `README.md:64` ("0–7 count of filled contact fields"),
  `docs/audits/049-data-dictionary.md:282-283` (0–7), `docs/audits/048-data-quality-validation.md:47`
  (requires `0 <= completeness_score <= 7`), `cli.py:135-201` (`cmd_validate` checks names,
  BOM, coords, rating, duplicates — **not** `completeness_score`)
- **Breaks:** there are 8 counted fields and 7 possible points because `enricher.py:111`
  collapses `facebook`/`instagram`. `README.md:46` describes the gate as "at least one
  **contact** signal", which is not what `address` is. And `leadminer validate` passes a CSV
  containing `completeness_score=42`, so a corrupted or half-written master is indistinguishable
  from a healthy one. `docs/audits/048:134, 202` asks for exactly this validation and it was
  never implemented.
- **Trigger:** write a CSV with `completeness_score=42`; `cmd_validate` returns `0`.
- **Fix:** add a range check to `cmd_validate`, and change `README.md:64` to
  "0–7 count of filled contact fields (`facebook`/`instagram` share one point)".

**The tests:**

```python
class TestCompletenessCeilingAndRangeGate(unittest.TestCase):
    def test_eight_fields_but_seven_points(self):
        # 8 fields, 7 points. If facebook/instagram are ever split, this is the
        # test that must change -- and README.md:64 must change with it.
        self.assertEqual(len(FIELDS), 8)
        self.assertEqual(completeness_score(dict.fromkeys(FIELDS, "x")), 7)

    def test_validate_rejects_out_of_range_completeness(self):   # needs cli.py:135 change
        # reuse TestCli._fixture(d, completeness_score="42")
        rc = cmd_validate(argparse.Namespace(file=p))
        self.assertEqual(rc, 1)
        self.assertIn("completeness", out.lower())

    def test_validate_rejects_negative_completeness(self):
        # reuse TestCli._fixture(d, completeness_score="-1")
        ...  # asserts rc == 1
```

---

### S3 — Testability: `enrich()` cannot be exercised offline without two workarounds

- **Where:** `enricher.py:327` (`import urllib3` inside `enrich`, with nothing left that
  needs it now that TLS verification is back on), `enricher.py:128`
  (`from httpclient import get_session`)
- **Breaks:** in a bare checkout ✅ `enrich()` raises `ModuleNotFoundError: No module named
  'urllib3'` with no network call attempted — the import is unconditional at function scope.
  And the suite's existing `_stub_requests()` (`tests/test_lead_signal.py:23-63`) is *not*
  sufficient to control `enrich()`: `get_session()` builds a `requests.Session` from the
  stubbed module, whose `.get` always raises, so every site comes back `UNKNOWN`. That is
  usable (test 3.2 above relies on it) but it makes it impossible to test the `LIVE` path
  without monkeypatching `enricher.get_session`.
- **Trigger:** `python3 -c "import sys; sys.path.insert(0,'.'); from enricher import enrich; enrich([])"` in an env without `urllib3`.
- **Fix:** delete `enricher.py:327-328` (dead `disable_warnings` for a warning the code no
  longer triggers), and export the seam — `enricher.check_websites(records, session_factory=...)`
  or simply document `enricher.get_session` as the patch point.

**The test** (a test that fails until the dead import goes):

```python
class TestEnrichIsOffline(unittest.TestCase):
    def test_enrich_needs_no_third_party_import_at_call_time(self):
        """enrich() must not import urllib3 (enricher.py:327) -- nothing left to silence."""
        import ast, pathlib
        tree = ast.parse(pathlib.Path("enricher.py").read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "enrich")
        imported = {a.name for n in ast.walk(fn) if isinstance(n, ast.Import)
                    for a in n.names}
        self.assertNotIn("urllib3", imported)
```

---

### S3 — Record shape is unstable after `enrich()`, which makes assertions fragile

- **Where:** `enricher.py:251-259` vs `enricher.py:340-348`
- **Breaks:** `enrich()` `setdefault`s `facebook`, `instagram`, `whatsapp`, `linkedin`,
  `website_live`, `rating`, `review_count` — but **not** `email`, `phone`, `website` or
  `address`. `check_websites` only creates `email` on the `LIVE` path. ✅ A record whose site
  is `DEAD` or `UNKNOWN` comes out of `enrich()` with **no `email` key at all**, and a test
  written as `out["email"]` raises `KeyError` while the same test on a `LIVE` record passes.
  `write_csv` copes (`csv.DictWriter` + `restval`, `main.py:125-127`) and
  `completeness_score` copes (`.get`), so this is hygiene — but it is exactly the kind of
  thing that makes a future test suite flaky-by-accident.
- **Trigger:** ✅ `enrich([{"name":"Acme","country":"LB","website":"https://acme.example","source":"osm"}])[0]`
  → `"email" in out` is `False`.
- **Fix:** add `r.setdefault("email", None)` (and siblings) to `enricher.py:340-348`.

**The test:**

```python
    def test_all_scored_fields_exist_after_enrich(self):
        with contextlib.redirect_stdout(io.StringIO()):
            out = enrich([{"name": "Acme", "country": "LB",
                           "website": "https://acme.example", "source": "osm"}])[0]
        for f in FIELDS:
            self.assertIn(f, out, f"completeness_score reads {f!r}; it must exist after enrich")
```

---

## Properties vs examples — what each test is really pinning

| Property | Test | Why a property, not an example |
|---|---|---|
| Blank/None/absent are interchangeable | 1.1 | 8 fields × 2 forms; an example set would cover 2 of 16 cases |
| Monotone in presence | 1.2 | Catches any re-weighting that makes a richer record score lower |
| `0 ≤ score ≤ 7`, integral | 1.4 | Exhaustive over all 256 masks — complete coverage of the domain in one loop |
| Score is a pure function of the 8 fields | 1.3 | Any dependence on key order or on the record's own prior score is a bug class, not a case |
| Post-`enrich()` self-consistency | S1-ordering `test_score_equals_a_fresh_recomputation_after_enrich` | Holds for *every* record and *every* code path, including all three fetch outcomes — no example list can keep up |
| Idempotence | `test_enrich_is_idempotent_on_completeness` | Re-running the pipeline on the master CSV must not move the number |
| Reachability gates agree | S2-gates | 9 phones + 7 emails today; the property survives new junk shapes |
| Derived score never survives from input | `test_stale_master_score_is_always_overwritten` | Covers all `0..7` stale values and both merge orders at once |

Examples are still right for **1.5, 1.6, 1.8** and the range gate, because those pin
*specific* documented numbers and messages rather than a relation.

---

## What genuinely cannot be tested here, and why

1. **Whether the seven weights are correct.** `address` is worth the same as `linkedin`;
   `whatsapp` (the dominant channel in Lebanon per `docs/audits/017-osm-overpass-query.md:55`)
   is worth the same as `rating`-adjacent trivia. That is a product-calibration question with
   no oracle. A test can only lock in the status quo, so only the *structure* (ceiling 7,
   social collapse, monotonicity, range) is worth a test — the individual weights are not.
2. **Distribution or golden-file calibration.** `data/` is gitignored and
   `docs/audits/032-fixtures-offline-replay.md` proposed `tests/fixtures/` that does not
   exist. There is no recorded run to diff against, so "does the score distribution look
   right" is untestable until a run is recorded — and recording one requires the network
   this audit forbids.
3. **Anything about `check_websites` scheduling.** 40 workers, `as_completed`
   (`enricher.py:237-262`). Completion order is nondeterministic; never assert on the
   progress lines (`enricher.py:262`) or on the order of the summary counts
   (`enricher.py:271-275`). Only final record state is assertable. (I hit this myself: a
   first draft asserted `instagram == "acme_cafe"` and failed nondeterministically against a
   stub whose body argument was silently ignored.)
4. **The quality of what `_fetch_website` extracts** — which feeds 4 of the 7 points.
   ✅ With a `mailto:` before an `instagram.com/` link, `_INSTAGRAM_RE`'s `@` branch
   (`enricher.py:131`) matches inside the email address and the record gets
   `instagram="acme.example"`; ✅ reversing the link order yields `"acme_cafe"`. Whether that
   is acceptable is not a `completeness_score` question, and asserting today's value would
   bake a live bug into a fixture. Test the enricher's *use* of contacts (tests 3.1–3.3), not
   the extraction.
5. **End-to-end `qualified_businesses.csv` membership.** `main.main()` runs three live
   scrapers (`main.py:155-168`) with no injection seam, so the file cannot be produced
   offline. The closest honest proxy is test 1.7 (the predicate) plus the
   post-`enrich()` invariant — between them they cover every record the pipeline can produce.
6. **Non-`dict` inputs.** The signature says `dict` but nothing enforces it. ✅
   `completeness_score(None)` raises `AttributeError`. No caller ever does this
   (`enricher.py:349` passes dicts from `dedup.dedup`), so there is nothing to pin — record
   it as undefined rather than writing a test that blesses an exception.

---

## Not a bug, but worth knowing

- **The only product meaning is `0` vs `≥ 1`.** `main.py:206` is the sole branch. If the
  team wants the 0–7 number to mean something, that is a *spec* change (and then
  `qualified_businesses.csv` needs a documented threshold), not a test change. Write test 1.4
  now so the ceiling is at least pinned.
- **`address` alone qualifies a row.** `{"address": "Hamra, Beirut"}` → `1` ✅, so a business
  with no way to contact anyone reaches `qualified_businesses.csv`, contradicting
  `README.md:46` ("at least one contact signal"). Either accept it and say "contact *or*
  location" in the README, or exclude `address` from the qualification predicate — but do
  not leave it as an undocumented accident.
- **`whatsapp` and `linkedin` are unreachable from two of three sources.** OSM hardcodes
  `whatsapp=None` (`scrapers/osm.py:113`) and Google hardcodes all four
  (`scrapers/google_places.py:294-297`), so those two arms fire only from `_fetch_website`
  (`enricher.py:212-220`) — i.e. **only for `LIVE` sites**. A dead or unreachable site can
  never earn them, and the 5/7 score is unreachable for most OSM rows. Related:
  `facebook` is scored at `enricher.py:111` but `_fetch_website` extracts no Facebook at all
  (`enricher.py:130-136`), exactly as `docs/audits/025-enricher-regex-precision.md:279`
  says. Not a test bug; do not write a test that assumes otherwise.
- **`pitch_recommender.py:48-52` reads the score and never uses it.** Either delete the dead
  coercion or wire it into the `if`-chain; until then, `recommend_service` cannot be broken
  *by* this function, which narrows the blast radius to exactly one CSV filter.
- **`cli.py:269` recomputes `lead_score` but not `completeness_score`.** `leadminer score`
  therefore leaves a stale `completeness_score` in the file. Harmless today (the field has no
  consumer after `main.py:206`), but it is a trap for whoever gives the 0–7 number meaning.

---

## Recommended order of work

1. **`tests/test_enricher_pure.py`, new file** — the pure-function class: 1.1–1.8 plus the
   three junk classes from S1. No network, no fixtures, stdlib-only, matching the style of
   `tests/test_lead_signal.py`. Expect **~11 failures** on first run; each failure is a real,
   already-verified defect, not a broken test.
2. **The post-`enrich()` invariant** (`test_score_equals_a_fresh_recomputation_after_enrich`)
   plus the `UNKNOWN` and idempotence siblings. Highest value per line in this report: one
   assertion guards the four-commit ordering regression and any future reordering by
   `docs/audits/044`.
3. **Fix the S1 junk path** — score from validated values, not truthiness: reuse
   `dedup._is_missing` / `dedup._validity` / `normalize_phone`, and move
   `main.py:192-194` ahead of scoring. Turn the failing tests green; do not weaken them.
4. **Unify the two reachability gates** behind one helper, then land the S2 gate-agreement
   properties.
5. **Range gate in `cmd_validate`** + the `README.md:46, 64` wording, then the ceiling tests.
6. **Testability cleanups** — remove the dead `import urllib3` (`enricher.py:327`), add the
   missing `setdefault`s (`enricher.py:340-348`), document `enricher.get_session` as the
   HTTP seam.

**Do not** write a `_merge`-level test asserting `completeness_score == 5` survives a fresh
scrape. It passes today only because `"5" > "0"` lexicographically (`dedup.py:184`); assert
the enrich-boundary guarantee instead.

---

## Appendix — file layout

```
tests/
  test_lead_signal.py      existing, 20 tests, unchanged
  test_enricher_pure.py    new: 11 property/boundary tests + 4 junk classes (offline)
  test_enrich_ordering.py  new: enrich() post-condition + idempotence (monkeypatched session)
```

Run with `python3 -m unittest discover -s tests -v` (stdlib only, as
`tests/test_lead_signal.py:4-6` documents) or `pytest` (`pyproject.toml:75-78`;
`hypothesis>=6.100` is already a declared dev dependency at `pyproject.toml:25` if the
exhaustive-mask loops above are later replaced with `@given` strategies).