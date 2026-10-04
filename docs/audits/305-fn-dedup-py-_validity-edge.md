# 305 — `_validity` edge-case audit (`dedup.py:120`)

## Verdict

`_validity` never raises. Across 1,400 field × value combinations (every JSON-decodable
type, plus `bytes`, `Decimal`, `Fraction`, `complex`, `datetime`, `object()`, 1 MB
strings, emoji, ZWSP/BOM/LRM, Arabic-Indic and fullwidth digits) the only exceptions
came from two deliberately hostile classes that override `__str__`/`__float__` to raise
— unreachable from JSON. **The edge lens finds no crash in this function.**

What it finds instead is that `_validity` silently awards its **maximum score of 3 to
values that are not what the field claims to hold**, and the biggest offenders are
non-finite floats and an **unanchored `_URL_OK`**. `float()` is used as a stand-in for
"is this a coordinate," so `lat = nan`, `lat = 999.0`, `lat = "١٢٣"`, `lat = True` and
`lat = "1_0"` all score 3; and `_URL_OK` (`dedup.py:117`) has no trailing anchor, so
`"https://foo.com http://bar.com"` scores 3 and is not a URL. The single most dangerous
input is a multi-URL OSM `contact:website` tag — see the last section.

A structural finding outranks all of them: **`_validity`'s score is the *second* term of
`_pick`'s rank tuple (`dedup.py:179-187`), behind `_trust_for`.** So a high-trust source
can install garbage with `_validity = 0` and be believed. `_validity` is doing its job and
being ignored.

---

## Contract, as documented vs as implemented

```
docstring (dedup.py:121):  "0-3 bonus for whether a value is merely present or actually plausible."
actual return set:          {-100, 0, 1, 3}
```

- `2` is unreachable; `-100` is unreachable from the only call site (proven below).
- The function is only ever called from `dedup.py:182`, inside `rank()`, which is defined
  at `dedup.py:179` — *after* `_pick`'s two early returns at `dedup.py:168` and
  `dedup.py:170`. Both early returns fire on `_is_missing`, the same predicate
  `_validity` guards with at `dedup.py:122`. **The `-100` branch is therefore dead code.**

  Proof by instrumentation (wrapping `_validity` and recording the `_is_missing` result of
  every call):

  ```
  _merge({'email': None}, {'email': 'a@b.co'})     _validity called 0x; missing=True seen: False
  _merge({'email': ''},   {'email': '   '})        _validity called 0x; missing=True seen: False
  _merge({'lat': None},   {'lat': 33.8})           _validity called 0x; missing=True seen: False
  _merge({'website': None, 'rating': ''}, {...})   _validity called 0x; missing=True seen: False
  full dedup() over 3 hostile records                _validity called 8x; missing=True seen: False
  ```

  Consequence: the sentinel's magnitude (`-100`, vs a `0..3` bonus range) advertises an
  "always lose to missing" contract that nothing consumes. It is documentation theatre,
  and it is the kind of thing a later edit to `_pick` will silently rely on.

---

## Findings

### S1 — `_pick` compares trust *before* validity, so high-trust garbage beats a valid low-trust value

- **Where:** `dedup.py:179-187` (`rank` tuple), `dedup.py:97-110` (`_SOURCE_TRUST`)
- **Breaks:** `rank()` returns `(trust, validity, recency, str(value))`. Tuple comparison
  is lexicographic, so `trust` decides before `validity` is ever consulted. Any source with
  a higher trust number can install a value that `_validity` scored **0**, and `_validity`
  will not object.

  The email column is the sharpest case, because `_SOURCE_TRUST` rates OSM highest for it
  (`dedup.py:102`, `"email": 3`) while Wikidata is 2 (`dedup.py:107`) and Google is absent
  → `_DEFAULT_TRUST` 1 (`dedup.py:111`).

- **Trigger** (all measured, `scraped_at` set to make the case non-degenerate):

  ```
  OSM      email = "info@"            trust 3, _validity = 0
  Wikidata email = "sales@shop.com"   trust 2, _validity = 3
  _merge(...)[email] == 'info@'       order-stable = True

  OSM      email = "contact"          trust 3, _validity = 0
  Wikidata email = "sales@shop.com"   trust 2, _validity = 3
  _merge(...)[email] == 'contact'     order-stable = True

  OSM      website = "http://localhost:8000"  trust 3, _validity = 0
  Google   website = "https://real-shop.example" trust 2, _validity = 3
  _merge(...)[website] == 'http://localhost:8000'
  ```

  This is not hypothetical input. `scrapers/osm.py:90` reads `tags.get("email")` with no
  format check, and OSM `contact:email` / `email` tags in the wild are frequently
  `"info@"`, `"contact"`, or a domain with no local part. `scrapers/osm.py:91` reads
  `contact:website` / `website` / `url`, which routinely hold intranet addresses.
  Because `main.py:180` dedups `raw_filtered + master` and `main.py:209` writes the merged
  result back into the **cumulative** master, a bad value that wins once is permanent.

- **Fix:** make validity a hard gate rather than a tiebreak — return the higher-validity
  value first and use trust only to break ties at equal validity, or score
  `(validity, trust, ...)` and add an explicit "validity 0 must never displace validity ≥ 3"
  rule.

### S1 — `float()` is used as a proxy for "plausible coordinate"; non-finite, out-of-range, Unicode-digit, underscore and bool values all score 3

- **Where:** `dedup.py:128-133`
- **Breaks:** the check is `try: float(value) except (TypeError, ValueError): return 0`
  with **no range check and no finiteness check** — unlike the sibling `rating` branch at
  `dedup.py:134-139`, which does apply a range. `float()` is far more permissive than
  "a latitude."

  Measured, `_validity("lat", v)`:

  | Input | validity | Why |
  |---|---|---|
  | `float("nan")`, `"nan"`, `"-nan"` | **3** | `float()` accepts NaN; the lat branch has no range test, so unlike `rating` nothing rejects it |
  | `float("inf")`, `"inf"`, `"-Infinity"`, `"1e400"`, `"9"*400` | **3** | overflow to `inf` is still a successful `float()` |
  | `999.0`, `-33.89` | **3** | no `[-90, 90]` / `[-180, 180]` bound |
  | `0.0`, `-0.0` | **3** | Null Island scores as a perfect coordinate |
  | `True` / `False` | **3** | `float(True) == 1.0` |
  | `"1_0"`, `"1_0.5"` | **3** | PEP-515 numeric-literal underscores |
  | `" 33.8951 "` | **3** | `float()` strips surrounding ASCII whitespace |
  | `"٣٣.٨٩"` (Arabic-Indic U+0663…), `"３３.８９"` (fullwidth) | **3** | `float()` accepts any Unicode `Nd` digit |
  | `b"33.89"` | **3** | `float(b"...")` works on bytes |
  | `"33,89"`, `"33.89°"`, `"33 89"`, `"0x1p3"` | 0 | correctly rejected |
  | `[33.89]`, `{"lat": 33.89}` | 0 | correctly rejected (TypeError) |

  Two of these are **live**, not hypothetical. `main.py:90-95` casts master CSV coords with
  `float(row[field])`, which happily produces real NaN/inf floats from the *text* `nan` /
  `-inf` that `write_csv` itself wrote on a previous run. Round-trip verified:

  ```
  master CSV row: lat="nan"  lon="-inf"  rating="1e400"
  load_master ->  lat=nan     lon=-inf
  _validity("lat", nan)    = 3      <-- full marks
  _validity("lon", -inf)   = 3
  _validity("rating", inf) = 0      (rating's range check does catch inf -- correct)
  ```

  And in `_pick`, Google's `lat` (trust 3, `dedup.py:99`) beats any real coordinate from
  OSM or Wikidata (trust 1):

  ```
  Google lat = "nan"    trust 3, validity 3
  OSM    lat = 33.8951  trust 1, validity 3
  _merge(...)[lat] == 'nan'
  ```

  Downstream, NaN coords make `enricher.py:92`'s `lat_min <= lat <= lat_max` silently
  false, so `infer_region` returns `None` and the row loses its region while still being
  reported as fully valid. Nothing anywhere logs that a coordinate was rejected.

- **Contrast (correctly handled):** the `rating` branch at `dedup.py:134-139` *does* apply
  `0 <= v <= 5`, so `nan`, `inf`, `-1`, `"N/A"`, `"4,5"`, `5.0000001` all score 0. The
  asymmetry between the two float branches is the bug — `lat`/`lon` are the only numeric
  fields in `BusinessRecord` with no plausibility bound at all.

- **Fix:** reuse the rating pattern plus finiteness and a real geographic bound:
  `v = float(value); return 3 if -90 <= v <= 90 and math.isfinite(v) else 0` for `lat`,
  `-180 <= v <= 180` for `lon`, and reject `bool` explicitly (`isinstance(value, bool)` →
  0) since `float(True)` is `1.0`.

### S2 — `_URL_OK` has no trailing anchor, so any URL followed by whitespace junk scores 3

- **Where:** `dedup.py:117`, consumed at `dedup.py:126-127`
- **Breaks:** `_URL_OK = r"^https?://[^\s/]+\.[^\s]+"`. There is no `$`, so the match stops
  at the first whitespace and everything after it is ignored. `_EMAIL_OK` at `dedup.py:116`
  is correctly anchored with `$`; this one is not. Measured, `_validity("website", v)`:

  ```
  'https://x.com'                        -> 3   (correct)
  'https://x.com BUY NOW extra'          -> 3   (not a URL)
  'https://x.com\t<a href=x>'            -> 3   (HTML injected into the value)
  'https://x.com\nBcc: attacker@evil.tld'-> 3   (header-injection payload)
  'https://x.com\x00 junk'               -> 3
  'https://x.com/menu?q=a b'             -> 3
  'https://🌐@x.com'                      -> 3   (emoji in the authority)
  'x.com'  (no scheme)                   -> 0   (correct)
  'ftp://x.com' / 'javascript://x.com'   -> 0   (correct)
  'http://localhost'  (no dot)           -> 0   (correct)
  ```

  The source is realistic: `scrapers/osm.py:91` takes `contact:website` / `website` / `url`
  verbatim, and `osm.py:95` only prepends `https://` when no scheme is present — so a
  space-separated pair (`"www.foo.com www.bar.com"`, extremely common from copy-pasted OSM
  tags) becomes a string that starts with a valid scheme and scores full marks.

- **Blast radius (measured, with `source` set so trust applies):** OSM `website` trust is 3
  (`dedup.py:103`), Google's is 2 (`dedup.py:100`). A junk OSM value that scores 3 therefore
  ties Google's clean value on validity and wins on trust:

  ```
  OSM    website = 'https://foo.com http://bar.com'  trust 3, validity 3
  Google website = 'https://real-shop.example'       trust 2, validity 3
  _merge(osm, google)[website] == 'https://foo.com http://bar.com'
  ```

  `enricher.py:175` then GETs that string; `requests` rejects it, `_fetch_website` swallows
  the exception and returns `UNKNOWN` (`enricher.py:176-178`), so `website_live` becomes
  `None`. Per `enricher.py:303` that forfeits the `+20` rebuild pitch the *real* dead site
  would have earned, and `main.py:199-200` files the row under `with_websites` on a website
  that was never reachable. Silent, permanent, in the cumulative master.

- **Fix:** anchor it — `r"^https?://[^\s/]+\.[^\s/]+(?:[/?#][^\s]*)?$"` — and run
  `urlsplit`/`hostname` validation on top. The same anchoring question should be re-checked
  against `_EMAIL_OK`: it is anchored with `$`, which also matches *before* a trailing
  newline, so `"info@x.com\n"` scores 3. That one is harmless only because
  `dedup.py:125` strips first — the strip is load-bearing and undocumented.

### S2 — a non-ASCII or control-character value scores 3 and survives `_pick` as the record's contact

- **Where:** `dedup.py:124-125`, `dedup.py:188`
- **Breaks:** `[^@\s]` and `[^\s]` are Unicode-aware negated classes: they exclude
  whitespace and nothing else. So any non-whitespace codepoint — emoji, bidi overrides,
  zero-width joiners, NUL, BOM — counts as a valid address character.

  ```
  'معلومات@شركة.كوم'  (Arabic local + Arabic domain) -> 3
  '🍎@x.com'          (emoji local part)             -> 3
  'info@x.co\u200b'   (trailing ZWSP)                -> 3
  'info@x.co\ufeff'   (trailing BOM)                 -> 3
  'info@x.co\u202e'   (trailing RTL override)        -> 3
  'info@x.com\x00'    (trailing NUL)                 -> 3
  ['a@x.com']         (single-element list!)         -> 3   <-- str(['a@x.com']) == "['a@x.com']"
  ```

  The list case is the sharpest: `_validity` type-coerces with `str(value)` and never checks
  that `value` is a `str`, so a one-element `list` whose repr happens to match the pattern is
  certified as a maximum-quality email. `_is_missing` (`dedup.py:88`) only treats `str` as
  strippable, so a `list` counts as *present* too. The list then wins `_pick` and is written
  to the CSV:

  ```
  _validity('email', ['contact@shop.com', 'sales@shop.com']) = 0    # two elements: repr has a space
  _validity('email', ['a@x.com'])                             = 3    # one element: repr has no space
  _merge(osm_list, wikidata_str)['email'] == ['contact@shop.com', 'sales@shop.com']
  main.has_any_contact({'email': [...]})  -> True     # main.py:142
  enricher.completeness_score(...)        -> +1       # enricher.py:106
  ```

  `dedup.py:140` (`return 1`, the unrecognised-field default) is the real gap here: fields
  outside the six handled names get *no* type check whatsoever. Confirmed — non-`str` field
  keys (`5`, `None`, `3.5`, `b"lat"`, `("lat",)`) and case/whitespace variants
  (`"LAT"`, `"Lat"`, `"lat "`, `" lat"`) all fall through to `return 1` and are never
  validated.

- **Fix:** gate on `isinstance(value, str)` before the regex branches and return 0
  otherwise; add `[A-Za-z0-9]` (plus a small IDN allowance) to the email/URL patterns, and
  reject control/format codepoints explicitly.

### S3 — field-name matching is exact, so an unrecognised header disables validation entirely and can crash `enrich`

- **Where:** `dedup.py:124-139` vs `main.py:43` (`_FLOAT_FIELDS`)
- **Breaks:** `_validity` compares `field == "lat"` / `field in ("lat", "lon")` exactly.
  `main.py:90-95` casts master CSV floats by iterating the same literal names. Both miss
  any header variant, so a `lat ` column is left as a **string**, `_validity` scores it 1
  via the `return 1` default (i.e. *valid*), and `enricher.py:92` then compares a `float`
  bound against a `str`:

  ```
  header 'lat'      -> load_master lat=33.8951 (float)  _validity=3  infer_region -> 'Beirut'
  header 'lat '     -> lat='33.8951' (str)              _validity=1  TypeError: '<=' not supported between 'float' and 'str'
  header ' lat'     -> lat='33.8951' (str)              _validity=1  TypeError
  header 'Lat'      -> lat='33.8951' (str)              _validity=1  TypeError
  header 'latitude' -> lat='33.8951' (str)              _validity=1  TypeError
  ```

  This is reachable in normal operation: `main.py:118` writes `utf-8-sig` specifically so
  Google Sheets renders Arabic correctly, and `.github/workflows/scrape.yml` round-trips the
  CSVs through Drive/rclone. A single stray space typed into a header in Sheets, or one
  space-separated merge in the CSV text, kills the whole run at `main.py:187` —
  `enricher.infer_region` has no `try`/`except` around its comparison.

- **Fix:** normalise keys once at the boundary (`re.sub(r"\s+", "", k).lower()` in
  `load_master`, and a `set()` of known field names in `_validity` that logs-and-defaults
  rather than silently returning 1); independently, coerce with `float()` at the
  `infer_region` boundary so a stray string cannot kill the pipeline.

---

## Not a bug, but worth knowing

- **No catastrophic backtracking (ReDoS is ruled out).** Both patterns have at most one
  quantifier per position and a single literal separator, so backtracking is linear.
  Measured, doubling cost tracks n exactly:

  | shape | field | n=1k | n=10k | n=100k | n=1M | n=4M |
  |---|---|---|---|---|---|---|
  | `"https://"+"a"*n` | website | 0.04 ms | 0.19 ms | 1.0 ms | 12 ms | 126 ms |
  | `("a."*n)` | email | 0.02 ms | 0.37 ms | 2.2 ms | 23 ms | 261 ms |
  | `("a@"*n)` | email | 0.001 ms | 0.001 ms | 0.002 ms | 0.002 ms | 0.007 ms |

  Even a 4 MB `contact:website` tag costs ~180 ms per call, and `_validity` is at most
  called twice per key per merge — so no cost finding. This is worth stating explicitly
  because `_URL_OK` is the shape that usually *does* blow up.

- **`_pick`'s `str(value)` tiebreak is genuinely order-stable**, including for dicts and
  lists, because it compares the same pair of strings regardless of which side is `a`.
  Verified: `_merge(d1, d2)['tag'] == _merge(d2, d1)['tag'] == {'b': 1, 'a': 2}`. The
  "deterministic final tiebreak" comment at `dedup.py:184` is accurate. It does mean the
  winner among equal-trust/equal-validity values is *lexicographically largest*, which is
  arbitrary rather than meaningful — but not a correctness bug.

- **`_is_missing` correctly treats `""` and whitespace as missing** (`dedup.py:88-89`), which
  is a real fix over the old `is None`. `"\xa0"` and `"\u3000"` are also caught, because
  `str.strip()` and `re`'s `\s` agree on Unicode whitespace.

- **`False`/`0` counting as *present* is right, not a bug.** `dedup.py:90` returns `False`
  for non-`str` non-`None` values, so `website_live=False` and `review_count=0` are
  treated as information. That is the correct semantics and it matches
  `enricher.py:300-304`. Just be aware it means `[]` and `{}` are *also* "present".

- **The `float()` acceptance of Unicode decimal digits is shared with `main.py:93`**, so
  `'٣٣.٨٩'` in a master CSV is laundered into a genuine `float` 33.89. `_validity` and
  `load_master` agree here, so it is not an inconsistency — it is a shared permissiveness.

---

## Classification table

Every input from the brief, with the measured result. **Crash** = raises a traceback from
`_validity` itself; **Wrong** = returns a value that misrepresents the data; **OK** =
correctly handled.

| Input | Field | Result | Class |
|---|---|---|---|
| `None` | all | `-100` (unreachable) | OK |
| `""` | all | `-100` | OK |
| `"   "` / `"\xa0"` / `"\u3000"` | all | `-100` | OK |
| `0` / `0.0` | lat, rating | `3` | OK (`rating` 0 is legitimately in range; `lat` 0.0 is Null Island — see S1) |
| `False` / `0` | website_live, review_count | `1` | OK (present, not missing) |
| `True` | lat, rating | `3` | **Wrong** — `float(True) == 1.0` |
| `999.0`, `-33.89` | lat | `3` | **Wrong** — no range check |
| `float("nan")`, `"nan"`, `"-nan"` | lat/lon | `3` | **Wrong** |
| `float("inf")`, `"inf"`, `"1e400"`, `"9"*400` | lat/lon | `3` | **Wrong** |
| `"nan"`, `inf` | rating | `0` | OK (range check catches it) |
| `"1_0"`, `"1_0.5"` | lat | `3` | **Wrong** — PEP-515 underscores |
| `" 33.8951 "` | lat | `3` | OK (stripped by `float`) |
| `"٣٣.٨٩"`, `"３３.８９"` | lat | `3` | **Wrong** — Unicode `Nd` accepted |
| `b"33.89"` | lat | `3` | **Wrong** — `float(bytes)` works |
| `"33,89"`, `"33.89°"`, `"33 89"`, `"0x1p3"` | lat | `0` | OK |
| `[33.89]`, `{"lat": 33.89}` | lat | `0` | OK |
| `"info@x.com"`, `"Info@X.COM"`, `"a@b.c"` | email | `3` | OK |
| `"info@localhost"`, `"@x.com"`, `"info@.com"`, `"info@@x.com"` | email | `0` | OK |
| `"info@x.com\nBcc: v@y.com"` | email | `0` | OK (`$` anchor works) |
| `"info@x.com\x00"`, `"info@x.com\r"` | email | `3` | **Wrong** — control char passes |
| `"معلومات@شركة.كوم"`, `"🍎@x.com"` | email | `3` | **Wrong** — non-ASCII accepted |
| `"info@x.co\u200b"` / `\ufeff` / `\u202e` | email | `3` | **Wrong** — zero-width/bidi passes |
| `["a@x.com"]` (1 element) | email | `3` | **Wrong** — non-`str` certified valid |
| `["a@x.com", "b@x.com"]` (2 elements) | email | `0` | OK (accidentally, via the repr's space) |
| `"https://x.com"`, `"HTTPS://X.COM"` | website | `3` | OK |
| `"x.com"`, `"ftp://x.com"`, `"javascript://x.com"`, `"http://localhost"` | website | `0` | OK |
| `"https://x.com BUY NOW"`, `"…\nBcc: …"`, `"…\t<a href=x>"` | website | `3` | **Wrong** — no trailing anchor |
| `"https://🌐@x.com"` | website | `3` | **Wrong** |
| `"https://شركة"` (dotless) | website | `0` | OK |
| 1 MB / 4 MB strings | email, website | `0` or `3` in ≤ 261 ms | OK (linear) |
| field = `5`, `None`, `b"lat"`, `"LAT"`, `"lat "` | any | `1` | **Wrong** — silently unvalidated |
| `__str__`/`__float__` raising | any | `RuntimeError` | Crash — but unreachable from JSON |

---

## The single most dangerous input

```python
record["website"] = "https://foo.com http://bar.com"   # from an OSM contact:website tag
```

**Why this one.** It is the only input that gets `_validity`'s **maximum** score while being
unambiguously not a URL, and it lands on the field with the **highest source trust**:

1. `_validity` scores it **3** because `_URL_OK` (`dedup.py:117`) has no trailing anchor.
   Compare with `"x.com"` or `"ftp://x.com"`, which are correctly scored 0 — the regex is
   not merely lenient, it is lenient in exactly the direction that matters.
2. `scrapers/osm.py:91` copies `contact:website` verbatim, and `osm.py:95` prepends
   `https://` when no scheme is present, so a space-separated pair of URLs — the single most
   common shape of a hand-edited OSM website tag — arrives already looking scheme-correct.
3. `_SOURCE_TRUST["osm"]["website"] = 3` (`dedup.py:103`) is the highest in the table, above
   Google's `websiteUri` at 2 (`dedup.py:100`). Combined with the S1 ordering, the junk
   value wins over a clean Google URL **and** wins even when both score the same validity.
4. `website` is in `_VOLATILE` (`dedup.py:114`), so it is actively re-decided on every
   merge against the freshest record — this field is rewritten more often than any other.
5. The consequence is silent and irreversible: `enricher.py:175` GETs an unparseable URL,
   `_fetch_website` returns `UNKNOWN` (`enricher.py:176-178`), `website_live` becomes
   `None`, and `enricher.py:303` withholds the `+20` rebuild pitch that the site's *real*
   dead-site verdict would have earned. `main.py:199-200` still files it under
   `with_websites`. `main.py:209` then writes it into the cumulative master, so it is
   never re-evaluated.

The input is realistic, the defect is in `_validity` itself, the trust table amplifies it,
and there is no log line, no warning, and no path back. That combination — maximum score,
highest trust, most-volatile field, silent, permanent — is why this one ranks above the
NaN coordinate and above the trust-before-validity inversion, both of which damage more
data per occurrence but require a rarer input to trigger.

## Recommended order of work

1. Reorder `_pick`'s rank tuple so validity gates trust (S1, `dedup.py:179-187`).
2. Add `math.isfinite` + geographic bounds + a `bool` guard to the `lat`/`lon` branch, and
   mirror `rating`'s structure (`dedup.py:128-133`).
3. Anchor `_URL_OK` with `$` and validate the host (`dedup.py:117`).
4. `isinstance(value, str)` gate before the email/website regexes (`dedup.py:124-127`).
5. Normalise field keys at the `load_master` boundary and coerce floats at the
   `infer_region` / `lead_score` boundary so a header typo cannot kill the run
   (`main.py:86-104`, `enricher.py:89-92`, `enricher.py:312-313`).
6. Delete the unreachable `-100` branch or make `_pick` actually consult it
   (`dedup.py:122-123`).
