# 085 — Regression hunt: the four main.py changes

## Verdict

Three of the four changes are correct and hold up under end-to-end tracing: the
`website_live` tri-state is threaded correctly (it touches neither
`completeness_score` nor the five CSV subset definitions), and the atomic write
does not leak `.tmp` files into rclone during a normal CI run. The one real
defect is `resolve_country`: it runs **after** dedup, so the dedup index still
keys phones under the *raw* country, and the exact bug the change claims to fix
("made normalize_phone guess") survives unchanged in `dedup.py`. That leaves two
sources of truth for `country` and can split one business into two rows with two
different countries.

## Findings

### S2 — `resolve_country` runs after dedup, so the dedup phone key never sees the resolved country

- **Where:** `main.py:180` (`records = dedup(combined)`) vs `main.py:183-184`
  (`for r in records: r["country"] = resolve_country(r)`); `dedup.py:110` and
  `dedup.py:133` still call `normalize_phone(..., record.get("country") or "LB")`.
- **Breaks:** the commit message ("resolve_country() replaces
  `record.get('country', 'LB')` throughout") is not true of the one place that
  matters most. The dedup index groups records on the phone **before** any
  country canonicalisation, so the country used to *key* a record and the
  country written to the CSV are two different values. When they disagree, the
  same business is keyed under two different E.164 numbers and survives as two
  rows — with one of the two carrying a wrong country.
- **Trigger:** a record whose raw `country` is `None` or an alias. `_COUNTRY_CODES`
  (`dedup.py:23-26`) recognises only `"LB"`/`"SA"`; `resolve_country`
  (`main.py:61-67`) additionally maps `LEB`/`LBN`→`LB` and `KSA`/`SAU`/`SAU-AR`→`SA`.
  Concrete example — two sources emit the same Riyadh shop:
  - Row A: `{"country": "KSA", "phone": "050 123 4567"}` (alias + bare national)
  - Row B: `{"country": "SA",  "phone": "050 123 4567"}`
  - Dedup keys A as `normalize_phone("0501234567", "KSA")` →
    `_COUNTRY_CODES.get("KSA", "961")` = `961` → **`+961501234567`**.
    B keys as `normalize_phone("0501234567", "SA")` → **`+966501234567`**.
    Different keys → not merged → **duplicate in the same run**.
  - `resolve_country` then rewrites A's country to `"SA"` and
    `main.py:194` rewrites A's phone to `+966501234567`, so the stored row and
    its dedup key no longer even agree with each other.
  - A second, milder instance is the legacy `country=None` record the commit
    itself says exists ("Every master record without an explicit country got
    country=None"). A bare-national Saudi number in such a row is keyed as
    Lebanese (`+961…`) at dedup time, and `resolve_country` cannot recover it
    because the phone carries no `+966`/`00966` prefix to detect
    (`main.py:69-73`). So the "guess" the commit set out to remove is still made
    for exactly those rows.
- **Fix:** canonicalise before grouping. Move the loop above the dedup call:
  ```python
  combined = raw_filtered + master
  for r in combined:
      r["country"] = resolve_country(r)
  records = dedup(combined)
  ```
  This is safe: `resolve_country` reads the *raw* phone, which is still intact
  here (dedup has not yet run), and `_merge` then sees a canonical `"LB"`/`"SA"`
  instead of `None`/aliases. Note this does **not** rescue a bare-national
  number with `country=None` — that ambiguity needs a source-level country or a
  national-prefix heuristic, not a reorder.

### S3 — `resolve_country` alias handling is asymmetric; "Saudi Arabia" silently becomes "LB"

- **Where:** `main.py:61-67` (alias branch) vs `main.py:74` (unconditional `LB`
  default).
- **Breaks:** a record carrying the full country name is silently relabelled
  Lebanese. `"Lebanon"` happens to come out right only because `LB` is the
  fallback default, not because it is recognised.
- **Trigger:** `resolve_country({"country": "Saudi Arabia"})` →
  code `"SAUDI ARABIA"` is in neither `("LB","SA")`, `("LEB","LBN")`, nor
  `("KSA","SAU","SAU-AR")`, so it falls through to the phone check (no phone)
  and returns `"LB"`. The test suite (`tests/test_lead_signal.py:139-141`) only
  asserts `"Lebanon"` (passes by default-luck) and `"KSA"` (handled), so the gap
  is invisible to it.
- **Fix:** add full names to the mapping — `("LEBANON",)` → `"LB"` and
  `("SAUDI ARABIA", "SAUDI", "SAU")` → `"SA"` — or, better, canonicalise
  against a single dict that includes full names, ISO codes and aliases.

### S3 — the timestamped rclone snapshot uploads `data/` unfiltered, so a lingering `.tmp` can reach Drive

- **Where:** `.github/workflows/scrape.yml:49` —
  `rclone copy data/ "gdrive:leads/data/$TIMESTAMP/" --drive-chunk-size 8M`
  carries no `--include`, unlike the download step at `:38` and the master sync
  at `:50` (both `--include "*.csv"`).
- **Breaks:** `write_csv` writes `all_businesses.csv.tmp` then
  `os.replace`s it (`main.py:122,130`). In a successful CI run all five
  `write_csv` calls finish inside the "Run scrapers" step before the upload
  step, so no `.tmp` survives to be copied — and a hard kill (OOM / the 300-min
  timeout / SIGKILL) kills the whole job, so the later upload never runs either.
  The hazard is therefore latent in CI, but real wherever `data/` persists: a
  stale `.tmp` from a previously killed run is picked up verbatim by the
  unfiltered `:49` snapshot and written to Drive as a partial CSV. The
  `.csv.tmp` suffix correctly does **not** match `*.csv`, so the download and
  the master sync are already protected — only the unfiltered snapshot is
  exposed.
- **Fix:** add `--include "*.csv"` to `scrape.yml:49` (and optionally sweep
  `data/*.tmp` before upload).

## Not a bug, but worth knowing

- **The column count is 23, not 22.** `FIELDS` (`main.py:33-40`) has 23 entries;
  `BRIEF.md` calls it "22 columns" and `BusinessRecord` (`scrapers/base.py:5-28`)
  also carries 23 keys. A documentation drift, not a data fault — `write_csv`'s
  `extrasaction="ignore"` (`main.py:125`) means no key is lost either way.
- **`enrich()` computes `lead_score` that `main.py` always throws away.**
  `enricher.py:342` sets `lead_score` before `industry_priority` exists; `main.py:197`
  then recomputes it correctly once priority is known (`main.py:190`). The first
  computation is dead work — harmless, but the comment at `main.py:195-196` is the
  only thing preventing someone "optimising" it away and reintroducing a 0-point
  priority bug.
- **`write_csv` fsyncs the file but not the directory.** `os.replace` is atomic,
  but without an `os.fsync` on the parent directory fd the rename itself can be
  lost on a power failure on some filesystems. A durability nit only; the
  in-process crash case (the one the change targets) is fully covered.
- **`website_live` tri-state is genuinely clean.** Verified end-to-end:
  `completeness_score` (`enricher.py:101-117`) reads only
  phone/email/website/address/facebook|instagram/whatsapp/linkedin — never
  `website_live`; the subsets (`main.py:199-206`) key off `website`, `completeness_score`,
  `has_any_contact` and `industry_priority`, none of which touch `website_live`;
  `lead_score` (`enricher.py:292-296`) and `recommend_service`
  (`pitch_recommender.py:34-36`) both treat `None` (unreachable) as distinct from
  `False` (dead). The round-trip through `load_master` (`main.py:102-103`) maps
  `"True"`/`"False"`/`""` back to `True`/`False`/`None` correctly.

## Recommended order of work

1. Move the `resolve_country` loop above `dedup(combined)` (`main.py:183-184` →
   before `:180`). One-line reorder; closes the two-sources-of-truth gap for the
   dedup index.
2. Add full-name aliases to `resolve_country` (`main.py:64-67`) so
   `"Saudi Arabia"`/`"Lebanon"` canonicalise explicitly instead of relying on the
   `LB` default.
3. Add `--include "*.csv"` to the unfiltered snapshot upload
   (`scrape.yml:49`).
