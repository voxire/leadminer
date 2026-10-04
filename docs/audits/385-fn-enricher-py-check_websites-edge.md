# 385 — check_websites edge cases: the body cap is on the wrong side of the decompressor, and a blank URL is a phantom lead

## Verdict

`check_websites` (`enricher.py:225`) is **crash-proof at the HTTP boundary and fail-open at every other boundary**. Every malformed, mistyped, whitespace-only, scheme-less or hostile URL is absorbed by the `except Exception` at `enricher.py:176` and recorded as `UNKNOWN` — so the function never dies on bad *data*, but it also never distinguishes "the server refused us" from "we never made a request", and both land in the same column. The two defects that actually matter are: **(1) the 200 KB body cap at `enricher.py:188` is applied to the *compressed wire bytes*, so a 195 KiB gzip response inflates to 205,756,292 bytes (1029×) before the cap can act — 18 concurrent bombs OOM the runner and the run's CSV is never written;** and **(2) `website_live` is overwritten unconditionally at `enricher.py:250`, so one transient `UNKNOWN` permanently erases a DEAD verdict that a previous run already confirmed and wrote to the cumulative master** — destroying the single strongest sales signal in the product.

## The single most dangerous input

**A `website` value that is an ordinary, contract-conforming `str`, pointing at a host that replies `HTTP 200` with `Content-Encoding: gzip` and ~195 KiB of zeros.** Not a wrong type, not whitespace, not an emoji — a syntactically perfect URL, indistinguishable at `enricher.py:231` from any real target.

Ranked against the 60-odd other hostile inputs I exercised, it wins on every axis:

1. **It is the only input that kills the run instead of corrupting one row.** Every other hostile input ends in a caught exception or a wrong `website_live` value on one record. This one reaches SIGKILL — `except Exception` cannot catch it — and because `write_csv` runs only once at the end (`main.py:209-213`), it destroys the entire run's output, not just the enrichment. Every S1 consequence in BRIEF:114 ("a run that dies") applies.
2. **The mitigation that is supposed to stop it is in the wrong place.** `enricher.py:186-187` says in a comment "Cap the body before decoding it", and `enricher.py:188` reads `_MAX_BODY_BYTES` — but `decode_content=True` means the cap bounds the bytes read *off the socket*, and the decoder inflates them afterwards. The guard is on the wrong side of the zlib call. `026-enricher-ssrf-security.md:59` filed this as an S1 and its `stream=True` fix (`enricher.py:175`) closed only half of it, so the file currently reads as protected against a threat it does not protect against. A reviewer trusting the comment will not look again.
3. **The amplification is measured, not hypothesised.** 200,000 wire bytes → **205,756,292** bytes decoded (**1029×**, versus the DEFLATE spec maximum of 1032:1). That is 0.383 GiB per in-flight worker once the `bytes` and the decoded `str` are both resident. `max_workers=40` (`:237`) puts ~18 of them over the 7 GiB line of a stock `ubuntu-latest` runner — a runner the workflow does not size or memory-limit (`.github/workflows/scrape.yml:24-25`). The attacker's cost is 195 KiB of HTTP body.
4. **It needs no privilege, no malformed data, and no API key.** It is reachable through exactly the threat model `026` and `054` already assume: an anonymous crowdsourced OSM `website` tag edit, or any host that a listed business happens to point at. It violates no documented contract.
5. **It is a one-line fix**, and the same fix also repairs the legitimate-large-page case that the mis-placed cap silently mis-reports as `UNKNOWN`.

Runner-up, for completeness: a **whitespace-only `website` string** (S2-b) — lower blast radius (one lead, not one run) but it is the only input that puts a business with *no website* into `with_websites.csv` while deleting a real one from `without_websites.csv`, which BRIEF:54 names as the product's new-site pitch list.

### Methodology note (read this before trusting the tables)

`requests` and `urllib3` are **not installed** in this environment (`python3 -c "import requests"` → `ModuleNotFoundError`), and I was told not to install packages. So I imported the **real** `enricher.py` with `requests`/`httpclient` stubbed in `sys.modules`, reproducing the two library entry points that decide whether a URL is fetchable at all (`requests.adapters.get_adapter` and `PreparedRequest.prepare_url` semantics: `TypeError`/`AttributeError` on a non-`str` URL, `InvalidURL` on control characters, `MissingSchema` without a scheme, `InvalidURL` on IDNA/port failure). Everything in the tables below is therefore **executed real `enricher.py` code**. Claims that depend on library internals I could not execute (`raw.read(amt, decode_content=True)` bounding *wire* bytes; `requests` `Response.text` guarding `LookupError`) are labelled **[library]** and reasoned from source. Python-version note: the local interpreter is 3.14; the workflow pins **3.12** (`.github/workflows/scrape.yml:34`). `ThreadPoolExecutor` `max_workers` semantics (`None` → `min(32, cpu+4)`, `<= 0` → `ValueError`) are identical in both, so the `workers` table transfers.

---

## Part A — Input enumeration (the `website` value)

The only thing `check_websites` does with the `website` value before fetching is a **bare truthiness test** (`enricher.py:231`). There is no type check, no `str()` coercion, no `.strip()`, no scheme check, no length check, no allow-list.

| `website` input | truthy at `:231`? | HTTP request issued? | `website_live` | classification |
|---|---|---|---|---|
| `None` | no | 0 | `None` | **correctly handled** (never a target) |
| `""` | no | 0 | `None` | **correctly handled** |
| `0` (int) | no | 0 | `None` | **correctly handled** (falsy) |
| `0.0` | no | 0 | `None` | **correctly handled** |
| `False` | no | 0 | `None` | **correctly handled** |
| `[]`, `{}` | no | 0 | `None` | **correctly handled** |
| `"   "` (spaces) | **yes** | 1 | `None` | **silently wrong** — see S2-b |
| `"\n\t"` | **yes** | 1 | `None` | **silently wrong** |
| `"0"` | **yes** | 1 | `None` | **silently wrong** (reported unreachable) |
| `"null"`, `"None"`, `"false"`, `"n/a"`, `"-"` | **yes** | 1 | `None` | **silently wrong** |
| `123` (int) | **yes** | 1 | `None` | **silently wrong** (contract says `str \| None`, `scrapers/base.py:15`) |
| `3.4` (float) | **yes** | 1 | `None` | **silently wrong** |
| `b"https://x.com"` (bytes) | **yes** | 1 | `None` | **silently wrong** |
| `["https://a.com"]` (list) | **yes** | 1 | `None` | **silently wrong** |
| `{"url": ...}` (dict) | **yes** | 1 | `None` | **silently wrong** |
| `"www.bakery.com"` (no scheme) | **yes** | 1 | `None` | **silently wrong** — a perfectly good domain, never contacted |
| `"httpx.com"` | **yes** | 1 | `None` | **silently wrong** |
| `"http://"` | **yes** | 1 | `None` | **silently wrong** |
| `"://x.com"` | **yes** | 1 | `None` | **silently wrong** |
| `"javascript:alert(1)"` | **yes** | 1 | `None` | correctly handled as unreachable (no scheme adapter) |
| `"file:///etc/passwd"` | **yes** | 1 | `None` | correctly handled — no local file is read |
| `"data:text/html,x"` | **yes** | 1 | `None` | correctly handled |
| `"mailto:a@b.com"` | **yes** | 1 | `None` | correctly handled |
| `"https://a.com/\r\nHost: evil"` | **yes** | 1 | `None` | **correctly handled** — CRLF rejected, no header injection |
| `"https://مطعم.الزبون.com"` (Arabic IDN) | **yes** | 1 | `None` | **correctly handled** — IDNA-encoded; if it failed, absorbed |
| `"https://☕.com"` (emoji host) | **yes** | 1 | `None` | correctly handled |
| `"https://a.com/🍽️"` (emoji path) | **yes** | 1 | `None` | **correctly handled** — percent-encoded |
| `"https://" + "a"*5000 + ".com"` | **yes** | 1 | `None` | correctly handled (absorbed), but see S3-f |
| `"https://a.com/?" + "x"*100000` | **yes** | 1 | `None` | correctly handled |
| `"https://a.com:99999999999999/"` | **yes** | 1 | `None` | correctly handled (absorbed) |
| `"https://user:p@ss@host/x"` | **yes** | 1 | `None` | **correctly handled** |

**Zero of these inputs crash.** That is the single most reassuring result of this lens: the `except Exception` at `enricher.py:176` (and `:190`, `:196`) plus the `try/except` around `future.result()` at `:242-248` form a complete firewall around the network boundary. Note this also means **`083-SYNTH-s1-backlog.md:81` (item 36, "a single website-worker exception escapes `future.result()`") is stale** — that path is now guarded at `:242-248`. The remaining crash surface is *outside* the pool (Part C).

## Part B — Response enumeration (hostile and boundary responses)

Executed against real `enricher.py` with a fake session returning each response:

| response | `website_live` | extracted `email` | extracted `instagram` | classification |
|---|---|---|---|---|
| `200`, empty body | `True` | `None` | `None` | correct |
| `200`, `charset=utf8mb4` | **`None`** | `None` | `None` | **silently wrong** — S2-d |
| `200`, `charset=ISO-8859-6-I` | **`None`** | `None` | `None` | **silently wrong** — S2-d |
| `200`, `charset=` (empty) | `True` | `a@b.com` | `b.com` | **correctly handled** — `"" or "utf-8"` falls back |
| `200`, no `charset` header | `True` | `a@b.com` | `b.com` | **correctly handled** — falls back to utf-8 |
| `200`, UTF-16 Arabic body | `True` | `None` | `None` | correct |
| `200`, emoji + Arabic body | `True` | — | `bestbakery` | correct |
| `304 Not Modified` | **`True`** | `None` | `None` | **silently wrong** — S3-c |
| `403` Cloudflare challenge | **`False`** | `None` | `None` | **silently wrong** — already tracked, see cross-refs |
| `429` rate limited | **`False`** | `None` | `None` | **silently wrong** — already tracked |
| `500` | `False` | `None` | `None` | correctly handled (a real "broken" answer) |
| `200`, parked page w/ `abuse@sedo.com` | `True` | `abuse@sedo.com` | **`sedo.com`** | **silently wrong** — see cross-refs (025) |
| `200`, `mailto:info@b.com?subject=Book&body=Hi` | `True` | **`info@b.com?subject=book&body=hi`** | `b.com` | **silently wrong** — see cross-refs (025) |
| `200`, `mailto:info` (no `@`) | `True` | **`info`** | `None` | **silently wrong** — see cross-refs (025) |
| `200`, `wa.me/965501234567` | `True` | `None` | `None` | correct (`whatsapp=+965501234567`) |
| `raw.read()` raises mid-body | `None` | `None` | `None` | correct |
| `200`, `Content-Encoding: gzip`, 195 KiB of zeros | `True` + **~412 MB RSS** | (regexes run over 205 MB) | | **run-killing** — S1-a |

## Part C — `records` and `workers` boundaries

| call | result |
|---|---|
| `check_websites(None)` | **CRASH** `TypeError: 'NoneType' object is not iterable` (at `:231`) |
| `check_websites([])` | OK — returns the input list, **prints nothing** |
| `check_websites(("...generator..."))` | **CRASH** `TypeError: 'generator' object is not subscriptable` (at `:249`, **after** the fetch already ran) |
| `check_websites(["http://x"])` | **CRASH** `AttributeError: 'str' object has no attribute 'get'` (at `:231`) |
| `check_websites([None])` | **CRASH** `AttributeError: 'NoneType' object has no attribute 'get'` (at `:231`) |
| `check_websites({"k": "v"})` | **CRASH** `AttributeError: 'str' object has no attribute 'get'` (at `:231`) — dict iterates its keys |
| `workers=0` | **CRASH** `ValueError: max_workers must be greater than 0` (at `:237`) — *after* printing `Fetching 1 websites — … (0 workers)` |
| `workers=-1` | **CRASH** `ValueError: max_workers must be greater than 0` |
| `workers="40"` | **CRASH** `TypeError: '<=' not supported between instances of 'str' and 'int'` |
| `workers=None` | OK — silently becomes `min(32, cpu+4)` **[library]** |
| `workers=1.5` | OK — silently spawns 2 threads **[library]** |
| `workers=True` | OK — becomes 1 worker |
| `workers=10**9` | OK — lazily spawns only as many threads as there are targets |

Identity / aliasing, executed:

- `check_websites(x) is x` → **`True`**. The caller's list is returned and every dict is mutated in place (`:233`, `:250`, `:253-259`, `:276`).
- `shallow = records.copy()` shares dicts with `records` (`True`) → any caller that shallow-copies before calling still sees every `website_live`/contact mutation.
- The same dict object appearing twice in `records` is fetched **twice** and written twice; both entries end up with the same verdict, and `live_count` counts it twice.
- 5 records sharing one URL → **5 identical HTTP requests** (no URL de-duplication at `:231-238`).

---

## Findings

### S1 — The 200 KB body cap is on the *compressed* wire bytes, so a 195 KiB gzip response inflates 1029× and OOM-kills the run

- **Where:** `enricher.py:188` (`raw = r.raw.read(_MAX_BODY_BYTES, decode_content=True) or b""`), cap defined at `enricher.py:158` ← `httpclient.py:54` (`DEFAULT_MAX_BODY_BYTES = 200_000`)
- **Breaks:** `r.raw` is the urllib3 `HTTPResponse`. `read(amt, decode_content=True)` bounds `amt` **at the socket** and then *inflates* it **[library]**. The cap therefore constrains compressed bytes, not the string that `html` becomes at `:189`. Measured on this machine: feeding a 200,000-byte prefix of `gzip.compress(b"\x00" * 300MB, 9)` to a `zlib` decompressobj emits **205,756,292 bytes** (ratio **1029×**; the DEFLATE spec maximum is 1032:1).
  Cost accounting per in-flight worker: ~0.19 GiB for the `bytes` object + ~0.19 GiB for the decoded `str` = **0.383 GiB**. With `max_workers=40` (`:237`) a stock `ubuntu-latest` runner (7–16 GiB; the workflow pins no runner size, `.github/workflows/scrape.yml:24-25`) is OOM-killed at **~18 concurrent bomb responses on 7 GiB, ~10 on 4 GiB**. Then `re.findall`/`finditer` run five regexes over a 205 MB string (`:199-220`).
  Two outcomes, both bad: the cgroup OOM killer sends **SIGKILL**, which `except Exception` cannot catch — and because `write_csv` runs only once at the very end (`main.py:209-213`), the SIGKILL destroys **every** hour of scraping that run produced, not just the enrichment. If Python instead raises `MemoryError` it *is* caught at `:190` and the row silently becomes `UNKNOWN` — after the process is already thrashing.
  The mirror-image failure is also real: a *legitimate* page whose **compressed** size exceeds 200 KB is truncated mid-gzip-member; `zlib`'s decompressor cannot detect truncation without an EOF check, so `_fetch_website` proceeds with a partial body instead of reporting `UNKNOWN`.
  **This is the same defect `026-enricher-ssrf-security.md:59` filed as S1 and believed closed.** That report analysed the *old* `r.text` path; `enricher.py:186-188` fixed the `stream=False` half but left the cap on the wrong side of the decoder. `054-politeness-and-ssrf.md:34-36` gestures at decompression bombs but attributes them to the missing `Content-Length` pre-check, not to the cap placement.
- **Trigger:** one row whose `website` points at a host returning `Content-Encoding: gzip` with ~195 KiB of zeros. Fully reachable from crowdsourced input — an OSM `website` tag edit (which is all `026`/`054` assume an attacker needs) — and the value itself is a perfectly contract-conforming `str`.
- **Fix:** cap the **decoded** stream and break out of the loop, instead of capping the read:
  ```python
  buf = bytearray()
  for chunk in r.iter_content(chunk_size=16_384):
      buf += chunk
      if len(buf) >= _MAX_BODY_BYTES:
          break
  html = bytes(buf).decode(r.encoding or "utf-8", errors="replace")
  ```
  `iter_content` yields *post*-decompression chunks, so the cap then means what it says. Pair it with a per-run byte budget so 40 bombs cannot arrive at once.

### S1 — `website_live` is overwritten unconditionally: one transient `UNKNOWN` permanently erases a confirmed DEAD from the cumulative master

- **Where:** `enricher.py:250` (`r["website_live"] = True if outcome == LIVE else (False if outcome == DEAD else None)`)
- **Breaks:** `data/all_businesses.csv` is **cumulative across runs** (BRIEF:51) and `load_master` deliberately restores the tri-state on read: `row["website_live"] = True if wl == "True" else (False if wl == "False" else None)` (`main.py:102-103`). `website_live` is also marked `_VOLATILE` in dedup so the freshest observation survives the merge (`dedup.py:114`). So a DEAD verdict observed in run *N* is faithfully carried into run *N+1*'s input — and then **thrown away** the moment run *N+1*'s single fetch returns `UNKNOWN`. There is no merge, no "previous verdict" column, no grace period.
  The consequence is not cosmetic. `pitch_recommender.py:36,57-58` makes a confirmed dead site **Tier 1 "Website rebuild + maintenance"** — "the strongest pitch in the database" per its own comment at `pitch_recommender.py:55` — and `enricher.py:303-304` awards **+20 `lead_score`** for it. One network blip on one run moves the business out of the rebuild funnel, out of the top of the sales-ready list, and off the pitch a rep was going to make. The evidence is then overwritten in the master and is **unrecoverable**.
  This is worse than the no-history behaviour it replaced, because the pipeline now *has* the history and throws it away.
- **Trigger:** a record whose master row carries `website_live=False` from a prior run, whose host answers `429` or resets the connection on the next run's fetch (trivially provoked by the 40-worker burst described in `005`/`054`) → `enricher.py:250` writes `None`; `main.py:209` persists `None` to `all_businesses.csv`.
- **Fix:** stop treating the column as write-only. Either keep the previous verdict and record the new one separately (`website_live`, `website_live_prev`, `website_probed_at`, `probe_failures`), or require corroboration before demoting `False` → `None`:
  ```python
  if outcome is UNKNOWN and r.get("website_live") is False:
      r["website_live"] = False          # keep the confirmed verdict
      r["website_probe_uncertain"] = True # but say we could not re-confirm
  ```
  `enricher.py:246` already shows the author knows how to carry extra state; it just does not.

### S2 — The bare truthiness gate treats whitespace and placeholders as "has a website", inventing phantom leads and deleting real pitch targets

- **Where:** `enricher.py:231` (`if r.get("website")`) — and it is not `check_websites`'s own filter; `main.py:199-200` uses the identical bare-truthiness test to split the product:
  ```python
  with_websites    = [r for r in records if r.get("website")]
  without_websites = [r for r in records if not r.get("website")]
  ```
- **Breaks:** `"   "` is truthy. Executed: such a record **is** fetched (1 request), comes back `UNKNOWN`, is written to `with_websites.csv`, and is **excluded from `without_websites.csv`** — which BRIEF:54 defines as the *new-site pitch targets*, the primary output of the whole product. It also earns **+1 `completeness_score`** at `enricher.py:107` (`if record.get("website")`), which is what promotes it into `qualified_businesses.csv` (`main.py:206`). One junk character therefore fabricates a qualified lead and simultaneously destroys a real one.
  The producer side is broken too. `scrapers/osm.py:95-96` (and the identical `scrapers/wikidata.py:103-104`) do:
  ```python
  if website and not website.startswith("http"):
      website = "https://" + website
  ```
  `startswith("http")` is not `startswith("http://")`, so (a) `" https://x.com"` — a leading space, extremely common in hand-edited OSM tags — becomes `"https:// https://x.com"`, a control-character URL that `requests` rejects; and (b) `"httpbin.org"`, a perfectly good scheme-less host, is left scheme-less because it *starts with* `http`. Both then land in the table above and become `UNKNOWN` forever.
- **Trigger:** OSM node tagged `website=https://bakery.com ` (trailing space), or `website=httpbin.org`.
- **Fix:** one predicate, used everywhere, instead of four independent truthiness tests:
  ```python
  def usable_website(value) -> bool:
      return isinstance(value, str) and bool(_URL_RE.match(value.strip()))
  ```
  with `_URL_RE = re.compile(r"^https?://[^\s/]+\.[^\s/]+", re.I)` — the same shape `dedup.py:117` (`_URL_OK`) already uses for its `_validity` ranking. Apply it at `enricher.py:231`, `main.py:199-200`, `enricher.py:107`, and fix `osm.py:95`/`wikidata.py:103` to `startswith("http://") or startswith("https://")` after `.strip()`.

### S2 — "We never made a request" and "the server refused us" are both recorded as `UNKNOWN`, so the reachability metric is unfalsifiable

- **Where:** `enricher.py:231` (no validation) → `enricher.py:175-178` (`except Exception: return UNKNOWN`) → `enricher.py:250` (`... else None`) → counted at `enricher.py:266`
- **Breaks:** every row in the "silently wrong" block of Part A ends up in the same bucket as a genuinely unreachable host. `"www.bakery.com"`, `"   "`, `123`, `"n/a"` and a DNS failure are indistinguishable in `website_live`, in the `[Enricher] N live / M dead / K unreachable` line (`:271`), and in `main.py:217` (`unknown = sum(1 for r in with_websites if r.get("website_live") is None)`). An operator watching `unreachable` climb to 40% cannot tell whether the network broke, the targets are dead, or the scraper's own URL normalisation is broken — and those three demand opposite responses. The 200 KB cap's sibling problem is the same shape: contacts past the cap are silently never found, and a `LIVE` row with no contacts looks identical to a site that genuinely publishes no email.
- **Trigger:** any of the Part A "silently wrong" rows; then read the summary at `enricher.py:271`.
- **Fix:** keep the tri-state (it is right, and `043-scoring-upgrade.md` rightly insists on it) but make it four-state by *reason*, not by outcome: distinguish `NO_URL` (never had a usable URL → `website` normalised to `None`), `UNREACHABLE` (transport failure after a validated URL), `BLOCKED` (403/429/challenge), and `UNKNOWN` for everything else. One extra `probe_error: str` field — the pattern `067-error-hierarchy` and `064-retry-backoff-design` argue for elsewhere — makes the summary line auditable.

### S2 — A declared-but-unknown charset turns an HTTP 200 live site into `UNKNOWN`, losing its contacts and its +10

- **Where:** `enricher.py:189` (`html = raw.decode(r.encoding or "utf-8", errors="replace")`)
- **Breaks:** `errors="replace"` guards against *malformed bytes*, but not against an unknown *codec name*. `bytes.decode("utf8mb4")` raises `LookupError`, and `except Exception` at `:190` converts that into `UNKNOWN` — for a site that just answered **HTTP 200**. Verified on this machine:

  | declared charset | Python codec? | `r.encoding` → `website_live` |
  |---|---|---|
  | `charset=utf8mb4` | **no** | `None` (UNKNOWN) |
  | `charset=ISO-8859-6-I` (Arabic logical) | **no** | `None` (UNKNOWN) |
  | `charset=ISO-8859-6-E` (Arabic visual) | **no** | `None` (UNKNOWN) |
  | `charset=x-user-defined` | **no** | `None` (UNKNOWN) |
  | `charset=ISO-8859-8-E` | no | `None` (UNKNOWN) |
  | `charset=` (empty) | — | `True` (falls back via `or`) |
  | `charset=UTF-8`, absent, `windows-1256`, `euc-kr` | yes | `True` |

  `utf8mb4` is the charset emitted by `header("Content-Type: text/html; charset=utf8mb4")`, i.e. by a large share of PHP/MySQL small-business sites — exactly this pipeline's target market — and Python has no such codec alias. `ISO-8859-6-I/E` are the historical Arabic HTML charsets, i.e. legacy Lebanese and Saudi pages. Cost of the bug: a confirmed-live site loses `+10` `lead_score`, loses all four extracted contacts, and is counted as `unreachable` in the operator summary.
  The irony is that `requests`' own `Response.text` property wraps this exact call in `except (LookupError, TypeError): content = str(self.content, errors="replace")` **[library]**. `enricher.py:189` deliberately bypasses `.text` to get the streaming cap (the right call), and in bypassing it also bypassed requests' codec fallback. That is the real lesson: the fix for one defect silently removed another.
- **Trigger:** a 200 response carrying `Content-Type: text/html; charset=utf8mb4`.
- **Fix:** try the declared codec, then fall back — never let a codec lookup failure become a liveness verdict:
  ```python
  try:
      html = raw.decode(r.encoding or "utf-8", errors="replace")
  except LookupError:
      html = raw.decode("utf-8", errors="replace")
  ```
  and record `r["website_encoding_fallback"] = True` so the misconfiguration is visible rather than swallowed.

### S2 — Pre-pool input validation is unguarded: a bad `records` or `workers` value aborts the whole run

- **Where:** `enricher.py:231` (list comprehension, no guard) and `enricher.py:237` (`ThreadPoolExecutor(max_workers=workers)`, no guard)
- **Breaks:** the *inside* of the pool is firewalled (`:242-248`), but the two lines that create and populate it are not. `check_websites([None])` raises `AttributeError: 'NoneType' object has no attribute 'get'` from inside a list comprehension with no useful context; `check_websites(None)` raises `TypeError`; a generator argument raises `TypeError: 'generator' object is not subscriptable` at `:249` — i.e. **after** the fetch has already been dispatched, so it fails mid-flight with a half-mutated list. All of these propagate out of `enrich()` (`:337`) and kill `main()` before a single CSV is written. `workers=0` and `workers=-1` raise `ValueError` — and only *after* printing the reassuring `Fetching 1 websites — liveness + contacts (0 workers)` line, so the log's last words before the traceback are a lie about the configuration.
  Every one of these violates the declared signature `list[dict]`, so a caller *shouldn't* do them — but the whole point of the rest of this function is that it refuses to trust its input. It is inconsistent to be total over network responses and partial over the argument list.
- **Trigger:** `check_websites(records, workers=0)` → `ValueError: max_workers must be greater than 0`; one malformed row (e.g. a future CSV row where a field parsed to a non-dict) → `AttributeError`.
- **Fix:** validate once, up front, and skip rather than abort:
  ```python
  if not isinstance(records, list):
      raise TypeError(f"records must be a list, got {type(records).__name__}")
  if not isinstance(workers, int) or workers < 1:
      raise ValueError(f"workers must be a positive int, got {workers!r}")
  targets = [(i, r["website"]) for i, r in enumerate(records)
             if isinstance(r, dict) and usable_website(r.get("website"))]
  ```
  Move the `[Enricher] Fetching N websites` print **after** both checks. And because the function genuinely mutates its argument, document that in the docstring (`""` — see S3-d).

### S3 — `304 Not Modified` is classified `LIVE`

- **Where:** `enricher.py:182` (`if status >= 400: return DEAD`) — everything below 400 falls through to `return LIVE` at `:222`
- **Breaks:** a 3xx that `requests` does not follow (304 carries no `Location`) is read as a body-less success and recorded as a **live site with zero contacts**, which is indistinguishable from a JS shell (the "live but empty" case `097-enrichment-depth-strategy` / 083#66 flags). Executed: `304` → `website_live=True`.
- **Trigger:** any host answering `304` to an unconditional GET.
- **Fix:** `if not (200 <= status < 300): return DEAD if status >= 400 else UNKNOWN, contacts`.

### S3 — `dead_count`/`live_count` do not require a website, so the summary can report dead sites that have no website

- **Where:** `enricher.py:264-265` vs `:266`
- **Breaks:** `unknown_count` correctly requires `r.get("website")`; `live_count` and `dead_count` do not. A record whose `website` was dropped during the merge but whose `website_live=False` survived independently (both are `_VOLATILE` and ranked per-field by `_pick`, `dedup.py:114,159-187`) is counted as a dead site. The number an operator reads at `main.py:222-223` can therefore exceed the row count of `with_websites.csv`'s dead subset.
- **Fix:** make all three consistent: `if r.get("website") and r.get("website_live") is ...`.

### S3 — `workers` is unvalidated and echoed verbatim into the log

- **Where:** `enricher.py:235`, `:237`
- **Breaks:** `workers=1.5` silently spawns 2 threads **[library]**; `workers=None` silently becomes `min(32, cpu+4)`; `workers=True` becomes 1. The print at `:235` interpolates whatever it was given, producing log lines like `(1.5 workers)` and `(None workers)` that a future operator will spend time on.
- **Fix:** coerce once (`workers = max(1, min(int(workers or 40), 64))`) before the print.

### S3 — The no-target path is completely silent

- **Where:** `enricher.py:232-233` — `return records` fires **before** the first `print`
- **Breaks:** executed: `check_websites([])` and `check_websites([{"website": None}])` produce **zero stdout**. `main.py:186` prints `Enriching records (website liveness + contacts)...` unconditionally, so a run in which enrichment did nothing is byte-identical, in the log, to a run in which enrichment was skipped or crashed. `040-observability` argues for a run manifest; this is the cheapest possible instance of it — one `print` before the early return.
- **Fix:** move the `[Enricher] Fetching N websites` print above the `if not targets: return records`, or add an explicit `[Enricher] 0 records have a website — skipping liveness pass`.

### S3 — In-place mutation, aliased return, no URL de-duplication

- **Where:** `enricher.py:233`, `:250`, `:253-259`, `:276`
- **Breaks (all executed):** `check_websites(x) is x` → `True`; a shallow `records.copy()` shares dicts and observes every mutation; the same dict listed twice is fetched twice; and **5 records sharing one URL produce 5 identical HTTP requests** because `targets` (`:231`) is per-record, not per-URL. The last one is an amplification vector: duplicates are common after a website is shared across branches of one group, and each duplicate is another concurrent request at the same host from a pool with no per-host limiter — the documented trigger for the 403/429 → `DEAD` misclassification below.
- **Fix:** document the mutation in the docstring; de-duplicate `targets` on a normalised URL (`url.split("#")[0].rstrip("/").lower()`) with a `dict[str, list[int]]` fan-out, and write each verdict to every index that shared it. `053-caching-layer.md:189,195` already reaches this conclusion for the cross-run case — this is the same fix applied within the run.

---

## Not a bug, but worth knowing

- **`UNKNOWN` is genuinely a good design and it was earned.** `enricher.py:164-166` and `145-156` are explicit that a blocked crawler is not a broken site, and `:303-304` / `pitch_recommender.py:36,57` honour the distinction. Given that `verify=False`, `status < 500` liveness and a module-level `Session` were the state of this code at `99493b9`, the current tri-state is a large, real improvement. My findings are about the *inputs to* that design, not the design.
- **The network boundary is genuinely total.** Across 33 malformed input values and 16 hostile responses, `enricher.py:176`, `:190`, `:196` and `:244` absorbed **every** failure. No unhandled exception reached the caller from inside the pool. (As noted, this makes `083-SYNTH-s1-backlog.md:81` stale.)
- **`file://`, `javascript:`, `data:` and `mailto:` are all inert.** They have no requests adapter, so they become `UNKNOWN` without reading a local file or executing anything. Combined with `verify=True` at `:175`, the TLS hole is genuinely closed.
- **Unicode is a non-issue for this function.** Arabic IDN hosts (`https://مطعم.الزبون.com`), emoji in hosts and paths, UTF-16 bodies and mixed Arabic/emoji bodies all handled cleanly: IDNA via requests, percent-encoding for paths, and the contact regexes are ASCII-only so legacy `windows-1256` mojibake (correctly tolerated by `errors="replace"` and the `"utf-8"` fallback at `:189`) costs nothing.
- **Truncation at the 200 KB cap cannot corrupt the string.** `errors="replace"` means a body cut mid-UTF-8-sequence yields `U+FFFD`, not an exception. (The *size* of the resulting string is the S1-a problem, not its validity.)
- **Whitespace inside the URL is rejected, not injected.** `"https://a.com/\r\nHost: evil"` hits requests' control-character guard → `UNKNOWN`. No header injection, no request smuggling.
- **`False` as a `website` value round-trips into a real bug — but not here.** `load_master` maps `""` → `None` (`main.py:87-89`), yet a CSV round-trip of `False` re-reads as the truthy **string** `"False"`, which would then be fetched. Worth checking once the pipeline ever writes a non-string into `website`; the `usable_website()` predicate in S2-b closes it for free.

## Cross-references (already in the corpus — not counted as new findings)

- **403 / 429 / Cloudflare challenge → `DEAD` → Tier 1 rebuild pitch on a healthy site.** This is the correct current behaviour per `043-scoring-upgrade.md` (dead must mean *server-confirmed*), and it is already the **#1 item** in `083-SYNTH-s1-backlog.md:11` with 9 corroborating reports. My edge lens adds only the concrete trigger: `enricher.py:182` has no `Retry-After` inspection and no blocked-page fingerprint, so the 40-worker burst *this same function configures* at `:237` is what manufactures the 429s it then misreads as dead sites.
- **`mailto:` with `?subject=` → `email='info@b.com?subject=book&body=hi'`, and `mailto:info` → `email='info'`.** Executed above. Already filed at `025-enricher-regex-precision.md:79`.
- **`@sedo.com` in a parked page → `instagram='sedo.com'`.** Executed above. The bare `@` alternative at `enricher.py:131` is already filed at `025:287`.
- **SSRF via crowdsourced `website` tags.** `026-enricher-ssrf-security.md`, `054-politeness-and-ssrf.md`.
- **Cross-run re-fetching and duplicate-URL single-flight.** `053-caching-layer.md:9,189,195`.

## Recommended order of work

1. **S1-a — move the cap to the decoded stream** (`enricher.py:188-189`). One-line-shaped fix, closes a run-killer, and it is the difference between `026`'s S1 being fixed and merely moved. Add a per-run byte budget so 40 bombs cannot land at once.
2. **S1 — stop overwriting `website_live` on `UNKNOWN`** (`enricher.py:250`). Preserves the cumulative master's strongest signal and makes the pipeline's history additive instead of destructive.
3. **S2-b — one `usable_website()` predicate** across `enricher.py:231`, `enricher.py:107`, `main.py:199-200`, plus `startswith("http://")` in `osm.py:95` / `wikidata.py:103`. Protects the primary product list (`without_websites.csv`) from phantom and lost leads.
4. **S2-d — `LookupError` fallback on the charset** (`enricher.py:189`). Two lines; recovers live sites and their contacts today.
5. **S2 — validate `records` and `workers` before the pool** (`enricher.py:231,235,237`), and move the log line after validation.
6. **S2-c — record the failure *reason*, not just the tri-state**, so `[Enricher] N live / M dead / K unreachable` (`:271`) becomes a diagnosable number.
7. **S3 sweep** — 304 handling (`:182`), consistent counting (`:264-266`), `workers` coercion (`:235`), the silent no-target path (`:232`), and same-run URL de-duplication with the mutation contract documented (`:233,276`).
