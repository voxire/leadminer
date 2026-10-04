# 300 — `_is_missing`: a predicate over a record *field* that never receives the field

## Verdict

`_is_missing` (`dedup.py:79`) returns a correct `bool` for every input I could construct,
and its *output* half of the contract is airtight — both call sites use it in a plain `if`
and neither can be broken by its shape. The defect is entirely on the input/decision half:
the function implements the presence rules for exactly one of the six type families declared
in `BusinessRecord` (`str | None`), is then called on **every** key of `set(a) | set(b)`
(`dedup.py:192`) including keys that are not in the TypedDict at all, and it has no way to
receive the one piece of context it needs — the field name. Its return value is then used at
`dedup.py:168-171` as a **one-directional veto** rather than as a filter, which means a
present-but-invalid value (an OSM `email=test@test`) is adopted with no validity check at all
and lands in `sales_ready.csv` as an actionable lead. That is the one finding here that is
live on every run; the rest are real but latent or cosmetic.

---

## Findings

### S1 — A blank peer value is treated as sufficient reason to adopt the other side *unvalidated*

- **Where:** `dedup.py:168-171` (the early returns in `_pick`), triggered by `dedup.py:89`
  (`return not value.strip()`), bypassing the guard at `dedup.py:122-140` (`_validity`).
- **Breaks:** `_pick` treats `_is_missing` as a veto in one direction only:

  ```python
  av, bv = a.get(field), b.get(field)
  if _is_missing(av):
      return bv            # dedup.py:169  <-- bv is adopted with ZERO checks
  if _is_missing(bv):
      return av            # dedup.py:171
  ```

  Once either side is classified blank, `_validity` (`dedup.py:120`) never runs for that
  field. So the *only* way a junk value can be rejected is if the other record happens to
  hold a non-blank value too. When the peer's cell is blank — which is the common case for
  the sparse half of an OSM+Google merge — the junk value wins outright and `_is_missing`
  has declared it "present information" by virtue of not being whitespace.

  Measured, on the real merge path:

  ```
  master = {... "email": None, "rating": None, "scraped_at": "2026-09-01T00:00:00+00:00"}
  fresh  = {... "email": "test@test", "rating": 99, "scraped_at": "2026-10-03T00:00:00+00:00"}
  # _EMAIL_OK.match("test@test") -> None       (rejected if both were present)
  # _validity("rating", 99)      -> 0          (rejected if both were present)
  dedup([master, fresh])[0]
    -> email='test@test'  rating=99
  # same pair with rating=4.0 in master instead:
  _pick("rating", {**master, "rating": 4.0}, fresh) -> 4.0     # correctly rejected 99
  ```

  Downstream, with the rest of the record filled in normally:

  | field | with junk `email="test@test"` | with `email=None` |
  |---|---|---|
  | `completeness_score` (`enricher.py:105`) | 4 | 2 |
  | `lead_score` (`enricher.py:288`, +20) | **60** | 48 |
  | `has_any_contact` (`main.py:139`) | `True` | phone only |
  | in `sales_ready.csv` (`main.py:202-205`) | **yes** | no |

  Worse, it is **self-sealing**. `enricher.py:252` only fills a blank contact:
  `if not r.get("email") and contacts["email"]:` — `"test@test"` is truthy, so the address
  genuinely scraped off the business website can never replace it, in this run or any future
  run, because the junk is written to the master CSV at `main.py:209` and re-read at
  `main.py:180`. One bad OSM tag poisons the lead permanently.

  `website` has the same shape. OSM tag `website="N/A"` is rewritten to `"https://N/A"` at
  `scrapers/osm.py:95-96`, is truthy at `main.py:199` so the row enters `with_websites.csv`,
  and is handed to `requests.get` at `enricher.py:231 -> 175`, where it is swallowed by the
  broad `except Exception` at `enricher.py:176` and reported as **unreachable** rather than
  live or dead. That inflates the `unreachable` bucket at `main.py:217`, which
  `pitch_recommender.py:34-36` deliberately refuses to treat as a rebuild signal.

- **Trigger:** One OSM node tagged `contact:email=test@test` (a real and common OSM
  placeholder; `_EMAIL_OK` at `dedup.py:116` requires a dot in the domain) for a business that
  also appears in the master with `email=` blank. No exotic input required — OSM tag hygiene
  is the weakest link in the chain and it feeds directly into the two highest-value output
  columns.
- **Fix:** Run the plausibility check before adopting, not after — in `_pick`, replace the
  bare early return with `if _is_missing(av) or _validity(field, av) <= 0: return bv`
  (and symmetrically), so a blank *or* implausible peer yields; better still, normalise junk
  to `None` at the scraper boundary so the predicate only ever sees clean data.

---

### S2 — `website_live=False` is "present", so a stale server-confirmed verdict is monotone against a fresher `UNKNOWN`

- **Where:** `dedup.py:90` (`return False` for `bool`), consumed at `dedup.py:168-169`;
  declared intent at `dedup.py:114` where `website_live` is listed in `_VOLATILE`.
- **Breaks:** `website_live` is the only tri-state field whose `False` is genuinely
  informative — `enricher.py:154-156` defines it as "the server answered 4xx/5xx", a fact
  about the world. So `_is_missing(False) is False` is the *right* answer. The bug is what
  `_pick` does with it: because `False` is "present", line 169 returns it immediately and
  the recency comparison in `rank` (`dedup.py:183`) — the only mechanism `_VOLATILE` feeds —
  is never reached for this field.

  ```
  stale  = {... "website_live": False, "scraped_at": "2026-09-01T00:00:00+00:00"}   # dead, last week
  fresh  = {... "website_live": None,  "scraped_at": "2026-10-03T00:00:00+00:00"}   # today, never reached
  _merge(stale, fresh)["website_live"] -> False
  ```

  All three scrapers hardcode `website_live=None` (`scrapers/osm.py:109`,
  `scrapers/wikidata.py:117`, `scrapers/google_places.py:292`), so the *only* way two present
  values ever meet is master-vs-master. The consequence is a record that asserts
  "server-confirmed dead" while carrying today's `scraped_at` — internally inconsistent, and
  monotonic: no amount of fresh evidence can clear it, because "we could not reach it" is
  indistinguishable from "nothing to say" to this predicate. A site that was repaired last
  month and now times out keeps its rebuild pitch until some other record supplies a
  non-blank value.

  **Honest scoping:** `main.main()` masks this today. `enricher.check_websites` re-fetches
  and unconditionally overwrites `website_live` at `enricher.py:250` for every record with a
  truthy `website` (`enricher.py:231`). So today the damage is limited to records without a
  website, whose master row carried a stale verdict (reachable via a hand-edited or legacy
  master). The day anyone adds a `dedup()`-only consumer — which is exactly what the
  clustering rewrite in `docs/audits/095-dedup-strategy-v2.md` proposes — it becomes live.
- **Trigger:** Two `all_businesses.csv` rows for the same phone where the older has
  `website_live=False` and the newer has `website_live=` (blank → `None` at `main.py:87-89`
  and `main.py:103`). `dedup()` returns the older `False` with the newer `scraped_at`.
- **Fix:** Treat "no observation" as superseding a prior observation for evidence fields:
  give `_VOLATILE` fields a dedicated branch in `_pick` that prefers whichever side actually
  carries a verdict from the newer `scraped_at`, instead of relying on the `_is_missing`
  early return.

---

### S3 — The signature omits the field name, so this function cannot be fixed in isolation

- **Where:** `dedup.py:79` (`def _is_missing(value) -> bool:`).
- **Breaks:** `_is_missing` is a predicate over a *typed record field*, but it receives only
  the value. That is enough for `str | None` and nothing else. For every other declared type
  the `return False` at `dedup.py:90` is unconditional:

  | TypedDict field | declared type | line | non-`None` value `_is_missing` calls "present" | is that right? |
  |---|---|---|---|---|
  | `name`…`linkedin`, `source`, `scraped_at`, `category`, `region`, `country`, `address` | `str \| None` / `str` | `base.py:6-15,18-19,26-27` | `""`, `"   "` | yes, handled |
  | `website_live` | `bool \| None` | `base.py:16` | `False` | yes (S2 is about the caller) |
  | `lat`, `lon` | `float \| None` | `base.py:11-12` | `0.0` | yes — (0,0) is a real coordinate |
  | `review_count` | `int \| None` | `base.py:22` | `0` | yes — zero reviews is a real pitch |
  | `rating` | `float \| None` | `base.py:21` | `0.0` | **no** — and it is scored maximally valid |
  | `lead_score`, `completeness_score` | `int` (**non-optional**) | `base.py:25,28` | `0` | **no** — a lie every scraper tells |

  Two things follow.

  1. **The two `int` fields have no legal way to say "unknown".** They are declared
     non-optional, so `None` is a type error, so every producer hardcodes `0`
     (`scrapers/osm.py:118,121`, `scrapers/wikidata.py:126,129`,
     `scrapers/google_places.py:301,304`). `_is_missing` is then *obliged* to believe `0`
     means "scored zero". It cannot be fixed by adding `if value == 0: return True` —
     that would be wrong for `lat=0.0`/`lon=0.0` and for `review_count=0`. The decision is
     field-dependent and the function is not field-aware. Note the sibling `_validity`
     (`dedup.py:120`) *does* take `field`, so the information is available one line away.
  2. **`rating = 0.0` is reachable and maximally trusted.** `load_master` casts the cell
     `"0"` to `0.0` (`main.py:90-95`), and `_validity("rating", 0.0)` returns **3** — the
     maximum — because the range check at `dedup.py:139` is `0 <= v <= 5`, while Google's
     documented range is 1–5. Nothing ever contradicts it (no producer emits a rating below
     1), so it is sticky:

     ```
     lead_score({... "rating": 4.4, "review_count": 12, "website_live": True,
                 "industry_priority": "medium", "phone": "+96170123456"}) -> 38
     lead_score(same, "rating": 0.0)                                       -> 48
     ```

     +10 from `enricher.py:312-314`, permanently, on a record whose real rating is 4.4.
     (`pitch_recommender.py:73` is shielded by its own `rating and …` truthiness guard, so
     the pitch text is unaffected — only the score is.)
- **Trigger:** A legacy or hand-edited master row where `rating` is blank-but-zero, or any
  `lead_score`/`completeness_score` cell that ever round-trips through a producer that
  prefers `None`.
- **Fix:** Make the two score fields `int | None` in `base.py:25,28`, have the scrapers emit
  `None`, and change the signature to `_is_missing(field: str, value: object) -> bool` so
  the per-field sentinel decision has the context it needs.

---

### S3 — `_pick`'s final tiebreak is `str(value)`, which orders the two `int` fields lexicographically

- **Where:** `dedup.py:184`.
- **Breaks:** `rank` is `(trust, validity, recency, str(value))`. For `lead_score` and
  `completeness_score` the first three are all ties by construction: neither field is in
  `_SOURCE_TRUST` (`dedup.py:97-110`) so `_trust_for` returns `_DEFAULT_TRUST = 1`
  (`dedup.py:111`); neither matches a branch in `_validity` so it returns the flat `1`
  (`dedup.py:140`); neither is in `_VOLATILE` so recency is `""` (`dedup.py:183`). The winner
  is therefore decided by string comparison of the decimal text.

  It currently produces the **right answer for the wrong reason**: every scraper emits `0`,
  and `"0"` is the lexicographic minimum of non-negative decimal strings, so the master's real
  score always wins.

  ```
  master = {"lead_score": 100, "completeness_score": 7, "scraped_at": "2026-09-01..."}
  fresh  = {"lead_score": 0,   "completeness_score": 0, "scraped_at": "2026-10-03..."}
  _merge(master, fresh)["lead_score"]        -> 100
  _merge(master, fresh)["completeness_score"] -> 7
  ```

  Introduce any other sentinel and it inverts silently: `"-5" < "0"` because `'-'` (0x2D)
  precedes `'0'` (0x30), so a negative or non-numeric sentinel would beat every real score.
  Worth stating plainly: this computation is also **entirely dead weight** in the current
  pipeline, because `enricher.py:349-350` and then `main.py:197` recompute both fields
  unconditionally for every record after `dedup()` returns. So a fragile ordering primitive
  is being paid for on every merge and then thrown away.
- **Trigger:** Any producer that emits `lead_score`/`completeness_score` as a float, a
  negative, or anything other than the literal `0`.
- **Fix:** Compare numerically for numeric fields (`float(value)` in the tiebreak), or drop
  the tiebreak for score fields and prefer the larger value.

---

### S3 — Whitespace: `_is_missing` says present, `_validity` strips to validate, `_pick` stores the value unstripped

- **Where:** `dedup.py:89`, `dedup.py:125`/`dedup.py:127` (`.strip()` inside `_validity`),
  `dedup.py:169`/`dedup.py:171` (returns the raw value).
- **Breaks:** Three components disagree about the same string.

  ```
  _is_missing(" https://a.example")                  -> False   # "present"
  _URL_OK.match(str(" https://a.example").strip())   -> matched # _validity -> 3, "VALID"
  stored value                                       -> ' https://a.example'   # unstripped
  ```

  `website` is in `_VOLATILE` (`dedup.py:114`), so a padded value wins purely on recency and
  is written to the master verbatim. `enricher.py:231` then hands it straight to
  `requests.get` (`enricher.py:175`) with no trim, so the request is rejected and swallowed by
  `except Exception` at `enricher.py:176` → `UNKNOWN`. Net effect: a live site is reported
  unreachable (`main.py:217`), gets no contact extraction, and earns no dead-site credit in
  `enricher.py:303`. Same asymmetry applies to `email`/`instagram`/`whatsapp`/`linkedin`,
  where `_EMAIL_OK` strips (`dedup.py:125`) but the stored value keeps its padding.
  `phone` is the one field immune, because `normalize_phone` strips non-digits
  (`dedup.py:44`).
- **Trigger:** A value with leading or trailing whitespace — OSM tag whitespace, or any CSV
  whose cell was not trimmed on write. Note `main.load_master` only converts `""` to `None`
  (`main.py:87-89`); it does not strip, so padded cells round-trip through the master forever.
- **Fix:** Have `_pick` return a normalised survivor (`return (bv or "").strip() or None if
  isinstance(bv, str) else bv`), or strip once in `load_master` at `main.py:87-89`.

---

### S3 — `_validity`'s `-100` sentinel is unreachable, and would not dominate even if it were

- **Where:** `dedup.py:122-123`.
- **Breaks:** `_is_missing(value) → return -100` reads like a veto that outranks everything.
  It can never execute: `_pick` returns early at `dedup.py:168-171` whenever either side is
  missing, so `_validity` is only ever called with two non-missing values. And if it *were*
  reachable it would not veto anything, because `rank` (`dedup.py:179-187`) compares
  `_trust_for` **first**; the tuple is compared element-wise, so a later `-100` can only
  matter when trust already ties. Confirmed: `_is_missing(True) is False`, so
  `_validity("email", True)` returns `0`, not `-100` — a `True` in a `str | None` field is
  silently scored as "present but implausible" rather than as a type error.
- **Trigger:** None. This is a readability trap, not a live defect.
- **Fix:** Delete the branch, or make `_validity` the single entry point that also handles
  absence (which is what S1's fix needs anyway).

---

## Answers to the four lens questions

### 1. Declared types vs actual types, in practice

| | Declared | Actual in practice | Holds? |
|---|---|---|---|
| `_is_missing` parameter | *no annotation* → `Any` (`dedup.py:79`) | every non-`str`, non-`None` value the record can hold: `bool`, `float`, `int`, and — from `csv.DictReader`'s `restkey` — **`list[str]`** | **No.** Nothing type-checks the input. `dedup(records: list[dict])` (`dedup.py:197`) is untyped at the record level, so `_pick`'s `a.get(field)` (`dedup.py:167`) is `Any` all the way in. |
| `_is_missing` return | `bool` | always a real `bool`: `value is None` → `bool`, `not value.strip()` → `bool`, `return False` → `bool` | **Yes.** Airtight. |
| Record shape | `BusinessRecord`, 23 keys (`base.py:5-28`) | `main.FIELDS`, 23 keys (`main.py:33-40`) | **Yes — verified identical, zero drift.** All three scrapers construct every key explicitly, and `load_master` produces exactly the declared types (`float` for `lat/lon/rating`, `int` for `review_count/completeness_score/lead_score`, tri-state `bool` for `website_live`, `str | None` elsewhere). This part of the contract is genuinely sound. |

### 2. Fields read/written that are not in the TypedDict

`_is_missing` reads no fields and writes nothing — it receives a scalar, so strictly the
answer is *none*. The key **set** it is invoked over is the problem, because `_merge` iterates
`set(a) | set(b)` (`dedup.py:192`), not `BusinessRecord`:

- **Extra CSV columns.** `main.load_master` uses a bare `csv.DictReader` with no field-name
  validation (`main.py:86`), so every column in `data/all_businesses.csv` becomes a key. None
  of them is in the TypedDict. Verified:

  ```
  header: name,country,phone,source,scraped_at
  row 2:  A,LB,70123456,osm,2026-01-01T00:00:00+00:00,junk,extra
  -> load_master row 2 keys: [..., None]          # DictReader restkey
  -> _is_missing(['junk','extra']) is False        # a LIST is "present"
  -> dedup() output keys: [None, 'country', ...]  # merged as if it were a field
  -> then dropped without a word by write_csv(extrasaction="ignore")  # main.py:125
  ```

  So an unknown column is faithfully merged on every run and then silently erased on write
  (same root cause as `docs/audits/028-csv-roundtrip-fidelity.md`).
- **Direction of drift is clean.** I checked the other direction: nothing in `main.FIELDS` is
  missing from `BusinessRecord` and nothing in `BusinessRecord` is missing from `FIELDS`.
  `enricher.py:340-348` `setdefault`s only declared keys. No orphan field anywhere in the
  pipeline.

  Worth recording for whoever fixes S9 in `020-type-contract-import-cycle.md`: the *current*
  per-field `_pick` is **immune** to the "extra keys steer merge priority" abuse, because
  `_trust_for`/`_validity` are computed per field (`dedup.py:148-152`, `dedup.py:120`). The
  old global `_field_count` was the vulnerable version. Extra columns today are carried and
  dropped, but they cannot influence any other field's winner.

### 3. Return value used in a way that breaks on an unexpected shape

**None of the call sites can break on the return shape.** There are exactly three
(`dedup.py:122`, `dedup.py:168`, `dedup.py:170`); all are `if _is_missing(x):` against a
`bool`-typed function. `_validity` returns `int` and both call sites consume it
arithmetically.

The breakage is one level up: the return value is used as a **sufficient** condition for
adoption rather than as a **necessary** one (S1). A predicate named "is this field missing"
gets promoted into "so therefore the other field is good enough to ship", which is a
different and much stronger claim than the function makes. That promotion is the entire S1.

### 4. Can a caller violate the contract silently?

Yes, and it already does. `BusinessRecord` is enforced at exactly **three** call sites — the
`BusinessRecord(...)` constructors in `scrapers/osm.py:98`, `scrapers/wikidata.py:106` and
`scrapers/google_places.py:281`. Everywhere else it is decorative:

- `BaseScraper.scrape() -> Iterator[BusinessRecord]` (`base.py:33`) is the *only* declared
  producer edge, and `main.py:158` immediately launders it: `list(scraper.scrape())` assigned
  to an untyped `batch`, then `raw.extend(batch)` into an untyped `list[dict]`.
- `load_master` returns `list[dict]` (`main.py:77`) with no `validate_record` — it happens to
  emit the right types, but by CSV convention, not by check.
- `dedup(records: list[dict]) -> list[dict]` (`dedup.py:197`) takes plain `dict`, so the one
  function that *makes* records — `_merge` returns `dict` (`dedup.py:190`) — cannot state its
  output type either.

So the TypedDict constrains the three scrapers and nothing downstream. That is why the S1
junk path is silent: there is no gate between `scrapers/` and the CSV that could object.

---

## Not a bug, but worth knowing

- **`main.py:172` whitelist the fresh scrape but not the master.** `raw_filtered` is built
  from `raw` only, then `combined = raw_filtered + master` (`main.py:179`). Every historical
  row enters `dedup()` unfiltered, and its `category` — which is what the whitelist keys on —
  is re-derived into `industry_priority` at `main.py:190` regardless. This widens S1's blast
  radius: junk in the master is never even offered a chance to be filtered.
- **`max(av, bv)` at `dedup.py:177` bypasses both type-hardening helpers.** `_sources_of`
  wraps in `str(...)` (`dedup.py:144`) and `_recency` wraps in `str(...)` (`dedup.py:156`) —
  and line 177 calls neither, comparing the raw values. `_is_missing` guarantees both are
  non-blank `str` *today* (all three scrapers use `utc_now_iso()`, `httpclient.py:220`), so
  this cannot currently raise. But the two helpers exist precisely to make that guarantee
  unnecessary, and the one place that needs them does not use them. A master row carrying a
  legacy naive `datetime.utcnow().isoformat()` timestamp also sorts before an equal-instant
  offset-aware one, which is the exact hazard `httpclient.py:222-225` documents.
- **The `-100` sentinel would not work as written even if it ran.** Covered in the last S3 —
  `rank` compares `_trust_for` before `_validity`, so validity can never veto a trust win.
  This is a design decision that is easy to misread as the opposite.
- **Two CSV round-trip hazards `load_master` handles but `_is_missing` inherits.** A short row
  yields `scraped_at=None` (verified — `csv.DictReader` fills missing trailing columns with
  `restval`), and a blank `website_live` yields `None` via `main.py:103`. Both are correctly
  "missing" to `_is_missing`. This part is fine and worth keeping.
- **The junk-vs-blank problem is not specific to `_is_missing`.** `completeness_score`
  (`enricher.py:103-116`), `lead_score` (`enricher.py:288`) and `has_any_contact`
  (`main.py:137-145`) all use raw truthiness, so *any* junk string that survives dedup is
  scored as a real contact channel. Fixing S1 at `_pick` closes the entry point; hardening the
  three consumers closes the class of bug.

---

## Recommended order of work

1. **S1** — in `_pick`, make `_validity` run before a value is adopted over a blank peer
   (`dedup.py:168-171`). One line per direction, closes the only live wrong-output path.
2. **S3 (signature)** — change `_is_missing(field, value)`, make `lead_score` and
   `completeness_score` `int | None` in `scrapers/base.py:25,28`, and have the six scraper
   construction sites emit `None`. Doing this before #4 makes #4 unnecessary.
3. **S2** — give `_VOLATILE` fields a recency-first branch in `_pick`, so a fresher
   `website_live=None` supersedes a stale `False`. Cheap now; mandatory the moment a
   `dedup()`-only consumer exists (see `docs/audits/095-dedup-strategy-v2.md`).
4. **S3 (whitespace)** — normalise the survivor in `_pick`, or strip in `load_master`.
5. **S3 (tiebreak)** — numeric comparison for numeric fields; while there, delete the dead
   `-100` branch at `dedup.py:122-123`.
6. **Enforcement (not a finding, but the reason S1 was silent)** — annotate
   `dedup(records: list[BusinessRecord]) -> list[BusinessRecord]` and give `dedup()` a real
   validation gate so the TypedDict stops being decorative outside `scrapers/`. `main.py:172`
   should whitelist the master rows too.