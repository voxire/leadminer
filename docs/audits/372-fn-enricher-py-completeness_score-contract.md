# 372 — `completeness_score` contract: declared shape vs. actual shape

**Lens:** contract. **Target:** `enricher.py:101`. **Read with:** `scrapers/base.py:5-29`.

## Verdict

The *name-level* contract is clean and the *return* contract holds: the function reads
exactly 8 keys, **all 8 of which are declared in `BusinessRecord`**, it writes nothing, and
it provably returns an `int` in `[0, 7]`. Every defect is in the **value-level** contract:
the function awards a point for any *truthy* value with no emptiness check and no validity
check, so OSM/Wikidata placeholder strings (`"N/A"`, `"none"`, `"https://n/a"`) and
whitespace cells each score like real contact data. Combined with the fact that the score is
computed at `enricher.py:349` **before** `main.py:194` can delete `phone` outright, the
exported `completeness_score` column routinely describes a row that no longer matches it.

`BusinessRecord` constrains nothing at this call site (`record: dict` at `enricher.py:101`
is `dict[Any, Any]`), so mypy `strict = true` (`pyproject.toml:64-68`) cannot catch any of it.

---

## The contract, precisely

### Declared

```python
# enricher.py:101
def completeness_score(record: dict) -> int:
```
```python
# scrapers/base.py:5-29
class BusinessRecord(TypedDict):   # total=True — every key required
    name: str | None
    ...
    completeness_score: int
```

### Actual input at the call site

`enricher.py:349` is the **only** call site (`grep completeness_score` returns exactly one
call). It runs on records that came from `main.py:179-180` (`combined = raw_filtered + master`,
`records = dedup(combined)`), i.e. a mix of three scraper `BusinessRecord`s and `dict`s
straight out of `csv.DictReader` via `load_master` (`main.py:77-105`).

### Every field read, and whether it is in the TypedDict

| Field read | Read at | In `BusinessRecord`? | Declared type | Actual type at `enricher.py:349` |
|---|---|---|---|---|
| `phone` | `enricher.py:103` | yes — `base.py:13` | `str \| None` | `str \| None` — holds |
| `email` | `enricher.py:105` | yes — `base.py:14` | `str \| None` | `str \| None` — holds |
| `website` | `enricher.py:107` | yes — `base.py:15` | `str \| None` | `str \| None` — holds |
| `address` | `enricher.py:109` | yes — `base.py:8` | `str \| None` | `str \| None` — holds |
| `facebook` | `enricher.py:111` | yes — `base.py:17` | `str \| None` | `str \| None` — holds |
| `instagram` | `enricher.py:111` | yes — `base.py:18` | `str \| None` | `str \| None` — holds |
| `whatsapp` | `enricher.py:113` | yes — `base.py:19` | `str \| None` | `str \| None` — holds |
| `linkedin` | `enricher.py:115` | yes — `base.py:20` | `str \| None` | `str \| None` — holds |

**Answer to the assigned question: there are ZERO fields read that are not in the TypedDict,
and ZERO fields written — the function is pure.** The single write is at the call site,
`enricher.py:349` → `completeness_score`, which *is* declared (`base.py:28`, `int`), and the
function returns `int`. So on the narrow "does it read/write undeclared keys" question, the
function is clean. That is a real result, not a hedge — the failures below are all about
*values* and *ordering*, not key names.

### Every consumer of the return value

| Consumer | Shape assumption | Breaks on unexpected shape? |
|---|---|---|
| `enricher.py:349` `r["completeness_score"] = completeness_score(r)` | `int` | No. Return is provably `int ∈ [0,7]`. |
| `main.py:206` `r.get("completeness_score", 0) >= 1` | `int`, **not `None`** | **Yes — `TypeError`, uncaught.** See S2-2. |
| `pitch_recommender.py:48-52` | any / `None` / `str` | No — guarded by `try/except int()`. But the value is **never used** (S2-3). |
| `main.py:44` + `main.py:96-101` `_INT_FIELDS` | coercible to float | Lenient; `int(float("3.7"))` → 3, `int(float("1e2"))` → 100. |
| `main.py:37` / `write_csv` (`main.py:125`) | any | No. |
| `cli.py:247-277` `cmd_score` | — | **Never recomputes it.** See S2-2. |
| `cli.py:135-201` `cmd_validate` | — | **Never validates the range.** See S3-3. |

---

## Findings

### S1 — The score is computed before `normalize_phone` deletes the phone, so the exported number describes a field that is blank in the same row

- **Where:** `enricher.py:349` (compute) → `main.py:194` (mutate) → `main.py:206` (filter) →
  `main.py:209` (export). Nothing recomputes in between.
- **Breaks:** `enrich()` scores the raw, un-normalized `phone` and freezes the result into
  `r["completeness_score"]`. Eleven lines later `main.py:194` runs
  `r["phone"] = normalize_phone(raw_phone, ...)`, and `normalize_phone` returns `""` for
  anything under 7 digits or falsy (`dedup.py:40-46`). The row is then exported with
  `phone=""` and `completeness_score=1` — a score of 1 claiming a signal the row does not
  contain. `main.py:206` (`>= 1`) then puts that row in `qualified_businesses.csv`, whose
  documented definition is *"Subset with at least one contact signal"*
  (`README.md:46`). The printed count at `main.py:221` inherits the same inflation.
- **Trigger** (verified by execution):
  ```python
  record = {"phone": "12345", "address": None, "email": None, "website": None,
            "facebook": None, "instagram": "", "whatsapp": None, "linkedin": ""}
  completeness_score(record)   # -> 1
  normalize_phone("12345", "LB")   # -> ''   (dedup.py:45, len(digits)=5 < _MIN_DIGITS=7)
  ```
  Exported row: `phone="" , completeness_score=1` → lands in `qualified_businesses.csv`.
  `"N/A"` works identically: `normalize_phone("N/A", "LB") -> ''`, score 1.
  A row whose *only* completeness signal is a junk phone scores 1 and is sold as qualified.
- **Fix:** one line — in `main.py`, move the assignment to after the normalization loop
  (`main.py:189-197`), or recompute `r["completeness_score"] = completeness_score(r)` on
  line 198 alongside the existing `lead_score` recomputation that already exists for the
  identical ordering reason.

### S1 — Any truthy value earns a point: no emptiness check, no validity check, so placeholder junk scores like real data

- **Where:** `enricher.py:103-116`. Every test is a bare `if record.get(field):`.
- **Breaks:** the codebase already has two stricter predicates and this function uses neither:
  `dedup._is_missing` (`dedup.py:79-90`) strips whitespace and treats `""`/`None` as missing,
  and `dedup._validity` (`dedup.py:120-140`) runs `_EMAIL_OK` / `_URL_OK`
  (`dedup.py:116-117`) against email and website. `load_master` only nulls the exact empty
  string: `if row[k] == ""` (`main.py:88-89`). So `" "`, `"N/A"`, `"none"`, `"-"` all
  survive the round-trip and all score a point here. Worse, `osm.py:95-96`
  (`if website and not website.startswith("http"): website = "https://" + website`) turns an
  OSM `website=n/a` tag into the syntactically plausible `"https://n/a"`, which then gets
  fetched by `check_websites` (`enricher.py:231`) — fails DNS, becomes `UNKNOWN`
  (`enricher.py:250`) — and still counts as a website for scoring.
- **Trigger** (verified by execution):
  ```python
  record = {"phone": "N/A", "email": " ", "website": "https://n/a",
            "address": "  ", "facebook": None, "instagram": "",
            "whatsapp": None, "linkedin": ""}
  completeness_score(record)        # -> 4
  ```
  All four awarded points are junk. `has_any_contact` (`main.py:137-145`) correctly returns
  `False` for this record — 0 dialable digits, no `"@"` in the email, no Instagram handle —
  so it is excluded from `sales_ready.csv`. But it is **included** in
  `qualified_businesses.csv` with `completeness_score=4`. A rep sorting that file
  descending by score puts this record near the top.
- **Fix:** one shared predicate — `has_value(v)` = `v is not None and str(v).strip() != ""`,
  with `_EMAIL_OK` / `_URL_OK` applied to `email` / `website`; import it from `dedup` rather
  than re-implementing the looser version a third time.

### S2 — `record: dict` binds to nothing: `BusinessRecord` is never enforced on the input, and `strict = true` cannot catch it

- **Where:** `enricher.py:101` (`record: dict`) and `enricher.py:326` (`records: list[dict]`).
- **Breaks:** the `BusinessRecord` annotation on `BaseScraper.scrape` (`base.py:33`) is erased
  at the first boundary crossing — `main.py:158` does `list(scraper.scrape())` into `raw:
  list[dict]` (`main.py:154`) and `main.py:187` passes that to `enrich()`. From there
  everything is `dict[Any, Any]`, so `record.get("phone")` is `Any` and no check applies. A
  `dict[str, str]` taken straight from `csv.DictReader` without `load_master` type-checks
  cleanly and returns an `int` — nothing happens to break *today* only because all 8 fields
  happen to be strings-or-absent in every source.
- The declaration is also satisfied by a lie on the way in: `BusinessRecord` is `total=True`,
  so it asserts `completeness_score: int` and `lead_score: int` are *present and computed* on
  a freshly-scraped record, when all three scrapers emit placeholder `0`s —
  `osm.py:121`, `wikidata.py:129`, `google_places.py:304`. The type is honoured; the meaning
  is not.
- Finally, `BusinessRecord` describes neither the input nor the output dict:
  `dedup._merge` iterates `set(a) | set(b)` (`dedup.py:192`), so the merged key set is the
  union of whatever the scraper and the master CSV happened to contain, not `BusinessRecord`'s
  23 keys. An extra column in a master CSV survives into the record reaching line 349.
  `write_csv`'s `extrasaction="ignore"` (`main.py:125`) hides the divergence at the output.
- **Fix:** `def completeness_score(record: BusinessRecord) -> int`, and split the model into
  `ScrapedRecord(TypedDict, total=False)` (no `lead_score` / `completeness_score` /
  `industry_priority` / `recommended_service`) and `EnrichedRecord` where those become
  required. Do it in the same pass that adds a type gate to CI, or it will not hold.

### S2 — `main.py:206` is the only consumer that compares, and it is the only one that is not `None`-safe; `leadminer score` never refreshes the field

- **Where:** `main.py:206`; `cli.py:247-277`.
- **Breaks (a) — latent `TypeError` that loses a whole run.**
  `r.get("completeness_score", 0) >= 1` guards a *missing key* but not a `None` value.
  Verified:
  ```python
  {"completeness_score": None}.get("completeness_score", 0) >= 1
  # TypeError: '>=' not supported between instances of 'NoneType' and 'int'
  ```
  This raises uncaught at `main.py:206`, which runs **before** the first `write_csv` at
  `main.py:209` — so the entire run's five exports are lost, not just one file.
  Today it is *latent*: `enricher.py:349` unconditionally assigns an `int` for every record
  at `main.py:187`. It becomes live the moment uncomputed fields adopt a `None` sentinel —
  which is exactly the migration the ingestion-layer audits recommend, and exactly what
  `BusinessRecord(total=True)` is currently lying about preventing. Note the asymmetry:
  `pitch_recommender.py:49-52` wraps the same read in `try/except int()`; `main.py:206`
  does not.
- **Breaks (b) — `leadminer score` silently refreshes 2 of 3 score columns.**
  `cmd_score` (`cli.py:247-277`) recomputes `industry_priority` (267), `recommended_service`
  (268) and `lead_score` (269) and then writes the file back (275). It never calls
  `completeness_score`. So `leadminer score --file x.csv`, whose stated purpose is
  "re-score existing records with current rules" (`cli.py:12`, `cli.py:248`), emits a file
  whose `completeness_score` column is whatever the *previous* scoring model produced. Today
  that is invisible because the values happen to agree; the moment the completeness rules
  change, `leadminer score` is the operator's tool for propagating the change and it will
  report "0 changed" (`cli.py:270-276`) while leaving the column untouched.
- **Fix:** `(r.get("completeness_score") or 0) >= 1` at `main.py:206`, and add
  `r["completeness_score"] = completeness_score(r)` to the `cmd_score` loop at `cli.py:269`.

### S2 — `recommend_service` reads `completeness_score` into a local it never uses, so the only in-program consumer of the 0–7 range ignores it

- **Where:** `pitch_recommender.py:48-52`.
- **Breaks:** the value is fetched, guarded, and then dead. AST proof — every `Load` node for
  the local `completeness` in `recommend_service` sits on line 50, inside the assignment
  itself:
  ```
  line  48  ctx=Store
  line  50  ctx=Store
  line  50  ctx=Load
  line  50  ctx=Load
  line  52  ctx=Store
  ```
  Zero loads in any `if`, comparison or `return`. The comment at
  `pitch_recommender.py:68-69` documents "weak completeness" as part of the Tier-3 pitch, but
  the predicate at `pitch_recommender.py:70` is `has_website and website_live and
  review_count < 20` — completeness is not consulted.
  The only *comparative* use in the entire program is the `>= 1` threshold at `main.py:206`.
  So the 0–7 scale collapses to one bit: a business with phone + address + website (3) and a
  business with a single phone number (1) are equally "qualified". `qualified_businesses.csv`
  is a primary qualification artifact and the scale in it currently does no work.
- **Fix:** either delete `pitch_recommender.py:48-52` and correct the comment at
  `pitch_recommender.py:68-69` so it stops promising a condition the code does not implement,
  or put `completeness` into the Tier-3 predicate and raise the `main.py:206` threshold so
  the scale actually selects.

### S3 — `facebook` and `instagram` are worth 1 point *together*

- **Where:** `enricher.py:111` — `if record.get("facebook") or record.get("instagram"): score += 1`.
- Verified by execution: facebook-only → 1, instagram-only → 1, **both → 1**. A business on
  both platforms scores exactly as much as one on a single platform. Note also that nothing
  in the pipeline can populate `facebook`: the contacts dict at `enricher.py:168` has no
  `facebook` key, `_fetch_website` never extracts one, `google_places.py:293` and
  `wikidata.py:118` hardcode `facebook=None`. Only the OSM `contact:facebook` tag
  (`osm.py:92`) can ever populate it.
- **Fix:** score them separately (2 points for both), or delete `facebook` from the function
  and stop pretending it is a signal the enricher can find.

### S3 — `completeness_score` never reads `website_live`, so the highest-priority lead is scored as a fully complete one

- **Where:** `enricher.py:107` awards +1 for `website` whether `website_live` is `True`,
  `False`, or `None`.
- **Breaks:** a server-confirmed-dead site is the top-priority pitch in the database —
  `pitch_recommender.py:55-58` returns `"Website rebuild + maintenance"` for it and
  `enricher.py:303-304` awards it +20 in `lead_score`, twice the +10 for a live site. Yet
  `completeness_score` scores a 404 homepage identically to a healthy one, so the exported
  number gives a rep no hint which row is a rebuild. This is a scoring-model point rather
  than a crash; listed here because a column named `completeness` that ignores the
  neighbouring tri-state column is a declared-vs-actual mismatch a consumer will misread.
- **Fix:** award `website` only when `website_live is True`, or document in
  `README.md:64` that the score is source-population only and is blind to liveness.

### S3 — `leadminer validate` does not range-check the field, and the README mislabels one of the eight

- **Where:** `cli.py:135-201`, `README.md:64`.
- **Breaks:** `cmd_validate` checks name, BOM, coordinates, rating range and duplicate
  `(name, phone)` pairs — it never checks `completeness_score ∈ [0,7]` or
  `lead_score ∈ [0,100]`. A hand-edited or truncated master containing
  `completeness_score=99` passes the gate with `return 0` (`cli.py:200`) and is written back
  out unchanged by `leadminer score` (`cli.py:275`). The `int(float(x))` coercion at
  `main.py:96-101` makes this worse: `"3.7"` silently becomes `3` and `"1e2"` becomes `100`.
  Separately, `README.md:64` says *"0–7 count of filled contact fields"* — the range is
  correct (verified: 7 on a fully populated record), but `address` (`enricher.py:109`) is a
  location field, not a contact field.
- **Fix:** two range checks in `cmd_validate` (`completeness_score`, `lead_score`), and
  change the README wording to "count of filled contact/identity fields".

---

## Not a bug, but worth knowing

- **The function cannot raise `KeyError`.** All 8 reads go through `.get()`, and
  `enricher.py:340-348` `setdefault`s 4 of the 8 (`facebook`, `instagram`, `whatsapp`,
  `linkedin`) before line 349 — but the four it reads most heavily (`phone`, `email`,
  `website`, `address`) are **not** in that setdefault block. Safety comes entirely from the
  `.get()` calls, not from the setup. If this function is ever rewritten to use `[]`, the
  four non-defaulted keys are where it breaks first.
- **The return type is the one part of the contract that genuinely holds.** `int`, provably in
  `[0, 7]`, with no `None`, `float`, `bool` or exception path (verified: `0` on `{}`, `7` on
  a fully populated record). `lead_score` (`enricher.py:283`) shares this property; the
  asymmetry with `main.py:206`'s missing `None` guard is what makes the latter worth fixing.
- **Zero test coverage, and the one fixture in the repo already encodes a wrong value.**
  `tests/test_lead_signal.py:66` imports `lead_score` and `infer_region` but **not**
  `completeness_score`. The field's only occurrence in the test suite is
  `tests/test_lead_signal.py:202` — the string `"2"` in the CLI fixture at
  `tests/test_lead_signal.py:196-205`. That row has `phone="70123456"`,
  `website="https://x.com"`, `address="1 St, Beirut"` and blanks everywhere else, which
  computes to **3** under the current rules. Nothing checks it, because
  `cmd_stats`/`cmd_validate` never read the column. The canonical fixture is therefore an
  unverified, already-wrong example of the field.
- **`pitch_recommender.py:49-52` is the defensive reader, `main.py:206` is not.** Two
  consumers of the same field, two different levels of `None`/`str` tolerance. That asymmetry
  is the actual shape of the contract today: undeclared and inconsistent, enforced by
  whichever line happens to run first.

---

## Recommended order of work

1. **S1, first, one line each.** Recompute `completeness_score` in `main.py` alongside the
   `lead_score` recomputation that already exists at `main.py:197` — the ordering bug is
   identical and the fix is already half-written. Then add the strip/validity predicate from
   `dedup._is_missing` + `dedup._validity` to `enricher.py:103-116`. These two changes
   together are what makes the exported number mean anything.
2. **S2, the two `None` guards.** `main.py:206` → `(r.get("completeness_score") or 0) >= 1`,
   and add the missing `completeness_score` recompute to `cmd_score` at `cli.py:269`. Both
   are small and both must land *before* any migration that introduces `None` sentinels,
   or that migration will kill a run with an uncaught `TypeError`.
3. **S2, decide the semantics of the scale.** Resolve `pitch_recommender.py:48-52`: either
   wire `completeness` into the Tier-3 predicate or delete it and fix the comment. Then set
   `main.py:206`'s threshold to something above `1`, or accept that the 0–7 range is
   decorative and say so in `README.md:64`.
4. **S2, then the type model.** Annotate `enricher.py:101` with `BusinessRecord`, split
   `total=False` ingestion records from required enriched records, and add a CI type gate —
   in that order, since an annotation without a gate will rot.
5. **S3s, opportunistically.** Split the `facebook`/`instagram` point, decide whether
   `website_live` should influence the score, add the two range checks to `cmd_validate`,
   and add the first `completeness_score` unit tests (empty dict → 0, fully populated → 7,
   junk placeholders → 0, `None` field values → 0) — including fixing the wrong `"2"` at
   `tests/test_lead_signal.py:202`.