# 046 — Dead and redundant code

## Verdict

Most of this codebase is genuinely live, but the contact-detection logic is
triplicated and has silently drifted, so `sales_ready.csv` drops WhatsApp- and
LinkedIn-only leads that the rest of the pipeline explicitly treats as
contactable. Separately, `enrich()` computes a `lead_score` that is always
overwritten, and the `GEOGRAPHIC_NOISE` block is ~90% dead weight for the OSM
scraper whose tag keys it can never match. The rest is hygiene (placeholder-less
f-strings, an inert TypedDict contract, a summary-only `with_social`).

## Findings

### S1 — None

No finding here causes wrong output silently across the board, loses data in
the master, or kills a run. The closest candidate (the drifted `has_any_contact`)
is scoped to one CSV and is fixed by a one-line change, so it is S2, not S1.

### S2 — `has_any_contact` is a third, drifted copy of contact detection, and it drops WhatsApp/LinkedIn-only leads

- **Where:** `main.py:137-145` (`has_any_contact`); used only at `main.py:202-205` to build `sales_ready`.
- **Breaks:** The premise "duplicates `completeness_score`" is imprecise — the two
  are *not* the same predicate. `completeness_score` (`enricher.py:101-117`) returns
  a 0–7 presence count over seven fields; `has_any_contact` returns a boolean over
  three fields with stricter format checks. But "does this record have a usable
  contact channel" is now implemented in **three** places with **three** different
  field sets:
  - `completeness_score` counts `whatsapp` (`enricher.py:113-114`) and `linkedin`
    (`enricher.py:115-116`) as contact fields;
  - `lead_score` awards +15 for `whatsapp` (`enricher.py:282-283`) and (via the
    contact block) treats `email`/`phone`/`instagram` as signals (`enricher.py:280-287`);
  - `has_any_contact` checks **only** `phone`, `email`, `instagram`
    (`main.py:138-144`) — it ignores `whatsapp` and `linkedin` entirely.
- **Trigger:** A business whose website exposes a `wa.me/…` or `whatsapp.com/send`
  link and nothing else. `enricher._fetch_website` extracts it
  (`enricher.py:204-206`), so the record has `whatsapp` set and `phone`/`email`/
  `instagram` all empty. `lead_score` scores it +15 and `completeness_score` gives it
  a point, but `has_any_contact(r)` returns `False`, so `main.py:204` drops it from
  `sales_ready.csv`. In Lebanon/KSA, where WhatsApp is the dominant B2B channel,
  this excludes exactly the leads the product is built to surface.
- **Fix:** Either fold `whatsapp` and `linkedin` into `has_any_contact`, or delete
  `has_any_contact` and gate `sales_ready` on the already-present contact score, e.g.
  reuse `lead_score`'s contact block. Smallest correct fix: add
  `or len(str(record.get("whatsapp") or "").strip()) > 3` and a `linkedin` check to
  `main.py:141-145` so the three definitions agree.

### S3 — `enrich()` computes `lead_score` that `main.py` immediately overwrites

- **Where:** `enricher.py:342` (`r["lead_score"] = lead_score(r)`) vs `main.py:197`
  (`r["lead_score"] = _lead_score(r)`); the recompute is already acknowledged in the
  comment at `main.py:195-196`.
- **Breaks:** The value computed inside `enrich()` is both **redundant** (every record
  is re-scored at `main.py:197`) and **incorrect** — at enrich time
  `industry_priority` is still `None` (it is set later at `main.py:190`), so the
  priority branch `lead_score` reads at `enricher.py:298-302` always contributes 0.
  The enrich-side score is therefore a partial score that is silently discarded.
  It is pure dead work: an O(n) recompute over every record, plus a latent trap if
  anyone ever calls `enrich()` outside `main.py` and trusts its `lead_score`.
- **Fix:** Delete `enricher.py:342` and let `main.py` be the single place that
  scores (it already is). If `enrich()` must remain self-contained, move the
  `industry_priority` assignment (`main.py:190`) into `enrich()` *before* the score
  instead of duplicating the call.

### S3 — `BusinessRecord`'s declared types are inert and mis-declare required fields

- **Where:** `scrapers/base.py:5-28` (the TypedDict); constructed at `scrapers/osm.py:95`,
  `scrapers/wikidata.py:75`, `scrapers/google_places.py:249`; contradicted by
  `main.py:96-101`.
- **Breaks:** `BusinessRecord` is used only as a **runtime constructor**
  (`BusinessRecord(...)`), which is just `dict(...)` — it validates nothing and enforces
  nothing. No type-checker is configured anywhere (BRIEF: "zero tests, zero packaging"),
  so the annotations are documentation that nothing consumes. Worse, they are *wrong*
  for real data:
  - `lead_score: int` (`base.py:25`) and `completeness_score: int` (`base.py:28`) are
    declared **required** (no `| None`), but `load_master` turns a blank CSV cell into
    `None` for both via `_INT_FIELDS` (`main.py:44`, `main.py:96-101`). A master round-trip
    can therefore produce `lead_score=None`, violating the contract.
  - `source: str` (`base.py:26`) and `scraped_at: str` (`base.py:27`) are declared required,
    but partial records entering `dedup._merge` can lack or null them, and downstream
    consumers read them with `record.get(...)` + fallback (`pitch_recommender.py:48`,
    `main.py:238`), i.e. they are already treated as optional.
  - The shape is declared a second time as `FIELDS` (`main.py:33-40`), and the two lists
    are kept in sync only by hand.
- **Fix:** Single-source the shape from `BusinessRecord.__annotations__` (derive `FIELDS`
  from it), mark genuinely-optional keys `NotRequired` (or `| None`), and either stop
  using a TypedDict as a constructor or add a runtime check. This overlaps prior audits
  (020, 021, 028) but the required-vs-`None` contradiction for `lead_score`/`completeness_score`
  is the concrete dead/incorrect-annotation issue worth acting on here.

### S3 — Three f-strings with no placeholders

- **Where:** `main.py:219` (`print(f"\nSummary:")`), `main.py:232`
  (`print(f"\n  By region:")`), `main.py:240`
  (`print(f"\n  Sales-ready by recommended service:")`).
- **Breaks:** The `f` prefix does nothing — there is no `{...}` in any of the three.
  Harmless at runtime but misleading (suggests interpolation that isn't happening).
  All other `print(f"…")` sites in `main.py`, `enricher.py`, `osm.py`, `wikidata.py`,
  and `google_places.py` interpolate something.
- **Fix:** Drop the `f` prefix on these three lines (make them plain string literals).

### S3 — `with_social` is computed but never exported (the "unused" premise is off)

- **Where:** `main.py:201` (definition) and `main.py:225` (sole use).
- **Breaks:** The brief lens called `with_social` "unused"; it is not — it is read at
  `main.py:225` for the summary line "With social media". The real redundancy is that
  it is the *only* subset computed alongside `with_websites`, `without_websites`,
  `sales_ready`, and `qualified` (`main.py:199-206`) that is **not written to any CSV**
  (`main.py:209-213`). The count is computed, printed once, then thrown away — the
  information cannot be consumed downstream. If a `with_social.csv` is wanted, it is
  missing; if it is not wanted, the subset materialization is wasted work over the
  full record list.
- **Fix:** Either emit a sixth CSV (and add it to the manifest/Drive upload), or drop
  the list and print `sum(1 for r in records if r.get("facebook") or r.get("instagram"))`
  inline at `main.py:225`.

### S3 — ~45 of ~50 `GEOGRAPHIC_NOISE` entries are unreachable from the OSM scraper

- **Where:** `scrapers/whitelist.py:85-102` (the set) vs the Overpass query tag keys at
  `scrapers/osm.py:10-26` and the category derivation at `scrapers/osm.py:68-75`.
- **Breaks:** OSM's `category` is always the **value** of one of exactly six tag keys —
  `shop`, `amenity`, `office`, `tourism`, `craft`, `healthcare`
  (`osm.py:68-75`), and the query only requests those keys (`osm.py:14-23`). Almost none
  of the noise strings are valid values of those keys:
  - `place=*` values — `village`, `town`, `city`, `hamlet`, `suburb`, `neighbourhood`,
    `quarter`, `borough`, `municipality`, `place`, `locality` — unreachable.
  - `natural=*`/`waterway=*` values — `mountain`, `peak`, `hill`, `ridge`, `valley`,
    `plateau`, `watercourse`, `river`, `stream`, `lake`, `spring`, `waterfall`, `wadi`,
    `bay`, `cape`, `island`, `beach`, `coast`, `forest`, `wood`, `grassland`, `meadow`,
    `wetland` — unreachable.
  - `historic=*` / `man_made=*` / `highway=*` values — `monument`, `memorial`,
    `archaeological_site`, `ruins`, `castle`, `fort`, `tower`, `obelisk`, `statue`,
    `highway`, `road`, `path`, `track`, `junction`, `roundabout`, `bus_stop` — unreachable.
  - `building=*` / `religion=*` values — `church`, `mosque`, `temple`, `shrine`,
    `synagogue` — unreachable (the amenity value is `place_of_worship`).
  - Non-tags: `village/town/city in Lebanon` (a concatenated description no scraper
    emits), `human settlement`, `fuel_dispenser` (the real tag is `amenity=fuel`, so the
    category would be `fuel`), `metaorganization`, `organization` — unreachable from OSM
    (some, e.g. `human settlement`/`organization`, can come from Wikidata's `P31` label).
  - Only these can match an OSM category: `place_of_worship`, `grave_yard`, `monastery`,
    `parking`, `yes`.
- **Trigger:** Any OSM `amenity=place_of_worship` node → `category="place_of_worship"`,
  matched at `whitelist.py:115`. A `building=church` detail is never surfaced, so
  `"church"` in the set is dead for OSM specifically. A `natural=peak` element is not
  even fetched by the query, so `"peak"` is doubly unreachable.
- **Fix:** The block is not globally dead — `is_business_category` also filters Wikidata
  (`P31` labels like `mountain`, `village`) and Google Places `types`, so the entries
  still serve those sources. But for OSM the comment "irrelevant records leaking from
  OSM and Wikidata" (`whitelist.py:4-5`) is wrong about OSM: the query structurally
  cannot emit those categories. Either (a) reduce the OSM-relevant hard-block to the five
  reachable values and keep the rest under a clearly-labeled Wikidata/Google section, or
  (b) leave the set but fix the comment and add a note that it is a Wikidata/Google
  filter, not an OSM filter. Do not blindly delete the entries — that would un-filter
  Wikidata geographic noise.

## Not a bug, but worth knowing

- **`main.py:186`** `print("\nEnriching records …")` is already a plain string (not an
  f-string) — good; it is the three lines in the summary block (`219`, `232`, `240`) that
  need the `f` removed.
- **`PRIORITY_INDUSTRIES` contains `"tourism"`** (`whitelist.py:23`), which is a tag
  *key*, not a value; OSM will never emit a category literally equal to `"tourism"`.
  Same class of bug as the `GEOGRAPHIC_NOISE` entries, but in the allow-list, so it is a
  dead allow entry rather than a dead block entry. Worth fixing in the same pass.
- The enrich-side `lead_score` (`enricher.py:342`) and the `with_social` subset
  (`main.py:201`) both already have adjacent prior-audit coverage (050, 044); this report
  adds the concrete line-level mismatch rather than new scope.

## Recommended order of work

1. **S2** — reconcile `has_any_contact` (`main.py:137-145`) with the contact fields the
   rest of the pipeline already treats as contactable (`whatsapp`, `linkedin`). One-line
   change, fixes a real output gap in `sales_ready.csv`.
2. **S3** — delete `enricher.py:342` (dead recompute); the score already lives at
   `main.py:197`.
3. **S3** — drop the `f` prefix at `main.py:219`, `232`, `240`.
4. **S3** — decide `with_social`: emit a CSV or inline the count.
5. **S3** — correct the `GEOGRAPHIC_NOISE` comment/scope and (separately) the dead
   `"tourism"` allow entry in `whitelist.py`.
6. **S3** — single-source `FIELDS` from `BusinessRecord` and fix the required-vs-`None`
   annotations for `lead_score`/`completeness_score`.
