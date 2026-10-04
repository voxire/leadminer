# 200 — Concurrency review of `build_parser` (`cli.py:280`)

## Verdict

`build_parser()` is **safe**. It is a pure function that allocates every object it
returns on each call, touches no module-level mutable state, performs no I/O,
creates no thread and opens no socket. Measured: 64 threads calling it
concurrently produced one identical structural signature, zero exceptions, and
zero new threads. The 40-thread pool in `check_websites`, the 5-worker pool in
`GooglePlacesScraper` and the 3-scraper pool in `main()` are all constructed
*after* argument parsing, inside the command functions, and none of them is
reachable from `build_parser`.

The real risk in this file is not thread-level. It is (a) the exit code the
flagship `run` command hands to a scheduler, and (b) two *processes* writing the
same CSV — which I reproduced as silent loss of 4000 leads with both processes
reporting success.

---

## Findings

### S1 — `leadminer run` exits 0 even when every scraper failed, and can write an empty master

- **Where:** `cli.py:33-37` (`cmd_run`), `cli.py:313-315` (`main`), `main.py:148`
- **Breaks:** `cmd_run` returns `pipeline.main()`, and `main.main()` is declared
  `-> None` (`main.py:148`) and contains **no `return` statement at all** — it
  falls off the end. `cli.main()` then coerces it: `int(args.func(args) or 0)`
  (`cli.py:315`), and `int(None or 0) == 0`. So `leadminer run` reports success
  unconditionally. Every per-scraper failure is swallowed by the
  `except Exception: print(...)` at `main.py:167-168`, and each scraper
  additionally degrades to an empty result rather than an error:
  `google_places.py:169-171` ("API key not set, skipping" → `return`),
  `osm.py:47-56` (Overpass failure → `return`), `wikidata.py:79-85` (→ `return`).
  On a machine with no existing `data/all_businesses.csv`, `master` is `[]`
  (`main.py:150`, `main.py:77-80`), `records == []`, and the pipeline writes a
  header-only `data/all_businesses.csv` plus four empty exports
  (`main.py:209-213`) and exits 0. A scheduler or CI step reading that exit
  code sees green over an empty product.
- **Trigger:** `leadminer run` with `GOOGLE_PLACES_API_KEY` unset and Overpass
  down, in a clean checkout. Expected: a non-zero exit and an error. Actual:
  exit 0, five near-empty CSVs, `Total raw records from scrapers: 0`.
- **Mitigation that exists today, and its limit:** the Actions job still calls
  `python main.py` (`scrape.yml:69`), not `leadminer run`, and it has a
  `ROWS -lt 100` guard (`scrape.yml:78-83`) plus a master-downloaded check
  (`scrape.yml:57-63`). So CI is protected by *not using the CLI*. The CLI is
  the documented entry point (`pyproject.toml:33`, `cli.py:33-37`) and is what
  humans and agents type; nothing protects it.
- **Fix:** make `main.main()` return `int` and have `cmd_run` propagate it —
  non-zero when zero scrapers returned records or when `records` is empty.

### S2 — Deterministic temp filename: two concurrent writers corrupt the master, or kill each other

- **Where:** `main.py:122` (`tmp = path.with_suffix(path.suffix + ".tmp")`),
  `main.py:124-130`, `main.py:131-133`. Reached from `cli.py:274-275`
  (`out = pathlib.Path(args.out) if args.out else path` → **in place by
  default**, target `data/all_businesses.csv` at `cli.py:305`) and from
  `main.py:209`.
- **Breaks:** the temp name is derived purely from the target path — no PID, no
  nonce, no random suffix. Verified: `data/all_businesses.csv` →
  `data/all_businesses.csv.tmp`, and the same for all four other exports. Two
  processes therefore `open()` **the same file** in mode `"w"` (`main.py:124`).
  I ran `main.write_csv` verbatim (AST-extracted from `main.py`, no network)
  from two forked processes and got three distinct failures:

  1. **Torn CSV — 5/5 trials.** Two writers, 4000 records each. Result: a
     4223-row file mixing both writers' rows —
     `mix={'RUN': 4000, '': 1, 'SCORE': 222}`, `intact=False`. Neither process
     ever wrote 4223 rows. This file is *syntactically valid* and passes every
     check in `cmd_validate` (`cli.py:135-201`): names are non-blank, and the
     `(name, phone)` duplicate test at `cli.py:190` does not fire because the
     interleaved rows are distinct records. The corruption is invisible to the
     project's own quality gate.
  2. **A run dies.** `os.replace` raised
     `FileNotFoundError: .../all_businesses.csv.tmp` from `main.py:130`,
     because the other process's `except BaseException: tmp.unlink(missing_ok=True)`
     (`main.py:131-132`) deleted the shared temp out from under it. Observed
     exit codes `[1, 0]` — one process tracebacks with a bare `FileNotFoundError`.
  3. **Lost update.** `cmd_score` is read-modify-write on the same file: read at
     `cli.py:259`, written back at `cli.py:275`. With `run` landing its write in
     between, the master went **500 → 4500 → 500 rows**. 4000 freshly scraped
     leads gone, exit codes `[0, 0]` — both commands reported success.
- **Trigger:** `leadminer run &` then `leadminer score` (no `--out`) in the same
  working tree; or two agents touching `data/` in parallel. The Actions
  `concurrency: {group: leadminer-scrape}` block (`scrape.yml:15-17`) serialises
  *CI* runs, which is why this has not burned anyone yet — it is exactly the
  unprotected human/local path.
- **Severity defence:** S2 rather than S1 because it needs two concurrent
  processes, CI already serialises the scheduled path, and the fix is two lines.
  It becomes S1 the moment `leadminer run` is wired into CI (the workflow's
  `python main.py` at `scrape.yml:69` is a single-line change) or anyone
  parallelises `leadminer` invocations.
- **Fix:** `tmp = path.with_suffix(f".{os.getpid()}.tmp")`, and hold an exclusive
  `fcntl.flock` on `data/.lock` across the whole `load_master` → `write_csv`
  sequence so read-modify-write is atomic, not just the final rename.

### S3 — `check_websites` is correctly built but silently depends on `dedup` never aliasing a record

- **Where:** `enricher.py:231` (`targets` snapshots `(idx, url)`),
  `enricher.py:238` (submits the **URL, not the record**),
  `enricher.py:240-259` (main thread writes `records[idx]`). The invariant comes
  from `dedup.py:210` and `dedup.py:218` (`phone_index[key] = dict(record)`) and
  `dedup.py:191` (`_merge` returns a fresh dict).
- **Breaks today:** nothing. This is the right pattern — workers never hold a
  record dict, so there is no worker/main race over record mutation, and
  `enricher.py:249-259` runs single-threaded in the `as_completed` loop after the
  `with` block at `enricher.py:237` has joined.
- **The landmine:** `r = records[idx]` (`enricher.py:249`) silently assumes
  `records[i1] is not records[i2]`. `main.py:179-180` builds the list by
  concatenation and `dedup` copies; nothing re-checks downstream. If two indices
  ever aliased one dict, two futures would write one record and the last
  `as_completed` wins — one lead gets another lead's `email`, `instagram` and
  `website_live`, with no exception, no lock and no log line. Silent wrong
  results, which is the S1 class, from a two-character change in `dedup`.
- **Fix:** one cheap guard at the top of `check_websites`:
  `assert len({id(r) for r in records}) == len(records)`, or key results by `id(r)`.

### S3 — `--source` has two independent sources of truth

- **Where:** `cli.py:290` (`choices=["osm", "wikidata", "google_places"]`) vs
  `cli.py:51-55` (`registry` keys). Verified in sync today.
- **Breaks:** drift gives the same user error two different behaviours. Add a
  scraper to `registry` but not `choices` → argparse's exit-2 usage error, even
  though the CLI supports it. Remove it from `registry` but leave `choices` →
  `cli.py:56-59` prints `unknown source ... choose from [...]` and returns 2.
  `build_parser` is the declaration site and the one place worth fixing.
- **Fix:** hoist the registry to module scope and pass `choices=sorted(registry)`
  at `cli.py:289-290`.

### S3 — `scrape` imports all three scrapers before checking which one it needs

- **Where:** `cli.py:46-49`, all executed before the `args.source` dispatch at
  `cli.py:56`.
- **Breaks:** demonstrated on this checkout, where `requests` is not installed:

  ```
  scrapers.base          : OK
  scrapers.osm           : FAIL ModuleNotFoundError No module named 'requests'
  scrapers.wikidata      : FAIL ModuleNotFoundError No module named 'requests'
  scrapers.google_places : FAIL ModuleNotFoundError No module named 'requests'
  ```

  `leadminer scrape --source osm` dies with a raw traceback, because
  `scrapers/osm.py:4` → `httpclient.py:43` pulls in `requests`, and so does
  `cli.py:49` for the source you did *not* ask for. This is the exact failure
  `cmd_doctor` was written to prevent, and it handles it cleanly at
  `cli.py:227-231` ("doctor exists to diagnose a broken environment, so it must
  never be the thing that tracebacks") — `scrape` does not.
- **Fix:** move the three imports inside the `if args.source` branch. Also
  `cli.py:46`'s `BaseScraper  # noqa: F401 (import check)` is a smoke test
  written as a dead import; make it explicit or drop it.

---

## Not a bug, but worth knowing

- **`build_parser`'s own concurrency profile, measured.** `p1 is p2` → `False`;
  the subparser `choices` dicts are distinct objects; all six subparser objects
  differ by identity. 64 threads × `build_parser()` → 1 distinct structural
  signature `(name, prog, dests, defaults, choices)`, 0 errors.
  `threading.active_count()` is 1 before import, after import and after 64
  concurrent builds. Reason: `argparse` keeps no module-level mutable state —
  every `_ActionsContainer`, `_SubParsersAction` and `Namespace` is allocated per
  call — and `cli.py:20-24` imports only stdlib, so constructing the parser
  cannot start a thread, open a session or touch the filesystem. `DATA_DIR`
  (`cli.py:26`) is a `pathlib.Path`, which is immutable — assigning `.name`
  raises `AttributeError`. `build_parser` is called exactly once per process
  (`cli.py:313`, via `pyproject.toml:33`), so even the "one parser per process"
  question is moot.
- **Session reuse: three schemes, all correct.** Thread-local
  (`httpclient.py:58` `threading.local()`, `httpclient.py:61-71`), per-task
  (`google_places.py:189` `_make_session()` inside `fetch_query`), and
  per-pool-thread (`osm.py:35`, `wikidata.py:73` `get_session()`). No
  `requests.Session` is shared by two threads anywhere in the codebase.
- **Audit 004 is stale on the shared-session point.** `004-enricher-thread-safety.md:9-13`
  rates "shared session cookies make same-origin fetches order-dependent" **S2**,
  citing `enricher.py:124-125, 142-146` and a module-level `_SESSION`. That
  symbol no longer exists — `enricher.py:128` now imports `get_session`. Treat
  004's S2 #1 as resolved; its S2 #2 (unpinned urllib3 PoolManager eviction,
  `requirements.txt`) is still open and is not a `build_parser` concern.
- **`google_places.py:227` never closes `resp`.** Abandoned on the 401 path
  (`google_places.py:231-233`), the 429 `continue` (`google_places.py:247`) and
  `raise_for_status` (`google_places.py:249-252`). I checked whether this leaks
  sockets: `requests.Session.send` accesses `r.content` whenever `stream=False`,
  so the connection is released inside `post()` regardless of whether you later
  call `.json()` or `.close()`. Hygiene, not a leak — worst case is a few KB of
  already-read body held across `time.sleep(delay)` on the 429 path. Still
  worth `with session.post(...) as resp:`.
- **`google_places.py` locking is correct.** `seen_ids` is guarded by
  `seen_lock` around the check *and* the add (`google_places.py:263-266`) — the
  compound operation is inside the critical section, so no duplicate place ID can
  slip through. `all_records.extend(results)` is guarded by `records_lock`
  (`google_places.py:191-192`). `f.result()` at `google_places.py:197`
  re-raises, so a worker failure surfaces instead of vanishing.
- **Shared mutable state in the enrichment path is only stdout.** I tried to
  reproduce line tearing from 4-5 threads emitting `print(f"[Google] '{query}'
  page {page}: {len(places)} results")` (`google_places.py:257`) and could not on
  CPython — every line came back intact. Not claiming log corruption.
- **`enricher.py:328` mutates process-global state.** `urllib3.disable_warnings(...)`
  edits the global `warnings.filters` list. It runs on the main thread before the
  40-worker pool exists (`enricher.py:337`), so there is no race today. It is also
  vestigial: verification is `verify=True` (`enricher.py:175`), so the warning it
  suppresses can no longer fire.
- **No CLI knob for the 40 workers.** `enrich()` calls `check_websites(records)`
  with the default `workers=40` (`enricher.py:225`, `enricher.py:337`) and
  `build_parser` exposes nothing to change it. A local `leadminer run` therefore
  opens 40 concurrent connections to arbitrary small-business hosts with no way to
  dial it down. See `054-politeness-and-ssrf.md`.
- **Docs drift inside the parser's own help text.** `cli.py:14` says "Commands
  other than `run`/`scrape`/`doctor` never touch the network", but `doctor` does
  exactly that (`cli.py:233-243`, three live GETs). And the workflow still runs
  `python main.py` (`scrape.yml:69`), so `leadminer run` — the flagship command
  `cli.py:285-286` binds — is exercised only by humans.

---

## Recommended order of work

1. Return an exit code from `main.main()` and propagate it through `cmd_run`
   (`cli.py:37`) — this is the S1, and it is a four-line change.
2. PID-suffix the temp path at `main.py:122` and wrap
   `load_master` → `write_csv` in an exclusive lock; it converts three distinct
   failure modes (torn CSV, `FileNotFoundError` death, lost update) into none.
3. Add the `id()` identity assertion to `check_websites` (`enricher.py:231`) so a
   future `dedup` change cannot silently cross-contaminate leads.
4. Single source of truth for `--source` between `cli.py:290` and `cli.py:51-55`.
5. Make `cmd_scrape` import scrapers lazily (`cli.py:46-49`) so a broken optional
   dependency produces `doctor`-style output rather than a traceback.
6. Add `--workers` to the `run` subparser and pass it through to `check_websites`.
