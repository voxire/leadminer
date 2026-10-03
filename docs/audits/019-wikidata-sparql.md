# 019 — Wikidata SPARQL coverage and result integrity

## Verdict

The `LIMIT 5000` applies to solution rows, not unique businesses, and the query has neither a deterministic ordering nor pagination; it can silently return an unstable, partial sample. More immediately, the scraper reads label-service aliases for literal-valued contact/address properties instead of their actual variables, so those values are likely lost. Even after that fix, the query covers only five of the nine field dimensions emitted by OSM and omits useful business facts such as employee count, inception, headquarters, industry, and coordinates.

## Findings

### S1 — Reads labels instead of property values

- **Where:** `scrapers/wikidata.py:11-16,22`; `scrapers/wikidata.py:66-70`
- **Breaks:** The query binds the property values as `?website`, `?phone`, `?email`, and `?address`, but selects and the Python code reads `?websiteLabel`, `?phoneLabel`, `?emailLabel`, and `?addressLabel`. The label service is for labels of entities; these properties carry URL, phone, email, and address values, not entity labels. Consequently those `*Label` bindings are unbound in normal results, and the emitted `website`, `phone`, `email`, and `address` will be `None` even when the Wikidata item has the statements. `?categoryLabel` is different: it is explicitly bound by the `rdfs:label` pattern at lines 18-20.
- **Trigger:** A Lebanon item with P856 and P1329 statements; the SPARQL result has `website`/`phone` values but no corresponding labels, and Python reads only the missing label keys.
- **Fix:** Read the value bindings (`row.get("website", {}).get("value")`, etc.) for P856/P1329/P968/P6375; retain label-service variables only for entity-valued fields such as item/category labels.

### S2 — LIMIT can truncate rows, and the selected sample is not repeatable

- **Where:** `scrapers/wikidata.py:11-25`
- **Breaks:** `LIMIT 5000` caps solution mappings, not distinct `?item` values. It is reachable whenever the query produces at least 5,000 mappings. The query has no business-type constraint (`P17` is its only required pattern), and its independent `OPTIONAL` patterns can multiply rows when an item has multiple websites, phone numbers, emails, addresses, or P31 categories. Thus the limit can be consumed by duplicates and leave later items out. Static inspection cannot establish whether the current endpoint snapshot returns 5,000 rows, but the cap is not a guaranteed 5,000-business limit.
- **Trigger:** One item with two P856 values and three P31 values already contributes multiple mappings; across the broad Lebanon P17 population, mappings can reach the cap before 5,000 distinct items.
- **Fix:** Order and page by item URI (prefer keyset pagination), and aggregate/select multi-valued properties so one business cannot consume many page slots. Record whether a page is truncated rather than silently treating it as a complete scrape.

- **Determinism:** There is no `ORDER BY` before `LIMIT` (`scrapers/wikidata.py:25`). SPARQL does not promise a stable order for unordered solutions, so when the limit is reached, which mappings/items are included can vary with endpoint execution or data changes. Retrying the same query is not a reproducible continuation strategy.

### S2 — Wikidata's current field set leaves a measurable OSM coverage gap

- **Where:** `scrapers/wikidata.py:11-21`; compare `scrapers/osm.py:64-90`
- **Breaks:** OSM emits nine comparable business-data dimensions: category, address, latitude, longitude, phone, email, website, Facebook, and Instagram. Wikidata queries only five: category (P31), address (P6375), phone (P1329), email (P968), and official website (P856). It therefore omits four of the nine OSM dimensions (lat/lon and Facebook/Instagram): a **44.4% structural field gap** versus OSM. This is a schema/query comparison, not a claim that OSM has values for every business. Given the label-variable issue above, only category is reliably mapped from these five in the current implementation; as written, up to **8/9 (88.9%)** of the OSM field dimensions are not populated from the Wikidata result path.
- **Missing business properties:** Add P1128 (employees), P571 (inception), P159 (headquarters location), P452 (industry), and P625 (coordinate location). For website-like variants, P856 (official website) is already queried, but the query does not handle a separate P1581 (official blog URL) or deliberately preserve multiple P856 values. These would supplement—not map one-to-one to—OSM's `website`, `contact:website`, and `url` tag fallbacks (`scrapers/osm.py:88`). P31 category is not equivalent to a normalized business-industry field.
- **Fix:** Expand the query with the missing properties and map P625 coordinates into `lat`/`lon`; represent additional URLs explicitly or normalize them under a documented precedence rule. Keep optional enrichments from multiplying the main item result set.

## Not a bug, but worth knowing

- P1329 is the correct Wikidata property for a phone number. Wikidata labels it **phone number** and defines the value as a telephone number in RFC 3966 format without the `tel:` prefix ([P1329](https://www.wikidata.org/wiki/Property:P1329)). “Telephone” is the concept in the definition, not a different property that should replace P1329; `?phone` is merely this query's variable name. OSM's `phone` / `contact:phone` tags are the analogous source fields.
- P856 is already present and means official website ([P856](https://www.wikidata.org/wiki/Property:P856)); it is the additional URL variants and multiple-value handling—not P856 itself—that are absent.
- Property references: [P1128 employees](https://www.wikidata.org/wiki/Property:P1128), [P571 inception](https://www.wikidata.org/wiki/Property:P571), [P159 headquarters location](https://www.wikidata.org/wiki/Property:P159), [P452 industry](https://www.wikidata.org/wiki/Property:P452), [P1581 official blog URL](https://www.wikidata.org/wiki/Property:P1581), and [P625 coordinate location](https://www.wikidata.org/wiki/Property:P625).

## Recommended order of work

1. Fix value-vs-label bindings and add a small fixture/assertion that verifies populated P856/P1329/P968/P6375 values survive conversion.
2. Make results reproducible and complete enough for the intended workload: deterministic item ordering, pagination, and protection against optional-property fan-out.
3. Add the high-value missing facts (especially coordinates, headquarters, industry, and employee count), and define how multiple website URLs are represented.
