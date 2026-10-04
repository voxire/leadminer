# 200 — `cmd_score` contract: declared types vs. actual shapes

**Target:** `cli.py:247-277` · **Read alongside:** `scrapers/base.py:5-28`, `main.py:33-134`, `main.py:183-197`

## Verdict

`cmd_score` is the **only** subcommand that round-trips records through `load_master` → mutate → `write_csv`. It is therefore the only place where the *declared* `BusinessRecord` shape and the *actual* runtime shape are ever allowed to meet — and they do not meet. `load_master` is annotated `-> list[dict]` (`main.py:77`) and in practice yields dictionaries where four fields the TypedDict declares **non-optional** are `None` (`lead_score`, `completeness_score`, `source`, `scraped_at`), where `rating` can be `nan`/`inf`, and where extra keys — including a non-string `None` key holding a `list[str]` — ride along untouched. `cmd_score` writes its own four fields correctly, but it **persists every one of those inherited violations back into the master, in place by default** (`cli.py:274`). So the tool you reach for to repair the dataset is the tool that makes the shape drift permanent. Two of its defects silently rewrite the lead signal itself: a lossy boolean re-parse of `website_live` (`main.py:103`), and an unguarded schema-narrowing rewrite (`main.py:125`).

**Nothing below is theoretical.** Every "Breaks"/"Trigger" was reproduced against the real `cli.cmd_score`, `main.load_master`, `main.write_csv` with `requests`/`urllib3` stubbed out (no network).

---

## Findings

### S1 — `website_live` re-parse accepts exactly two spellings; everything else silently becomes "unknown", and `cmd_score` bakes the result into the master

- **Where:** `main.py:103` (the parse), `enricher.py:300-304` (the scoring consequence), `pitch_recommender.py:34-36` (the pitch consequence), `cli.py:269` + `cli.py:274` (re-score and persist, in place).
- **Contract:** `BusinessRecord.website_live` is `bool | None` (`base.py:16`) — a genuine tri-state, and `enricher.py:145-156` goes out of its way to keep the three states distinct ("conflating them was the single worst bug in the pipeline"). But `main.py:103` is the only thing that turns that tri-state back into a value on reload, and it recognises **two literals out of the many a CSV can legitimately contain**:

  ```python
  row["website_live"] = True if wl == "True" else (False if wl == "False" else None)
  ```

- **Breaks:** Every other spelling — `TRUE`, `FALSE`, `true`, `false`, `1`, `0`, `yes`, `Y`, `tRue` — falls into the `else None` branch, i.e. **`UNKNOWN`**. `UNKNOWN` is not a neutral value downstream; it is the *worst* value, because in `enricher.py:300-304` it scores **zero points**, less than either state it replaced:

  ```python
  live = record.get("website_live")
  if live is True:      score += 10
  elif record.get("website") and live is False: score += 20
  # None -> nothing. Both branches miss.
  ```

  And in `pitch_recommender.py:34-36` it is not even a tie-break — it changes the *tier*:

  ```python
  website_live  = record.get("website_live") is True
  website_dead  = record.get("website_live") is False
  ```

- **Trigger (verified, one row, full in-place `leadminer score`):** a master row whose `website_live` cell is `False` vs. `FALSE`, identical in every other column (`category=clinic`, `rating=3.0`, `phone=""`, `industry_priority=high`):

  | cell in | `website_live` after `load_master` | `lead_score` after `cmd_score` | `recommended_service` after `cmd_score` |
  |---|---|---|---|
  | `False` | `False` | **45** | **`Website rebuild + maintenance`** |
  | `FALSE` | `None`   | **25** | `Lead-gen overhaul (landing pages + Google Ads + WhatsApp capture)` |

  rc=0 both times. Output line: `re-scored 1 records, 1 changed`. The pitch moves off "the strongest offer in the database" (`pitch_recommender.py:55-58`) onto a generic tier-5 fallback, and 20 lead-score points evaporate, purely because of the **casing of one cell**. Nothing in stdout, stderr, or the exit code says the boolean was misread.

  The likely producer is not hypothetical: the master is written with `utf-8-sig` specifically so "Excel and Google Sheets" render it (`main.py:118-119`), it is uploaded to Google Drive by rclone (`main.py:8-12`, `.github/workflows/scrape.yml`), and it is the file the sales team reads. Excel and Google Sheets both export boolean cells as `TRUE`/`FALSE`, not `True`/`False`. One save by one human rewrites the column, and the next `leadminer score` permanently converts **every live and dead verdict in the dataset to "unreachable"** — which `enricher.py:296-299` explicitly says is *not* a pitch signal. The rebuild-pitch queue empties and nothing reports it.

- **Fix:** Stop round-tripping a tri-state through two string literals. Either persist `enricher.LIVE`/`DEAD`/`UNKNOWN` (`enricher.py:154-156`) as the cell contents and parse those three exactly, or parse case-insensitively (`wl.strip().lower()` against `{"true","1","yes"}` / `{"false","0","no"}`) **and** count collapses — e.g. `n_collapsed = sum(1 for r in records if raw_cell not in {"True","False",""})` — and refuse to write, or at minimum print, when it is non-zero.

---

### S1 — The in-place rewrite is schema-narrowing and schema-unaware: it silently deletes every column not in `main.FIELDS`, and will silently overwrite any export `--out` names

- **Where:** `cli.py:274` (`out = ... if args.out else path` — defaults to overwriting the input), `main.py:125` (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`), `main.py:209-213` (the five products that share this schema).
- **Breaks:** `write_csv` projects every record through a hard-coded 23-name list and discards everything else **without a warning, without a count, and with exit code 0**. `cmd_score` inherits that projection but adds the in-place default. Two consequences:

  1. **Columns are destroyed.** Any provenance, run-id, cost, or operator column present in the master is gone after one `leadminer score`. This is not hypothetical: audits 050 (provenance/idempotency), 066 (typing at scale) and 042 (exports design) all recommend adding exactly those columns to the master, and this command is what an operator would run first when they wanted to try it.
  2. **Files are clobbered.** `--out` accepts any path with no check against the five canonical exports and no check that it differs from the other four subsets. `leadminer score data/all_businesses.csv --out data/without_websites.csv` replaces a curated 2-row subset with all 7 rows of the master, exit 0, no warning.

- **Trigger (verified, both variants):**
  ```
  header before: ...,source,scraped_at,scraped_from_run_id,operator_notes
  header after : ...,source,scraped_at
  rc=0   'scraped_from_run_id' survived? False

  # and:
  without_websites.csv (2 rows: NoSite A, NoSite B)
  -> after `score all_businesses.csv --out without_websites.csv`:
     without_websites.csv has 7 rows: Row0..Row6, rc=0
  ```
- **Fix:** (a) Before writing, diff `set(record.keys())` against `set(FIELDS)` and `sys.exit` with the list of keys that would be dropped unless `--allow-schema-narrowing` is passed; (b) refuse `--out` equal to any of `all_/qualified_/with_websites_/without_websites_/sales_ready` .csv unless it is the file being read; (c) make `--out` mandatory for `score`, or require `--in-place` to be explicit.

---

### S2 — `cmd_score` is not score-equivalent to `leadminer run`: it omits `normalize_phone`, so it persists `lead_score` values the pipeline would never produce

- **Where:** `cli.py:265-272` vs. `main.py:189-197`. The pipeline's scoring loop is:
  ```python
  # main.py:190-197
  r["industry_priority"] = industry_priority(r.get("category"))
  r["recommended_service"] = recommend_service(r)
  raw_phone = r.get("phone")
  if raw_phone:
      r["phone"] = normalize_phone(raw_phone, r.get("country") or _DEFAULT_COUNTRY)   # <-- omitted by cmd_score
  r["lead_score"] = _lead_score(r)
  ```
  `cli.py:266-272` performs the same four steps **in the correct order** but without step 3. Ordering is right; normalisation is missing.
- **Breaks:** `cmd_score`'s own docstring (`cli.py:248`) is "Re-score an existing CSV with **current rules**". It does not apply them. `lead_score` counts contact points on the *raw* phone digits (`enricher.py:285, 292`: `len(re.sub(r"\D","",phone)) >= 7`), while the pipeline scores the *normalised* phone — and `normalize_phone` can collapse a 7+ digit string down to 3 digits (`dedup.py:50-51` strips the `00` international prefix; `dedup.py:63` does `.lstrip("0")`). So a phone that earns +15 through `cmd_score` earns +0 through `main.py`. The divergence is then **written into the master in place**, so `lead_score` and the `sales_ready.csv` ordering derived from it are wrong until the next full run silently corrects them — with no record that a correction happened.
- **Trigger (verified, `leadminer score` loop vs. `main.py` loop on the same CSV, one row, `industry_priority=high`):**

  | `phone` cell | `cmd_score` → `lead_score` | `leadminer run` → `lead_score` | `main.py` normalised phone |
  |---|---|---|---|
  | `0000000`      | **30** | **15** | `+961` |
  | `0000000000`   | **30** | **15** | `+961` |

  (+15 points, deterministic.) The trigger class is narrow — a phone of 7+ digits that normalizes to fewer than 7 — and in practice it comes from **legacy master rows written before commit `5222d32` normalised phones**, which is exactly the population `leadminer score` exists to repair. Two honest caveats: nine other probe values I tested (`00961000000`, `0123456789`, `0001234567`, `0501234567`, `+961`, `00`, …) agreed exactly, and `cmd_score` **is** idempotent across repeated in-place passes (verified: 4 passes, byte-identical output after pass 0). So this is a bounded, one-directional divergence, not general chaos — hence S2, not S1.
- **Fix:** Reuse the pipeline loop instead of re-implementing it — move `main.py:183-197` into one `rescore(records) -> list[dict]` in `main.py` and have both `main()` and `cmd_score` call it. Two copies of a scoring loop will drift; that is the actual root cause here.

---

### S2 — `cmd_score` re-derives the core lead signal from an arbitrarily old probe, with no freshness signal anywhere in its output

- **Where:** `cli.py:269` (`lead_score(r)` over whatever `website_live` the CSV holds), `cli.py:14-15` ("commands other than `run`/`scrape`/`doctor` never touch the network, so they are safe to run anywhere"), `enricher.py:296-304`.
- **Breaks:** `website_live` is the single largest term in `lead_score` (10 points live, 20 dead, 0 unknown) and it is a **derived, time-bound** field — `enricher.py:148-153` is explicit that a DEAD verdict means "the server answered 4xx/5xx". `cmd_score` cannot refresh it (no network by design) and does not say so. So a master last scraped in January gets its dead/live verdicts re-asserted in October and baked in, while every site Voxire has since rebuilt is still scored `+20 "server-confirmed dead site = rebuild pitch"`. The operator sees `re-scored 12,043 records, 87 changed` and reasonably concludes "the rules changed, the data is fine". The 87 is not the drift that matters; the 12,043 stale verdicts are, and they are invisible.
- **Trigger:** `scraped_at` (which *is* in every row) is never read by `cmd_score`. A master whose newest `scraped_at` is `2026-01-15` re-scored on `2026-10-03` produces byte-identical `website_live`-derived scores with no warning, no diff, and rc=0.
- **Fix:** Compute `age = now - max(r["scraped_at"])` across the input; if it exceeds a threshold (say 14 days), require `--allow-stale-probes` or at minimum print `WARNING: website_liveness is up to N days old; lead_score reflects a stale probe`. Do not silently overwrite a stale verdict with a freshly-computed number over it — that is the worst of both worlds: the number looks new, the evidence is old.

---

### S2 — `cmd_score` refreshes one file, so the five products disagree after a re-score; and its return value is an exit code that a crash duplicates

These are two separate defects on the same "shape of the contract" theme.

**(a) One of five files is rewritten.**

- **Where:** `cli.py:275` (`pipeline.write_csv(out, records)` — exactly one path), `main.py:209-213` (five exports), `main.py:202-205` (`sales_ready` membership is a function of `industry_priority` and contacts, both of which `cmd_score` rewrites).
- **Breaks:** `leadminer score` is advertised as applying current rules. It applies them to the file you name and to no other. `sales_ready.csv` membership depends on `industry_priority in ("high","medium")` (`main.py:204`), and `industry_priority` is recomputed at `cli.py:267`. So after a re-score, `all_businesses.csv` can say a lead is `high` priority with a contact channel while `sales_ready.csv` still omits it.
- **Trigger (verified):** a 4-row master with `category=tax_advisor`, `industry_priority=low` in the file, and an empty `sales_ready.csv`:
  ```
  after: master industry_priority='high'  lead_score=80
         recommended_service='Website rebuild + maintenance'
         main.has_any_contact(record) = True
         sales_ready.csv rows=0   <-- untouched
  rc=0; nothing said about the four exports it did not write.
  ```
- **Fix:** either have `score` re-derive and rewrite all five exports from the rescored master (the useful behaviour, and the reason someone runs this command after a rules change), or print an explicit list of the exports that are now stale.

**(b) `1` means both "no usable rows" and "crashed".**

- **Where:** `cli.py:262` returns `1` for "no usable rows"; `cli.py:314-318` catches **only** `KeyboardInterrupt`. `cli.py:315` is `return int(args.func(args) or 0)`.
- **Breaks:** Any exception raised inside the loop escapes `main()` and produces a raw traceback, whose process exit code is **also 1**. In CI or in the Actions job, "the file had no rows" and "`recommend_service` blew up on row 4,000" are indistinguishable. The module docstring (`cli.py:14-15`) promises these are the commands "you reach for when a run looks wrong" — precisely the moment a clean, unambiguous answer matters most.
- **Trigger (verified):**
  ```
  cmd_score(Namespace(file=...))        -> AttributeError: 'Namespace' object has no attribute 'out'
                                            raised at cli.py:274, AFTER the loop did all the work
  leadminer score data/  (--out a dir)   -> IsADirectoryError: ... 'adir.tmp' -> 'adir'
                                            uncaught -> traceback, exit 1
  ```
- **Fix:** wrap `args.func(args)` in `except Exception` in `main()` and return a distinct code (e.g. `70`) with a one-line `type(e).__name__: e` on stderr; reserve `1` for "data problem found". Separately, assert `getattr(args, "out", None)` is not an unset attribute *before* doing any work, or change the parameter to `args.out` via `parser.get_default`.

---

### S3 — Four fields the TypedDict declares non-optional come back out of `cmd_score` blank, and `completeness_score` is never recomputed

- **Where:** `base.py:25-28` (`lead_score: int`, `source: str`, `scraped_at: str`, `completeness_score: int`), `main.py:87-89` (blank → `None`), `main.py:96-101` (unparseable int → `None`), `cli.py:265-272` (only `lead_score` is repaired).
- **Breaks:** `BusinessRecord` promises four non-optional fields. `load_master` guarantees none of them. Verified, straight out of `load_master` on an all-blank row:
  ```
  lead_score         = None      # declared int
  completeness_score = None      # declared int
  source             = None      # declared str
  scraped_at         = None      # declared str
  ```
  `cmd_score` fixes `lead_score` (→ `0`) and leaves the other three, so the file it writes still violates the declared contract. Two concrete downstream costs:
  - `completeness_score` is never recomputed even though `enricher.completeness_score` is a pure function of fields already in the row (verified: a row whose cell is blank loads as `None`, is written back blank, and `enricher.completeness_score(row)` would have returned `4`). `qualified_businesses.csv` is defined as `completeness_score >= 1` (`BRIEF.md:52`), so the one command whose job is "make the master consistent with current rules" leaves the qualification key inconsistent.
  - A blank `source` is not cosmetic: `_trust_for` (`dedup.py:148-152`) falls back to `_DEFAULT_TRUST = 1` for **every** field of a record with no source, so that record permanently loses survivorship in all future merges. The multi-source bonus at `enricher.py:316` is lost too.
- **Trigger:** the all-blank row above, `leadminer score` in place → `completeness_score=''`, `source=''`, `scraped_at=''`, rc=0.
- **Not a live crash, but a loaded one:** `main.py:206` does `r.get("completeness_score", 0) >= 1`, and `.get(k, default)` returns `None` when the key is **present** with value `None` — verified: `TypeError: '>=' not supported between instances of 'NoneType' and 'int'`. It does not fire today only because `enricher.enrich` overwrites the field at `enricher.py:349` before `main.py:206` is reached. `cmd_score` is the process that keeps that null in the master in the first place; removing the `enrich()` call, or adding a `write_csv`-without-`enrich` path, detonates it.
- **Fix:** make the four declared-non-optional fields honestly optional in `BusinessRecord` (`int | None` / `str | None`) *and* have `cmd_score` fill `completeness_score` from `enricher.completeness_score`; or keep them non-optional and make `load_master` default them (`completeness_score` → 0, `source` → `"unknown"`, `scraped_at` → epoch). Pick one; today the declaration is the thing that is wrong.

---

### S3 — `changed` counts only `lead_score`, and a blank `lead_score` cell makes every row report as changed

- **Where:** `cli.py:269-271`, `cli.py:276`.
- **Breaks (two ways, both verified):**
  1. `cli.py:266-268` rewrite `country`, `industry_priority`, and `recommended_service` and none of them is counted. Verified: a row went `recommended_service: 'OLD PITCH'` → `'RTYLR commerce OS (POS, online ordering, CRM)'` and `industry_priority` was rewritten, while the only number reported was `changed = 1` — which reflected the `lead_score` delta `0 → 60`. A run that changes 100% of the pitch column can print `0 changed`.
  2. `cli.py:270` uses `r.get("lead_score")`. When the cell was blank, `load_master` made it `None` (`main.py:96-101`), so `new != None` is always true. Verified: a 1000-row master with an empty `lead_score` column → `re-scored 1,000 records, 1,000 changed`, i.e. the command's headline number is a constant.
- **Fix:** diff each rewritten field against its pre-loop value and report a per-field count; use `"lead_score" in r` to distinguish "absent" from "changed".

---

### S3 — `rating` accepts `nan`/`inf`: the value satisfies `float | None` but is outside the declared 0–5 domain, and `cmd_score` bakes the consequence in

- **Where:** `main.py:90-95` (`float()` accepts `nan`, `inf`, `-inf`), `base.py:21` (`rating: float | None`), `enricher.py:312-314` (`rating is not None and rating < 4.0`), `pitch_recommender.py:73` + `_is_low_rating` (`pitch_recommender.py:135-140`), `dedup.py:134-139`.
- **Breaks:** `float("nan")` is a `float`, so the declared type is satisfied while every downstream range check silently misbehaves. Verified, full in-place `leadminer score`:

  | `rating` cell | after `load_master` | `lead_score` | `recommended_service` |
  |---|---|---|---|
  | `nan`  | `nan`  | 60 | `RTYLR commerce OS …` (no reputation signal) |
  | `inf`  | `inf`  | 60 | `RTYLR commerce OS …` |
  | `-inf` | `-inf` | **70** | **`Reputation + SEO turnaround`** |
  | `4.5` (control) | `4.5` | 60 | `RTYLR commerce OS …` |

  A `-inf` rating is *indistinguishable from a 1-star review* to the scorer and flips the pitch; `nan` silently suppresses the reputation signal that `enricher.py:311` calls "a pain point worth pitching". In dedup, `_validity("rating", ...)` computes `0 <= v <= 5`, which is `False` for all three, so a poisoned rating gives that record validity `0` and it loses the field in **every future merge, forever**. `cmd_validate` would catch it (`cli.py:185-186` checks `0 <= v <= 5`) but `cmd_score` does not call `cmd_validate` — see below.
- **Trigger:** any of the three literal cells above in the `rating` column.
- **Fix:** `math.isfinite(v)` in the `load_master` float cast (`main.py:92-95`), plus the same guard in `cmd_validate` — `float("nan")` currently passes its `float()` try block at `cli.py:181-183`.

---

## Not a bug, but worth knowing

- **`cmd_score` writes nothing outside `BusinessRecord`.** Answering the brief's question directly: the function writes exactly four record fields — `country` (`cli.py:266`), `industry_priority` (`cli.py:267`), `recommended_service` (`cli.py:268`), `lead_score` (`cli.py:272`) — and **all four are declared in `base.py:5-28`** (lines 23, 24, 25, 10). Verified programmatically. It reads `category`, `country`, `phone`, `lead_score` directly, plus, transitively through `recommend_service` and `lead_score`: `website`, `website_live`, `email`, `phone`, `instagram`, `facebook`, `rating`, `review_count`, `completeness_score`, `category`, `industry_priority`, `source`. Also all in the TypedDict. The only non-TypedDict attributes `cmd_score` touches are `args.file` and `args.out`, which are `argparse.Namespace` fields, not record fields.
- **…but it *carries* undeclared keys into `write_csv`.** `load_master` returns whatever `csv.DictReader` produced, and verified: a row with one extra column yields an extra string key (`['extra1','extra2']`), and a row with **more fields than the header** yields the DictReader restkey — the non-string key `None` mapped to a `list[str]` (`{'name','category','lead_score', None: ['GARBAGE']}`). That key is not in `BusinessRecord`, has type `list` where every other value is `str | None`, and survives `dedup._merge` (`dedup.py:190-194` iterates `set(a) | set(b)`). It is harmless today only because `csv.DictWriter(..., extrasaction="ignore")` (`main.py:125`) happens never to look it up. Any future `BusinessRecord(**record)`, dataclass conversion, or `for k, v in record.items(): v.strip()` breaks on it.
- **`cmd_score` is strictly more permissive than `cmd_validate`, which only reads.** `cmd_validate` rejects a file where "every row has a blank name" (`cli.py:151-152`), where >1% of coordinates fall outside both markets (`cli.py:164-173`), where any rating is outside 0–5 (`cli.py:175-188`), and where >5% of `(name, phone)` pairs duplicate. `cmd_score` runs none of those and rewrites the file in place regardless. Verified: a 2-row file with blank names, `lat=99.9 lon=-140.0` and `rating=42` → `rc=0`, both rows re-scored and written back, message `re-scored 2 records, 2 changed`. The tool that *writes* is less strict than the tool that only *looks*.
- **A header-only CSV is handled honestly.** `load_master` returns `[]` → `rc=1`, `"{path} has no usable rows"`, and the file is left untouched. Verified. Note the wording is slightly off: `load_master` rejects no row at all, so "no usable rows" can only ever mean "no data rows".
- **Row count is preserved on the happy path.** `write_csv` writes the same list object it was given, so `cmd_score` cannot silently drop rows; the only losses are columns (above) and semantic mis-scoring (above).
- **`os.replace` + `fsync` on the file, but no `fsync` on the directory** (`main.py:129-130`). The rename is atomic for concurrent readers, which is what the docstring at `main.py:110-119` promises, but it is not durable across a power loss: the directory entry may not survive. Low impact on a gitignored `data/` directory that is regenerated weekly.
- **`completeness` is a dead variable in `recommend_service`** (`pitch_recommender.py:48-52`; no reference after line 52 — only a comment at line 69). The tier-3 heuristic "no SEO signals … weak completeness" in the docstring at `pitch_recommender.py:68-69` has never run. Not `cmd_score`'s bug, but it means `cmd_score` recomputing `completeness_score` would have had no effect on the pitch anyway — which slightly lowers the urgency of the S3 above.
- **`BRIEF.md` undercounts the schema.** It says "BusinessRecord TypedDict (22 keys)" (`BRIEF.md:18`) and "The 22 columns" (`BRIEF.md:57`); both `BusinessRecord` and `main.FIELDS` actually have **23** (verified). `completeness_score` is the 23rd, and it is the one field declared non-optional that the pipeline routinely leaves null.
- **`cmd_run` shares the `int(... or 0)` sink and is worse.** `main.main()` is annotated `-> None` (`main.py:148`), so `cmd_run` (`cli.py:37`) always yields exit 0 — even when all three scrapers raised, because `main.py:167-168` swallows each exception and continues with an empty batch. Outside this lens's target, but it is the same return-shape hole and it means the CI job can never distinguish "scraped 40k businesses" from "all three APIs are down".

## Answering the brief's two direct questions

**Q: Every field `cmd_score` reads or writes that is not in the TypedDict?**

**Record fields: none.** Verified: the four written keys (`country`, `industry_priority`, `recommended_service`, `lead_score`) and every key read directly or transitively are all in `base.py:5-28`. The violations are all in the opposite direction — fields the TypedDict declares non-optional (`lead_score`, `completeness_score`, `source`, `scraped_at`) arriving as `None`, and `rating` arriving as `nan`/`inf`. Non-record attributes touched: `args.file` (`cli.py:254`) and `args.out` (`cli.py:274`), neither guaranteed to exist by the declared parameter type `argparse.Namespace` (verified: `AttributeError` at `cli.py:274` if it is absent — after the loop has already run). Undeclared keys *inherited* from `load_master` and carried into `write_csv`: any extra CSV column name, plus the `None` restkey holding a `list[str]`.

**Q: Every place the return value is used in a way that breaks on an unexpected shape?**

`cmd_score` returns `int` (`0` success / `1` no usable rows / `2` no such file). There are exactly two consumers, and both are shape-tolerant in the worst way:

| Site | Expression | Breaks when |
|---|---|---|
| `cli.py:315` | `return int(args.func(args) or 0)` | **Any falsy return becomes 0 = success.** Verified mapping: `0→0, 1→1, 2→2, None→0, False→0, True→1`. A future `return changed > 0` silently turns "everything changed" into exit 1 while `1` already means "no usable rows"; a `return None` on an early-return refactor becomes exit 0 on a partially-written file. `int()` on anything non-numeric (`SystemExit`, a `Decimal` is fine, a list is not) raises `TypeError` → traceback → exit 1. |
| `cli.py:322` | `raise SystemExit(main()))` | Exit code is the *only* machine-readable output. `1` is produced both by `cli.py:262` ("no usable rows") and by any uncaught exception escaping `cli.py:314-318`, which catches only `KeyboardInterrupt`. Verified `AttributeError` and `IsADirectoryError` both surface as tracebacks with process exit 1. |

There is no JSON/`--json` mode, no `--dry-run`, and no structured summary, so a caller cannot get the per-field change counts programmatically — only the single misleading integer at `cli.py:276`.

## Recommended order of work

1. **Fix `main.py:103` boolean parsing** (S1-1) and add a collapse counter that aborts the write. This is ~5 lines and it stops the worst silent corruption in the tool. Nothing else on this list matters until a human saving the file in Excel stops erasing the rebuild-pitch queue.
2. **Guard the write in `cmd_score`** (S1-2): refuse to write when `set(record) - set(FIELDS)` is non-empty, refuse `--out` pointing at one of the four exports other than the input, and require an explicit `--in-place` to overwrite the input. Also switch `write_csv` to `extrasaction="raise"` once the guard exists, so the invariant is enforced at the boundary too.
3. **Extract one `rescore(records)` in `main.py` and call it from both `main()` and `cmd_score`** (S2-1). This removes the duplicated loop, which is the root cause of the phone-normalisation divergence and of every future divergence.
4. **Report staleness in `cmd_score`** (S2-2) — read `max(scraped_at)` and warn/refuse when `website_live` is stale. Five lines; converts a silent wrong answer into a question.
5. **Reconcile the return-value contract** (S2-3): `except Exception` in `main()` returning a distinct code, per-field change counts instead of the single `changed` integer, and either rewrite all five exports or print which ones went stale.
6. **Make `BusinessRecord` honest** (S3): either relax the four non-optional fields to `| None`, or default them in `load_master`. Then add `math.isfinite` to the float cast and a `completeness_score` recompute to `cmd_score`.
7. **Add `tests/test_cli_score.py`** — the round trips in this report (TRUE/FALSE collapse, extra-column drop, `--out` clobber, phone divergence, `changed` inflation) are all ~10-line offline tests with no network, and `tests/` currently contains one file that never touches `cli.py`.