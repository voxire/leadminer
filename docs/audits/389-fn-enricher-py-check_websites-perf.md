# 389 — `check_websites` performance at 500k records

**Target:** `enricher.py:225` **Lens:** perf

## Verdict

`check_websites` is **O(n)** in its orchestration — I measured per-record cost flat at
18.4 / 20.1 / 20.7 µs for 10k / 100k / 500k, with time ratios of 10.92× for 10× n and
5.14× for 5× n. There is no hidden O(n²) in the harness. But it calls `_fetch_website`,
whose per-record cost is **O(k²) in the longest character run of a web page**, and
Python's `re` never releases the GIL, so that quadratic is a **hard serial floor** that
the 40 workers cannot hide. Ordinary pages — a base64 hero image, a long CDN URL — cost
19–31 s of GIL-held CPU each. **At 1% of sites carrying such a page, a 500k run goes from
2.86 h to 24.7 h and exceeds the 5 h CI budget (`scrape.yml:25`) fivefold.**

The single largest speedup is a **one-line bounded-quantifier rewrite of `_EMAIL_RE`
at `enricher.py:130`**: measured **768–943× faster** on real body shapes, identical
recall, quadratic → linear. Nothing else in the function is worth more than a bounded
constant factor.

---

## Findings

### S1 — `_EMAIL_RE` is quadratic in the longest character run, and the GIL makes it serial

- **Where:** pattern defined at `enricher.py:130`, executed at `enricher.py:200`
  inside `_fetch_website`, reached from the pool created at `enricher.py:237`.

  ```python
  _EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
  ```

- **Breaks:** the pattern begins with an **unbounded** repeat and has **no literal first
  character**, so CPython's `sre` cannot apply its `memchr` prefix optimisation and must
  attempt a match at every offset. At each offset `[a-zA-Z0-9._%+\-]+` consumes the
  entire run of legal characters, fails on `@`, backtracks one character at a time, and
  the engine then restarts at the next offset. Cost is **O(k²) in the longest run length
  k**, not in the body size.

  Nothing bounds it. `timeout=8` at `enricher.py:175` covers only socket operations. The
  body read at `enricher.py:188` is capped at `_MAX_BODY_BYTES` = 200,000
  (`httpclient.py:54`), but the regex runs *after* the body is fully in hand
  (`enricher.py:189` → `:200`) and is subject to no deadline.

  **Measured scaling of `_EMAIL_RE.findall` on `"A" * n`** (no `@` present, so every
  attempt fails at the last step — the pure worst case):

  | bytes | seconds | × time for × bytes |
  |---|---|---|
  | 20,000 | 0.4867 | — |
  | 40,000 | 1.9216 | **4.00×** |
  | 80,000 | 7.7863 | **4.35×** |
  | 160,000 | 34.6753 | **4.00×** |
  | 200,000 | 49.7205 | — (the `enricher.py:158` cap) |

  4× per doubling is clean quadratic. Through the whole `enricher.py:199-222` block, a
  200,000-byte body costs **157 s in a single worker**.

- **Trigger — this is not a contrived input.** It fires on three completely ordinary page
  elements. Cost of `_EMAIL_RE.findall` at the 200 KB cap:

  | body shape | bytes | seconds |
  |---|---|---|
  | `<img src="data:image/png;base64,QUJDREVG...">` | 105,034 | **13.4 – 25.3** |
  | `<img src="https://cdn.example.com/a1b2c3d4...">` | 128,040 | **20.1 – 28.6** |
  | `<noscript><img src="https://px.abcdefgh...">` | 130,045 | **21.9 – 30.6** |
  | plain CMS page, contacts in footer | 200,000 | 0.0032 |
  | SPA with minified JS | 117,617 | 0.0122 |

  A base64 `data:` URI and a single long CDN path segment are on an enormous fraction of
  small-business sites built on WordPress, Shopify and page builders. **This is the
  expected case, not an attack.**

  Proof the cost is about run length and not byte count — same ~192,000 bytes, only the
  longest run varies:

  | longest run | bytes | seconds | vs run=200 |
  |---|---|---|---|
  | 200 | 192,984 | 0.1682 | 1.0× |
  | 2,000 | 192,120 | 1.7621 | 10.5× |
  | 12,000 | 192,040 | 11.7712 | 70.0× |
  | 50,000 | 192,027 | 44.1294 | **262.3×** |

  Inserts one space every 500 chars into the same bytes and it drops to 0.17 s.

- **Why 40 workers do not help.** `re` is a C extension that does not release the GIL.
  Measured, same 17,624-byte body:

  | threads | tasks | wall | vs serial prediction |
  |---|---|---|---|
  | 1 | 1 | 0.8968 s | — |
  | 4 | 4 | 3.6644 s | **1.02×** |
  | 8 | 8 | 7.0443 s | **0.98×** |

  Wall time equals `n_threads × single-thread time`. The extraction CPU has **zero**
  parallelism; it is a floor, not a throughput term.

- **The model, measured against the real pool shape.** I simulated 40 workers where each
  request is `sleep(1.5s)` then `extract(body)`, and varied the fraction of sites whose
  body triggers the quadratic:

  | evil fraction | evil sites / 3,000 | measured | vs clean | projected to 275,000 sites |
  |---|---|---|---|---|
  | 0% | 0 | 117.34 s | 1.00× | **2.99 h** |
  | 1% | 30 | 976.50 s | 8.32× | **24.86 h** |
  | 5% | 150 | 4,412.81 s | 37.58× | **112.36 h** |
  | 10% | 300 | 8,708.20 s | 74.16× | **221.74 h** |
  | 20% | 600 | 17,298.99 s | 147.33× | **440.48 h** |

  The relation is exact, not fitted: `wall = sites×L/workers + n_evil × 28.6 s`. Check
  the 5% row: 150 × 28.6 = 4,290 s predicted added, 4,412.81 − 117.34 = 4,295 s measured.

  So at n=500k with 275,000 sites:
  - **360 hostile bodies — 0.13% of sites — double the entire run.**
  - **628 hostile bodies — 0.23% of sites — exceed the whole 5 h CI budget on their own.**

- **Projected wall clock, n = 10k / 100k / 500k**, at `workers=40` (`enricher.py:225`)
  with 55% of records carrying a website:

  | n | sites | all clean | 1% quadratic bodies | 5% quadratic bodies |
  |---|---|---|---|---|
  | 10,000 | 5,500 | 0.06 h | 0.50 h | 2.24 h |
  | 100,000 | 55,000 | 0.57 h | 4.94 h | 22.4 h |
  | 500,000 | 275,000 | **2.86 h** | **24.71 h** | **112.10 h** |
  | | | `scrape.yml:25` budget = **5.00 h** | | |

  `scrape.yml:9` runs this **weekly**, so it recurs unattended, and `main.py:209-213`
  writes all five CSVs only *after* `enrich` returns — a timeout produces nothing.

- **Fix:** bound the repeats so no start position can do unbounded work.

  ```python
  _EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,255}\.[a-zA-Z]{2,}")
  ```

  Measured (64 is the RFC 5321 maximum local-part length; 255 the maximum domain):

  | body shape | as written | bounded | speedup |
  |---|---|---|---|
  | clean CMS 200 KB | 0.0032 s | 0.00311 s | 1× |
  | base64 hero image 105 KB | 13.3710 s | 0.01741 s | **768×** |
  | long CDN URL 128 KB | 20.0573 s | 0.02127 s | **943×** |
  | ad noscript blob 130 KB | 30.5944 s | 0.03259 s | **939×** |
  | `"A" * 160,000` | 32.6882 s | 0.02677 s | **1,221×** |

  Confirmed linear — time ratio per doubling of input: **2.00×** (bounded) vs **4.00×**
  (original). Recall is unchanged: on 8 real address forms
  (`info@acme-lb.com`, `a.b+tag@sub.domain.co`, `x_y-z%q@example.museum`,
  `UPPER.CASE@Example.COM`, `a@b.co`, `first.last@mail.domain.gov.lb`,
  `o'brien@mail.com`, `user_name123@sub-domain.example.org`) plus three malformed decoys,
  both patterns return the identical list of 8.

  > **Do not "fix" this with a possessive quantifier.** I tested
  > `[a-zA-Z0-9._%+\-]++@…` and it is *still quadratic* — 26.73 s at 160 KB, ratio 4.01×
  > per doubling. `++` stops the local part backtracking, but the engine still restarts
  > one character later and re-consumes the whole run, so it makes n full passes. The
  > **bounded** form is the one that works.

---

### S2 — Every future is materialised before the first result is consumed: 505 MB at n=500k

- **Where:** `enricher.py:238`

  ```python
  futures = {pool.submit(_fetch_website, url): idx for idx, url in targets}
  ```

- **Breaks:** this is a dict comprehension, so all N `Future` objects exist
  simultaneously, before `as_completed` at `enricher.py:240` is ever entered. There is no
  streaming, no backpressure: `ThreadPoolExecutor._work_queue` is an unbounded
  `queue.SimpleQueue`, so `submit` enqueues the entire dataset up front. Measured cost is
  **~1,930 bytes per target** (Future + `_WorkItem` + args tuple + result tuple +
  the per-fetch `contacts` dict from `enricher.py:168`):

  | targets | peak live allocation | bytes/target | time to submit all |
  |---|---|---|---|
  | 10,000 | 18.5 MB | 1,940 | 202 ms |
  | 50,000 | 92.7 MB | 1,945 | 1,064 ms |
  | 275,000 | **505.4 MB** | 1,927 | 5,635 ms |

  Total orchestration peak (targets list + futures + queue) is **567 MB at n=500k**.

- **Trigger:** 500,000 records with 275,000 carrying a website. On a GitHub Actions
  `ubuntu-latest` runner (7 GB) this is survivable but it is a hard linear floor that
  sits on top of the record list, the five CSV writes, and the 505 MB is *live* for the
  entire multi-hour enrichment — it is not reclaimed until the last future resolves.

- **Fix:** a bounded submission window — keep at most `workers * 4` futures alive and
  submit a replacement as each completes. Measured: **505.4 MB → 0.8 MB, a 630×
  reduction, with no measurable time cost.** There is also `concurrent.futures.wait` with
  `return_when=FIRST_COMPLETED`, which is the correct primitive; the current code uses
  `as_completed` over a fully-materialised dict, which is what forces the materialisation.

---

### S2 — 9.5× of all network traffic re-fetches an answer already in the master CSV

- **Where:** `enricher.py:231` builds `targets` with no URL deduplication;
  `main.py:150` loads the entire cumulative master, `main.py:179` concatenates it with
  new records, `main.py:180` dedups, `main.py:187` calls `enrich`, which reaches
  `enricher.py:337` → `check_websites`.

- **Breaks:** `enricher.py:231` filters on `r.get("website")` **only**. It never looks at
  `website_live`, `email`, `instagram`, `whatsapp` or `linkedin` — all of which are
  already populated in the master from the previous run. I grepped the whole repo: there
  is no skip condition anywhere, and `cli.py:247` (`cmd_score`) exists precisely because
  re-scoring needs an offline path — but re-*enriching* does not.

  `dedup.dedup()` (`dedup.py:197-232`) keys on normalised phone, else
  `(normalised_name, city)`. It **never considers `website`**, so two records sharing one
  URL survive as two records and are fetched twice. This is realistic because
  `osm.py:91` reads `tags.get("website") or tags.get("contact:website") or tags.get("url")`
  and OSM `url` is very often a shared or franchise URL.

- **Trigger:** the weekly run at `scrape.yml:9`. Assuming 15,000 new sites discovered per
  run (a plausible weekly yield for a 68-query paginated Places sweep over 3 cities plus
  full OSM Lebanon):

  | run | master sites | GETs issued this run | cumulative | waste |
  |---|---|---|---|---|
  | 1 | 15,000 | 15,000 | 15,000 | 1.00× |
  | 5 | 75,000 | 75,000 | 225,000 | 3.00× |
  | 10 | 150,000 | 150,000 | 825,000 | 5.50× |
  | 18 | 270,000 | 270,000 | 2,565,000 | **9.50×** |

  Reaching 500k records (275,000 sites) takes ~18 weekly runs. Cumulative GETs issued:
  **2,565,000**. GETs needed: **270,000**. Total traffic is quadratic in the number of
  runs.

  Within a single run, modelling domain popularity as Zipf (the `osm.py:91` shared-URL
  case):

  | scenario | records | unique URLs | unique hosts | GETs saved |
  |---|---|---|---|---|
  | n=100k, clustered hosts | 55,000 | 11,878 (21.6%) | 11,878 | **4.6×** |
  | n=500k, clustered hosts | 275,000 | 59,369 (21.6%) | 59,369 | **4.6×** |
  | n=500k, flat, 4 paths/domain | 275,000 | 243,178 (88.4%) | 173,581 | 1.13× / 1.58× |

- **Fix:** two independent changes, both cheap.
  1. Inside `check_websites`, key a memo on the URL. `_fetch_website` is a deterministic
     function of the URL, so this is free in correctness terms and removes the
     within-run duplicate traffic. A `url → (outcome, contacts)` dict costs **16.3 MB at
     275,000 entries** — 31× smaller than the futures dict it replaces.
  2. Skip records whose `website_live` is already set and whose `website` has not changed
     since it was set. This is the 9.5× term and nothing else can address it.

  Combined at n=500k, L=1.5s, `workers=40`:

  | scenario | GETs | wall |
  |---|---|---|
  | as written, cold run | 275,000 | 2.86 h |
  | + within-run URL memo | 59,369 | 0.62 h |
  | + skip already-enriched | 28,947 | 0.30 h |
  | + both | 6,249 | **0.07 h** |

  The extraction CPU does not scale down with this fix, which is precisely why S1 must
  land first.

---

### S3 — Seven separate passes over `records` where one fused pass does

- **Where:** `enricher.py:264-270` — seven independent `sum(1 for r in records ...)`
  generator passes over the full list, not over `targets`.
- **Breaks:** nothing functional. At n=500k they cost **753 ms** of the ~10.3 s
  orchestration total (4.65 M `dict.get`/s). A single fused loop measured **238 ms**.
- **Trigger:** any large `records` list; the passes scale linearly and are pure
  interpreter overhead on a hot path that also prints.
- **Fix:** fuse into one `for r in records:` loop accumulating seven counters — saves
  **515 ms** at n=500k for a three-line change.

---

### S3 — `allow_redirects=True` multiplies the per-site network worst case by up to 30

- **Where:** `enricher.py:175`

  ```python
  r = get_session().get(url, timeout=8, allow_redirects=True, stream=True)
  ```

- **Breaks:** `timeout=8` is a **per-socket-operation** timeout, not a total-budget one,
  and `requests.Session.max_redirects` defaults to 30. A redirect chain therefore holds
  a worker slot for up to **30 × 8 s = 240 s** while appearing, in the progress output at
  `enricher.py:261-262`, as a single slow site. At 275,000 sites, even a 2% redirect rate
  against a slow destination adds `5,500 × 240 / 40 = 9.2 h`.
- **Trigger:** a `website` field pointing at an `http://` URL that 301s to `https://`
  (extremely common — `osm.py:95` and `wikidata.py:104` both prepend `https://`, but
  Google's `websiteUri` is frequently the bare `http://` form), landing on a host that
  accepts the connection and then stalls.
- **Fix:** pass `max_redirects=3`. Real sites need at most the `http` → `https` → `www`
  triple. This is also worth doing for correctness, since a redirect to a login wall or a
  parked-domain page currently yields a confident LIVE verdict.

---

## Not a bug, but worth knowing

- **The un-precompiled pattern at `enricher.py:199` is not recompiled per call.**
  `re.findall` with a string pattern is served by `re._cache`. Measured 342.42 µs/call
  (string pattern) vs 340.71 µs/call (precompiled) — noise. I checked specifically
  because this is a common false positive; it is not one here.

- **Only `_EMAIL_RE` is bad. The other three patterns are linear.** Same 20→160 KB
  charset-run input, measuring the time ratio per doubling:

  | pattern | 20 KB | 40 KB | 80 KB | 160 KB | verdict |
  |---|---|---|---|---|---|
  | `_EMAIL_RE` (`:130`) | 0.48731 | 1.92157 | 7.78631 | 34.67526 | **superlinear** |
  | `_INSTAGRAM_RE` (`:131`) | 0.00034 | 0.00068 | 0.00131 | 0.00262 | linear |
  | `_WHATSAPP_RE` (`:132`) | 0.00045 | 0.00086 | 0.00171 | 0.00353 | linear |
  | `_LINKEDIN_RE` (`:136`) | 0.00010 | 0.00020 | 0.00040 | 0.00080 | linear |
  | `mailto` findall (`:199`) | 0.00010 | 0.00020 | 0.00040 | 0.00080 | linear |

  The other three all start with a literal or a small character set, so `sre` *can*
  memchr to an anchor. `_EMAIL_RE` is the only one that cannot, and it is 1000–40,000×
  more expensive than its siblings on the same input.

- **The 40-thread concurrency model is sound.** I was looking for GIL saturation on the
  network path and did not find it. 4,000 simulated 50 ms requests:

  | workers | wall | req/s | ideal `N·L/w` | overhead/req |
  |---|---|---|---|---|
  | 1 | 235.62 s | 17.0 | 200.00 s | 8.905 ms |
  | 8 | 28.42 s | 140.7 | 25.00 s | 0.855 ms |
  | 40 | 5.64 s | 709.2 | 5.00 s | 0.160 ms |
  | 320 | 0.78 s | 5099.4 | 0.62 s | 0.040 ms |

  Scaling is near-linear. So `wall ≈ sites × mean_latency / workers` holds, and
  `workers=40` is the only concurrency knob — and it is hardcoded as a default argument
  at `enricher.py:225` with no way to override it from `main.py` or the CLI
  (`cli.py:33-37` calls `pipeline.main()`, which passes nothing). For reference, fitting
  `scrape.yml:25`'s 300 minutes at n=500k needs 23 workers at L=1.5 s, 46 at L=3.0 s and
  123 at L=8.0 s. The S1 fix is what makes the budget reachable at all.

- **`_EMAIL_RE.findall` at `enricher.py:200` materialises every match before the loop
  breaks** on line 204, and `mailto_hits + _EMAIL_RE.findall(html)` at line 200 builds a
  second list by concatenation. On a clean 200 KB body with one address I measured 5.78 ms
  eager vs 5.77 ms with `finditer` + early `break` — negligible. Worth folding into the
  S1 edit as a free allocation saving, but **not worth its own finding**. Note the
  contrast with `enricher.py:206`, which already does the right thing with `finditer`.

- **The orchestration is genuinely O(n); I looked for O(n²) and it is not there.**
  `_fetch_website` stubbed to a no-op, so this is pure harness cost:

  | n | targets | min wall | µs/record | peak alloc | ratio check |
  |---|---|---|---|---|---|
  | 10,000 | 5,500 | 0.184 s | 18.40 | 11.9 MB | — |
  | 100,000 | 55,000 | 2.008 s | 20.08 | 113.3 MB | 10.92× for 10× n |
  | 500,000 | 275,000 | 10.330 s | 20.66 | 567.3 MB | 5.14× for 5× n |

  Per-record cost rises only 12% across a 50× range. Breakdown at n=500k:
  `targets` build (`enricher.py:231`) 379 ms / 27.4 MB; the seven counting passes
  (`enricher.py:264-270`) 753 ms; submit + consume (`enricher.py:238-240`) ~9.2 s
  including the 505 MB of allocation. So the harness is **~10% of a 2.9 h run** — the
  per-site HTTP cost is the dominant term by two orders of magnitude, and it is the only
  term S1/S2 touch.

- **Per-worker transient memory is fine.** `enricher.py:188-189` reads at most 200 KB and
  decodes it, so at 40 workers the transient peak is roughly 40 × 400 KB ≈ 16 MB. The
  `_MAX_BODY_BYTES` cap (`httpclient.py:54`, applied at `enricher.py:158`) is doing its
  job and is not a finding — it is only the *downstream* regex that ignores it.

- **Measurement environment.** macOS, 12 logical CPUs, 25 GB RAM, CPython 3.14.8
  GIL-enabled. Benchmarks lived in
  `/private/var/folders/…/T/opencode/bench_{a,a2,b,c,c1b,c2,c3,e,f,f2,g}_*.py`. The
  quadratic is a property of CPython's `sre` and reproduces on 3.12; absolute seconds
  will differ, the ratios will not. `requests` was not installed and I did not install it,
  so the HTTP path was exercised through `http.client` / sleep-based simulation and a
  stubbed `_fetch_website`; no scraper or external site was contacted.

---

## Recommended order of work

1. **Bound the repeats in `_EMAIL_RE` (`enricher.py:130`).** One line, 768–943× on real
   body shapes, identical recall, quadratic → linear. This is the single largest speedup
   available and the only change that converts an *unbounded* per-body cost into a
   bounded one — and because `re` holds the GIL, nothing else in the function can hide it.
   Take the URL memo (item 2) in the same pass so it feeds off the S2 win.
2. **Memoize `_fetch_website` by URL inside `check_websites` (`enricher.py:231`), and skip
   records whose `website_live` is already set.** 4.6× within-run and 9.5× cross-run on
   the network term; 2.86 h → 0.62 h → 0.07 h combined. Costs 16.3 MB of cache.
3. **Bound the submission window (`enricher.py:238`).** 505.4 MB → 0.8 MB, no time cost.
   Removes the linear memory floor before it becomes an OOM at larger n.
4. **Cap redirects at `max_redirects=3` (`enricher.py:175`).** Trivial; bounds a
   per-site worst case of 240 s, and incidentally stops redirect-to-parking-lot being
   scored LIVE.
5. **Fuse the seven counting passes (`enricher.py:264-270`).** 515 ms at n=500k.
6. **Make `workers` reachable** — `enricher.py:225` takes a default that no caller sets.
   After items 1–2 the binding constraint flips from CPU to latency, and at L=3.0 s or
   8.0 s a 500k run needs 46–123 threads, which is the point at which this needs to be a
   flag on `cli.py:285` rather than a literal.