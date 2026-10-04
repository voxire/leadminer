# 320 — `_trust_for` (dedup.py:148) — correctness lens

## Verdict

`_trust_for` is **not correct**, and the failure is not subtle tuning — it is a category
error. It reads `record["source"]`, but `source` is a **union of every source that has
ever touched the record**, collapsed at `dedup.py:174`, and it carries no record of
*which source supplied the surviving value*. So `_trust_for` attributes a merged record's
authority to whichever source happened to be loudest historically, not to the data in
hand. Because `main.py:179` re-merges the whole master against fresh scrapes on every
run, trust **ratchets upward on run 1 and never decays, ever**. The concrete cost: once
a business has been seen by two sources, the merged row permanently outranks the
authoritative fresh source, and its stale `website` beats Google's current URL on every
subsequent run forever — producing `recommended_service = "Website rebuild +
maintenance"` and `lead_score = 55` for a business that has a perfectly working site,
and dropping it out of `without_websites.csv` entirely.

Severity is **S1** for that one mechanism. I could not find a way for `_trust_for` to
merge two *different* businesses or split one — dedup keys never consult it
(`dedup.py:205`, `dedup.py:228`) — so the blast radius is corrupted field *values*, not
record *count*. That is the one thing holding this below "silent data loss".

All findings below were executed against the real module (Python 3.14.8, `dedup`
imports cleanly), not reasoned about in the abstract.

---

## Findings

### S1 — Trust ratchets on the source *union*, so stale master values beat fresh authoritative ones permanently

- **Where:** `dedup.py:151` (the `max()`), enabled by `dedup.py:174` (source is
  collapsed to a union), consumed at `dedup.py:181` as the *first* element of `rank`,
  driven by `main.py:179` (`combined = raw_filtered + master`).
- **Breaks:** `_pick` resolves each field independently (`dedup.py:192-193`), so a
  merged record's surviving `website` may well have come from OSM. But its `source`
  string is now `"google_places|osm"`, and `_trust_for("website", merged)` returns
  `max(3_from_osm, 2_from_google) = 3` — a number earned by a field Google is more
  authoritative for but whose *value* came from OSM. On the next run that inflated 3 is
  compared against a fresh `google_places` record's honest 2, and the master wins. Run 3
  behaves identically. There is no decay term, and `_VOLATILE` (`dedup.py:114`) never
  gets consulted because trust (`dedup.py:181`) is compared *before* recency
  (`dedup.py:183`).

  Verified trust table (`_trust_for` output, all executed):

  | source | name | email | website | rating | lat | category |
  |---|---|---|---|---|---|---|
  | `google_places` | 3 | 1 | 2 | 3 | 3 | 3 |
  | `osm` | 2 | 3 | 3 | 1 | 1 | 2 |
  | `google_places\|osm` | **3** | **3** | **3** | **3** | **3** | **3** |

  Note `email`/`website`/`rating`/`lat` all *rise* on merge. `google_places.py:290` and
  `:293-296` hardcode `email`, `facebook`, `instagram`, `whatsapp`, `linkedin` to
  `None` — Google can never produce them — yet after one merge a Google-only record
  claims `email` trust 3, `facebook` 3, `instagram` 3, `whatsapp` 3.

- **Trigger (exact input, exact wrong output).** Business "Star Bakery" at
  `+9611234567`. Google is authoritative for `website` (`google_places` trust 2) and
  reports the live domain every run. Three consecutive `dedup()` calls through
  `main.py`'s sequence:

  ```
  RUN 1  osm website="https://starbakery-old.com"  scraped_at=2026-01-15T00:00:00Z
         google website="https://starbakery.com"   scraped_at=2026-01-15T00:00:00Z
      -> merged website='https://starbakery-old.com'  source='google_places|osm'
      -> _trust_for('website') = 3

  RUN 2  dedup([{google, website="https://starbakery.com",  ts=2026-02-15...}, master])
      -> merged website='https://starbakery-old.com'   <-- Google's value REJECTED

  RUN 3  dedup([{google, website="https://starbakery-new.com", ts=2026-03-15...}, master])
      -> merged website='https://starbakery-old.com'   <-- still rejected
  ```

  Then, feeding that record through the real `enricher.lead_score` and real
  `pitch_recommender.recommend_service` with `website_live=False` (what
  `enricher.py:250` will record for the dead domain):

  ```
  lead_score          = 55
  recommended_service = 'Website rebuild + maintenance'
  ```

  Correct output is `'SEO audit + visibility upgrade'` (`pitch_recommender.py:70-71`)
  or `'Digital marketing retainer'` (`pitch_recommender.py:95-96`).
  `enricher.py:303-304` adds +20 to `lead_score` specifically because
  `website_live is False` — a penalty earned by pointing at a URL we should have
  replaced two runs earlier.

- **CSV impact, per affected record, every run, forever:**
  - `all_businesses.csv` — wrong `website`, wrong `website_live`
  - `with_websites.csv` — present, carrying a dead URL
  - `without_websites.csv` — **missed** (`main.py:200`); this is the new-site pitch
    list named in the brief, and a business with a live site is excluded from it
  - `sales_ready.csv` — present but tagged `Website rebuild + maintenance`, i.e. the
    top-tier pitch from `pitch_recommender.py:55-58` applied to a lead that does not
    need a rebuild. A rep calls them about a site that works.
  - one wasted HTTP GET at 40-way concurrency (`enricher.py:231`, `:238`) against the
    same dead domain, every run, indefinitely.

  The same mechanism freezes `name` and `address`: a Google-typo'd `name="Starbakery"`
  wins over OSM's `"Star Bakery"` at trust 3 vs 2, and OSM's later correction to
  `"Star Bakery & Patisserie"` loses again on the next run — verified. A master row
  whose `name` was frozen also can never re-match on the name-keyed dedup path
  (`dedup.py:214`) if its phone later goes stale.

- **Also verified, not an ordering artefact.** Swapping arguments changes nothing:
  `_merge(master, fresh_google)["website"]` and `_merge(fresh_google, master)["website"]`
  both return `'https://old.example'`. This is a systematic ranking error, not a race.
- **Fix (one line):** stop inferring per-field authority from a source *union* — carry
  per-field provenance, e.g. store `merged["_src_<field>"]` in `_pick` and have
  `_trust_for` read `_SOURCE_TRUST[record["_src_" + field]][field]` instead of
  `max()`-ing over every source in the union.
- **Cheaper interim fix if per-field provenance is too large:** make the master's
  accumulated union *not* confer trust on the next merge — e.g. in `main.py:179` merge
  the master as the *loser* explicitly, or gate `_trust_for` on
  `record.get("_is_master")` and return `0` for master rows so a fresh source always
  outranks history for volatile fields.

---

### S2 — `_sources_of` does not strip or case-fold, so one stray space destroys all trust *and* permanently corrupts `source`

- **Where:** `dedup.py:145` — `{s for s in raw.split("|") if s}`.
- **Breaks:** tokens are neither stripped nor lower-cased, and `dedup.py:174`
  re-serialises those same raw tokens, so the corruption is **self-perpetuating** — once
  a spaced token enters `source`, every subsequent merge re-emits it and it never
  recovers. Trust for that record collapses to the floor `_DEFAULT_TRUST = 1`
  (`dedup.py:111`) for *every* field, silently, with no log line.

- **Trigger (exact input → exact output):**
  ```
  a = {"website": "https://old.example", "source": "osm | google_places",
       "scraped_at": "2026-01-15T00:00:00Z"}
  b = {"website": "https://new.example", "source": "google_places",
       "scraped_at": "2026-02-15T00:00:00Z"}

  _sources_of(a)        -> {'osm ', ' google_places'}     # both unknown
  _trust_for('website', a) -> 1                           # was 3
  _trust_for('website', b) -> 2
  _merge(a, b)["website"]  -> 'https://new.example'        # wrong winner
  _merge(a, b)["source"]   -> ' google_places|google_places|osm '
                             ^ note: leading space, trailing space, and a DUPLICATE
                               'google_places' token that can never be normalised away
  ```
  Verified fix output: `{'source': ' google_places | OSM '}` →
  `_sources_of` = `{'google_places', 'osm'}`, `_trust_for('website')` = `3`.

- **Reachability:** no current code path writes a spaced `source` — the three scrapers
  emit bare literals (`osm.py:119`, `wikidata.py:127`, `google_places.py:302`) and
  `dedup.py:174` joins with a bare `"|"`. But `all_businesses.csv` is a
  hand-openable, Excel-editable artifact (`main.py:121` writes `utf-8-sig` *for*
  Excel), `main.py:86` reads it back with no `strip()`, and any future writer that
  joins with `" | "` or `", "` silently disables the entire trust system for that row.
  Cheap to harden, expensive to diagnose in the field.
- **Fix (one line):** `{s.strip().lower() for s in raw.split("|") if s.strip()}`.

---

### S2 — `max()` gives zero credit for corroboration, and `.get(field, _DEFAULT_TRUST)` conflates "no opinion" with "weak opinion"

- **Where:** `dedup.py:151` — both defects on one line.
- **Breaks (a):** `max()` over the union collapses a *multiply-confirmed* record to
  exactly the score of its single best source, so the trust system is blind to
  corroboration — which is the single strongest correctness signal available for free.
  Verified:
  ```
  _trust_for('name', {"source": "osm"})                 -> 2
  _trust_for('name', {"source": "wikidata"})             -> 2
  _trust_for('name', {"source": "osm|wikidata"})         -> 2   # two independent
  _trust_for('name', {"source": "google_places"})        -> 3   # confirmations, no gain
  _trust_for('name', {"source": "google_places|osm"})    -> 3   # vs single-source 3
  ```
  A business two sources independently confirm is scored identically to one source's
  word. Meanwhile `enricher.py:316-317` *does* pay `+5` to `lead_score` for precisely
  this signal — so the pipeline values corroboration for ranking and discards it for
  merge decisions.
- **Breaks (b):** `.get(field, _DEFAULT_TRUST)` means a source with **no opinion** about
  a field is scored `_DEFAULT_TRUST = 1` rather than *abstaining*. `_trust_for` returns
  an int with no abstain sentinel, so "I don't rate this field" and "I rate this field
  1, the lowest possible" are indistinguishable to `dedup.py:181`. Concretely
  `google_places` has no `email` entry, so a Google record's email trust is 1 — which
  happens to be *correct* today only because `google_places.py:290` emits
  `email=None` and `dedup.py:169` returns early. The correctness is accidental, not
  designed.
- **Trigger:** `{"name": "X", "source": "osm|wikidata"}` → `_trust_for("name") == 2`,
  identical to `{"source": "osm"}`. Confirmed above.
- **Fix (one line):** `best = min(3, best + len(_sources_of(record)) - 1)` after the
  loop, and return a `0` (abstain) sentinel instead of `_DEFAULT_TRUST` for
  unrated `(source, field)` pairs so `dedup.py:181` can skip them.

---

### S2 — `_SOURCE_TRUST` covers only 13 of 22 output columns; for 9 of them `_trust_for` is constant and `str(value)` decides

- **Where:** `dedup.py:97-110` (the table), `dedup.py:184` (the `str(value)` tiebreak).
- **Breaks:** verified — `_trust_for` returns the *same* number for all three sources
  for these fields, so the first two elements of `rank` are constant and the outcome is
  decided by a lexicographic string comparison of the values:
  ```
  phone [2]  linkedin [1]  country [1]  region [1]  website_live [1]
  completeness_score [1]  lead_score [1]  industry_priority [1]  recommended_service [1]
  ```
  That is 9 of the 22 columns in the brief's table. `phone` is the most alarming entry:
  it is the primary dedup identity field and `_SOURCE_TRUST` gives it `2` for
  `google_places`, `osm`, *and* `wikidata` (`dedup.py:100`, `:104`, `:107`), so
  `_trust_for` is a **no-op for phone** — there is no authority ranking for the one
  field that decides which records are the same business.
- **Trigger (exact input → exact wrong output):**
  ```
  a = {"lead_score": 9,  "source": "osm",           "scraped_at": "2026-02-15T00:00:00Z"}
  b = {"lead_score": 47, "source": "google_places", "scraped_at": "2026-01-15T00:00:00Z"}
  _merge(a, b)["lead_score"] -> 9     # '9' > '47' as strings; fresher wins too
  ```
  A lower score beats a higher one. Same class: `_merge({"industry_priority": "low"},
  {"industry_priority": "high"})` → `"low"` wins, `"low" > "high"` lexicographically.
- **Reachability — honest scoping:** this is **masked in the only production path**.
  `main.py:206` reads `completeness_score` and `main.py:202-204` reads
  `industry_priority`, but both are recomputed before any CSV is written:
  `enricher.py:349-350` overwrites `completeness_score` and `lead_score`, `main.py:190`
  overwrites `industry_priority`, and `main.py:197` recomputes `lead_score`. So no
  shipped CSV is wrong today *because of this*. It is live for any second caller of
  `dedup()` (a CLI `dedup` subcommand, a partial-refresh path, a
  `carry_enrichment` re-merge as proposed in `docs/audits/035-incremental-scraping.md`)
  and it is a trap for whoever adds one.
- **Fix (one line):** never route a computed/derived column through `_pick` — have
  `_merge` take an explicit skip-set of recomputed fields, or add a numeric comparator
  so `dedup.py:184` does not order ints as strings.

---

### S3 — `_validity` has no rule for `name`/`category`/`address`, so trust is the *only* discriminator there and a placeholder freezes forever

- **Where:** `dedup.py:120-140` (falls through to `return 1` at `:140`), combined with
  `dedup.py:181` placing trust *above* validity.
- **Breaks:** for these four fields `_validity` is a constant, so `_pick` is decided by
  trust alone. Verified:
  ```
  _validity('name',     'N/A')         -> 1
  _validity('name',     'Star Bakery') -> 1
  _validity('category', 'establishment')-> 1
  _validity('category', 'bakery')      -> 1
  _validity('address',  'N/A')         -> 1
  ```
  So a placeholder from the higher-trust source wins, and — via S1 — wins *forever*:
  ```
  _merge({"name":"N/A","category":"establishment","address":"N/A","source":"google_places"},
         {"name":"Star Bakery","category":"bakery","address":"Hamra St, Beirut","source":"osm"})
    -> name='N/A'  category='establishment'  address='N/A'
  then, fresh OSM truth at ts=2026-06-01: merged name='N/A'   <-- still rejected
  ```
  `category='establishment'` additionally maps to `industry_priority == "low"`
  (`whitelist.py:139-145`), which `main.py:204` excludes from `sales_ready.csv` — a
  lost lead. Note the whitelist at `main.py:172` filters only `raw_filtered`, *before*
  dedup, so a master row whose category got frozen to a junk value is never re-filtered
  on later runs.
- **Trigger:** requires the high-trust source to emit a placeholder. `google_places`
  is the high-trust source for all three fields (`dedup.py:100`). Plausible emitters:
  `google_places.py:328` (`_pick_category` returns `types[0]`, so an all-generic
  `types` list yields `"establishment"`).
- **Fix (one line):** add `_validity` rules that reject known placeholders for
  `name`/`category`/`address` (`{"n/a","na","none","unknown","-","establishment",
  "business",""}`, case-folded) so a real value at trust 2 beats a placeholder at 3.

---

### S3 — `_SOURCE_TRUST["wikidata"]["category"] = 1` is dead configuration, equal to `_DEFAULT_TRUST`

- **Where:** `dedup.py:108` (`"category": 1`) vs `dedup.py:111` (`_DEFAULT_TRUST = 1`).
- **Breaks:** the entry encodes an intent to *distrust* Wikidata categories below
  baseline, but `1 == _DEFAULT_TRUST`, so it is a no-op and the value is
  indistinguishable from an unrecognised source. Verified:
  ```
  _trust_for('category', {"source": "wikidata"})  -> 1
  _trust_for('category', {"source": "?????"})     -> 1
  ```
  Worse, it is a latent trap: the moment anyone lowers `_DEFAULT_TRUST` to 0 to model
  abstention (which is the fix for S2-b), the wikidata entry silently becomes the
  *only* source that outranks an unknown one, for a field where Wikidata's
  `categoryLabel` (`wikidata.py:100`) is actually reasonable data.
- **Fix (one line):** change `dedup.py:108` to `"category": 0`, or delete the key and
  add a comment saying wikidata categories are deliberately not distinguished.

---

### S3 — `_validity` accepts `nan`, `inf`, `True` and `1e400` as valid `lat`/`lon`

- **Where:** `dedup.py:128-133`.
- **Breaks:** `float("nan")`, `float("inf")`, `float(True)` and `float("1e400")` all
  succeed, so all four score full validity 3. Verified:
  ```
  _validity('lat', 'nan')   -> 3
  _validity('lat', 'inf')   -> 3
  _validity('lat', True)    -> 3
  _validity('lat', '1e400') -> 3
  ```
  Reachable from the CSV side: `main.py:90-95` does `float(row["lat"])` inside a
  `try` that only catches `ValueError`/`TypeError`, so a master cell reading `nan` is
  accepted as a float. A `nan` latitude then flows into `enricher.py:92`
  (`lat_min <= lat <= lat_max`), where every comparison is `False`, so `infer_region`
  returns `None` and the record is filed under region `"Unknown"` (`main.py:230`) —
  permanently, because trust for `lat` is 3 for any Google-touched record (S1).
- **Fix (one line):** after the `float()` conversion in `dedup.py:132`, add
  `and math.isfinite(v)`.

---

## Not a bug, but worth knowing

- **`_trust_for` cannot corrupt record *identity*.** `dedup()` derives keys only from
  `normalize_phone` and `normalize_name` (`dedup.py:205`, `:214`, `:228`); `_trust_for`
  is reachable only through `rank` at `dedup.py:181`, which decides *values*. So this
  function cannot merge two different businesses, split one, or inflate the record
  count. That is the main reason S1 stops short of "silent data loss".
- **`_pick` really is order-independent — I checked, and it is clean.** Six permutations
  of a Google/OSM/Wikidata triple all produce byte-identical output:
  ```
  6 orderings of google/osm/wikidata -> 1 distinct result
  {'source': 'google_places|osm|wikidata', 'name': 'Starbakery', 'lat': 33.9,
   'website': 'https://o.example', ...}
  ```
  The guarantee is: `rank` is a total order on `(trust, validity, recency, str(value))`
  compared with `>=` at `dedup.py:187`, and the accumulated source set after *k*
  merges is the union of those *k* sources regardless of arrival order — so the
  previous generation's "merge order is non-deterministic" class of bug is genuinely
  fixed. The bug I found is orthogonal: order-independence is achieved, but the ranking
  function it evaluates is wrong.
- **`_merge` iterating `set(a) | set(b)` (`dedup.py:192`) is safe** even though set
  order varies per process: `_pick` reads only `a` and `b`, never the partially built
  `merged` dict, so key iteration order cannot affect any field's value.
- **The `lead_score`/`completeness_score` string-ordering bug (S2, last one) is masked**
  in production by `enricher.py:349-350` and `main.py:197`, both of which recompute
  before any CSV write. I could not construct a shipped-CSV wrong output from it. Listed
  as S2 rather than S1 for that reason.
- **`country` is decided by `str()` too** (`_trust_for` = 1 for all sources), but
  `main.py:184` overwrites it with `resolve_country()` immediately after `dedup()`, so
  the garbage is discarded. Same mask, one line earlier.
- **Within a single run the S1 ratchet does not fire.** `_pick` is called with the
  original `a`/`b` (`dedup.py:193`), so a record's own union never inflates trust
  against its sibling in the same `_merge`. The ratchet strictly requires a *second*
  merge — i.e. a second run over the master. That is why `main.py:179` is the load-bearing
  line for this whole finding.

---

## Recommended order of work

1. **S1** — give `_pick` per-field provenance so `_trust_for` reads the trust of the
   source that actually supplied the candidate value, instead of `max()`-ing over the
   historical union at `dedup.py:151`. This one change fixes the stale-website pitch, the
   frozen name/address, and the frozen coordinates together; nothing else in this report
   matters as much.
2. **S2 (whitespace)** — one-line `.strip().lower()` at `dedup.py:145`, plus a `.strip()`
   in `main.py:87` so the master cannot carry spaced tokens in the first place.
3. **S2 (corroboration)** — credit additional confirming sources in `_trust_for`, and
   introduce an abstain sentinel so "no opinion" stops meaning "lowest trust".
4. **S2 (uncovered fields)** — take computed columns out of `_pick` before adding any
   second caller of `dedup()`; today the mask is `enricher.py:349-350` and `main.py:197`,
   and it will not survive the first incremental-merge refactor.
5. **S3s** — placeholder rules in `_validity`, `math.isfinite`, and fix or delete the
   dead wikidata entry.
6. **Regression tests** to add alongside whichever fix lands: assert that a two-source
   master row does **not** outrank a fresh single-source record for `website`, `name`,
   `lat`/`lon`, and that `_trust_for` is invariant under reordering of `source` tokens.

---

## Verification method

Findings were produced by importing `dedup` (and, for downstream impact, `enricher`
with a stubbed `requests` and the real `pitch_recommender`) and executing `_trust_for`,
`_merge`, and `dedup` directly on the exact inputs quoted above. No network calls, no
package installs, no source modifications — `git status` shows no changes to any tracked
file. Probe scripts were written outside the repository, under
`/private/var/folders/.../T/opencode/audit_320_trust/`.