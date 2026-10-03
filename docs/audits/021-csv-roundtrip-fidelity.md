# 021 — CSV round-trip fidelity (`load_master` → `write_csv` → `load_master`)

## Verdict

For the **current 23-column schema**, the round trip is essentially correct: every
value type in the pipeline survives a write/read cycle — floats, ints, bools, `None`,
embedded commas, newlines, quotes, and Arabic RTL text all come back intact (verified
empirically below). The real risk is **schema drift**: `write_csv` writes with
`extrasaction="ignore"` against a hand-maintained `FIELDS` list that is a second,
disconnected copy of the `BusinessRecord` schema, so any column that is added to one
and not the other is **silently erased on every weekly run** — and because
`all_businesses.csv` is cumulative, that loss compounds. Two secondary lossy
normalizations (`"" → None`, and `website_live` parsing only the literal `"True"`/`"False"`)
are footguns that don't bite on a clean run but turn into silent data loss the moment
anything upstream drifts.

---

## Findings

### S2-1 — `write_csv` silently drops any key not in `FIELDS`; there is no single source of truth for the schema
- **Where:** `main.py:76` (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`), with `FIELDS` at `main.py:32-39` duplicating `BusinessRecord` at `scrapers/base.py:5-28`.
- **Breaks:** The flat dict *is* the schema, but the writer does not validate it — it just ignores anything it doesn't recognize. Two drift directions:
  1. Add a field to `BusinessRecord` (e.g. `opening_hours`, `google_place_id`) and emit it from a scraper, but forget to add it to `FIELDS` → the value is dropped **in the same run it is produced**, never reaching the master.
  2. An old master (or one touched by another writer) contains a column not in today's `FIELDS` → `load_master`'s `DictReader` reads it into the dict, `dedup._merge` propagates it (`all_keys = set(a) | set(b)`, `dedup.py:46`), but the next `write_csv` **erases it**.
- **Trigger:** I confirmed the mechanism — a record `{"name":"X","phone":"+961","opening_hours":"09:00-18:00"}` written against `fieldnames=["name","phone"]` produces `X,+961`; the third column is gone. Because `all_businesses.csv` is cumulative, a column dropped once is dropped forever and the gap widens weekly.
- **Fix:** Make the schema a single definition both sides reference, and stop ignoring drift — `extrasaction="raise"`, or assert `set(record) <= set(FIELDS)` (and warn on `set(FIELDS) - set(record)`) before writing. Deriving `FIELDS` from `BusinessRecord.__annotations__` would keep the two copies from ever diverging.

### S2-2 — A UTF-8 BOM on the master silently wipes the `name` column
- **Where:** `main.py:51` (`open(path, ..., encoding="utf-8")`) — reads BOM as `\ufeff`, not stripped.
- **Breaks:** The CSVs are "the product" and are uploaded to Drive (`README`/workflow) for sales to open. If anyone opens `all_businesses.csv` in Excel/Sheets and re-saves it (which prepends a UTF-8 BOM), the first header `name` becomes `\ufeffname`. `csv.DictReader` then yields a `\ufeffname` key and **no `name` key**; `load_master` uses `row.get(...)` throughout so it does not crash — every record silently comes back with `name = None`, and the next `write_csv` persists that loss to the whole master. I confirmed: `\ufeffname,phone,website_live` → `row.get("name")` is `None`.
- **Trigger:** one external open/save round-trip in Sheets or Excel.
- **Fix:** open with `encoding="utf-8-sig"` on read (`main.py:51`). (Keep plain `utf-8` on write.)

### S3-3 — `website_live` parses only the literal strings `"True"`/`"False"`; anything else becomes `None`
- **Where:** `main.py:68-69`.
- **Breaks:** The parser is exact-match and case-sensitive. Any value other than `"True"` or `"False"` — `"true"`, `"false"`, `"1"`, `"0"`, `"TRUE"`, or an empty/`NULL`-style token — is silently coerced to `None`. `None` is treated as "never checked", so:
  - the dead-website sales signal is lost (`enricher.py:243-244` awards `+20` only when `website_live is False`), and
  - the live/dead summary undercounts both (`main.py:155-156`).
- **Trigger:** Today's writer only ever emits `"True"`/`"False"`/`""` (Python `bool` → `str(bool)`; `None` → `""`), so a clean run round-trips. The loss fires the moment the CSV is produced by a different writer or hand-edited (same trigger class as S2-2).
- **Fix:** normalize before parsing: `wl = str(row.get("website_live")).strip().lower()` then map `{"true":"1"}` → `True`, `{"false":"0"}` → `False`, else `None`.

### S3-4 — `""` and `None` are irreversibly conflated on every load
- **Where:** `main.py:53-55` (`if row[k] == "": row[k] = None`) paired with `write_csv` relying on `csv`'s implicit `None → ""` (verified).
- **Breaks:** An empty string never survives a round trip; it always comes back as `None`. This is *lossless today* only by convention — the codebase never distinguishes `""` from `None` (every presence check is truthiness: `if record.get("phone")`, `if address and country == "LB"` at `enricher.py:85`, `has_any_contact` at `main.py:82-90`). But the round trip is not actually fidelity-preserving: a field can never hold "present but blank" distinct from "never scraped", so any future field or check that needs that distinction (e.g. "address supplied but empty" vs "no address attempt") will be silently wrong.
- **Fix:** Keep the normalization, but make it explicit and symmetric — a single `_normalize(value)` used on both write (map `None → ""`) and read (map `"" → None`), so the contract is documented instead of depending on `csv`'s `None` handling.

---

## Value-by-value round-trip (the enumeration requested)

Verified by running the exact `write_csv`/`load_master` logic on a synthetic record
containing every edge case; the happy path is correct across the board.

| Value type | Survives? | Notes |
|---|---|---|
| `None` | ✅ as `None` | `csv.DictWriter` writes `None → ""`; `load_master:53-55` maps `"" → None`. Round trip is stable. |
| Empty string `""` | ❌ becomes `None` | Irreversible (`main.py:53-55`). Benign today, footgun later (S3-4). |
| `website_live` bool | ✅ for `True`/`False` | `str(True)→"True"`, parsed back at `main.py:68-69`. Only exact `"True"`/`"False"` survive; see S3-3. |
| `lat` / `lon` / `rating` float | ✅ exact | `float→str→float` is exact in Python (`repr`); cast at `main.py:56-61`. A whole-number `rating` (`int 4`) normalizes to `4.0` — value preserved, type normalized. |
| `review_count` / `completeness_score` / `lead_score` int | ✅ | Cast `int(float(x))` at `main.py:62-67`. `0` survives (guard is `== ""`, not truthiness). |
| Embedded commas | ✅ | `csv` QUOTE_MINIMAL quotes them. |
| Newlines in name/address | ✅ | `newline=""` on both opens (`main.py:51,75`) + QUOTE_MINIMAL. |
| Quotes in name/address | ✅ | `"` doubled as `""` and un-doubled on read. |
| Arabic RTL text | ✅ | `utf-8` both ways, Unicode-clean; Arabic and mixed LTR/RTL verified. |

## Not a bug, but worth knowing

- **`lead_score` and `completeness_score` are recomputed for every record every run** (`enricher.py:289-290`), so their stored values are write-only and the int cast on load is dead work for those two. The int round-trip only materially matters for `review_count` (the one derived field that persists from a prior run).
- **`source` uses `"|"` as a separator and is re-`union`-ed on merge** (`dedup.py:54-56`). Idempotent today, but any future source name containing `|` would silently split/corrupt. Consider a non-printing separator or a real list.
- **Embedded commas survive the CSV but break city inference** — `_extract_city` does `address.split(",")[-1]` (`dedup.py:34-38`), so an address like `"St. Georges, Beirut"` still infers `Beirut`, but a multi-line Google `formattedAddress` ending in a country/zip mislabels city. Adjacent to this lens, not CSV fidelity.
- **`scraped_at` merges via `max(av, bv)`** (`dedup.py:57-58`). `datetime.utcnow().isoformat()` includes microseconds, so width is uniform — fine — but only as long as timestamps stay `+Z`/UTC; a tz-aware or offset timestamp would break lexicographic ordering. Not a round-trip issue.
- **The brief says "22 columns"; `FIELDS` and `BusinessRecord` actually have 23.** They match each other (same 23 keys), so no code bug — but it underscores S2-1: the column count is only correct by coincidence of two hand-maintained lists.

## Recommended order of work

1. **S2-1** — single-source the schema and `extrasaction="raise"` (or assert key coverage) at `main.py:76`. This is the only thing that can permanently delete data at scale.
2. **S2-2** — `encoding="utf-8-sig"` on `main.py:51` (one-line, removes the total-`name`-wipe vector).
3. **S3-3** — normalize `website_live` before parsing (`main.py:68-69`) so casing/`1`/`0` don't silently null the liveness signal.
4. **S3-4** — make the `None`↔`""` contract explicit and symmetric, and document that empty string is not preserved.
