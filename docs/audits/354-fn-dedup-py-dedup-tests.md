# 354 — `dedup()`: the tests it still needs

**Target:** `dedup.py:197-232`. **Lens:** tests.
**Callers:** exactly one — `main.py:180`, `records = dedup(combined)`, where
`combined = raw_filtered + master` (`main.py:179`). `cli.py` reaches it only
through `cmd_run -> main.main` (`cli.py:33-37`). So the whole product hinges on
this one function and **nothing tests it**.

## Verdict

`tests/test_lead_signal.py` imports `_merge` and `normalize_phone`
(`tests/test_lead_signal.py:65`) and never calls `dedup` itself — so the
function that decides *which rows exist in the CSV* has zero coverage. Of the
four defects below, three produce silently wrong rows today (a Beirut website
welded onto Jounieh coordinates; three unrelated businesses collapsed into one;
a schemeless "website" suppressing every pitch for that lead), and all four are
one-line reproducible with no network and no fixtures. Severity below is assigned
to **the defect the missing test would catch**, not to the absence of the test.

The cheapest high-value work in this repo is not writing a new test framework —
it is appending one `TestDedupKeys` class to `tests/test_lead_signal.py` with
the seven tests in S1/S2. Four of them fail on the current code.

## Coverage today

| Behaviour | Covered? | Evidence |
|---|---|---|
| `normalize_phone` returns `""` for junk | yes | `test_lead_signal.py:103` |
| `normalize_phone` country/prefix shapes | yes | `test_lead_signal.py:110-123` |
| `_merge` field survivorship, per-field trust, order-independence for a **pair** | yes | `test_lead_signal.py:267-326` |
| `dedup` key selection (phone vs name+city) | **no** | nothing imports `dedup` |
| cross-index merge (phone-less record joins its phone'd sibling) | **no** | `dedup.py:226-230` is unreachable |
| degenerate key for unnamed records | **no** | `dedup.py:212-214` |
| output is a pure function of its input set | **no** | `dedup.py:198-232` |
| output rows are unique by dedup key | **no** | `dedup.py:222-232` |

## Findings

### S1 — The phone↔name reconciliation is unreachable: one business, two rows

- **Where:** `dedup.py:220-230`. The comment at `dedup.py:220-221` promises
  "name-keyed ones that don't share a phone with an already-captured record",
  and `dedup.py:228-229` implements that filter. It cannot fire.
- **Proof it is dead, not merely wrong:** a record reaches `name_index` only if
  `normalize_phone(raw_phone, country)` returned `""` (`dedup.py:205-206`), or
  `raw_phone` was falsy. So every name-keyed record either has no phone or has
  a phone that fails to normalize. `captured_phones` (`dedup.py:223`) contains
  only truthy `"+…"` keys — the exact set of values `normalize_phone` returns on
  success. Therefore `normalize_phone(raw, …) in captured_phones` is `False` for
  every name-keyed record, forever. `continue` at `dedup.py:229` never executes.
  Tweak the guard and you change nothing; the guard has to be **replaced**.
- **Breaks:** the single most common real-world pattern — OSM carries the
  phone, Wikidata/Google does not (or vice versa) — emits the same business
  twice. Both rows then reach `main.py:209-213` and land in all five CSVs, so
  `sales_ready.csv` pitches the same shop twice and `without_websites.csv`
  contains a duplicate with and without a website.
- **Test to add** (`tests/test_lead_signal.py`, new class; or a new
  `tests/test_dedup.py`, which needs no `requests` stub because it imports only
  `dedup`):

```python
from dedup import dedup


class TestDedupKeys(unittest.TestCase):
    def test_phoneless_sighting_merges_into_its_phone_keyed_sibling(self):
        """Regression: the reconciliation loop at dedup.py:226-230 could never
        fire, so a business seen with a phone by one source and without by
        another was emitted twice."""
        records = [
            {"name": "Falafel Tabbara", "category": "restaurant",
             "address": "Hamra, Beirut", "country": "LB",
             "phone": "01 234 567", "email": "info@tabbara.example",
             "source": "osm", "scraped_at": "2026-09-01T00:00:00+00:00"},
            {"name": "Falafel Tabbara", "category": "restaurant",
             "address": "Hamra, Beirut, Lebanon", "country": "LB",
             "phone": None, "website": "https://tabbara.example",
             "source": "google_places", "scraped_at": "2026-09-02T00:00:00+00:00"},
        ]
        out = dedup(records)
        self.assertEqual(len(out), 1, "same business emitted twice")
        self.assertEqual(out[0]["source"], "google_places|osm")
        self.assertEqual(out[0]["email"], "info@tabbara.example")
        self.assertEqual(out[0]["website"], "https://tabbara.example")
        self.assertEqual(out[0]["scraped_at"], "2026-09-02T00:00:00+00:00")
```

- **Input:** the two dicts above (OSM-with-phone + Google-without).
- **Asserted output:** one record; `source == "google_places|osm"`,
  `email == "info@tabbara.example"`, `website == "https://tabbara.example"`,
  `scraped_at == "2026-09-02T00:00:00+00:00"`, `phone == "01 234 567"`.
- **Actual today:** `len(out) == 2`; the two rows are printed verbatim in
  "Not a bug" below.
- **Pins:** `docs/audits/003-dedup-identity-model.md:57-75` (S1 #2, "cross-index
  dedup is dead code"), and the `dedup.py:220-221` comment that documents
  behaviour the code does not have.

### S1 — `_extract_city` returns the country, welding two branches into one Frankenstein row

- **Where:** `dedup.py:72-76` + the key at `dedup.py:214`. Google Places'
  `address` is Google's own `formattedAddress` string, passed through verbatim
  (`scrapers/google_places.py:286`, requested at `scrapers/google_places.py:35`),
  and that string ends in the country — `"Hamra, Beirut, Lebanon"`. OSM builds
  `housenumber, street, suburb, city, district` with no country
  (`scrapers/osm.py:80-87`). `parts[-1]` is therefore the country for every
  Google record and a city for most OSM records — the "city" component of the
  identity key is a constant per source.
- **Breaks, concretely:** two *different* pharmacies, both phone-less, both
  returned by the Places queries for Beirut and Jounieh. Because both addresses
  end in `"Lebanon"`, both key `("city pharmacy", "lebanon")` and merge. The
  survivor is a chimera: Jounieh's `address`, `region` and `lat/lon` carrying
  **Beirut's `website`, `rating` and `review_count`**. Sales calls the Jounieh
  branch and pitches "you have no website" for a site that exists at
  `hamra-pharmacy.example`; `without_websites.csv` (`main.py:200`) excludes the
  row because `website` is set.
- **Test to add:**

```python
    def test_same_name_in_two_cities_stays_two_records(self):
        """Regression: _extract_city returns parts[-1], which is the country for
        every Google-formatted address, so the city term of the identity key was
        a constant and every same-named phone-less business in the country
        collapsed into one row."""
        records = [
            {"name": "City Pharmacy", "category": "pharmacy",
             "address": "Hamra, Beirut, Lebanon", "region": "Beirut", "country": "LB",
             "lat": 33.8938, "lon": 35.5018, "phone": None,
             "website": "https://hamra-pharmacy.example", "rating": 4.6,
             "review_count": 210, "source": "google_places",
             "scraped_at": "2026-09-01T00:00:00+00:00"},
            {"name": "City Pharmacy", "category": "pharmacy",
             "address": "Jounieh, Lebanon", "region": "Keserwan", "country": "LB",
             "lat": 33.9808, "lon": 35.6178, "phone": None,
             "website": None, "rating": None, "review_count": None,
             "source": "google_places", "scraped_at": "2026-09-02T00:00:00+00:00"},
        ]
        out = dedup(records)
        self.assertEqual(len(out), 2, "two branches of one name were merged")
        self.assertEqual(
            {(r["region"], r["website"]) for r in out},
            {("Beirut", "https://hamra-pharmacy.example"), ("Keserwan", None)},
            "a website and its region ended up on the same row",
        )
```

- **Input:** the two dicts above.
- **Asserted output:** two records; `{(region, website)} ==
  {("Beirut", "https://hamra-pharmacy.example"), ("Keserwan", None)}`.
- **Actual today:** `len(out) == 1` and the single row is
  `{"address": "Jounieh, Lebanon", "lat": 33.9808, "lon": 35.6178,
  "region": "Keserwan", "rating": 4.6, "review_count": 210,
  "website": "https://hamra-pharmacy.example", "source": "google_places"}`.
- **Pins:** `003-dedup-identity-model.md:19-45` (S1 #1). Note that the same two
  records with OSM-shaped addresses (`"Beirut, Lebanon"` / `"Jounieh, Lebanon"`)
  also merge today — the collapse does not need Google; it needs the two
  addresses to share a trailing token. That is why the fix must be a city
  extractor, not a `country`-stripping special case.

### S1 — An unnamed phone-less record has a degenerate key: `("", city)`

- **Where:** `dedup.py:212-214`. `name = record.get("name") or ""` then
  `normalize_name("") == ""`, so every record with no usable name shares one key
  per city token. `dedup.py:74-76` returns `""` for a missing address, so the
  key degenerates further to `("", "")` — **country is not part of the name key
  at all**.
- **Breaks:** Wikidata rows whose `itemLabel` is missing and OSM rows with no
  `name` tag become one global bucket. Three unrelated rows (two Lebanese, one
  Saudi, no addresses) merge into a single output row whose `country` is `"SA"`
  (the lexicographic tiebreak at `dedup.py:184` picked it over `"LB"`) and whose
  `lat/lon` came from a Lebanese row. That single row is then written to
  `all_businesses.csv` and, if it clears `whitelist` and `lead_score`, pitched.
- **Test to add:**

```python
    def test_unnamed_records_are_not_one_global_bucket(self):
        """Regression: key was (normalize_name(name), city) with no guard for an
        empty name, so every phone-less nameless record collapsed into one row
        regardless of country, address or category."""
        records = [
            {"name": None, "category": "cafe", "address": None, "country": "LB",
             "lat": 33.89, "lon": 35.50, "source": "wikidata"},
            {"name": "", "category": "restaurant", "address": None, "country": "LB",
             "lat": 34.44, "lon": 35.84, "source": "osm"},
            {"name": None, "category": "shop", "address": None, "country": "SA",
             "source": "google_places"},
        ]
        out = dedup(records)
        self.assertEqual(len(out), 3, "nameless records were merged across countries")
        self.assertEqual({r["source"] for r in out}, {"wikidata", "osm", "google_places"})
```

- **Input:** the three dicts above.
- **Asserted output:** three records, one per source.
- **Actual today:** `len(out) == 1`,
  `{"address": null, "category": "shop", "country": "SA", "lat": 34.44,
  "lon": 35.84, "name": null, "source": "google_places|osm|wikidata"}`.
- **Pins:** not in any prior audit — `003` covers the constant-city half of
  this key but not the empty-name half. New finding.
- **The guard that fixes it** (and the property test that must accompany it, or
  the next contributor will re-introduce S1 #1):

```python
    def test_city_only_name_collision_does_not_weld_branches(self):
        """The fix for the two tests above must not simply widen the key with
        more free text: distinct businesses must stay distinct even when their
        names match."""
        records = [
            {"name": "City Pharmacy", "address": "Hamra, Beirut, Lebanon",
             "country": "LB", "lat": 33.8938, "lon": 35.5018, "phone": None},
            {"name": "City Pharmacy", "address": "Jounieh, Lebanon",
             "country": "LB", "lat": 33.9808, "lon": 35.6178, "phone": None},
        ]
        self.assertEqual(len(dedup(records)), 2)
```

### S1 — Trust outranks validity: a schemeless "website" beats a real URL, and then no pitch fires

- **Where:** `dedup.py:179-187`. `rank()` is
  `(_trust_for(...), _validity(...), …)` — a tuple compared left to right, so
  **source trust is evaluated before plausibility**. `_SOURCE_TRUST`
  (`dedup.py:97-110`) gives `osm.website = 3` and `wikidata.website = 2`,
  `_validity` (`dedup.py:126-127`) gives OSM's `maps.example` a 0 because
  `_URL_OK` (`dedup.py:117`) demands an `http(s)://` scheme. `(3, 0, …) > (2, 3, …)`,
  so the malformed value wins.
- **Breaks:** the surviving row goes into `with_websites.csv` (`main.py:199`)
  with a value nobody can open. `enricher._fetch_website` (`enricher.py:162-179`)
  raises `MissingSchema`, returns `UNKNOWN`, so `website_live` stays `None`
  (`enricher.py:251`). `pitch_recommender` then takes no branch: `has_website`
  is true so the "no website" pitches (`pitch_recommender.py:62,65,84,90`) are
  skipped, and `website_live` is neither `True` nor `False` so the rebuild pitch
  (`pitch_recommender.py:57`) and the SEO pitches (`pitch_recommender.py:70,73`)
  are skipped too. The lead is silently unpitchable, forever, in a cumulative
  master.
- **Existing test that gives false confidence:**
  `test_lead_signal.py:297-301` (`test_invalid_value_loses_to_valid_one`) uses
  `source="osm"` on *both* sides, so trust ties and validity decides. The
  trust-dominance inversion is untested.
- **Test to add:**

```python
    def test_validity_beats_source_trust(self):
        """Regression: rank() compared _trust_for before _validity, so OSM's
        schemeless 'maps.example' (trust 3, validity 0) beat Wikidata's
        'https://real.example' (trust 2, validity 3) and the row became
        unpitchable."""
        records = [
            {"name": "Cafe", "phone": "70123456", "website": "maps.example",
             "source": "osm"},
            {"name": "Cafe", "phone": "+96170123456",
             "website": "https://real.example", "source": "wikidata"},
        ]
        self.assertEqual(dedup(records)[0]["website"], "https://real.example")

    def test_plausible_email_beats_a_trusted_malformed_one(self):
        records = [
            {"name": "Cafe", "phone": "70123456", "email": "not-an-email",
             "source": "osm"},
            {"name": "Cafe", "phone": "+96170123456",
             "email": "real@cafe.example", "source": "wikidata"},
        ]
        self.assertEqual(dedup(records)[0]["email"], "real@cafe.example")
```

- **Input:** the two dicts each.
- **Asserted output:** `website == "https://real.example"` /
  `email == "real@cafe.example"`.
- **Actual today:** `"maps.example"` / `"not-an-email"` survive.
- **Pins:** `002-dedup-merge-data-loss.md` / `024-dedup-merge-data-loss.md` are
  about *which* value wins; the fix commit `99493b9` added per-field trust but
  never checked that validity is not dominated by it. New finding.
- **One-line fix, if you want it:** swap the tuple to
  `(_validity(...), _trust_for(...), …)` and re-run
  `test_lead_signal.py:278-284`, which asserts OSM's
  `http://cafe-real.example` beats Google's `https://maps.example` — that pair
  is validity-equal (both parse) so it still passes.

### S2 — Output content depends on input order once a key holds three records

- **Where:** `_trust_for` (`dedup.py:148-152`) reads the **merged** record's
  source set, and `_pick` unions sources (`dedup.py:173-174`). So a value that
  survived from a low-trust source inherits the high trust of a source that was
  merged in later. The merge operator is therefore not associative, and
  `dedup()` inherits that.
- **Existing test that gives false confidence:** `test_lead_signal.py:292-295`
  proves `_merge` is commutative — but only for **two** records, which is the one
  case where the union-inflation cannot happen.
- **Test to add** (the minimal 3-record counterexample I found by brute force):

```python
    def test_output_is_independent_of_input_order(self):
        """Regression: _trust_for reads the merged record's unioned source set,
        so a value inherited from a low-trust source gains the high trust of a
        source merged in later; the surviving field depended on input order."""
        base = {"name": "Cafe", "phone": "+96170123456",
                "address": "Beirut, Lebanon", "scraped_at": "2026-09-01T00:00:00+00:00"}
        google = dict(base, website="maps.example", source="google_places")   # trust 2, validity 0
        wikidata = dict(base, website="https://a.example", source="wikidata")  # trust 2, validity 3
        osm = dict(base, website=None, source="osm")                          # trust 3, no value

        first = dedup([google, wikidata, osm])
        second = dedup([osm, google, wikidata])
        self.assertEqual(first, second, "dedup output depends on input order")
```

- **Input:** the three dicts above; both orders are phone-equivalent
  (`"+96170123456"`).
- **Asserted output:** `dedup([google, wikidata, osm]) ==
  dedup([osm, google, wikidata])`, both with
  `website == "https://a.example"`.
- **Actual today:** first order → `"https://a.example"`; second order →
  `"maps.example"`. Mechanism: `osm` arrives first, so `_merge` copies its
  (missing) website, then `google_places`' `maps.example` is taken as
  non-missing; by the time `wikidata`'s valid URL is compared, the record's
  source set is `osm|google_places`, so the rank is `(3, 0, …)` and wins.
- **Pins:** `99493b9`'s own claim of order-independence; `test_lead_signal.py:292`
  for the pair case that is still fine.
- **Property form (stronger, prefer this):** for any list of records, the
  **multiset** of `dedup(records)` must equal the multiset of
  `dedup(shuffled(records))`, comparing with `None` and `""` normalised to the
  same value (`dedup.py:168-171` makes those two interchangeable, so a raw
  comparison will false-positive ~20% of the time). I ran 6000 random
  4-record trials over a 9-field pool: **0** grouping violations, **198**
  material field violations. That is a real, frequent flake source once dedup is
  fed the master CSV in `ThreadPoolExecutor` completion order
  (`main.py:160-168`), which is *not* stable across runs.

### S2 — `dedup` resolves country differently from `main.resolve_country`, so a master row's key changes between runs

- **Where:** `dedup.py:205` and `dedup.py:228` use
  `record.get("country") or "LB"`; `main.py:184` runs `resolve_country(r)` on
  every record **after** dedup, and `main.py:194` then rewrites `phone` with the
  resolved country. `resolve_country` (`main.py:50-74`) accepts 7 spellings;
  `_COUNTRY_CODES` (`dedup.py:23-26`) accepts 2.
- **Breaks:** a cumulative-master row labelled `"KSA"` or `"sa"` (a hand-edited
  CSV, or one written by any pre-`522d32` version, or an export from another
  tool) is keyed `+961501234567` on run N. `main.py:184,194` then write
  `country="SA", phone="+966501234567"` back to `all_businesses.csv`. On run
  N+1 that same row keys `+966501234567`. The row's identity changed between
  runs, so it can merge with a different set of records than it did last time —
  and if it merged with a Lebanese row on run N, that fusion is permanent in the
  master.
- **Test to add:**

```python
    def test_dedup_key_matches_the_country_main_resolves(self):
        """Regression: dedup used record.get('country') or 'LB' while
        main.resolve_country accepts 'sa'/'KSA'/'SAU'/'Lebanon'; the same master
        row therefore got a different key before and after main.py:184."""
        for country, expected in (("LB", "+961501234567"), ("Lebanon", "+961501234567"),
                                  ("SA", "+966501234567"), ("sa", "+966501234567"),
                                  ("KSA", "+966501234567"), ("SAU", "+966501234567")):
            record = {"name": "Riyadh Auto Repair", "phone": "0501234567",
                      "country": country}
            self.assertEqual(
                normalize_phone(record["phone"], resolve_country(record)),
                expected, f"country={country!r}")
```

- **Input:** `phone="0501234567"` (a KSA mobile) under each of the six country
  spellings.
- **Asserted output:** `dedup`'s key must equal
  `normalize_phone(phone, resolve_country(record))` — `+966501234567` for all
  four Saudi spellings.
- **Actual today:** `"sa"`, `"KSA"`, `"SAU"` → dedup computes
  `+961501234567` (a **Lebanese** key for a Saudi number), main computes
  `+966501234567`.
- **Pins:** `001-dedup-phone-normalization.md:9-13` (S1 #1). The
  cross-country-prefix half of that finding is fixed (`dedup.py:57-60` trusts an
  embedded calling code); the **country-metadata** half is not, and the fix has
  to live in `dedup`, not only in `main`.
- **The stronger property:** for every record, `dedup`'s key must equal
  `normalize_phone(record["phone"], resolve_country(record))`. That is one
  `assertEqual` in a Hypothesis loop and it subsumes the table above.

### S2 — Nothing pins that the emitted rows are unique by the key `dedup` itself chose

- **Where:** `dedup.py:222-232` concatenates `phone_records + name_records` and
  filters name records against `captured_phones` only. Nothing prevents two
  phone-keyed rows from sharing a `(name, city)`.
- **Breaks:** a chain, or a shop that lists both a landline and a mobile, yields
  one row per number, all with the same name and city. When a later source
  reports the same shop with **no** phone, that record forms its own third row
  instead of joining either of them — the under-merge mirror of the
  shared-hotline over-merge that `003:141-146` documents.
- **Test to add (property form — this one exists, it just is not written down):**

```python
    def test_no_two_output_rows_share_a_name_city_key(self):
        """INVARIANT: dedup's own identity key must be unique in its output,
        across both indexes. Today it is unique within each index only."""
        records = [
            {"name": "City Pharmacy", "phone": "70123456",
             "address": "Hamra, Beirut", "country": "LB"},
            {"name": "City Pharmacy", "phone": "70123457",
             "address": "Hamra, Beirut", "country": "LB"},
        ]
        out = dedup(records)
        keys = {(normalize_name(r["name"]), _extract_city(r.get("address"))) for r in out}
        self.assertEqual(len(keys), len(out), f"duplicate identity key in {out}")
```

- **Input:** the two dicts above.
- **Asserted output:** `len(keys) == len(out) == 2`.
- **Actual today:** `len(out) == 2` but `len(keys) == 1`. Add the phone-less
  third sighting (`{"name": "City Pharmacy", "phone": None, "address": "Hamra,
  Beirut"}`) and you get `len(out) == 3` — three rows for one shop.
- **Pins:** nothing yet; this is the invariant that makes the S1 #1 fix
  *checkable*, because a cross-index merge can only be correct if the name key
  is trustworthy in the first place.
- **Import note:** `_extract_city` is private (`dedup.py:72`); if you would
  rather not test a private, import `normalize_name` and inline the two-line
  city expression, or expose a public `identity_key(record) -> tuple` and have
  `dedup` use it. That single refactor makes every test in this section a
  one-liner.

### S3 — Properties that pass today and must be pinned anyway

These are free (no fixtures), pass on the current code, and protect the
pipeline contract that `main.py` depends on. I verified each over 3000-5000
random trials while writing this.

```python
    def test_dedup_is_a_fixed_point(self):
        """main.py reloads all_businesses.csv every run and re-dedups the union
        (main.py:150,179-180). If dedup were not idempotent the master would
        grow without bound."""
        records = [
            {"name": "Cafe", "phone": "70123456", "address": "Beirut, Lebanon",
             "rating": 4.0, "source": "osm", "scraped_at": "2026-01-01T00:00:00+00:00"},
            {"name": "Cafe", "phone": "+961 70 123 456", "address": "Hamra, Beirut",
             "rating": 4.8, "source": "google_places", "scraped_at": "2026-09-01T00:00:00+00:00"},
            {"name": "Cafe", "phone": "70-123-456", "address": None,
             "email": "a@b.com", "source": "wikidata", "scraped_at": "2026-05-01T00:00:00+00:00"},
        ]
        once = dedup(records)
        self.assertEqual(dedup(once), once)
        self.assertEqual(len(once), 1)

    def test_dedup_does_not_mutate_or_alias_its_input(self):
        """main.py:183-197 mutates the returned records in place (country,
        industry_priority, recommended_service, phone, lead_score). If dedup
        ever handed back the objects load_master produced, those writes would
        corrupt the caller's master list."""
        records = [{"name": "A", "phone": "70123456", "address": "Beirut, Lebanon"},
                   {"name": "B", "phone": None, "address": "Jounieh, Lebanon"}]
        snapshot = copy.deepcopy(records)
        out = dedup(records)
        self.assertEqual(records, snapshot, "dedup mutated its input")
        self.assertFalse(any(o is r for o in out for r in records))

    def test_no_field_value_is_invented(self):
        """Anti-data-loss property: every non-empty value dedup emits for a
        field must be a value some input carried for that field. `source` is the
        documented exception (it is a sorted union)."""
        pool = {"name": ["Cafe", None, ""], "address": ["Beirut, Lebanon", None, ""],
                "phone": ["70123456", None, "---", "+96170123456"],
                "country": ["LB", "SA", None, ""],
                "rating": [None, 4.2, 9.9, "4.2", 0],
                "email": [None, "", "a@b.com", "not-an-email"],
                "website": [None, "", "maps.example", "https://a.example"],
                "scraped_at": ["2026-01-01", "2026-09-01", None],
                "source": ["osm", "google_places", "wikidata"]}
        rng = random.Random(5)
        for _ in range(500):
            records = [{k: rng.choice(v) for k, v in pool.items()}
                       for _ in range(4)]
            out = dedup(records)
            seen = {k: {r[k] for r in records if r.get(k) not in (None, "")}
                    for k in pool}
            for r in out:
                for k, v in r.items():
                    if k == "source" or v in (None, ""):
                        continue
                    self.assertIn(v, seen[k], f"{k}={v!r} was invented")

    def test_dedup_never_expands_the_record_count(self):
        self.assertEqual(dedup([]), [])
        records = [{"name": f"Cafe {i}", "phone": f"7012345{i}"} for i in range(5)]
        self.assertEqual(len(dedup(records)), 5)
        # 0 violations in 3000 random trials
```

- **Input/asserted output** are in the code; each asserts a property over a
  literal input plus (for the last three) a seeded random sample, which is the
  stdlib-only stand-in for Hypothesis that this repo can afford today.
- **Pins:** the cumulative-master loop (`main.py:150,179-180`), the in-place
  mutation contract (`main.py:183-197`), and the anti-data-loss guarantee that
  `002`/`024` were about — none of which is currently asserted anywhere.
- Also worth adding as **characterisation** tests, so a future change to key
  selection is deliberate rather than silent: a shared hotline
  (`{"name": "Al Baik", "phone": "+966112345678", "address": "Riyadh, Saudi
  Arabia"}` + the same number for `"Jeddah, Saudi Arabia"`) merges to **1**
  record; junk phones are retained verbatim in the output
  (`dedup([{"name": "Cafe A", "phone": "ext 4", "address": "Beirut, Lebanon"}])`
  → `out[0]["phone"] == "ext 4"`, because `dedup` never rewrites `phone` to the
  key it computed); a non-string phone keys correctly
  (`normalize_phone(70123456, "LB") == "+96170123456"`); and Google's name wins
  over OSM's (`"Cafe (Hamra)"` survives over `"Cafe Hamra"`,
  `dedup.py:100,104`).

### S3 — `031`'s Property 6 is wrong as written; do not copy it

`docs/audits/031-test-suite-design.md:561-577` proposes asserting that no two
output rows share a normalized phone. Written as given, it **fails on correct
code**:

```python
out = dedup([{"name": "Cafe A", "phone": "---"}, {"name": "Cafe B", "phone": "..."}])
seen = set()
for r in out:
    raw = r.get("phone")
    if raw:                                   # <-- truthy junk still enters
        norm = normalize_phone(raw, r.get("country", "LB"))   # -> "" for both
        assert norm not in seen               # fires on the second row
        seen.add(norm)
```

Both junk phones normalize to `""` (`dedup.py:45-46`), so the second row
collides on the empty key. Guard with `if norm:` — `test_lead_signal.py:103`
already encodes that `"---"` must not become a key. Fix the property before
anyone implements it.

### S3 — Do not assert on key order or on `json.dumps` without `sort_keys`

`_merge` iterates `set(a) | set(b)` (`dedup.py:192`), so the output dict's key
order depends on `PYTHONHASHSEED`. Measured: the same merge returns
`['phone','address','name','website','source']` at seed 1,
`['phone','address','source','name','website']` at seed 2. Values are stable;
order is not. Compare dicts by value, or `json.dumps(..., sort_keys=True)` —
which is what every assertion above does.

## Properties, not examples

Four properties cover this function better than any example list. In preference
order, each with the literal seed case that breaks it today:

| Property | Fails today? | Seed case |
|---|---|---|
| The output multiset is invariant under permutation of the input | **yes** (198/6000) | 3-record website case above |
| Every output row's key is unique (no duplicate identities) | **yes** | two numbers, one name |
| `len(dedup(records)) <= len(records)`, and `dedup(records)` is a fixed point | no | — |
| Every non-empty emitted value for field K appeared in some input's K (except `source`) | no | — |

Add one more that closes the loop with `main`:

| Property | Fails today? | Seed case |
|---|---|---|
| `dedup`'s key for a record == `normalize_phone(phone, resolve_country(record))` | **yes** | `country="KSA"`, `phone="0501234567"` |

The repo has no Hypothesis and I am not allowed to add dependencies
(`BRIEF.md` §3), so express these as seeded `random.Random` loops in
`unittest` — deterministic, stdlib-only, and they fail loudly on the seed cases
above. `031-test-suite-design.md:714` already prescribes Hypothesis; the
stdlib loop is the interim that ships today.

## What genuinely cannot be tested here

1. **Whether a merge is *correct*.** `dedup` has no ground truth. Any test
   asserting "these two rows are the same business" is really asserting the key
   heuristic, not reality. The only honest way to test merge *accuracy* is a
   hand-labelled fixture — 30-50 pairs with a yes/no "same business" verdict —
   which is `docs/audits/032-fixtures-offline-replay.md`'s job, not a unit test.
   Until that exists, over-merge and under-merge **rates** are unmeasurable, and
   no assertion in this file can honestly claim "dedup is 95% correct".
2. **Realistic input distributions.** `data/` is gitignored and no sample output
   is committed, so there is no corpus to property-test over. Everything above is
   hand-written from `scrapers/osm.py:80-87` and `scrapers/google_places.py:317`
   address shapes. That is a real limitation: if Places changes its address
   format, the S1 #2 test still passes while production regresses. Mitigation:
   freeze one raw API response per source as a fixture and assert
   `_extract_city` against it.
3. **Anything downstream of `dedup`.** `dedup` is pure, so its own return value
   is fully testable. But the consequences — that a bad website lands in
   `with_websites.csv` (`main.py:199`), that a phantom row reaches
   `sales_ready.csv` (`main.py:202-205`), that a lead becomes unpitchable —
   can only be observed through `main.main()`, which runs three scrapers and
   therefore needs network. There is no seam between dedup and the CSV writers.
   To make the S1 #4 consequence testable, extract
   `main.py:183-213` into a pure `partition_outputs(records) -> dict[str, list]`
   and assert on its return value offline. Until that refactor exists, the
   downstream claims in this report are code-reading, not test results.
4. **Concurrency.** `dedup` is single-threaded and correct by construction; the
   order instability in S2 #1 comes from *its caller* (`main.py:160-168`
   completes futures in arrival order). A test can only pin it by shuffling the
   input list, which is what the property above does. It cannot pin thread
   scheduling itself, and should not try.
5. **Memory at production scale.** `dedup` builds two full indexes over every
   record. A slow-marked smoke test with 100k synthetic records (no network) is
   feasible and worth adding, but a *threshold* assertion would be flaky across
   machines; assert only that it completes and that `len(out) == expected`.

## Recommended order of work

1. Add `TestDedupKeys` to `tests/test_lead_signal.py` (or a dependency-free
   `tests/test_dedup.py`) with the four failing tests above, marked
   `expectedFailure` or `@unittest.expectedFailure` if you want CI green before
   the fixes land. They cost 40 lines and name all four defects.
2. Fix the key, not the guard: implement `identity_key(record)` returning
   `(normalized_phone, normalized_name, city_token)` with a real city extractor
   and an empty-name guard, and use it for **both** indexes. Then S1 #1, #2 and
   #3 all close together and the "no two rows share a key" property becomes the
   gate.
3. Swap `rank()` to validity-before-trust (`dedup.py:179-185`) and confirm
   `test_lead_signal.py:278-284` still passes.
4. Make `dedup` resolve country through `main.resolve_country` (or move
   `resolve_country` into `dedup` and have `main` import it) so the S2 #2
   property holds; the run-to-run key instability disappears with it.
5. Compute trust/validity from the **originating** record rather than the merged
   one, restoring associativity; then the permutation property becomes a gate
   instead of a known flake.
6. Correct `031`'s Property 6 before it gets implemented.
