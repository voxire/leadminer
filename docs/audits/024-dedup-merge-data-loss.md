# 024 — Dedup merge can discard conflicting field values

## Verdict

The suspected whole-record comparison is real, and it silently drops one of two different, non-null values for any overlapping field; which value survives depends on the records’ total completeness, not on that field. However, the specific complementary case “OSM phone + Google Places rating” does **not** lose the rating: OSM emits `rating=None`, and `_merge()` takes Google’s non-null rating before it reaches the completeness comparison. Missing keys and explicit `None` values are also treated alike for both counting and value selection, with a minor output-shape normalization described below.

## Findings

### S1 — Unrelated populated fields decide which conflicting value survives

- **Where:** `dedup.py:41-42`, `dedup.py:45-60`
- **Breaks:** For each key where both records have non-null values, line 60 chooses that key’s value using `_field_count(a)` versus `_field_count(b)`. Those counts include every non-null value in each whole record, unrelated to the key under consideration. Thus a record with extra social/contact/location fields can win a conflict in `category`, `address`, `website`, or another field without that field being more trustworthy. The losing non-null value is discarded; there is no per-field provenance or conflict record. Equal counts favor `a` because the comparison uses `>=`.
- **Trigger:** For example, let `a` contain `category="OSM category"`, `phone="03..."`, `website="https://a.example"`, `facebook="https://facebook.example/a"`, and `source="osm"`; let `b` contain `category="Google category"`, a phone that normalizes to the same key, `rating=4.6`, and `source="google_places"`. Include `scraped_at` on both. `a` has more non-null fields, so the merged `category` is `"OSM category"` even though the extra fields that made `a` win say nothing about which category is right. The Google category is silently lost. The unique Google rating survives (see trace below).
- **Fix:** Choose winners per field using explicit field/source precedence (or preserve conflicting values and their provenance); do not use whole-record non-null counts as a proxy for field quality.

### S2 — Missing keys and explicit `None` do not differ for merge decisions

- **Where:** `dedup.py:41-42`, `dedup.py:46-53`
- **Breaks:** `_field_count()` excludes explicit `None`; a missing key contributes no value either. In the merge loop, `a.get(key)` / `b.get(key)` return `None` for both a missing key and an explicit `None`. So the two cases are equivalent for winner selection: a non-null value on either side is retained, and if both are absent/null the result for a key in the union is `None`. The only shape difference is that `all_keys` causes a key explicitly present as `None` on either side to appear in the output as `None`; a key absent from both sides remains absent. This can matter to consumers that distinguish key absence from null, but the scraper `BusinessRecord` shape declares its data fields and the scrapers populate them with `None` when unavailable (`scrapers/base.py:5-28`, `scrapers/osm.py:95-119`, `scrapers/google_places.py:249-273`).
- **Trigger:** `_merge({"name": "A"}, {"rating": None})` returns both keys, with `rating: None`; `_merge({"name": "A"}, {})` returns only `name`. Neither explicit null nor absence increases either record’s field count.
- **Fix:** If absent and null carry distinct semantics for downstream consumers, preserve key presence explicitly; otherwise document/normalize to the current union-of-keys behavior.

## Not a bug, but worth knowing

- **OSM phone + Google rating is preserved when the records actually merge.** OSM sets `phone` from `phone` / `contact:phone` and always sets `rating=None` (`scrapers/osm.py:86, 95-119`). Google sets `phone` from `nationalPhoneNumber` and `rating` from `place.get("rating")` (`scrapers/google_places.py:249-273`). To reach `_merge()` through the phone index, both records must have phones that normalize to the same value (`dedup.py:68-75`); if Google has no phone, it goes into the separate name index rather than merging directly with the OSM phone-keyed record (`dedup.py:76-83, 90-95`). With matching phones, for `rating`, `av` is `None` and `bv` is Google’s rating, so line 51 assigns `bv` without consulting `_field_count`. For `phone`, both sides are non-null, so line 60 picks the raw phone string from whichever whole record has the larger count (or `a` on a tie). The unique rating is not lost; conflicting non-null fields remain the data-loss case.
- `source` is explicitly unioned and `scraped_at` explicitly takes the maximum (`dedup.py:54-58`), but this does not retain per-field source attribution for other conflicts.

## Recommended order of work

1. Replace the record-wide count comparison with explicit per-field/source precedence or a conflict-preserving representation; add cases for a conflicting field plus unrelated extra fields and for complementary OSM/Google fields.
2. Decide whether missing and explicit `None` should remain equivalent; test/document the chosen output-shape behavior.
