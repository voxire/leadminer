# 066 — Typing strategy: make `BusinessRecord` a contract, not documentation

## Verdict

`BusinessRecord` is a `TypedDict` used only as a constructor — a runtime-inert alias for
`dict` — and every function downstream of `scrapers/` is annotated `dict` /
`list[dict]` / `Mapping`, which collapses to `dict[Any, Any]` and poisons every
`.get()` with `Any`. The fix is not to swap `TypedDict` for a runtime-validating object
model, but to keep `TypedDict`, move it to a top-level `contracts.py`, thread it through
every signature, and add a hand-rolled validator at the only two places untyped `str`
data enters or leaves the system (`load_master` and `write_csv`). That, plus `mypy
--strict` with `disallow_any_expr`, turns the type from prose into something the compiler
enforces — and it is the single cheapest change that prevents the class of silent-shape
bugs the audits keep re-finding (020 S1/S2/S4, 021 S2-1, 028 S2-1).

---

## The decision: keep `TypedDict`; reject pydantic, dataclasses, and attrs

The pipeline is dict-native end to end, and that is the deciding fact:

- `csv.DictReader` yields `dict` (`main.py:86`);
- `dedup._merge` mutates dicts with `all_keys = set(a) | set(b)` and `a.get(key)`
  (`dedup.py:83-99`);
- `enricher` mutates in place with `r["x"] = ...` and `r.setdefault(...)`
  (`enricher.py:242-251, 322-342`);
- `csv.DictWriter` consumes `dict` (`main.py:125-127`).

A `TypedDict` is *still a `dict` at runtime*, so all four idioms keep working unchanged.
The other three options replace `dict` with a new object that has to be converted to and
from `dict` (`asdict()`, `.model_dump()`, `attr.asdict()`) at every boundary, and they
break the `set(a) | set(b)` merge and the `r["x"] = ...` mutation outright.

**pydantic `BaseModel`** — its value is nested schema, JSON (de)serialization, and
ecosystem integration. A 23-column flat CSV uses none of it. It would add a heavy
dependency, runtime overhead, and a serialize/deserialize seam for no benefit. Its
"field not present = validation error" default also fights the codebase's reality: the
record is *partial* during scraping, *differently partial* during dedup, and only
complete at export (020 S4). You would paper over that with `Optional` defaults, which
re-conflates "absent" and "None" — the exact ambiguity that is already causing bugs (020
S5, 021 S3-4).

**`dataclasses`** — give a real `__init__` and `__eq__` (nice for tests) but no
validation, need `asdict()`/manual construction for `DictWriter`, and `field(default=None)`
re-conflates absent/None. **`attrs`** is a superset of dataclasses but is still
object-semantics plus a new dependency, and its validators/converters are the wrong tool
for a flat row.

**`TypedDict` wins on four axes:** (1) zero runtime change, (2) zero new dependency, (3)
it matches the dict-native CSV + in-place-mutation code, and (4) `Required`/`NotRequired`
is the *only* one of the four that can model the partial-record stages that are the root
cause of 020 S4. Runtime validation is still needed, but only at the two IO chokepoints —
a ~30-line validator, not a whole object model.

The one scenario where pydantic/attrs would win is if we simultaneously adopt the
normalized entity model in audit 034. That is a separate architectural decision; for the
*current* flat-CSV architecture, `TypedDict` is the only honest choice.

---

## Findings

### S1 — The type dies at the `scrapers/` boundary; every downstream signature is `dict`

- **Where:** `scrapers/base.py:5-28` (the `TypedDict`), vs `dedup.py:102`,
  `dedup.py:83`, `dedup.py:79`, `enricher.py:101`, `enricher.py:275`, `enricher.py:217`,
  `enricher.py:318`, `main.py:108`, `main.py:137`, `pitch_recommender.py:25`.
- **Breaks:** `BusinessRecord` appears in only 4 files, all under `scrapers/`
  (grep-confirmed in 020 S2). `main.py` never imports it. So a dropped kwarg, a typo
  (`lead_scor=0`), or `review_count="12"` in one scraper ships clean — nothing is checked
  anywhere, because every downstream `.get()` returns `Any`, and `Any` is accepted in
  every position.
- **Trigger:** delete `whatsapp=None` from `osm.py:109`; the row still writes 23 columns
  (with a blank `whatsapp` cell) and no tool, test, or type checker notices.
- **Fix:** thread `BusinessRecord` through every signature (full list in "The spec"
  below). The `list[dict]` / `dict` / `Mapping` annotations are the reason mypy sees
  nothing today.

### S2 — Required vs. optional is declared backwards; four "required" keys are the ones most likely missing

- **Where:** `scrapers/base.py:25-28` (`lead_score: int`, `source: str`,
  `scraped_at: str`, `completeness_score: int` — all non-optional) vs the assignment
  order at `main.py:190-197` and `enricher.py:331-342`.
- **Breaks:** `main.py` filters and dedups *before* anything sets `industry_priority`,
  `recommended_service`, `completeness_score`, or `lead_score`; a `master` record that
  never re-matched a fresh scrape keeps `lead_score=None`. The four "required" keys are
  exactly the four that are absent at the stage where the type stops existing (020 S4).
- **Trigger:** a stale master row that no scrape matches this month.
- **Fix:** `class BusinessRecord(TypedDict, total=False)` with `name: Required[str]` and
  everything else `NotRequired`. Then `r["lead_score"]` (direct subscript) becomes a
  mypy error forcing `.get()` or an explicit presence check — the actual failure mode.

### S3 — `website_live: bool | None` cannot express the tri-state the product needs

- **Where:** `scrapers/base.py:16`; consumed at `enricher.py:292-296` and
  `pitch_recommender.py:34-35, 54`.
- **Breaks:** 403/404/429/5xx/transport-failure all collapse to one value, so
  `pitch_recommender.py:54` pitches "Website rebuild" at sites that are fine but refuse
  the crawler (020 S7). `bool | None` has no room for the `live`/`dead`/`blocked`/
  `unknown` distinction the product's #1 pitch tier actually depends on.
- **Fix:** `WebsiteStatus = Literal["live", "dead", "blocked", "unknown"]` and
  `website_live: WebsiteStatus | None`. This is a **schema change**: the CSV will now
  store `live`/`dead`/`blocked`/`unknown` instead of `True`/`False`, so `load_master`'s
  parser, the summary counts at `main.py:215-217`, and the `enricher.py:242` assignment
  must change together (see "Coordinated change sites" below). Under `strict_equality`,
  the leftover `is True` / `is False` comparisons become non-overlapping-comparison
  errors — mypy mechanically finds every site that must change.

### S4 — `str | None` fields compared to string literals / truthiness are the latent `TypeError`s

- **Where:** `enricher.py:304-305` (`rating is not None and rating < 4.0`),
  `pitch_recommender.py:43-52` (`int(review_count)` / `int(completeness)` in a
  `try/except`), `pitch_recommender.py:135-140` (`float(rating)` in a `try/except`).
- **Breaks:** these defensive `try/except` blocks exist precisely because `rating` /
  `review_count` arrive as `Any` (a `str`, an `int`, a `float`, or `None`) and the code
  cannot know which. Two of the three functions guard the *same field* differently
  (`enricher.py:305` assumes numeric, `pitch_recommender.py:138` wraps in `float()`+try)
  — 020 S2 flagged this asymmetry as a genuine type-safety hole.
- **Trigger:** a master row whose `rating` cell was edited to `"4.5 stars"` by a human.
  `lead_score` raises `TypeError`; `_is_low_rating` silently returns `False`.
- **Fix:** once `record: BusinessRecord` is threaded, `record.get("rating")` is
  `float | None`, and `float(rating)` on a `float | None` becomes a mypy error. The
  `try/except` falls away and the boundary validator (below) is the one place a bad cell
  is rejected with a loud, located error.

### S5 — Two of the worst bugs are *logic* bugs typing cannot catch; the boundary validator is the backstop

- **Where:** the UTF-8 BOM `name`-wipe (020 S1, 021 S2-2, 028 S1-1) and the
  `.get("country", default)` default-never-fires (020 S5).
- **Breaks:** these are the two silent, irreversible data losses in the audits. Neither
  is a type error: reading `\ufeffname` instead of `name` is a byte-string issue, and
  `.get(k, default)` returning `None` when `k` is present-but-`None` is *valid Python* —
  mypy will not and cannot flag either.
- **Fix:** `encoding="utf-8-sig"` + header validation on read (020 S1's actual fix), and
  `r.get("country") or "LB"` at the four sites (`main.py:193`, `dedup.py:110,133`,
  `enricher.py:326`). Type annotations get you *nothing* here; say so plainly rather than
  pretending `mypy` closes them.

---

## The spec

### 1. One home, one order — `contracts.py`

```python
# contracts.py — single canonical record shape. Moved out of scrapers/base.py
# (kills 020 S11/S12's import cycle, which is triggered the moment enricher imports
#  the type back from scrapers.base).

from typing import Literal, NotRequired, Required, TypedDict

WebsiteStatus = Literal["live", "dead", "blocked", "unknown"]
Priority = Literal["high", "medium", "low"]


class BusinessRecord(TypedDict, total=False):
    name: Required[str]          # the only field the product cannot run without

    category: str | None
    region: str | None
    country: str | None
    address: str | None
    lat: float | None
    lon: float | None
    phone: str | None
    email: str | None
    website: str | None
    website_live: WebsiteStatus | None
    facebook: str | None
    instagram: str | None
    whatsapp: str | None
    linkedin: str | None
    rating: float | None
    review_count: int | None
    completeness_score: int
    lead_score: int
    industry_priority: Priority
    recommended_service: str
    source: str
    scraped_at: str


# Column order is part of the "product" (CSVs are diffed/hand-edited). Derive it,
# never hand-maintain it — 020 S3, 021 S2-1, 028 S2-1 all trace to the two copies.
FIELDS: tuple[str, ...] = tuple(BusinessRecord.__annotations__)
```

Notes:

- Declaration order above matches `main.py:33-40` exactly, so the CSV column order is
  byte-identical after the change.
- `total=False` makes every field `NotRequired` except `name`. This is deliberate: the
  four formerly-"required" keys (`lead_score`, `source`, `scraped_at`,
  `completeness_score`) are not guaranteed until late in `main.py`, so leaving them
  `NotRequired` forces `.get()`/presence-check downstream instead of a false guarantee.
- `website_live` becomes the `WebsiteStatus` literal (S3). This is the one behavioral
  change; see the coordinated sites below.

### 2. Thread the type through every signature

Replace these annotations (bare `dict`/`Mapping`/`list[dict]` → `BusinessRecord`):

| File:line | Current | New |
|---|---|---|
| `scrapers/base.py:31-33` | `scrape() -> Iterator[BusinessRecord]` | keep, but `BusinessRecord` now imports from `contracts` |
| `dedup.py:79` | `_field_count(record: dict) -> int` | `(record: BusinessRecord) -> int` |
| `dedup.py:83` | `_merge(a: dict, b: dict) -> dict` | `(a: BusinessRecord, b: BusinessRecord) -> BusinessRecord` |
| `dedup.py:102` | `dedup(records: list[dict]) -> list[dict]` | `(records: list[BusinessRecord]) -> list[BusinessRecord]` |
| `enricher.py:101` | `completeness_score(record: dict) -> int` | `(record: BusinessRecord) -> int` |
| `enricher.py:158` | `_fetch_website(url: str) -> tuple[str, dict]` | `-> tuple[WebsiteStatus, _Contacts]` (typed `_Contacts` `TypedDict`) |
| `enricher.py:217` | `check_websites(records: list[dict], workers: int = 40) -> list[dict]` | `list[BusinessRecord]` |
| `enricher.py:275` | `lead_score(record: dict) -> int` | `(record: BusinessRecord) -> int` |
| `enricher.py:318` | `enrich(records: list[dict]) -> list[dict]` | `list[BusinessRecord]` |
| `main.py:50` | `resolve_country(record: dict) -> str` | `(record: BusinessRecord) -> str` |
| `main.py:77` | `load_master(path) -> list[dict]` | `-> list[BusinessRecord]` |
| `main.py:108` | `write_csv(path, records: list[dict]) -> None` | `(path, records: list[BusinessRecord]) -> None` |
| `main.py:137` | `has_any_contact(record: dict) -> bool` | `(record: BusinessRecord) -> bool` |
| `pitch_recommender.py:25` | `recommend_service(record: Mapping) -> str` | `(record: BusinessRecord) -> str` |
| `pitch_recommender.py:135` | `_is_low_rating(rating) -> bool` | `(rating: float | None) -> bool` |
| `main.py:157` | `def run(scraper):` | `def run(scraper: BaseScraper) -> tuple[str, list[BusinessRecord]]:` |

`is_business_category(category: str | None)` and `industry_priority(category: str | None)`
(`whitelist.py:105,133`) are already correctly typed and stay put.

### 3. The boundary validator — the two places untyped `str` enters/leaves

mypy can only see *internal* code. Data enters from an external CSV (`load_master`) and
leaves to one (`write_csv`); that drift (BOM, renamed columns, `"TRUE"`, extra columns) is
invisible to any type checker. Close it with a validator, not with `try/except`:

```python
def load_master(path: pathlib.Path) -> list[BusinessRecord]:
    with open(path, newline="", encoding="utf-8-sig") as f:      # 020 S1 fix
        reader = csv.DictReader(f)
        missing = set(FIELDS) - set(reader.fieldnames)
        extra = set(reader.fieldnames) - set(FIELDS)
        if missing:
            raise ValueError(f"master CSV missing columns: {sorted(missing)}")
        if extra:
            raise ValueError(f"master CSV has unknown columns: {sorted(extra)}")
        for row in reader:
            yield _coerce(row)          # per-field cast to the declared type
```

`_coerce` is the single place a cell becomes a typed value: `lat/lon/rating -> float`,
`review_count/completeness_score/lead_score -> int(float(x))`,
`website_live -> WebsiteStatus` via a `_parse_status()` that accepts
`live/dead/blocked/unknown` (and, for tolerance, maps legacy `True/False/true/false/1/0`
— 021 S3-3, 028 S3-1), everything else stays `str | None` with `"" -> None`. A bad cell
raises here, with the column name in the message, instead of being silently swallowed as
`None` (`main.py:92-101` today).

`write_csv` keeps the atomic-write logic (`main.py:121-134`, which the regression test
`tests/test_lead_signal.py:155-177` already protects) but flips
`extrasaction="ignore"` → `extrasaction="raise"` and asserts
`set(record) == set(FIELDS)` before writing, so an undeclared key that slips past the
scraper (which `TypedDict` now makes a type error *at its source*) fails loudly at the
exporter (020 S8, 021 S2-1).

### 4. Coordinated change sites for `website_live`

Switching to `WebsiteStatus` touches, in one commit:

- `enricher.py:151-153` — keep `LIVE/DEAD/UNKNOWN` but add `BLOCKED`; `_fetch_website`
  already returns a distinct `UNKNOWN` for transport failure (`enricher.py:170,184`),
  so the `blocked`/`unknown` split needs only a new branch for `4xx` vs `5xx`/other.
- `enricher.py:242` — `r["website_live"] = outcome` (drop the `True/False/None` collapse).
- `main.py:215-217` — summary counts keyed on `WebsiteStatus` values.
- `pitch_recommender.py:34-35` — `website_live == "live"` / `== "dead"`.
- `enricher.py:292-296` — `lead_score` gates on `== "live"` / `== "dead"`.
- `load_master`'s `_parse_status()` (above) and the README column description
  (`README.md:40`).

mypy `strict_equality` + `truthy-bool` will flag every remaining `is True` / `is False`
against the literal, so this list can be verified mechanically, not by memory.

---

## The mypy strict configuration

```toml
[tool.mypy]
python_version = "3.12"

strict = true
# strict enables: disallow_any_generics, disallow_untyped_defs, disallow_untyped_calls,
# disallow_incomplete_defs, check_untyped_defs, disallow_untyped_decorators,
# disallow_subclassing_any, no_implicit_optional, warn_redundant_casts,
# warn_unused_ignores, warn_return_any, no_implicit_reexport, strict_equality,
# warn_unused_configs.

# The one flag ABOVE strict that this codebase actually needs. It forces every
# `record.get(...)` that currently returns `Any` to be dealt with. It is noisy:
# flip it LAST, after BusinessRecord is threaded through (see order of work).
disallow_any_expr = true

# Extra error codes beyond the defaults.
enable_error_code = [
    "truthy-bool",          # `is True` / `is False` on a bool/Literal (S6/S7)
    "redundant-expr",
    "possibly-undefined",
    "ignore-without-code",
    "explicit-override",
]

files = [
    "main.py",
    "dedup.py",
    "enricher.py",
    "pitch_recommender.py",
    "scrapers",
    "contracts.py",
]

[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_defs = false
```

Dev dependencies to add (to a `requirements-dev.txt` or a `[project.optional-dependencies]`,
not the runtime `requirements.txt`): `mypy`, `types-requests`, `types-urllib3`.
Delete `beautifulsoup4` and `lxml` (dead — 020 S10). `requests`/`urllib3` are the only
third-party imports; their stubs let `disallow_untyped_calls` and `check_untyped_defs`
stay on without a blanket `ignore_missing_imports`.

### Which audit bugs this configuration catches — and which it cannot

| Audit finding | Caught by | Flag |
|---|---|---|
| 020 S2 — type dies at boundary | ✅ | `disallow_any_generics` (bare `dict`/`Mapping`/`list[dict]` become errors) |
| 020 S4 — required/optional backwards | ✅ | `TypedDict(total=False)` + `NotRequired`; `r["lead_score"]` access becomes an error |
| 020 S7 — `website_live` tri-state | ✅ | `Literal[...]` + `strict_equality` + `truthy-bool` flag `is True`/`is False` |
| 020 S6 — asymmetric `website_live` guard | ⚠️ indirect | logic bug, but the `Literal` change *forces* rewriting the `is True` guard into the correct `website and == "dead"` |
| 020 S8 / 021 S2-1 / 028 S2-1 — silent shape drift | ✅ | `TypedDict` makes `r["notes"]=` a source-level error; `extrasaction="raise"` catches external drift |
| 020 S3 — `FIELDS` vs `BusinessRecord` divergence | ✅ | single source: `FIELDS = tuple(BusinessRecord.__annotations__)` |
| enricher/pitch `rating`/`review_count` `Any` casts | ✅ | `disallow_any_expr` + `float(float|None)` becomes an error |
| google_places `resp.json()` → `Any` flowing into `_pick_category`/`infer_region` | ✅ | `warn_return_any` + `disallow_any_expr` |
| 020 S1 / 021 S2-2 / 028 S1-1 — UTF-8 BOM `name` wipe | ❌ | byte-level IO; needs `utf-8-sig` + header assert + the existing regression test |
| 020 S5 — `.get("country", default)` default-never-fires | ❌ | valid Python; needs `or "LB"` + `test_none_country_falls_back` |
| 028 S2-2 / 034 S1-1 — `_field_count` winner-takes-all merge | ❌ | logic bug; needs the merge fix, not typing |
| 021 S3-3 / 028 S3-1 — `"TRUE"`/`"FALSE"`/`"1"` parsing | ⚠️ indirect | typing the field as `Literal` forces a `_parse_status` that must handle these spellings |

---

## Not a bug, but worth knowing

- **`disallow_any_expr` is the linchpin and the loudest flag.** Flipped on *before*
  threading the types it produces hundreds of errors in the three scrapers (every
  `el.get(...)`, `place.get(...)`, `row.get(...)` on `resp.json()` output is `Any`). The
  correct order is: thread `BusinessRecord` first, reach zero under plain `--strict`,
  then flip `disallow_any_expr` and fix the scrapers' `Any` leaks by typing the `resp.json()`
  payloads with narrow `TypedDict`s (`OverpassElement`, `WikidataBinding`,
  `PlacesPlace`). The `[tool.mypy.overrides]` on `tests.*` keeps the stdlib-only
  `unittest` file from blocking CI.

- **`NotRequired` access is stricter in pyright than mypy.** mypy flags direct subscript
  of a `NotRequired` key as an error in most cases, but if the team wants the sharpest
  version of S2's catch, pyright's `reportTypedDictNotRequiredAccess = "error"` is the
  definitive guard. The `TypedDict`-based strategy here is checker-agnostic — the
  contract and the validator work under either.

- **The `TypedDict` is only the *final* shape.** The pipeline's *starting* shape (raw
  scraper fields) and *final* shape (enriched) are genuinely different, and audit 034's
  normalized model is the real answer to that. Within the flat model, `total=False` is
  the pragmatic compromise; if you want more precision, split it into
  `RawObservation(TypedDict, total=False)` (what `scrape()` yields) and
  `BusinessRecord(RawObservation)` (adds the `NotRequired` enriched fields). That costs
  one extra type and buys a compile-time guarantee that scrapers don't set scoring fields.

---

## Recommended order of work

1. **Create `contracts.py`** with the `BusinessRecord` above; delete the copy in
   `scrapers/base.py:5-28` and re-export (`from contracts import BusinessRecord`) so the
   scrapers keep importing from `base`. Simultaneously delete `main.FIELDS` and set
   `FIELDS = tuple(BusinessRecord.__annotations__)`. One commit; no behavior change yet
   (except the `FIELDS` derivation, which is byte-identical).
2. **Fix the two logic bugs typing can't reach** — `utf-8-sig` + header assert in
   `load_master` (020 S1) and `or "LB"` at the four `country` sites (020 S5). Do these
   *before* adding the checker so `mypy` is looking at correct code.
3. **Thread `BusinessRecord` through the signature table above**, and add the
   `load_master`/`write_csv` validators (`extrasaction="raise"`, key-coverage assert).
   Reach zero errors under `strict = true` only.
4. **Switch `website_live` to `WebsiteStatus`** in the same pass as step 3's consumer
   sites; let `strict_equality`/`truthy-bool` enumerate the sites you must touch.
5. **Flip `disallow_any_expr = true`** and type the scraper JSON payloads
   (`OverpassElement` etc.) to eliminate the last `Any`.
6. **Add `mypy` to CI** (`.github/workflows/scrape.yml` or a dedicated lint job) with
   `mypy` as a hard gate, and delete the dead `beautifulsoup4`/`lxml` pins while adding
   `types-requests`/`types-urllib3` to dev deps.
