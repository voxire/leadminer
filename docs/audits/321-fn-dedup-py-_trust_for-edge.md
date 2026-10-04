# 321 — `_trust_for` edge lens: trust laundering, and a floor that turns merges alphabetical

## Verdict

`_trust_for` itself is short and cannot crash on any input the pipeline can actually
produce, but it is the root cause of two silent-wrong-value failures. First, it returns
the **max** trust across a record's whole `source` set — and `dedup._pick` rewrites
`source` into a permanent union at `dedup.py:174` — so after a single merge any surviving
value inherits the trust of a source that never supplied it (`email` trust goes 1 → 3 at
`dedup.py:151`). Second, because `_DEFAULT_TRUST = 1` is both the floor and the answer
for every field absent from the trust table, **10 of the 23 columns are unranked**, and
for those the merge is decided by `str(value)` at `dedup.py:184` — i.e. alphabetically.
On `region` that produces a Saudi lead labelled `Mount Lebanon` in 17 of the 40
possible cross-border region pairs, deterministically and permanently.

Neither failure raises, logs, or counters anything. Both are baked into
`all_businesses.csv` and are re-read on every subsequent run.

---

## Contract

```python
# dedup.py:148-152
def _trust_for(field: str, record: dict) -> int:
    best = _DEFAULT_TRUST
    for src in _sources_of(record):
        best = max(best, _SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST))
    return best
```

Documented intent (`dedup.py:93-96`): *"How much each source is trusted per field …
Trust therefore has to be per-field, not per-record."*

What it actually computes: **the maximum per-field trust over every source the record has
ever been merged with.** Those are not the same thing once `source` is a union.

Single caller: `rank()` at `dedup.py:181`, itself only reached from `_pick`
(`dedup.py:187`) → `_merge` (`dedup.py:193`) → `dedup` (`dedup.py:208`, `dedup.py:216`).

---

## Findings

### S1 — Trust laundering: a value inherits the trust of a source that never supplied it

- **Where:** `dedup.py:151` (the `max`), `dedup.py:174` (`source` becomes a union),
  `dedup.py:181` (trust is the first, dominant element of the rank tuple)
- **Breaks:** `_pick` at `dedup.py:174` sets a merged record's `source` to
  `"|".join(sorted(_sources_of(a) | _sources_of(b)))` — a union of every source that has
  ever touched the record, regardless of which source supplied each field. `_trust_for`
  then takes the **max** across that union. So after Google Places (email trust 1, because
  `google_places.py:290` hardcodes `email=None`) merges with an OSM record that had *no*
  email at all, Google Places' junk email is now scored 3 — OSM's trust (`osm` email = 3,
  `dedup.py:103`) on a field OSM never contributed.
- **Because trust is tuple element 0** (`dedup.py:181`) and `_validity` is element 1
  (`dedup.py:182`), laundered trust 3 beats a real, syntactically valid value at trust 2.
  `_validity("email", "info@")` = 0 vs `_validity("email", "hello@cafex.example")` = 3,
  and trust wins anyway.
- **Trigger (fully realistic — field sets copied verbatim from the three scrapers):**
  two Wikidata entities for one shop with disagreeing emails plus one OSM row with no
  email. Six arrival orders, **two produce the broken address**:

```
per-source email trust: {'wikidata': 2, 'osm': 3}
  OK  order=W_BAD|W_GOOD|OSM   email='hello@cafex.example'   source='osm|wikidata'
  BAD order=W_BAD|OSM|W_GOOD    email='info@'                 source='osm|wikidata'
  OK  order=W_GOOD|W_BAD|OSM    email='hello@cafex.example'   source='osm|wikidata'
  OK  order=W_GOOD|OSM|W_BAD    email='hello@cafex.example'   source='osm|wikidata'
  BAD order=OSM|W_BAD|W_GOOD    email='info@'                 source='osm|wikidata'
  OK  order=OSM|W_GOOD|W_BAD    email='hello@cafex.example'   source='osm|wikidata'
```

  Arrival order is not under the pipeline's control: `main.py:160-166` appends each
  scraper's batch in `ThreadPoolExecutor` `as_completed` order, which varies run to run.
  **The same three records produce a different `email` column on different runs.**
- **Why it never self-heals:** the laundered row is written to `all_businesses.csv`
  (`main.py:209`), read back by `load_master` (`main.py:150`), and prepended to the next
  run's input at `main.py:179`. Verified round-trip:

```
  written to all_businesses.csv: source='google_places|osm|wikidata' email='shop@'
  reloaded on run N+1 -> email trust = 3
```

- **Fix:** stop reading trust off the record. Have `_pick` carry per-field provenance
  (a parallel `field -> source` map merged alongside the record) and rank on the trust of
  the source that owns the *surviving* value. Minimum viable stopgap: keep
  `_SOURCE_TRUST` but make `_pick` pass the *incoming* record's source for `b` and the
  *pre-merge* accumulated source set for `a`, and refuse to let a union raise a value's
  trust above the trust of a source that actually emits that field
  (`_SOURCE_TRUST[src]` containing the key at all).

---

### S1 — `_DEFAULT_TRUST` floor makes 10 of 23 columns unranked, so `str(value)` decides them

- **Where:** `dedup.py:151` (`.get(field, _DEFAULT_TRUST)`), `dedup.py:111`
  (`_DEFAULT_TRUST = 1`), `dedup.py:184` (`str(value)  # deterministic final tiebreak`)
- **Breaks:** the trust table only covers 13 fields. Everything else scores 1 for every
  source, and `_validity` returns a flat 1 for all non-special fields
  (`dedup.py:140`), and none of them are in `_VOLATILE` (`dedup.py:114`) so
  `dedup.py:183` contributes `""`. `rank()` collapses to `(1, 1, "", str(value))` and the
  alphabetically **greater** string wins.

```
fields _SOURCE_TRUST can discriminate: ['address','category','email','facebook',
  'instagram','lat','lon','name','phone','rating','review_count','website','whatsapp']
fields that ALWAYS score 1 for every source: ['completeness_score','country',
  'industry_priority','lead_score','linkedin','recommended_service','region',
  'scraped_at','source','website_live']
```

- **Trigger (`region`, with the real closed value sets from `enricher.py:58-76`):**
  `region` is a *derived* field — `google_places.py:275` computes it via
  `infer_region`, and `osm.py:100` / `wikidata.py:110` hardcode `region=None` — yet
  `_merge` treats it as an independently observed attribute and merges it. For the 40
  possible Saudi-region × Lebanese-region pairings, **17 resolve to the Lebanese label**:

```
   SA Riyadh   loses to LB South Lebanon     ('South Lebanon' > 'Riyadh')
   SA Jeddah   loses to LB North Lebanon     ('North Lebanon' > 'Jeddah')
   SA Dammam   loses to LB Mount Lebanon     ('Mount Lebanon' > 'Dammam')
   SA Mecca    loses to LB Mount Lebanon     ('Mount Lebanon' > 'Mecca')
   SA Medina   loses to LB North Lebanon     ('North Lebanon' > 'Medina')
   ... 17 of 40 pairs total
```

  `Riyadh` loses to `South Lebanon` because `S > R`. `Medina` loses to `Mount Lebanon`
  because `M-o` > `M-e`. End to end through `dedup()`, in **both** arrival orders:

```
  order=sa,lb  -> region='South'  country='SA' address='King Fahd Rd, Riyadh'
  order=lb,sa  -> region='South'  country='SA' address='King Fahd Rd, Riyadh'
```

  A Riyadh address and a Saudi country code with a `South Lebanon` region. The
  mislabel is permanent: `enricher.py:331` re-infers `region` only `if not r.get("region")`,
  and the value is non-empty. It also feeds `main.py:229-234`'s by-region summary and
  the sales pitch.
- **Same mechanism hits `linkedin`:** `_pick("linkedin", {… "https://…/sa-lead",
  "source":"osm"}, {… "https://…/lb-lead", "source":"google_places"})` returned
  `…/sa-lead` purely because `s > l`. A wrong company's LinkedIn profile is sold as a
  contact channel, and `main.py:139-145` counts it toward `sales_ready.csv`.
- **Reachability, stated honestly:** only `google_places` emits `region`, so this needs two
  GP rows for one phone with different region boxes (different coords, or `country`
  flipping between `enricher.py:90`'s two box tables), and/or a GP row merging against a
  prior run's master row. It is reachable, but it is rarer than the `email` case. The
  *mechanism* — unranked field decided by string order — is unconditional.
- **Fix:** never merge `country`, `region`, or `linkedin` by trust. Derive `country` and
  `region` exclusively after `dedup` from `resolve_country` (`main.py:50`) and
  `infer_region`, and treat a `country` mismatch as a **split**, not a merge — the module's
  own header (`dedup.py:5-7`) says *"Data leaks across borders constantly"*, and a
  cross-border merge is the one case where `str()` is guaranteed to pick the wrong country.

---

### S2 — `_sources_of` parses `source` with no normalisation, so any variant silently drops that source's trust

- **Where:** `dedup.py:143-145`
- **Breaks:** `raw.split("|")` and nothing else — no `strip()`, no `lower()`, no
  validation against the known source names. Every one of the 30 hostile variants below
  returns `_DEFAULT_TRUST` instead of the source's real trust, with **no exception, no log
  line, no counter**. A renamed source (`osm` → `osm_v2`), an alias
  (`openstreetmap`), or a space from a spreadsheet round-trip would silently drop OSM's
  `email`/`website`/`facebook`/`instagram`/`whatsapp` trust from 3 to 1 for the entire
  corpus and change every merge decision, undetectably.
- **Trigger:** each of these currently returns `1` where the intent is `3`:

```
  source=' osm'            (leading space)      -> 1
  source='osm '            (trailing space)     -> 1
  source='osm | wikidata'  (spaced union)       -> 1
  source='osm\twikidata'   (tab separated)      -> 1
  source='osm\nwikidata'   (newline separated)  -> 1
  source='OSM'             (uppercase)          -> 1
  source='OpenStreetMap'   (camel alias)        -> 1
  source='openstreetmap'   (lower alias)        -> 1
  source='osm_overpass'    (near-miss key)      -> 1
  source='\ufeffosm'       (BOM prefix)         -> 1
  source='osm\x00'         (NUL suffix)         -> 1
  source='google_places\n' (trailing newline)   -> 1
  source='المصدر'          (Arabic)             -> 1
  source='osmفي'          (Arabic suffix)      -> 1
  source='osmé'            (French accent)      -> 1
  source='osm🙂'           (emoji suffix)       -> 1
  source='🙂🙃'            (emoji only)          -> 1
  source=b'osm|wikidata'   (bytes)              -> 1
  source=['osm']           (list)               -> 1
  source={'osm': 1}        (dict)               -> 1
  source=0 / 0.0 / False   (falsy scalars)     -> 1   (via `or ""`, dedup.py:144)
```

  Note the double failure: `source=0`, `source=0.0`, `source=False` and `source=[]` all
  take the `or ""` branch at `dedup.py:144`, so a *present but falsy* source is
  indistinguishable from an *absent* one.
- **Reachability, stated honestly:** all three scrapers emit clean literals
  (`osm.py:119` `"osm"`, `google_places.py:302` `"google_places"`, `wikidata.py:127`
  `"wikidata"`) and `_pick` at `dedup.py:174` writes sorted, space-free unions. So
  **nothing in the current code path produces these values today.** This is latent. It
  becomes live the instant (a) a source is renamed or aliased, (b) an older
  `all_businesses.csv` from a previous code version is loaded, or (c) anyone opens a CSV
  in Excel and re-saves — which `main.py:118-119` explicitly invites: *"encoding is
  utf-8-sig so Excel and Google Sheets detect UTF-8."* `cli.py:259`
  (`cmd_score` → `pipeline.load_master`) also reads a user-supplied path.
- **Fix:** `return {s.strip().lower() for s in raw.split("|") if s.strip()}` and add a
  module-level `_KNOWN_SOURCES = {"osm","google_places","wikidata"}`; log a warning
  (dedup.py has no logger today — `enricher.py` and `scrapers/osm.py` do, so one is
  available) listing any source not in that set. A trust table whose misses are silent
  is the same class of bug the module's own docstrings complain about at `dedup.py:34-38`
  and `dedup.py:80-85`.

---

### S2 — No "no opinion" sentinel: a source that provably cannot emit a field is scored exactly like an unknown one

- **Where:** `dedup.py:151`
- **Breaks:** `.get(field, _DEFAULT_TRUST)` conflates *"this source has no opinion about
  this field"* with *"this source is unreliable for this field."*
  `google_places.py:290,292-296` hardcodes `email`, `facebook`, `instagram`, `whatsapp`
  and `linkedin` to `None` — Places simply does not expose them. Yet a
  `google_places` record scoring `1` on `email` is indistinguishable in the rank tuple
  from a record from a source nobody has heard of, and only one point below `wikidata`
  (`2`) and two below `osm` (`3`). That is the precondition that makes S1 possible: the
  laundered jump is only 1 → 3 *because* "no opinion" is expressed as `1`.
- **Trigger:** `_trust_for("email", {"source": "google_places"})` → `1`.
  `_trust_for("email", {"source": "not_a_real_source"})` → `1`. Same number, no way to
  tell them apart, and no way for `_pick` to express "reject this value outright".
- **Fix:** return a distinct sentinel for an absent key — `0`, or `None` — and have
  `rank()` treat `None` as an automatic loss unless both sides are `None` (in which case
  `_is_missing` at `dedup.py:168-171` already short-circuits anyway). That single change
  makes the S1 laundering produce trust `0` for GP's email even inside a union, because
  `osm` is in the union but contributed nothing to that field.

---

### S3 — No type guard on `record` or `field`: crashes, but only on inputs `_merge` cannot produce

- **Where:** `dedup.py:148`, `dedup.py:144`
- **Breaks:** the signature is a bare `field: str, record: dict` with no runtime check.

```
  CRASH  field=['email']  (unhashable)  -> TypeError: cannot use 'list' as a dict key (unhashable type: 'list')
  CRASH  field={'email'}  (unhashable)  -> TypeError: cannot use 'set' as a dict key (unhashable type: 'set')
  CRASH  record=None                    -> AttributeError: 'NoneType' object has no attribute 'get'
  CRASH  record=[]                      -> AttributeError: 'list' object has no attribute 'get'
  CRASH  record='osm'                   -> AttributeError: 'str' object has no attribute 'get'
```

  Contrast `_validity` at `dedup.py:128-139`, which defensively wraps `float()` in
  `try/except (TypeError, ValueError)`. `_trust_for` has no equivalent posture.
- **Not currently reachable:** `field` is always a `set(a) | set(b)` member
  (`dedup.py:192`), so it is always hashable; `record` is always a dict built at
  `dedup.py:210` / `dedup.py:218` via `dict(record)`. So this is **not** a live crash.
  It matters because `_trust_for` is module-private, has no docstring documenting the
  precondition, and is the obvious thing to reach for when adding a fourth source.
- **Fix:** one line at `dedup.py:150` —
  `if not isinstance(record, dict): return _DEFAULT_TRUST`.

---

### S3 — `_trust_for` cost is unbounded in `len(source)` and recomputed 2× per field per merge

- **Where:** `dedup.py:150` (calls `_sources_of`), `dedup.py:144` (`split` + set
  comprehension), `dedup.py:181` / `dedup.py:187` (two `rank()` calls per field)
- **Breaks:** every `rank()` re-parses `source` from scratch, so cost is
  `O(merges × fields × 2 × len(source))` with a large constant (full `split` plus a set
  build per call). Measured single-call cost, then full `dedup()` fan-in on N
  same-phone records (worst case — N-1 `_merge` calls):

```
single _trust_for call:
  len=3          ->   0.00 ms
  len=393        ->   0.04 ms
  len=4893       ->   0.25 ms
  len=58893      ->   2.96 ms
  len=688893     ->  36.78 ms

full dedup():
  len=3        n=20  ->    0.5 ms      (realistic source: negligible)
  len=28893    n=5   ->   45.7 ms
  len=28893    n=10  ->  139.4 ms
  len=28893    n=20  ->  413.6 ms
  len=338893   n=5   -> 1021.8 ms
  len=338893   n=10  -> 2323.5 ms
  len=338893   n=20  -> 4750.2 ms
```

  A 339 KB `source` cell across 20 records in one dedup group costs 4.75 s; extrapolating
  to a 5,000-record corpus that is ~20 minutes of pure `_trust_for` work against a
  300-minute CI budget (`.github/workflows/scrape.yml` `timeout-minutes: 300`).
- **Reachability, stated honestly:** the union written at `dedup.py:174` is capped at
  33 characters (`google_places|osm|wikidata`) as long as source names stay constant, so
  the pipeline cannot inflate this today. It becomes a real hazard the moment a source
  identifier is per-record (e.g. `osm:node/12345`), which is the obvious next
  implementation for the 27 other audit proposals that want per-source record IDs —
  then `source` grows to O(n) per merged row and the master CSV stores a kilobyte per
  cell. Bound it now rather than after that change.
- **Fix:** parse `source` once per record, not once per field comparison. Memoize on
  `id(record)` or, better, have `_merge` precompute `{field: trust}` once for `a` and
  `b` and pass it into `rank()` instead of recomputing.

---

## Edge-input classification

Every row below is an executed call to `_trust_for`, not a reading of the code.

### `field` (record fixed at `{"source": "osm"}`)

| Input | Class | Result |
|---|---|---|
| `None` | correctly handled | `1` — `dict.get(None)` is legal, falls to default |
| `""` (empty string) | correctly handled | `1` |
| `" "` (whitespace) | correctly handled | `1` |
| `" email"` / `"EMAIL"` / `"e-mail"` | correctly handled | `1` |
| `0` (zero) | correctly handled | `1` |
| `-1`, `-999` (negative) | correctly handled | `1` — floor of 1 applies |
| `3.5` (wrong type, float) | correctly handled | `1` |
| `True` (wrong type, bool) | correctly handled | `1` |
| `("email",)` (tuple) | correctly handled | `1` |
| `["email"]` / `{"email"}` (unhashable) | **crashes with a traceback** | `TypeError: cannot use 'list'/'set' as a dict key (unhashable type)` |
| `"e" * 10_000_000` (10 MB) | correctly handled | `1`, one dict lookup — `_trust_for` does not touch `field` beyond `.get` |

### `record` / `record["source"]` (field fixed at `"email"`)

| Input | Class | Result |
|---|---|---|
| `None` | **crashes with a traceback** | `AttributeError: 'NoneType' object has no attribute 'get'` |
| `[]`, `"osm"` (wrong type) | **crashes with a traceback** | `AttributeError: 'list'/'str' object has no attribute 'get'` |
| `{}` (no `source` key) | correctly handled | `1` |
| `source=None` / `""` / `"   "` | correctly handled | `1` |
| `source=0` / `0.0` / `False` / `[]` | correctly handled (falsy → `""`) | `1` — but see S2 |
| `source=True` | correctly handled | `1` |
| `source=["osm"]` / `{"osm": 1}` / `b"osm"` | correctly handled | `1` — `str()` repr defeats the lookup |
| `" osm"` / `"osm "` / `"osm\n"` | **silently wrong** | `1`, intended `3` |
| `"osm | wikidata"` / `"osm\twikidata"` / `"osm\nwikidata"` | **silently wrong** | `1`, intended `3` |
| `"OSM"` / `"OpenStreetMap"` / `"openstreetmap"` / `"osm_overpass"` | **silently wrong** | `1`, intended `3` |
| `"\ufeffosm"` (BOM) / `"osm\x00"` (NUL) | **silently wrong** | `1`, intended `3` |
| `"المصدر"` / `"osmفي"` (Arabic) / `"osmé"` (French) / `"osm🙂"` / `"🙂🙃"` (emoji) | correctly handled | `1` — a genuinely unknown source *should* score 1 |
| `"osm"` | correctly handled | `3` |
| `"osm\|wikidata"` / `"\|osm\|\|wikidata\|"` | correctly handled | `3` — empty segments filtered by `dedup.py:145` |
| `"osm\|osm\|osm\|osm"` | correctly handled | `3` — set dedupes |
| `"google_places\|osm"` | **silently wrong** | `3` for `email` — a Google value has been laundered to OSM's trust |
| `"google_places"` (GP email is hardcoded `None` upstream) | **silently wrong** | `1`, should be "no opinion" — see S2 |

### Contract violations

| Input | Class | Result |
|---|---|---|
| `source` naming a field the source cannot emit (`email` on `google_places`) | **silently wrong** | scored `1` as if it were merely untrusted, not impossible |
| `source` = a union including a source that never supplied the surviving value | **silently wrong** | trust inflated to the union max — S1 |
| `field` present in a record but absent from every `_SOURCE_TRUST` entry (`region`, `country`, `linkedin`, `website_live`, …) | **silently wrong** | `1` for all sources, so `str(value)` decides — S1 |

**No input produces a wrong-looking *correct-looking* result at the `_trust_for` boundary
other than the two S1s.** Everything hostile that reaches it either raises a clean
`AttributeError`/`TypeError` (unreachable today) or returns a defensible-looking `1`.
That is precisely the problem: `1` is returned for "unknown source", "no opinion", "typo",
"BOM", and "laundered to nothing" alike, and nothing upstream distinguishes them.

---

## The single most dangerous input

**A record whose `source` is already a union — `"osm|wikidata"` — merged into a group
that also contains a Google Places record carrying a junk value in a field Places cannot
emit.**

```
_trust_for("email", {"source": "google_places"})        -> 1
_trust_for("email", {"source": "osm"})                  -> 3
_trust_for("email", {"source": "osm|wikidata"})         -> 3
_trust_for("email", {"source": "google_places|osm"})    -> 3   # laundered
```

Why this one and not the others:

1. **It is the only hostile input that produces a confidently wrong answer rather than a
   refusal.** A crash is loud and the run dies; a `1` for an unknown source is humble and
   defensible. Trust `3` on a value that OSM never supplied is neither.
2. **It inverts the ranking, not just shifts it.** Because trust is tuple element 0
   (`dedup.py:181`) and validity is element 1 (`dedup.py:182`), laundered trust 3 beats
   a real value at trust 2 even when the real value is a well-formed address and the
   laundered one is `info@`. The entire point of `_validity` at `dedup.py:120-140` is
   overridden by a provenance bug.
3. **It is order-dependent, and the order is not the pipeline's choice.** Two of the six
   arrival orders of three realistic records give `info@`; the rest give the correct
   address. `main.py:162` uses `as_completed`, so the same scrapes on the same day can
   produce different `email` values in the same CSV column. A merge you cannot reproduce
   is a merge you cannot debug.
4. **It is persisted and self-reinforcing.** The laundered row is written at
   `main.py:209`, read at `main.py:150`, and re-merged at `main.py:179` on the next run —
   so once a bad email wins, it wins with inflated trust forever, and re-scoring it
   (`cli.py:247-276`) cannot detect it because nothing records which source supplied
   which field.
5. **It damages the product, not just the log.** `email` is a `sales_ready.csv`
   gate (`main.py:137-145`), an outreach target (`main.py:212`), and a `completeness_score`
   contributor (`enricher.py:103-104`). `info@` counts as a contact channel, so a broken
   address buys a sales-ready lead that a human then emails and bounces.

The 10 MB `field` string is a more dramatic input but a harmless one — it is a single dict
lookup. The unhashable `field` is a crash, which at least fails loudly.

---

## Not a bug, but worth knowing

- **`_trust_for` is order-independent.** It `max()`es over a `set`, so set iteration order
  never leaks into the result, and `_pick`'s final `str(value)` tiebreak (`dedup.py:184`)
  makes the whole merge reproducible given an arrival order. The *arrival order* is the
  only nondeterminism, and it comes from `main.py:162`, not from here.
- **`_validity`'s `-100` branch at `dedup.py:123` is unreachable from `_pick`.**
  `_pick` returns early for missing values at `dedup.py:168-171` before `rank()` is ever
  called. Harmless today, but it means the rank tuple's element 1 has a range that the
  code can never actually produce.
- **`{"source": "|osm||wikidata|"}` → `3`, not `1`.** `dedup.py:145`'s `if s` filters empty
  segments correctly, which is easy to get wrong and is worth keeping.
- **Falsy `source` values are handled but conflated.** `dedup.py:144`'s
  `str(record.get("source") or "")` means `0`, `0.0`, `False`, and `[]` are treated as
  "no source". Safe, but it is a second place where a real value would be indistinguishable
  from an absent one.
- **`_trust_for("source", ...)` is never called** — `_pick` short-circuits that field at
  `dedup.py:173-174`. So the union-rewrite and the trust lookup cannot recurse.
- **Only `google_places` emits `region`** (`google_places.py:275`); `osm.py:100` and
  `wikidata.py:110` hardcode `None`. That narrows the reachability of the second S1 —
  but it also means `region` is a derived field being merged as if observed, which is the
  design error underneath it.
- **`lat`/`lon` are the one high-trust field that behaves correctly.** Google wins at
  trust 3 (`dedup.py:99`) and a laundered union cannot raise Wikidata's `1` to beat it, so
  the Saudi lead keeps its Riyadh coordinates. Confirmed:
  `google_places` 33.89/35.50 beats `wikidata` 24.71/46.67 after a union — correct by
  accident of the trust table, not by design of the mechanism.

---

## Recommended order of work

1. **Give `_pick` per-field provenance** (S1, first finding). Without it, every other
   `_trust_for` fix is a patch on top of a function that is answering the wrong question.
   Add a `field -> source` map merged in lockstep with the record at `dedup.py:190-194`,
   and rank on the trust of the source that owns the surviving value. Highest value per
   line changed: it makes the S1 email case, the `region` scramble, and the `lat` fragility
   all disappear at once.
2. **Stop merging `country` and `region` by trust** (S1, second finding). Derive them
   after `dedup` from `main.py:50` `resolve_country` and `enricher.py:79` `infer_region`,
   and split the dedup group on a `country` mismatch. Add the 40-pair region table above as
   a test — it is 8 lines and it pins the exact regression.
3. **Normalise and validate `source` in `_sources_of`** (S2). `strip().lower()`, plus a
   `_KNOWN_SOURCES` set and a warning per unknown value. Cheap, and it converts the
   silent-degradation class into a visible one before anyone relies on it.
4. **Return a distinct "no opinion" sentinel** (S2) instead of `_DEFAULT_TRUST`. Makes
   `google_places.email` — which `google_places.py:290` proves impossible — lose to
   `None`, and gives `rank()` a way to reject a value outright.
5. **Add the two regression tests** that would have caught both S1s:
   - the six-permutation `W_BAD / OSM / W_GOOD` email case (assert one outcome, not two),
   - and a round-trip assertion through `load_master` that trust does *not* increase after
     a merge round-trips through `all_businesses.csv`.
   `tests/test_lead_signal.py:293-319` already tests `_merge` directly, so the harness
   exists — it just never tests the same input in different arrival orders.
6. **Add `isinstance` guards** (S3) and cap `len(source)` (S3) when the per-field
   provenance map from step 1 makes a single parse per record natural. Do the cap now if
   step 1 is deferred, since it is three lines and pre-empts the per-record-source-ID
   hazard.

---

## Reproducing this

All numbers in this report came from executing `dedup.py` unmodified under Python 3.14.8
(the project targets 3.12; every quoted message is valid on both — `TypeError: cannot use
'list' as a dict key (unhashable type: 'list')` is 3.12+ wording). Probes were run from a
scratch directory outside the repo; no file in `leadminer/` was modified, and no scraper,
network call, or package install was involved.