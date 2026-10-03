# 035 — Delta scraping: per-record content hash, changed-since-last-seen, and per-field TTLs

## Verdict

The linear cost is real but it is **almost entirely in one line of code**: every run
re-loads the whole master (`main.py:95,124`), then `check_websites` re-fetches every
record that has a website, unconditionally (`enricher.py:182`). Scraping itself is
**not** the problem — OSM is one POST, Wikidata one SPARQL `LIMIT 5000`, Google 67
queries, all of fixed size per run regardless of history. The fix is to add a stable
per-record identity plus a content hash and per-field TTLs, and to switch the website
pass from "GET every site every run" to a three-tier path: *no request* (unchanged and
within TTL), *conditional GET* (unchanged but TTL-expired, expecting `304`), *full GET*
(new/changed only). The exact schema additions and algorithm are specified below; the
three blockers in the current code that must be closed first are in **Findings**.

## Scope: what grows linearly, and what does not

Reading the data flow (`main.py:93-153`), the per-run work splits into a fixed part and
a history-proportional part:

| Stage | Cost driver | Grows with history? |
|---|---|---|
| OSM Overpass | one POST, `osm.py:38-44` | No (full sweep, fixed size) |
| Wikidata | one SPARQL, `wikidata.py:40-49` | No (`LIMIT 5000`, `wikidata.py:25`) |
| Google Places | 67 queries, `google_places.py:45-124` | No (fixed query count) |
| Whitelist + dedup | `whitelist.py:105`, `dedup.py:64` over `raw + master` | Yes, but CPU/cheap |
| **Website enrichment** | **`check_websites`, `enricher.py:180-220`** | **Yes — this is the linear cost** |
| Region inference + scoring | `enricher.py:270-290` | Yes, but CPU/cheap |

The only network cost that scales with accumulated history is `check_websites`: it builds
`targets` from *every* record with a website (`enricher.py:182`) and does one GET each
(`enricher.py:146`), every run, for records that were already enriched last run. This is
the ~200k-fetch / ~250-CI-minute / ~$161-proxy line item at 500k records modeled in
`073-cost-model.md` ("No database means recompute cost every run", §Not-a-bug). Region
inference is already half-way to delta — it only fills empty regions
(`enricher.py:271` `if not r.get("region")`) — but the website fetch has no equivalent guard.

The design below makes the website pass cost ∝ (new + changed + TTL-expired), not ∝ master size.

---

## Design spec

### 1. Schema additions (exact)

Append these columns to `FIELDS` (`main.py:32-39`) and to `BusinessRecord`
(`scrapers/base.py:5-28`). All round-trip as CSV strings; timestamps are ISO-8601 UTC.

| Column | Type | Nullable | Meaning | Written by |
|---|---|---|---|---|
| `record_id` | str, 64-hex | no | `sha256(identity_key)` — stable cross-run surrogate | `main.py` (post-dedup) |
| `source_id` | str | yes | provider-scoped external id, pipe-joined like `source` (`osm:node/123`, `wikidata:Q123`, `google:ChIJ…`) | scrapers |
| `record_hash` | str, 64-hex | no | `sha256` of enrichment-relevant *source* fields (§2) | `main.py` (post-dedup) |
| `website_etag` | str | yes | `ETag` response header from last fetch | `enricher.py` |
| `website_last_modified` | str | yes | `Last-Modified` response header from last fetch | `enricher.py` |
| `website_body_hash` | str, 64-hex | yes | `sha256(response body)` from last successful fetch | `enricher.py` |
| `website_checked_at` | str ISO-8601 | yes | last website fetch (incl. `304`) — clock for liveness + contacts | `enricher.py` |
| `enriched_at` | str ISO-8601 | yes | last region/scores/contacts computation | `enricher.py` |
| `first_seen_at` | str ISO-8601 | yes | first time the record entered the master | `main.py` |

`load_master` (`main.py:46-71`) needs two additions: parse the three new timestamps back
from strings (same pattern as the existing `scraped_at` handling), and keep the six
hash/id columns as raw strings. `write_csv` already drops unknown keys via
`extrasaction="ignore"` (`main.py:76`), so old rows without the new columns load as `None`
and are treated as **new** (safe default: they get re-enriched once).

### 2. Per-record content hash

`record_hash` is computed **only from source fields** — the fields the scrapers populate
directly, before any enrichment. It must *exclude* every enrichment-derived field
(`email`-from-website, `website_live`, `region`, `completeness_score`, `lead_score`,
`recommended_service`, `whatsapp`, `linkedin`, and note `rating`/`review_count` too — see
below). Otherwise the hash would incorporate enrichment output and change whenever
enrichment changes, defeating the purpose.

```python
import hashlib
from dedup import normalize_phone, normalize_name

def _canon_url(u: str | None) -> str:
    if not u:
        return ""
    u = u.strip().lower().split("#")[0]     # lowercase, drop fragment
    return u.rstrip("/")

def _fmt_num(x) -> str:
    if x is None:
        return ""
    return f"{x:.6f}" if isinstance(x, float) else str(x)

SOURCE_FIELDS = (
    "name", "category", "country", "address", "lat", "lon",
    "phone", "website", "facebook", "instagram", "source",
)

def record_hash(rec: dict) -> str:
    parts = [
        normalize_name(rec.get("name") or ""),
        (rec.get("category") or "").strip().lower(),
        rec.get("country") or "",
        (rec.get("address") or "").strip().lower(),
        _fmt_num(rec.get("lat")), _fmt_num(rec.get("lon")),
        normalize_phone(rec["phone"], rec.get("country", "LB")) if rec.get("phone") else "",
        _canon_url(rec.get("website")),
        (rec.get("facebook") or "").strip().lower(),
        (rec.get("instagram") or "").strip().lower(),
        rec.get("source") or "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
```

**Why `rating`/`review_count` are excluded:** they are source-freshness fields refreshed
by the Google re-sweep, not by website enrichment. A rating change must update `lead_score`
(`enricher.py:253`, local/cheap) but must **not** trigger a website re-fetch. Putting them
in the hash would cause a pointless GET every time a review count ticks.

**Why `website` is included:** a URL change invalidates the stored `website_etag` /
`website_body_hash`, so a changed URL must force a full re-fetch. Including it makes the
hash subsume "URL changed" automatically.

**Stable identity** — `record_id` reuses the *existing* dedup key so it stays consistent
with `dedup.py:69-83` (phone-first, else `(normalized_name, city)`):

```python
def identity_key(rec: dict) -> str:
    if rec.get("phone"):
        return "ph:" + normalize_phone(rec["phone"], rec.get("country", "LB"))
    return "nm:" + normalize_name(rec.get("name", "")) + "|" + rec.get("address", "").split(",")[-1].strip().lower()

record_id = hashlib.sha256(identity_key(rec).encode("utf-8")).hexdigest()
```

### 3. Changed-since-last-seen algorithm

Replace the unconditional `combined = raw_filtered + master; records = dedup(combined);
records = enrich(records)` (`main.py:124-129`) with a classified pipeline:

```python
master_by_id = {r["record_id"]: r for r in master if r.get("record_id")}

def classify(rec: dict, old: dict | None) -> str:
    if old is None:
        return "NEW"
    if record_hash(rec) != old.get("record_hash"):
        return "CHANGED"
    return "UNCHANGED"

for rec in fresh_records:                    # after whitelist filter + dedup
    rid = hashlib.sha256(identity_key(rec).encode("utf-8")).hexdigest()
    old = master_by_id.get(rid)
    rec["record_id"] = rid
    rec["record_hash"] = record_hash(rec)    # recomputed post-merge

    state = classify(rec, old)
    if state == "NEW":
        rec.setdefault("first_seen_at", now)
        full_enrich(rec)                     # fetch website, extract, infer, score
    elif state == "CHANGED":
        carry_enrichment(rec, old)           # seed with old derived values
        rec["first_seen_at"] = old.get("first_seen_at")
        full_enrich(rec)                     # inputs changed -> re-derive + refetch
    else:  # UNCHANGED
        carry_enrichment(rec, old)           # region/scores/contacts/hashes/ts
        rec["first_seen_at"] = old.get("first_seen_at")
        ttl_refresh(rec, old)                # network only for stale fields
```

`carry_enrichment` copies the enrichment-managed columns from `old` verbatim:
`email, website_live, whatsapp, linkedin, region, completeness_score, lead_score,
recommended_service, website_etag, website_last_modified, website_body_hash,
website_checked_at, enriched_at`. It does **not** copy source columns — those come from
`rec` (the fresh scrape). This is the direction-aware rule that replaces `_merge`'s
bidirectional `_field_count` tiebreak (see S2).

### 4. Skip re-enrichment when a website is unchanged

`full_enrich` and `ttl_refresh` both funnel website work through one conditional fetcher,
replacing `_fetch_website` (`enricher.py:142-177`). Three tiers:

```python
def fetch_website(url: str, old: dict) -> tuple[bool, dict]:
    """Returns (live, contacts). Skips body+parse when unchanged."""
    headers = {}
    if old.get("website_etag"):
        headers["If-None-Match"] = old["website_etag"]
    elif old.get("website_last_modified"):
        headers["If-Modified-Since"] = old["website_last_modified"]

    try:
        r = _SESSION.get(url, timeout=8, allow_redirects=True, verify=False, headers=headers)
    except Exception:
        return None, {}                      # network failure -> leave stale, don't mark dead

    if r.status_code == 304:                 # server says unchanged -> live, zero body
        return True, {"etag": old.get("website_etag"),
                      "last_modified": old.get("website_last_modified"),
                      "body_hash": old.get("website_body_hash")}
    if r.status_code >= 400:
        return False, {}
    body = r.text[:200_000]
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if h == old.get("website_body_hash"):    # 200 but byte-identical -> skip re-extract
        return True, {"etag": r.headers.get("ETag") or old.get("website_etag"),
                      "last_modified": r.headers.get("Last-Modified") or old.get("website_last_modified"),
                      "body_hash": h}
    contacts = extract_contacts(body)        # existing regex block, enricher.py:154-175
    return True, {**contacts,
                  "etag": r.headers.get("ETag"),
                  "last_modified": r.headers.get("Last-Modified"),
                  "body_hash": h}
```

Then in `ttl_refresh`:

```python
def ttl_refresh(rec: dict, old: dict) -> None:
    stale = {f for f, ttl in FIELD_TTL.items()
             if ttl_expired(old, f, ttl)}
    website_fields = {"website_live", "email", "instagram", "whatsapp", "linkedin"}
    if not (stale & website_fields):
        return                                # tier 1: NO network at all
    live, res = fetch_website(rec["website"], old)
    rec["website_checked_at"] = now
    if live is None:
        return                                # transient failure, keep prior values
    rec["website_live"] = live
    if "body_hash" in res:
        rec["website_body_hash"] = res["body_hash"]
    if res.get("etag"):        rec["website_etag"] = res["etag"]
    if res.get("last_modified"): rec["website_last_modified"] = res["last_modified"]
    for f in ("email", "instagram", "whatsapp", "linkedin"):
        if res.get(f) and not rec.get(f):     # preserve existing precedence
            rec[f] = res[f]
    rec["enriched_at"] = now
```

The three tiers, in order of preference:

1. **No request** — record UNCHANGED and no website-derived field is TTL-expired.
   Dominant case at short cadence; zero network, zero bandwidth.
2. **Conditional GET** — UNCHANGED but TTL-expired. Sends `If-None-Match` /
   `If-Modified-Since`; a `304` transfers ~zero body and skips parsing. Still one request,
   but ~100 ms and no bandwidth.
3. **Full GET + parse** — NEW or CHANGED, or a `200` whose `website_body_hash` differs from
   the stored one. Only here do the regex extractors (`enricher.py:154-175`) run.

Two correctness notes that differ from today's code:

- **Do not mark transient network failures as dead.** Today `except Exception: return False`
  (`enricher.py:151-152`) records a blocked/timeout site as `website_live=False` and then
  `lead_score` rewards it with +20 (`enricher.py:243-244`). In delta mode the fetcher must
  return `live=None` and leave the prior value untouched, otherwise every transient block
  flips a live lead to "dead" and inflates its score (cross-ref `005-enricher-http-robustness.md`).
- **304 means live.** A `304 Not Modified` is proof the server is reachable and the content
  is what we last saw, so `website_live` is affirmed `True` without parsing.

### 5. Per-field TTL

One clock per group of fields, expressed as a code-level dict (TTL is a policy constant, not
a stored column — storing nine `*_checked_at` timestamps buys nothing here because liveness
and contacts are always fetched together):

```python
WEBSITE_CLOCK_FIELDS = {"website_live", "email", "instagram", "whatsapp", "linkedin"}

FIELD_TTL = {           # seconds; clock = website_checked_at unless noted
    "website_live": 7 * 86400,          # 7d  — liveness flips fast; drives lead_score +20
    "email":        30 * 86400,         # 30d — contact info is semi-stable
    "instagram":    30 * 86400,
    "whatsapp":     30 * 86400,
    "linkedin":     30 * 86400,
    # rating / review_count: clock = the Google re-sweep itself, not enrichment.
    # They refresh every run (full sweep); TTL here only flags staleness if a sweep is missed.
    "rating":        7 * 86400,
    "review_count":  7 * 86400,
}

def ttl_expired(old: dict, field: str, ttl: int) -> bool:
    clock = old.get("website_checked_at") if field in WEBSITE_CLOCK_FIELDS else old.get("scraped_at")
    if not clock:
        return True
    return (now - parse_ts(clock)) > ttl
```

`region` has no TTL: it is re-inferred only when `record_hash` changes (address/coords moved),
matching the existing `if not r.get("region")` guard (`enricher.py:271`). `completeness_score`,
`lead_score`, `industry_priority`, `recommended_service` are recomputed every run regardless —
they are local and free (`enricher.py:289-290`, `main.py:132-137`), so they need no TTL.

**Cadence interaction:** with a monthly run, `website_live` (7d TTL) and the contacts (30d
TTL) are *always* expired, so every website still gets one request — but tier-2 conditional
GETs, of which the large majority are `304`/byte-identical. Bandwidth and parse cost collapse
even though request count stays ~1/site/month. To also cut request count, widen the TTLs
(e.g. contacts 90d) or run weekly and let tier 1 absorb most records. The invariant to state
to stakeholders: **delta scraping converts cost from "∝ master size" to "∝ change rate +
TTL-expiry rate", and shrinks each retained request from a full 200 KB GET to a ~300-byte
conditional round-trip for unchanged sites.**

---

## Findings

### S1 — No stable source identity is captured, so delta scraping has nothing to key on

- **Where:** `scrapers/base.py:5-28` (no `source_id` field); `scrapers/osm.py:58-95`
  (element `id`/`type` discarded); `scrapers/google_places.py:228-234` (`place.id` used only
  for in-run `seen_ids` de-dupe, then thrown away); `scrapers/wikidata.py:61` (`?item` QID
  never stored).
- **Breaks:** Delta scraping needs a *stable* key that survives a business changing its name
  or phone. The only key the system has is the dedup key — normalized phone, else
  `(normalized_name, city)` (`dedup.py:69-83`) — which is derived from *mutable* attributes.
  If a business changes its phone number, its `record_id` changes, and the next run treats it
  as a brand-new record: it gets re-enriched, and worse, a naive merge leaves a stale
  duplicate of the old phone behind. Without a provider-scoped external id, "changed since
  last seen" cannot be computed reliably at the source level.
- **Trigger:** A single Google Places business that lists a new phone number between runs.
- **Fix:** Add `source_id` to `BusinessRecord` and capture it in each scraper —
  `osm.py` `f"{el['type']}/{el['id']}"`, `wikidata.py` the `?item` URI, `google_places.py`
  the existing `place_id`. Join it pipe-delimited in `_merge` alongside `source`
  (`dedup.py:54-56`). `record_id` can stay phone/name-based for v1 (consistent with today's
  dedup), but `source_id` is what makes the identity durable and unlocks per-source change
  detection later.

### S2 — `_merge` is direction-agnostic and will clobber stored enrichment across runs

- **Where:** `dedup.py:45-61`, specifically `merged[key] = av if _field_count(a) >= _field_count(b) else bv` (`dedup.py:60`).
- **Breaks:** `_merge` decides per-field by which record has *more non-None fields overall* —
  a bidrectional tiebreak with no notion of "source value" vs "enrichment value." A freshly
  scraped OSM record carrying `email`/`instagram` from tags (`osm.py:87,90`) can overwrite a
  richer website-extracted email stored in the master, or vice-versa, on every merge. Under
  delta scraping this is silent cross-run data churn: enrichment work from the previous run
  is discarded whenever the source's field-count happens to win the tiebreak.
- **Trigger:** A record that exists in both OSM (5 tags filled) and Google (3 fields filled,
  but with the extracted `email` and `website_live`). The `_field_count` comparison flips
  depending on which record is `a` vs `b` in the merge order.
- **Fix:** Replace `_merge`'s use in the delta path with the direction-aware `carry_enrichment`
  rule from §3 — source columns come from the fresh scrape, enrichment columns (`email`,
  `website_live`, `whatsapp`, `linkedin`, `region`, scores, hashes, timestamps) come from the
  master unless re-enriched. Keep `_merge` only for merging *within* a single run's raw scrape,
  not across runs.

### S3 — `check_websites` has no conditional-request or body-hash fast path

- **Where:** `enricher.py:180-220` (unconditional GET per site at `enricher.py:146`),
  `enricher.py:150` (`r.text[:200_000]` every time).
- **Breaks:** Even after adding identity + hashes + TTLs, the savings only materialize if the
  fetcher can say "unchanged, skip the body." Today it always downloads and parses 200 KB per
  site; there is no `If-None-Match`/`If-Modified-Since` and no `ETag`/`Last-Modified` capture,
  so the 200k-fetch / ~$161-proxy / ~250-CI-minute cost at 500k records (`073-cost-model.md`)
  is structurally unavoidable. This is the single change that makes delta scraping worth doing.
- **Trigger:** A monthly run against a 100k-record master: ~40k full website GETs, ~95% of
  which return content byte-identical to last month.
- **Fix:** Implement `fetch_website` from §4: store `ETag`/`Last-Modified`/`website_body_hash`,
  send conditional headers, treat `304` as live-without-parse, and skip re-extraction on a
  byte-identical `200`.

---

## Not a bug, but worth knowing

- **The three source APIs have unequal delta support, so "delta scrape" is mostly a
  fiction on the source side.** Overpass supports diff queries via `[adiff:"from","to"]`
  (Attic data) — a real "changed since" for OSM. Wikidata SPARQL can filter
  `schema:dateModified > xsd:dateTime(...)`. Google Places Text Search (New) offers
  **no** "updated since" filter; its cost is query-count-driven and already fixed per run.
  Conclusion: pursue source-side delta for OSM/Wikidata opportunistically, but the design
  above assumes (correctly) that the scrapers still run full sweeps and the delta logic sits
  downstream of them, at the merge/enrich boundary.
- **`record_hash` over merged source fields can false-positive "changed".** If a business is
  seen by OSM + Google this run but only Google next run (coverage flapping), the merged
  record's `SOURCE_FIELDS` differ, so `record_hash` changes and triggers a spurious
  re-enrichment. It is bounded (idempotent, one extra GET) and acceptable for v1. The correct
  fix is a per-source `source_hash` keyed by `source_id` (pipe-joined like `source`), comparing
  each source's contribution independently — schedule it after S1 lands `source_id`.
- **`scraped_at` already carries a precision edge case** (`013-main-state-idempotency.md` S3)
  that delta scraping makes more load-bearing, since `scraped_at` becomes the rating/review
  TTL clock (§5). Normalize it to a canonical fixed-width UTC form when adding the new
  timestamp columns.
- **The source columns chosen for `record_hash` must stay in sync with the scrapers.** If a
  new scraper starts populating a field (e.g. a future `yelp` source adding `review_count`),
  that field must either be added to `SOURCE_FIELDS` (if it gates enrichment) or explicitly
  excluded (if it only feeds `lead_score`). Add a comment next to `SOURCE_FIELDS` to this effect.

## Recommended order of work

1. **S1 → S3 together (they are one unit).** Add `source_id` to `BusinessRecord` + the three
   scrapers, then add the nine schema columns to `FIELDS` and implement `fetch_website` with
   conditional GET + body hash. Without S3's fast path, S1 buys nothing; without S1's identity,
   S3 has nothing to key on.
2. **S2 — make the cross-run merge direction-aware.** Route `combined = raw_filtered + master`
   through the classified pipeline (§3) instead of `dedup(combined)`; keep `_merge` for
   intra-run raw merging only.
3. **Add the TTL table and `ttl_refresh`** (§5), with the `live=None` transient-failure
   handling so blocked sites stop being scored as "dead".
4. **Normalize `scraped_at`** to a fixed-width UTC form before it becomes a TTL clock.
5. **Follow up (later):** per-source `source_hash` for OSM/Wikidata diff queries; only after
   `source_id` is populated and stable.
