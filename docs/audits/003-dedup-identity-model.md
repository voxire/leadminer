# 003 — dedup.py identity model: phone-then-(name, city) keying

## Verdict

The identity key is `normalize_phone(phone)`, else `(normalize_name(name), city)`
where `city` is the **last** comma-separated segment of the address
(`dedup.py:34-38`). Because Lebanese/KSA addresses end in the *country*
("Beirut, Lebanon" → `lebanon`, "Hamra, Beirut, Lebanon" → `lebanon`), the
"city" component is almost always the country name, so the name-level
disambiguator is a silent no-op and every same-named, phone-less business
nationwide collapses into one record. On the other side, the code's attempt to
reconcile name-indexed records against phone-indexed records is dead code
(`dedup.py:90-95`), so any business present with a phone in one source and
without in another is emitted twice. The model over-merges distinct businesses
and under-merges the same one, in the same run.

## Findings

### S1 — `_extract_city` returns the country, not the city → same-named businesses over-merge

- **Where:** `dedup.py:34-38` (`_extract_city`) and `dedup.py:77-79` (the `(name, city)` key).
- **Breaks:** `_extract_city` splits on `,` and returns `parts[-1]`. For the
  address formats actually produced by the scrapers, the last segment is the
  country:

  | Address | `_extract_city` result |
  |---|---|
  | `Beirut, Lebanon` | `lebanon` |
  | `Hamra, Beirut, Lebanon` | `lebanon` |
  | `Jounieh, Lebanon` | `lebanon` |
  | `Tripoli, Lebanon` | `lebanon` |
  | `Riyadh, Saudi Arabia` | `saudi arabia` |

  So the `city` discriminator is constant per country. Two **different**
  phone-less businesses with the same normalized name in different cities get
  the same key and are merged, and `_merge` (`dedup.py:45-61`) silently discards
  the losing record's address/lat/lon. Chains and generic names are worst-hit:
  `Zaatar w Zeit` in Beirut and in Jounieh, or two independent `City Pharmacy`
  shops, become one record.
- **Trigger:** `{name: "Zaatar w Zeit", address: "Beirut, Lebanon", phone: None}`
  and `{name: "Zaatar w Zeit", address: "Jounieh, Lebanon", phone: None}` →
  both key `("zaatar w zeit", "lebanon")` → merged, one branch lost.
- **Fix:** extract the city as the second-to-last segment, strip the country
  token, and validate against a known-city list — or better, drop the address
  heuristic entirely and key on a stronger signal (see "Recommended order").

**Working the "Beirut, Lebanon" vs "Hamra, Beirut, Lebanon" case specifically:**
these two addresses describe the *same* city (Hamra is a neighborhood of
Beirut), so a correct city extractor should map both to `beirut` and merge them
as intended. The current code does merge them — but for the wrong reason: both
collapse to `lebanon`, the country. The harm is that it also merges
"Beirut, Lebanon" with "Tripoli, Lebanon" or "Sidon, Lebanon" whenever the names
match, because the country token erases all city-level distinction. The
`Hamra`/`Beirut` difference was the one piece of information that could have
separated two same-named businesses, and it is dropped entirely.

### S1 — Cross-index dedup between phone and name keys is dead code → same business under-merges

- **Where:** `dedup.py:90-95`, contradicting the comment at `dedup.py:85-86`.
- **Breaks:** Records with a phone go to `phone_index`; records without a phone
  go to `name_index`. The reconciliation loop reads `record.get("phone")` off
  the *name-index* values — but those are only ever constructed from phone-less
  records (`dedup.py:76-83`), and `_merge` preserves a `None` phone. So `raw` is
  always falsy, the `continue` never fires, and a business that appears with a
  phone in one source and without in another is emitted **twice**. The comment
  ("name-keyed ones that don't share a phone with an already-captured record")
  describes behavior that cannot happen.
- **Trigger:** Record A (OSM) `{name: "Falafel Tabbara", phone: "01 234 567"}`
  → `phone_index["+9611234567"]`. Record B (Wikidata, phone rarely populated via
  `P1329`, `scrapers/wikidata.py:14`) `{name: "Falafel Tabbara", phone: None}`
  → `name_index[("falafel tabbara", "lebanon")]`. Output contains both A and B.
- **Fix:** after building both indexes, for each `name_index` record compute its
  normalized name+city and merge it into any `phone_index` record with a
  matching `(normalize_name(name), city)` — i.e. cross-reference by name, not by
  the phone the name-index record doesn't have.

### S1 — Address-format variance makes the name key brittle → same business under-merges across sources

- **Where:** `dedup.py:34-38` combined with the scrapers' different address
  shapes: `scrapers/osm.py:77-84` builds `"housenumber, street, suburb, city,
  district"` (no country), while `scrapers/google_places.py:254` uses
  `formattedAddress`, which ends in the country.
- **Breaks:** the last-segment heuristic depends on how many commas and whether
  the country is present. The same phone-less business yields different keys:

  | Source | Address | city key |
  |---|---|---|
  | OSM | `Hamra, Beirut` | `beirut` |
  | Google | `Hamra, Beirut, Lebanon` | `lebanon` |
  | OSM | `Hamra Street, Hamra, Beirut` | `beirut` |
  | Google | `Hamra Street, Hamra, Beirut, Lebanon` | `lebanon` |

  Same business, different keys → no merge. The problem is symmetric to S1:
  the country token appears in Google addresses but not OSM addresses, so the
  "city" string is not stable across sources.
- **Trigger:** an OSM phone-less record with `address: "Hamra, Beirut"` and a
  Google phone-less record with `address: "Hamra, Beirut, Lebanon"`.
- **Fix:** canonicalize the address to a city token that is independent of
  country/neighborhood presence (strip country, map to a city gazetteer), and
  prefer `lat`/`lon` proximity as a tiebreak when both records have coordinates.

### S2 — `normalize_name` under-normalizes → same business under-merges

- **Where:** `dedup.py:28-31`.
- **Breaks:** NFKD + strip-combining handles Latin accents but not the cases
  that matter for Arabic-named businesses, nor legal-suffix/punctuation noise:

  - Arabic alef variants `أ`/`ا`/`إ`/`آ` have no canonical decomposition and are
    left distinct; `ة` vs `ه` and `ى` vs `ي` are also not unified. So
    `مطعم الأرز` vs `مطعم الارز` produce different keys.
  - Legal-entity suffixes are not stripped: `ABC Real Estate S.A.L.` vs
    `ABC Real Estate` differ (and `S.A.L.`/`SARL`/`sarl` are pervasive in
    Lebanon).
  - Punctuation and stop-words are kept: `Cafe Younes'` vs `Cafe Younes`,
    `Al Tawouk` vs `Tawouk`.

- **Trigger:** `{name: "مطعم الأرز"}` vs `{name: "مطعم الارز"}`; or
  `{name: "ABC Real Estate S.A.L."}` vs `{name: "ABC Real Estate"}`.
- **Fix:** add Arabic normalization (strip hamza, map `ة→ه`, `ى→ي`), strip
  legal-suffix tokens and punctuation, and consider a secondary identity such as
  normalized website domain.

### S3 — `_merge` resolves field conflicts by whole-record field count, not field quality

- **Where:** `dedup.py:41-42` (`_field_count`) and `dedup.py:59-60` (the
  tiebreak).
- **Breaks:** when two records of the same business conflict on a field, the
  winner is "the record with more non-None fields overall" — applied uniformly,
  including to `name`. A record that happens to have more populated columns but
  a noisy name (e.g. `Cafe (Hamra)`) overwrites a cleaner name from a sparser
  record. `_field_count` also treats `""` and `0` as "present", so an empty
  string beats a `None` in the count even though it's worse.
- **Trigger:** `a = {name: "Cafe (Hamra)", phone:…, website:…, email:…, rating:…}`
  (5 fields) vs `b = {name: "Cafe Hamra"}` (1 field) → `a`'s name wins.
- **Fix:** per-field quality (prefer non-empty, longer, or a source-priority
  ranking) instead of a single global count; special-case `name` to prefer the
  longer or less-punctuated value.

## Not a bug, but worth knowing

- **Phone-first precedence means a shared phone over-merges distinct
  businesses.** `dedup.py:70-75` keys any record with a phone on the phone
  alone, ignoring name/city. A chain using one central hotline (or a building
  reception number shared by tenants) merges all branches into one record.
  Defensible for lead-gen, but it's a real source of over-merge that no later
  step corrects.
- **`country` is inferred from the search query, not the place.**
  `scrapers/google_places.py:126-135` (`_country_from_query`) defaults to `"LB"`
  and only keys off substrings like "riyadh"/"jeddah". A KSA place surfaced by a
  query that doesn't contain those substrings is tagged `LB`, and
  `normalize_phone` will then prefix it `+961`. A Lebanese number with the same
  trailing digits would collide. `normalize_phone`'s
  `_COUNTRY_CODES.get(country, ("961", "+961"))` (`dedup.py:13`) silently maps
  any unknown country to Lebanon, compounding this.
- **`normalize_phone` doesn't strip extensions.** `"01 234 567 ext 12"` →
  `+961123456712` ≠ `+9611234567`, so the same business with an extension on one
  source under-merges. It also returns `"+" + digits` for sub-7-digit inputs
  (`dedup.py:23-25`), which will never match the full form.
- **Phone normalization happens after dedup on the first run** (`main.py:134-136`),
  but the master CSV is reloaded pre-normalized on later runs. It happens to be
  idempotent today, but dedup's correctness depends on `normalize_phone` staying
  idempotent and on scrapers not introducing new formats.
- **Name+city cannot distinguish same-name businesses in the same city.** Even a
  correct city extractor cannot separate two phone-less `City Pharmacy` shops in
  Beirut. `lat`/`lon` proximity is the missing disambiguator for that case.

## Recommended order of work

1. **Fix `_extract_city` / the name key** — extract the actual city (strip the
   country token, use second-to-last segment or a gazetteer) so "Beirut" and
   "Hamra" map to the same `beirut` while "Tripoli" stays distinct. This is the
   largest silent-data-loss fix (S1).
2. **Make the phone/name cross-dedup actually run** — merge `name_index` records
   into `phone_index` records by matching `(normalize_name(name), city)` (S1).
3. **Stabilize the name key across sources** — canonicalize address, and add a
   `lat`/`lon`-proximity fallback so OSM "Hamra, Beirut" and Google
   "Hamra, Beirut, Lebanon" merge (S1).
4. **Add Arabic + legal-suffix name normalization** (S2).
5. **Harden `normalize_phone`** (strip extensions; stop silently defaulting
   unknown countries to Lebanon) and **improve `_merge` field selection** (S3).
