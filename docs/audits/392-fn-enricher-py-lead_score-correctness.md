# 392 — `lead_score()` correctness audit

**Target:** `lead_score()` at `enricher.py:283-319`
**Lens:** correctness — does the published number answer the question the CSVs ask?
**Method:** every claim below was executed against the real module (imported with
`requests`/`urllib3` stubbed exactly as `tests/test_lead_signal.py:23-63` does; no
network, no files written, no source modified). Outputs are quoted verbatim.

**Callers (complete list):**

| Caller | Line | Priority available? | Phone state |
|---|---|---|---|
| `enrich()` | `enricher.py:350` | **no** — `main.py:190` sets it later | **raw** (pre-normalization) |
| `main.main()` | `main.py:197` | yes (`main.py:190`) | normalized (`main.py:194`) |
| `cli.cmd_score()` | `cli.py:269` | yes (`cli.py:267`) | **raw** (never normalized) |

> **Line-number drift:** earlier audits cite this function at `enricher.py:241-259`
> (`009`) and `275-311` (`043`). Current body is `283-319`; use these numbers.
> `393-fn-enricher-py-lead_score-edge.md` audits the same function under the *edge*
> lens (boundary/hostile inputs) and already owns: the `rating` `TypeError`, non-finite
> and out-of-range ratings, the `min()` clamp, `\D` digit counting, `website_live`
> identity checks, and the missing `review_count` gate. This report deliberately does
> **not** repeat those; it covers the semantic correctness of what gets scored, which
> survives all of 393's edge fixes.

---

## Verdict

The arithmetic is fine and the function is pure, total over the record types the
pipeline actually produces, and correctly refuses to treat an unreachable site as a
dead one. What it gets wrong is **the set of things it counts**. `lead_score`,
`completeness_score` (`enricher.py:101-117`) and `has_any_contact` (`main.py:137-145`)
define "this lead has a channel" three different ways, so one run ships
`qualified_businesses.csv`, `sales_ready.csv` and a `lead_score` column that
contradict each other about the same row. On top of that, `lead_score` pays full price
for strings that are not channels at all (`"n/a"`, `" "`, `"-"`), it structurally caps
the one segment the product exists to sell to (`without_websites.csv`, `BRIEF.md:54`) at
**75 of 100**, and it pays +5 for "multi-source confirmation" that a dedup collision
manufactured. The output is a plausible-looking integer answering a different question
than the one the sales team asks of it.

**The invariant it is missing, stated precisely:** *every field `lead_score` rewards is
a validated, non-placeholder value of its declared type, and the set of rewarded
fields is the same set the rest of the pipeline uses to decide a lead is reachable.*
Both halves fail today, independently and by different mechanisms.

**No new crash from this lens.** The only input class that raises is a non-numeric
`rating` (`TypeError` at `enricher.py:313`), and both CSV callers coerce it at
`main.py:90-95` before it can arrive. 393 rates that S1; I rate a *live* wrong-output
defect above a *hypothetical* crash, which is why my S1 is different from theirs.

---

## Findings

### S1 — "Has a contact channel" is defined three incompatible ways, and one run ships all three answers

- **Where:** `enricher.py:287-295` (scores `email`, `whatsapp`, `phone`, `instagram`)
  vs `enricher.py:111-116` (`completeness_score` also counts `facebook`, `whatsapp`,
  `linkedin`) vs `main.py:137-145` (`has_any_contact` counts only phone/email/instagram).
- **Breaks:** `lead_score` never reads `facebook` or `linkedin`, and `has_any_contact`
  never reads `facebook`, `whatsapp` or `linkedin`. Executed, one channel per record
  (`industry_priority="high"` throughout, so the numbers differ only by channel):

  | record | `lead_score` | `completeness_score` | `has_any_contact` | in `qualified` | in `sales_ready` |
  |---|---|---|---|---|---|
  | `facebook="https://facebook.com/levantdental"` | **15** | 1 | False | **yes** | **no** |
  | `instagram="levantdental"` (same audience) | **25** | 1 | True | yes | yes |
  | `linkedin="https://linkedin.com/company/levant-law"` | **15** | 1 | False | **yes** | **no** |
  | `whatsapp="+96170123456"` | **30** | 1 | False | **yes** | **no** |
  | `phone="+96170123456"` | 30 | 1 | True | yes | yes |
  | `email="info@levant.eg"` | 35 | 1 | True | yes | yes |
  | `website="https://levant.eg", website_live=True` | 25 | 1 | False | **yes** | **no** |

  Three rows are published as *qualified leads with a score* and simultaneously
  withheld from the one export that is supposed to be actionable. The same business
  with the same audience is worth 15 through Facebook and 25 through Instagram. And
  the segment this hits hardest is the one `whitelist.py:33-35` labels
  "professional services (high LTV)" — a law firm, accountant or real-estate agency
  whose only channel is a LinkedIn company page scores **15** and never reaches
  `sales_ready.csv`.
- **Why it is S1:** it is not a preference, it is a contradiction between two files
  that ship in the same run (`main.py:210` vs `main.py:213`), it is mechanically
  enumerable (every row whose only channel is facebook/linkedin/whatsapp/website), and
  `lead_score` is the column the team sorts by. Fixing it changes published numbers,
  so it must be decided deliberately rather than discovered by a rep.
- **Trigger:** `{"facebook": "https://facebook.com/levantdental", "industry_priority": "high"}`
  → `lead_score` **15**, `completeness_score` **1**, `has_any_contact` **False**.
  The row is in `qualified_businesses.csv`, out of `sales_ready.csv`, and ranked below
  an email-only lead.
- **Fix:** define one `reachable_channels(record) -> set[str]` helper and have
  `lead_score`, `completeness_score` and `has_any_contact` all call it — one line each,
  e.g. score facebook/linkedin/whatsapp on the same footing and let
  `has_any_contact` test that same set instead of re-deriving it (`main.py:141-145`).

### S2 — Full points for values that are not channels: `"n/a"`, `" "`, `"-"`, `"none"`

- **Where:** `enricher.py:288-289` (`if record.get("email"): score += 20`), and
  identically `290-291` (`whatsapp`), `294-295` (`instagram`). Only `phone` is
  normalised and length-checked (`285`, `292-293`).
- **Breaks:** bare truthiness, so any non-empty string buys the full weight. Executed:

  ```
  email='n/a'   -> lead_score=35   completeness=1  has_any_contact=False
  email=' '     -> lead_score=35   completeness=1  has_any_contact=False
  email='-'     -> lead_score=35   completeness=1  has_any_contact=False
  email='none'  -> lead_score=35   completeness=1  has_any_contact=False
  email='info'  -> lead_score=35   completeness=1  has_any_contact=False
  email='email.com' -> lead_score=35  completeness=1  has_any_contact=False
  ```

  (baseline `{"industry_priority": "high"}` = 15, so the email is worth 20.)
  A **35-point** published lead with a *fake* email channel, and `main.py:142-143`
  (`"@" in email and len(email) > 5`) correctly refuses to put it in
  `sales_ready.csv`. Two exports of the same run disagree about the same row, which is
  the S1 contradiction showing up with a value instead of a field.
- **Reachability:** OSM tags are free text and land verbatim at `osm.py:90`
  (`email = tags.get("email")`); `dedup._is_missing` (`dedup.py:79-90`) treats
  non-empty strings as present, so the junk survives the merge. It is then written to
  the master (`main.py:213`) and `main.py:88` (`if row[k] == ""`) only nulls the
  **empty** string, so `" "` and `"N/A"` round-trip forever, run after run.
  `_EMAIL_BLACKLIST` (`enricher.py:138-141`) filters scraper output but is never
  applied on the scoring path, and it would not catch these anyway — it matches
  domains, not placeholders.
- **Fix:** validate once at the top of `lead_score` and treat placeholders as absent,
  e.g. `email = str(record.get("email") or "").strip(); if EMAIL_OK.match(email): score += 20`
  reusing `dedup._EMAIL_OK` (`dedup.py:116`) so the gate and the scorer cannot drift.

### S2 — The score rises monotonically with "already has a website"; the target segment is capped at 75/100 by construction

- **Where:** `enricher.py:300-304` (`live +10`, `dead +20`, `absent +0`) and
  `enricher.py:290-291` (`whatsapp +15`).
- **Breaks:** this is 009's non-monotonicity finding (`009-lead-score-model.md:20-33`)
  with new measurements that make it exact rather than impressionistic.
  1. `whatsapp` is **only ever** set from a successful website fetch. All three
     scrapers hard-code it to `None` — `osm.py:112`, `wikidata.py:120`,
     `google_places.py:295` — and the sole write is `enricher.py:257`, inside
     `if outcome == LIVE:` at `enricher.py:251`. So **+15 is unreachable for every
     row without a website.** (`instagram` is the exception: `osm.py:111` can supply it
     from a tag.)
  2. Therefore the arithmetic ceiling of a no-website row is
     `20+15+10+15+10+5 = 75`, measured, while a website row reaches 100 (live) or
     110 (dead, then clamped). The flagship segment `BRIEF.md:54` describes as
     "new-site pitch targets" **cannot outrank a lead that already has a website. Ever.**
  3. `+10` for a *live* site is a reward for having nothing to sell.

  Measured, same business, one variable changed:

  ```
  A  phone + website(live) + whatsapp + instagram, high, 4.5*   -> 65
  B  phone, no website,                     high, 4.5*         -> 30
  B' B + an OSM-sourced email                                  -> 50
  ```

  A outranks B by 35 points. A needs no new site; B is the pitch.
- **Trigger:** any `without_websites.csv` row vs its website-having twin; sort
  `all_businesses.csv` by `lead_score` descending and the pitch targets sit at the
  bottom, below businesses that already have live sites and full contact sets.
- **Fix:** add the missing third branch so the dimension is monotonic —
  `elif not record.get("website"): score += 20  # greenfield build` — with
  `dead >= live >= absent`, and re-score + diff the master before shipping because this
  moves a large share of rows.

### S2 — "+5 multi-source confirmation" is paid for a dedup name collision

- **Where:** `enricher.py:316-317` (`if "|" in str(record.get("source") or "")`),
  consuming the field written by `dedup._pick` at `dedup.py:173-174`.
- **Breaks:** the comment calls this "Multi-source confirmation". All it actually
  tests is that the `source` string contains a pipe — i.e. that *dedup merged two rows*.
  When a record with no phone merges on the name key (`dedup.py:212-214`,
  `key = (normalize_name(name), city)`), two unrelated businesses are fused and the
  merged row is credited with corroboration that does not exist.
- **Trigger (executed end-to-end through `dedup()` then `lead_score()`):**

  ```python
  one = {"name": "Star Cafe", "address": "Main St, Sidon", "phone": None,
         "category": "cafe", "source": "osm", "rating": None, "website": None}
  two = {"name": "Star Cafe", "address": "Blv 8, Sidon", "phone": None,
         "category": "cafe", "source": "google_places", "rating": 4.9, "website": None}
  dedup([one, two])  -> 1 row, source='google_places|osm'
  lead_score(row)    -> 5        # the +5 is for two different cafes
  ```

  The merge itself is `dedup`'s bug, but the +5 is `lead_score`'s: it converts a
  known-bad merge into a positive trust signal in the published ranking. This matters
  more than its size because `source` is the provenance column the rest of the master
  work is built on, and `316-fn-dedup-py-_sources_of-contract.md` already flags that
  the scorer should consume a precomputed source count instead of parsing the string.
- **Fix:** take the count, not the delimiter — `if len(_sources_of(record)) >= 2: score += 5`,
  and never let a name-key merge contribute (dedup would need to record *how* it merged).

### S3 — `min(score, 100)` erases a full signal, and 009's "it never binds" is wrong

- **Where:** `enricher.py:319`; weights at `288-317` sum to **110**.
- **Breaks:** `009-lead-score-model.md:59-64` concludes the clamp "effectively never
  binds". Measured, it binds for exactly the best leads — and it does not merely clamp,
  it creates **ties between leads that differ by a whole signal**:

  | record | raw | published |
  |---|---|---|
  | all 8 signals | 110 | **100** |
  | identical, Instagram handle not on the page | 100 | **100** |
  | identical, IG present, rating ≥ 4 | 90 | 90 |
  | identical, IG present, no rating, single source | 85 | 85 |

  Rows 1 and 2 are 10 raw points apart and publish identically. With a *live* site the
  maximum is exactly 100 (no bind); with a *dead* site it is 110 (binds) — so the clamp
  only ever fires on the strongest leads, which is the worst place to lose resolution.
  393 has this as an S2; I record only the correction to 009's claim.
- **Fix:** either raise `max_score` to the real 110 (see `036-config-management.md:54`,
  which already names `scoring.max_score`) or normalise the weights so the scale tops
  out at 100.

### S3 — `website_live is True` is not gated on `website`, unlike the dead branch beside it

- **Where:** `enricher.py:301-304`. The dead branch is guarded
  (`elif record.get("website") and live is False`); the live branch is not.
- **Trigger (executed):**

  ```
  {"website": None, "website_live": True}    -> 25   # phantom +10, no website exists
  {"website": "",    "website_live": True}   -> 25   # phantom +10
  {"website": None, "website_live": False}   -> 15   # correctly guarded
  ```

  `pitch_recommender.py:34-36` guards both of its branches with `has_website`; only the
  scorer half-guards.
- **Reachability — honest:** I could not make this fire from pipeline-produced data.
  `check_websites` only sets `website_live` for rows that have a website
  (`enricher.py:231`, `250`), and `dedup._pick` (`dedup.py:167-171`) cannot return a
  missing `website` when either side has one, so `website_live is True ⟹ website` is an
  invariant of the current pipeline. It fires on any CSV not written by this pipeline —
  the same population 393 shows `leadminer score` silently corrupts.
- **Fix:** hoist one guard above the branch —
  `if record.get("website") and live is True: ... elif record.get("website") and live is False: ...`

### S3 — `leadminer score` and the pipeline apply different phone rules

- **Where:** `cli.py:269` scores the raw CSV `phone`; `main.py:194` normalizes first
  and `main.py:197` scores the normalized value.
- **Breaks (executed):** for any CSV cell whose normalization shrinks it below 7
  digits, the same row scores differently depending on which entry point produced it:

  | csv cell | pipeline (`main.py:194`+`197`) | `leadminer score` (`cli.py:269`) |
  |---|---|---|
  | `"0096 123"` | `+96123` → **0** | **15** |
  | `"0096123"` | `+96123` → **0** | **15** |
  | `"0000000"` | `+961` → **0** | **15** |
  | `"+96170123456"` | 15 | 15 |
  | `"123 456"` | `""` → 0 | 0 |

  `leadminer score` is documented as "re-score an existing CSV with current rules"
  (`cli.py:248`); for this input class it does not reproduce the pipeline's rules, and
  `cmd_score` defaults `--out` to the input path (`cli.py:274`), so the divergent
  number is written back over the master.
- **Invariant that makes it safe otherwise:** `normalize_phone` preserves digit count
  except when it strips an `00` prefix (`dedup.py:50-52`), and a pipeline-written CSV
  can never contain a leading `00` because normalization already removed it. Verified
  on every realistic value above. So this only bites hand-edited or pre-normalization
  CSVs — but that is precisely the population the command exists for.
- **Fix:** normalize inside the scorer, as 393 recommends for `website_live` —
  `digits = re.sub(r"\D", "", normalize_phone(str(record.get("phone") or ""), record.get("country") or "LB"))`
  — and drop `main.py:194`'s ordering dependency.

---

## Not a bug, but worth knowing

- **`lead_score` is pure.** Verified: the input dict is byte-identical after the call.
  No mutation, no hidden state, no ordering dependence — so it is trivially replayable
  and testable, and the double call at `enricher.py:350` + `main.py:197` is at worst
  wasted work, never corruption.
- **The score computed at `enricher.py:350` is knowingly wrong and is discarded.**
  Measured for a fresh scraper record (`industry_priority=None`, as `osm.py:116` and
  `google_places.py:299` emit): `enrich()` returns **15**, `main.py:190`+`197` returns
  **30**. It is also computed from the *pre-normalization* phone. This is already
  documented at length (`012-main-orchestration-order.md:5`, `043:46`, `044:5`,
  `083-SYNTH-s1-backlog.md:23` item) and is **correct today only because
  `main.py:197` recomputes** — `cli.cmd_run` is a thin wrapper over `main.main()`
  (`cli.py:33-37`), so there is no shipped path that publishes the bad value.
- **`rating` handling is inconsistent with the rest of the repo, in the *lenient*
  direction, and only for two values:** `0.0` and `nan`. `lead_score(0.0)` = **25**,
  identical to `3.9` — but `0.0` is an explicitly *valid* rating per this repo's own
  contracts (`dedup.py:139` `0 <= v <= 5`, `cli.py:185` the same) and Google documents
  rating as "from 1.0 to 5.0" (Places field reference, `places.rating`), so a 0 means
  "unrated", not "0-star reputation". `float("nan")` survives `main.py:93` and silently
  *suppresses* the bonus (`nan < 4.0` is False) while `cmd_validate` (`cli.py:185-188`)
  fails the row. Already covered by `345:120` and `048:218-220`; the fix belongs at the
  scoring site (`0.0 < rating < 4.0`, plus 393's `review_count` sample gate).
- **The `+10` Instagram bonus is the easiest signal to fabricate, and the fabrication
  is upstream.** Measured against the real `_fetch_website` with a fake session, body
  `<a href="mailto:info@levantcafe.com">` → stored
  `instagram='levantcafe.com'` (the shop's own email domain), and that record scores
  **55** instead of 45. Cause is the bare `@` alternative in `_INSTAGRAM_RE`
  (`enricher.py:131`), fully documented at `006:30-38`, `025:37`, `_diag_d1:24`,
  `_diag_d7:45`. Not counted again here — but note the *scoring* consequence: this is the
  cheapest +10 in the function to earn by accident, and no scorer-side validation would
  catch it.
- **`\D` agrees with `dedup` on what a digit is.** Both use `re.sub(r"\D", "", ...)`
  (`enricher.py:285`, `dedup.py:44`), and Unicode `Nd` digits survive in both, so
  `lead_score` and the dedup key can never disagree. Accidental, but it is the one
  agreement you should not break when fixing the S3 phone finding — keep one function.
- **`lead_score` has no consumer that filters on it.** `grep` finds exactly three
  callers plus the CSV writer; `main.py:199-206` gates every export on
  `completeness_score`, `has_any_contact` and `industry_priority`, never on
  `lead_score`. This caps the blast radius of every scoring defect above at "a human
  sorts by the wrong column" — which is why the S1 is a contradiction between two
  exports rather than a crash.

---

## Recommended order of work

1. **S1 — one channel definition.** Extract `reachable_channels(record)` and call it
   from `enricher.py:287-295`, `enricher.py:111-116` and `main.py:141-145`. This is the
   only finding that makes two shipped CSVs agree with each other. Re-score and diff
   the master before shipping; expect facebook/linkedin/whatsapp rows to move.
2. **S2 — placeholder validation.** Strip and pattern-match `email`/`whatsapp`/
   `instagram` instead of truthiness (`enricher.py:288-295`), reusing `dedup._EMAIL_OK`.
   One helper, three call sites; kills the 35-point junk-email rows.
3. **S2 — monotonic website dimension.** Add the absent-website branch
   (`enricher.py:303`) so `dead >= live >= absent`, then diff the score distribution:
   this moves the entire `without_websites.csv` segment upward, which is the point.
4. **S2 — source count, not pipe count** (`enricher.py:316-317`); coordinate with
   `316-fn-dedup-py-_sources_of-contract.md` and record *how* a row merged.
5. **S3s.** `max_score` → 110 (or renormalise weights); hoist the `website` guard above
   `enricher.py:301`; normalize the phone inside the scorer so `cli.py:269` and
   `main.py:197` cannot diverge.
6. **With 393:** the `rating` guard (`enricher.py:312-314`) is a one-line `float()`
   `try` and is a prerequisite for any storage change (the SQLite migration in
   `033-storage-sqlite-migration.md` will hand back whatever the column type says), so
   do it before the persistence work, not after.