# 200 — `cmd_score` (`cli.py:247`) under a cron: what it prints, what it swallows, how it fails silently

## Verdict

`cmd_score` is the one command in this repo whose whole job is to **rewrite the
cumulative master in place from untrusted input**, and it is the only command with
no safety net at all: no input contract check, no snapshot, no dry-run, no lock,
no progress output, and one misleading success line. Its only report —
`re-scored N records, M changed` (`cli.py:276`) — counts `lead_score` and nothing
else (`cli.py:270`), so a pitch-rules change that rewrites **every**
`recommended_service` in the file prints `0 changed` and exits `0` (demonstrated
below). Meanwhile the shared temp path (`main.py:122`) silently degrades the
"atomic" write the README promises (`README.md:51-52`) into a byte-level splice
whenever two runs overlap — I reproduced a master containing **field-level hybrids
of two different datasets, published with exit code 0**.

One framing correction before the findings, because it changes which risks are real:
**`score` is not a six-hour job.** 200,000 rows re-score in **8.0 s / 358 MB peak
RSS** on this machine. The six hours belong to `python main.py`'s network phase
(`.github/workflows/scrape.yml:69`). So the cron risks here are not "the run is
slow and gets killed" — they are "the run is *fast*, silent, and rewrites the master
before anyone has looked at it". Note also that `score` is **not wired into the
workflow at all** (`scrape.yml:69` runs `python main.py`), which means the only
realistic invocation is a human typing `leadminer score` — no `--out`, straight at
`data/all_businesses.csv`, which is the default (`cli.py:274`, `cli.py:305`).

All findings below were reproduced locally against this checkout with no network
access (`requests` stubbed exactly as `tests/test_lead_signal.py:23-63` does).

---

## Findings

### S1 — Two overlapping runs splice two datasets into the master, and the winner reports success

- **Where:** `cli.py:274-275` (in-place default + single unguarded write), `main.py:122` (`tmp = path.with_suffix(path.suffix + ".tmp")`), `main.py:124` (`open(tmp, "w")`), `main.py:130` (`os.replace`)
- **Breaks:** The temp filename is a fixed, non-unique, non-exclusive name. Any two
  processes writing the same output path write to *the same inode*:
  - Process A opens `all_businesses.csv.tmp` `"w"` (truncate) and streams 68 MB.
  - Process B opens the same path `"w"` while A is mid-stream and streams its own bytes from offset 0.
  - Whichever calls `os.replace` **last** publishes the mixture. `os.replace` is
    atomic, so nobody ever sees a truncated file — they see a *well-formed* file
    whose rows belong to two different snapshots.
  The corruption lands **mid-record** whenever the byte boundaries disagree, producing
  rows that are structurally valid (22 fields, name populated, coords in the
  Lebanon/Saudi box, rating in range) but contain another lead's data. `leadminer
  validate` (`cli.py:135-201`) cannot see this: every one of its checks
  (`cli.py:151`, `164-173`, `175-188`, `190-192`) passes on a spliced row.
- **Trigger (reproduced):** two `leadminer score` runs against the same `--out`
  whose write phases overlap. `.github/workflows/scrape.yml:15-17` has a
  `concurrency: group: leadminer-scrape` that prevents two *workflow* runs racing,
  but it protects nothing else: a `leadminer score` on a laptop while the cron runs
  on the runner, a re-run after a manual cancel, or `score` racing `main.py`'s own
  `write_csv(DATA_DIR / "all_businesses.csv", ...)` at `main.py:209` all hit the
  same `.tmp`. Measured result of the forced race (120k-row run vs 20k-row run):
  ```
  published master: 20.17 MB, 119941 lines
    MID-rows (A wrote 120,000) = 109,940
    FAST-rows (B wrote 20,000) = 9,999
  line 10060: MID-10058,cafe,Beirut,LB,"Hamra, Beirut",33.89,35.5,+96170123456,,https://x.com,False,,,,,4.2,12
  line 10061: MID-10059,cafe,Beirut,LB,"Hamra, Beirut",33.89,35.5,+96170123456,,https://x.com,False,,,FAST-100
  line 10062: FAST-10001,cafe,Beirut,LB,...
  ```
  Row 10061 is one record whose first 11 columns are lead `MID-10059` and whose tail
  is lead `FAST-10000`. A exited 0; B exited 1 with `FileNotFoundError` on the
  `os.replace` (A had already renamed the tmp away). Two runs that *both* complete
  exit 0 and 0.
- **Amplifier:** `--out` defaults to in-place (`cli.py:274`) and there is no backup
  of the pre-score master anywhere. `data/` is gitignored (`.gitignore`), and the
  Drive copy is only refreshed by the next weekly run (`scrape.yml:88-89`), so a
  spliced local master is the only copy for up to seven days.
- **Fix:** make the temp name unique per writer and refuse to clobber —
  `tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")` opened with
  `os.O_CREAT|os.O_EXCL`; plus an `flock` on `data/.lock` held for the whole of
  `cmd_score`.

### S1 — `changed` counts only `lead_score`, so the command reports `0 changed` after rewriting every pitch

- **Where:** `cli.py:270-271` (`if new != r.get("lead_score"): changed += 1`), `cli.py:266-269`
- **Breaks:** The loop overwrites four columns — `country` (266), `industry_priority`
  (267), `recommended_service` (268), `lead_score` (272) — but the only one measured
  is `lead_score`. `lead_score` never reads `recommended_service`
  (`enricher.py:283-319`), and it reads `industry_priority` only through the
  `high`/`medium` bands (`enricher.py:306-310`). So any edit to the *category sets*
  inside `pitch_recommender.py` — `_ECOM_FRIENDLY` (103-109), `_RTYLR_TARGETS`
  (112-117), `_LEAD_GEN_VERTICALS` (120-132) — changes `recommended_service` for the
  entire database while leaving `lead_score` bit-identical.
- **Trigger (reproduced):** remove `"cafe"` from `_RTYLR_TARGETS` and re-run
  `leadminer score` over 500 cafe rows:
  ```
  re-scored 500 records, 0 changed -> .../t4_out2.csv
  500 rows now pitch: Discovery call - scope the right service
  ```
  Every row moved from `RTYLR commerce OS (POS, online ordering, CRM)` to
  `Discovery call - scope the right service`. Exit code 0. Told `0 changed`, a human
  concludes the edit was a no-op and reverts it — or worse, ships it, because the
  only signal the command emits says nothing happened.
- **Same defect, second direction:** `country` is rewritten on line 266 and is not
  counted either. A run that relabels 40,000 rows' country prints `0 changed`.
- **Fix:** count per-column diffs and print them:
  `changed = {c: sum(1 for ...) for c in ("country","industry_priority","recommended_service","lead_score")}`,
  and exit non-zero when `--expect-changes` is set and the count is zero.

### S1 — No input contract check: any header mismatch rewrites the master as 22-column empty rows, exit 0

- **Where:** `cli.py:259` (`load_master` accepts whatever the header says), `cli.py:275` → `main.py:125` (`DictWriter(..., fieldnames=FIELDS, extrasaction="ignore")`)
- **Breaks:** `load_master` (`main.py:86-104`) produces dicts keyed by
  whatever the file's header says. `cmd_score` never compares that key set to
  `main.FIELDS` (`main.py:33-40`). On write, every missing key becomes `""`
  (`restval`) and every extra key is dropped (`extrasaction="ignore"`). Result: a
  file with the right *shape* and none of the right *content*.
- **Trigger (reproduced):** a CSV headed `Name,Category,Web Site,notes` — a CRM
  export, a hand-saved Google Sheet, or one renamed column (`Name`, `web_site`,
  `website `) in the master:
  ```
  rc=0
    Written: .../renamed_out.csv (3 records)
    re-scored 3 records, 3 changed -> .../renamed_out.csv
  output : 3 rows; row0 = {'name': '', 'category': '', ..., 'country': 'LB',
           'lead_score': '0', 'industry_priority': 'low',
           'recommended_service': 'Full digital launch (brand + website + social setup)', ...}
  non-empty cells in output row0: 4 of 22
  ```
  Point that at `data/all_businesses.csv` (the default) and the cumulative master
  becomes 100% empty rows with a `Full digital launch` pitch on every lead. The
  `notes` column is silently gone. A file with **no header at all** behaves the same
  way: `DictReader` promotes row 1 to the header (`main.py:86`).
- **Why it stays hidden:** nothing in the cron path would catch it. `scrape.yml:71-83`
  is a *size* check (`wc -l`), not `leadminer validate`; `cmd_validate` is never run
  automatically. And it is not obvious from the output — `3 records` and `3 changed`
  look like a healthy run.
- **Fix:** after `load_master`, assert `set(records[0]) == set(pipeline.FIELDS)` and
  return `5` (data error) naming the first missing/unexpected column; refuse
  in-place writes when the assertion fails.

### S1 — `website_live` is reinterpreted, not validated: human-entered `no`/`yes` silently deletes every rebuild pitch

- **Where:** `cli.py:269` (`lead_score` consumes the coerced value), `main.py:102-103` (`True if wl == "True" else (False if wl == "False" else None)`), `enricher.py:300-304` (the +20 dead-site credit), `pitch_recommender.py:34-36,57-58` (Tier 1 rebuild pitch)
- **Breaks:** `load_master` accepts exactly two literal strings. Anything else —
  `yes`, `no`, `1`, `0`, `TRUE`, `Y`, `dead`, `live`, a stray space — becomes
  `None`, i.e. **"we never reached it"**. `cmd_score` then re-scores on that value
  and overwrites the original cell, destroying the human's evidence. The damage is
  the core revenue signal of the whole product: a confirmed-dead site is the
  strongest pitch in the database (`pitch_recommender.py:55-58`) and is worth +20
  `lead_score` (`enricher.py:303-304`).
- **Trigger (reproduced):** two rows whose `website_live` a human typed in a
  spreadsheet, before → after:
  ```
  in : website_live='no'  / 'yes'
  out: website_live=''    / ''
       lead_score=30 (was 63 for the 'no' row)
       pitch='RTYLR commerce OS (POS, online ordering, CRM)'
  ```
  The "rebuild this dead site" pitch became "buy them a POS". Both rows are
  reported as `2 changed`, exit 0.
- **Note the direction is safe, the *data* is not:** `None` never becomes a false
  rebuild claim (`enricher.py:300-304`), so this does not invent bad leads. It
  deletes good ones — and `leadminer validate` has no check for
  `website_live` at all, so the loss is permanent and unflagged.
- **Fix:** in `cmd_score`, count rows whose `website_live` cell is non-empty but not
  exactly `True`/`False` in the *raw* text and print them as a warning before
  overwriting; refuse to write in place unless `--accept-loose-bools` is passed.

### S2 — `score` refreshes one of five product CSVs; the four that sales actually reads keep the old scores and pitches

- **Where:** `cli.py:275` (the only `write_csv` call), `main.py:199-213` (the slicing lives only inside `main.main()`), `cli.py:280-309` (there is no `export` subcommand), `main.py:202-205` (`sales_ready`), `main.py:206` (`qualified`)
- **Breaks:** Per the project's own table (`README.md:41-49`), the deliverable set is
  five files and `sales_ready.csv` is the actionable one. `cmd_score` rewrites
  whichever single file you point it at — by default only `all_businesses.csv`.
  The other four keep the pre-change `lead_score`, `industry_priority` and
  `recommended_service` indefinitely, and the **only** code that can regenerate them
  is `main.main()`, which requires the full network scrape. So the documented way to
  apply a rules change ("re-score an existing CSV with current rules",
  `README.md:16`; `cli.py:248`) leaves the sales-facing list stale, with no warning
  and no offline remedy.
- **Trigger:** change any weight in `lead_score`, run `leadminer score`, then open
  `data/sales_ready.csv`. Every row shows the previous week's scoring.
- **Fix:** make `cmd_score` re-slice and rewrite all five files from the master
  (the slicing is nine lines, `main.py:199-213`), or add the `export` subcommand
  `037-cli-design.md:352-380` already specified and call it from `score`.

### S2 — `completeness_score` is never recomputed, so the master mixes new scores with old completeness

- **Where:** `cli.py:266-272` (four columns rewritten; `completeness_score` is not among them), `enricher.py:349` (the only writer), `enricher.py:101-117`, `main.py:206`
- **Breaks:** `completeness_score` is a pure function of the record's contact
  fields (`enricher.py:101-117`), exactly as `lead_score` is. It is set in
  `enrich()` (`enricher.py:349`), which is only reachable through the network path.
  `cmd_score` copies the previous value through untouched while rewriting
  `lead_score` next to it, so the file ends up internally inconsistent under a
  "re-score with current rules" claim, and `qualified_businesses.csv`
  (`completeness_score >= 1`, `main.py:206`) keeps its old membership forever.
  `region` has the same problem, and it is worse: `cmd_score` writes `country`
  (266), which is precisely the input `infer_region` needs (`enricher.py:79-94`) —
  an entirely offline function — but never calls it, so blank-region rows stay blank.
- **Trigger (reproduced):** 3 records with `completeness_score=0`; change the rule;
  re-run:
  ```
  re-scored 3 records, 0 changed -> .../comp_out2.csv
  C0: completeness='0' (unchanged)  lead='15'
  ```
- **Fix:** call `enricher.completeness_score(r)` and, when `r["region"]` is blank,
  `enricher.infer_region(...)` inside the loop. Both are offline; neither needs the
  `check_websites` half of `enrich()`.

### S2 — `resolve_country`'s default-to-LB stamping, inside a command called "score", permanently mislabels KSA leads and poisons the next dedup

- **Where:** `cli.py:266`, `main.py:47` (`_DEFAULT_COUNTRY = "LB"`), `main.py:59-74`, `main.py:69-74`
- **Breaks:** `resolve_country` falls back to `LB` whenever the cell is blank and
  the phone carries no `+966`/`00966`. A Saudi lead stored with a blank country and
  a *national-format* phone (`0501234567`, which is how most KSA numbers arrive from
  non-Google sources) is stamped `LB` — permanently, because `resolve_country`
  returns any explicit `LB`/`SA` unchanged thereafter (`main.py:59-63`). A
  non-LB/SA code (`AE`, `EG`) is also overwritten to `LB`.
- **Trigger (reproduced):**
  ```
  {"country":"", "phone":"0501234567", "lat":24.7136, "lon":46.6753}
    resolve_country -> 'LB'                     (was blank)
    infer_region(..., 'LB') -> None             infer_region(..., 'SA') -> 'Riyadh'
    normalize_phone('0501234567','LB') -> '+961501234567'   # a Lebanese number
  ```
  Then, on the **next** `leadminer run`, `dedup()` keys that record on
  `+961501234567` (`dedup.py:205`). A Beirut record with that number and a
  Riyadh record with the Saudi national form now share a key:
  ```
  2 records in -> 1 record out
    name='Riyadh Auto Care' phone='0501234567' address='King Fahd Rd, Riyadh'
  ```
  A Riyadh business has been merged into a Lebanese one, and it is in
  `sales_ready.csv` with a `country=LB` label it will never be allowed to shed.
- **Fix:** do not rewrite `country` in `cmd_score` at all (it is not a scoring
  column), or make the fallback consult `lat`/`lon` and refuse to default when the
  coordinates are unambiguously outside Lebanon (`main.py:159-162` already has the
  bounding boxes).

### S2 — Locale and sentinel garbage in numeric columns is nulled silently, deleting scoring signals

- **Where:** `main.py:90-101` (coercion with a bare `except`), consumed at `cli.py:269`, `enricher.py:312-314` (+10 low-rating), `pitch_recommender.py:44-47,70-71` (`review_count < 20`)
- **Breaks:** `float("4,2")` and `float("N/A")` both raise, both are swallowed, and
  both become `None`. Two distinct scoring signals disappear with no log line:
  - `rating` → `None` loses the +10 "low rating = pain point" credit
    (`enricher.py:312-314`) for that row.
  - `review_count` → `None` → `review_count = 0` (`pitch_recommender.py:44-47`) →
    `review_count < 20` is true → **every** row with a live website is re-pitched
    `SEO audit + visibility upgrade`, regardless of whether it has 4 reviews or 4,000.
- **Trigger (reproduced), one row `rating='4,2'`, `review_count='1,234'`:**
  ```
  re-scored 1 records, 1 changed
  rating='' review_count='' lead_score=40
  pitch='SEO audit + visibility upgrade'
  ```
  A comma decimal is what a de-DE locale export or an Arabic-locale spreadsheet
  produces; `1,234` is what a human writes. The `1 changed` line reports the
  damage as if it were a rules effect. `cmd_validate` cannot catch it either: it
  `continue`s past unparseable values (`cli.py:166-169`, `cli.py:180-184`) and prints
  `all checks passed`. **`cmd_score` launders input corruption into blank cells that
  `cmd_validate` then certifies as clean.**
- **Fix:** count and print rows whose raw cell was non-empty but failed coercion,
  per field, and exit non-zero unless `--allow-coercion-loss` is passed.

### S2 — Interruption: Ctrl-C is handled, SIGTERM produces an empty log, and there is no progress output at all

- **Where:** `cli.py:316-318` (KeyboardInterrupt only), `cli.py:264-276` (loop prints nothing), `main.py:131-133` (`except BaseException` cleanup), `main.py:122` (leftover temp name)
- **Breaks:** Three separate gaps, in increasing order of how long they stay hidden:
  1. **Ctrl-C is handled correctly** — verified: interrupting after 700 of 1000 rows
     leaves the master byte-identical, writes no partial output, and
     `cli.py:316-318` prints `interrupted` and returns 130. The same holds for an
     interrupt *inside* `write_csv`, because `main.py:131-133` catches `BaseException`,
     unlinks the temp and re-raises. This part is right.
  2. **SIGTERM is not handled.** SIGTERM/SIGKILL raise no Python exception, so
     `main.py:131-133` never runs and `cli.py:316-318` never fires. Verified by
     signalling the process mid-run: `returncode -15`, `stdout=''`, `stderr=''` —
     **not one byte of output**, and the `data/*.csv.tmp` file left behind holds the
     entire new dataset. A cancelled job, a `timeout-minutes: 300`
     (`scrape.yml:25`) kill, or `docker stop` all produce a log that is
     indistinguishable from "the process never started".
  3. **No progress output exists to interrupt.** The loop (`cli.py:265-272`) prints
     nothing; the only output is `main.py:134` and `cli.py:276` at the very end. For
     an 8-second run that is merely unfriendly; if the master ever grows to the
     multi-minute range there is no way to tell "still working" from "hung", and the
     `changed` counter — the one number that matters — is only visible after the
     master has already been replaced.
- **Fix:** install a SIGTERM handler that raises `KeyboardInterrupt`, and print
  progress to **stderr** every 10k rows (plus `records processed` in the SIGTERM
  path) so a killed run always says how far it got.

### S3 — Whole-file memory with no streaming: ~1.7 KB/row resident, no cap

- **Where:** `main.py:81` (`records = []` accumulating every row), `cli.py:259`, `cli.py:265`
- **Measured:** 200,000 rows / 36.6 MB CSV → **8.0 s wall, 358 MB peak RSS**
  (≈1.7 KB/row, excluding a ~20 MB interpreter baseline). Extrapolating: 500k rows
  ≈ 870 MB, 1M ≈ 1.7 GB, on a 7 GB GitHub runner that also holds the network phase's
  own copy. Today's master is orders of magnitude smaller, so this is a ceiling
  problem, not a present outage — but nothing in the function streams, so the limit
  arrives without warning.
- **Fix:** iterate `csv.DictReader` lazily and write row-by-row (the recompute is
  per-row and independent), or at minimum log the row count and the estimated
  footprint before the loop.

### S3 — Wrong-but-loud error handling: raw tracebacks for a directory or non-UTF-8 bytes

- **Where:** `cli.py:314-318` (only `KeyboardInterrupt` is caught)
- **Breaks:** Two inputs produce a full traceback and exit 1 instead of a one-line
  message. Both verified:
  ```
  leadminer score data/                 -> IsADirectoryError, traceback, exit 1
  leadminer score master-with-0xE9.csv  -> UnicodeDecodeError, traceback, exit 1
  ```
  The exit code is right (the master is untouched in both cases) and this is the
  *loud* failure mode, so severity is low — but a cron log that ends in a Python
  stack trace reads as "the tool is broken" rather than "the input is wrong", and
  the Unicode case is reachable in practice: the master arrives on the runner via
  `rclone copy` (`scrape.yml:56`) and `load_master` hard-codes
  `encoding="utf-8-sig"` (`main.py:85`) with no `errors=` policy.
- **Fix:** catch `(OSError, UnicodeError, csv.Error)` in `main()` and map them to
  exit `2` with a one-line stderr message; add `--encoding` with an
  `errors="replace"` fallback.

### S3 — The `no usable rows` message is inaccurate, and `score` mutates a data column it never mentions

- **Where:** `cli.py:260-262`, `cli.py:266`
- **Breaks:** `load_master` never drops a row (`main.py:104` appends every parsed
  row unconditionally), so `has no usable rows` (`cli.py:261`) can only fire for a
  0-byte file, a header-only file, or a BOM-only file — all three verified to return
  `1` with the output file left untouched, which is correct behaviour with a
  misleading message. Separately, `cmd_score` rewrites `country` (266), a column
  with nothing to do with scoring, and says nothing about it; an operator diffing the
  master will find a `country` column that moved and a `region` column that did
  not, with no line in the output explaining either.
- **Fix:** say `no data rows in {path}`; list the rewritten columns in the output
  line; add a `--dry-run` that reports the diff without writing.

---

## What it actually prints, swallows, and does

**Prints** (`cli.py:276`, plus `main.py:134`), and nothing else on success:

```
  Written: data/all_businesses.csv (200000 records)
re-scored 200,000 records, 200,000 changed -> data/all_businesses.csv
```

Prose with a bare path, not greppable or parseable, on **stdout** — so a cron
wrapper cannot assert on it without `grep`. Errors go to stderr (`cli.py:256`,
`cli.py:261`), which is correct.

**Swallows:**

| Swallowed | Where | Consequence |
|---|---|---|
| Every unparseable numeric cell | `main.py:90-101` | signal deleted, reported as a "change" |
| Every non-`True`/`False` `website_live` cell | `main.py:102-103` | human verdicts become "unknown", pitches deleted |
| Every column not in `FIELDS` | `main.py:125` `extrasaction="ignore"` | `notes` and friends vanish |
| Every field not in the input header | `main.py:125` `restval=""` | whole rows blanked |
| `industry_priority`, `recommended_service`, `country` changes | `cli.py:270-271` | `changed` under-reports by design |
| `completeness_score`, `region`, `phone` staleness | `cli.py:266-272` | master internally inconsistent |
| SIGTERM | `cli.py:314-318` | zero output, no cleanup |
| No per-row exception isolation | `cli.py:265-272` | one structural failure loses the whole batch — though in practice `industry_priority`, `recommend_service` and `lead_score` are total functions of their inputs (`whitelist.py:133-145`, `pitch_recommender.py:44-52`, `enricher.py:283-319`), so this is a latent rather than an observed risk |

**Called with an empty list** (the closest real analogue): 0-byte file → `rc 1`,
`data/x.csv has no usable rows` on stderr, output file untouched. Header-only file →
identical. Both correct, both verified. Note what is *not* caught: a file of 500,000
fully-blank rows sails through as "usable", gets re-scored, and is published as
500,000 rows with `lead_score=0` and a `Full digital launch` pitch each.

**A dependency returns garbage:** covered by the `website_live` S1 and the
locale/sentinel-numerics S2. Neither is detected, both are counted as legitimate changes, and both
survive `leadminer validate`.

**Network drops halfway:** not applicable — and correctly so. `cmd_score` makes no
network calls, which is the command's one genuinely good property. The dependency
is *import-time* rather than call-time, though: `cli.py:249` imports `main`, which
imports `enricher` (`main.py:30`), which does `import requests` (`enricher.py:2`).
In a checkout without `requests` installed, `leadminer score` cannot start at all
with `ModuleNotFoundError`, while `stats` and `validate` — also documented as
network-free (`cli.py:14`) — work fine. I had to stub `requests` to run any of the
experiments in this report, exactly as `tests/test_lead_signal.py:23-63` does.

---

## Ranked: every silent failure, by how long a human takes to notice

| # | Silent failure | Exit | First signal | Notice in |
|---|---|---|---|---|
| 1 | **Overlap splice** — two writers share `main.py:122`; master published as field-level hybrids of two datasets (*S1 — overlap splice*) | 0 / 1 | nothing; `validate` passes the torn rows | weeks–never. A rep reads a corrupted row |
| 2 | **Four product CSVs left stale** — `sales_ready.csv` keeps pre-change scores and pitches forever (*S2 — only one of five files refreshed*) | 0 | nothing | weeks. The rep works the old list |
| 3 | **`changed` lies** — a pitch-rules rewrite reports `0 changed` (*S1 — `changed` counts only `lead_score`*) | 0 | nothing | weeks. Wrong pitch in an outbound message |
| 4 | **Header mismatch** — master replaced by 4-of-22-populated rows (*S1 — no input contract check*) | 0 | nothing in CI | days. A rep opens a nameless row |
| 5 | **`country` → `LB`** — permanent mislabel, later cross-country dedup merge (*S2 — country defaults to LB*) | 0 | nothing | weeks. A rep calls a Lebanese number for a Riyadh shop |
| 6 | **`website_live` `no` → `None`** — every rebuild pitch deleted (*S1 — `website_live` reinterpreted*) | 0 | the `changed` count, unexplained | days. Someone notices the rebuild list shrank |
| 7 | **Locale numerics nulled** — +10 rating credit gone, whole DB re-pitched `SEO audit` (*S2 — locale and sentinel garbage*) | 0 | the `changed` count, unexplained | days, only if someone diffs the score distribution |
| 8 | **SIGTERM** — empty log, no `interrupted`, temp file orphaned (*S2 — interruption handling*) | 143 | the runner's red X | minutes, but only if the cron is *watched* |
| 9 | **`completeness_score` / `region` frozen** (*S2 — `completeness_score` never recomputed*) | 0 | nothing | months. `qualified_businesses.csv` is quietly wrong |
| 10 | **No progress output** (*S2 — interruption handling*, item 3) | — | nothing | minutes, only while watching |

---

## Not a bug, but worth knowing

- **The evaluation order at `cli.py:266-269` is correct and is the fix for a real
  earlier bug.** `industry_priority` is assigned (267) *before*
  `recommend_service` (268) and *before* `lead_score` (269), so the +15/+8 priority
  credit (`enricher.py:306-310`) is applied. `main.py` has to comment its way around
  the same problem at `main.py:195-197` ("enrich() computed lead_score before
  industry_priority was set, so recompute here"). `cmd_score` gets it right by
  construction. Do not "simplify" this loop by reordering it.
- **It is idempotent.** Running `score` twice over its own output produces a
  byte-identical file (`pass1 == pass2: True`) and reports `0 changed` on the second
  pass. A cron can safely retry it.
- **Skipping `normalize_phone` causes no score divergence**, which surprised me and
  is worth recording so nobody "fixes" it: `normalize_phone` returns `""` only for
  fewer than 7 digits (`dedup.py:45-46`), and `lead_score` already requires ≥7 digits
  (`enricher.py:292`), so the phone column cannot disagree with the pipeline's.
  Verified: `normalize_phone` and `lead_score` agree on `("0501234567","LB")` and on
  every junk input in `tests/test_lead_signal.py:106`.
- **Ctrl-C is genuinely safe** — see item 1 of the interruption finding. The atomic-write work at
  `main.py:121-133` plus the handler at `cli.py:316-318` does its job for the one
  signal Python actually gets.
- **`cmd_score` has no test.** `tests/test_lead_signal.py:191-264` (`TestCli`) covers
  `cmd_stats` and `cmd_validate` only; `087-SYNTH-test-gap.md:19` already lists
  `cmd_score` as uncovered. Every finding above is reproducible in about fifteen
  lines of fixture CSV, which is exactly the kind of test that should exist.

## Recommended order of work

1. **Make the temp name unique and lock the data directory** (`main.py:122`,
   `cli.py:275`) — one line each, closes the only finding that silently corrupts the
   master. Until then, treat `leadminer score` as strictly single-writer.
2. **Count all four rewritten columns, not just `lead_score`** (`cli.py:270-271`) and
   print the per-column diff — without this the command's only output is
   untrustworthy.
3. **Check the input header against `main.FIELDS` before the loop** and refuse to
   write in place on a mismatch (`cli.py:259`); then add `--dry-run` so an operator
   can see the diff before the master is touched.
4. **Warn on lossy coercions** — non-`True`/`False` `website_live`, unparseable
   `rating`/`review_count`/`lat`/`lon` — and make them visible in the output line
   instead of counting them as changes.
5. **Re-slice all five CSVs (and recompute `completeness_score`, and `region` when
   blank) inside `cmd_score`**, or ship the `export` subcommand from
   `037-cli-design.md:352-380` and call it at the end of `score`.
6. **Stop rewriting `country` inside `cmd_score`**, or fix the LB default in
   `main.resolve_country` to consult coordinates before defaulting.
7. **Progress on stderr every 10k rows + a SIGTERM handler**, so a killed run always
   reports how far it got.