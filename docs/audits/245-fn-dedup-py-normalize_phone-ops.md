# 200 — `normalize_phone` under a long cron run: every silent failure, ranked

## Verdict

`normalize_phone` (`dedup.py:33`) is pure and deterministic, which is good news and
also the problem: it is the only function in the pipeline that decides *which
business records exist*, and it emits **nothing** — zero `print`, zero `logging`,
zero counters (`dedup.py` imports only `re` and `unicodedata`, `dedup.py:1-2`).
Worse, `main.py:194` overwrites the raw phone with the normalized form *before the
master CSV is written*, so every defect below is stamped into the one durable
artefact the business runs on, cannot be repaired by re-running, and rolls back
exactly one successful run. On a cron whose only output gate is
`ROWS >= 100` (`scrape.yml:80`), a run that destroys 199 of 200 records passes.

### Scope note on the lens questions

The lens asks what this function prints, what it swallows, what happens when the
network drops halfway, when a dependency returns garbage, when it is called with an
empty list, and when it is interrupted. Three of those do not apply literally, and
saying so is part of the finding:

- **Network:** `normalize_phone` does zero I/O. It cannot observe a dropped socket.
  The network question lands one stage earlier (`main.py:163-168`) and one stage
  later (`main.py:187`), and the consequence — a partial or empty batch is
  normalized and republished anyway — is S1-2 below.
- **Empty list:** genuinely safe. `normalize_phone([])` returns `""` via the
  falsiness guard at `dedup.py:40`; `dedup([])` returns `[]`. No crash. The
  *adjacent* hazard — an empty `sales_ready.csv` being uploaded over the Drive
  master with no gate on it — is S2-6.
- **Interruption:** `normalize_phone` is called at `dedup.py:205`, inside
  `dedup(combined)` at `main.py:180`, which is entirely in memory. A kill anywhere
  between `main.py:150` and `main.py:209` discards it. This is the *fastest-detected*
  failure in the system (a red X after 5 hours), which makes it the control case
  against which the silent ones should be read.

Every `Trigger:` below was executed against the current working tree (importing
`dedup` directly, and `main` with the repo's own dependency stub from
`tests/test_lead_signal.py:23-63`). No network calls were made.

---

## Findings

### S1 — Zero-filled numbers still collapse into one shared dedup key, contradicting the function's own docstring

- **Where:** `dedup.py:63` (`return "+" + cc + digits.lstrip("0")`), guarded only by
  the length test at `dedup.py:45`; consumed as an index key at `dedup.py:205-210`.
- **Docstring vs. behaviour:** `dedup.py:34-38` states the contract as *"Normalize to
  E.164, or return `""` if the input is not a usable number … Junk used to normalize
  to `"+"`, which made unrelated businesses share one dedup key."* The regression is
  **not fixed** — the shared key is now `"+961"` instead of `"+"`. Any input of 7+
  zero digits satisfies `len(digits) >= _MIN_DIGITS` (`dedup.py:45`) and
  `lstrip("0")` empties it, leaving the bare country code as a truthy key.
- **Breaks:** every all-zero-phone record in a country collapses into
  `phone_index["+961"]` and is `_merge`d (`dedup.py:207-208`) into one row. Names,
  addresses, websites and emails of N distinct businesses are collapsed onto one
  survivor. `_pick` has no trust entry for `name`/`address` that would resist this
  beyond the generic tiebreak at `dedup.py:184`.
- **Trigger (verified):**
  ```
  normalize_phone("0000000",       "LB") -> '+961'
  normalize_phone("00000000000",   "LB") -> '+961'
  normalize_phone("000-000-0000",  "SA") -> '+966'
  ```
  5 records with phone `"0000000"`, country `"LB"`, distinct names/emails/websites
  → `dedup()` returns **1** row. 200 such records → **1** row.
- **Detection is actively anti-correlated with severity:** `main.py:220` prints
  `Total unique businesses : 1`. A collapsing row count reads as *dedup working*.
  The only automated gate, `ROWS >= 100` (`scrape.yml:80`), is applied to
  `all_businesses.csv`, which retains every other row, so it passes.
- **Note on prior reports:** this is also filed as S1 in
  `084-SYNTH-adversarial-fix-review.md:31`. I re-verified it and confirm it, and add
  the operational half that report does not cover: the placeholder is now durable
  (see S1-2) and the console metric moves the wrong way.

### S1 — `main.py:194` destroys the raw phone before the master is written, so a run that scrapes *nothing* still corrupts and republishes the master

- **Where:** `main.py:192-194` (`r["phone"] = normalize_phone(raw_phone, …)`) then
  `main.py:209` (`write_csv(DATA_DIR / "all_businesses.csv", records)`), uploaded to
  the Drive master at `scrape.yml:89`.
- **Breaks:** `all_businesses.csv` is described in `BRIEF.md:51` as the cumulative
  master, all time. It is also the **only** durable copy of the data: the download
  step at `scrape.yml:56` fetches `gdrive:leads/*.csv` at `--max-depth 1`, so the
  per-run snapshots written to `gdrive:leads/data/$TIMESTAMP/` (`scrape.yml:88`) are
  never downloaded or verified. Once a run replaces a raw phone with its normalized
  form, the raw string is gone. Deploying a fix next week does not restore it; it
  only stops new damage.
- **The worst case needs no scrape at all.** `main.py:167-168` catches every scraper
  exception and continues. If all three fail (quota, network, 429), `raw == []`,
  `combined == master`, and the pipeline still runs `dedup`, `resolve_country`,
  `enrich`, and the `main.py:194` rewrite, then uploads. A zero-yield run rewrites
  every phone in the master.
- **Trigger (verified), simulating an all-scrapers-failed run:**
  ```
  master phones: ['0501234567', '03 123 456', '000-000-0000']
  after the run: ['+961501234567', '+9613123456', '+961']
  ```
  A Saudi national mobile is now permanently labelled `+961`. The
  `ROWS >= 100` gate passes, because the master is large.
- **Self-perpetuation, verified:** the corrupted value is stable and re-detects as
  the wrong country forever, because `resolve_country` only pattern-matches `+966`
  / `00966` (`main.py:70`) and the string now says `+961`:
  ```
  run1 normalize_phone('0501234567', country=None) -> '+961501234567'
  run1 resolve_country({'country':None,'phone':'+961501234567'}) -> 'LB'
  run2 normalize_phone('+961501234567', 'LB')       -> '+961501234567'
  control resolve_country({'country':None,'phone':'+966501234567'}) -> 'SA'
  ```
  Control group: `normalize_phone('96170123456', 'lb')`, `'SAU'`, `'KSA'`,
  `'Lebanon'`, `'JO'`, `''`, `'  '`, `'XX'` all yield `+961501234567` for the Saudi
  input `0501234567` — `_COUNTRY_CODES.get(country, "961")` (`dedup.py:48`) makes
  every unrecognised country code silently Lebanon, including typo variants of `"SA"`.
- **Rollback depth is exactly one successful run**, and only via the unverified
  timestamped snapshot. There is no manifest, no checksum, no key-version marker
  (`100-run-manifest-design.md:265` notes the key-change hazard but does not close it).
- **Fix:** write the normalized value to a **new** column (`phone_e164`) and leave
  `phone` as scraped; make `dedup()` keys derived, never destructive; and gate
  publication on `raw` being non-empty (`if not raw: exit 1`).

### S2 — Extension and trailing-garbage digits are folded into the number, and the result scores as a premium contact

- **Where:** `dedup.py:44` (`re.sub(r"\D", "", raw)`) with no extension handling and
  no post-normalisation length check.
- **Breaks:** two effects. (a) One business splits into two permanent dedup keys.
  (b) The longer string survives into the CSV as an undialable number that is
  scored as a real contact channel, because both scoring paths use a bare
  `len(digits) >= 7` test — `enricher.py:292` (`+15` to `lead_score`) and
  `main.py:142` (`has_any_contact` → `sales_ready.csv`).
- **Trigger (verified):**
  ```
  normalize_phone('+961 3 123 456 ext. 9', 'LB') -> '+96131234569'
  normalize_phone('+961 3 123 456',      'LB') -> '+9613123456'   # same business, 2 keys
  normalize_phone('03 123 456 ext. 9',    'LB') -> '+96131234569'
  normalize_phone('050-123-4567 x123',    'SA') -> '+966501234567123'
  ```
  `has_any_contact({'phone': '+966501234567123', 'industry_priority': 'high'})`
  returns `True` (verified) — the row ships in `sales_ready.csv` with a 15-digit
  number a rep cannot dial. This is also why these rows are *not* caught by the
  junk gate: they are longer than any legitimate number, not shorter.
- **Fix:** strip extension markers (`ext|x|#|poste|داخلي`) before digit extraction,
  store the extension separately, and assert `7 <= len(subscribers) <= 9` after the
  country code; drop to `""` on violation.

### S2 — No upper bound and no numbering-plan check: arbitrary junk becomes a truthy key *and* a scored contact

- **Where:** `dedup.py:45` is the only length gate (a floor); `dedup.py:63` will
  accept any digit string.
- **Breaks:** `str()`-coerced values and out-of-region numbers become
  indistinguishable from real E.164 in the output CSV, are scored `+15`, and pass
  `has_any_contact`. Related to `055-phone-validation-deep.md:19` (which flags
  unallocated prefixes) but adds the *scoring* consequence, which I verified and
  which no existing report states.
- **Trigger (verified):**
  ```
  normalize_phone('+1 800 555 0199',      'LB') -> '+96118005550199'   # US toll-free, Lebanese code
  normalize_phone('+44 20 7946 0958',     'LB') -> '+961442079460958'
  normalize_phone('+33 1 42 68 53 00',    'LB') -> '+96133142685300'
  normalize_phone('12345678901234567890', 'LB') -> '+96112345678901234567890'
  normalize_phone(2**70,                  'LB') -> '+9611180591620717411303424'
  ```
  `main.py:70-73` then searches for `+966`/`+961` anywhere in the string, so
  `'+96118005550199'` resolves to `LB` and the fabrication is confirmed, not caught.
  Note `2**70` is `str()`-coerced at `dedup.py:42` — an integer leaking into the
  phone column becomes a 22-digit key rather than an error.
- **Fix:** after normalisation, assert the result matches
  `^\+(961|966)\d{7,9}$`; return `""` otherwise and count it.

### S2 — `str(phone)` at `dedup.py:42` replaced a crash with silent corruption

- **Where:** `dedup.py:42` (`raw = str(phone)`).
- **Breaks:** this line was added to fix the `TypeError` recommended in
  `020-error-handling-partials.md:24,209`. It does fix the crash — by widening
  acceptance. Structured garbage now type-coerces into a *valid* dedup key instead
  of raising, so the failure moved from loud (S1, caught in seconds) to silent
  (indefinite). `re.sub(r"\D", …)` strips quotes, brackets and braces, so container
  types pass the digit test.
- **Trigger (verified):**
  ```
  normalize_phone(['96170123456'],        'LB') -> '+96170123456'
  normalize_phone({'phone':'96170123456'},'LB') -> '+96170123456'
  normalize_phone(70123456,               'LB') -> '+96170123456'
  normalize_phone(70123456.0,             'LB') -> '+961701234560'   # phantom trailing 0
  normalize_phone(float('nan'),           'LB') -> ''
  ```
  The float case is the dangerous one: `70123456` and `70123456.0` are the *same
  business* but produce different keys (`+96170123456` vs `+961701234560`), so a
  single float-typed record splits itself into a duplicate that recurs in every
  future run against the cumulative master. `load_master` casts only `lat`, `lon`,
  `rating` and the three int fields (`main.py:43-44, 90-101`) — `phone` is never
  cast, so any non-string that reaches the column survives the CSV round trip.
- **Fix:** `if not isinstance(phone, str): return ""` before coercion. Rejecting is
  correct here; there is no meaningful phone value hidden inside a float.

### S2 — `dedup.py` has no instrumentation at all, unlike the stage that does network I/O

- **Where:** `dedup.py` in full — `print` count 0, `logging` count 0. Compare
  `enricher.py:235` (`Fetching N websites`), `enricher.py:261-262` (progress every
  200), `enricher.py:271-275` (live/dead/unreachable + per-channel contact counts).
- **Breaks:** this is the finding that sets the detection latency for every other
  one. The stage that touches the network and can fail in a thousand ways is
  instrumented; the stage that decides record existence is mute. Specifically there
  is no way to distinguish, from the console or the artefacts, between:
  - 40 records rejected as junk (`""`),
  - 40 records merged into an existing key,
  - 40 records keyed under a fabricated `+961`/`+966` placeholder,
  - 40 records keyed under a cross-border-rewritten number.
  All four print `After merge + dedup: N unique businesses` (`main.py:181`).
  `main.py:181` also reports the *input-merged* count, not the merge count, so it
  cannot be differenced against `main.py:220`.
  `040-observability.md:309-312` proposes the right counters generically; the gap
  is that none of them exists, and that `dedup.py` — the highest-consequence
  transform in the pipeline — has zero of the ten prints the rest of the codebase
  has.
- **Fix:** four counters emitted at the end of `dedup()` — `phone_keyed`,
  `name_keyed`, `phone_merged`, `phone_rejected_junk` — plus a distinct counter for
  keys that are a bare country code (`key == "+" + cc`), which is the S1-1 canary.

### S2 — An empty subset CSV is uploaded over the Drive master with no gate

- **Where:** `scrape.yml:74-83` validates `data/all_businesses.csv` only. `scrape.yml:89`
  (`rclone copy data/ "gdrive:leads/" --include "*.csv"`) uploads all five files.
- **Breaks:** `sales_ready.csv` — the file a salesperson opens — is written from a
  list comprehension at `main.py:202-205` over records whose contact channels come
  straight from `normalize_phone`'s output (`main.py:142`) and from the unvalidated
  WhatsApp harvest at `enricher.py:214` (`"+" + m.group(1).lstrip("+")`, which never
  passes through `normalize_phone` at all — see `087-SYNTH-test-gap.md:446`). If a
  run's scrapers and enrichment both fail, `sales_ready.csv` is written with a
  header and zero rows (`write_csv` prints `Written: … (0 records)` at `main.py:134`
  and raises nothing) and that empty file replaces the good one on Drive. The
  `all_businesses.csv` gate still passes because the master is large.
- **Fix:** assert non-zero row counts on `sales_ready.csv` and
  `qualified_businesses.csv` in the validation step, and never upload a file with
  fewer rows than the copy already on Drive without an explicit override.

### S2 — A national trunk zero after the country code splits one business into two keys, permanently

- **Where:** `dedup.py:58-60` returns `"+" + digits` immediately when the number
  starts with a known code, with no trunk-zero removal (contrast `dedup.py:63`,
  which does `lstrip("0")` for the bare-national branch).
- **Breaks:** the same business keyed two ways, in a *cumulative* master, so the
  duplicate is re-emitted every week and grows the CSVs forever.
- **Trigger (verified):**
  ```
  normalize_phone('+961 01 234 567', 'LB') -> '+96101234567'
  normalize_phone('01 234 567',      'LB') -> '+9611234567'
  ```
  `dedup()` on those two records returns **2** rows, one per source (verified).
  `tests/test_lead_signal.py` has no case for this; `031-test-suite-design.md:210`
  specifies one (`test_normalize_phone_retains_trunk_zero_creating_duplicate_keys`)
  that has not been written.
- **Fix:** after matching a known country code, strip one leading `0` from the
  subscriber part for `961`/`966`.

### S3 — `had_plus` is a substring test over the whole raw string

- **Where:** `dedup.py:43` (`had_plus = "+" in raw`), which gates the `00` /
  national-prefix branches at `dedup.py:50` and `dedup.py:53`.
- **Breaks:** a `+` anywhere — in an extension marker, a typo, a pasted note —
  suppresses the `00` prefix stripping and yields a doubled country code.
- **Trigger (verified):**
  ```
  normalize_phone('0+96137123456',       'LB') -> '+96196137123456'
  normalize_phone('+00961 3 123 456',   'LB') -> '+9613123456'   # correct by luck
  ```
- **Fix:** `had_plus = raw.strip().startswith("+")`.

### S3 — `_KNOWN_COUNTRY_CODES` contains the two-digit `"20"`, hijacking any national number beginning `20`

- **Where:** `dedup.py:20` (`"20",  # Egypt`), tested as a bare prefix at
  `dedup.py:58-59`.
- **Breaks:** the prefix loop has no length awareness, so a two-digit entry
  outranks the three-digit entries for any national number starting `20`.
- **Trigger (verified):** `normalize_phone("2012345", "LB")` → `'+2012345'`;
  `normalize_phone("20 123 4567", "LB")` → `'+201234567'`. Confirmed as S3 in
  `084-SYNTH-adversarial-fix-review.md:167`; I re-verified and agree with the
  severity. Low real-world frequency for Lebanese/Saudi numbering plans, but it is a
  live landmine for the next person who adds a short code to the tuple.
- **Fix:** drop `"20"`, or require the match to be a full country-code prefix
  followed by a valid subscriber length.

### S3 — `country` has no trust entry, so `_pick` decides it by lexicographic string comparison

- **Where:** `country` is absent from all three `_SOURCE_TRUST` tables
  (`dedup.py:97-110`), so `_trust_for("country", rec)` returns `_DEFAULT_TRUST == 1`
  (verified) for every source, `_validity` returns `1` (`dedup.py:140`), and the
  final tiebreak at `dedup.py:184` is `str(value)`.
- **Breaks:** when two merged records disagree about country, `"SA"` wins over
  `"LB"` because `S > L` (verified). `country` is a *key input* to
  `normalize_phone`, so an accidental tiebreak is worse here than for any other
  field. Scope is narrow — `main.py:184` overwrites `country` with
  `resolve_country` before it reaches the CSV — but the merged value is live at
  `dedup.py:228`, where it decides whether a name-keyed record is suppressed as a
  phone-keyed duplicate.
- **Trigger (verified):** `_merge({'country':'LB', …}, {'country':'SA', …})['country']`
  → `'SA'`.
- **Fix:** add `"country"` to `_SOURCE_TRUST` per source, and special-case it in
  `_pick` alongside `source`/`scraped_at` (`dedup.py:173-177`).

---

## Ranked silent-failure table

Ordered by **time until a human notices**, longest first. "Detectable by" is the
cheapest signal that would have caught it, and the current state of each.

| # | Silent failure | Detectable by | Latency today |
|---|---|---|---|
| 1 | Placeholder `+961`/`+966` mass-merge (S1-1) | master row count dropping week-over-week; a rep finding a stranger's website on a row | **weeks** — row count *falls*, which reads as success; `scrape.yml:80` passes |
| 2 | Zero-yield / partial run rewrites and republishes the master (S1-2) | nothing; the run "succeeded" and gained 0 leads | **weeks** — needs a customer complaint about an unreachable number |
| 3 | Cross-border rewrite `"0501234567"` → `+961501234567` (S1-2) | a Saudi rep dialling a `+961` number; `country` column says LB for a Riyadh address | **1–3 days** if the file is used daily |
| 4 | Phone column blanked to `""` by short-junk rejection, shipped as `""` not `None` | a rep opening `sales_ready.csv` and finding no phone | **days** |
| 5 | Undialable 15-digit keys scoring `+15` and entering `sales_ready.csv` (S2-2, S2-3) | a dialled call that does not connect | **days–weeks** |
| 6 | Permanent duplicate rows from trunk zero / extensions / float coercion (S2-2, S2-7) | a human noticing the same business twice during review | **weeks** |
| 7 | Out-of-region numbers given `+961` (S2-3) | comparing a sample back to the source; never automated | **weeks–months** |
| 8 | `+` anywhere suppresses prefix stripping (S3-1) | dialling, or a source audit | **months** |
| 9 | `"20"` → Egypt (S3-2) | dialling | **months** |
| 10 | `country` decided lexicographically (S3-3) | a duplicate pair with mismatched `country` | **months** |
| — | Run exceeds `timeout-minutes: 300` (`scrape.yml:25`) | GitHub red X at 5h; Drive keeps the previous master; no artefact, since a job timeout kills the `if: always()` step at `scrape.yml:93` too | **seconds** |

The contrast in the last row is the point of this table. The only failure in the
system with a signal is the one that cannot corrupt anything.

**On the six-hour premise:** the job cannot run for six hours.
`timeout-minutes: 300` (`scrape.yml:25`) is five. A run that needs six is
SIGKILLed mid-`enrich` (`main.py:187`, the 40-thread crawl — by far the longest
phase), and because every CSV write happens at `main.py:209-213` with nothing
written before it, `dedup`'s merges and `normalize_phone`'s work are discarded
whole. The previous master survives because `write_csv` is atomic
(`main.py:121-133`), which is the correct trade — but it means the pipeline has no
resume point, so every timeout discards the full scrape, and the cost is invisible
because the Drive master is unchanged. If a six-hour run is genuinely required,
the timeout and the phasing (checkpoint after `dedup`, checkpoint after `enrich`)
both have to change.

**On a partial network failure:** if Overpass or Google dies after 3 of 5 hours,
`main.py:167-168` catches it and the run continues with whatever arrived. The
records that did arrive are normalized and merged; the ones that did not are
simply absent. Since the master is cumulative, the *dedup keys are unaffected* —
but the missing day's leads are indistinguishable from a day with no new
businesses, and `scrape.yml:80` only rejects a run below 100 total rows. A run
that fetched 5% of the normal yield still passes. That is the same
detection-latency class as row 1 above, one stage upstream.

---

## Not a bug, but worth knowing

- **`normalize_phone` is idempotent on its own output, which hides the damage.**
  `031-test-suite-design.md:467` proposes idempotency as a property test; it holds
  (`'+961501234567'` → `'+961501234567'`). But it is *wrong-stable*: run 1 writes
  `+961501234567` for a Saudi number and run 2 re-normalises it to the same wrong
  value, so the round trip through the master looks perfectly clean while carrying
  corruption forward. Idempotency is what makes the S1-2 lock-in invisible.
- **It is called three times per phone record.** `dedup.py:205` (index key),
  `dedup.py:228` (duplicate suppression), and `main.py:194` (CSV rewrite). `052-memory-streaming.md:174`
  flags the first two; the third is a separate pass with a *different* country
  argument (`resolve_country(r)` rather than `record.get("country") or "LB"`),
  which is the only reason the CSV and the dedup key can disagree. Not a
  correctness bug today because `resolve_country` is idempotent on an
  already-E.164 value — but it is a latent divergence if `normalize_phone` ever
  changes.
- **`dedup()` normalizes correctly for the key and stores the record raw**
  (`phone_index[key] = dict(record)`, `dedup.py:210`), which `002-dedup-merge-data-loss.md:338`
  and `012-main-orchestration-order.md:22` both confirm is right. The design is
  sound: derive for comparison, keep raw for output. **`main.py:194` is the line
  that breaks that contract**, by discarding the raw value 25 lines before the
  write.
- **The dedup key is a *silently versioned* artefact of the code.** Any change to
  `_KNOWN_COUNTRY_CODES`, `_MIN_DIGITS`, or the `country` default re-keys the entire
  cumulative master on the next run, with no version marker in the CSV and no
  migration. `100-run-manifest-design.md:265` names this; there is no key version in
  `FIELDS` (`main.py:33-40`).
- **`_MIN_DIGITS = 7` (`dedup.py:30`) rejects the emergency short codes 112/911 and
  junk equally** (both → `""`), with no way for a caller to tell them apart. Both are
  correctly excluded from dedup; the missing piece is the counter from S2-5 that
  would let someone see the rate.

---

## Recommended order of work

1. **Stop destroying raw data.** Write `normalize_phone`'s output to a new
   `phone_e164` column at `main.py:194`; keep `phone` as scraped. One-line change,
   and it converts every finding above from *permanent* to *next-run-fixed*.
   Backfill the existing master from `gdrive:leads/data/<last-good-timestamp>/`
   before shipping, because that is the only pre-corruption copy in existence.
2. **Fix the placeholder collapse at `dedup.py:63`** — return `""` when
   `digits.lstrip("0")` is empty. This is the single highest-yield one-liner in the
   file and is already agreed in `084:188`.
3. **Make `country` reach `normalize_phone`.** Move the `resolve_country` loop
   above `dedup(combined)` (`main.py:183-184` → before `:180`, as proposed in
   `085:49-58`), and expand the alias map so `"Saudi Arabia"`/`"sa"` do not fall
   through to the `"961"` default at `dedup.py:48`.
4. **Instrument `dedup()`.** Four counters (`phone_keyed`, `name_keyed`,
   `phone_merged`, `phone_rejected_junk`) plus a canary for keys equal to a bare
   country code. Print them next to `main.py:181`. Without this, nothing else in
   this list is observable and none of the fixes above can be verified in
   production.
5. **Validate the output string, not just the input length.** After
   `dedup.py:63`, assert `^\+(961|966)\d{7,9}$` and return `""` otherwise; strip
   extension markers before `dedup.py:44`; make `has_any_contact`
   (`main.py:142`) and `lead_score` (`enricher.py:292`) validate the string rather
   than counting digits, so a 22-digit junk key can never score `+15`.
6. **Reject non-`str` input at `dedup.py:42`** instead of coercing it. The `str()`
   that fixed the `TypeError` recommended in `020:209` is now the reason a
   `float` splits a business into two rows.
7. **Add the four missing regression tests** specified in `031-test-suite-design.md:210`
   (trunk zero), plus explicit cases for `"0000000"`, `"+961 3 123 456 ext. 9"`,
   `"0+96137123456"`, and `70123456.0`. All four currently fail.
