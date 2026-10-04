# 200 — `cmd_score` (`cli.py:247`): the 8 defects it still needs tests for, and the 19 tests that pin them

## Verdict

`cmd_score` is a **destructive, in-place, schema-narrowing** command with **zero tests**, and it
has never been exercised end to end — `docs/audits/087-SYNTH-test-gap.md:19` lists it by name as
untested. Two things are wrong and they compound: the command **silently deletes every CSV column
not in `main.FIELDS`** (`cli.py:275` → `main.py:125`), and it **overwrites the cumulative master
by default** (`cli.py:274`, `cli.py:306`). `docs/audits/070-schema-migration.md:20` already
prescribes "an explicit, repeatable backfill" that preserves unknown columns — but the tool that
backfill would actually be executed with is the thing that drops them. Run it on the Drive-hosted
master today and any `scoring_version` column that a future fix adds is gone on first write.

The single most valuable test is a golden differential: **pipe `main()`'s own output through
`cmd_score` and assert zero diff.** I verified that harness works fully offline and it currently
passes. Everything below is built around it.

---

## The function under test

```
cmd_score                                   cli.py:247-277
├── path.exists() / error exit 2            cli.py:255-257
├── pipeline.load_master(path)              cli.py:259  → main.py:77-105
│     "" → None                              main.py:88-89
│     lat/lon/rating → float|None           main.py:90-95
│     review_count/completeness/lead → int|None  main.py:96-101
│     website_live: "True"→True "False"→False else→None  main.py:103
├── `if not records: return 1`              cli.py:260-262
├── per record:                             cli.py:265-272
│     r["country"] = resolve_country(r)     cli.py:266 → main.py:50-74
│     r["industry_priority"] = industry_priority(cat)  cli.py:267 → whitelist.py:133-145
│     r["recommended_service"] = recommend_service(r)  cli.py:268 → pitch_recommender.py:25-99
│     changed += (lead_score(r) != old)     cli.py:269-271 → enricher.py:283-319
├── out = args.out or path (IN PLACE)       cli.py:274
├── pipeline.write_csv(out, records)        cli.py:275 → main.py:108-134
│     fieldnames=FIELDS, extrasaction="ignore"          main.py:125
└── return 0                                cli.py:277
```

**What `main.py` does that `cmd_score` does not** (`main.py:183-197` vs `cli.py:265-272`):

| Step | `main.py` | `cmd_score` | Divergence |
|---|---|---|---|
| country | `:184` | `cli.py:266` | same |
| region (blank → `infer_region`) | `:332-335` | **absent** | F4 |
| website liveness | `:187` (`enrich`) | **absent by design** | not a bug |
| `completeness_score` | `:349` | **absent** | F3 |
| `industry_priority` | `:190` | `cli.py:267` | same |
| `recommended_service` | `:191` | `cli.py:268` | same |
| `normalize_phone` | `:194` | **absent** | F5 |
| `lead_score` | `:197` | `cli.py:269` | same |
| dedup | `:180` | **absent** | by design, but see E8 |

---

## Findings

### F1 · S1 — `leadminer score` silently deletes every column outside `FIELDS`, in place, on the master

- **Where:** `cli.py:275` (`pipeline.write_csv(out, records)`) → `main.py:125`
  (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`); column list `main.py:33-40`.
- **Breaks:** `load_master` preserves every column the CSV has (`main.py:86-104`, no filtering).
  `write_csv` then drops anything not named in `FIELDS`. With `out=None` the target **is** the
  input, so the loss is immediate and there is no second copy: `data/` is gitignored
  (`.gitignore:7`), the workflow's only guard is `if [ ! -s data/all_businesses.csv ]`
  (`.github/workflows/scrape.yml:88-91`) which a locally-run `leadminer score` never passes
  through, and the Drive copy is overwritten on the next CI publish.
- **Trigger (verified):** a 26-column master `name,...,scraped_at,place_id,scoring_version,gmaps_url`
  where `place_id="ChIJxyz"`, `scoring_version="lead-v1"`. After `cmd_score(Namespace(file=p, out=None))`:
  `rc=0`, header drops from 26 columns to 23, and
  `set(before) - set(after) == {"place_id", "scoring_version", "gmaps_url"}`.
- **Why S1 and not S2:** this is silent, irreversible loss of the irreplaceable cumulative master,
  and it is guaranteed to fire on any schema evolution. `docs/audits/070-schema-migration.md:38`
  instructs the team to add `scoring_version` and `scored_at` and backfill them — the backfill tool
  deletes them. The defect is *created by* the prescribed remedy.
- **Fix:** derive the output header as `FIELDS + sorted(extra input columns)` in `write_csv`, or
  have `cmd_score` abort with rc 2 if the header would shrink.

### F2 · S2 — the `N changed` counter watches one field, so the command reports "0 changed" while rewriting pitches

- **Where:** `cli.py:269-272`. Only `lead_score` participates in the comparison.
- **Breaks:** `industry_priority`, `recommended_service` and `country` are all overwritten
  unconditionally (`cli.py:266-268`), and none of them can move the counter.
- **Trigger (verified):** the exact migration this command exists for. Take a row the current rules
  score correctly (`lead_score=30`, `recommended_service="RTYLR commerce OS (POS, online ordering, CRM)"`),
  roll only `recommended_service` back to the pre-`5222d32` value `"Website rebuild + maintenance"`,
  re-run. Output: `re-scored 1 records, 0 changed -> ...`, `rc 0` — and the file on disk now says
  `"RTYLR commerce OS (POS, online ordering, CRM)"`. The file changed; the counter says zero; the
  exit code says success.
- **Why S2:** the operator running a scoring-rule migration gets no machine-readable signal that the
  migration moved anything, and no signal that it moved *more* than the counter reports. `rc` is 0
  whether 0 or 500k rows changed (`cli.py:277`).
- **Fix:** compare a 4-tuple `(country, industry_priority, recommended_service, lead_score)` per row,
  and return a distinct exit code (e.g. 3) when `changed == 0`, or add `--fail-on-change/--fail-if-unchanged`.

### F3 · S2 — `completeness_score` is not recomputed, so one row holds two different scoring eras

- **Where:** `cli.py:265-272` omits `enricher.completeness_score` (`enricher.py:101-117`), which
  `main.py:349` recomputes on every run.
- **Breaks:** after `leadminer score`, `lead_score` reflects current rules while
  `completeness_score` reflects whatever rules last wrote the file. `qualified_businesses.csv` is
  defined by `completeness_score >= 1` (`main.py:206`), so the re-scored master no longer agrees
  with the subset file built from it.
- **Trigger (verified):** `{"name":"Cafe","category":"cafe","country":"LB","phone":"70123456",
  "email":"a@b.com","website":"https://x.com","address":"x","completeness_score":0}` →
  after `cmd_score`: `completeness_score == 0` while `completeness_score(record) == 4`.
  Reported as `1 changed` (only because `lead_score` moved) — the counter hides the stale column.
- **Fix:** add `r["completeness_score"] = completeness_score(r)` to the loop at `cli.py:266`.
- **Note:** F3, F4 and F5 are one root cause: `cmd_score` was written as a transcription of
  `main.py:190-197`, and the three steps that live *outside* that block — `infer_region`
  (`enricher.py:330-335`), `completeness_score` (`main.py:349`) and `normalize_phone`
  (`main.py:194`) — were left behind. All three fixes are one line each in the loop at `cli.py:266`.

### F4 · S2 — `region` is never re-inferred, even though `cmd_score` computes the exact input `infer_region` needs

- **Where:** `cli.py:266` sets `country`, then jumps straight to priority. The only caller of
  `infer_region` for blank regions is `enrich()` at `enricher.py:330-335`.
- **Breaks:** `cmd_score` resolves country on line 266 — the precise `country` argument
  `infer_region` requires at `enricher.py:85` / `:90` — and then never calls it. Legacy master rows
  (which is what a re-score migration is pointed at) are exactly the rows with a blank region.
  `cmd_stats`' "by region" histogram (`cli.py:124-126`) then buckets them all under `Unknown`.
- **Trigger (verified):** `{"name":"Cafe","category":"cafe","country":"LB","region":"",
  "address":"Hamra, Beirut, Lebanon","lat":None,"lon":None}` → after `cmd_score`, `region` is still
  `None`. The same record through `main()` yields `region == "Beirut"` (verified via the harness in P1).
- **Fix:** one line — `if not r.get("region"): r["region"] = infer_region(r.get("address"), r.get("lat"), r.get("lon"), r.get("country"))`.

### F5 · S2 — `phone` is not normalized, so `leadminer score` emits phones `main.py` never produces

- **Where:** `cli.py:265-272` omits `normalize_phone`, which `main.py:194` always applies.
- **Breaks:** `docs/audits/037-cli-design.md:486` explicitly advertises
  `leadminer score -i data/test_enriched.csv -o data/test_scored.csv`. Pointed at anything other
  than a master `main.py` already wrote, this command breaks the E.164 invariant that
  `tests/test_lead_signal.py:110-123` exists to defend. `lead_score` is unaffected (its phone test
  is `len(digits) >= 7`, `enricher.py:292`, which both forms satisfy), so nothing catches it.
- **Trigger (verified):** `{"name":"Cafe","category":"cafe","country":"SA","phone":"0501234567"}` →
  after `cmd_score`, `phone == "0501234567"`. `main.py:194` yields `+966501234567`.
- **Fix:** port `main.py:192-194` into the loop.

### F6 · S2 — a row of entirely blank cells is scored into a phantom lead and written back to the master

- **Where:** `cli.py:260-262`. The message says `"has no usable rows"` but the guard is
  `if not records`, which only catches a header-only file.
- **Breaks:** `csv.DictReader` yields a row for `,,,,,,,,,,,,,,,,,,,,,,,`. `cmd_score` proceeds,
  and every predicate in `recommend_service` reads the resulting all-`None` row as "nothing
  present", landing on Tier 6 (`pitch_recommender.py:90-91`).
- **Trigger (verified):** header line plus one all-empty line. `rc=0`, and the master now holds
  `{"name": None, "country": "LB", "industry_priority": "low",
  "recommended_service": "Full digital launch (brand + website + social setup)", "lead_score": 0}`.
  A helper opening `without_websites.csv` sees a row that pitches a full digital launch at a
  business with no name.
- **Fix:** drop rows with no `name`, matching `cmd_validate`'s own check at `cli.py:151`.

### F7 · S3 — non-canonical `website_live` text parses to `None` and the downgrade is persisted

- **Where:** `main.py:103`, reached from `cli.py:259`. Only the exact Python reprs `"True"` and
  `"False"` are recognised.
- **Breaks:** a master that has been through Google Sheets (the master lives on Drive,
  `.github/workflows/scrape.yml:98-99`) or a human editor yields `"FALSE"`. That becomes `None`,
  and the highest-value pitch in the product (`pitch_recommender.py:55-58`) is replaced. `cmd_score`
  then **writes the downgrade back**, so one round trip makes it permanent. `cmd_stats` counts the
  same cell as neither live nor dead (`cli.py:107-110`), so the two commands disagree about one file.
- **Trigger (verified):** identical row, `website_live` cell `False` vs `FALSE`:
  `False` → `False` → `"Website rebuild + maintenance"`; `FALSE` → `None` → `"Discovery call - scope the right service"`.
  `"false"`, `"0"`, `"1"`, `""` all behave as `FALSE`.
- **Why S3:** main.py's own output round-trips correctly, so this needs a spreadsheet round trip
  or a hand edit. Reachable, not the default path.
- **Fix:** `main.py:103` → case-insensitive match on `{"true","1","yes"}` / `{"false","0","no"}`.

### F8 · S3 — `resolve_country` launders unrecognised country codes into `"LB"`, and `cmd_score` persists it

- **Where:** `cli.py:266` → `main.py:62-74`.
- **Breaks:** `resolve_country` recognises `LB/SA/LEB/LBN/KSA/SAU/SAU-AR` and the phone prefixes
  `+966/00966/+961/00961`, then falls through to `_DEFAULT_COUNTRY = "LB"` (`main.py:74`).
  `dedup._KNOWN_COUNTRY_CODES` (`dedup.py:8-21`) knows **twelve** countries including Jordan `962`;
  `resolve_country` knows two. The two country resolvers disagree, and `cmd_score` is a one-shot
  backfill that hits every legacy row at once and has no third state to fall back to — it must write something.
- **Trigger (verified):** `{"name":"X","category":"cafe","country":"Jordan","phone":"+962791234567"}`
  → after `cmd_score`, `country == "LB"`. The row is now a Lebanese lead.
- **Fix:** return `None` for unmapped values and have `cmd_score` leave the cell alone (or fail loudly);
  share one country table between `dedup.py:8` and `main.py:50`.

### F9 · S3 — no `--dry-run`, no backup, no confirmation, and the default is destructive

- **Where:** `cli.py:274` (`out = ... else path`), `cli.py:306` (`--out` defaults to `None`).
- **Verified:** `build_parser().parse_args(["score"])` → `file='data/all_businesses.csv'`, `out=None`.
  `parse_args(["score","--dry-run"])` → `SystemExit 2`.
- **Why S3:** not incorrect, just sharp. A verification run of a scoring change requires manually
  copying the master to a scratch path, and the copy step is exactly the step people forget.
- **Fix:** `--dry-run` that stops before `cli.py:275`, plus an automatic `path.with_suffix(".csv.bak")` copy when `out is None`.

---

## The tests this function still needs

Every assertion below was executed against the current tree. **PASS-now** marks a regression guard
for behaviour that is already correct. **FAIL-now** marks a test that will fail until the
corresponding finding is fixed — those are written first, on purpose.

Harness prerequisite for all of these: `sys.path` must include the repo root *and* `tests/`, and you
must call `_stub_requests()` from `tests/test_lead_signal.py:23` before importing anything that
transitively imports `enricher`. Do not reimplement the stub — see "Not a bug, but worth knowing".

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_lead_signal import _stub_requests
_stub_requests()

import argparse, contextlib, csv, io, tempfile, unittest
from pathlib import Path

import cli, enricher, main
from main import FIELDS, load_master, write_csv
from enricher import completeness_score, infer_region, lead_score
```

---

### Properties

#### P1 — `test_score_is_a_fixpoint_on_pipeline_output` · **PASS-now** · the golden differential

**Property:** for any records `main()` produces, `cmd_score` changes nothing.
**Pins:** every scoring bug `5222d32` and `99493b9` fixed, at the only place they are reachable
end-to-end. This is the test that would have caught a partial `leadminer score` backfill.

Input — three fixtures exercising a live site, a dead site, and an unreachable site:

```python
FIXTURE = [
    {"name": "Cafe Hammam", "category": "restaurant", "country": "LB",
     "address": "Hamra, Beirut, Lebanon", "lat": 33.89, "lon": 35.50,
     "phone": "70123456", "website": "https://a.example",
     "rating": 3.4, "review_count": 3, "source": "osm"},
    {"name": "Dead Clinic", "category": "clinic", "country": "LB",
     "address": "Verdun, Beirut, Lebanon", "lat": 33.87, "lon": 35.45,
     "phone": "+9611234567", "website": "https://b.example",
     "rating": 4.9, "review_count": 120, "email": "x@b.com", "source": "osm"},
    {"name": "Blocked Shop", "category": "boutique", "country": "LB",
     "address": "Jounieh, Lebanon", "lat": 33.98, "lon": 35.62,
     "website": "https://c.example", "rating": 2.0, "source": "google_places"},
]

class OSM:      # three distinct classes: main.py:158 dispatches on type(scraper).__name__
    def scrape(self): return iter(FIXTURE)
class Wiki:
    def scrape(self): return iter([])
class Places:
    def scrape(self): return iter([])
```

Patch, inside the test (restore in `addCleanup`): `main.OSMScraper = OSM`,
`main.WikidataScraper = Wiki`, `main.GooglePlacesScraper = Places`,
`enricher.check_websites = lambda records, workers=40: records`, `main.DATA_DIR = tmpdir/"data"`.
Run `main.main()` under `redirect_stdout`. Then `cmd_score(Namespace(file=str(master), out=str(out)))`.

Asserted output (observed exactly):

| name | country | region | phone | `website_live` | `industry_priority` | `lead_score` | `recommended_service` |
|---|---|---|---|---|---|---|---|
| Cafe Hammam | LB | Beirut | `+96170123456` | `None` | high | 40 | RTYLR commerce OS (POS, online ordering, CRM) |
| Dead Clinic | LB | Beirut | `+9611234567` | `None` | high | 50 | Lead-gen overhaul (landing pages + Google Ads + WhatsApp capture) |
| Blocked Shop | LB | Mount Lebanon | `None` | `None` | high | 25 | Discovery call - scope the right service |

```python
self.assertEqual(rc, 0)
self.assertIn("re-scored 3 records, 0 changed", out_text)   # exact observed string
self.assertEqual([dict(r) for r in load_master(out)], produced)
```

#### P2 — `test_second_run_reports_zero_changed_and_is_byte_identical` · **PASS-now**
**Property:** `cmd_score` is idempotent. **Pins:** the "re-scoring keeps moving the numbers"
class of bug — the reason you cannot safely re-run a migration.

Input: any file already produced by `cmd_score`. Asserted output: first rerun prints
`0 changed`, `rc == 0`, and `load_master(path)[0] == snapshot` for every row. Verified on the P1
output and on a single-row restaurant fixture. This is also the cheapest place to pin the exit-code
gap: `rc == 0` even when nothing moved, so a CI gate cannot distinguish a working migration from a
no-op. Add `self.assertEqual(rc, 0)` now and change it to `3` when the F2 fix lands.

#### P3 — `test_score_preserves_every_input_column` · **FAIL-now (S1)**
**Property:** `set(output header) ⊇ set(input header)` and
`set(output header) \ set(input header) ⊆ set(FIELDS)`. **Pins:** S1.

Input — a 26-column header, `FIELDS + ["place_id", "scoring_version", "gmaps_url"]`, one row with
`place_id="ChIJxyz"`, `scoring_version="lead-v1"`, `gmaps_url="https://maps.google.com/?cid=1"`.
Run `cmd_score(Namespace(file=p, out=None))`. Asserted output:

```python
before, after = load_master(p)[0].keys(), load_master(p)[0].keys()   # before captured first
self.assertEqual(set(before) - set(after), set())          # FAILS: {place_id, scoring_version, gmaps_url}
self.assertEqual(after["scoring_version"], "lead-v1")
self.assertEqual(after["place_id"], "ChIJxyz")
```

Observed today: `rc == 0`, 26 → 23 columns, dropped exactly
`{'place_id', 'scoring_version', 'gmaps_url'}`. Run this test **before** implementing the S1 fix;
it is the proof of data loss.

#### P4 — `test_score_does_not_mutate_the_input_when_out_is_given` · **PASS-now**
**Property:** with `--out`, the input is byte-identical afterwards. **Pins:** the F-series of
"score wrote over the master" bugs; the counterpart to S3.

Input: one-row CSV, `{"name":"X","category":"cafe","country":"LB","lead_score":99}`; capture
`p.read_bytes()`; run `cmd_score(Namespace(file=str(p), out=str(tmp/"sub"/"o.csv")))`.
Asserted output: `rc == 0`, `p.read_bytes() == before`, output has 1 row, and the nested output
directory was created (`main.py:121`). Also assert no `.tmp` sibling survives — the same
invariant `tests/test_lead_signal.py:178` already asserts for a failed write.

#### P5 — `test_lead_score_survives_the_csv_round_trip` · **PASS-now**
**Property:** for every row, `lead_score(load_master(write_csv(rows)))` equals `lead_score(rows)`
where `rows` is already the parsed form. **Pins:** any future numeric signal added to
`enricher.lead_score` whose type is not registered in `_FLOAT_FIELDS`/`_INT_FIELDS`
(`main.py:43-44`); today it is the guard that keeps `cli.py:275` from silently changing scores by
rewriting floats as strings.

Input: three rows covering the tri-state and the numeric fields —
`{"name":"A","category":"restaurant","country":"LB","phone":"+96170123456","email":"a@b.com",
"whatsapp":"+96170123456","instagram":"a","website":"https://a.example","website_live":True,
"rating":3.2,"review_count":"5","industry_priority":"high","source":"osm|wikidata"}`;
a DEAD row with `website_live=False, rating=4.9, review_count="250"`; and an all-blank row.
Asserted output: `[lead_score(r) for r in before] == [lead_score(r) for r in after]` — observed
`True`. **If you add a field to `lead_score`, add it to `_FLOAT_FIELDS`/`_INT_FIELDS` or this fails.**

#### P6 — `test_score_preserves_the_website_live_tri_state` · **PASS-now**
**Property:** `website_live ∈ {True, False, None}` after a round trip, and `LIVE`→`True`,
`DEAD`→`False`, `UNKNOWN`→`None` with no collapse. **Pins:** the 043 bug the whole `enricher.py:145-156`
comment block describes — "conflating them was the single worst bug in the pipeline".

Input: three rows, `website_live` cells `True`, `False`, `""`. Asserted output:
`[r["website_live"] for r in load_master(p)] == [True, False, None]`.
Note this pins only the *canonical* trio. The `FALSE`/`0`/`1` variants are E9, and they currently
fail — see S3.

#### P7 — `test_changed_count_covers_every_recomputed_field` · **FAIL-now (S2)**
**Property:** the reported `changed` equals the number of rows where **any** of
`country, industry_priority, recommended_service, lead_score` differs before and after.
**Pins:** S2. This is the migration-visibility contract.

Input: a row already scored correctly by current rules, with **only** `recommended_service` rolled
back to `"Website rebuild + maintenance"`. Asserted output:

```python
self.assertIn("1 changed", out_text)   # FAILS: observed "re-scored 1 records, 0 changed"
self.assertNotEqual(load_master(p)[0]["recommended_service"], "Website rebuild + maintenance")
```

Observed today: `rc == 0`, `"re-scored 1 records, 0 changed"`, file changed to
`"RTYLR commerce OS (POS, online ordering, CRM)"`.

#### P8 — `test_score_is_total` · **PASS-now**
**Property:** `cmd_score` never raises and never returns a code outside `{0, 1, 2}`, for any
input file. **Pins:** the "re-score dies halfway through a 500k-row migration" failure, and the
fact that `cli.py:249-252` imports the whole pipeline to do pure arithmetic.

Input — one table-driven loop over these CSV cells, one row each, all with
`name="X", category="cafe", industry_priority="high", lead_score=0`:
`rating` ∈ `{"4.2", "3.0", "", "abc", "0", "-1", "1e400", "NaN", "  "}`;
`website_live` ∈ `{"True", "False", "", "TRUE"}`;
`country` ∈ `{"", "  ", "sa", "SAU-AR", "Jordan", "+966501234567"}`;
`phone` ∈ `{"", "---", "00966501234567", "+962 51 234 5678", "(0)"}`;
plus a ragged row (`A,LB` with fewer fields than the header) and a row containing a quoted newline.
Asserted output per case: `rc in (0, 1, 2)`, `path.exists()`, and no `.tmp` file left in the
directory. This loop is also where you will discover the `NaN` case — `float("NaN")` succeeds at
`main.py:93`, `load_master` yields `nan`, and `nan < 4.0` is `False` at `enricher.py:313`, so a
`NaN` rating silently loses its low-rating penalty. Add the pin when you fix it.

#### P9 — `test_exit_codes_are_distinct_and_documented` · **PASS-now**
**Property:** missing file → 2, zero data rows → 1, success → 0. **Pins:** shell-scripting errors,
and the fact that `cmd_score`'s error handling is untested while `cmd_stats`' is
(`tests/test_lead_signal.py:255-264`).

| literal input | asserted |
|---|---|
| `Namespace(file=str(tmp/"nope.csv"), out=None)` | `rc == 2`, stderr contains `no such file` |
| header line only, `",".join(FIELDS)+"\n"` | `rc == 1`, stderr contains `no usable rows` |
| one good row | `rc == 0`, stdout contains `re-scored 1 records` |

Observed: `2`, `1`, `0`. Note `cmd_score` returns `1` for a *zero-row* file whose message claims
"no usable rows" — the message over-promises. E7 pins the fix.

#### P10 — `test_score_is_a_pure_function_of_the_row` · **PASS-now**
**Property:** re-scoring row *i* cannot depend on row *i-1*, and row order is preserved.
**Pins:** accidental shared mutable state in the scoring helpers.

Input: five rows whose categories are `["restaurant", "clinic", "", "boutique", "cafe"]` with
varying `website_live`. Asserted output: run once, capture
`[r["recommended_service"] for r in load_master(p)]`; shuffle the input rows, run again on the
shuffled file, and assert the *per-row* mapping is identical (compare by `name`, not by index).
Also assert `[r["name"] for r in out] == [r["name"] for r in shuffled_input]`.

---

### Examples

#### E1 — `test_score_does_not_recommend_a_rebuild_for_an_unreachable_site` · **PASS-now**
**Pins:** `5222d32` / audit 043, verified end-to-end through the CLI.
`tests/test_lead_signal.py:85-91` asserts this at the `recommend_service` level only; nothing
asserts it survives a CSV load/score/write cycle, and the CSV cycle is where the bug lived.

Input row: `{"name":"Cafe Hammam","category":"restaurant","country":"LB",
"phone":"+96170123456","website":"https://a.example","website_live":"",
"industry_priority":"high","lead_score":30}` (note the **empty** cell, i.e. UNKNOWN, not False).
Asserted output:

```python
self.assertEqual(rc, 0)
self.assertIsNone(load_master(p)[0]["website_live"])            # unreachable stays unreachable
self.assertEqual(load_master(p)[0]["lead_score"], 30)           # +15 phone, +15 high, no site credit
self.assertNotEqual(load_master(p)[0]["recommended_service"],
                    "Website rebuild + maintenance")
```

Observed: `website_live None`, `lead_score 30`, pitch
`"RTYLR commerce OS (POS, online ordering, CRM)"`.

#### E2 — `test_score_backfills_the_rebuild_pitch_for_a_confirmed_dead_site` · **PASS-now**
**Pins:** the same fix, in the direction that matters for a backfill — the strongest pitch in the
product must be *re-derived* from a pre-043 master, not preserved from it.

Input row: as E1 but `website_live="False"`, `lead_score=0`,
`recommended_service="Website rebuild + maintenance"` (stale), `review_count="0"`.
Asserted output: `load_master(p)[0]["recommended_service"] == "Website rebuild + maintenance"`
and `lead_score == 35` (`+15 phone +20 dead site`, no priority on this fixture).
**This is the row that P7 says will be reported as `0 changed`** — run E2 and P7 together.

#### E3 — `test_score_recomputes_completeness_score` · **FAIL-now (S2)**
**Pins:** S3-the-finding above (score/completeness era mismatch); `audit 070:21-25`.

Input: `{"name":"Cafe","category":"cafe","country":"LB","phone":"70123456",
"email":"a@b.com","website":"https://x.com","address":"x","completeness_score":0,
"industry_priority":"high","lead_score":0}`.
Asserted output: `load_master(p)[0]["completeness_score"] == completeness_score(row) == 4`.
Observed today: `0`. **FAILS.**

#### E4 — `test_score_reinfers_region_for_a_blank_region_row` · **FAIL-now (S2)**
**Pins:** the `enricher.py:330-335` behavior that `cmd_score` omits; also
`tests/test_lead_signal.py:147-153` (blank country disabling region inference — `cmd_score` fixes
country on line 266 and then still does not use it).

Input, two rows to pin both inference paths:
`{"region":"", "address":"Hamra, Beirut, Lebanon", "country":"LB", "lat":"", "lon":""}` and
`{"region":"", "address":"", "country":"SA", "lat":"24.7136", "lon":"46.6753"}`.
Asserted output: `["Beirut", "Riyadh"]`. Observed today: `[None, None]`. **FAILS.**
Note the second row proves the fix must pass the resolved `country` through to `infer_region`,
since `enricher.py:90` picks the coordinate box set from it.

#### E5 — `test_score_normalizes_phone` · **FAIL-now (S2)**
**Pins:** `5222d32`'s cross-country phone fix and the E.164 invariant of
`tests/test_lead_signal.py:110-123`.

Input: four rows, `country` ∈ `{"SA","LB","SA","LB"}`, `phone` ∈
`{"0501234567","70123456","00966501234567","00961370123456"}`.
Asserted output: `["+966501234567","+96170123456","+966501234567","+961370123456"]`
— identical to `dedup.normalize_phone(phone, country)`, i.e. identical to `main.py:194`.
Observed today: the four inputs come back unchanged. **FAILS.**

#### E6 — `test_score_rejects_blank_rows` · **FAIL-now (S2)**
**Pins:** the phantom-lead defect; the invariant `cmd_validate` already enforces at `cli.py:151`
but `cmd_score` never applies.

Input: header line plus two lines — one all-empty, one `,,,,0,,,,,,,,,,,,,,,,,,,` (blank name,
`lead_score=0`).
Asserted output: `rc == 0`, output has **0** rows, message contains the row count.
Observed today: output has 2 rows, both with `name=None`, both with
`recommended_service == "Full digital launch (brand + website + social setup)"`,
`industry_priority == "low"`, `country == "LB"`, `lead_score == 0`, and the message says
`re-scored 2 records, 2 changed`. **FAILS.**
If you prefer not to drop rows in-place, the alternative assertion is `rc == 1` with
`"no usable rows"` — but then the file must not have been rewritten. Pick one and pin it.

#### E7 — `test_score_survives_a_string_rating_cell` · **PASS-now**
**Pins:** the `main.py:90-95` float cast, which is the only thing standing between a hand-edited
master and a `TypeError`. `enricher.py:313` does `rating < 4.0` unguarded; a `str` rating raises
(verified: `lead_score({"rating": "4.2"})` → `TypeError: '<' not supported between instances of
'str' and 'float'`). `cmd_score` is safe only because `load_master` casts first. Nothing tests that.

Input: one row per cell, `rating` ∈ `{"4.2", "3.0", "", "abc"}`, everything else
`{"name":"A","category":"cafe","country":"LB","industry_priority":"high","lead_score":0}`.
Asserted output (observed exactly):

| cell | parsed `rating` | `lead_score` |
|---|---|---|
| `"4.2"` | `4.2` | `15` |
| `"3.0"` | `3.0` | `25` |
| `""` | `None` | `15` |
| `"abc"` | `None` | `15` |

`15 = priority high only`; `25 = +10` low-rating penalty (`enricher.py:312-314`). If someone
removes `rating` from `_FLOAT_FIELDS`, rows 1 and 2 crash the command.

#### E8 — `test_score_does_not_dedup` (or `test_score_dedups`) · **PASS-now** · pick deliberately
**Pins:** nothing yet — this is an **undocumented scope decision** that needs to become one.
`main.py:180` runs `dedup(combined)` before scoring; `cli.py` never does. So
`leadminer score` cannot be used to apply the *dedup* rules of `99493b9` to a historical master,
which is the one thing an operator would most plausibly reach for it to do, and
`cmd_validate`'s duplicate check (`cli.py:190`) will flag a master that `score` cannot fix.

Characterization version (assert today's behaviour): input = the same
`{"name":"Cafe","category":"cafe","country":"LB","phone":"+96170123456"}` twice;
assert `len(load_master(p)) == 2` after `cmd_score`, and `rc == 0`.
Desired version (assert the fix): `len(load_master(p)) == 1`, and the surviving row must be the
`_merge` winner per `tests/test_lead_signal.py:278-283`. **Do not write both.** Decide, then write
one and note the decision in the `cmd_score` docstring at `cli.py:248`.

#### E9 — `test_score_rejects_or_normalises_non_canonical_website_live` · **FAIL-now (S3)**
**Pins:** the `main.py:103` exact-string match; the disagreement between `cmd_score` and
`cmd_stats` (`cli.py:107-110`) about the same file.

Input: six rows, identical except the `website_live` cell, values
`["True", "False", "", "TRUE", "FALSE", "0"]`, each with `website="https://x.com"`,
`category="restaurant"`, `industry_priority="high"`.
Asserted output: parsed values `[True, False, None, True, False, False]` and pitches
`["SEO audit + visibility upgrade", "Website rebuild + maintenance",
"RTYLR commerce OS (POS, online ordering, CRM)", "SEO audit + visibility upgrade",
"Website rebuild + maintenance", "Website rebuild + maintenance"]`.
Observed today: parsed `[True, False, None, None, None, None]`, so rows 4-6 all get the RTYLR /
Discovery pitch and the master is rewritten with the loss. **FAILS.**

---

## False positive — do not write this test

`docs/audits/084-SYNTH-adversarial-fix-review.md:95` claims:

> "If `leadminer score` (`cli.py:cmd_score`) is run on the exported CSV, `load_master` converts
> `""` back to `None`. Rescoring the lead now evaluates `has_phone = False`, silently mutating
> `recommended_service` to a different pitch than what `main.py` originally produced."

**This does not hold.** I tested it with identical field values, varying only `""` vs `None`:

```
pitch('' variant)   : RTYLR commerce OS (POS, online ordering, CRM)
pitch(None variant) : RTYLR commerce OS (POS, online ordering, CRM)
equal: True
```

Every field `recommend_service` reads is consumed through `bool(...)`
(`pitch_recommender.py:31`, `:37-41`), `is True`/`is False` (`:34-35`), `or 0` (`:43`, `:48`),
`int(x) if x else 0` (`:45`, `:50`), or `.strip().lower()` (`:53`) — all of which treat `""` and
`None` identically. Same for `lead_score` (`enricher.py:288-317`) and `industry_priority`
(`whitelist.py:138`). `resolve_country` does distinguish them at `main.py:60`
(`isinstance(value, str) and value.strip()`) but treats them the same too, so `""` and `None` both
fall through to the phone check and then `_DEFAULT_COUNTRY`.

There **is** one genuine `""`/`None` asymmetry in the chain, and it points the other way:
`enricher.py:313` does `rating is not None and rating < 4.0`, which raises `TypeError` on `""`
and is safe on `None`. `load_master`'s cast (`main.py:90-95`) normalises `""` → `None` first, so
`cmd_score` is safe — that is what **E7** pins. Nobody should write a test for the pitch mutation
084 describes; it would be testing a behaviour that does not exist and would "pass" for the wrong
reason.

---

## What genuinely cannot be tested here, and why

1. **Concurrent `leadminer score` vs a live pipeline run.** `write_csv` replaces the master
   wholesale via `os.replace` (`main.py:130`), and the temp path is fixed
   (`path.with_suffix(path.suffix + ".tmp")`, `main.py:122`) — two concurrent scorers write the
   *same* temp file, and a scorer running while the GitHub Actions job is mid-flight clobbers it.
   `concurrency: group: leadminer-scrape` (`.github/workflows/scrape.yml:13-15`) protects workflow
   runs from each other only; a local process is outside it. A threaded unit test would be flaky
   and would prove nothing about POSIX rename atomicity. This needs an `flock` on the master plus
   a test that asserts the lock is acquired, not a race simulation.

2. **Whether the re-scored values match what a *live* enrichment would produce.** `cmd_score`
   deliberately makes no HTTP call (`cli.py:12`, `cli.py:248`). A stale `website_live` in the
   master means the score is a function of the last observation, not of now — `audit 070:25`
   says so explicitly. Nothing offline can assert "this score is correct"; the strongest available
   claim is "this score is the current rule applied to these stored features", which is exactly
   what P1 pins. Getting closer requires a recorded HTTP fixture, which is audit 032's job and does
   not exist yet.

3. **Anything at master scale.** `data/` is gitignored (`.gitignore:7`), there is no
   `tests/fixtures/`, and I may not create one. Every assertion here is a 1-to-6-row CSV. Row
   *count* effects — the 500k-row memory profile of `load_master` (`main.py:87` materialises the
   whole file), and whether the `--out`-vs-in-place path choice is sane at that size — can only be
   characterised with a generated fixture, not asserted here.

4. **Whether `extrasaction="ignore"` is the *right* schema policy** (S1's fix). "Preserve unknown
   columns" and "reject unknown columns" are both defensible; only one can be pinned. Same for
   `--dry-run` (S3): asserting the flag is *absent* is a test that must fail when someone improves
   the code. Write the behaviour test after the decision, not before.

5. **Whether `leadminer score` is safe on a spreadsheet round-tripped master.** The BOM half is
   covered (`tests/test_lead_signal.py:180-188`). Whether Google Sheets renders Arabic business
   names correctly, or whether it rewrites `FALSE` (S3), is a property of Sheets, not of this code.

---

## Not a bug, but worth knowing

- **`cmd_score` requires `args.out` to exist as an attribute** (`cli.py:274`). The three existing
  CLI tests build `argparse.Namespace(file=p)` (`tests/test_lead_signal.py:219`, `:238`, `:249`)
  and only work because `cmd_stats`/`cmd_validate` never touch `args.out`. Copying that idiom into
  a `cmd_score` test raises `AttributeError: 'Namespace' object has no attribute 'out'` (verified).
  New tests must pass `out=...` explicitly.

- **`cli.py:12` promises `score` "never touches the network" — true, but it still needs
  `requests` installed.** `cli.py:249-252` imports `main`, `enricher`, `pitch_recommender` and
  `scrapers.whitelist` to do pure arithmetic, and `enricher.py:2` imports `requests` at module
  scope. `main.py:25-27` additionally imports all three scraper classes. New tests must import
  `_stub_requests` from `tests/test_lead_signal.py:23`, not reimplement it — the `urllib3` stub at
  `tests/test_lead_signal.py:52-60` is required too, because `enricher.py:327` imports it inside
  `enrich()`.

- **`main.FIELDS` has 23 entries, not the 22 the brief lists** (`main.py:33-40`). This matters
  beyond documentation: S1's blast radius is exactly "whatever is not in that list".

- **`cmd_score` is the only CLI command that mutates `data/` in place.** `cmd_run` writes through
  `main.main()`; `cmd_stats`/`cmd_validate`/`cmd_doctor` are read-only or network-only. The
  workflow's size gate (`.github/workflows/scrape.yml:88-96`) only runs in CI, so a local
  `leadminer score` is completely ungated.

- **`lead_score` has no defensive cast on `rating`** (`enricher.py:312-313`). Safe through
  `cmd_score` (E7), but `main.py:197` calls it on raw `dedup` output where `rating` is typed
  `float | None` (`scrapers/base.py:21`) — a `TypedDict`, which is not enforced at runtime.
  `scrapers/google_places.py:297` passes `place.get("rating")` straight through. One API response
  shape change from string to float and `main.py` dies after a full scrape.

- **`load_master` preserves the negative and out-of-range cases too:** `lead_score="-5"` → `-5`,
  `lead_score="999"` → `999` (`main.py:96-101` only casts, never ranges). `cmd_score` will
  faithfully propagate an absurd stored score into the summary and into `sales_ready` ordering.
  `cmd_validate` catches out-of-range `rating` (`cli.py:175-188`) but has no equivalent check for
  `lead_score`.

---

## Recommended order of work

1. **Write the failing tests first, before any fix:** P3 (F1, column loss), E6 (F6, blank rows),
   E3/E4/E5 (F3/F4/F5, the three omitted steps). Five tests, five one-line fixes — four in the loop
   at `cli.py:266-272`, one at `main.py:125`. This clears all of S1 and S2 except the counter.
2. **P1 + P2** — the golden differential and idempotence. Both pass today and are the regression
   net for every subsequent scoring change. Land them *before* the step-1 fixes so you can prove
   those fixes did not break the pipeline contract.
3. **P7 (F2) + `--dry-run`/backup (F9).** Fix the counter, then add
   `test_no_op_has_a_distinct_exit_code` alongside it.
4. **E8** — decide dedup-in-or-out, write one test, record it in the `cli.py:248` docstring.
5. **E9 (F7) and the `main.py:103` case-insensitive parse**, plus a matching fix in
   `cmd_stats` (`cli.py:107-110`) so the two commands stop disagreeing about one file.
6. **F8** — one shared country table for `dedup.py:8` and `main.py:50`, and a `None` state so
   `cmd_score` can decline to invent a country.
7. **A file lock on the master**, and only then a test that asserts the lock is taken. Do not
   attempt a concurrency race test.