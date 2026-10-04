# 381 — `_fetch_website` is O(1) and correctly bounded, but the pipeline calls it O(master) times per run, and 80% of its CPU sits in the two regexes with no literal prefix

## Verdict

The complexity answer is clean: `_fetch_website` (`enricher.py:161-222`) is **O(1) in the number
of businesses** — it touches one URL, holds no state across calls, and allocates nothing that
survives the return. The full pass is therefore **O(n)**. It is not O(n²), and it does not
allocate proportionally.

But two things make it expensive at 500k anyway, and neither is a complexity problem.

1. **`re` does not release the GIL** (measured: 4 threads doing this function's regex block get
   **0.99×** — i.e. zero parallel speedup). So the tail block's CPU is *strictly serial* across
   the 40 workers and is **added** to the network-bound wall clock rather than hidden by it. At a
   200 KB page it is **33 ms/call**, which at 200k fetches is **110 minutes of single-threaded
   wall clock**.
2. **`_EMAIL_RE` (`enricher.py:130`) and `_WHATSAPP_RE` (`enricher.py:132-135`) are the only two
   of the five patterns with no anchorable literal prefix, and they are 80% of that CPU.** A
   one-line `html.lower()` + `in` pre-filter is a measured **1.67×–1.99×** with byte-identical
   output on all 48 permutations I tested.

The single largest speedup, though, is not inside the function: **memoize by URL / honour the
`website_live` already sitting in the master.** `enrich()` re-fetches every website in the
cumulative master on every run (`main.py:179` → `main.py:187` → `enricher.py:231`) with no cache,
while `load_master` carefully parses a `website_live` column (`main.py:102-103`) that
`enricher.py:250` then unconditionally overwrites. Growing the master to 500k over 33 weekly runs
costs **3,366,000 fetches (46.8 h cumulative) today versus 198,000 (2.8 h) memoized — 17.0×.**
Steady-state per-run cost drops **33×** (200,000 fetches → 6,000).

## Findings

### S2 — No memoization and no freshness check: the cumulative master is re-fetched in full on every run, making the pipeline's HTTP cost quadratic in the number of runs

- **Where:** `enricher.py:231` (`targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]`),
  reached via `enricher.py:337` (`records = check_websites(records)`) ← `main.py:187`
  (`records = enrich(records)`) ← `main.py:179` (`combined = raw_filtered + master`) ← `main.py:150`
  (`master = load_master(DATA_DIR / "all_businesses.csv")`).
- **Breaks:** the master is *cumulative by design* (`main.py:209` writes every record back to
  `all_businesses.csv`; the workflow runs weekly, `.github/workflows/scrape.yml:9`). Enrichment
  therefore receives `new + all_of_history`, and `_fetch_website` is invoked once per record with
  a website in that whole set. Nothing deduplicates the URLs, nothing skips a record whose verdict
  is already known, and `website_live` — which **is** loaded from the CSV at `main.py:102-103` and
  carried through dedup — is used only for reporting (`enricher.py:264-266`) and then blown away
  at `enricher.py:250`. (Note `dedup.py:114` lists both `website` and `website_live` in
  `_VOLATILE`, so the fresh scrape's URL wins on merge — meaning the URL set is stable run to run
  and memoizing on it is safe.)
- **Arithmetic.** With ~15,000 new records per run (68 Places queries × up to 60 results, plus
  OSM/Wikidata), 40% carrying a website = 6,000 new fetches per run, and run *k* fetching
  6,000·*k*:

  | | fetches | cumulative wall clock @ L=2 s, W=40 |
  |---|---|---|
  | today, to grow master to ~500k over 33 runs | **3,366,000** | **46.8 h** |
  | per-URL memoized | **198,000** | **2.8 h** |
  | | **17.0× less** | |

  Steady state, one run at a 500k master: **200,000 fetches → 6,000 (33×)**, i.e. 183 min → 15 min
  against the `timeout-minutes: 300` ceiling (`.github/workflows/scrape.yml:25`).
- **Trigger:** any master above ~50k records. There is no threshold — the waste is 100% of all
  pre-existing websites, on every run, forever.
- **Cost beyond wall clock:** 3.37M outbound requests to third-party SMB servers, repeatedly, for
  data already in hand. This is the politeness/infra-load axis audits 054 and 062 cover; the perf
  lens just wants the multiplier on the record: **17×**.
- **Fix:** memoize on the normalized URL for the duration of the run *and* across runs —
  `functools.lru_cache(maxsize=...)` on a small wrapper around `_fetch_website`, or better a
  SQLite `site_probe(host, url, checked_at, outcome, contacts)` table keyed by URL with a TTL
  (audit 053 already proposes a cache layer). Separately and immediately: skip any record where
  `r.get("website_live") is not None` and the URL is unchanged from the master row, unless
  `--force-refetch`. Long term, key the cache on the **registrable domain**
  (`urlparse(url).hostname` minus `www.`) — a chain with 300 branches on one corporate domain
  currently costs 300 fetches; audited as unmeasured because I am not permitted to scrape.

### S2 — 80% of the CPU is in the two patterns with no anchorable literal prefix; a `in html.lower()` pre-filter is a measured 1.67×–1.99× for four lines

- **Where:** `enricher.py:199-221` (the scan), with the patterns at `enricher.py:130`
  (`_EMAIL_RE`), `enricher.py:132-135` (`_WHATSAPP_RE`), `enricher.py:131` (`_INSTAGRAM_RE`),
  `enricher.py:136` (`_LINKEDIN_RE`) and the inline literal at `enricher.py:199`.
- **Mechanism, isolated on one 200 KB body with no match anywhere (best-of-60, GC off):**

  | pattern shape | ms | vs cheapest |
  |---|---|---|
  | single literal prefix `linkedin\.com/company/` (`enricher.py:136`) | 0.125 | 1.0× |
  | single literal prefix `href=["']mailto:` (`enricher.py:199`) | 0.114 | 1.1× |
  | single literal prefix `wa\.me/` | 0.115 | 1.1× |
  | **3-way literal BRANCH, no common prefix (`enricher.py:132-135`)** | **1.198** | **0.1× (10.4× slower)** |
  | **charset start, no prefix (`enricher.py:130`)** | **5.970** | **0.02× (48× slower)** |

  When a pattern begins with a literal, CPython's `sre` compiles a `memchr` fast path. When it
  begins with a charset (`_EMAIL_RE`) or with a `BRANCH` whose branches share no first character
  (`wa\.me/` / `whatsapp\.com/send?phone=` / `api\.whatsapp\.com/send?phone=` → `w`,`w`,`a`),
  there is no prefix, and the engine retries the whole pattern at **every one of the 204,800
  character positions**.
- **Measured share of `enricher.py:199-221` at the 200 KB cap** (page with one email, no IG/WA/LI;
  best-of-50, GC off): `_EMAIL_RE.findall` **41.1%** (6.163 ms), `_WHATSAPP_RE.search` **38.6%**
  (5.797 ms), `_LINKEDIN_RE.search` 10.7% (1.613 ms), `re.findall(mailto)` 9.6% (1.436 ms).
  `_INSTAGRAM_RE` is 0% when an `@` appears early and **23% of the total** (4.548 ms of 19.906 ms)
  on a page with no `@` at all before the footer — see S3 below.
- **Fix and measured result.** One `.lower()` (a single C pass, **0.014–0.139 ms** for 20–200 KB)
  gates the two expensive patterns:

  ```python
  low = html.lower()                                   # ~0.1 ms at 200 KB
  mailto_hits = re.findall(_MAILTO_SRC, html, re.IGNORECASE) if "mailto" in low else []
  for addr in (mailto_hits + _EMAIL_RE.findall(html)) if "@" in low else []:
      ...
  if "wa.me" in low or "whatsapp" in low:
      m = _WHATSAPP_RE.search(html)
  if "linkedin" in low:
      m = _LINKEDIN_RE.search(html)
  ```

  | page | current | pre-filtered | speedup |
  |---|---|---|---|
  | 20 KB | 1.998 ms | 1.195 ms | **1.67×** |
  | 50 KB | 5.014 ms | 3.000 ms | **1.67×** |
  | 100 KB | 12.079 ms | 6.062 ms | **1.99×** |
  | 200 KB (the cap) | 33.249 ms | 17.589 ms | **1.89×** |

  **Equivalence verified, not assumed:** 48 permutations (5/50/200 KB × email-at-top vs
  email-in-footer × whatsapp present/absent × instagram present/absent × linkedin
  present/absent) → `current(...) == prefiltered(...)` on all 48, contacts included. Also hoist the
  `enricher.py:199` pattern to a module-level `_MAILTO_RE` while you are there.
- **Why this is S2 and not S3:** the win is small in absolute terms at a median page (saves ~7 min
  of a 183 min run), but it is **load-bearing at the tail**. At the 200 KB cap with a 3.5 s mean
  latency, 500k records cost **402 min — a hard overrun of the 300-minute CI budget**
  (`.github/workflows/scrape.yml:25`), and 110 of those 402 minutes are this CPU. The pre-filter
  removes 44 of them. It converts a run that dies with nothing written into one that finishes.
- **The reason CPU matters at all here, stated once:** `re` holds the GIL, so none of these
  milliseconds can be spread over the 40 workers in `check_websites` (`enricher.py:225`, called
  with no argument at `enricher.py:337`). Wall clock is
  `F·L/W` (parallel network) **+ `F·c`** (serial, GIL-held), not `max(...)`.

### S3 — `_INSTAGRAM_RE`'s `@` alternative forces a full-document scan and returns nothing on most pages

- **Where:** `enricher.py:131` — `(?:instagram\.com/|@)([a-zA-Z0-9_.]{2,30})/?`, consumed at
  `enricher.py:206-210`.
- **Breaks:** nothing is lost — it is a pure cost. The `@` branch means the pattern matches
  essentially any `@` on the page, and because the alternation has no common literal prefix the
  engine attempts it at all 204,800 positions before giving up. Measured on a 200 KB page with no
  instagram link and no email: **4.548 ms, 23% of the function's entire CPU**, returning nothing.
  Most small-business homepages carry no Instagram link at all (the handle lives on a separate
  social profile), so this is paid on the majority of fetches.
- **Trigger:** any 200 KB business page whose only `@` is in the footer, or which has none.
- **Fix:** gate it on the literal like the others — `if "instagram.com" in low or "@" in low:`
  before `enricher.py:206`. That is *not* a complete fix (it still matches any `@`), but it makes
  the zero-instagram case fall through the cheap `@ in low` test. A complete fix means dropping the
  `@` branch or requiring a plausible handle shape, which **changes extracted results** and belongs
  to the enrichment-depth decision in audit 097, not to a perf patch. Deliberately not proposed
  here.

### S3 — The eager `findall` + list concatenation allocates up to 1.2 MB per call to keep one string. It is not worth changing for speed, and the obvious "fix" makes things slower

- **Where:** `enricher.py:199-200`
  (`mailto_hits = re.findall(...)` then `for addr in mailto_hits + _EMAIL_RE.findall(html):`).
- **Measured, and the honest verdict is "leave it".** `tracemalloc` peak per call:
  1 email → 1.7 KB; 100 emails (200 matches) → 14.5 KB; 2,000 emails (4,000 matches) → **303 KB**;
  and a fully adversarial 200 KB body of nothing but addresses (**18,618 matches**, the worst case
  the `_MAX_BODY_BYTES` cap at `enricher.py:158`/`httpclient.py:54` permits) → **1.20 MB peak,
  28–50 ms**. The `+` at `enricher.py:200` doubles the list.
- **The tempting fix is a regression.** Replacing both `findall` calls with
  `itertools.chain(...finditer...)` and breaking on the first hit is *break-even on realistic
  input* and only wins on adversarial bodies, because `findall` iterates in C while a chained
  generator pays Python-level resumption per match:

  | page | current (eager) | `chain(finditer)` | ratio |
  |---|---|---|---|
  | 20 KB, realistic | 1.965 ms | 1.983 ms | 0.99× (**slower**) |
  | 50 KB, realistic | 4.947 ms | 4.925 ms | 1.00× |
  | 100 KB, realistic | 9.995 ms | 9.890 ms | 1.01× |
  | 200 KB, realistic | 19.906 ms | 19.794 ms | 1.01× |
  | 200 KB, 18,618 matches | 28.1 ms | 1.4 ms | **19× faster**, 1.20 MB → 2.0 KB |

  Recorded so nobody spends a review cycle "fixing" this. Take the S2 pre-filter instead; it is
  orthogonal and strictly better on both realistic and adversarial input.

## Not a bug, but worth knowing

- **Complexity verdict, stated plainly.** `_fetch_website` is **O(1) in n** (number of businesses).
  Its only parameter is `url`; it reads no globals that mutate (`enricher.py:130-142` are
  module-level compiled patterns, immutable), and no loop in it is indexed by dataset size. Cost is
  **O(body size)**, measured at a clean **~75 µs/KB** across 20/50/100/200 KB — linear, so O(1) in
  n. It is **not O(n²)** and **does not allocate proportionally**: every intermediate
  (`raw` at `enricher.py:188`, `html` at `enricher.py:189`, the two `findall` lists at
  `enricher.py:199-200`) is dead at return. Peak RSS contribution is O(1) — at most ~200 KB of
  bytes plus a ~200 KB `str` plus the 1.2 MB adversarial regex worst case, all transient, times
  the 40 workers in flight (~24 MB ceiling).

- **The body cap is correct and the old "decompression bomb" finding is fixed.** `enricher.py:175`
  uses `stream=True` and `enricher.py:188` does `r.raw.read(_MAX_BODY_BYTES, decode_content=True)`,
  so the 200 KB limit (`enricher.py:158` ← `httpclient.py:54`) applies to **decompressed** bytes
  and the socket stops being read at the cap. Audit 083 item 7 (`docs/audits/083-SYNTH-s1-backlog.md:23`)
  described this as "the limit is applied after requests has downloaded and decompressed the full
  response" — that is no longer true of the current code. Do not re-file it.

- **`r.close()` at `enricher.py:195` forfeits connection reuse — and puts a dead socket back in
  the pool.** Verified against requests 2.32.3 `src/requests/models.py`: `Response.close()` is
  `if not self._content_consumed: self.raw.close()` followed by `release_conn()`. Because
  `_fetch_website` reads `r.raw` directly, `_content_consumed` stays `False`, so the socket is
  closed and the closed connection is then returned to the pool. **This costs nothing in practice:**
  every task targets a different business's host, so cross-task connection reuse is ~0 by
  construction, and the thread-local `Session` (`httpclient.py:61-71`) is a correctness win (audit
  004), not a throughput one. The dead-socket-in-pool case needs the same host+scheme twice in one
  thread — which the S2 memoization would eliminate anyway.

- **`decode` at `enricher.py:189` is not a cost.** 200 KB: utf-8 **0.02 ms**, utf-16 **0.06 ms**,
  cp1256 **0.13 ms**. Ignore it. (A non-perf caveat for whoever owns the correctness lens: the
  charset is taken from the server's `Content-Type` at `enricher.py:189` with no allow-list, which
  is a content-spoofing surface, not a speed one.)

- **`re.findall` with a string literal at `enricher.py:199` does not recompile the pattern.** The
  pattern and flags are identical on every call, so `re._compile` hits its cache after the first
  invocation and it degrades to a dict lookup on a string whose hash is already cached on the code
  object. There is no per-call compile cost. Worth knowing only because it looks like one.

- **Concrete totals.** Wall clock = `F·L/40 + F·c`, where `F = 0.40 × records` (share carrying a
  website; audits 052/097 use 40%), `L` = mean fetch latency, `c` = measured tail CPU (5.0 ms at a
  50 KB page, 33.2 ms at the 200 KB cap). The `F·c` term is serial because of the GIL.

  | records | fetches | L=0.6 s | L=2.0 s | L=3.5 s |
  |---|---|---|---|---|
  | 10,000 | 4,000 | 1.3 min | **3.7 min** | 6.2 min |
  | 100,000 | 40,000 | 13.3 min | **36.7 min** | 61.7 min |
  | 500,000 | 200,000 | 66.7 min | **183.3 min** | 308.3 min |

  (50 KB page.) Worst case — every page at the 200 KB cap and `L=3.5 s` — 500,000 records is
  **402.3 min, 134% of the 300-minute budget** (`.github/workflows/scrape.yml:25`), and because
  `write_csv` only runs at `main.py:209-213` (after `enrich()` at `main.py:187`) an overrun writes
  **nothing**. The atomic temp-file + rename at `main.py:108-134` means the previous master survives,
  so an overrun wastes the run rather than corrupting it — that part is already correct.

  Where `L` comes from, so the estimate is falsifiable: DNS+TCP+TLS to Gulf/Lebanon hosts from a
  `ubuntu-latest` runner is 80–250 ms; SMB TTFB is 200–1500 ms (audit 005 documents this
  explicitly); `allow_redirects=True` at `enricher.py:175` adds 1–3 round trips for the typical
  `http→https→www→/locale` chain at 200–600 ms; and `timeout=8` at `enricher.py:175` is a
  **per-socket-operation** timeout, not a total deadline, so every parked domain or dead DNS burns
  the full 8 s. If 25% of sites are unreachable that alone adds 2.0 s to `L` — which is why the
  table spans 0.6 s to 3.5 s.

- **`workers=40` (`enricher.py:225`, passed with no override at `enricher.py:337`) is the second
  largest lever and it is a one-token change.** Wall clock is `1/W` in the network term, so
  40 → 150 is **3.75×**: 183 min → 49 min at 500k. Do not do this alone — it needs the per-host
  politeness from audits 054/062, and the `ubuntu-latest` 2-vCPU runner will become the constraint
  before 150 threads do. CPU stops being the bottleneck well before that: the network term exceeds
  the serial-CPU term while `W < L/c`, i.e. `W < 2.0/0.005 = 400` at a 50 KB page.

- **Fix ranking for the 500k / 200k-fetch case**, all cumulative:

  | change | wall clock | vs baseline |
  |---|---|---|
  | baseline: W=40, 50 KB page, L=2 s | 183.3 min | 1.0× |
  | + literal pre-filter (S2) | 176.6 min | 1.04× |
  | + workers 40→150 | 54.4 min | 3.4× |
  | + per-URL memo (S2) | **15.0 min** | **12.2×** |
  | all three | **11.3 min** | **16×** |

  The pre-filter looks negligible here only because a 50 KB page is cheap; at the 200 KB cap it is
  the difference between 402 min and 358 min.

- **Single change with the largest speedup: memoize `_fetch_website` on the URL (and stop
  re-fetching rows whose `website_live` is already in the master).** 33× on a steady-state run,
  **17.0×** cumulative over the 33 runs it takes to reach 500k, and it also removes the outbound
  load that politeness audits are worried about. The `workers` bump is 3.4× for one token; the
  pre-filter is 1.04× at median / 1.11× at the cap for four lines. None of the three is worth
  anything if the first is not done, because all three are per-fetch costs and the first removes
  97% of the fetches.

- **Measurement caveat.** Timings are CPython 3.14.8 on a 12-core Mac16/8, best-of-40/50 with the
  GC disabled, on a machine concurrently running a large agent pool — so I report **best-of-N**
  rather than median, and absolute microsecond figures should be treated as ±20%. Relative ratios
  (literal prefix vs none; pre-filter vs current; GIL 0.99× vs parallel) are stable properties of
  CPython's `sre` and held across every size I tested. The brief specifies Python 3.12; `sre` is
  unchanged in behaviour between 3.12 and 3.14. No network calls were made; all page bodies are
  synthetic, and the L=0.6/2.0/3.5 s latency figures are stated assumptions, not measurements.

## Recommended order of work

1. **Memoize by URL, and stop re-fetching known verdicts (S2, first finding).** Largest lever by an
   order of magnitude: 183 min → 15 min at 500k, 46.8 h → 2.8 h cumulative, 3.37M → 198k outbound
   requests. Two implementation levels: (a) tonight, skip any record whose `website_live` is not
   `None` and whose `website` matches the master row, unless a `--force-refetch` flag is passed —
   this is a few lines at `enricher.py:231` and it makes the run's cost track the *scrape*, not the
   history; (b) properly, a `site_probe` table keyed by URL with a TTL and a recorded
   `checked_at`, joined in `check_websites` before submitting, which also gives you the freshness
   column audit 101 wants. Consider keying on the registrable domain rather than the full URL to
   collapse multi-branch chains; measure the collapse rate before committing to it.
2. **Add the literal pre-filter at `enricher.py:199-221` (S2, second finding).** Four lines, output
   proven identical across 48 permutations. Cheap now, decisive at the 200 KB cap, and it is the
   change that makes a 500k run fit inside `timeout-minutes: 300` when combined with step 1.
   Hoist the `enricher.py:199` pattern to a module-level `_MAILTO_RE` in the same edit.
3. **Then raise `workers` (`enricher.py:225`), not before.** 3.4× for one token, but only *after*
   step 1 has cut the fetch count, and only alongside the per-host politeness from audits 054/062.
   Make it a config value rather than a default argument while you are there.
4. **Gate `_INSTAGRAM_RE` on `enricher.py:206` (S3).** One line, folds into step 2. Do not touch the
   `@` branch — changing what it extracts is an enrichment-depth decision (audit 097), not a
   performance one.
5. **Do not touch `enricher.py:200`'s `findall` + concatenation (S3).** It allocates up to 1.2 MB
   per call on adversarial input, but the lazy-generator rewrite is measurably *slower* on every
   realistic page size. It is recorded here so it does not get "optimized" later.