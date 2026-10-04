# 394 — `lead_score`: what is still untested, and the tests it needs

**Target:** `lead_score()` — `enricher.py:283-319`
**Callers (3, all reachable):**
- `enricher.py:350` — inside `enrich()`, unconditionally, for every record
- `main.py:197` — the authoritative recompute, after `industry_priority` is set at `main.py:190`
- `cli.py:269` — `leadminer score`, the operator's re-score command, over a whole CSV

**Existing coverage:** `tests/test_lead_signal.py:76-78` (1 assertion) and
`tests/test_lead_signal.py:80-83` (1 `assertGreater`, no exact value). Two assertions
in 28 tests. Suite is green: `python3 -m unittest discover -s tests` → `Ran 28 tests … OK`.

**How everything below was verified:** `lead_score` was imported and called directly.
It is pure (`re` + dict reads, no I/O, no globals), so it needs no stubbing beyond the
`_stub_requests()` already at `test_lead_signal.py:23-63` (`enricher.py:2` and
`httpclient.py:43` both import `requests`, which is **not installed** in this checkout —
the stub is mandatory, not decorative). `enrich()` is exercisable **offline** for records
with no website, because `check_websites` short-circuits at `enricher.py:232-233`.
Every asserted number in this report is an observed value, not a derivation.

---

## Verdict

`lead_score` is a 37-line pure function that turns out to be *exactly* an additive sum of
eight booleans — I cross-checked it against an executable spec over all **1024 states
with 0 mismatches**, so it is trivially and cheaply testable. The suite tests almost none
of it: of the eight signals, **seven have no assertion at all** (`email`, `whatsapp`,
`phone`, `instagram`, live-site, `rating < 4.0`, `medium` priority), the eighth (`dead`,
+20) is asserted only as an ordering with no value, and `multi-source` is pinned only in
its negative form. `industry_priority="high"` → 15 is pinned, but only *transitively*,
because every other field in that fixture is absent.

The consequence is that `sales_ready.csv` ordering rests on nine integer literals
(`enricher.py:289,291,293,295,302,304,308,310,314,317`) that **no test reads**. A
rebalance proposed by `009-lead-score-model.md` or `043-scoring-upgrade.md`, or a
one-character typo, silently re-orders the entire export and the suite stays green. I
proved this by mutation: across 13 mutations to the weights and guards, the existing suite
catches **1**; a 1024-state spec property catches **13 of 13**. A fourteenth mutation is
unreachable by construction and is discussed under "What cannot be tested".

The most valuable single addition is therefore not an example table. It is one
`itertools.product` loop that asserts `lead_score(record) == spec(8 booleans)`.

---

## Findings

### S1 — The entire weight table is unpinned; seven of eight signals have no assertion

- **Where:** `enricher.py:288-317`. Existing coverage: `tests/test_lead_signal.py:76-83`.
- **Breaks:** the score *is* the product — it decides the row order of `sales_ready.csv`
  (`BRIEF.md:55`) and is the only number a rep sees before dialling (`060-sales-handoff-contract.md:25`).
  Nothing verifies it. Signal-by-signal:

  | signal | weight | line | asserted anywhere? |
  |---|---|---|---|
  | `email` | +20 | `:288-289` | **no** |
  | `whatsapp` | +15 | `:290-291` | **no** |
  | `phone` ≥7 digits | +15 | `:292-293` | **no** |
  | `instagram` | +10 | `:294-295` | **no** |
  | website live | +10 | `:301-302` | **no** (only `None` is tested) |
  | website dead | +20 | `:303-304` | ordering only, no value |
  | priority high | +15 | `:307-308` | transitively, via `test_lead_signal.py:78` |
  | priority medium | +8 | `:309-310` | **no** |
  | `rating < 4.0` | +10 | `:312-314` | **no** |
  | multi-source | +5 | `:316-317` | transitively as 0 |
  | `min(score, 100)` | clamp | `:319` | **no** |

- **Trigger (mutation testing, run against a copy of the tree):**

  | mutation | existing suite | proposed suite |
  |---|---|---|
  | `enricher.py:293` phone `+15` → `+10` | **OK** | FAILED (27) |
  | `enricher.py:304` dead `+20` → `+15` | **OK** | FAILED (27) |
  | `enricher.py:288` `if email` → `if email or phone` | **OK** | FAILED (27) |
  | `enricher.py:313` `rating is not None and rating < 4.0` → `if rating:` | **OK** | FAILED (15) |
  | `enricher.py:319` `min(score, 100)` → `score` | **OK** | FAILED (3) |
  | `enricher.py:292` `>= 7` → `>= 6` | **OK** | FAILED (2) |
  | `enricher.py:309` `== "medium"` → `== "low"` | **OK** | FAILED (2) |
  | `enricher.py:316` drop the `"|"` requirement | **OK** | FAILED (15) |
  | `enricher.py:301` `if live is True:` → `if live is not None and live is not False:` | **OK** | FAILED (9) |
  | `enricher.py:294` count `facebook` too | **OK** | FAILED (1) |
  | `enricher.py:290` count `linkedin` too | **OK** | FAILED (1) |
  | `enricher.py:303` drop the `record.get("website") and` guard | **OK** | FAILED (1) |
  | **the 043 pre-fix inversion** (`+10 if live is True else 20`) | FAILED (2) | FAILED (17) |

  The last row is the only mutation the current suite catches, and it is caught by
  `test_lead_signal.py:76-78` alone — the one historical bug that suite was built for.
- **Fix:** add `test_matches_executable_spec_on_all_1024_states` and
  `test_attainable_scores_are_exactly_forty_values` (below). No source change needed.

### S2 — A `rating` that arrives as a string raises `TypeError`, and the caller is unguarded

- **Where:** `enricher.py:313` — `if rating is not None and rating < 4.0:`, a bare
  comparison against `float`. Callers: `enricher.py:350` (inside an unguarded `for` loop
  at `:339`), `main.py:197` (inside an unguarded `for` loop at `:189`), `cli.py:269`
  (inside an unguarded `for` loop at `:265`). **No `try` on any of the three paths, and
  all five `write_csv` calls come after them** (`main.py:209-213`).
- **Breaks:** one record with a string `rating` raises
  `TypeError: '<' not supported between instances of 'str' and 'float'`, aborting the run
  *after* three scrapers have been paid for and after the multi-hour enrichment pass, with
  **zero CSVs written**.
- **Trigger (verified):** `lead_score({"name": "X", "source": "osm", "rating": "3.5"})`
  → `TypeError`. Same for `"bad"`.
- **Reachability, stated honestly:** **not** reachable via `main()` or `leadminer score`
  today, because `load_master` casts `rating` at `main.py:90-95`. It is one caller away,
  and the codebase has already decided that a string rating is a legitimate input:
  - `dedup._validity` explicitly coerces it — `float(value)` at `dedup.py:135-139`,
    so a string rating scores validity 3 and **survives `_merge`**;
  - the test suite's own canonical CSV fixture writes `rating: "4.2"` as a string
    (`tests/test_lead_signal.py:201`);
  - `cli.py:87` and `cli.py:143` build `csv.DictReader` rows with no casting at all, so
    any caller that feeds those rows to `lead_score` crashes.
- **Also note the asymmetry that makes this a bug rather than a style note:** the two
  sibling consumers of the same field both defend against it —
  `pitch_recommender._is_low_rating` wraps `float(rating)` in `try/except`
  (`pitch_recommender.py:135-140`) and `dedup._validity` does the same
  (`dedup.py:135-139`). `lead_score` is the only one of the three that does not.
- **Fix:** `try: low = float(rating) < 4.0; except (TypeError, ValueError): low = False`
  — and reuse `_is_low_rating`. **Test:** `test_string_rating_raises` (documents today's
  behaviour, so the fix is a deliberate flip) or, better, the test asserts the *fixed*
  behaviour and is written first.

### S2 — A non-canonical `website_live` scores silently as "no website", with no error

- **Where:** `enricher.py:300-304` — `if live is True: … elif record.get("website") and
  live is False: …`. `is True` / `is False` are **identity** comparisons, and the `else`
  is a silent fall-through: any value that is not the singleton `True` or the singleton
  `False` scores 0 and raises nothing.
- **Breaks:** `"True"`, `"False"`, `1`, `0` — every value a spreadsheet, a JSON API
  change, or a hand-edit can produce — collapses to "no website signal". A live site
  silently loses 10 points; a **confirmed dead site silently loses 20**, which is the
  single strongest pitch in the product (`pitch_recommender.py:55-58`).
- **Trigger (verified, end-to-end through `load_master`):**

  | `website_live` cell | `load_master` yields | `lead_score` |
  |---|---|---|
  | `True` | `True` | 25 |
  | `False` | `False` | 35 |
  | `TRUE` / `true` / `1` / `False ` / `yes` | `None` | **25 → 0 signal** |

  (`load_master` at `main.py:103` does `wl == "True" else wl == "False" else None`, so the
  cast itself drops the value; `lead_score` then cannot distinguish "unreachable" from
  "the operator typed TRUE in Sheets".) The CSVs are handed to humans by design —
  `write_csv` uses `utf-8-sig` explicitly so **Excel and Google Sheets** render Arabic
  names (`main.py:118-120`) — so a spreadsheet round-trip is the expected path, not an edge
  case. `leadminer score` is the operator's documented recovery command (`cli.py:12`).
- **Fix:** normalise the tri-state at the boundary — one helper
  (`coerce_website_live(value) -> bool | None`) used by both `load_master` and
  `lead_score`, raising or logging on an unrecognised value instead of scoring it as `None`.
- **Pins:** the *intent* of `043-scoring-upgrade.md:12-13` (never conflate unreachable with
  dead) extended to the case the fix missed — conflating unreachable with *malformed*.

### S2 — `enrich()` still scores before `industry_priority` exists; the defect is live and untested

- **Where:** `enricher.py:350` calls `lead_score(r)` inside `enrich()`, but
  `industry_priority` is not assigned until `main.py:190`. `lead_score` reads it at
  `enricher.py:306-310`. `main.py:197` then recomputes and overwrites, so the exported
  value is right — but the first computation is dead work and it is wrong.
- **Breaks:** two distinct failures. (a) A fresh record gets 0 priority points during
  `enrich()` — up to 15 low. (b) A **master-loaded row carries last run's *string*
  priority** (`load_master` re-casts floats/ints/`website_live` at `main.py:90-103` but
  never `industry_priority`), so during `enrich()` those rows are scored under a
  *different, stale* rule than the fresh rows — the two halves of the deduped set are
  scored by different rubrics inside the same pass.
- **Trigger (verified, fully offline — no website, so `check_websites` returns at `:232`):**

  | record | `enrich()` `lead_score` | after `main.py:190-197` | gap |
  |---|---|---|---|
  | `category="restaurant"`, phone, no website | 15 | 30 | **15** |
  | `category="bakery"` (medium), phone, no website | 15 | 23 | **8** |
  | `category="bakery"` **carrying stale `industry_priority="high"`** | **30** | 23 | **−7, wrong direction** |
- **Why S2 not S1:** the exported CSVs are correct because `main.py` recomputes. Any
  standalone caller of `enrich()` — a notebook, a new CLI verb, the incremental-scraping
  path proposed in `035-incremental-scraping.md` — reads 15-point-low numbers.
- **Fix:** delete `enricher.py:350`; `main.py:197` is the single source of truth
  (`012-main-orchestration-order.md:34`, `043-scoring-upgrade.md:751`).
- **Pins:** `012` S2 and `043` S2 — both filed it, **neither was implemented**, and
  `tests/test_lead_signal.py` has no case. `enrich()` has **zero** tests anywhere in the
  suite; only `infer_region` (`:152`) and `recommend_service` (`:88-96`) are covered.

### S3 — The two website branches guard different preconditions

- **Where:** `enricher.py:301` (`if live is True:` — no `website` check) versus
  `enricher.py:303` (`elif record.get("website") and live is False:` — requires one).
- **Breaks (verified):** `lead_score({"…", "website_live": True})` with **no** `website`
  key scores **25** — identical to a record that genuinely has a live site. The mirror
  case is guarded: `website_live=False` with no website scores 15, same as no site. So
  `True` and `False` are treated asymmetrically within a single three-line block.
- **Trigger:** any caller that hand-builds a record. `check_websites` only sets
  `website_live` for records that have a website (`enricher.py:231`), so the pipeline
  cannot produce it — but `lead_score` is a public function (`cli.py:250` imports it by
  name), and the moment `website` and `website_live` are decided by different sources
  (`_merge` resolves each field independently, `dedup.py:159-194`) it becomes reachable.
- **Fix:** make both branches require `record.get("website")`, and raise on
  `website_live is True and not record.get("website")`.
- **Test:** `test_live_bonus_does_not_require_a_website_field` — written to *document* the
  asymmetry today; flip its assertion when fixed.

### S3 — `rating` is not range-checked here, and `0` is scored as pain but not pitched as reputation

- **Where:** `enricher.py:312-314`. Compare `dedup._validity`, which rejects
  `not 0 <= v <= 5` (`dedup.py:139`), and `cli.cmd_validate`, which fails the gate on
  out-of-range ratings (`cli.py:175-188`). `lead_score` accepts anything comparable.
- **Breaks (all verified):** `rating=0` → **+10** ("low rating = pain point worth
  pitching", the comment at `:311`); `rating=-1` → **+10**; `rating=99` → **0**, so a
  corrupt high rating silently *removes* the signal; `rating=float("nan")` → 0
  (`nan < 4.0` is `False`).
- **And the two halves of the export disagree** (verified):

  | `rating` | `lead_score` | `recommend_service` |
  |---|---|---|
  | `0` | 20 — the +10 pain credit | `"Discovery call - scope the right service"` |
  | `3.9` | 20 — the same +10 | `"Reputation + SEO turnaround"` |
  | `None` | 10 | `"Discovery call - scope the right service"` |
  | `"3.5"` | **TypeError** | `"Reputation + SEO turnaround"` |

  The score says "this business has a reputation problem, prioritise it"; the pitch says
  "nothing to see here". `recommend_service` guards the field with a truthiness check
  (`pitch_recommender.py:73`, `if … and rating and _is_low_rating(rating)`) that
  `lead_score` does not mirror.
- **Fix:** reject `0` and out-of-range the way the two siblings already do, and make both
  functions call one `_is_low_rating`.

### S3 — Five fields earn completeness points and no lead-score points

- **Where:** `completeness_score` (`enricher.py:101-117`) counts `facebook`, `website`,
  `address`, `linkedin`. `lead_score` (`enricher.py:283-319`) reads none of them.
- **Breaks (verified):** `{"facebook": "https://x.example"}` →
  `lead_score = 0`, `completeness_score = 1`. Same for `linkedin`. A rep sorting on
  `lead_score` never sees a Facebook-only business, which is a large share of the
  `ADJACENT_BUSINESSES` tail in `scrapers/whitelist.py:45-64` — exactly the businesses
  with the least digital infrastructure.
- **Fix:** decide deliberately whether social presence is an opportunity signal, then
  make `lead_score` and `completeness_score` agree about it. This is the same
  double-counting complaint as `009-lead-score-model.md:37-53`, seen from the other side.
- **Test:** `test_facebook_and_linkedin_earn_completeness_but_not_score`.

### S3 — "no website" scores 0, so the flagship pitch target is ranked last

- **Where:** `enricher.py:301-304`. `absent = 0 < unreachable = 0 < live = 10 < dead = 20`.
- **Breaks (verified, restaurant + phone + high priority):** absent **30**, unreachable
  **30**, live **40**, dead **50**. `without_websites.csv` is the project's headline
  segment (`BRIEF.md:54`) and it scores **lower than a business whose site is broken** —
  and `absent == unreachable`, so a greenfield build and a site we could not reach are
  indistinguishable in the ranking.
- **Fix:** make the website dimension monotonic in opportunity
  (`009-lead-score-model.md:33-35`, `043-scoring-upgrade.md`).
- **Test:** ship it as `@unittest.expectedFailure` **now**
  (`test_absent_website_is_not_ranked_below_dead`). An xfail is a tracked debt item that
  flips to green on the day someone fixes the model; a passing test would freeze the bug.

---

## Test cases this function still needs

28 methods. **Every asserted value below is an observed value from running the function**,
and the whole file is green today (`Ran 28 tests … OK (expected failures=1)`), so it can be
committed as pure characterisation. Each entry names the past bug it pins.

### 1. The spec property — replaces every weight-table example

`enricher.py:288-319`. Prefer this to any per-signal example table: the function is
*provably* a sum of 8 booleans (1024 states, 0 mismatches), so one `itertools.product`
loop pins all nine weights at once and fails loudly on any single-number drift.

```python
SITE_POINTS    = {"absent": 0, "live": 10, "dead": 20, "unreachable": 0}
PRIORITY_POINTS = {"high": 15, "medium": 8, "low": 0, None: 0}

def spec(email, whatsapp, phone_ok, instagram, site, priority, low_rating, multi):
    return min(20 * email + 15 * whatsapp + 15 * phone_ok + 10 * instagram
               + SITE_POINTS[site] + PRIORITY_POINTS[priority]
               + 10 * low_rating + 5 * multi, 100)
```

| method | literal input | asserted | pins |
|---|---|---|---|
| `TestLeadScoreSpecProperty.test_matches_executable_spec_on_all_1024_states` | every combination of `{email,whatsapp,phone≥7,instagram} × {high,medium,low,None} × {rating 3.5,absent} × {single,multi-source} × {absent,live,dead,unreachable}` | `lead_score(r) == spec(...)` for all 1024; `n == 1024` | every weight in `enricher.py:289-317`; the S1 above |
| `TestLeadScoreSpecProperty.test_attainable_scores_are_exactly_forty_values` | same sweep, collect `set(lead_score(r))` | `len == 40`, `min == 0`, `max == 100` | the clamp at `:319`; 61 of the 101 values in 0–100 are **unreachable**, which is the concrete form of `009-lead-score-model.md:55-71` ("the clamp is dead code") |
| `TestLeadScoreSpecProperty.test_adding_a_signal_never_lowers_the_score` | base `{phone, industry_priority="medium"}` + each of 7 signals | `lead_score(base + s) >= lead_score(base)` | monotonicity; the sanity half of `043-scoring-upgrade.md:30` |
| `TestLeadScoreSpecProperty.test_score_is_bounded_and_integral` | `{}` and `{email:"a@b.com"}` | `isinstance(v, int)`, `0 <= v <= 100` | `048-data-quality-validation.md:135` (`0 <= lead_score <= 100`, currently unasserted) |
| `TestLeadScoreSpecProperty.test_cap_clamps_only_the_all_signals_dead_case` | all 8 signals with `website_live=False` / `=True` / `rating=4.5` | `100`, `100`, `100` (raw 110 / 100 / 100) | `:319`; the one configuration where the clamp binds |
| `TestLeadScoreSpecProperty.test_does_not_mutate_and_is_key_order_invariant` | `{email, phone, rating:3.5, website, website_live:False}`, keys shuffled 200× | record unchanged; `65` every time | purity — the precondition for trusting a cached/memoised score later |

### 2. Website signal (`enricher.py:300-304`)

| method | literal input | asserted | pins |
|---|---|---|---|
| `TestWebsiteSignal.test_absent_unreachable_and_dead_weights` | `{industry_priority:"high", source:"osm"}` + `{website,website_live}` = `{}` / `(url,None)` / `(url,True)` / `(url,False)` | `15`, `15`, `25`, `35` | the `043` inversion fix; extends `test_lead_signal.py:76-78` from "None is 15" to all four states |
| `TestWebsiteSignal.test_dead_outranks_live_by_exactly_ten` | same pair | `dead - live == 10` | replaces the `assertGreater` at `test_lead_signal.py:83` with an exact value |
| `TestWebsiteSignal.test_non_canonical_website_live_scores_silently_as_absent` | `website_live` ∈ `"True","False","TRUE","true","1",0,1,"yes",b"True"` | each `== 15`, i.e. **identical to no website** | the S2 above; written as a *characterisation* — flip when `coerce_website_live` lands |
| `TestWebsiteSignal.test_live_bonus_does_not_require_a_website_field` | `{website_live:True}` (no `website`) vs `{website,website_live:True}` | equal (`25`); and `{website_live:False}` (no website) `== 15` | the S3 guard asymmetry at `:301` vs `:303` |

### 3. Rating signal (`enricher.py:312-314`)

| method | literal input | asserted | pins |
|---|---|---|---|
| `TestRatingSignal.test_threshold_is_exclusive_at_four` | `rating` = `3.999`, `4.0`, `4.5`, `None` | `10`, `0`, `0`, `0` | the `<` vs `<=` boundary — untested today |
| `TestRatingSignal.test_string_rating_raises` | `rating` = `"3.5"`, `"bad"` | `TypeError` | the S2 above. **Prefer** asserting the fixed behaviour once the guard lands; this documents today |
| `TestRatingSignal.test_out_of_range_ratings_are_not_defended` | `0`, `-1`, `99`, `float("nan")` | `10`, `10`, `0`, `0` | the S3 above |
| `TestRatingSignal.test_zero_rating_is_scored_as_pain_but_not_pitched_as_reputation` | `{website, website_live:True, review_count:100, category:"electrician", rating:0}` vs `rating=3.9` | `lead_score` equal (both 20); `recommend_service` **differs** | the score/pitch disagreement at `:313` vs `pitch_recommender.py:73` |
| `TestRatingSignal.test_string_rating_kills_the_score_but_not_the_pitch` | same rows with `rating="3.5"` | `lead_score` raises; `recommend_service("3.5") == recommend_service(3.9)` | `_is_low_rating`'s guard (`pitch_recommender.py:135-140`) vs the absence of one |

### 4. Phone signal (`enricher.py:285,292-293`)

Prefer the **properties**; the presentation-variant test is the one that matters and it
replaces the per-variant example tables other audits propose.

| method | literal input | asserted | pins |
|---|---|---|---|
| `TestPhoneSignal.test_threshold_is_seven_unicode_digits` | `"123456"`, `"1234567"`, `"12345678901234567890"` | `0`, `15`, `15` | the threshold at `:292` — **zero** coverage today |
| `TestPhoneSignal.test_presentation_variants_score_identically` | `"+96170123456"`, `"96170123456"`, `"0096170123456"`, `"+961 70 123 456"`, `"(961) 70-123-456"`, `96170123456` (int) | all `15` | `012-main-orchestration-order.md:29` (order-insensitivity to `normalize_phone`); `055-phone-validation-deep.md:293` |
| `TestPhoneSignal.test_junk_scores_zero` | `""`, `None`, `"---"`, `"..."`, `"  "`, `"n/a"`, `"ext 4"`, `0` | all `0` | extends the junk corpus of `test_lead_signal.py:106` from 5 to 8 inputs |
| `TestPhoneSignal.test_digits_embedded_in_junk_text_still_count` | `"Tel: 01 234 567 ext 4"`, `"abc1234567def"`, `"961-70-123-456-EXT-99"` | all `15` | the gate is "≥7 Unicode digits anywhere", not "a parseable number" — `055`'s extension-merge row |
| `TestPhoneSignal.test_non_ascii_digits_count` | `"٠٧٩٠١٢٣٤"` (Arabic-Indic), `"０７９０１２３４"` (fullwidth) | `15` each | `\D` is Unicode-aware, so the score *agrees* with `has_any_contact` here while `dedup.normalize_phone` **disagrees** (`200-fn-dedup-py-normalize_phone-tests.md` S1: `normalize_phone("٠٧٩٠١٢٣٤","LB")` → `'+961٠٧٩٠١٢٣٤'`). **Canary:** unifying the two phone rules will change this number |
| `TestPhoneSignal.test_agrees_with_has_any_contact_on_the_phone_clause` | 11 values incl. `"123456"`, `"1234567"`, `0`, `"ext 4"` | `lead_score(r) == 15` ⟺ `has_any_contact(r)` | the duplicated rule at `enricher.py:285` vs `main.py:138`; `055-phone-validation-deep.md:286-288` says replace both with one call — this test makes the duplication visible until then |

### 5. Multi-source and priority signals (`enricher.py:306-310, 316-317`)

| method | literal input | asserted | pins |
|---|---|---|---|
| `TestMultiSourceSignal.test_bonus_is_flat_not_per_source` | `source` = `"osm"`, `"osm\|google_places"`, `"osm\|wikidata\|google_places"`, `None` | `0`, `5`, `5`, `0` | `"|" in …` at `:316` is a **flat +5**, not per-source. Untested, and `dedup._pick` produces exactly these strings (`dedup.py:174`) |
| `TestIndustryPrioritySignal.test_exact_string_match_only` | `"high"`→`15`, `"medium"`→`8`; `"low","High","HIGH"," medium","medium ","none","",None`→`0` | as listed | `==` at `:307`/`:309` is case- and whitespace-sensitive; `whitelist.industry_priority` (`whitelist.py:133-145`) returns lowercase today, so this is a **latent** coupling to that function's casing |

### 6. Lifecycle order (`enricher.py:350` vs `main.py:190-197`)

Fully offline — `check_websites` returns at `enricher.py:232-233` when no record has a
website, so no stub and no network.

| method | literal input | asserted | pins |
|---|---|---|---|
| `TestLeadScoreLifecycleOrder.test_enrich_alone_loses_the_priority_points` | `{category:"restaurant", country:"LB", address:"Hamra, Beirut", lat:33.89, lon:35.50, phone:"01 234 567", source:"osm"}` → `enrich([r])` | `lead_score == 15`; after `industry_priority(...)` → `30` | `012` S2 / `043` S2 — **both filed, neither implemented, neither tested**. Gap = 15 (high) / 8 (medium) |
| `TestLeadScoreLifecycleOrder.test_enrich_reads_stale_priority_from_a_master_row` | same but `category="bakery"` (truly medium) and `industry_priority="high"` (last run's value) | `enrich(...)[0]["lead_score"] == 30` — **higher** than the correct 23 | the stale-priority half; `012-main-orchestration-order.md:14` |

### 7. Fields it ignores, and the CSV boundary

| method | literal input | asserted | pins |
|---|---|---|---|
| `TestFieldsLeadScoreIgnores.test_facebook_and_linkedin_earn_completeness_but_not_score` | `{"facebook":"https://x.example"}`, `{"linkedin":…}` | `lead_score == 0`; `completeness_score == 1` | the S3 above |
| `TestFieldsLeadScoreIgnores.test_absent_website_is_not_ranked_below_dead` **(@expectedFailure)** | `{phone, industry_priority:"high"}` vs same `+ {website, website_live:False}` | `absent >= dead` — currently `30 >= 50` is false | `009` S2 / `043` — the model inversion, tracked as xfail debt |
| `TestCsvRoundTrip.test_canonical_cells_round_trip` | header + `X,restaurant,Hamra Beirut,LB,+96170123456,,https://a.example,True,3.5,high,osm` | `load_master` → `lead_score == 50` (15 phone + 10 live + 15 high + 10 rating) | the happy path of the `cli.py:269` caller, end-to-end |
| `TestCsvRoundTrip.test_spreadsheet_edited_cells_silently_zero_the_site_signal` | same row with `website_live` = `TRUE`, `true`, `1`, `False `, `yes` | `lead_score == 40` for each — the 10 points vanish, no error | the S2 above, through the operator's real recovery path |

### Harness note

`tests/test_lead_score.py` must `import test_lead_signal` **before** importing `enricher`
to get `_stub_requests()` (`test_lead_signal.py:23-63`), because `requests` is not
installed in a bare checkout and both `enricher.py:2` and `httpclient.py:43` import it at
module scope. `sys.path` needs both the repo root and `tests/`.

---

## Not a bug, but worth knowing

- **The function is genuinely pure.** No I/O, no globals, no mutation of the input,
  order-independent (verified over 200 shuffles). It is the most testable function in the
  codebase and the least tested. That is the whole finding.
- **The clamp at `enricher.py:319` binds in 1 of 768 states** — the all-signals
  dead-website record (raw 110). Every other combination is ≤ 100. `048`'s proposed
  `ERR_REF_LEAD_SCORE_OVERFLOW` assertion would therefore never fire on real data.
- **The phone gate is an equivalent mutant.** Trimming to the last 7 digits
  (`phone = phone[-7:]`) then testing `>= 7` is the *same function* as testing the whole
  string's length. No black-box test can distinguish them, and I confirmed the survivor
  empirically. Do not spend time trying.
- **`lead_score` and `completeness_score` are 60% redundant by construction** (`009` S2).
  A property test asserting `lead_score >= something * completeness_score` would be
  meaningless; the useful cross-function properties here are the two *disagreements*
  documented above (`has_any_contact` on the phone clause, `recommend_service` on rating).
- **The score is not persisted with a version.** No `score_version` column exists, so a
  weight change silently re-scores historical rows on the next `leadminer score`
  (`cli.py:247-277`) with nothing to distinguish "75 under the old rules" from "75 under
  the new". `043-scoring-upgrade.md:40` flags this; a test cannot fix it, but
  `test_attainable_scores_are_exactly_forty_values` will make any rebalance visible in the
  diff, which is the most a test can do here.

## What genuinely cannot be tested here, and why

1. **Whether a DEAD verdict is *correct*.** `lead_score` consumes `website_live`; it never
   fetches. Deciding that a site is dead requires `_fetch_website` (`enricher.py:161-222`)
   and real HTTP. That is `enricher.check_websites`'s test surface and it needs
   `responses`/`requests-mock` or a hand-rolled `get_session` fake. **Out of scope for this
   function** — and note the audit that owns it must first settle the SSRF question in
   `008-enricher-ssrf-security.md` before a fake-fetch harness is worth building.
2. **Whether the ranking is *good*.** Every test above can prove the score is
   *reproducible*; none can prove it is *right*. There is no labelled data and no closed
   loop — no conversions, no replies, no won deals (`060-sales-handoff-contract.md`).
   "absent website should outrank dead website" (S3 above) is a **product assertion, not a
   derived one**, which is exactly why it must ship as `@unittest.expectedFailure`: it
   encodes a decision nobody has formally made, and it must not be laundered into a
   passing assertion by someone who picks whichever side makes CI green.
3. **Whether the weights are *calibrated*.** `60 * P(reachability) + 40 * P(opportunity)`
   has no fitted parameters. A test asserting "the top 10% of rows are the best leads"
   is unfalsifiable until there is outcome data. The reachable step is
   `048-data-quality-validation.md`'s bound check, which `test_score_is_bounded_and_integral`
   covers.
4. **The `enrich()` stale-priority case needs a real master CSV to be faithful.** My
   reproduction injects the stale string directly; `test_lead_signal.py`'s existing
   `TestCli._fixture` (`tests/test_lead_signal.py:195-205`) is the right place to grow a
   true multi-run fixture (write CSV → `load_master` → re-enrich), but that needs the
   fixture harness `242-x.md:235-289` describes and one HTTP fake for the website pass.
5. **Thread-safety of the score.** Irrelevant — the function is called single-threaded at
   all three call sites; only `check_websites` is concurrent.

## Recommended order of work

1. **Commit `TestLeadScoreSpecProperty`** (5 methods, ~40 lines, no source change, no
   fixtures). It converts the whole weight table from implicit to asserted and catches 12
   of 13 mutations. Highest value per line in the entire test suite.
2. **Commit the phone, multi-source and priority property groups** — 8 methods, no source
   change. They close the six zero-coverage signals and pin the
   `lead_score`/`has_any_contact` duplication.
3. **Commit the website and rating characterisation groups** — 9 methods, all asserting
   *today's* behaviour including the three defects. These turn the S2/S3 findings above
   into tracked debt that flips visibly on the day someone fixes them.
4. **Then fix, in this order:** (a) `coerce_website_live` shared by `load_master` and
   `lead_score` (S2, silent data loss); (b) the `float()` guard on `rating`, ideally by
   reusing `pitch_recommender._is_low_rating` (S2, run-dies); (c) delete
   `enricher.py:350` (S2, 15 points low for standalone callers).
5. **Last, and only with a product decision:** the absent-website monotonicity change
   (`009` S2 / `043`). Keep the `@unittest.expectedFailure` until the weights are
   rebalanced and a `score_version` column exists, so a re-score is distinguishable from
   the original.