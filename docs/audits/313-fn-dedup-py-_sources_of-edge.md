# 313 — Edge / boundary audit of `dedup._sources_of`

**Target:** `_sources_of` — `dedup.py:143-145`
**Lens:** edge (boundary, hostile, and contract-violating inputs)

```python
143: def _sources_of(record: dict) -> set[str]:
144:     raw = str(record.get("source") or "")
145:     return {s for s in raw.split("|") if s}
```

## Verdict

`_sources_of` is **crash-proof but not correctness-proof**. It never raises on any
`source` *value* — `None`, `""`, `0`, `False`, 1 MB strings, pipe-riddled strings,
emoji, Arabic, French and embedded NULs all return a set. The single crash path
(non-dict `record`) is **masked** by `dedup.py:202`, which dereferences
`record.get("phone")` first and dies there instead.

The real defect is that it is a **lenient parser with no canonicalisation step and
no validation**. Any `source` string that is not byte-identical to a key in
`_SOURCE_TRUST` is silently downgraded to `_DEFAULT_TRUST` (1, `dedup.py:111`) —
the *lowest possible* trust — for every field. That inverts the exact survivorship
policy the module documents at `dedup.py:93-96`, with no exception, no log line and
no counter. Verified below: one leading space in `"source"` makes Google's
trust-2 website beat OSM's trust-3 website.

**Most dangerous input: `" osm "`** (a single leading space). Full argument in
§"Most dangerous input".

---

## Contract

Declared: `source: str` (`scrapers/base.py:26`), optionally pipe-delimited to record
multi-source provenance. Produced only as a string literal by each scraper
(`scrapers/osm.py:119`, `scrapers/wikidata.py:127`,
`scrapers/google_places.py:302`) and as a `"|"`-join by `_pick`
(`dedup.py:174`), then round-tripped through `all_businesses.csv`
(`main.py:150` read, `main.py:209` write).

Two callers, both correctness-critical:

| Caller | Line | Consequence of a wrong return |
|---|---|---|
| `_trust_for` | `dedup.py:150-151` | Decides **which value wins** on every field conflict during merge |
| `_pick` (source branch) | `dedup.py:174` | Determines the `source` column written to every output CSV |

Because `_trust_for` gates the merge, `_sources_of` is not a metadata helper — it
is on the critical path of the product's data quality.

---

## Boundary matrix (all rows executed against the real module)

### Correctly handled — no crash, correct result

| Input `record` | `_sources_of` | Verdict |
|---|---|---|
| `{}` (key absent) | `set()` | correct |
| `{"source": None}` | `set()` | correct |
| `{"source": ""}` | `set()` | correct |
| `{"source": 0}` | `set()` | correct (`0` is falsy → `""`) |
| `{"source": False}` | `set()` | correct |
| `{"source": "\|\|"}` / `{"source": "\|"}` | `set()` | correct (empty segments filtered) |
| `{"source": "\|osm\|"}`, `"\|osm"`, `"osm\|"` | `{"osm"}` | correct |
| `{"source": "osm\|\|google_places"}` | `{"osm","google_places"}` | correct |
| `{"source": "osm\|osm\|osm"}` | `{"osm"}` | correct (set dedups) |
| `"🇱🇫"`, `"مصدر"`, `"Wikidonnées"`, `"osm\x00wikidata"`, `"osm\nwikidata"` | 1-element set | no crash |
| `{"source": "x"*1_000_000}` | 1-element set | no crash |
| `{"source": "\|"*500_000}` | `set()` | no crash |

Delimiter hygiene is genuinely good: leading/trailing/duplicated/repeated pipes,
absent keys, `None`, `""`, and falsy scalars are all correct. `str()` at
`dedup.py:144` means no `source` *value* can raise.

### Crashes with a traceback

| Input | Result |
|---|---|
| `_sources_of(None)` | `AttributeError: 'NoneType' object has no attribute 'get'` |
| `_sources_of([])` | `AttributeError: 'list' object has no attribute 'get'` |
| `_sources_of("osm")` | `AttributeError: 'str' object has no attribute 'get'` |
| `_sources_of(42)` / `((... ,))` / `object()` | `AttributeError` |

**Reachability: masked.** `dedup()` calls `record.get("phone")` at `dedup.py:202`
before anything reaches `_sources_of`, so `dedup([{...}, None])` raises the same
`AttributeError` from `dedup.py:202` instead. `_merge`/`_pick` only ever receive
dicts (`dict(record)` at `dedup.py:210` and `dedup.py:218`, or a previous `_merge`
result at `dedup.py:208`/`dedup.py:216`). There is **no live path** from the
public entry point to this crash today. Real defect, zero current blast radius.

### Silently wrong

| Input | Returns | Why it is wrong |
|---|---|---|
| `{"source": " osm "}` | `{" osm "}` | No `.strip()` → trust lookup misses → trust 1 |
| `{"source": "OSM"}` | `{"OSM"}` | No case-fold → trust 1 |
| `{"source": ["osm"]}` | `{"['osm']"}` | `str()` mangles the repr → trust 1 |
| `{"source": ("osm","wikidata")}` | `{'("osm", "wikidata")'}` | same → trust 1 |
| `{"source": {"source":"osm"}}` | `{"{'source': 'osm'}"}` | same → trust 1 |
| `{"source": b"osm"}` | `{"b'osm'"}` | same → trust 1 |
| `{"source": "osm, google_places"}` | 1 element | Comma is not a delimiter → trust 1 |
| `{"source": " "}` | `{" "}` | `_is_missing(" ")` is `True`, `_sources_of` says **present** |

---

## Findings

### S1 — A `source` string that misses `_SOURCE_TRUST` silently costs the record *all* of its trust, inverting survivorship

- **Where:** `dedup.py:144-145` (parser), consequence at `dedup.py:151`
  (`_SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST)`) and `dedup.py:111`
  (`_DEFAULT_TRUST = 1`).
- **Breaks:** `_sources_of` returns a token that is used *verbatim* as a
  `_SOURCE_TRUST` key. There is no `.strip()`, no `.lower()`, no membership check
  against `_SOURCE_TRUST`, and no unknown-source counter. An off-key token falls
  through to `_DEFAULT_TRUST = 1`, which is **lower than every entry in the trust
  table** (the minimum real value is `2`). So a malformed `source` does not merely
  lose its own trust bonus — it makes the record the **least trusted thing in the
  merge**, including less trusted than a record with no `source` at all, which
  `_sources_of` maps to `set()` and which therefore also yields 1.
- **Trigger (measured):**

  ```
  clean  = {"source": "osm",         "website": "http://acme-lb.example"}   # trust 3
  padded = {"source": " osm ",       "website": "http://acme-lb.example"}   # trust 1
  google = {"source": "google_places","website": "https://gmaps.example"}  # trust 2

  _merge(clean, google)["website"] -> 'http://acme-lb.example'   # policy-correct: OSM 3 > google 2
  _merge(padded, google)["website"] -> 'https://gmaps.example'   # INVERTED: google 2 > " osm " 1
  ```

  Full demotion table for `" osm "` vs `"osm"`:
  `email 3→1, website 3→1, facebook 3→1, instagram 3→1, whatsapp 3→1,
  phone 2→1, name 2→1, address 2→1, category 2→1`.
  That is **every field OSM is trusted for**, wiped by one space.
- **Why it is silent:** the fall-through is `.get(field, _DEFAULT_TRUST)` — a
  default, not an error. Nothing counts unknown sources, `dedup()` returns
  normally, and the only observable is `lead_score` and the CSV contents.
- **Reachability — honest assessment:** the three scrapers emit literals
  (`osm.py:119`, `wikidata.py:127`, `google_places.py:302`), so they cannot emit
  an off-key value. The live vector is the **cumulative master CSV**: `main.py:150`
  loads `data/all_businesses.csv` on every run and `main.py:209` writes it back,
  and `load_master` (`main.py:87-89`) nulls only `""` — **it strips nothing**.
  The file is explicitly written `utf-8-sig` "so Excel and Google Sheets detect
  UTF-8" (`main.py:118-119`), i.e. it is designed to be opened and re-saved by a
  human. One padded or case-mangled cell in that file demotes that record forever
  with no error. It is also one `str` literal away from firing in any future
  scraper. It is S1 by class ("wrong results, silent") and latent by reachability
  — both facts matter and neither is discoverable from the output.
- **Fix:** canonicalise and validate, and make unknown sources loud:

  ```python
  def _sources_of(record: dict) -> set[str]:
      raw = record.get("source")
      if not isinstance(raw, str) or not raw.strip():
          return set()
      return {s.strip().lower() for s in raw.split("|") if s.strip()} & _KNOWN
  ```
  plus a `collections.Counter` of unknown tokens reported once per run.

### S2 — `_sources_of` and `enricher.py:316` disagree on what "multiple sources" means, corrupting `lead_score`

- **Where:** `dedup.py:143-145` vs `enricher.py:316`
  (`if "|" in str(record.get("source") or ""): score += 5`).
- **Breaks:** two independent parsers of the same field with different semantics.
  `_sources_of` counts *non-empty* `|`-delimited segments; `enricher` does a raw
  substring test. Measured divergences:

  | `source` | `_sources_of` sees | `enricher` grants +5 |
  |---|---|---|
  | `"osm"` | 1 source | no |
  | `"osm\|"` | 1 source | **yes** |
  | `"\|osm"` | 1 source | **yes** |
  | `"osm\|\|"` | 1 source | **yes** |
  | `"\|"` | **0 sources** | **yes** |
  | `"google_places\|osm"` | 2 sources | yes |

  A record with `"source": "osm|"` — a single source with a dangling delimiter —
  is scored as multi-source-confirmed. `lead_score` is a product column
  (`main.py:37`) and feeds the `sales_ready.csv` decision, so records are
  inflated on a signal that means "we saw this in two places".
- **Trigger:** `{"source": "osm|"}` → `_sources_of` length 1, `"|" in raw` → `True`.
- **Fix:** export one parser and use it in both places:
  `if len(_sources_of(record)) >= 2: score += 5`.

### S2 — Three divergent parsers of `source`, only one of which feeds trust

- **Where:** `dedup.py:144-145`; `enricher.py:316`; `cli.py:119-121`.
- **Breaks:** `cli.cmd_stats` — described in its own docstring as "the first
  thing to check" (`cli.py:81`) — counts *raw whole cells*:
  `collections.Counter(r.get("source") for r in rows)`. So `"google_places|osm"`
  lands in its own bucket instead of counting toward either source, and
  `" osm "` is a separate bucket from `"osm"`. The operator debugging a
  survivorship complaint is shown a breakdown that does not correspond to any
  other part of the system. `r.get("source")` is also un-`or`-ed, so a `None`
  cell prints as a literal `None` bucket (`cli.py:121`).
- **Trigger:** `cli.py stats` on a master containing `{"source": "google_places|osm"}`
  reports a source named `google_places|osm` with a count, and two separate
  `"osm"`/`" osm "` rows.
- **Fix:** `for k, v in Counter(s for r in rows for s in _sources_of(r)).most_common(8)`.

### S3 — Non-dict `record` raises `AttributeError` (masked, but undefended)

- **Where:** `dedup.py:144`.
- **Breaks:** `_sources_of(None)` and every other non-mapping raise
  `AttributeError: ... has no attribute 'get'`. Masked today by `dedup.py:202`,
  so a malformed record dies one frame earlier than it should and the traceback
  points at `dedup` rather than at the record. The annotation `record: dict` is
  not enforced at runtime, and `_sources_of` is called from two places
  (`dedup.py:150`, `dedup.py:174`) that would propagate rather than absorb.
- **Trigger:** `_sources_of("osm")`.
- **Fix:** `if not isinstance(record, dict): return set()`.

### S3 — Non-`str` `source` is `str()`-mangled instead of rejected

- **Where:** `dedup.py:144`.
- **Breaks:** `str()` guarantees no crash but guarantees garbage for any
  non-`str`. `["osm"]` → `"['osm']"`, `b"osm"` → `"b'osm'"`,
  `{"source": "osm"}` → `"{'source': 'osm'}"`, `5` → `"5"`, `float("nan")` →
  `"nan"`. Each becomes a phantom source that then **persists into the output
  CSV** via `dedup.py:174` and is re-ingested on the next run by `main.py:150`.
  `BusinessRecord.source` is declared `str` (`scrapers/base.py:26`) but a
  `TypedDict` is not enforced, so this is a contract violation the code silently
  launders rather than rejecting.
- **Trigger:** `_sources_of({"source": ["osm"]})` → `{"['osm']"}`;
  `_merge({"source": ["osm"]}, {"source": "google_places"})["source"]` →
  `"['osm']|google_places"`, which is then written to `all_businesses.csv`.
- **Fix:** `if not isinstance(raw, str): return set()` (subsumed by the S1 fix).

### S3 — `_is_missing` and `_sources_of` disagree about what "empty" means, 5 lines apart

- **Where:** `dedup.py:88-89` (`.strip()` → missing) vs `dedup.py:145`
  (`if s` → truthy on whitespace).
- **Breaks:** two notions of emptiness in one module:

  | value | `_is_missing` | `_sources_of` |
  |---|---|---|
  | `""` | `True` | `set()` — agrees |
  | `" "` | `True` | `{" "}` — **disagrees** |
  | `"\t"` | `True` | `{"\t"}` — **disagrees** |
  | `" osm "` | `False` | `{" osm "}` — agrees it is present, wrong about *which* |
  | `"\|"` | `False` | `set()` — **disagrees** |

  A whitespace-only `source` is "missing" to `_pick` (`dedup.py:168-171`, so it
  is discarded correctly at the merge boundary) yet "present" to `_trust_for`
  (`dedup.py:150`), which reads the record's own source. So `{"source": " ",
  "email": "a@b.com"}` loses its email to any record with a real source — the
  record is simultaneously treated as having no provenance and as having
  unrecognised provenance.
- **Trigger:** `_is_missing(" ")` → `True`; `len(_sources_of({"source": " "}))` → `1`.
- **Fix:** the S1 canonicaliser; add `s.strip()` to the filter at `dedup.py:145`.

### S3 — `_sources_of` re-splits the full string on every ranked field, with no memo

- **Where:** `dedup.py:181` calls `_trust_for`, which calls `_sources_of`
  (`dedup.py:150`); `dedup.py:174` calls it again for the source field itself.
- **Breaks:** measured **24 `_sources_of` invocations for a single `_merge` with
  12 conflicting fields** — i.e. ~1.8× the field count, not 1. On a hostile
  200 KB pipe-dense `source` (100 000 `"osm|"` repeats) one merge took **36.5 ms**
 ; a single call on a 1 MB `"||"` string took **34.5 ms** (`split` materialises
  500 001 intermediate strings before the filter discards them). Cost scales with
  `len(source) × conflicting_fields × merge_depth`; a deep merge chain (a popular
  phone number accumulating dozens of contributing records) multiplies it.
  Bounded by the set dedup — `" osm |osm"` stays stable across 20 further merges —
  so this is a cost issue, not unbounded growth.
- **Trigger:** `_merge(a, b)` with `a["source"] = b["source"] = "osm|"*100_000`.
- **Fix:** `functools.lru_cache` on a `_source_key(record_id)` helper, or compute
  `_sources_of` once per `_merge` and pass the set down.

---

## Not a bug, but worth knowing

- **The return type is a `set`, but both call sites are order-independent.**
  `_trust_for` takes a `max` over the set (`dedup.py:150-151`); `_pick` applies
  `sorted()` before joining (`dedup.py:174`). `PYTHONHASHSEED` randomisation
  therefore cannot make output non-deterministic. This is correct and worth
  keeping under test — converting either call site to a bare `"|".join(set)` would
  introduce run-to-run flakiness in a product column.
- **Round-trip stability holds.** `_pick`'s own output re-parses to itself:
  `"google_places|osm"` → `{"google_places","osm"}` → `"google_places|osm"`.
  Pinned by `tests/test_lead_signal.py:316-319`.
- **`_sources_of` is not covered directly.** `grep -rn "_sources_of" tests/`
  returns nothing; the only coverage is the `_merge` round-trip above. There is no
  test for `None`, for whitespace, or for a non-`str`.
- **`str()` at `dedup.py:144` is load-bearing for crash-safety**, not just for
  coercion — it is the only reason a hostile `source` value cannot raise. It
  should not be removed without adding the `isinstance` guard above.
- **`enricher.py:316` uses `str(record.get("source") or "")`, duplicating
  `dedup.py:144` verbatim.** The two expressions were clearly written to be the
  same line and have since diverged in what they mean. Worth a comment
  cross-reference at minimum.

---

## Most dangerous input

**`{"source": " osm "}` — one leading space.**

It is the only input in the whole matrix that satisfies *every* condition needed
to do maximum damage:

1. **It is contract-legal.** `source: str` (`base.py:26`). It passes every
   `isinstance` check, every `if not value` check, and `_is_missing(" osm ")`
   is `False` — the record looks complete to every other part of the module.
2. **It never raises.** The 34 other hostile values either return `set()`
   (harmless) or raise an `AttributeError` that would be traced and fixed.
   This one returns a plausible-looking `{" osm "}` and is never noticed.
3. **It costs the record 100% of its trust**, not part of it — every one of
   `_SOURCE_TRUST`'s `3`s and `2`s (`dedup.py:97-110`) falls to
   `_DEFAULT_TRUST = 1`, below the minimum real value. Verified: OSM's trust-3
   website loses to Google's trust-2 website, the exact inverse of the policy
   documented at `dedup.py:93-96`.
4. **It survives every downstream stage.** `str()` keeps it, `split("|")` keeps
   it, `sorted()` re-emits it (`dedup.py:174`), `csv.DictWriter` writes it
   unquoted (`|` is not the delimiter), and `load_master` reads it back without
   stripping (`main.py:87-89`). One bad cell in the cumulative master is permanent.
5. **The only diagnostic is a line an operator is unlikely to read.** `cli.py:119-121`
   would show `" osm "` as its own bucket in `leadminer stats`, indistinguishable
   at a glance from a legitimately different source.

Every other hostile input is either caught by the falsy check
(`None`/`""`/`0`/`False`/`"|"`), neutralised by the `if s` filter
(duplicate/leading/trailing pipes), or loud (non-dict → `AttributeError`;
non-`str` → a `repr` containing `[` or `'`). `" osm "` is the one that is quiet,
total, and permanent.

---

## Recommended order of work

1. **S1** — canonicalise in `_sources_of`: `isinstance` guard, `.strip()`,
   `.lower()`, intersect against the known source set, and count unknown tokens
   per run so a corrupted `source` is reported instead of silently absorbed.
2. **S2** — make `enricher.py:316` and `cli.py:119-121` call the same parser.
   One canonical definition of "how many sources is this record".
3. **S3** — add a `_sources_of` unit-test table (the matrix above is directly
   reusable as assertions) and reconcile `_is_missing` with the segment filter.
4. **S3** — memoize per-merge; hoisting `_sources_of` out of `rank()` removes
   the ~1.8× re-split for free.
