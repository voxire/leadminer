# 368 — `completeness_score` correctness (`enricher.py:101`)

## Verdict

`completeness_score` is 16 lines of `if record.get(...)` truthiness tests with **no
validity check at all**, and it is the sole gate on `qualified_businesses.csv`
(`main.py:206`) — the pipeline's second-most-consumed deliverable. It awards a point
for any non-empty string, including `" "`, `"n/a"`, `";"` and `"yes"`, and it is
computed at `enricher.py:349` **before** `main.py:194` blanks the phone column with
`normalize_phone`, so the number it stamps on a row can be provably wrong about that
same row. The result is that `qualified_businesses.csv` accumulates businesses with
zero contact channels, permanently, across every run.

The one thing the function *does* guarantee is `0 <= score <= 7` — verified below.

**Every finding below was executed, not reasoned about.** `requests`/`urllib3` were
stubbed in `sys.modules` (no installs, no network); the real `enricher.completeness_score`,
real `enricher.enrich`, real `dedup`, and real `main.load_master`/`main.write_csv` were
imported from the repo.

---

## Findings

### S1 — The score is computed on the *pre-normalization* phone, so it is provably inconsistent with the row it annotates

- **Where:** `enricher.py:349` (`r["completeness_score"] = completeness_score(r)`, inside
  `enrich()`, called from `main.py:187`) runs **before** `main.py:193-194`
  (`r["phone"] = normalize_phone(raw_phone, ...)`).
- **Breaks:** `completeness_score` reads `enricher.py:103` `record.get("phone")` and sees
  whatever the scraper emitted. `normalize_phone` (`dedup.py:33-63`) then rejects anything
  with fewer than 7 digits (`dedup.py:45-46`) and returns `""`. The score keeps the point;
  the exported `phone` column is empty. The row then lands in `qualified_businesses.csv`
  with **no contact channel at all**.
- **Trigger:** `{"phone": "yes"}` — `phone=yes` is listed under "Possible tagging mistakes"
  in the OSM wiki, <https://wiki.openstreetmap.org/wiki/Key:phone>. `osm.py:89` passes
  `tags.get("phone")` through verbatim.

  Exact observed output, three simulated runs through the real `dedup` → `enrich` →
  `normalize_phone` → `write_csv` → `load_master` cycle:

  ```
  run 1: phone=None   completeness_score=1  in_qualified=True  sales_ready=False
  run 2: phone=None   completeness_score=1  in_qualified=True  sales_ready=False
  run 3: phone=None   completeness_score=1  in_qualified=True  sales_ready=False
  ```

  The point is **permanent**, not a one-run artifact: the scraper re-supplies `phone="yes"`
  every run, `dedup._pick` (`dedup.py:167-171`) keeps it because the master's `None` is
  *missing*, and `main.py:194` blanks it again.
- **Also a split-brain:** `lead_score` is recomputed at `main.py:197`, *after*
  normalization, so it correctly refuses the +15 (`enricher.py:292`). The two scores in
  the same row disagree about whether the business has a phone.
- **Every value that trips this:** `12345`, `abc`, `N/A`, `123456`, `+`, `00`, `--`,
  `961`, `0` — all score `1`, all export as `""`.
- **Fix:** add `r["completeness_score"] = completeness_score(r)` to the existing
  recompute block at `main.py:195-197`, which already exists for precisely this class of
  ordering bug.

### S1 — Truthiness, not validity: whitespace and placeholder strings each earn a point

- **Where:** `enricher.py:103`, `105`, `107`, `109`, `111`, `113`, `115` — seven bare
  `if record.get(field):` tests. No `.strip()`, no format check.
- **Breaks:** the codebase already has the right predicate — `dedup._is_missing`
  (`dedup.py:79-90`) treats `None`, `""` **and** whitespace-only strings as missing — but
  `completeness_score` does not use it. Two incompatible definitions of "missing" in one
  pipeline. `main.py:87-89` only converts `""` → `None`, so a `" "` cell written by a
  scraper survives the CSV round-trip untouched and keeps scoring forever.
- **Trigger → exact output** (all executed):

  | input | `completeness_score` |
  |---|---|
  | `{"phone": " "}` | `1` |
  | `{"phone": "yes"}` | `1` |
  | `{"address": " "}` | `1` |
  | `{"email": "n/a"}` | `1` |
  | `{"website": ";"}` | `1` |
  | `{"instagram": " "}` | `1` |
  | `{"phone": "-"}`, `{"phone": "?"}`, `{"phone": ".."}`, `{"phone": "tel:"}` | `1` each |

- **Breaks the documented contract:** `README.md:46` says `qualified_businesses.csv` is
  the "Subset with at least one contact signal". A whitespace string is not a contact
  signal.
- **Fix:** reuse the existing predicate — `if not _is_missing(record.get("phone")):` —
  importing `_is_missing` from `dedup`, or `if str(record.get("phone") or "").strip():`.

### S2 — `whatsapp` is scored as an independent channel but is usually the same number as `phone`

- **Where:** `enricher.py:113` (`if record.get("whatsapp"):`) vs `enricher.py:103`
  (`if record.get("phone"):`) — two separate points, no cross-check.
- **Breaks:** `osm.py:112` hardcodes `whatsapp=None`, so the only producer is
  `_WHATSAPP_RE` (`enricher.py:132-135`, applied at `enricher.py:212-214`) scraping a
  `wa.me/<digits>` link off the business's own site. That number is, by construction,
  the business's phone number. Two of the seven points — 28% of the documented scale
  (`README.md:64`, "0–7 count of filled contact fields") — are awarded for one dialable
  channel.
- **Trigger:** `{"phone": "+961 70 123 456", "whatsapp": "+96170123456", "address": "Beirut"}`
  → **`completeness_score` returns `3`**, of which 2 come from the same number. Confirmed
  `normalize_phone` maps both to the identical string `+96170123456`. End-to-end export
  confirms it: `phone='+96170123456'`, `whatsapp='+96170123456'`,
  `completeness_score=3`.
- **Fix:** `if record.get("whatsapp") and normalize_phone(record["whatsapp"]) != normalize_phone(record.get("phone") or ""):`

### S2 — `qualified_businesses.csv` is not what `README.md:46` claims: an address alone, a dead website alone, or a LinkedIn URL alone qualifies a business

- **Where:** `enricher.py:107` counts `website` with **no reference to `website_live`**;
  `enricher.py:109` counts `address`; `enricher.py:115` counts `linkedin`. Threshold is
  `main.py:206` `>= 1` out of a 0–7 range.
- **Breaks:** three distinct shapes get into a file whose stated purpose is "at least one
  contact signal", and every one of them is rejected by `has_any_contact`
  (`main.py:137-145`), i.e. they can never reach `sales_ready.csv`.

  | record | `completeness_score` | in `qualified_` | `has_any_contact` |
  |---|---|---|---|
  | `{"name": "Corner Shop", "address": "Cairo St, Beirut"}` | `1` | **yes** | `False` |
  | `{"website": "https://x.com", "website_live": False}` | `1` | **yes** | `False` |
  | `{"website": "https://x.com", "website_live": None}` | `1` | **yes** | `False` |
  | `{"linkedin": "https://linkedin.com/company/acme"}` | `1` | **yes** | `False` |

  Note the `website_live` row carefully: `enricher.py:250` goes to real trouble to
  distinguish `DEAD` from `UNKNOWN` because conflating them "inverts the core lead
  signal" (`enricher.py:164-167`), yet `completeness_score` ignores the tri-state
  entirely. A site that 404s and a site we never reached both score identically to a
  working one.
- **Fix:** gate the point on liveness (`if record.get("website") and record.get("website_live") is True:`),
  drop `address` (it is location, not contactability), and raise the threshold to `>= 2`.

### S2 — The richest source in the pipeline produces most of the zero scores

- **Where:** `enricher.py:101-117` inspects exactly 7 fields. It never reads `rating`,
  `review_count`, `lat`, `lon`, `category`, or `name`.
- **Breaks:** a Google Places business with a 4.6★ rating, 812 reviews and exact
  coordinates, but no `formattedAddress`, no `nationalPhoneNumber` and no `websiteUri`
  (all three are plain `place.get(...)` calls that return `None`,
  `google_places.py:286`, `289`, `291`) scores **0** and is dropped from
  `qualified_businesses.csv`.
- **Trigger:** `{"name": "Starbucks", "category": "cafe", "lat": 24.7136, "lon": 46.6753,
  "rating": 4.6, "review_count": 812, "address": None, "phone": None, "website": None,
  "email": None, "facebook": None, "instagram": None, "whatsapp": None, "linkedin": None}`
  → **`completeness_score` returns `0`**, `qualified = False`.
- **Fix:** add one point for a resolvable location — `if record.get("lat") is not None and record.get("lon") is not None: score += 1`.

### S2 — OSM multi-value and placeholder tags produce both a point and a garbage export

- **Where:** `enricher.py:103/105/107` reward any truthy string; `osm.py:89` and
  `osm.py:91` forward `tags.get("phone")` / `tags.get("website")` verbatim.
- **Breaks:** the OSM wiki is explicit that `;` is a multi-value separator —
  *"In case of multiple phone numbers, use `phone=number;number`"* and *"The character
  `;` can be used to separate multiple phone numbers"* (<https://wiki.openstreetmap.org/wiki/Key:phone>).
  `osm.py` never splits on it.
- **Trigger → exact wrong output:**
  - `{"phone": "+961 1 234 567;+961 3 765 432"}` → `completeness_score = 1`, and
    `main.py:194` exports **`phone = '+96112345679613765432'`** — a 20-digit string.
    `has_any_contact` (`main.py:141-142`) returns `True` on it, so the row is admitted to
    `sales_ready.csv` and a rep tries to dial it.
  - `{"website": "http://a.com;https://b.com"}` → `enricher.py:231` issues one GET on the
    concatenated string, which cannot resolve → `website_live = None`. The website still
    earned its point.
  - `{"email": "a@a.com;b@b.com"}` → `completeness_score` includes a point for a string
    that is not an email address and is not run through `_EMAIL_BLACKLIST`
    (`enricher.py:138-141`), which only website-extracted emails are.
- **Fix:** split on `;` and take the first value at `osm.py:89`/`:91`
  (`(tags.get("phone") or "").split(";")[0].strip() or None`).

### S3 — `leadminer score` never recomputes `completeness_score`, so it ships a stale one

- **Where:** `cli.py:265-272` recomputes `country`, `industry_priority`,
  `recommended_service` and `lead_score` — but not `completeness_score`, even though the
  command's own docstring (`cli.py:248`) is "Re-score an existing CSV with current rules".
- **Trigger → exact wrong output:** a CSV row with `completeness_score=7` and only `phone`
  populated (all six other channel cells blank). After running `cmd_score`'s exact loop:

  ```
  on disk               : completeness_score = 7
  after 'leadminer score': completeness_score = 7   lead_score = 30
  truth (completeness_score) = 1
  ```

  The row still clears `main.py:206`'s `>= 1` either way, so membership is unaffected —
  but the exported number is wrong by 6, and there is no offline path that corrects it.
- **Fix:** add `from enricher import completeness_score` at `cli.py:250` and
  `r["completeness_score"] = completeness_score(r)` inside the loop at `cli.py:269`.

### S3 — `recommend_service` reads `completeness_score` and never uses it

- **Where:** `pitch_recommender.py:48-52` normalizes `record.get("completeness_score")`
  into a local named `completeness`, through a `try/except`. A grep for `completeness` in
  that file returns **4 hits: lines 48, 50, 52 (assignments) and line 69 (a stale
  comment)**. No `if` reads it.
- **Breaks:** `completeness_score` therefore has exactly **one** functional consumer in
  the entire system — the `>= 1` comparison at `main.py:206`. That is why every severity
  above lands on a single threshold with no second opinion, and why the `website_live`
  nuance at `enricher.py:164-167` never reaches the pitch column.
- **Fix:** delete `pitch_recommender.py:48-52`, or actually use `completeness` in the
  Tier-3 branch at `pitch_recommender.py:70-71` where the comment already claims it does.

---

## Not a bug, but worth knowing

- **The range invariant `0 <= completeness_score <= 7` does hold.** Seven `if` statements,
  each adding exactly 1, with no other arithmetic and no caller-supplied weights.
  Empirically: all 7 counted fields present → `7`; dropping any single one → `6`;
  `{}` → `0`. This matches `README.md:64`, `docs/audits/048-data-quality-validation.md:47`
  and `scrapers/base.py:28`. It is the only thing the function guarantees.
- **The ordering *inside* `enrich()` is correct.** `check_websites` runs at
  `enricher.py:337`, the score at `enricher.py:349`. So HTML-extracted `email`,
  `instagram`, `whatsapp` and `linkedin` (`enricher.py:252-259`) do count. Verified:
  a record goes `2` → `5` across the website pass. This is the fix `main.py:195-197`
  applies to `lead_score`; the *cross-function* ordering (S1 above) is what is still broken.
- **No score ratcheting across runs.** `enricher.py:349` overwrites unconditionally, so a
  business that loses a contact channel loses the point. Verified: score stays `5` when
  `website_live` later flips to `False` (the website keeps its point — that is the S2
  finding, not ratcheting).
- **The sentinel-zero merge bug from `docs/audits/002` is genuinely fixed.** Scrapers emit
  `completeness_score=0` (`osm.py:121`, `wikidata.py:129`, `google_places.py:304`) and
  master rows carry a computed value; `dedup._pick` now returns early on `_is_missing`
  (`dedup.py:167-171`) and falls through to a `str(value)` tiebreak (`dedup.py:184`) that
  keeps the master's `"5"` over the scraper's `"0"`. Caveat: that tiebreak is
  **lexicographic**, so it would silently invert at `completeness_score = 10` — and it is
  moot anyway because `enricher.py:349` overwrites the merge result.
- **The function cannot raise.** It is `.get()`-only, with no casts or comparisons.
  `{}` → `0`; `{"facebook": []}` → `0`; `{"phone": 0}` → `0`; `{"website_live": False}`
  → `0`. So `main.py:206`'s `r.get("completeness_score", 0) >= 1` can never hit the
  `None >= int` `TypeError` that `docs/audits/020` warned about — `enricher.py:349`
  guarantees an `int` for every record that reaches it.
- **`linkedin` is the only counted field that contributes to no downstream decision.**
  It is absent from `lead_score` (`enricher.py:283-319`) and from `has_any_contact`
  (`main.py:137-145`), yet it is a point toward `qualified_businesses.csv`. Note also that
  `enricher.py:136` will match `linkedin.com/company/<anything>` from any site's shared
  nav bar; only `{"company", "in", "pub"}` are blacklisted (`enricher.py:219`), so a
  false-positive LinkedIn handle is worth a free point.

---

## Recommended order of work

1. **Make the predicate honest (S1 #2).** One line per field: swap truthiness for
   `dedup._is_missing`. This kills the largest class of wrong rows — whitespace,
   `n/a`, `yes`, `;` — and removes the duplicate definition of "missing" in the codebase.
2. **Score after normalization (S1 #1).** Add `r["completeness_score"] = completeness_score(r)`
   to the block at `main.py:195-197`. Or move `normalize_phone` from `main.py:194` to
   just before `enrich(records)` at `main.py:187`, which also fixes the dedup key path.
3. **Split OSM `;` lists at the scraper boundary (S2).** `osm.py:89` and `osm.py:91`.
   Cheapest fix with the widest blast radius: it removes the 20-digit phone export and
   the unreachable-website fetch in one go.
4. **Stop double-counting `whatsapp` (S2).** Compare normalized values before awarding
   the second point.
5. **Redefine `qualified` (S2 ×2).** Raise `main.py:206` to `>= 2`, gate the website point
   on `website_live is True`, drop `address`, and credit `lat`/`lon` so rated Google
   businesses are not excluded. Write the new rule down in `README.md:46`.
6. **Close the two hygiene gaps (S3 ×2).** Add the recompute to `cli.py:265-272`; delete
   or use the dead `completeness` local at `pitch_recommender.py:48-52`.

Steps 1–3 are independent, mechanical, and together remove every confirmed phantom point.
Do them together; individually the output stays wrong.
