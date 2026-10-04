# 380 — `_fetch_website` contract: `tuple[str, dict]` is an unchecked promise

## Verdict

`_fetch_website` (`enricher.py:161`) is declared `-> tuple[str, dict]`, but every one of
its three contract surfaces — the input URL, the outcome token, and the contact dict —
is unconstrained, and its only consumer destructures that tuple **outside** the
`try/except` that was added to protect it. Any deviation is either silently recorded as
`website_live=None` or raises out of `check_websites` → `enrich` → `main()`, killing the
run after the CSVs would have been written. Nothing on the path from `scrapers/base.py`
to `main.py` would catch it: `enricher.py` never imports `BusinessRecord` at all.

The core defect is not that the shapes are loose today — they are currently correct —
it is that **the guard is on the wrong side of the boundary**. `enricher.py:242-248`
wraps `future.result()`; the six lines that actually consume the value sit at
`enricher.py:249-259`, naked.

## Declared vs. actual

| Surface | Declared | Actual in practice | Enforced? |
|---|---|---|---|
| `url` | `str` (`enricher.py:161`) | `r["website"]` from a `BusinessRecord` where it is `str \| None` (`base.py:15`); runtime gate is truthiness only (`enricher.py:231`) | No. No absolute-URL check, no scheme check, no `isinstance` check. |
| outcome | `str` | one of 3 module-level `str` constants `LIVE`/`DEAD`/`UNKNOWN` (`enricher.py:154-156`) | No. Not a `Literal`, not an `Enum`. Caller has an `else → None` catch-all. |
| contacts | `dict` | always exactly `{"email","instagram","whatsapp","linkedin"}` → all `str \| None`, initialized at `enricher.py:168` | No. Untyped `dict`; consumed by hard `[]` subscript at `enricher.py:252-259`. |
| return | `tuple[str, dict]` | 2-tuple on all 5 return paths (`178, 184, 192, 222`) | No. A 3-tuple unpacks silently — see S1-2. |
| `website_live` | `bool \| None` (`base.py:16`) | `True`/`False`/`None` (`enricher.py:250`) | Yes, but see S2-1: the *meaning* is not enforced, only the type. |

**Can a caller violate this silently? Yes, and it is the default behaviour.** Verified
offline by monkeypatching `enricher._fetch_website` (no network, no scraper run):

```
baseline (LIVE, full contacts)       -> OK   website_live=True
outcome is bool True                 -> OK   website_live=None     <-- silent
outcome is 'LIVE' (case typo)        -> OK   website_live=None     <-- silent
outcome is 'blocked' (4th)           -> OK   website_live=None     <-- silent
contacts missing 'email' key         -> RAISED KeyError: 'email'
contacts is None                     -> RAISED TypeError: 'NoneType' object is not subscriptable
returns a 3-tuple                    -> OK   website_live=None     <-- silent
returns a bare dict                  -> OK   website_live=None     <-- silent
contacts has extra key only          -> RAISED KeyError: 'instagram'
```

Four of nine shape variations destroy data with no error, no log line, and no
non-zero exit. Two kill the run.

---

## Findings

### S1 — The contact dict is consumed outside the `try/except`, so a shape change kills the run

- **Where:** `enricher.py:242-259`. The `try` spans only `future.result()` (line 243);
  the `except` ends at line 248. Lines 249-259 — `r["website_live"] = ...`,
  `contacts["email"]`, `contacts["instagram"]`, `contacts["whatsapp"]`,
  `contacts["linkedin"]` — are outside it.
- **Breaks:** `KeyError: 'email'` (or `'instagram'`, `'whatsapp'`, `'linkedin'`)
  propagates out of the `as_completed` loop, out of `check_websites`, through
  `enricher.py:337` (`records = check_websites(records)` — no guard), through
  `main.py:187` (`records = enrich(records)` — no guard), and out of `main()`.
  `write_csv` is not reached (`main.py:209-213`), so **all five CSVs are lost**,
  including the master the run was accumulating into. This is the same failure
  `docs/audits/029-error-handling-partials.md` filed as S1 and prescribed
  ("move lines 154-177 inside the try in `_fetch_website`"). The fix was applied
  inside `_fetch_website`; the consumption side was left open, so the same run-death
  is still reachable by a different door.
- **Trigger:** return `(LIVE, {})` from `_fetch_website` — e.g. after a refactor that
  builds the dict lazily, or a new "no contacts" early-return, or a cache layer
  (`docs/audits/053-caching-layer.md`) that stores `{}` for a non-2xx. Demonstrated:
  ```
  *** RAISED KeyError: 'email'
     B0 website_live= True     <- records processed before the death
     B1..B6 website_live= None
  ```
  The 40-worker pool is not the issue; the partial `records` mutation at lines 249-259
  means a crash leaves the in-memory set half-enriched, and nothing rolls back.
- **Fix:** subscript defensively — `contacts.get("email")` at `enricher.py:252-259`,
  and wrap 249-259 in the same `try` that already guards 243.

### S1 — Outcome is a bare `str` with an `else → None` catch-all, so any new outcome is silently erased

- **Where:** `enricher.py:154-156` (`LIVE = "live"` etc. are plain strings),
  `enricher.py:250`:
  `r["website_live"] = True if outcome == LIVE else (False if outcome == DEAD else None)`,
  and `enricher.py:251` (`if outcome == LIVE:`).
- **Breaks:** any outcome string that is not byte-identical to `"live"`/`"dead"` is
  recorded as `website_live=None` — the *unreachable* bucket — and its extracted
  contacts are dropped at lines 252-259 without a word of output. This is precisely the
  inversion the module's own comment at `enricher.py:151` was written to prevent
  ("BLOCKED / UNKNOWN … this is NOT a pitch signal"), but the type gives no way to
  represent `BLOCKED`, so it would be born silently misfiled.
- **Trigger:** three one-word edits, each of which passes the existing test suite:
  - `LIVE = "Live"` (case typo) → every live site becomes `website_live=None`.
  - `_fetch_website` returns `True` instead of `LIVE` (a natural "make it a bool"
    refactor) → same.
  - adding `BLOCKED = "blocked"` for Cloudflare challenges (`enricher.py:151`
    already names it) → every Cloudflare-protected site becomes "unreachable"
    instead of "blocked", and its contacts are thrown away.
  `tests/test_lead_signal.py:98-99` only asserts the three constants are *distinct*;
  it cannot catch any of these.
- **Fix:** `Outcome = Literal["live", "dead", "unknown"]` (or an `enum.StrEnum`),
  annotate `-> tuple[Outcome, ContactInfo]`, and make line 250's final branch
  `else: raise ValueError(outcome)` so a new token must be handled explicitly.

### S2 — `DEAD` is documented as "the site is broken" but is produced for 429 and for 5xx outages

- **Where:** `enricher.py:182-184` (`if status >= 400: return DEAD, contacts`) versus
  the declared contract in the docstring at `enricher.py:149-151`: *"DEAD — server
  answered 4xx/5xx. The site exists and is broken: a real rebuild pitch."*
- **Breaks:** a Cloudflare 429 (rate limit) or a 503 (30-second blip) is filed as a
  confirmed rebuild target. Downstream: `lead_score` awards **+20**, the largest single
  award in the function (`enricher.py:303-304`), and `recommend_service` returns
  `"Website rebuild + maintenance"` — the top of the funnel (`pitch_recommender.py:57-58`,
  whose comment reads *"the strongest pitch in the database"*). 40 workers
  (`enricher.py:225`) with no per-host throttle make 429s likely against Lebanese
  shared-hosting providers and multi-tenant builders. The `str` outcome type cannot
  carry "rate-limited" as distinct from "broken".
- **Trigger:** a site behind Cloudflare that 429s our 40-thread burst. One response
  is enough to produce a top-tier rebuild lead with a 20-point score bonus.
- **Fix:** `if status == 429 or 500 <= status < 600: return UNKNOWN, contacts`
  (retryable, per `httpclient._should_retry` at `httpclient.py:142-146`, which already
  draws this line); reserve `DEAD` for 400-428 and 430-499.

### S2 — `website_live` is a function of `website`, but `dedup._merge` picks the two fields independently

- **Where:** `dedup.py:190-195` (`_merge` calls `_pick` per key over `set(a) | set(b)`),
  `dedup.py:114` (`_VOLATILE = {"rating","review_count","website_live","website"}` —
  both fields are ranked on recency but *separately*), consumed at
  `enricher.py:250` and `enricher.py:303-304`.
- **Breaks:** the invariant `_fetch_website` establishes — *`website_live` is the verdict
  for **this** `website` URL* — is established inside one function call and destroyed by
  the next. `_pick` (`dedup.py:159-187`) chooses each field independently, so a merged
  row can carry a `website` from record A and a `website_live` from record B.
- **Trigger** (verified offline):
  ```python
  master = {... "website": "https://cafe-maroni-2019.example", "website_live": False,
             "source": "osm", "scraped_at": "2026-01-15T09:00:00+00:00"}
  fresh  = {... "website": "https://cafe-maroni.example",      "website_live": None,
             "source": "osm", "scraped_at": "2026-09-30T09:00:00+00:00"}
  dedup([fresh, master])[0]  ->  website = https://cafe-maroni.example   # fresh wins (recency)
                                 website_live = False                    # master's verdict, about the OLD url
                                 lead_score = 35   recommended_service = "Website rebuild + maintenance"
  ```
  The business relaunched on a new domain; we pitch them a rebuild of a site they no
  longer run. `tests/test_lead_signal.py:303-318` asserts `website_live` is treated as
  volatile *independently* — the merge behaviour is tested and blessed; the coupling is not.
- **Mitigation that exists:** in a full `main()` run, `dedup` (line 180) precedes
  `enrich` (line 187), so `check_websites` re-fetches and overwrites at
  `enricher.py:250`. **But `cli.cmd_score` (`cli.py:247-277`) does not call `enrich`.**
  It loads a CSV, recomputes `lead_score`, and writes it back with `website_live`
  untouched — so any CSV containing a fabricated pair is re-scored and re-exported
  with the fabricated rebuild pitch intact.
- **Fix:** in `_merge`, treat `website_live` as derived: drop it from both inputs and
  recompute `merged["website_live"] = None` whenever `merged["website"] != ` the URL the
  verdict came from. Cheapest version: have `dedup` clear `website_live` unconditionally
  (it is re-derived by `enrich` anyway) and have `cli.cmd_score` refuse to score rows
  whose `website_live` is not `None`.

### S2 — `LIVE` is returned for a 200 KB truncated read, and truncation is indistinguishable from "no contacts found"

- **Where:** `enricher.py:158` (`_MAX_BODY_BYTES = DEFAULT_MAX_BODY_BYTES` = 200 000,
  `httpclient.py:54`), `enricher.py:188` (`r.raw.read(_MAX_BODY_BYTES, decode_content=True)`
  — a hard cut, not a "stop at a chunk boundary" read), `enricher.py:222` (`return LIVE, contacts`).
- **Breaks:** the return type has no way to say *"I only read the first 200 KB"*. LIVE
  is the strongest verdict in the system: `lead_score` +10 (`enricher.py:301-302`) and
  `recommend_service` returns `"SEO audit + visibility upgrade"`
  (`pitch_recommender.py:70-71`). Contacts past the cut are invisible, and a regex
  match straddling byte 200 000 is destroyed mid-token. A JS-shell single-page app whose
  `<head>` is 180 KB and whose `<footer>` holds the `mailto:` link returns
  `(LIVE, {all None})` — byte-identical to a site that genuinely has no contact details.
  Deterministic per site, so the same lead loses the same contact every run, forever.
- **Trigger:** any site whose contact markup sits after the first 200 KB — common on
  template-heavy WordPress pages, which put the footer/CTA after large inline CSS and
  webfont blocks.
- **Fix:** return a third element (`truncated: bool`) or add `UNKNOWN`-adjacent state;
  at minimum log the count. `docs/audits/081-tech-stack-signals.md:13` already proposes
  widening this return value — do both in one change.

### S2 — `mailto:` query strings are written verbatim into the `email` column, and `dedup` rates them maximally valid

- **Where:** `enricher.py:199`
  (`re.findall(r'href=["\']mailto:([^"\'>\s]+)', ...)` — stops only at `"`, `'`, `>` or
  whitespace, so everything up to the closing quote is captured), `enricher.py:201-203`
  (the blacklist check compares `addr.split("@")[-1]`, which therefore also contains the
  query string), `enricher.py:253` (`r["email"] = contacts["email"]`).
- **Breaks:** `<a href="mailto:info@cafe.com?subject=Contact%20us">` yields
  `contacts["email"] == "info@cafe.com?subject=contact%20us"`. The declared type is
  `str | None` (`base.py:14`), which it satisfies, but the value is not an address. It
  then earns `_validity("email", ...) == 3` — the maximum — from
  `dedup._EMAIL_OK` (`dedup.py:116`), because `[^@\s]+` happily matches the query
  string. Verified:
  ```
  osm=admin@cafe-maroni.com    -> merged='info@cafe.com?subject=contact%20us'   GARBAGE WINS
  osm=contact@cafe-maroni.com  -> merged='info@cafe.com?subject=contact%20us'   GARBAGE WINS
  osm=owner@cafe-maroni.com    -> merged='owner@cafe-maroni.com'                real email wins
  ```
  The owner's real address is discarded on the alphabetical tiebreak (`dedup.py:184`)
  for any owner address sorting before `"info@"`. The garbage ships in
  `all_businesses.csv` and `sales_ready.csv`, and helpers will put it in an email client.
- **Fix:** strip the query/fragment before the blacklist test —
  `addr = addr.split("?", 1)[0].split("#", 1)[0]` at `enricher.py:200`, and anchor
  `_EMAIL_BLACKLIST` matching to the true domain (`addr.rsplit("@", 1)[-1]` *after* stripping).

### S3 — The 4-key contact shape is duplicated as a literal in two places with nothing tying them together

- **Where:** `enricher.py:168` (inside `_fetch_website`) and `enricher.py:247-248`
  (the `except` fallback inside `check_websites`). The two lists are textually identical
  and independently editable.
- **Breaks:** adding a fifth contact channel (a `tiktok` extraction is the obvious
  candidate) means editing two lists. Miss the fallback at 247-248 and every
  *exception-path* row raises `KeyError: 'tiktok'` — S1 all over again, but only for
  the rows that failed, i.e. non-deterministically.
- **Fix:** one module-level factory `_empty_contacts()` in `enricher.py`, used by both,
  typed as `ContactInfo(TypedDict)` with `total=True` so mypy flags a missing key.

### S3 — `instagram` has three different minimum-length contracts in one pipeline

- **Where:** `_fetch_website` enforces `len(handle) >= 2` (`enricher.py:208`) and writes
  a **bare handle** (`enricher.py:209`); `lead_score` accepts any truthy value
  (`enricher.py:294`, +10); `completeness_score` accepts any truthy value
  (`enricher.py:111-112`, +1); `main.has_any_contact` requires `len(instagram) > 3`
  (`main.py:141-144`). `osm.py:93` writes the raw `contact:instagram` tag, which is a
  bare handle in some entries and a full URL in others — the same `str | None` column
  holds two shapes.
- **Breaks:** a 2- or 3-character handle (permitted at `enricher.py:208`) scores +11
  across `lead_score` and `completeness_score` but does **not** make the row
  sales-ready, because `has_any_contact` wants 4+. The row looks like a lead and is
  filtered out of the product.
- **Fix:** normalize `instagram` to one canonical form (bare handle, or always a URL) in
  one place, and use one `len()` threshold everywhere.

---

## Off-schema surface (the explicit list the lens asked for)

**Record fields read or written by this path that are missing from `BusinessRecord`:
none.** All five writes are declared, all six reads are declared:

| Direction | Field | In `base.py`? |
|---|---|---|
| read | `website` | yes — `base.py:15` |
| read | `email`, `instagram`, `whatsapp`, `linkedin` | yes — `base.py:14, 18-20` |
| read | `website_live` | yes — `base.py:16` |
| write | `website_live` | yes — `base.py:16` |
| write | `email`, `instagram`, `whatsapp`, `linkedin` | yes — `base.py:14, 18-20` |

Reporting this plainly because it is the useful result: **the schema mismatch is not in
the record, it is entirely in the return value.** `enricher.py` never imports
`BusinessRecord` (grep: it appears only in `scrapers/{osm,wikidata,google_places}.py`
and `cli.py:46`), so `check_websites(records: list[dict])` (`enricher.py:225`) receives
`list[BusinessRecord]` and annotates it as `list[dict]`, and the entire half of the
pipeline after `scrape()` — dedup, enrich, score, recommend, export — is unchecked.

The off-schema surface is these three, none of which has a type:

1. `contacts` — a bare `dict` (`enricher.py:168`), semantically a `TypedDict` with
   4 required keys.
2. `outcome` — a bare `str` (`enricher.py:154-156`), semantically a 3-value `Literal`.
3. `_MAX_BODY_BYTES` truncation — an undeclared fourth element of the return
   (see S2 above).

## Where the return value is used in a way that breaks on an unexpected shape

| Site | Code | Breaks when |
|---|---|---|
| `enricher.py:243` | `outcome, contacts = future.result()` | return is not a 2-sequence → `ValueError` (caught at 244, degrades to UNKNOWN). Return is a `dict` with ≥2 keys → unpacks **keys** silently: `outcome='email'`, `contacts='a@b.com'` → `website_live=None`, contacts dropped. Verified. |
| `enricher.py:250` | `outcome == LIVE` / `== DEAD` | any other token → silently `None`. Verified for `True`, `"LIVE"`, `"blocked"`. |
| `enricher.py:251` | `if outcome == LIVE:` | same — a live site with a correct contacts dict loses all four contacts silently. |
| `enricher.py:252,254,256,258` | `contacts["email"]` etc. | any missing key → `KeyError` escaping the run. `contacts=None` → `TypeError`. Both outside the `try`. Verified. |
| `enricher.py:250` | writes `r["website_live"]` unconditionally | destroys a previously stored `False` verdict when the refetch returns `UNKNOWN`. Verified: a confirmed-dead rebuild lead scoring 35 silently becomes an unreachable row scoring 15 and pitching `"RTYLR commerce OS"` instead of a rebuild. The module comment at `enricher.py:296-299` fixed the *scoring* side of this (never award dead credit for UNKNOWN) but not the *storage* side. |
| `enricher.py:264-266` | counts by `website_live is True/False/None` | the counts are always consistent with the writes — but only because 250 is total. Once S1-2 lets a token through, the `[Enricher] N live / M dead / K unreachable` line at 271 reports the corruption with no indication anything is wrong. |

## Not a bug, but worth knowing

- **`url: str` is declared but never validated.** `enricher.py:231` gates on truthiness
  only, so any truthy non-string reaches `get_session().get(url, ...)` at line 175. I
  could not produce a reachable non-string from today's three scrapers (`osm.py:91-96`
  and `wikidata.py:96-104` prefix `https://` and yield `str`; `google_places.py:291`
  yields the API's `websiteUri` string; `main.load_master` at `main.py:85-105` only ever
  produces `str | None`). This is a latent hole, not a live bug.
- **But scheme-less URLs *are* reachable and are mislabeled.** `load_master`
  (`main.py:85-105`) does no URL validation on the way back in, so a hand-edited or
  third-party `all_businesses.csv` row with `website="www.cafe.com"` hits
  `requests` → `MissingSchema` → caught at `enricher.py:176` → `UNKNOWN` forever. Those
  leads are indistinguishable from genuinely-unreachable hosts. Note the codebase already
  has the URL contract — `_URL_OK` at `dedup.py:117` — and does not reuse it at the fetch
  boundary.
- **The `_fetch_website` body is correct about the failure taxonomy it was written for.**
  `verify=True` (line 175, with the reasoning at 170-174), the 200 KB cap (line 188), the
  `finally: r.close()` (193-197), and the three-way LIVE/DEAD/UNKNOWN split (154-156,
  178/184/192/222) are all right. Every finding above is about the *interface*, not the
  logic inside it.
- **`tests/test_lead_signal.py:98-99`** asserts `{LIVE, DEAD, UNKNOWN}` has 3 elements.
  That catches accidental aliasing but not a changed token, a non-str outcome, or a
  missing contact key — i.e. it does not cover any S1 in this report.
- `docs/audits/032-fixtures-offline-replay.md:386-399` specifies the test seam as
  `monkeypatch.setattr("enricher._fetch_website", fake)`. That is the most likely route
  by which a wrong-shaped value enters the pipeline in practice, which makes S1-1 and
  S1-2 live risks rather than theoretical ones.
- `docs/audits/054-politeness-and-ssrf.md:924-927` already proposes replacing this exact
  signature with a structured outcome. This report argues the same change with a
  narrower scope: no SSRF work required to land the typing fix.
- `BRIEF.md:57` says "The 22 columns". It is 23 — `base.py:6-28` declares 23 keys and
  `main.py:33-40` writes 23. Harmless, but the count is used when checking schema
  completeness, so it should be corrected wherever it is asserted.

## Recommended order of work

1. **`enricher.py:252-259` — `contacts.get(...)` and fold lines 249-259 into the
   existing `try`.** One-line-per-line change, removes the run-death. Do this first; it
   is independent of everything below.
2. **`enricher.py:250` — replace the `else` branch with `raise ValueError(outcome)`.**
   Turns every future S1-2 variant from silent data loss into a loud failure the moment
   it is introduced.
3. **Type the interface: `Outcome = Literal["live","dead","unknown"]` and
   `ContactInfo(TypedDict)` at `enricher.py:154-168`, with `_empty_contacts()` shared by
   lines 168 and 247-248.** This makes items 1 and 2 statically impossible to
   reintroduce and gives mypy a job at the boundary.
4. **`enricher.py:182` — 429 and 5xx → `UNKNOWN`.** Highest value per line in the whole
   report: stops false rebuild pitches on rate limits and blips.
5. **`enricher.py:200` — strip `?`/`#` from `mailto:` captures**, then tighten
   `dedup._EMAIL_OK` (`dedup.py:116`) to reject a `?` or `#`. Do both; fixing only the
   regex leaves the validator able to launder the next bad shape.
6. **`dedup._merge` — clear `website_live` on merge (`dedup.py:190-195`) and have
   `cli.cmd_score` (`cli.py:247-277`) refuse rows whose `website_live` is stale.** Fix
   the storage-side of the UNKNOWN-overwrites-DEAD problem while you are in there.
7. **`enricher.py:158,222` — report truncation** in the return value; fold in the `tech`
   dict from `docs/audits/081-tech-stack-signals.md:13` at the same time so the signature
   is widened once.
8. **Validate `url` against `_URL_OK` at `enricher.py:231`** and log rejects separately
   from UNKNOWN, so a malformed URL cohort stops hiding inside "unreachable".