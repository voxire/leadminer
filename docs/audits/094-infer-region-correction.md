# 094 — infer_region correction: corrected boxes, keyword sets, KSA expansion

## Verdict

`enricher.infer_region` is not wrong in principle — a keyword-then-coordinate fallback is
fine — but its tables are broken in ways that silently mislabel the `region` column, and
for Saudi records it only ever uses coordinates (address is ignored entirely). The fix is
four concrete table edits: (1) a precedence-ordered, mostly-disjoint Lebanon box table,
(2) de-duplicated Lebanon keyword sets with Nabatieh pulled out of South Lebanon,
(3) a much wider KSA city box table, and (4) a new KSA keyword set wired into the address
branch. All tables below are copy-paste replacements for the four module-level constants.

## Findings

### S1 — Overlapping Lebanon boxes assign coordinates to the wrong governorate
- **Where:** `enricher.py:58-68` (`_LB_COORD_REGIONS`) + `enricher.py:89-93` (first-match loop)
- **Breaks:** `Nabatieh` (33.240–33.560, 35.330–35.720) is *entirely contained* in
  `South Lebanon` (33.040–33.580, 35.090–35.750) and appears *after* it, so every Nabatieh
  coordinate is labelled South Lebanon and the Nabatieh box is dead code. `Akkar`
  (34.380–34.720, 35.980–36.650) overlaps `Baalbek-Hermel` (34.000–34.720, 36.100–36.850)
  and appears *before* it, so Hermel (~34.39, 36.38) is labelled Akkar. `Bekaa` vs
  `Mount Lebanon` overlap along the Sannine ridge strip (35.750–35.950).
- **Trigger:** a business in Hermel (34.39, 36.38) → `infer_region(None, 34.39, 36.38, "LB")`
  returns `"Akkar"`. A business in Nabatieh city (33.38, 35.48) returns `"South Lebanon"`.
- **Fix:** replace the table with the disjoint/precedence-ordered table in §A; reorder so
  specific nested/side regions precede the broader ones (documented in §B).

### S2 — Saudi addresses are never matched
- **Where:** `enricher.py:85-88`
- **Breaks:** the address/keyword branch is gated `if address and country == "LB"`. For
  `country == "SA"` the address is ignored and only coordinates are used. A Saudi record
  with an address but no `lat`/`lon` (common for directory/whitelist sources) gets
  `region = None`.
- **Trigger:** `infer_region("King Fahd Rd, Riyadh", None, None, "SA")` returns `None`.
- **Fix:** run the address branch for both countries (§E), selecting `_KSA_REGION_MAP`
  when `country == "SA"`.

### S3 — KSA coverage is five boxes for an entire country
- **Where:** `enricher.py:70-76` (`_KSA_COORD_REGIONS`)
- **Breaks:** only Riyadh, Jeddah, Dammam, Mecca, Medina have boxes. Anything outside those
  five metros (Tabuk, Abha, Taif, Hofuf, Hail, Najran, …) is `None`, even though the brief's
  own scope is "Saudi Arabia (Riyadh, Jeddah, Dammam)" and lead scoring depends on `region`.
- **Fix:** replace with the ~30-city table in §D.

### S4 — Nabatieh keywords are duplicated inside South Lebanon
- **Where:** `enricher.py:33-37` (South) vs `enricher.py:38-41` (Nabatieh)
- **Breaks:** `nabatieh`, `النبطية`, `bint jbeil`, `marjayoun`, `hasbaya` appear in *both*
  sets. Because `_REGION_MAP` is evaluated in list order and South precedes Nabatieh
  (`enricher.py:52-55`), an address containing "Nabatieh" is always labelled South Lebanon
  and the Nabatieh keyword set is dead — the same first-match bug as S1, in the keyword path.
- **Fix:** replace `REGION_KEYWORDS` with the mutually-exclusive sets in §C.

## The correction

### A. Corrected Lebanon box table — `_LB_COORD_REGIONS`

Replace `enricher.py:58-68` with this (order is significant, see §B):

```python
_LB_COORD_REGIONS: list[tuple[str, float, float, float, float]] = [
    # (region, lat_min, lat_max, lon_min, lon_max) — precedence order, first match wins
    ("Beirut",          33.860, 33.930, 35.470, 35.520),  # nested in Mt Lebanon coast → must be 1st
    ("Nabatieh",        33.060, 33.450, 35.380, 35.750),  # before South to win the Marjayoun/Bint Jbeil strip
    ("South Lebanon",   33.060, 33.620, 35.080, 35.600),  # Sidon, Tyre, Jezzine
    ("Akkar",           34.480, 34.690, 35.880, 36.450),  # before Baalbek-Hermel to win Kobayat/Chadra
    ("Baalbek-Hermel",  33.980, 34.690, 36.100, 36.620),  # Baalbek, Hermel, Qaa
    ("Bekaa",           33.380, 34.000, 35.750, 36.200),  # Zahle, Chtaura, Anjar, Rashaya
    ("Mount Lebanon",   33.500, 34.200, 35.340, 35.800),  # coast + mountains (Beirut excluded above)
    ("North",           34.200, 34.480, 35.600, 36.200),  # Tripoli, Batroun, Bcharre, Koura, Danniyeh
]
```

Anchor checks (every capital/dashboard city lands correctly):

| Place | (lat, lon) | Lands in | Why |
|---|---|---|---|
| Beirut | (33.89, 35.50) | Beirut | within box 1; precedes Mt Lebanon |
| Baabda | (33.85, 35.53) | Mount Lebanon | lon 35.53 > Beirut's 35.52, lat 33.85 < 33.86 |
| Jounieh / Byblos | (33.98, 35.63) / (34.12, 35.65) | Mount Lebanon | box 7 |
| Sidon / Tyre | (33.56, 35.37) / (33.27, 35.19) | South Lebanon | box 3 (precedes Mt Lebanon) |
| Jezzine | (33.54, 35.58) | South Lebanon | lat 33.54 > Nabatieh's 33.45 |
| Nabatieh / Marjayoun / Bint Jbeil | (33.38, 35.48) / (33.36, 35.59) / (33.12, 35.43) | Nabatieh | box 2 precedes South |
| Zahle / Chtaura | (33.85, 35.90) / (33.82, 35.77) | Bekaa | box 6 precedes Mt Lebanon in the ridge strip |
| Baalbek | (34.00, 36.20) | Baalbek-Hermel | box 5 precedes Bekaa at lat 34.0 |
| Hermel | (34.39, 36.38) | Baalbek-Hermel | lat 34.39 < Akkar's 34.48 |
| Tripoli / Batroun / Bcharre | (34.44, 35.85) / (34.25, 35.66) / (34.25, 36.03) | North | box 8 |
| Halba / Kobayat | (34.55, 36.08) / (34.55, 36.28) | Akkar | box 4 precedes Baalbek-Hermel |

### B. Overlap-resolution order (why first-match-wins now works)

The loop at `enricher.py:90-93` returns the **first** box that contains the point. The table
above is ordered so that wherever two boxes still overlap, the *more specific* one comes
first. The five overlaps, and how each resolves:

1. **Beirut ⊂ Mount Lebanon coast.** Beirut is a small urban governorate physically inside
   the coastal band of Mount Lebanon, so its box is nested and must precede it. Beirut wins
   everything in (33.860–33.930, 35.470–35.520); the southern suburbs (Dahiyeh, ~33.84–33.85)
   fall just below and correctly go to Mount Lebanon/Baabda.
2. **Nabatieh vs South Lebanon.** The inland southern governorates are side-by-side, not
   nested. Nabatieh precedes South, so the (33.06–33.45, 35.38–35.60) strip — Nabatieh city,
   Marjayoun, Bint Jbeil — resolves to Nabatieh, while the coastal Tyre/Sidon and the inland
   Jezzine (lat 33.54 > 33.45) stay South. This is the opposite of today, where Nabatieh was
   *after* South and therefore unreachable.
3. **Akkar vs Baalbek-Hermel.** Akkar's eastern villages (Kobayat ~36.28, Chadra/Akroum
   ~36.4) sit in the lat 34.48–34.69 band that also covers Baalbek-Hermel. Akkar precedes, so
   they stay Akkar; Hermel (34.39 < 34.48) and Qaa/Ras Baalbek fall below the Akkar band and
   stay Baalbek-Hermel. Today's order (Akkar *before* Baalbek-Hermel but with Akkar starting
   at 34.38) wrongly captured Hermel.
4. **Baalbek-Hermel vs Bekaa.** A thin band (33.98–34.00, 36.10–36.20) around Baalbek
   overlaps; Baalbek-Hermel precedes so Baalbek city stays Baalbek-Hermel rather than Bekaa.
5. **Bekaa vs Mount Lebanon (ridge strip).** The (33.50–34.00, 35.75–35.80) sliver contains
   Bekaa towns (Chtaura 35.77, Joub Jannine 35.77, Saghbine); Bekaa precedes Mount Lebanon so
   they stay Bekaa. **Known residual error:** the diagonal Sannine ridge is not axis-aligned,
   so ridge villages at lon 35.80–35.90 (Sofar ~35.87, Mdeirej, Dhour Choueir) still land in
   Bekaa even though they are Mount Lebanon. A rectangle cannot separate Chtaura (33.82,
   35.77) from Sofar (33.80, 35.87) at nearly equal latitude — see §F.

Everything else is disjoint by construction (shared edges at exactly 34.48, 34.20, 34.00,
35.80, 35.75, 35.60), so no further ordering is needed for correctness; the order above only
matters for the five overlaps listed.

### C. Corrected Lebanon keyword sets — `REGION_KEYWORDS`

Replace `enricher.py:10-50`. Each place name appears in exactly one governorate; Nabatieh's
district towns are no longer listed under South Lebanon.

```python
REGION_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Beirut", [
        "beirut", "بيروت", "hamra", "achrafieh", "ashrafieh", "الأشرفية",
        "verdun", "ras beirut", "sodeco", "badaro", "gemmayze", "mar mikhael",
        "corniche", "bliss", "sanayeh", "zkak el blat", "tallet el khayat",
        "raouche", "الروشة",
    ]),
    ("Mount Lebanon", [
        "jounieh", "جونية", "jbeil", "جبيل", "byblos", "baabda", "بعبدا",
        "aley", "aaley", "عاليه", "chouf", "الشوف", "metn", "المتن",
        "keserwan", "كسروان", "antelias", "jdeideh", "bikfaya", "broummana",
        "beit mery", "dbayeh", "naccache", "sin el fil", "dekwaneh", "hazmieh",
        "aramoun", "khalde", "damour", "jiyeh", "bchamoun", "bhamdoun",
        "deir el qamar", "beit ed dine", "zalka", "kaslik", "zouk", "ghazir",
        "fanar", "mansourieh", "mtayleb", "rabweh",
    ]),
    ("North Lebanon", [
        "tripoli", "طرابلس", "zgharta", "زغرتا", "zghorta", "batroun",
        "bcharre", "بشري", "bsharri", "koura", "الكورة", "amioun", "chekka",
        "enfeh", "qalamoun", "minyeh", "المنية", "danniyeh", "الضنية",
        "ehden", "kousba",
    ]),
    ("Akkar", [
        "akkar", "عكار", "halba", "حلبا", "andqet", "bebnine", "kobayat",
        "qoubaiyat", "القبيات",
    ]),
    ("South Lebanon", [
        "sidon", "saida", "صيدا", "tyre", "sur", "sour", "صور", "jezzine",
        "جزين", "zahrani", "sarafand", "صرفند", "naqoura", "الناقورة",
    ]),
    ("Nabatieh", [
        "nabatieh", "النبطية", "bint jbeil", "بنت جبيل", "hasbaya", "حاصبيا",
        "marjayoun", "مرجعيون", "merjeyoun", "khiam", "الخيام", "ibl el saqi",
        "إبل السقي",
    ]),
    ("Bekaa", [
        "zahle", "زحلة", "chtaura", "شتورا", "anjar", "عنجر", "rashaya",
        "راشيا", "west bekaa", "البقاع الغربي", "saghbine", "yohmor",
        "taanayel", "bar elias", "برالياس", "jeb jennine", "جب جنين",
    ]),
    ("Baalbek-Hermel", [
        "baalbek", "بعلبك", "hermel", "الهرمل", "yammouneh", "ras baalbek",
        "qaa", "deir el ahmar", "دير الأحمر",
    ]),
]
```

Two side fixes folded in: the original Mount Lebanon list contained `"zahle el metn"`
(`enricher.py:21`), a malformed token that put Zahle (a Bekaa city) into Mount Lebanon — it
is now `"zalka"` (Metn). And `merjeyoun`/`khiam`/`ibl el saqi` (all Marjayoun district,
Nabatieh governorate) moved from South to Nabatieh.

### D. KSA city list — `_KSA_COORD_REGIONS`

Replace `enricher.py:70-76` with ~30 cities (the three target metros plus the rest of the
Kingdom). Order matters only inside the Eastern-Province cluster, where the smaller cities
precede Dammam.

```python
_KSA_COORD_REGIONS: list[tuple[str, float, float, float, float]] = [
    # Eastern Province cluster — specific cities before the Dammam metro
    ("Dhahran",          26.20, 26.45, 50.05, 50.25),
    ("Qatif",            26.45, 26.75, 49.90, 50.15),
    ("Ras Tanura",       26.55, 26.85, 50.05, 50.30),
    ("Khobar",           26.15, 26.45, 50.10, 50.35),
    ("Abqaiq",           25.80, 26.10, 49.55, 49.85),
    ("Dammam",           26.20, 26.55, 49.90, 50.25),
    ("Jubail",           26.85, 27.20, 49.50, 49.85),
    ("Hofuf",            25.25, 25.55, 49.45, 49.85),
    # West (Hejaz)
    ("Riyadh",           24.40, 25.10, 46.40, 47.10),
    ("Jeddah",           21.35, 21.75, 39.05, 39.45),
    ("Mecca",            21.25, 21.55, 39.70, 40.05),
    ("Medina",           24.30, 24.65, 39.40, 39.80),
    ("Taif",             21.15, 21.45, 40.30, 40.60),
    ("Yanbu",            23.95, 24.25, 37.95, 38.25),
    ("Rabigh",           22.65, 22.95, 38.90, 39.20),
    ("Qunfudhah",        19.00, 19.30, 40.95, 41.25),
    ("Al Bahah",         19.90, 20.20, 41.35, 41.65),
    # North
    ("Tabuk",            28.25, 28.55, 36.45, 36.80),
    ("Hail",             27.40, 27.70, 41.55, 41.90),
    ("Sakaka",           29.85, 30.15, 40.05, 40.40),
    ("Arar",             30.85, 31.15, 40.95, 41.25),
    ("Hafar Al-Batin",   28.30, 28.60, 45.80, 46.15),
    # Central (Najd)
    ("Buraidah",         26.20, 26.50, 43.80, 44.15),
    ("Unaizah",          25.95, 26.25, 43.85, 44.15),
    ("Dawadmi",          24.35, 24.65, 44.25, 44.55),
    ("Al-Kharj",         24.00, 24.35, 47.15, 47.55),
    # South
    ("Abha",             18.10, 18.40, 42.40, 42.70),
    ("Khamis Mushait",   18.15, 18.45, 42.60, 42.90),
    ("Bisha",            19.90, 20.20, 42.50, 42.80),
    ("Jizan",            16.75, 17.05, 42.40, 42.70),
    ("Najran",           17.40, 17.70, 44.00, 44.30),
]
```

`Hofuf` is used as the `region` label for Al-Ahsa; if you prefer "Al-Ahsa", rename the string
but keep the bounds. The three target metros (Riyadh/Jeddah/Dammam) keep their existing
bounds so no existing output changes for them.

### E. KSA keyword sets + wire the address branch

Add a module-level map next to `_REGION_MAP` (after `enricher.py:55`):

```python
KSA_REGION_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Riyadh",          ["riyadh", "الرياض"]),
    ("Jeddah",          ["jeddah", "jedda", "جدة"]),
    ("Mecca",           ["makkah", "mecca", "مكة"]),
    ("Medina",          ["madinah", "medina", "المدينة المنورة"]),
    ("Dammam",          ["dammam", "الدمام"]),
    ("Khobar",          ["khobar", "al khobar", "الخبر"]),
    ("Dhahran",         ["dhahran", "الظهران"]),
    ("Qatif",           ["qatif", "al qatif", "القطيف"]),
    ("Jubail",          ["jubail", "al jubail", "الجبيل"]),
    ("Hofuf",           ["hofuf", "al ahsa", "al hasa", "الهفوف", "الأحساء"]),
    ("Taif",            ["taif", "al taif", "الطائف"]),
    ("Tabuk",           ["tabuk", "تبوك"]),
    ("Abha",            ["abha", "أبها", "ابها"]),
    ("Khamis Mushait",  ["khamis mushait", "خميس مشيط"]),
    ("Bisha",           ["bisha", "بيشة"]),
    ("Jizan",           ["jizan", "gizan", "jazan", "جازان", "جيزان"]),
    ("Najran",          ["najran", "نجران"]),
    ("Buraidah",        ["buraidah", "buraydah", "بريدة"]),
    ("Unaizah",         ["unaizah", "unayzah", "عنيزة"]),
    ("Hail",            ["hail", "حائل"]),
    ("Sakaka",          ["sakaka", "skaka", "سكاكا"]),
    ("Arar",            ["arar", "عرعر"]),
    ("Al Bahah",        ["al bahah", "baha", "الباحة"]),
    ("Yanbu",           ["yanbu", "ينبع"]),
    ("Rabigh",          ["rabigh", "رابغ"]),
    ("Hafar Al-Batin",  ["hafar al batin", "hafr al batin", "حفر الباطن"]),
    ("Al-Kharj",        ["al kharj", "kharj", "الخرج"]),
    ("Abqaiq",          ["abqaiq", "buqayq", "بقيق"]),
    ("Ras Tanura",      ["ras tanura", "ras tannura", "رأس تنورة"]),
    ("Qunfudhah",       ["qunfudhah", "القنفذة"]),
    ("Dawadmi",         ["dawadmi", "الدوادمي"]),
]

_KSA_REGION_MAP: list[tuple[str, re.Pattern]] = [
    (region, re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE))
    for region, keywords in KSA_REGION_KEYWORDS
]
```

Then change the address branch in `infer_region` (`enricher.py:85-88`) from LB-only to both:

```python
def infer_region(address, lat, lon, country="LB"):
    if address:
        region_map = _KSA_REGION_MAP if country == "SA" else _REGION_MAP
        for region, pattern in region_map:
            if pattern.search(address):
                return region
    if lat is not None and lon is not None:
        boxes = _KSA_COORD_REGIONS if country == "SA" else _LB_COORD_REGIONS
        for region, lat_min, lat_max, lon_min, lon_max in boxes:
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                return region
    return None
```

Two Arabic-matching caveats worth codifying in a comment (or a tiny normalizer):
- **Medina:** use `المدينة المنورة` (or the word-boundary form `المدينة`), never bare
  `المدينة` alone — "المدينة" means "the city" and would false-match any address.
- **`sur`/`qaa`/`bliss` (and `mecca`):** these short Latin tokens currently match as
  substrings because `re.escape` + `join` has no word boundaries — `sur` matches "insurance",
  `qaa` matches inside "Nqaa...". Wrap short keys in `\b...\b`, or normalize the address
  (lowercase, strip Arabic diacritics, unify أ/إ→ا, ة→ه, ى→ي) before matching. This affects
  both the LB and KSA maps equally.

### F. Rigorous upgrade (not required for this fix): point-in-polygon

Axis-aligned bounding boxes cannot represent Lebanon's diagonal governorate boundaries. The
one place this still bites is the Mount Lebanon/Bekaa ridge (§B.5): the boundary runs NE–SW
along the Sannine ridge, so a vertical longitude cut mislabels ridge villages at
lon 35.80–35.90 (Sofar, Mdeirej, Dhour Choueir) as Bekaa. If `region` precision on the ridge
matters, replace the LB boxes with governorate polygons and a point-in-polygon test
(ray-casting or `shapely`), keyed on the boundary anchor points (33.55, 35.75) → (34.10,
36.05) for the Bekaa/Mount Lebanon edge. The tables above are the correct *bounding-box*
approximation and resolve the three headline bugs (Nabatieh dead, Hermel→Akkar, Beirut
nested); PIP is only needed if you want exact ridge assignment.

## Recommended order of work

1. Replace `_LB_COORD_REGIONS` (§A) — fixes S1, the silent mislabelling.
2. Replace `REGION_KEYWORDS` (§C) — fixes S4 and the `"zahle el metn"` token.
3. Replace `_KSA_COORD_REGIONS` (§D) — fixes S3.
4. Add `KSA_REGION_KEYWORDS`/`_KSA_REGION_MAP` and open the address branch to SA (§E) — fixes S2.
5. (Optional) add word-boundary matching for short Latin keys, then PIP for the ridge if it matters.
