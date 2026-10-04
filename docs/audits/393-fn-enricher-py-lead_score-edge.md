# 393 — `lead_score()` edge / hostile-input audit

**Target:** `lead_score()` at `enricher.py:283-319`
**Lens:** edge — boundary and hostile inputs
**Method:** every claim below was executed against the real function (imported with
`requests`/`urllib3` stubbed, no network, no files written). Results are quoted verbatim
from those runs.

> **Line-number drift warning.** Earlier audits cite this function at the pre-refactor
> positions `enricher.py:241-259` (`009-lead-score-model.md`) and `enricher.py:275-311`
> (`043-scoring-upgrade.md`, `049-data-dictionary.md:294-295`, `066-typing-at-scale.md:270`).
> The file has since been edited; the current body is `enricher.py:283-319`. Follow the
> current numbers.

---

## Verdict

`lead_score()` has exactly **one** input class with no defensive branch at all: `rating`.
Any non-numeric `rating` raises an unhandled `TypeError` at `enricher.py:313`, and from
the `enrich()` call site (`enricher.py:350`) that fires **after** the entire 40-thread
website pass — the multi-hour stage the workflow's `timeout-minutes: 300` exists for — so
the whole run dies with zero CSV output. Everything else degrades quietly rather than
loudly, which is arguably worse for trust: a `phone` field containing a pasted URL scores
+15 and lands the lead in `sales_ready.csv`; `min(score, 100)` (`enricher.py:319`) ties
materially different leads; and `leadminer score` writes those losses back over the master
CSV in place.

---

## The contract, as declared

| Source | Promise |
|---|---|
| `scrapers/base.py:21` | `rating: float \| None` |
| `scrapers/base.py:16` | `website_live: bool \| None` (tri-state) |
| `scrapers/base.py:8` | `phone: str \| None` |
| `enricher.py:283` | `def lead_score(record: dict) -> int` |
| `enricher.py:280` | header comment: "Lead quality score (0–100)" |
| `README.md:65` | "`lead_score` \| 0–100 weighted quality score" |

`BusinessRecord` is a `TypedDict` — **runtime-inert**. Nothing at `enricher.py:283`
validates any field it reads. The only enforcement anywhere in the repo is the CSV cast at
`main.py:90-97`, which is 700 lines away from the comparison it protects.

**Maximum attainable raw score: 110**, not 100
(`20+15+15+10+20+15+10+5`, verified: an all-signals record returns `100`).

---

## Input matrix — classification of every boundary class

Executed against the real `enricher.lead_score`. Base record
`{"industry_priority": "high", "source": "osm"}` = 15.

### `rating` — `enricher.py:312-314`

| Input | Result | Class |
|---|---|---|
| key absent / `None` | `15` | ✅ correctly handled |
| `4.0` (exact boundary) | `15` (no bonus) | ✅ correct (`<` is strict) |
| `3.999999` | `25` | ✅ correct |
| `0` / `0.0` | `25` (**+10**) | ⚠️ **silently wrong** — out of the 0–5 range two other modules enforce |
| `-3.0` | `25` (**+10**) | ⚠️ **silently wrong** — negative rating is a "pain point" |
| `-inf` | `25` (**+10**) | ⚠️ **silently wrong** |
| `float("nan")` | `15` (bonus lost, silently) | ⚠️ **silently wrong + poisons the export** |
| `inf` / `"inf"` | `15` | ⚠️ silently wrong (out of range) |
| `True` | `25` (**+10**) | ⚠️ **silently wrong** — `bool` is not a rating |
| `"4.2"` | **`TypeError`** | 💥 **crash, unhandled** |
| `"3.9"`, `"0"`, `"٣٫٩"`, `"four stars"` | **`TypeError`** | 💥 **crash, unhandled** |
| `[]` | **`TypeError`** | 💥 **crash, unhandled** |

### `phone` — `enricher.py:285, 292-293` (base score 15)

| Input | Digits counted | Result | Class |
|---|---|---|---|
| `None`, `""`, `"   "`, `"\t\n"`, `0`, `0.0` | 0 | `15` | ✅ correct (`or ""` catches falsy) |
| `"ext 4"`, `"---"`, `"n/a"` | 1 / 0 / 0 | `15` | ✅ correct |
| `"📞📞📞📞📞📞📞"` | 0 | `15` | ✅ correct (emoji are not `\d`) |
| `"+961 70 123 456"`, `"70123456"`, `"9617012345"` | 11 / 8 / 10 | `30` | ✅ correct |
| `"٠٧٠١٢٣٤٥٦"`, `"+٩٦١٧٠١٢٣٤٥٦"` (Arabic-Indic) | 9 / 11 | `30` | ✅ arguably correct — `\d` matches Unicode `Nd`, and `dedup.normalize_phone` uses the identical regex, so the two agree |
| **`"1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"` (7 emoji keycaps)** | **7** | **`30` (+15)** | ⚠️ **silently wrong** — `U+FE0F U+20E3` are stripped, leaving the seven digits |
| **`"https://example.com/pricing" + a 7-digit path`** | **7+** | **`30` (+15)** | ⚠️ **silently wrong** — a URL in the phone field is a phone |
| **`"0000000"`, `"9999999"`** | 7 | **`30` (+15)** | ⚠️ **silently wrong** — zero-padded/round numbers pass |
| `"1"*20000` | 20000 | `30` | ✅ correct and **O(n)** — no ReDoS, no truncation crash |
| `12345678.0` (float) | 9 | `30` (+15) | ⚠️ silently wrong — `.0` becomes a digit |
| `["+961","70123456"]`, `{"n":"70123456"}` | 11 / 8 | `30` (+15) | ⚠️ silently wrong — `str()` of a container |

### `website_live` — `enricher.py:300-304` (base score 15)

| Input | Result | Class |
|---|---|---|
| `True` / `False` / `None` | `25` / `35` / `15` | ✅ correct; the tri-state is respected exactly |
| `False` but `website` absent | `15` (no +20) | ✅ correct — a dead site needs a URL |
| `"True"` / `"False"` (strings) | `15` | ⚠️ **silently wrong** — identity comparison rejects both |
| `1` / `0` / `"true"` / `"TRUE"` / `"yes"` / `""` | `15` | ⚠️ **silently wrong** — all collapse to "unreachable" |

### Other fields

| Input | Result | Class |
|---|---|---|
| `industry_priority = "High"` / `"HIGH"` | `0` bonus | ✅ internally unreachable — `whitelist.py:139,142,144,145` only ever returns lowercase, and `main.py:190`/`cli.py:267` always overwrite |
| `industry_priority = None` (the `enrich()` state) | `0` bonus | ⚠️ by design — `main.py:195-197` recomputes; see `046-dead-code-simplification.md` S3 |
| `source = "osm\|google_places"` | `+5` | ✅ correct — `dedup._pick` joins with exactly this separator (`dedup.py:174`) |
| `source = ["osm","google_places"]` (list) | `+0` | ⚠️ silently wrong — `str(list)` has no `\|` |
| `email = "-"` / `"noreply"` | `+20` | ⚠️ `lead_score` applies no validation; `_fetch_website` filters 8 domains (`enricher.py:138-141`) but only for HTML-derived mail |
| `review_count` = 1 vs 5000 (with `rating=3.9`) | `25` vs `25` | ⚠️ **silently wrong** — see S3 below |
| `lead_score({})`, all keys `None` | `0` | ✅ correct |
| `lead_score(None)` / `([])` / `("record")` / `(42)` | **`AttributeError`** | 💥 crash — contract says `dict` |

---

## Findings

### S1 — `rating` type confusion raises an unhandled `TypeError` at the worst possible moment

- **Where:** `enricher.py:313` — `if rating is not None and rating < 4.0:`
- **Contract violated:** `scrapers/base.py:21` declares `rating: float | None`.
- **Breaks:** `<` between `str` and `float` raises. Nothing between the record and this
  line coerces, validates, or try/excepts. This is the **only** field in the function with
  no defensive branch — compare `pitch_recommender.py:43-52,135-140`, which wraps the very
  same field in `float()` + `try/except`. The two consumers of `rating` guard it
  differently, and `066-typing-at-scale.md:118-124` already spotted the asymmetry; this
  entry supplies the concrete consequence the run has today.
- **Trigger (direct call, verified):**
  ```python
  >>> from enricher import lead_score
  >>> lead_score({"rating": "4.2", "industry_priority": "high", "source": "osm"})
  TypeError: '<' not supported between instances of 'str' and 'float'
  ```
  Identical traceback for `"3.9"`, `"0"`, `"٣٫٩"`, `"four stars"`, and `[]`.
- **Why S1 — the blast radius, not the likelihood:** `lead_score` is called at
  `enricher.py:350`, which sits **after** `check_websites(records)` at `enricher.py:337`.
  The exception escapes `enrich()` → `main.py:187` is a bare `records = enrich(records)`
  with no `try` → `cli.main` (`cli.py:314-317`) catches only `KeyboardInterrupt`. The
  process dies with a traceback **after** every website has been fetched, and none of the
  five CSVs in `main.py:209-213` are written. One upstream type slip converts a completed
  multi-hour run into nothing.
- **Reachability today: closed, but only by luck.** All three shipped scrapers yield
  `rating=None` or a JSON number (`osm.py:114`, `wikidata.py:122`,
  `google_places.py:297`), and `load_master` coerces CSV cells with
  `float(row[field])` + `except ValueError → None` (`main.py:90-97`). Verified: a cell of
  `"4.2"` reaches `lead_score` as `4.2`. So the specific trigger proposed in
  `066-typing-at-scale.md:120` ("a master row whose `rating` cell was edited to
  `'4.5 stars'` by a human") **does not reach this line** — that cell becomes `None`.
  The exposure is entirely in the untyped interior: `google_places.py:297` is
  `place.get("rating")` on a decoded JSON body with zero validation, and the pipeline is
  otherwise defensively hardened everywhere else (`enricher.py:176,190,242-248` wrap every
  other failure). One field slipped through.
- **Note:** `lead_score` is public API — `cli.py:250` imports it by name for the
  `leadminer score` command. Nothing advertises that the caller must pre-coerce `rating`.
- **Fix:** coerce and range-check once, at the boundary of the function, so the guarantee
  does not depend on a cast 700 lines upstream:
  ```python
  rating = record.get("rating")
  try:
      rating = float(rating)  # type: ignore[arg-type]
  except (TypeError, ValueError):
      rating = None
  if rating is not None and math.isfinite(rating) and 0.0 <= rating < 4.0:
      score += 10
  ```
  This closes S1 **and** S2 below in one edit, and matches `dedup._validity`
  (`dedup.py:134-139`) and `cli.cmd_validate` (`cli.py:180-186`), which both already
  define the valid domain as `0 ≤ rating ≤ 5`.

### S2 — Out-of-range and non-finite ratings are rewarded; `nan` silently deletes the signal and poisons the export

- **Where:** `enricher.py:313-314`
- **Breaks:** the guard is only `is not None`. There is no lower bound, no upper bound and
  no finiteness check — while the rest of the repo treats `0 ≤ rating ≤ 5` as the contract
  (`dedup.py:139` returns validity 0 outside it; `cli.py:185-188` raises
  `ERR`-class "rating outside 0-5" and exits 1). `lead_score` is the **only** consumer that
  treats an invalid rating as a *positive* signal.
- **Trigger (verified):** `lead_score({"rating": 0, ...})` → `25`; `rating=-3.0` → `25`;
  `rating=float("-inf")` → `25`. Each awards the +10 "Low rating = pain point" bonus
  (`enricher.py:311`). `0` is reachable today: `main.py:93` casts with **no range check**,
  so a CSV cell of `"0"` becomes `0.0` and earns the bonus.
- **The `nan` chain (verified):** `load_master` accepts `"nan"`, `"NaN"`, `"inf"`, `"-inf"`
  and `"1e1"` — `float()` parses all five, so the `except` at `main.py:94` never fires.
  Consequences:
  1. `nan < 4.0` is `False` → the record silently loses a bonus it *should* not have
     earned anyway, and nothing is logged.
  2. `write_csv` (`main.py:108-133`) serialises it as the literal `nan`, which round-trips
     stably into every future run — one poisoned cell is permanent.
  3. `cli.cmd_validate`'s `if not 0 <= v <= 5` (`cli.py:185`) is `True` for `nan`, so
     `leadminer validate` counts it as `bad_rating` and **exits 1 forever** on data the
     pipeline itself wrote.
  4. In Excel/Sheets the cell renders `#N/A`, not a number.
- **Trigger for `nan`:** `google_places.py:254` uses bare `resp.json()`, and Python's
  `json` module accepts the non-standard `NaN` / `Infinity` literals by default (verified:
  `json.loads('{"rating": NaN}')` → `{'rating': nan}`). One malformed, proxied or cached
  response is enough.
- **Fix:** the `math.isfinite` + range check in the S1 patch. Add a hard fail in
  `cli.cmd_validate` for non-finite cells (`cli.py:180-186`) so the gate matches what
  `load_master` will actually admit.

### S2 — `min(score, 100)` collapses the top band, and it binds on exactly the leads the product exists to find

- **Where:** `enricher.py:319`
- **Corrects an existing audit:** `009-lead-score-model.md` grades this clamp S3 and
  asserts it "effectively never binds". That is wrong, and the reason is the dead-site
  weight: `enricher.py:304` grants +20 where a live site gets +10, so the raw ceiling is
  **110**, not 100. The clamp binds for any record with all four contact fields, a dead
  site, `industry_priority == "high"`, and either a sub-4 rating or multi-source
  provenance — i.e. the archetypal best lead.
- **Trigger (verified):** enumerating all 256 on/off combinations of the eight signals,
  **8 distinct combinations export the identical value `100`**:
  ```
  100 -> email+whatsapp+phone+high+rating+multi          <- no website, no instagram
  100 -> email+whatsapp+phone+instagram+high+multi
  100 -> email+whatsapp+phone+instagram+dead+high+rating
  100 -> email+whatsapp+phone+instagram+dead+high+multi
  ... (4 more)
  ```
  The first one is a lead with **no website and no Instagram** — the project's flagship
  new-site pitch target (`BRIEF.md:54`) — tying with a dead-site lead that has both. The
  score cannot express the difference; the ordering a rep would sort on is destroyed in
  precisely the band that matters.
- **Silently wrong, not cosmetic:** `100` is written to `all_businesses.csv` as an
  unqualified int (`main.py:37`), indistinguishable from a genuinely-won 100.
- **Mitigating, and worth stating:** nothing in the repo *filters* on `lead_score`.
  `sales_ready` gates on `has_any_contact()` + `industry_priority` (`main.py:202-205`, per
  `086-SYNTH-data-contract.md:117`), and `qualified_businesses.csv` gates on
  `completeness_score >= 1` (`main.py:206`). So no CSV membership is wrong today. The
  damage is confined to the column a rep sorts by in a spreadsheet — which is why this is
  S2 and not S1.
- **Fix:** either raise the weights so 100 is the true ceiling, or stop pretending the
  output is bounded: drop `min(score, 100)` and document the real 0–110 range, or normalise
  by dividing by 110. Do not keep a clamp that fires on the top decile.

### S2 — `\D` digit counting lets any 7 digits buy the phone bonus *and* `sales_ready` membership

- **Where:** `enricher.py:285` (`re.sub(r"\D", "", ...)`) and `enricher.py:292-293`
  (`if len(phone) >= 7`)
- **Breaks:** the phone signal is a **character count, not a validation**. It is the only
  scoring input with no shape check, and `main.has_any_contact` (`main.py:138-145`) uses
  the *byte-identical* regex with the *identical* `>= 7` threshold, so one bad cell
  simultaneously inflates `lead_score` and puts the lead into `sales_ready.csv`
  (`main.py:204`). Verified, same base record:
  | `phone` | `lead_score` | `has_any_contact` |
  |---|---|---|
  | `"https://x.com/9617012345"` | `30` (+15) | `True` |
  | `"1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"` | `30` (+15) | `True` |
  | `"٠٧٠١٢٣٤٥٦"` | `30` (+15) | `True` |
  | `"0000000"` | `30` (+15) | `True` |
  | `"ext 4"` | `15` | `False` |
  `dedup.normalize_phone` already has the right predicate and it is stricter
  (`_MIN_DIGITS = 7` at `dedup.py:30`, returns `""` for junk — `dedup.py:45-46`, with
  regression tests at `tests/test_lead_signal.py:103-108`). `lead_score` re-implemented the
  same rule worse and inline.
- **Reachability:** the three scrapers give real phone strings, so this is not live from
  ingestion. It is live the moment a human touches the CSV — which is the documented
  workflow (rclone to Google Drive, `BRIEF.md:37`) and the explicit purpose of
  `leadminer score` (`cli.py:12`). A URL pasted into the phone column is the realistic
  accident, and it produces a "sales-ready" lead that a rep will dial.
- **Also silently wrong:** non-`str` types are stringified — `phone=12345678.0` yields
  `"12345678.0"`, whose `.0` becomes a 9th digit.
- **Fix:** reuse `dedup.normalize_phone` instead of re-implementing:
  ```python
  from dedup import normalize_phone
  if normalize_phone(str(record.get("phone") or ""), record.get("country") or "LB"):
      score += 15
  ```
  and make `main.has_any_contact` (`main.py:138-145`) call the same thing, so `lead_score`
  and `sales_ready` cannot disagree.

### S2 — `website_live` identity checks silently shed 10–25 points, and `leadminer score` writes the loss back over the master

- **Where:** `enricher.py:301` (`live is True`), `enricher.py:303` (`live is False`),
  interacting with `main.py:103` and `cli.py:269-276`
- **Breaks:** `lead_score` demands the *identical* `True`/`False` objects. `load_master`
  only produces them from the exact strings `"True"`/`"False"`
  (`row["website_live"] = True if wl == "True" else (False if wl == "False" else None)`);
  every other spelling — `"TRUE"`, `"true"`, `"1"`, `"yes"`, `"Y"`, `"T"` — silently
  becomes `None`. Verified: a record with `website_live="TRUE"` scores `40` where the same
  record with `"False"` scores `60`. Every non-canonical spelling of a dead site therefore
  reads as "unreachable", forfeiting the +20 rebuild bonus (`enricher.py:304`) — the
  distinction `043-scoring-upgrade.md` was written to eliminate.
- **The destructive part:** `cli.cmd_score` defaults `--out` to the **input path**
  (`cli.py:274`, `cli.py:305-307`) and calls `pipeline.write_csv(out, records)`
  (`cli.py:275`). End-to-end verification on a two-row CSV, both rows with a genuine
  server-confirmed-dead site stored at `lead_score=65`:
  ```
  Dead Cafe    website_live="False" -> recomputed 60 -> written 60
  Dead Cafe2   website_live="TRUE"  -> recomputed 40 -> written 40
  ```
  The 40 is now the stored truth, and the dead-site signal for that row is gone from the
  master. The CLI even reports it as routine progress: `re-scored 2 records, 2 changed`.
  Recovery is impossible because `website_live=False` is no longer recoverable from the
  CSV — only a re-fetch would restore it.
- **Reachability:** our own `write_csv` output round-trips safely (verified —
  `str(True) == "True"`). This fires on any CSV not written by this pipeline, i.e. exactly
  the ones `leadminer score` exists to process.
- **Fix:** coerce inside `lead_score` so it accepts the same vocabulary the CSV layer does,
  and make `cmd_score` refuse to write in place unless the file's `website_live` column
  already parses to a tri-state:
  ```python
  live = record.get("website_live")
  if isinstance(live, str):
      live = {"true": True, "false": False}.get(live.strip().lower())
  ```
  (`enricher.py:250` already has a `live_count` for LIVE; there is no reason for the
  scoring path to be stricter than the fetch path.)

### S3 — the "pain point" bonus ignores `review_count`, so a 1-review rating is treated as evidence

- **Where:** `enricher.py:311-314`
- **Comment says:** `# Low rating = pain point worth pitching`
- **Breaks:** `review_count` is collected (`scrapers/base.py:22`,
  `google_places.py:298`) and written to every CSV (`main.py:36`) but is **never read by
  `lead_score`** — verified by source inspection: `"review_count" in inspect.getsource(lead_score)` → `False`.
  So the strength of the evidence is ignored entirely.
- **Trigger (verified):**
  ```python
  lead_score({"rating": 3.9, "review_count": 1,    "industry_priority": "high", "source": "osm"})  # 25
  lead_score({"rating": 3.9, "review_count": 5000, "industry_priority": "high", "source": "osm"})  # 25
  ```
  One 5-star review from a customer's cousin is indistinguishable from 5000 reviews. A lead
  that is actually well-reviewed (4.6★) loses 10 points to one that is not, on evidence
  that carries no statistical weight.
- **Fix:** gate the bonus on a minimum sample, e.g.
  `if rating is not None and 0.0 <= rating < 4.0 and (record.get("review_count") or 0) >= 5:`,
  or halve it below the threshold. Note `review_count` is Google-only (`osm.py:115`,
  `wikidata.py:123` are `None`), so `review_count < 5` will read as "insufficient evidence"
  for every OSM/Wikidata record — that is the correct outcome, but it changes the score
  distribution, so re-score and diff before shipping.

### S3 — no argument validation: `None`/`list` records and `bool` ratings

- **Where:** `enricher.py:283`
- **Breaks:** `lead_score(None)`, `lead_score([])`, `lead_score("record")` and
  `lead_score(42)` all raise `AttributeError: '<type>' object has no attribute 'get'`
  (verified). `rating=True` returns `25` without complaint — `True < 4.0` is legal Python
  and `bool` is a subclass of `int`.
- **Why it matters despite the contract:** `cli.py:250` imports `lead_score` as a public
  entry point for a command whose input is an operator-supplied file, and the
  `record: dict` annotation is not checked at runtime (`066-typing-at-scale.md` S4/S5).
  A one-line guard at the top turns three exception types into one clear error message.
- **Fix:** `if not isinstance(record, dict): raise TypeError(f"lead_score expects a record dict, got {type(record).__name__}")`
  — or, for a batch function, skip and count the row instead of raising.

---

## The single most dangerous input

```python
lead_score({"rating": "4.2"})   # or "3.9", "0", "٣٫٩", "four stars", []
```

**A `rating` that is not a real number.** Three reasons, in order:

1. **It is the only input in the function with no branch at all.** `phone`, `source`,
   `website_live` and `industry_priority` are all guarded by truthiness or `str()`
   coercion; `rating` is handed straight to `<` against a `float` literal at
   `enricher.py:313`. `pitch_recommender.py` wraps the same field twice; `enricher.py`
   wraps nothing.
2. **The failure costs the entire run, at the most expensive possible moment.** The call
   site is `enricher.py:350`, after `check_websites()` at `enricher.py:337` has already
   spent the full 40-thread, one-GET-per-website budget over the whole corpus. There is no
   `try` at `enricher.py:350`, none in `enrich()`, none at `main.py:187`, and
   `cli.main` catches only `KeyboardInterrupt` (`cli.py:314-317`). Result: traceback,
   `SystemExit`, and **zero of the five CSVs** — versus the ~300-minute budget the workflow
   allots (`BRIEF.md:42`).
3. **It is one upstream slip from live.** It is unreachable today purely because
   `main.py:93` happens to cast `rating` on the CSV path — 700 lines and one process
   boundary away, protecting a comparison it knows nothing about. `google_places.py:297`
   takes `rating` straight from a decoded JSON body. The function's real problem is not
   that a string can arrive; it is that `lead_score` has no defence of its own and depends
   on a distant cast that a future refactor is free to move.

The runners-up, for calibration: a `phone` cell holding a pasted URL (S2) is the most
dangerous **currently-reachable** input, because it is silent, it scores, *and* it places
the lead in `sales_ready.csv` — a rep dials a URL. But it damages one row. The string
rating takes everything.

---

## Not a bug, but worth knowing

- **No ReDoS.** The only regex is `re.sub(r"\D", "", ...)` (`enricher.py:285`) — a single
  character class, no alternation, no nesting. `"1"*20000` and `"1"*1000000` are linear.
  Contrast `_EMAIL_RE`/`_INSTAGRAM_RE` (`enricher.py:130-131`) elsewhere in the file, which
  are the ones that would need a look.
- **`\D` and Unicode agree with dedup.** Arabic-Indic digits survive `\D` because `\d`
  matches Unicode `Nd`; `dedup.normalize_phone` (`dedup.py:44`) uses the same pattern, so
  `lead_score` and the dedup key never disagree about what a digit is. This is
  accidental consistency, not a decision — if either side changes, both must change.
- **Locale-formatted ratings are silently dropped, never mis-scored.** `float("4,2")`
  (French decimal comma) and `float("٣٫٩")` (Arabic decimal separator `U+066B`) both raise
  `ValueError`, which `main.py:94` converts to `None` — verified. A CSV round-tripped
  through a French-locale spreadsheet therefore loses every sub-4 rating and its +10 bonus,
  silently and permanently. `float("٣.٩")` (Arabic-Indic digits, ASCII dot) **does** parse,
  so the behaviour is inconsistent across spreadsheets. Not `lead_score`'s bug — it is the
  rating edge, and no log line reports it.
- **The `website_live` tri-state itself is handled correctly.** `live is True` → +10;
  `live is False` **and** a website present → +20; `live is None` → +0; `False` with no
  `website` → +0. Verified all four. The inversion bug of `043-scoring-upgrade.md` has not
  regressed; `tests/test_lead_signal.py:76-99` still guards it.
- **The double computation of `lead_score`** (`enricher.py:350` then `main.py:197`) is
  already tracked as `046-dead-code-simplification.md` S3. Not re-litigated here, but note
  the interaction with S2: the `enricher.py:350` value is always missing the
  `industry_priority` bonus, so anyone calling `enrich()` directly sees scores up to 15
  points low — and if that call raises per S1, they get nothing at all.
- **Absolute worst-case inputs are safe.** `"1"*1000000`, a 1 MB emoji string, a deeply
  nested list in `phone` — all return an int in linear time with no exception. The function
  is not fragile to size, only to type.

---

## Recommended order of work

1. **S1** — coerce `rating` once with `float()` + `try/except` + `math.isfinite` + `0 ≤ r ≤ 5`
   at `enricher.py:312-313`. One edit kills the only crash in the function, closes S2
   (out-of-range rewarded), and aligns with `dedup.py:139` / `cli.py:185`. Add the four
   regression cases from `tests/test_lead_signal.py` style: `"4.2"`, `0`, `nan`, `-inf`.
2. **S2 (phone)** — replace the inline `\D` + `len >= 7` at `enricher.py:285,292` with a
   call to `dedup.normalize_phone`, and repoint `main.has_any_contact` (`main.py:138-145`)
   at it. Removes the duplicated predicate and the `sales_ready` divergence in one change.
3. **S2 (`website_live`)** — accept the `"true"`/`"1"`/`"True"` vocabulary at
   `enricher.py:301,303`, and make `cli.cmd_score` (`cli.py:274-275`) refuse to
   overwrite the input by default. Until the second half ships, treat any
   `leadminer score` run against a foreign CSV as destructive.
4. **S2 (cap)** — decide the real range and stop clamping to 100. If the score must stay
   0–100, rescale; the top band currently cannot rank the leads the product is built for.
   Re-score the existing master and diff the distribution before and after.
5. **S3 (`review_count`)** — add the minimum-sample gate. This changes scores for every
   OSM/Wikidata record (`review_count` is `None` there), so it is the change most likely
   to surprise; do it on a branch with a score histogram to compare.
6. **S3 (argument validation)** — add the `isinstance(record, dict)` guard while you are in
   the function.