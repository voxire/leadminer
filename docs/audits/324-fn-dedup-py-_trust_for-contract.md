# 324 — `_trust_for` (dedup.py:148): the trust lookup cannot fail, and its answer is not about the value it ranks

## Verdict

`_trust_for` is the only place in the merge that decides **which source wins a field**, and it has
three defects that all fail silently. (1) It returns `_DEFAULT_TRUST = 1` for any `field` it has no
entry for *and* for any `source` string it cannot parse, so "this source has no opinion about this
field" is indistinguishable from "I do not know what source this record is". (2) It returns a **max
over the record's whole provenance set**, not the provenance of the candidate value — and because the
master CSV is re-merged into every run (`main.py:179-180`) that aggregate only ever grows, so an
accumulated master row permanently outranks freshly scraped ground truth. (3) `country` and `region`
have **no entry in any `_SOURCE_TRUST` map**, so `_pick` resolves them with the `str(value)` tiebreak
at `dedup.py:184` — that is, alphabetically.

Verified end to end against the real code: two consecutive runs turn a Beirut row into
`country=SA, region=Riyadh, address="Hamra, Beirut, Lebanon", lat=33.895, phone=+9611234567`, with
no exception, no log line, and no way to tell from the output that a decision was made at all.

**Scope note:** `docs/audits/020-type-contract-import-cycle.md` and `024-dedup-merge-data-loss.md`
both analyse `_merge`, but against the *pre-`99493b9`* implementation (`dedup.py:46` was
`all_keys = set(a) | set(b)` with `_field_count` deciding whole-record richness). The current
`_merge` (`dedup.py:190-194`) no longer has `_field_count`, and its order-independence claim is
half-true — see S2 below. Everything here is against `dedup.py` at `99493b9`.

---

## Contract inventory (the two questions asked, answered exhaustively)

**What `_trust_for` reads.** Exactly one field: `source`, and only through `_sources_of`
(`dedup.py:143-145`, `record.get("source")`). `source` **is** in the TypedDict (`base.py:26`,
declared `str`, required). So the one field it touches is nominally in-contract.

**What `_trust_for` writes.** Nothing. It is pure.

**What `_trust_for` is *asked about* that is not in the TypedDict.** Every key of
`set(a) | set(b)` (`dedup.py:192-193`), which is the union of whatever the two dicts happen to
carry — the 23 `BusinessRecord` keys are not enforced, filtered, or checked. Proven:

```
_merge({"name":"X","phone":"1","source":"osm","notes":"human typed this"}, {...})
  -> {'name': 'X', 'source': 'osm', 'phone': '1', 'notes': 'human typed this'}
  _trust_for('notes', m) = 1        # silently, no warning
```

`write_csv` then drops `notes` via `extrasaction="ignore"` (`main.py:125`), so a non-contract column
enters the merge, participates in `_pick`, and vanishes — the worst of both.

**`BusinessRecord` fields with no trust entry in any source** (i.e. `_trust_for` returns 1 for all
three sources, verified):

```
completeness_score, country, industry_priority, lead_score, linkedin,
recommended_service, region, scraped_at, source, website_live
```

`source` and `scraped_at` are short-circuited inside `_pick` (`dedup.py:173,176`) and
`industry_priority` / `recommended_service` / `lead_score` / `completeness_score` are overwritten
downstream unconditionally (`main.py:190-197`, `enricher.py:349-350`), so four are harmless.
`website_live` is in `_VOLATILE` so recency breaks its tie. **`country`, `region` and `linkedin`
are the three that reach the `str(value)` tiebreak with nothing but lexicographic order to decide
them.** That is S1.

**How the return value is consumed.** `_pick` builds a 4-tuple with `_trust_for` at **index 0**
(`dedup.py:180-185`) and compares it with `>=` (`dedup.py:187`). So the `-> int` is load-bearing,
compared *first*, and only ever order-compared — never printed, never persisted, never asserted.
There is no place in the codebase where a wrong trust value is observable except as a wrong field
value. Proven fragility (see S3): a `_trust_for` that returns `None` for one source and `2` for
another produces `TypeError: '>=' not supported between instances of 'NoneType' and 'int'` from
inside `_merge`, with the traceback naming `dedup.py:187` and not one hint that trust was involved.

---

## Findings

### S1 — `country` and `region` are decided alphabetically, silently relabelling businesses across markets

- **Where:** `dedup.py:97-110` (`_SOURCE_TRUST` has no `country` / `region` entry for any source),
  `dedup.py:111` (`_DEFAULT_TRUST = 1`), `dedup.py:184` (`str(value),  # deterministic final
  tiebreak`), `dedup.py:187` (`av if rank(a, av) >= rank(b, bv) else bv`)
- **Breaks:** for `country` and `region`, `_trust_for` returns 1 on both sides, `_validity` returns 1
  on both sides (`dedup.py:140`), and neither is in `_VOLATILE` (`dedup.py:114`), so the third tuple
  element is `""` on both sides. The decision reaches the fourth element and becomes
  `max("LB", "SA")` → **`"SA"`, always, in both argument orders**. `"SA" > "LB"` is a fact about
  the alphabet, not about the data. Downstream:

  ```
  merged country = SA | resolve_country -> SA            # main.py:184; "SA" is accepted at main.py:62
  infer_region("Hamra, Beirut, Lebanon", 33.895, 35.483, country="SA") -> None   # enricher.py:90
  infer_region(                          33.895, 35.483, country="LB") -> "Beirut"
  ```

  `enricher.py:330-335` only fills `region` `if not r.get("region")`, and `region` arrived as
  `"Riyadh"`, so it is **never re-inferred**. The row keeps `region=Riyadh` and `country=SA`
  forever, in `all_businesses.csv`, in the per-region summary (`main.py:228-234`), and in
  `leadminer stats` (`cli.py:98-103`). `lat`/`lon` are *also* absent from `_VOLATILE`, so the stale
  Beirut pin then wins its own tiebreak by string order (`max("24.71", "33.895")`).

- **Trigger (two consecutive real runs, no data corruption required):**
  `google_places._country_from_query` (`google_places.py:127-136`) assigns `country` by **substring
  match on the query text**, not from the result. Its own query list includes
  `"perfume brands in Saudi Arabia"`, `"SaaS companies in Saudi Arabia"`,
  `"law firms in Riyadh"`, `"restaurants in Dammam"` (`google_places.py:86-124`), any of which can
  return a Levant business. `seen_ids` (`google_places.py:181`) dedups by place id **within one run**,
  so across runs the same place is re-emitted under whatever query surfaced it:

  ```
  run 1  query "cafes in Beirut"          -> country="LB", region="Beirut", lat=33.895, phone="+961 1 234 567"
  run 2  query "perfume brands in ..."   -> country="SA", region="Riyadh", lat=24.71,  phone="+9611234567"
  dedup(run2 + master)                   -> country=SA  region=Riyadh  lat=33.895
                                            address="Hamra, Beirut, Lebanon"  phone="+9611234567"
  ```

  All three records share one normalized phone key, because `normalize_phone` returns early for any
  number carrying a known country code (`dedup.py:57-60`) and is therefore **country-independent by
  design** — the comment at `dedup.py:5-7` says this is intentional ("data leaks across borders
  constantly"), so the collision is a designed-for case with no trust entry to handle it.

- **Second, independent trigger:** OSM is Lebanon-only (`osm.py:13`) and hardcodes
  `country="LB"` (`osm.py:103`); Google KSA queries hardcode `"SA"`
  (`google_places.py:135`). One shared phone between an OSM node and a KSA-labelled Google result
  merges them, and `country` becomes `"SA"`.
- **Fix:** stop letting a controlled-vocabulary field fall through to the string tiebreak. Either
  give `country`/`region` explicit entries, or special-case them next to `source`/`scraped_at` in
  `_pick` — `country` should resolve by *majority of the source set*, and `region` by
  `infer_region(address, lat, lon, country=<resolved country>)` rather than by `str()`. Add
  `lat`/`lon` to `_VOLATILE`.

### S1 — `_trust_for` scores the record's *paper trail*, not the candidate value, and the paper trail only ever grows — so the master permanently outranks fresh scrapes

- **Where:** `dedup.py:148-152` (`best = max(best, ...)` over `_sources_of(record)`),
  called once per candidate at `dedup.py:181`; the master is folded into the same comparison at
  `main.py:179-180` (`combined = raw_filtered + master` → `dedup(combined)`)
- **Breaks, two distinct ways:**

  **(a) Laundering.** `_merge` for `source` (`dedup.py:173-174`) unions the source sets, so a
  record that has been merged once carries *every* source's trust for *every* field — including
  fields whose surviving value came from the least-trusted source, or from a source that had no
  value at all:

  ```
  merge(google website=https://new.example, osm email=real@cafe.example, osm website=None)
    -> website='https://new.example'   # Google's value survived
       _trust_for('website', merged) = 3      # OSM's score, and OSM had no website at all
       _trust_for('website', google) = 2      # what it should be
  ```

  **(b) Monotonicity, which is the part that compounds.** Because trust is a max over a set that
  only grows, a master row's trust per field is a **ratchet**. Once a record has been touched by
  any source with a 3 for field *F*, it scores 3 for *F* forever — and since `_trust_for` is index
  0 of the rank tuple, it beats recency, validity and freshness outright:

  ```
  run 1: osm website=http://stale.example  +  google website=https://new.example
         -> master website='http://stale.example', source='google_places|osm'   (osm trust 3 beats google 2)

  run 2: OSM's website tag has rotted away; Google has corrected its websiteUri to
         https://correct.example  (source='google_places', trust 2)
         dedup([fresh] + [master])  ->  website='http://stale.example'
  ```

  The URL this run actually scraped is discarded because a record from a *previous* run once shared
  a phone number with an OSM node. Proven against the real `dedup()`.
- **Trigger:** any record whose master row carries a multi-source `source` string and whose
  high-trust source has since stopped reporting the field. At 40 KSA queries + 35 LB queries against
  a Places API with drift, plus OSM tag rot, this is the normal case, not an edge case.
- **Fix:** trust must be a property of *(field, candidate value)*, not of the record. Track the
  winning source per field — e.g. have `_pick` return `(value, source)` and let `_merge` record
  `merged["_src_website"] = winner_source` — and score with
  `_SOURCE_TRUST[winner_source].get(field, _DEFAULT_TRUST)`. Cheaper stopgap if that is too big:
  stop folding the master into `_merge` (dedup the fresh batch, then join onto the master on the
  same keys) and always let the master lose on `_VOLATILE` fields.
- **Severity defence:** S1 because it is silent wrong output in the shipped CSV, it is
  *self-reinforcing* (the master is the input to the next run), and the operator has no signal —
  the run summary (`main.py:219-242`) reports counts, not merge decisions.

### S2 — `_trust_for` has no failure mode: 10 distinct malformed inputs all return the same `1`

- **Where:** `dedup.py:148-152`, feeding on `_sources_of` (`dedup.py:143-145`) and
  `_DEFAULT_TRUST = 1` (`dedup.py:111`)
- **Breaks:** the function returns `_DEFAULT_TRUST` for (i) an unknown *source* string and (ii) an
  unknown *field* name, with the same value and no distinction. Verified:

  ```
  key missing entirely                      -> _sources_of=[]                     email=1 website=1 rating=1
  source=None (load_master blank cell)      -> _sources_of=[]                     email=1 website=1 rating=1
  source=''                                 -> _sources_of=[]                     email=1 website=1 rating=1
  source='osm'      (trailing space)        -> _sources_of=['osm ']                email=1 website=1 rating=1
  source='OSM'      (case)                 -> _sources_of=['OSM']                 email=1 website=1 rating=1
  source='osm,google_places' (one operator)-> _sources_of=['osm,google_places']   email=1 website=1 rating=1
  source=['osm']    (list)                 -> _sources_of=["['osm']"]             email=1 website=1 rating=1
  source=123        (int)                  -> _sources_of=['123']                 email=1 website=1 rating=1
  source='google_places'                    -> _sources_of=['google_places']      email=1 website=2 rating=3
  source='osm'                               -> _sources_of=['osm']                email=3 website=3 rating=1
  ```

  `source='osm '` is not hypothetical: `load_master` (`main.py:86-104`) never strips the cell, and
  the master is a Google-Drive CSV that humans open and edit. It is also what happens the moment
  `_pick`'s `"|".join(sorted(...))` (`dedup.py:174`) is fed anything it did not produce.
  Consequence: a provenance string drifts by one space, and every field in that record silently
  drops to trust 1 — so the merge reverts to pure validity + recency + `str()`, which for
  `country`/`region`/`linkedin` is pure alphabet. And a typo in `_SOURCE_TRUST` itself — a field
  name spelled wrong, a source renamed — is **indistinguishable from a deliberate trust of 1**.
  There is no assertion, no log, no counter.
- **Trigger:** add one field to `_SOURCE_TRUST` with a typo, or edit one `source` cell in the
  master. Both produce a plausible-looking run and a subtly wrong merge.
- **Fix:** make the vocabulary explicit and closed. Parse `source` against
  `frozenset(_SOURCE_TRUST)`, `raise` (or count and report at end of run) on an unrecognised token,
  and raise on a `field` that is not a member of the record contract. `enricher.py` already
  distinguishes `LIVE`/`DEAD`/`UNKNOWN` as named constants (`enricher.py:154-156`) — the same
  discipline applies here.

### S2 — `_merge` is commutative but **not associative**, so `dedup()`'s output depends on list order, and `main.py` supplies a thread-completion order

- **Where:** `dedup.py:190-194` (`_merge`, applied repeatedly left-fold by `dedup.py:208/216`),
  root cause at `dedup.py:148-152` + `dedup.py:181`; input order supplied by
  `main.py:160-166` (`for future in as_completed(futures)`)
- **Breaks:** exhaustive random search over 30 000 triples of realistic records:

  ```
  30000 random triples: non-commutative=0   non-associative=5298   (17.7%)
  ```

  `_pick` is pairwise order-independent (ties on the first three tuple elements are resolved by
  `str(value)`, which is itself commutative — `dedup.py:184`), so
  `tests/test_lead_signal.py:292-295` (`test_merge_is_order_independent`) passes and is true, but
  it only tests *pairs*. Three or more records diverge. The mechanism: an intermediate merged
  record's `_trust_for` is the max over the *union* of its sources, and that inflated number is
  then attributed to whichever value happened to win the earlier, differently-grouped merge.

  Minimal counterexample, run through the real `dedup()` with the same normalized phone key:

  ```
  a = google_places, email='x@y.com'          (google has no `email` entry  -> trust 1)
  b = osm,          email=None                (osm trust 3)
  c = osm,          email='mailto:x@y.com'    (osm trust 3)

  dedup([a,b,c]) -> email='x@y.com'
  dedup([c,b,a]) -> email='mailto:x@y.com'
  ```

  `_merge(a,b)` keeps Google's address (OSM's is missing) and inherits OSM's `email: 3`, so `c`
  can never win the tiebreak. `_merge(b,c)` keeps `c`'s address and scores 3 legitimately, so `a`
  never gets a look in. Same three records, same phone key, two different emails — and
  `main.py:160-166` collects scraper batches in `as_completed` order, i.e. **whichever HTTP request
  finished first**, so the master CSV is not reproducible across runs even on identical input.
- **Trigger:** any business confirmed by three or more records where the master and at least two
  scrapers disagree on a field. In a multi-run accumulating pipeline this is every business that has
  ever been matched more than twice.
- **Fix:** make the fold order-independent by making trust a property of the value
  (`merged["_src_<field>"]`), which also fixes S1(b) — one change, two findings. Failing that,
  sort `raw_filtered` by a stable key before `dedup` (`main.py:179`) so at least the run is
  reproducible, and add a 3-record non-associativity test.

### S2 — `source` is simultaneously the provenance key and a scoring input: a typo both zeroes trust and inflates `lead_score`

- **Where:** `dedup.py:143-145` (`_sources_of`, splits on `"|"` with no vocabulary check),
  `dedup.py:173-174` (`_pick` re-serialises `source`), `dedup.py:144` (`str(... or "")`),
  `enricher.py:316-317` (`if "|" in str(record.get("source") or ""): score += 5`)
- **Breaks:** two consumers read the same unvalidated free-text blob with incompatible parsers.
  `enricher` asks only "does this string contain a pipe?", `_sources_of` asks "is this string a
  pipe-joined list of known source names?". Proven:

  ```
  source='osm'                 trust(website)=3  trust(email)=3  lead_score=25
  source='osm '                trust(website)=1  trust(email)=1  lead_score=25
  source=' osm|google_places'  trust(website)=2  trust(email)=1  lead_score=30   <- +5 for ONE source
  source='osm,google_places'   trust(website)=1  trust(email)=1  lead_score=25
  source='osm|google_places'   trust(website)=3  trust(email)=3  lead_score=30
  ```

  A single operator who writes `"osm,google_places"` gets a single-source record; two operators who
  write `"osm|google_places"` get the multi-source +5. Both survive into the master CSV, and the
  +5 is permanent because the field is never recomputed from provenance.
  `_pick` compounds it by *re-serialising* junk rather than rejecting it:
  `_merge({'source':'osm,google_places'}, {'source':'google_places'})['source']` →
  `'google_places|osm,google_places'` — the malformed token is now two tokens, one of which
  outrides the valid one and will never parse.
- **Trigger:** one cell in `all_businesses.csv` edited by a human in Sheets.
- **Fix:** store provenance as a real list (`sources: list[Source]`, `Source = Literal["osm",
  "wikidata", "google_places"]`), derive the `"|"` string only at CSV-write time, and compute the
  multi-source bonus from `len(sources) > 1` instead of a substring test.

### S3 — `record: dict` vs `BusinessRecord`: the `field` argument is unbounded, non-contract keys are merged, and `-> int` is an unchecked ordering contract

- **Where:** `dedup.py:148` (`def _trust_for(field: str, record: dict) -> int`),
  `dedup.py:190` (`def _merge(a: dict, b: dict) -> dict`), `dedup.py:159`
  (`def _pick(field: str, a: dict, b: dict) -> object`),
  `base.py:5-28` (`BusinessRecord`, 23 keys, all required)
- **Breaks, three small things:**

  1. **Unbounded `field`.** The parameter is `str`, but the caller feeds it `set(a) | set(b)`
     (`dedup.py:192-193`). Nothing narrows it to the 23 contract keys, so `_trust_for` is called
     with arbitrary column names and answers `_DEFAULT_TRUST` (see S2 above). `_merge` therefore
     also *merges* non-contract keys (`_pick`'s `av if _is_missing(av) ...` runs on them) and
     *propagates* them — `_merge`'s output keyset is exactly `set(a) | set(b)`, never a subset.

  2. **Three hand-maintained key lists, no linkage.** `BusinessRecord.__annotations__` (23),
     `main.FIELDS` (`main.py:33-40`, 23) and the union-of-whatever `dedup` iterates. Today the
     first two happen to have the same key set (verified `True`); nothing enforces it, and the third
     is unbounded. `BusinessRecord` itself also declares `lead_score: int` and
     `completeness_score: int` as **required** (`base.py:25,28`) while `dedup` runs *before* either
     is ever computed (`main.py:180` precedes `enricher.py:349-350`), and `main.py` does not import
     `BusinessRecord` at all.

  3. **`-> int` is an unasserted ordering contract consumed at tuple index 0.** `dedup.py:181,187`
     compare it with `>=` before anything else. Nothing enforces `int`, the range, or even
     comparability:

     ```
     _trust_for returns None (for osm) and 2 (for google)  ->  TypeError:
         '>=' not supported between instances of 'NoneType' and 'int'
     ```

     raised from `dedup.py:187`, with `_pick`/`_merge`/`dedup` in the traceback and no mention of
     trust. It only surfaces when the two values are *unequal*, because tuple comparison skips
     indices that compare equal — so the failure condition is itself non-obvious. Today
     `_trust_for` cannot produce this, which is why it is S3 and not S1: it is a trap for the next
     person who "improves" the function (e.g. to return a `(trust, reason)` tuple, which compares
     fine, or `None` for unknown, which explodes).

     Relatedly, `_trust_for`'s `record: dict` is stricter than it needs to be: the function only
     calls `.get` (verified — a read-only `collections.abc.Mapping` works), while the sibling
     `recommend_service` (`pitch_recommender.py:25`) already honestly declares `Mapping`. And
     `_trust_for(field, object())` is an unguarded `AttributeError` — `_pick` and `_merge` declare
     `dict` too, so nothing catches it either.
- **Fix:** `field: str` → a `Literal`/keyed alias over `BusinessRecord.__annotations__`; iterate
  `for key in FIELDS` in `_merge` instead of `set(a) | set(b)`; assert
  `isinstance(t, int) and not isinstance(t, bool)` in `_pick` where it is consumed.

---

## Not a bug, but worth knowing

- **`_merge` guarantees the union keyset.** `_pick` returns `bv` when `av` is missing and vice
  versa (`dedup.py:167-171`), so every key of `set(a) | set(b)` is present in the output. That is
  genuinely good — no field is dropped — and it is why `enricher.py:340-348` only needs `setdefault`
  for 9 fields. It is also why S3(1) matters: "no field is lost" and "no *foreign* field is gained"
  are both properties someone has to choose, and this code chose the first and inherited the second.

- **`_is_missing` is doing real work and the docstring is accurate.** `dedup.py:79-90` treating `""`
  as missing is what makes the `website` trust-3-vs-2 rule actually reachable from real CSV data,
  where every blank cell is `""`. `tests/test_lead_signal.py:286-290` covers it.

- **Trust dominates validity, so a malformed high-trust value beats a valid low-trust one.** Not
  itself a `_trust_for` bug (the ordering at `dedup.py:180-185` is deliberate and documented), but
  it means `_trust_for`'s weight is only as good as the trusted source's *normalisation*, which is
  uneven:

  ```
  _trust_for('email', osm)=3,  _validity('email','mailto:owner@cafe.example')=3
  _trust_for('email', wikidata)=2, _validity('email','owner@cafe.example')=3
  merge -> 'mailto:owner@cafe.example'

  merge(osm website='https://www.facebook.com/PageName', google website='https://cafe.example')
    -> website = 'https://www.facebook.com/PageName'
  ```

  `osm.py:90` reads `contact:email` **without** stripping the `mailto:` scheme, whereas
  `wikidata.py:97-98` explicitly strips it (`_strip_prefix`, `wikidata.py:47-51`) — so OSM gets the
  *higher* trust and the *less* normalised value, and the `mailto:` form wins and is never
  overwritten (`enricher.py:252` only fills `email` when empty). Separately,
  `osm.py:91` accepts `website`, `contact:website` *and* `url`, so an OSM node whose only URL is a
  Facebook page outranks Google's real `websiteUri` — which removes the business from
  `without_websites.csv`, the product's headline output. The contract fix belongs in the scrapers;
  the *trust table* is what makes them wrong.

- **`_recency` is a raw string compare (`dedup.py:156`), and it is correct for the data the
  scrapers actually produce.** All three call `utc_now_iso()` (`osm.py:32`, `wikidata.py:67`,
  `google_places.py:173`), which returns `+00:00`-suffixed aware ISO (`httpclient.py:220-229`), so
  lexicographic order equals chronological order. Two identical-instant spellings still diverge —
  `max("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00")` picks the `Z` form (`0x5A > 0x2B`) —
  but that needs a hand-edited master CSV. `max()` on two strings cannot raise, so this is a silent
  wrong-ordering risk, not a crash; the naive-vs-aware `TypeError` that `httpclient.py:223-225`
  warns about cannot happen on this path.

- **`website_live` is in `_VOLATILE` (`dedup.py:114`) but is never merged meaningfully.** It is
  `None` in every scraper emit (`osm.py:109`, `wikidata.py:117`, `google_places.py:292`) and is only
  set by `check_websites` (`enricher.py:250`), which runs *after* `dedup` (`main.py:180` before
  `main.py:187`). Its only real inputs are the master row (via `load_master`, `main.py:103`) and
  fresh scrapes' `None`, so `_pick` short-circuits on `_is_missing` every time. Harmless today; the
  entry is documentation of an intent the pipeline does not implement.

- **`_pick`'s `_is_missing` short-circuit runs before the `source`/`scraped_at` special cases
  (`dedup.py:167-176`).** For `source` that means a master row with a blank cell adopts the fresh
  record's source outright rather than unioning — which is the *desired* recovery behaviour, but it
  also means a single blank cell silently erases a record's provenance history, and with it every
  accumulated `_trust_for` boost from S1(b). Same mechanism, opposite valence.

- **Nothing calls `_trust_for` outside `dedup.py`.** Verified across the tree: the only external
  references to any merge internal are `tests/test_lead_signal.py:65,278-326`, and the only
  `_pick` name collision is `google_places._pick_category` (`google_places.py:279,324`), which is
  unrelated. So the blast radius of every finding above is exactly `dedup()`'s output — and that
  output is the entire CSV product. There is no second consumer to notice a regression, and no
  existing test asserts any trust value directly.

---

## Recommended order of work

1. **S1 (country/region alphabetical)** — the only finding that produces a row whose own fields
   contradict each other (`country=SA` + `address="Hamra, Beirut"` + `lat=33.895`). Fix `_pick` to
   resolve `country` by majority-of-sources and `region` via `infer_region`, add `lat`/`lon` to
   `_VOLATILE`. Small, local, and it stops the incoherence from compounding in the master.
2. **S1 (trust aggregate / master ratchet)** — the largest correctness win and the only one that
   gets *worse every run*. Requires per-field provenance tracking, so pair it with S2(associativity):
   one change fixes both.
3. **S2 (`_trust_for` cannot fail)** — close the `source` vocabulary and make an unrecognised
   `field` or token loud. Do this *before* any of the above, because it is what makes the `source`
   and `_SOURCE_TRUST` typos invisible while you are fixing them.
4. **S2 (`source` is also a score input)** — store provenance as a list; derive the CSV string and
   the multi-source bonus from it. Do this alongside 3, same code.
5. **S2 (non-associativity / thread-order input)** — fixed for free by 2; until then, sort
   `raw_filtered` deterministically at `main.py:179` so runs are at least reproducible, and add a
   3-record order-permutation test next to `tests/test_lead_signal.py:292`.
6. **S3** — narrow `field`, iterate `FIELDS` in `_merge`, assert the `-> int` contract where it is
   consumed, and switch `record: dict` → `Mapping` for consistency with `pitch_recommender.py:25`.
7. **Not in this report's scope but on the same trust table:** normalise `mailto:` in `osm.py:90`
   and stop treating `url`/`contact:website` social links as `website` in `osm.py:91`, or the
   `osm: {website: 3, email: 3}` weights will keep importing OSM's formatting noise into the output.