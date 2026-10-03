# 007 — Region inference trusts overlapping rectangles

## Verdict

`infer_region()` uses first-match bounding boxes, not governorate boundaries. Several overlaps deterministically assign a point to the earlier box, including clear wrong results for Hermel, Hazmieh, Jbeil, and Nabatieh; Saudi coverage is only five boxes and address text is never consulted. Treat inferred regions as unreliable for coordinate-only records until the areas are modeled without ambiguous overlaps and the unhandled geography is covered.

## Findings

### S1 — Lebanon coordinate overlaps silently assign the wrong governorate

- **Where:** `enricher.py:58-68, 89-94`; the order at lines 61-67 is the effective tie-breaker.
- **Breaks:** Every box is an inclusive rectangle, and the loop returns the first hit. The intersections below are therefore captured by the listed earlier region instead of resolved against an actual boundary:

  | Intersecting boxes | Intersection (lat × lon) | First-match result |
  |---|---|---|
  | Beirut / Mount Lebanon | Beirut's entire 33.845–33.920 × 35.462–35.545 box | Beirut |
  | Akkar / Baalbek-Hermel | 34.380–34.720 × 36.100–36.650 | Akkar |
  | Akkar / North Lebanon | 34.380–34.680 × 35.980–36.300 | Akkar |
  | Baalbek-Hermel / Bekaa | 34.000–34.200 × 36.100–36.650 | Baalbek-Hermel |
  | Baalbek-Hermel / North Lebanon | 34.100–34.680 × 36.100–36.300 | Baalbek-Hermel |
  | Bekaa / North Lebanon | 34.100–34.200 × 35.750–36.300 | Bekaa |
  | Bekaa / Mount Lebanon | 33.540–34.120 × 35.750–35.950 | Bekaa |
  | South Lebanon / Bekaa | 33.380–33.580 × longitude 35.750 only | Bekaa at the shared boundary |
  | South Lebanon / Nabatieh | Nabatieh's entire 33.240–33.560 × 35.330–35.720 box | South Lebanon |
  | South Lebanon / Mount Lebanon | 33.540–33.580 × 35.370–35.750 | South Lebanon |
  | Nabatieh / Mount Lebanon | 33.540–33.560 × 35.370–35.720 | South Lebanon (which precedes both) |
  | North Lebanon / Mount Lebanon | 34.100–34.120 × 35.490–35.950 | North Lebanon |

  Concrete wrong results include Hermel (about `34.394, 36.384`) → **Akkar** because the Akkar box precedes Baalbek-Hermel; Hazmieh (about `33.855, 35.533`) → **Beirut** although it is in Mount Lebanon; and Jbeil/Byblos (about `34.1206, 35.651`) → **North Lebanon** because it crosses the Mount box's 34.120 maximum and lies in the North box. A point in the Baalbek-Hermel/North overlap is always called Baalbek-Hermel, and one in the Bekaa/North overlap is always called Bekaa, regardless of its actual side of the governorate boundary. These are not geographic polygons; the overlap/order rule can turn small coordinate or geocoding differences into silent governorate changes.
- **Trigger:** Call `infer_region(None, 34.394, 36.384)` and it returns `Akkar`; call `infer_region(None, 33.855, 35.533)` and it returns `Beirut`.
- **Fix:** Replace independent rectangles with non-overlapping governorate geometry or an authoritative reverse-geocoder; where that is unavailable, return an explicit ambiguous/unknown result rather than imposing list order.

### S1 — Nabatieh is unreachable as an inferred region

- **Where:** `enricher.py:33-40, 64-65, 86-88, 91-94`.
- **Breaks:** Both the address keywords and coordinate boxes for Nabatieh are wholly included in the earlier South Lebanon rules. The address loop reaches `South Lebanon` first because its keyword list already contains `nabatieh` and `النبطية`; the Nabatieh coordinate box is contained by the preceding South Lebanon box. Thus a Nabatieh city/district record is labeled **South Lebanon**, never **Nabatieh**.
- **Trigger:** Address `Nabatieh` returns `South Lebanon`. Coordinate-only point around Nabatieh (`33.377, 35.483`) also returns `South Lebanon`.
- **Fix:** Choose one administrative taxonomy (governorates or regions) and make the keyword and coordinate rules mutually exclusive and ordered to match it.

### S2 — Saudi coverage is five city boxes; addresses do not provide a fallback

- **Where:** `enricher.py:70-76, 85-94`.
- **Breaks:** Saudi has boxes only for Riyadh, Jeddah, Dammam, Mecca, and Medina. The address-keyword branch is gated on `country == "LB"`, and the coordinate branch returns `None` outside those five rectangles. A Taif coordinate (about `21.270, 40.420`) and an Abha coordinate (about `18.216, 42.505`) therefore produce `None`; an address naming either city is ignored as well. **Khobar is an important exception to the brief's shorthand:** a typical Khobar coordinate (`26.217, 50.197`) falls inside the Dammam box and is labeled **Dammam**, not `None`. An address-only Khobar record (no coordinates) does return `None` because SA addresses are ignored.
- **Trigger:** `infer_region("Taif", 21.270, 40.420, country="SA")` → `None`; `infer_region("Khobar", 26.217, 50.197, country="SA")` → `Dammam`; `infer_region("Khobar", None, None, country="SA")` → `None`.
- **Fix:** Use address matching/reverse geocoding for SA too, and cover all supported Saudi locations; represent unmatched cities explicitly rather than silently leaving the region blank.

## Not a bug, but worth knowing

- For LB, an address keyword hit takes precedence over coordinates (`enricher.py:85-88`). Coordinates only resolve ties/missing keyword matches; they do not correct a wrong address-derived region.
- `enrich()` only infers when `region` is falsey (`enricher.py:270-275`), so a pre-populated but wrong region is preserved rather than repaired.
- A missing latitude or longitude, or any coordinate outside the selected country's boxes, returns `None` (`enricher.py:89-94`). The Saudi examples above are concrete in-country misses; no separate Lebanon in-country `None` location is established by these boxes.

## Recommended order of work

1. Replace the Lebanon first-match rectangles and duplicate Nabatieh rules with one consistent, validated geographic model.
2. Add Saudi address/geocoding coverage and distinguish an unmatched location from an inferred governorate.
3. Add table-driven tests for every overlap, each named trigger above, missing coordinates, and locations outside the supported Saudi boxes.
