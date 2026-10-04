# 388 — `check_websites` contract: declared vs. actual

**Target:** `check_websites` at `enricher.py:225`
**Lens:** contract
**Reads:** `enricher.py` (full), `scrapers/base.py`, `scrapers/osm.py`, `scrapers/wikidata.py`,
`scrapers/google_places.py`, `main.py`, `dedup.py`, `cli.py`, `pitch_recommender.py`,
`httpclient.py`, `tests/test_lead_signal.py`, `pyproject.toml`, `.github/workflows/scrape.yml`

---

## Verdict

`check_websites` declares `list[dict]` and mutates it in place, and it is the **sole writer**
of `email`, `instagram`, `whatsapp`, `linkedin` and `website_live` in the entire pipeline.
It performs **zero validation** on anything it writes: `BusinessRecord` (`scrapers/base.py:5-28`)
is satisfied at the type level while being violated at the semantic level, and nothing in the
repository — not the type checker, not the linter, not CI, not a single test — is positioned
to notice. The concrete consequence is that a CSS at-rule on the page becomes a fabricated
Instagram handle, which becomes a sales-ready contact (`main.py:141-144`), which becomes the
pitch **"Website launch + capture their existing audience"** for a business that has no
audience (`pitch_recommender.py:65-66`). That is a false claim written into the document a
salesperson reads.

The second, independent problem is that `website_live` is a 3-state field written
destructively. `bool | None` cannot express "we could not reach it today but proved it live
last month", so a single timeout permanently overwrites a good verdict in the cumulative
master — which the CI then re-uploads over the Drive copy.

---

## The contract surface, as declared vs. as practiced

### Declared

| Site | Declaration | Location |
|---|---|---|
| input | `records: list[dict]` | `enricher.py:225` |
| `workers` | `int = 40` | `enricher.py:225` |
| output | `list[dict]` | `enricher.py:225` |
| record shape | `BusinessRecord` (22 keys) | `scrapers/base.py:5-28` |
| worker contract | `_fetch_website(url: str) -> tuple[str, dict]` | `enricher.py:161` |
| outcome | `LIVE`/`DEAD`/`UNKNOWN` module constants, all bare `str` | `enricher.py:154-156` |

### Actual

| Question | Answer |
|---|---|
| Is `BusinessRecord` ever used as a type? | **No.** `check_websites` (`enricher.py:225`), `completeness_score` (`enricher.py:101`), `lead_score` (`enricher.py:283`), `enrich` (`enricher.py:326`), `dedup.dedup` (`dedup.py:197`) and `main.resolve_country` (`main.py:50`) all take bare `dict`. `BusinessRecord` is instantiated at `scrapers/osm.py:98`, `scrapers/wikidata.py:106`, `scrapers/google_places.py:281` and never referenced as a type. |
| Would a type checker catch it? | **No, and it isn't run anyway.** `[tool.mypy] strict = true` is configured (`pyproject.toml:64-68`) and `mypy>=1.11` is a dev dep (`pyproject.toml:26`), but `dict` is `dict[Any, Any]`, so `list[dict]` accepts literally any shape — even under `--strict`. And `.github/workflows/scrape.yml` installs only `requirements.txt` (`requests`, `beautifulsoup4`, `lxml`) and runs `python main.py`; there is no `ruff`, no `mypy`, and no `pytest` step. |
| Does the record shape stay open in practice? | **No, it is structurally open.** `dedup._merge` unions key sets (`dedup.py:190-194`), so a merged record's keys are the union of whatever each source emitted; `main.load_master` (`main.py:86-104`) returns a `csv.DictReader` row with whatever columns the CSV on Drive happens to have. Two of the three producers do their own normalization; nothing enforces it for the fourth path (the CSV). |
| Does the output keep the declared types? | **Yes for `website_live`** (real `bool`/`None`). **No** for the four contact fields — see S1. |
| Does the function mutate its argument? | **Yes, and returns the same object.** `enricher.py:233` (early return) and `enricher.py:276` both `return records` — the identical `list`. `enricher.py:337` then does `records = check_websites(records)`, which rebinds an alias, not a copy. `main.py:187`'s `records` is already mutated when line 189's loop runs. |
| Can a caller violate the contract silently? | **Yes, and silently by design** — `enricher.py:176` catches bare `Exception`, so *any* input-shape violation becomes `UNKNOWN`, which is documented at `enricher.py:151-153` as a statement about the network ("DNS failure, TLS error, timeout, connection reset, Cloudflare challenge"). It is not a statement about whether a request was ever attempted. |

---

## Inventory: every field `check_websites` reads or writes

Exhaustive, from `enricher.py:225-276`:

| Field | Read | Written | Line(s) | In `BusinessRecord`? | Declared type | Type actually written |
|---|---|---|---|---|---|---|
| `website` | yes | no | 231, 266 | ✅ `base.py:15` | `str \| None` | — |
| `website_live` | yes | **yes** | 250, 264, 265, 266 | ✅ `base.py:16` | `bool \| None` | `bool \| None` — correct |
| `email` | yes | **yes** | 252, 253, 267 | ✅ `base.py:14` | `str \| None` | `str` — but **not necessarily an address** (S1) |
| `instagram` | yes | **yes** | 254, 255, 268 | ✅ `base.py:18` | `str \| None` | `str` — but **not necessarily a handle** (S1) |
| `whatsapp` | yes | **yes** | 256, 257, 269 | ✅ `base.py:19` | `str \| None` | `str`, well-formed by regex, but not E.164-normalized |
| `linkedin` | yes | **yes** | 258, 259, 270 | ✅ `base.py:20` | `str \| None` | `str` — anchored to `linkedin.com/company/`, the safest of the four |

**Answer to "list every field this function reads or writes that is not in the TypedDict":
none.** All six are declared. That is precisely the problem — the declared surface is exactly
right and completely unenforced, so the mismatch shows up as *semantic* drift rather than as a
type error. `check_websites` is also the **only** writer of `email`/`instagram`/`whatsapp`/
`linkedin` in the pipeline (the three scrapers set only OSM `contact:email`/Wikidata `mailto:`),
so it is the sole admission point for four of the eleven contact columns.

For contrast, the one place where a real declared-vs-actual mismatch exists and is not caught:
`BusinessRecord` declares `lead_score: int` and `completeness_score: int` as **non-optional**
(`base.py:25`, `base.py:28`), but `main.load_master` assigns `None` to both when the CSV cell
fails `int(float(...))` (`main.py:96-101`). That `None` flows straight through `check_websites`
(which never reads those fields) into `main.py:206`'s `r.get("completeness_score", 0) >= 1` —
see S3-4.

---

## Findings

### S1 — `check_websites` is the sole admission boundary for four contact columns and validates nothing

- **Where:** `enricher.py:250-259` (the writes), fed by `enricher.py:199-220`.
- **Breaks:** `_fetch_website` returns whatever its regexes produced and `check_websites`
  copies it into the record with no plausibility check. `_EMAIL_BLACKLIST` (`enricher.py:138-141`)
  is a 7-entry domain blacklist, not a validator — and the codebase already owns a real one,
  `dedup._EMAIL_OK` (`dedup.py:116`), used **only for merge ranking** (`dedup.py:124-125`),
  never for admission. So values that fail the project's own definition of an email address are
  admitted anyway, and values that *pass* it while still being unusable are admitted too.

  Verified offline against the exact regexes in `enricher.py:199-204`:

  | Input HTML | `contacts["email"]` | passes `dedup._EMAIL_OK`? |
  |---|---|---|
  | `<a href="mailto:?subject=Contact us">Mail</a>` | `?subject=contact` | **no** |
  | `<a href="mailto:info@cafe-lb.com?subject=Hi%20there">` | `info@cafe-lb.com?subject=hi%20there` | **yes** |
  | `<a href="mailto://info@cafe-lb.com">` | `//info@cafe-lb.com` | **yes** |

  The second row is the dangerous one: the query string survives validation, so `dedup._validity`
  awards it a `+3` bonus and it becomes the **winning** `email` value in every future merge.

  The `instagram` writer is worse. `_INSTAGRAM_RE` (`enricher.py:131`) has an unanchored `@`
  alternative, and `_IG_BLACKLIST` (`enricher.py:142`) contains no CSS or email tokens.
  Verified:

  | Input HTML | `contacts["instagram"]` |
  |---|---|
  | `<a href="mailto:info@cafe-lb.com">Contact</a>` | `'cafe'` |
  | `<style>@media (max-width:600px){…}</style>` | `'media'` |
  | `<style>@font-face{…}</style>` | `'font'` |
  | `<p>Follow us on X @BeirutFood</p>` | `'beirutfood'` |
  | `<p>hello world</p>` (no handle at all) | `None` |

  The regex-precision defect itself is already documented in
  `docs/audits/006-enricher-regex-precision.md:14-19` and
  `docs/audits/025-enricher-regex-precision.md:18-37`. **What is not documented anywhere is the
  contract consequence**, which is the whole reason the defect reaches the customer:

  1. `enricher.py:111-112` — `completeness_score += 1` for `record.get("instagram")`.
  2. `enricher.py:294-295` — `lead_score += 10`.
  3. `main.py:141-144` `has_any_contact` — `len(instagram) > 3`; `"media"` is 5 → the row is
     declared to have a contact channel and enters `sales_ready.csv` (`main.py:202-205`).
  4. `main.py:201` — `with_social` is True.
  5. `pitch_recommender.py:39-41, 62-66` — `has_social` is True, so a business with **no website
     and no social presence** is pitched
     **`"Website launch + capture their existing audience"`** instead of
     `"Full digital launch (brand + website + social setup)"` (`pitch_recommender.py:90`).

  A salesperson reads that cell and believes the prospect has an existing audience to capture.
  They do not. That is a false claim in the product, produced by a `bool | None`-typed field
  being filled with a string that satisfies the type and destroys the meaning.

- **Trigger:** Any fetched page whose HTML contains `@media`, `@font-face`, `@keyframes`,
  `@supports`, `@import`, or an email at a hyphenated domain. That is the majority of
  WordPress / Wix / Squarespace pages, and `<style>` blocks sit before every footer.
- **Fix:** Validate at the write boundary in `check_websites`, not in the regex — reject any
  candidate that fails `dedup._EMAIL_OK` after stripping `?...`, restrict handles to
  `instagram.com/<handle>` matches, and never write a contact field with a plausibility score of
  zero. A `Contact(TypedDict)` with a validated constructor would make this unrepresentable.

### S2 — `website_live` is written destructively; `bool | None` cannot express "last known good"

- **Where:** `enricher.py:250`.
- **Breaks:** `r["website_live"] = ...` is unconditional. It never reads the value the record
  arrived with, so it overwrites a proven verdict with `None` whenever this run's fetch fails.
  `enricher.py:151-153` is emphatic that UNKNOWN "is NOT a pitch signal" — and the write
  *deletes the pitch signal we already had* rather than declining to assert one.

  The field is in `dedup._VOLATILE` (`dedup.py:114`), so on the next run the degraded value is
  treated as a fresh observation and wins. `main.py:150` reads the cumulative master,
  `main.py:179-180` merges it with fresh scrapes, `main.py:209` writes back, and
  `scrape.yml` then runs `rclone copy data/ "gdrive:leads/" --include "*.csv"` — so the loss is
  permanent, not merely in-run.

  Downstream cost per affected record: `lead_score` loses +10 (`enricher.py:300-302`), and three
  branches of `recommend_service` (`pitch_recommender.py:70, 73, 95`) all require
  `website_live is True` — a live 4.2★ site with <20 reviews drops from
  `"SEO audit + visibility upgrade"` to whatever the lower tiers return.

  It does **not** move rows in or out of `sales_ready.csv`, because `main.has_any_contact`
  (`main.py:137-145`) does not read `website_live`. I checked specifically for that and it does
  not, which is why this is S2 rather than S1 — but it does corrupt the score, the pitch column,
  and the operator-facing live/dead/unreachable counts.
- **Trigger:** Run N returns `LIVE` for `https://cafe-lb.com`. Run N+1 hits a TLS timeout at
  8s (`enricher.py:175`) → `enricher.py:176` → `UNKNOWN` → `enricher.py:250` writes `None`.
  `all_businesses.csv` now has a blank `website_live` for a site that worked a month ago.
- **Fix:** Never overwrite a known verdict with `None` — only assign when `outcome != UNKNOWN`,
  or widen the field to a 4-state (`live`/`dead`/`unknown`/`last_known_live`) and keep the
  observation timestamp next to the verdict.

### S2 — `BusinessRecord` is decorative; nothing in the repository is positioned to enforce it

- **Where:** `scrapers/base.py:5-28` vs. `enricher.py:225`; `pyproject.toml:64-68`;
  `.github/workflows/scrape.yml`.
- **Breaks:** The project declares a 22-field record type, configures `mypy --strict`
  (`pyproject.toml:64-68`) and `ruff` with pyflakes `F` rules (`pyproject.toml:43-46`), installs
  both as dev dependencies (`pyproject.toml:25-26`) — and then runs neither. CI installs
  `requirements.txt` (3 pins, no `mypy`, no `ruff`, no `pytest`) and executes `python main.py`.

  Even if mypy *were* run it would not help: `check_websites(records: list[dict])` takes
  `dict[Any, Any]`, which is compatible with every possible shape. The TypedDict is therefore
  not a weak contract, it is a **non-contract**.

  Two live symptoms of the missing enforcement, both in this file's blast radius:
  - `enricher.py:4` — `from urllib.parse import urljoin, urlparse`. **Neither name is used
    anywhere in the repository** (verified by grep). This is exactly the F401 that the
    configured-but-unrun ruff would report on line 1 of CI. A dead import of the two functions
    that would have solved S2-3 is itself the evidence that the normalization was written and
    then dropped.
  - `base.py:25, 28` declare `lead_score: int` / `completeness_score: int` as non-optional;
    `main.py:96-101` writes `None` into both. Nothing complains.
- **Trigger:** Any code change that adds a 23rd field to a scraper record and forgets to add it
  to `main.FIELDS` (`main.py:33-40`) — `write_csv(extrasaction="ignore")` (`main.py:125`) drops
  it silently. Or the reverse: a field removed from `FIELDS` becomes `None` for every record,
  every run, permanently.
- **Fix:** Annotate the pipeline with `BusinessRecord` (`check_websites(records: list[BusinessRecord]`
  → `list[BusinessRecord]`) and add `ruff check && mypy . && pytest` as the first CI steps.
  Until mypy runs, adding annotations changes nothing.

### S2 — `_fetch_website`'s return contract is `tuple[str, dict]` with an undeclared inner shape, consumed by subscript outside the `try`

- **Where:** `enricher.py:161`, `enricher.py:168`, `enricher.py:242-259`.
- **Breaks:** three separate problems on one interface.
  1. `-> tuple[str, dict]` declares the payload as an untyped `dict`. The consumer uses
     **subscript**, not `.get()`: `contacts["email"]` (`enricher.py:252`), `contacts["instagram"]`
     (254), `contacts["whatsapp"]` (256), `contacts["linkedin"]` (258). If `_fetch_website` ever
     omits one key, that is a `KeyError`.
  2. The `try` at `enricher.py:242-248` wraps **only** `future.result()` (line 243). Lines
     249-259 — including every contact write — are outside it. A `KeyError` there propagates
     out of `check_websites`, out of `enrich`, out of `main.main`, and kills the job. Under
     `.github/workflows/scrape.yml`'s `timeout-minutes: 300` that is a multi-hour run discarded
     after the entire HTTP pass has completed.
  3. `outcome` is a bare `str` compared with `==` to three constants (`enricher.py:250`). Any
     value that is not exactly `"live"` or `"dead"` — a typo, a renamed constant, a new outcome —
     **silently degrades to `None`/UNKNOWN**, which is the exact conflation
     `enricher.py:145-147` calls "the single worst bug in the pipeline". There is no
     `assert outcome in (LIVE, DEAD, UNKNOWN)`.
  4. The fallback literal at `enricher.py:247-248` is a hand-copied duplicate of the one at
     `enricher.py:168`. Adding a fifth contact type to one and not the other produces (2) with
  no test to catch it.
- **Trigger:** Add a `tiktok` extractor to `_fetch_website` and build `contacts` as
  `contacts: dict = {}` populated only on a hit. Every page without a TikTok link now raises
  `KeyError('email')` at `enricher.py:252`.
- **Fix:** `type Outcome = Literal["live", "dead", "unknown"]`,
  `@dataclass class FetchOutcome: kind: Outcome; contacts: Contacts` with
  `class Contacts(TypedDict): email: str; instagram: str; whatsapp: str; linkedin: str`.
  One literal, one shape, no duplication.

### S2 — URL normalization lives in the scrapers, not at the boundary that needs it; the fix is imported and unused

- **Where:** `enricher.py:175` (the call), `enricher.py:176` (the catch-all),
  `enricher.py:4` (the dead import), vs. `scrapers/osm.py:95-96` and
  `scrapers/wikidata.py:103-104` (two hand-rolled copies).
- **Breaks:** `_fetch_website(url: str)` requires an absolute, fetchable URL, but it never
  checks. Three producers feed it and only two normalize: OSM prefixes `https://`
  (`osm.py:95-96`), Wikidata prefixes `https://` (`wikidata.py:103-104`), Google returns
  `websiteUri` (`google_places.py:291`) which carries a scheme. The **fourth** producer is
  `main.load_master` (`main.py:86-104`), which re-reads the cumulative master and performs **no
  URL normalization of any kind**.

  `requests` raises `MissingSchema` for a schemeless URL **before opening a socket**. That is
  caught by the bare `except Exception` at `enricher.py:176` and returned as `UNKNOWN` — which
  `enricher.py:271` then prints as `"unreachable"`. That statement is false: we never tried.
  The site is never learned about, on this run or any future run, because the degraded value is
  written back into the master and re-read every time.
- **Trigger:** `data/all_businesses.csv` contains a row with `website` = `www.abc-lb.com`
  (hand-edited, or written by any pre-`osm.py:95` build — git history shows the normalization
  postdates the first production runs). From that row forward it is permanently
  `website_live=<blank>` and contributes nothing but noise to the live/dead/unreachable counts.
- **Fix:** Normalize in `_fetch_website`, once, for every producer:
  `url = url if "://" in url else "https://" + url.lstrip("/")`, and treat "not a URL" as a
  distinct outcome from "could not reach the host" — or use the already-imported
  `urlparse(url).scheme` check and skip the fetch with an explicit `MALFORMED` outcome. Then
  delete the two scraper-side copies.

### S3 — `website_live` has two independent decoders that agree only by convention

- **Where:** `main.py:102-103` and `cli.py:105-113`.
- **Breaks:** `main.load_master` maps anything that is not exactly `"True"` / `"False"` to
  `None`. `cli.cmd_stats` independently compares the raw CSV cell to `"True"` and `"False"` and
  counts "unreachable" as *blank*. A cell reading `"true"`, `"TRUE"`, `"yes"` or `"1"` —
  trivially produced by a hand-edit or a Google Sheets round-trip — is counted in **none** of
  the three buckets by `cmd_stats` (line 110 requires `.strip()` to be falsy), and silently
  becomes `None` in `load_master`. `cli.py:80` bills this command as "the first thing to check
  when a run looks wrong", so the tool an operator reaches for first under-reports on exactly
  the row it was asked about. `tests/test_lead_signal.py:200, 211-226` pins the current
  `"True"` encoding, so the duplication is deliberate-but-unenforced rather than accidental.
- **Trigger:** Open `all_businesses.csv` in Sheets, retype a `website_live` cell as `TRUE`,
  save, upload to Drive. `leadminer stats` reports that site in none of live/dead/unreachable.
- **Fix:** One `parse_website_live(cell) -> bool | None` next to `load_master`, used by both.

### S3 — `workers` is declared, unvalidated and unreachable from any caller

- **Where:** `enricher.py:225`, `enricher.py:237`, `enricher.py:337`.
- **Breaks:** `enrich` calls `check_websites(records)` with no second argument, so `workers=40`
  is the only reachable value — the parameter is decorative. Nothing validates it either;
  `ThreadPoolExecutor(max_workers=0)` raises `ValueError`, and `workers` is a plain `int` so
  `check_websites(records, workers="40")` would fail deep inside the stdlib.
- **Trigger:** `check_websites(records, workers=0)`.
- **Fix:** Drop the parameter, or route it from a single config value and assert `workers >= 1`.

### S3 — `check_websites` returns its input object and skips its own reporting on the empty path

- **Where:** `enricher.py:232-233` vs. `enricher.py:264-276`.
- **Breaks:** Two small things.
  1. Both return paths return the **same list object**, so `enricher.py:337`'s
     `records = check_websites(records)` rebinds an alias. Any caller that kept a pre-call
     snapshot for comparison sees it mutated. Defensible for a 500k-row pipeline; it just needs
     to be written down.
  2. The `not targets` early return at line 233 skips the entire summary block, so a run in
     which every website was blank/absent prints **nothing** — indistinguishable in the log from
     a run that never called the function.
- **Trigger:** `check_websites([{"name": "x"}])` → no output at all.
- **Fix:** Document the in-place mutation in the docstring; move the early return after the
  summary, or print an explicit "0 websites to check".

### S3 — The internal live/dead/unknown counts use three different guards and can contradict the run summary

- **Where:** `enricher.py:264-266` vs. `main.py:215-217`.
- **Breaks:** `live_count` and `dead_count` iterate over **all** records, but `unknown_count`
  additionally requires `r.get("website")`. Any record that retains a stale `website_live`
  without a website inflates live+dead past the number of records that have websites, and
  `enricher.py:271` prints a triple that does not reconcile. `main.py:215-217` gets this right
  by filtering through `with_websites` — so the two summaries disagree on the same run.
- **Trigger:** Any record that survives `dedup` with `website=None` and `website_live` set from
  the other side of a merge (`dedup._pick`, `dedup.py:159-187`, picks `website` and
  `website_live` independently).
- **Fix:** Compute all three counts over the same filtered list `main.py:215-217` uses, or
  return the counts from `check_websites` instead of printing them.

### S3 — the write path of the function under audit has zero test coverage, and CI runs no tests

- **Where:** `tests/test_lead_signal.py:66` imports `DEAD, LIVE, UNKNOWN, infer_region,
  lead_score` — never `check_websites` or `_fetch_website`. `.github/workflows/scrape.yml` has
  no `pytest` step.
- **Breaks:** Every claim above is untested. `check_websites`'s only real behavioural guarantee
  — the LIVE/DEAD/UNKNOWN separation — is tested *downstream* at
  `tests/test_lead_signal.py:76-99` by hand-constructing dicts, so the function that produces
  those values has no test at all. A one-line change to `enricher.py:250` that inverted the
  tri-state would keep the suite green.
- **Trigger:** Any change to `enricher.py:250-259`.
- **Fix:** Four unit tests with `_fetch_website` monkeypatched: (a) UNKNOWN does not overwrite
  an existing `website_live`; (b) a rejected email candidate is never written; (c) an
  `outcome` outside the enum raises instead of degrading to UNKNOWN; (d) the empty-target path
  still reports.

---

## Not a bug, but worth knowing

- **The field inventory is clean.** All six fields `check_websites` touches exist in
  `BusinessRecord`. There is no rogue key, and `write_csv`'s `extrasaction="ignore"`
  (`main.py:125`) is not dropping anything this function produces.
- **`whatsapp` is the best-behaved of the four contact writers.** `_WHATSAPP_RE`
  (`enricher.py:132-135`) is anchored, and the digit class plus `{7,15}` bound guarantees a
  well-formed number. It is not run through `dedup.normalize_phone`, so `whatsapp` and `phone`
  can hold two different spellings of the same number — cosmetic, not a contract break.
- **`linkedin` is likewise safe**: `_LINKEDIN_RE` (`enricher.py:136`) is anchored to
  `linkedin.com/company/` and `enricher.py:219` rejects the three reserved slugs.
- **The thread-local session is genuinely correct now.** `get_session()` is called *inside* the
  worker at `enricher.py:175`, so each of the 40 threads builds its own
  `requests.Session` (`httpclient.py:61-71`) rather than sharing a mutable one. `docs/audits/004`
  is closed.
- **`check_websites` re-fetches the same URL once per record.** Two rows for the same domain
  produce two GETs. At 500k cumulative rows that is real duplicated load. Not a contract issue —
  it belongs to the caching lens — but the interface (`list[dict] in, list[dict] out`) has
  nowhere to hang a per-URL cache, which is worth knowing before scaling workers.
- **`load_master` short rows are benign.** `csv.DictReader` fills absent trailing fields with
  `None`, so `main.py:87-89`'s `""`→`None` normalization is a no-op for them and `check_websites`
  treats them as "no website". Extra trailing fields land under the `None` key and are dropped by
  `extrasaction="ignore"`. Neither path crashes.
- **`main.py:206`'s `r.get("completeness_score", 0) >= 1`** is a latent trap rather than a live
  bug: `.get`'s default only fires for an *absent* key, so a present-but-`None` value would raise
  `TypeError: '>=' not supported between instances of 'NoneType' and 'int'`. It is unreachable
  today only because `enricher.py:349` unconditionally assigns an `int`. If `enrich` is ever
  split or short-circuited, this fires *after* the 300-minute scrape and *before* any CSV is
  written.

---

## Recommended order of work

1. **S1** — put validation at the write boundary in `enricher.py:250-259`. Reuse `dedup._EMAIL_OK`
   after stripping `?...`; require `instagram` candidates to come from an `instagram.com/<handle>`
   match. Nothing else in this list matters as much, because it is the only defect that produces
   a false claim to a customer.
2. **S2 (tri-state)** — stop overwriting a known verdict with `None` at `enricher.py:250`, and
   record the observation timestamp next to it.
3. **S2 (type contract)** — annotate `check_websites`/`enrich`/`dedup` with `BusinessRecord`, then
   add `ruff check && mypy . && pytest` as the first three steps of `.github/workflows/scrape.yml`.
   Until mypy runs, annotations are documentation. This also removes the `enricher.py:4` dead
   import and makes the next item possible.
4. **S2 (worker contract)** — replace `tuple[str, dict]` with a dataclass + `TypedDict` carrying
   a `Literal` outcome, and move `enricher.py:249-259` inside the `try`.
5. **S2 (URL normalization)** — normalize once in `_fetch_website` with the already-imported
   `urlparse`, delete the copies at `osm.py:95-96` and `wikidata.py:103-104`, and give
   "malformed URL" an outcome distinct from UNKNOWN so `enricher.py:271` stops printing a lie.
6. **S3s** — one `website_live` decoder shared by `main.py:103` and `cli.py:107`; validate or
   remove `workers`; document the in-place mutation; make the three counts use one filter;
   add the four unit tests from the last finding.