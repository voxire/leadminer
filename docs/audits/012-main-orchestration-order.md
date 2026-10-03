# 012 — main.py order of operations: `lead_score` runs before `industry_priority` exists, and phone normalization is redundant

## Verdict

The `lead_score` column in the final CSVs is **correct only by accident**: `enrich()` computes it once before `industry_priority` exists, then `main()` recomputes it again after. The first computation is silently wrong (it drops the priority component for every freshly-scraped row and reads last-run's value for master rows) and is dead work that shadows the correct field name. Phone dedup, by contrast, is **not** broken: `dedup()` normalizes phones internally before comparing, so the `normalize_phone` call in `main()` is a second, redundant pass over the output field — not the comparison.

## Findings

### S2 — `lead_score` is computed twice; the first copy reads `industry_priority` before it exists

- **Where:** `enricher.py:290` (first computation, inside `enrich`) vs `main.py:137` (second computation); `industry_priority` is assigned at `main.py:132`, which runs *after* `enrich()` is called at `main.py:129`.
- **Breaks:** `lead_score()` reads `record.get("industry_priority")` at `enricher.py:246` and awards `+15`/`+8` for `high`/`medium` (`enricher.py:247-250`). At the moment `enrich()` calls it, `industry_priority` has not been computed for this run:
  - **Freshly-scraped rows** carry `industry_priority=None` (set explicitly at `osm.py:113`, `wikidata.py:93`, `google_places.py:267`). So `record.get(...)` returns `None`, both `if` branches are skipped, and the priority component contributes **0** — every fresh row is scored 0–15 points too low in the `enrich()` copy.
  - **Master-loaded rows** carry last run's *string* value (`load_master` at `main.py:46-71` only re-casts float/int/`website_live`, never `industry_priority`), so `lead_score` reads a **stale** `"high"`/`"medium"`/`"low"` instead of nothing. The two halves of the deduped set are therefore scored under *different* rules inside `enrich()`.
  - Either way the value is wrong or stale, and is immediately discarded: `main.py:132` sets the correct `industry_priority`, then `main.py:137` recomputes `lead_score` correctly. The final output happens to be right only because of this second call.
- **Trigger:** any fresh record whose category is in `PRIORITY_INDUSTRIES` (e.g. `"restaurant"`). During `enrich()`, its `lead_score` is 15 points lower than it should be; the correct value only appears after `main()`'s loop runs.
- **Fix:** delete the `r["lead_score"] = lead_score(r)` line at `enricher.py:290` (keep `completeness_score` at `enricher.py:289`, which *is* consumed by `recommend_service`). Leave the single authoritative computation at `main.py:137`, after `industry_priority` is assigned.

### S3 — Phone is normalized twice; dedup already normalizes, so `main.py`'s pass is redundant (not a comparison fix)

- **Where:** `dedup.py:71` (and `dedup.py:93`) vs `main.py:134-136`.
- **Breaks:** Nothing in the current code — this is the answer to the brief's question. `dedup()` does **not** compare raw phone strings. It calls `normalize_phone(raw_phone, record.get("country", "LB"))` at `dedup.py:71` to build its index key, so `"01 234 567"`, `"+961 1 234 567"`, and `"9611234567"` all collapse to the same key and merge correctly. The record *stored* in the index, however, keeps the raw phone (`phone_index[key] = dict(record)`, `dedup.py:75`), so the raw string survives dedup and `enrich()` untouched and is only normalized at the very end (`main.py:136`). That final call is a second normalization of the same field with the same function and the same `"LB"` default, so it is consistent — but redundant.
- **Trigger:** two scrapers returning the same Lebanese business with different phone formats (e.g. OSM `"+961 1 234 567"` and Google `"01 234 567"`). They dedup into one row (correctly), and that row's `phone` is normalized at `main.py:136`. No divergence occurs today.
- **Fix:** normalize once, at the source of truth. Normalize `record["phone"]` inside `dedup()` as it builds the key (store the normalized value on the record) and drop the `main.py:134-136` block; or keep the `main.py` pass but stop treating it as load-bearing. The risk is future drift: if the country default ever changes in one place and not the other, dedup keys will silently disagree with the stored `phone` column.

## Not a bug, but worth knowing

- **`recommend_service` runs before phone normalization** (`main.py:133` before `main.py:136`). This is fine: `pitch_recommender.py:35` only checks `bool(record.get("phone"))` (truthiness), which raw vs. normalized does not change. Flagging only because it *looks* like an ordering bug at a glance.
- **`lead_score`'s own phone signal is order-insensitive.** `enricher.py:229` strips non-digits before counting, so `"01 234 567"` (→ `01234567`) and `"+9611234567"` (→ `9611234567`) both satisfy `len(phone) >= 7` identically. Normalizing the phone before or after `lead_score` has no effect on its phone component.
- **`sales_ready` is correctly ordered.** It reads `industry_priority in ("high", "medium")` at `main.py:144`, which runs after the loop that assigns `industry_priority` at `main.py:132` — so it filters on the freshly-computed value, not the stale/None one.

## Recommended order of work

1. Remove `enricher.py:290` (the in-`enrich` `lead_score`) — eliminate the wrong-first/correct-second double computation and make `main.py:137` the single source of truth.
2. Move phone normalization into `dedup()` (normalize as it keys) and remove `main.py:134-136`, so the dedup key and the stored `phone` column can never drift apart.
3. (Optional, clarity) Add a one-line comment at `main.py:131-137` stating the ordering contract: `industry_priority` → `recommend_service` → `lead_score` must run in that sequence because `lead_score` reads `industry_priority`.
