# 329 — `_recency` (dedup.py:155) edge-case audit

## Verdict

`_recency` cannot crash, but it is far more dangerous than its three-line body
suggests: its return value is not a timestamp, it is a **ranking key** used at
`dedup.py:183` to arbitrate `website`, `rating`, `review_count` and `website_live`
(`dedup.py:114`). The body performs zero validation, zero normalisation and zero
length bound, so any `scraped_at` whose first character sorts above `'2'` wins
**every** volatile-field tiebreak permanently. Because the master CSV is
round-tripped through a human-writable Google Drive folder
(`.github/workflows/scrape.yml:56`, `:89`) and re-fed into the merge every run
(`main.py:150`, `main.py:179`), one junk cell is self-perpetuating forever — I
verified a BOM-prefixed stamp keeps a 2024 dead URL pinned to a business across
three simulated runs. Most dangerous single input: **`"\ufeff2026-10-03T12:00:00+00:00"`**
(a valid-looking timestamp with a leading U+FEFF).

---

## The function and its real contract

```python
# dedup.py:155-156
def _recency(record: dict) -> str:
    return str(record.get("scraped_at") or "")
```

**Sole call site** — `dedup.py:183`, inside the `rank()` tuple:

```python
# dedup.py:179-187
def rank(rec: dict, value) -> tuple:
    return (
        _trust_for(field, rec),
        _validity(field, value),
        _recency(rec) if field in _VOLATILE else "",
        str(value),  # deterministic final tiebreak
    )
return av if rank(a, av) >= rank(b, bv) else bv
```

So the implicit contract is **not** "return the timestamp". It is:

1. return a value that is **totally ordered by wall-clock time**, and
2. that ordering must be **identical across every producer** — the three
   scrapers (`scrapers/osm.py:32`, `scrapers/wikidata.py:67`,
   `scrapers/google_places.py:173`) *and* the master CSV (`main.py:150`),
4. for a **falsy** value, "infinitely old" is the correct answer, and
5. it must never raise.

There is no docstring and no assertion on `dedup.py:155-156`. Nothing checks any
of these. The declared type is `scraped_at: str` (`scrapers/base.py:27`), but
`main.py:85-105` (`load_master`) deliberately casts `lat/lon/rating` and
`review_count/completeness_score/lead_score` and pointedly **does not cast
`scraped_at`** — so `_recency` reads a raw, untyped CSV cell.

### Producers, verified

| Producer | Format | Anchored |
|---|---|---|
| All 3 scrapers | `datetime.now(utc).isoformat()` → `...+00:00`, width varies (see below) | `httpclient.py:220-229` |
| Master CSV re-import | whatever string the cell holds, `""`→`None` | `main.py:85-105`, `main.py:150` |
| Drive round-trip | file leaves the program and returns | `.github/workflows/scrape.yml:56`, `:89` |

### Can it crash? No — and that is worth stating plainly

I ran `_recency` against 21 hostile inputs. **Zero tracebacks.** More
importantly, `_recency` introduces **no new crash surface**: its only caller
touches the record first at `dedup.py:167` (`a.get(field)`) and early-returns on
missing values at `dedup.py:168-171`, so any record that would crash `_recency`
has already crashed `_pick` first. `_recency(None)` raises `AttributeError`, but
it is unreachable in practice. **Do not add defensive `try/except` here**; the
correct place for a guard is `load_master` (`main.py:85-105`).

---

## Findings

### S1 — `_recency` returns unvalidated master-CSV text as a total order, so one junk cell wins forever

- **Where:** `dedup.py:155-156`, consumed at `dedup.py:183`; amplified by
  `dedup.py:177` (`return max(av, bv)` for `scraped_at`) and `main.py:150` +
  `main.py:179` (master is merged back in every run).
- **Contract violated:** requirements 1, 2 and 3 above. `str()` guarantees a
  `str`, which makes the type safe, and thereby hides the fact that nothing
  guarantees the *content* is a timestamp.

- **Why any junk wins:** a canonical stamp starts with `'2'` (U+0032). Lexicographic
  comparison therefore means **any first character with a code point above
  `U+0032` beats every real timestamp in the years 2000–2999**. Exactly 19
  characters lose: ``space ! " # $ % & ' ( ) * + , - . / 0 1 2``. Everything else
  wins — all of `3-9`, all letters, `~`, DEL, **and every non-ASCII code point**.

- **Trigger (verified end-to-end).** One cell in `data/all_businesses.csv` reads
  `"\ufeff2026-10-01T09:00:00+00:00"`. Sources matched to `google_places` so
  trust and validity tie and `_recency` is the *sole* deciding factor:

  ```
  run 2: scraped_at='\ufeff2026-10-01T09:00:00+00:00'
          website=https://dead-2024.example  rating=3.1  review_count=2   <-- 3-day-old data kept
  run 3: scraped_at='\ufeff2026-10-01T09:00:00+00:00'
          website=https://dead-2024.example  rating=3.1  review_count=2   <-- 3-day-old data kept
  run 4: scraped_at='\ufeff2026-10-01T09:00:00+00:00'
          website=https://dead-2024.example  rating=3.1  review_count=2   <-- 3-day-old data kept
  ```

  The fresh scrape supplied `https://cafe-beirut.example`, `rating=4.8`,
  `review_count=900` on all three runs. All were silently discarded. The stale
  values land in `with_websites.csv` (`main.py:200`), the `without_websites.csv`
  pitch list (`main.py:201`), `completeness_score` (`enricher.py`) and therefore
  `lead_score` and `sales_ready.csv` (`main.py:213`) — the sales pipeline is
  built on wrong data with no error anywhere.

- **It self-perpetuates.** `dedup.py:177` computes `max(av, bv)` on the same
  unvalidated strings, so the junk stamp *wins its own replacement* and is written
  back out at `main.py:209`. `main.py:150` re-imports it next run. One bad cell in
  one row is a permanent, self-reinforcing corruption for that business, invisible
  forever. Confirmed by the three-run trace above.

- **Realistic sources, ranked:**
  1. **U+FEFF BOM prefix.** `main.py:82-84` shows the team already knows a BOM
     silently breaks this pipeline — but `encoding="utf-8-sig"` (`main.py:85`)
     only strips the BOM on **line 1** (the header). Any tool that emits a BOM per
     line (PowerShell `Out-File`, shell concatenation, several Android and
     Arabic-locale spreadsheet editors) produces this. It is the worst case
     because it is a *valid-looking* timestamp with an invisible prefix: it
     survives any "does this look like a date?" eyeball check.
  2. **Arabic-Indic digits** `"٢٠٢٦-١٠-٠٣"` (U+0660–0669 = 0x660–0x669 ≫ `'2'`).
     The target markets are Lebanon and Saudi Arabia, `main.py:118-119`
     explicitly targets Arabic rendering in Excel/Sheets, and the master lives on
     a shared human-writable Drive folder (`scrape.yml:56`, `:89`). A human
     "fixing" a cell in an Arabic-locale spreadsheet is enough.
  3. **Excel/Sheets date re-serialisation** → `"46237.024512963"`. `'4'` > `'2'`.
  4. **`"N/A"`, `"N/A – non vérifié"`, `"unknown"`, `"null"`, `"-"`-style
     placeholders** typed by a human. All start with `N`/`u`.
  5. **Emoji** (`"📈"`, U+1F4C8), **French accented** leading char (`"é…"`),
     en-dash `U+2013`, NBSP `U+00A0`. All win.

- **Fix:** in `load_master` (`main.py:85-105`), validate `scraped_at` against
  `datetime.fromisoformat`, coerce to a fixed UTC form
  (`dt.astimezone(dt.timezone.utc).isoformat()`), and set anything unparseable to
  `None` — quarantine the cell, never let it become a ranking key. Add the
  invariant: `_recency` returns `""` for any value that does not parse.

---

### S2 — Lexicographic order is genuinely inverted for valid ISO-8601 with mixed UTC offsets

- **Where:** `dedup.py:156`, `dedup.py:183`.
- **Contract violated:** requirement 1, even with no junk at all — both inputs
  are well-formed ISO-8601.

- **Trigger (verified):**

  ```
  a = '2026-10-03T12:00:00+03:00'   -> 09:00 UTC   rating 3.1   (OLDER)
  b = '2026-10-03T11:00:00+00:00'   -> 11:00 UTC   rating 4.8   (NEWER)
  _pick('rating', a, b) -> 3.1       # the older record won
  ```

  Comparing ISO-8601 strings orders them by **local wall clock then by offset
  character**, not by instant. `'1'` < `'2'`, so the genuinely fresher record
  loses.

- **Why it is S2 and not S1 today:** all three scrapers go through
  `utc_now_iso()` (`httpclient.py:220-229`), which hard-codes `timezone.utc`, so
  the offset is always `+00:00` and the bug is currently dormant. It becomes S1
  the moment anything else writes a stamp — a normaliser, a hand-edited master,
  or one of the proposed migrations (audit 034's SQL emits
  `YYYY-MM-DD"T"HH24:MI:SS.US` with **no offset at all**). The repo's own test
  fixtures already mix conventions: `tests/test_lead_signal.py:204` uses a `Z`
  suffix while `tests/test_lead_signal.py:305-307` use `+00:00`.
- **Fix:** compare parsed instants, never strings — sort key
  `(parsed_utc, original)`; see the naive/aware trap in "Not a bug" below.

---

### S2 — `_recency` coerces with `str()`, but the `scraped_at` merge at `dedup.py:177` does not: latent `TypeError` that kills the run

- **Where:** `dedup.py:156` coerces; `dedup.py:177` (`return max(av, bv)`) does
  not. Same field, two different safety assumptions.
- **Breaks:** `max()` on a `str` and a non-`str` raises. Verified:

  ```
  _pick('scraped_at', {'scraped_at': 0}, {'scraped_at': '2026-10-03T12:00:00+00:00'})
      -> TypeError: '>' not supported between instances of 'str' and 'int'
  ```

  `_is_missing` (`dedup.py:86-90`) returns `False` for `0` and for `False`, so the
  early returns at `dedup.py:168-171` do not fire and `dedup.py:177` is reached.
  This dies the run at `main.py:180` with no outputs written.

- **Why S2 not S1:** unreachable from `main.py` today, because `load_master`
  (`main.py:85-105`) leaves `scraped_at` as a CSV string. It is a latent crash
  armed for the first writer that stores an epoch integer — which is exactly what
  the storage migrations in audits 033/034 propose, and what a pandas/DuckDB path
  would produce.
- **Fix:** make `_pick` route `scraped_at` through the same parser as `_recency`
  instead of calling `max()` on raw values.

---

### S3 — `or ""` collapses meaningful falsy values, and whitespace-only beats "absent"

- **Where:** `dedup.py:156`.
- **Breaks (verified):** `_recency({'scraped_at': 0})` → `''`. So an epoch
  sentinel of `0`, a `0.0`, or `False` is silently reclassified as "infinitely
  old" rather than rejected or honoured. And `_recency({'scraped_at': ' '})` →
  `' '`, which beats `''` — so a whitespace-only cell outranks a record with no
  stamp at all, on the theory that *present beats absent* even when the content
  is meaningless.
- **Trigger:** a master CSV where a partial or interrupted run left `scraped_at`
  as a space, or a spreadsheet that zero-pads unknown dates.
- **Impact:** small and bounded — real stamps start with `'2'` (0x32), and both
  `' '` (0x20) and `''` sort below that, so whitespace never beats a *real*
  timestamp. It only corrupts ties between two junk rows.
- **Fix:** `raw = record.get("scraped_at"); return raw.strip() if isinstance(raw, str) else ""`.

---

### S3 — No length bound: the cell is returned verbatim and carried into the output

- **Where:** `dedup.py:156`.
- **Trigger (verified):** `_recency({'scraped_at': '9' * 2_000_000})` returns all
  2,000,000 characters. `_pick` cost with a 2 MB stamp is ~28 µs — comparison
  cost is negligible, so this is **not** a DoS.
- **Why it still matters:** `dedup.py:177` lets the giant string win
  `max()`, so it is written to `scraped_at` in all five CSVs (`main.py:209-213`).
  A 2 MB cell bloats every export, blows past Drive/Sheets cell limits, and — per
  the first-chars rule above — a `'9'` prefix makes it win every future
  comparison too. This is a symptom of S1, not an independent failure mode.
- **Fix:** reject `scraped_at` longer than ~40 chars in `load_master`.

---

## Not a bug, but worth knowing

- **The variable-width `isoformat()` is accidentally safe — do not "fix" it.**
  `utc_now_iso()` (`httpclient.py:229`) omits the fractional part when
  `microsecond == 0` and uses 3 digits when it is divisible by 1000:
  `2026-10-03T12:34:56+00:00`, `...56.001000+00:00`, `...56.123456+00:00`. This
  *looks* like the precision hazard flagged in `docs/audits/013-main-state-idempotency.md:16`.
  It is not one, because `'.'` (0x2E) > `'+'` (0x2B): a zero-fraction stamp is the
  very start of its second, so any fractional stamp in the same second correctly
  outranks it. I verified all three mixed-width pairs order correctly.
  **Leave `httpclient.py:229` alone; normalise in `load_master` instead.**
- **Date-only stamps are safe.** `"2026-10-03"` correctly sorts against
  `"2026-10-03T00:00:00+00:00"` and `"2026-10-02T23:59:59+00:00"`. Any
  zero-padded prefix in `YYYY-MM-DD` form is monotone. This is fortunate, since
  OSM's own element timestamps are date-granular.
- **Naive-vs-aware is a trap for the S2 fix.** Do not fix the mixed-offset bug
  with a bare `datetime.fromisoformat` — verified, `fromisoformat('2026-10-03T12:00:00Z')
  < fromisoformat('2026-10-03T11:00:00+03:00')` raises
  `TypeError: can't compare offset-naive and offset-aware datetimes` when the two
  strings differ in awareness. Coerce both sides to UTC-aware, defaulting an
  unparseable or naive value to the epoch rather than letting it raise.
- **`website_live` never reaches `_recency` in the main pipeline.** All three
  scrapers hard-set `website_live=None` (`scrapers/osm.py:109`,
  `scrapers/google_places.py:292`, `scrapers/wikidata.py:117`), so
  `_pick` early-returns the master's value at `dedup.py:168-171` and line 183 is
  never evaluated for it. The fields that actually consume `_recency` are
  `website`, `rating`, `review_count`.
- **Recency is only the third tiebreak, and that is intentional.**
  `dedup.py:180-185` orders `trust > validity > recency > str(value)`. So for
  `website`, an OSM URL (trust 3, `_SOURCE_TRUST` at `dedup.py:103`) beats a
  fresher Google URL (trust 2, `dedup.py:100`) regardless of age. That is a
  deliberate policy choice, not a bug — but it means cross-source recency is
  rarely decisive. The comparisons that *do* decide are master-vs-fresh within one
  merged record, which is precisely the path S1 corrupts.
- **Wrong types are swallowed, not rejected.** `_recency` returns
  `"{'x': 1}"` for a dict and `"[1, 2]"` for a list (both start with `{`/`[`, both
  beat every real stamp) and `"nan"` for `float('nan')` and `"True"` for `True`.
  I folded these into S1 because the root cause and the fix are identical: no
  validation. Do not file them separately.
- **`str(bytes)` leaks a Python repr.** `b"2026-10-03T12:00:00Z"` →
  `"b'2026-10-03T12:00:00Z'"`, a literal `b` and quotes inside a data column. Same
  root cause as above.

---

## Recommended order of work

1. **S1** — validate and canonicalise `scraped_at` in `load_master`
   (`main.py:85-105`): parse, convert to UTC, and set anything unparseable to
   `None`. Add a regression test asserting that a `\ufeff`-prefixed, an
   Arabic-Indic-digit, and a `"N/A"` stamp all rank **below** a canonical one, and
   that a fresh scrape's `website`/`rating` beat them across three simulated runs.
   This one edit also closes S3's length case and most of the wrong-type cases.
2. **S2** — replace the string `max()` at `dedup.py:177` and the
   `_recency(rec)` element at `dedup.py:183` with a shared parsed-instant key, so
   merged `scraped_at` and recency ranking cannot disagree and neither can raise
   `TypeError`. Guard the naive/aware comparison.
3. **S3** — tighten `_recency` to `isinstance(str)` + `.strip()`, rejecting
   non-strings outright rather than `str()`-coercing them. Do **not** wrap it in
   `try/except`; it has no reachable crash surface today.
