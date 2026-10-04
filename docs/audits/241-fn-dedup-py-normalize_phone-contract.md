# 200 — `normalize_phone` contract: declared types vs. actual types

**Lens:** the *type contract* of `normalize_phone` (`dedup.py:33`) against
`BusinessRecord` (`scrapers/base.py:5`). Numbering-plan correctness is already covered by
001/055/084/085 and is **not** re-derived here except where it changes the *shape* of the
contract. Every runtime claim below was executed offline against this checkout (no network,
no source modified; `python3 -B` so no bytecode was written).

## Verdict

The declared signature `normalize_phone(phone: str, country: str = "LB") -> str`
(`dedup.py:33`) is wrong on both sides and is enforced by nothing. All three call sites
in the repo pass a `str | None` for `country` (`dedup.py:205`, `dedup.py:228`,
`main.py:194`), and the return type `str` cannot express the two-state result — *usable
dedup key* vs. *unusable* — that three different call sites silently depend on. The `""`
sentinel that papers over this is **load-bearing and unenforced**: I replaced it with any
other truthy placeholder and three unrelated businesses collapsed into one row. Meanwhile
`mypy strict = true` sits configured at `pyproject.toml:64-68` and is never run by
`.github/workflows/scrape.yml`, so the fiction is never checked.

---

## Findings

### S1 — `country` is declared `str`; all three call sites pass `str | None`; unknown values are silently reinterpreted as Lebanon

- **Where:** `dedup.py:33` (signature), `dedup.py:48` (`_COUNTRY_CODES.get(country, "961")`),
  `dedup.py:205`, `dedup.py:228`, `main.py:194`, `scrapers/base.py:10`
  (`country: str | None`).
- **Breaks:** there are **two country normalizers in one pipeline with different
  alphabets**, and they run in the wrong order relative to the work that depends on them.
  `resolve_country` (`main.py:50`) canonicalises `sa`, `SA `, `leb`, `LBN`, `ksa`, `SAU`,
  `SAU-AR` → `LB`/`SA` at `main.py:61-67`. `normalize_phone` accepts **only** the exact
  strings `LB` and `SA` (`dedup.py:23-26`, `dedup.py:48`). `dedup()` runs at `main.py:180`,
  *before* `resolve_country` at `main.py:184` — so the **dedup key is computed from the raw
  CSV cell** and the **stored phone from the canonical value**. Two spellings of one country
  therefore produce two different keys for one business in one run, and the affected rows
  then migrate identity between runs.
- **Trigger (verified end-to-end).** Master row
  `{"country": "sa", "phone": "0112345678", "name": "Al Yamama Auto Repair"}` keyed as
  `normalize_phone("0112345678", "sa")` → **`+961112345678`**. A same-run Google row
  `{"country": "SA", "phone": "+966112345678", "name": "Al Yamama Auto Repair"}` keyed
  `+966112345678`. `dedup([...])` returns **2 records**. `main.py:184` then rewrites
  `country` to `"SA"` and `main.py:194` rewrites `phone` to `+966112345678` for *both*.
  Next run reloads from `all_businesses.csv` and `dedup` returns **1 record**. Nothing logs
  the transition. `all_businesses.csv` from run N ships a duplicate lead for the same
  business; the duplicate heals one run later, or never if run N is the last export.
- **Trigger (second, worse).** `country = None` — which `load_master` produces for **every
  blank cell** (`main.py:87-89`) and which `main.py:53-57` states already exists in shipped
  masters — with `phone = "0112345678"` (Saudi national-trunk form) gives key **and** stored
  value `+961112345678`: a Saudi landline permanently written into the master as a Lebanese
  number. Note that `resolve_country`'s phone sniff (`main.py:68-73`) only recognises
  `+966` / `00966` and never the `011…` national-trunk form that `normalize_phone` itself
  parses perfectly well at `dedup.py:63`. **Two functions in this repo disagree about what a
  Saudi phone number looks like**, and neither defers to the other.
- **Why `country=None` is not hypothetical.** Google documents
  `Place.nationalPhoneNumber` as *"A human-readable phone number in **national format**"*
  (Places API `Place` schema, `nationalPhoneNumber: string, example: "+1 650-253-0000"`) —
  explicitly **not** E.164, with no length guarantee and no promise that it carries a
  country code. `google_places.py:289` stores that string verbatim in `phone`. Every Google
  row therefore depends entirely on a correct, canonical `country`, and
  `google_places.py:127-136` derives it from **query keywords**
  (`_country_from_query`, defaulting to `LB` at `:136`) — one query-wording change silently
  moves a whole batch of rows onto the wrong code.
- **Fix:** change the signature to `normalize_phone(phone: str | None, country: str | None)
  -> str | None`, **remove the `, "961"` default** so an unrecognised country is a hard
  failure rather than a mislabel, and route `dedup.py:205`/`:228` through the same
  canonicaliser `main.py:184` uses (or take a `CountryCode` `Literal["LB","SA"]`).

### S1 — `str` cannot express the two-state result; the `""` sentinel is load-bearing and unenforced

- **Where:** `dedup.py:33-63` (docstring at `:34-39` promises *"Normalize to E.164, or return
  `""`"*); consumed at `dedup.py:206`, `dedup.py:228`, `main.py:194`.
- **Breaks:** one `str`-returning function discharges **three incompatible consumption
  contracts**: a truthiness flag (`dedup.py:206`), a set/dict membership test
  (`dedup.py:228`), and a field assignment into a `BusinessRecord` whose `phone` is
  `str | None` (`base.py:13`, `main.py:194`). Only the `None` half of that annotation is
  ever reachable — `""` is the sentinel — so the schema and the implementation disagree
  about what "absent" means. The docstring says the `""` return exists precisely because
  *"Junk used to normalize to `+`, which made unrelated businesses share one dedup key"*
  (`dedup.py:36-38`). **That fix is one plausible refactor away from reverting.**
- **Trigger (verified by monkeypatching the sentinel only, source untouched).** With
  `["1234", "ext 4", "n/a"]` on three unrelated rows
  (`Beirut Dental A / Tripoli Spares B / Sidon Bakery C`):
  - as shipped (`""` for junk) → **3 records**;
  - with `return value or "UNKNOWN"` → **1 record** (`Tripoli Auto Spares B`).
  Every junk-phone row in the corpus chains into one row via `dedup.py:208`, with the
  surviving name chosen by the `str(value)` tiebreak at `dedup.py:184` — i.e. the
  lexicographically largest name, not the best record. `docs/audits/088-SYNTH-migration-order.md:202`
  warns to "keep the `normalize_phone` `""`-return invariant"; this is the proof of why, and
  it is currently guarded only by a test (`tests/test_lead_signal.py:102-108`) that asserts
  a *value*, not the *invariant*.
- **Second structural break, same root cause:** `dedup.py:229`'s `continue` is
  **unreachable today**, provably. A record only reaches `name_index` either with a falsy
  `phone` (`dedup.py:205` guard) or with one that normalised to `""`; and `""` can never be a
  key in `phone_index` because `dedup.py:206` guards on truthiness. Verified: junk-phone rows
  are always kept. The moment junk becomes a reachable non-empty key, that `continue` starts
  **silently deleting records** with no log line and no counter.
- **Fix:** return `str | None` and test `is not None`; better, return a frozen
  `PhoneKey` dataclass carrying `.e164: str` and `.usable: bool` so "unusable" is not
  expressible as a plausible-looking string. Add a test that asserts the *count* of
  `dedup()` output across a junk corpus, so any sentinel change fails loudly.

### S1 — `""` is written into `phone` (`base.py:13` declares `str | None`), and `completeness_score` was already computed against the pre-`""` value

- **Where:** `dedup.py:41`, `dedup.py:46` (`return ""`); `main.py:192-194`;
  `enricher.py:103`; `enricher.py:349`; `main.py:187`; `main.py:206`;
  `scrapers/base.py:13`.
- **Breaks:** an ordering problem that only exists because the sentinel is a *string*
  rather than an absence. `main.py:187` calls `enrich()`, which sets
  `completeness_score` at `enricher.py:349` using `if record.get("phone"):`
  (`enricher.py:103`) — bare truthiness. `main.py:194` then overwrites `phone` with
  `normalize_phone`'s result. `""` is truthy to `enricher.py:103` but carries no digits.
  `completeness_score` is never recomputed. The result ships in
  `qualified_businesses.csv`, whose whole membership rule is `completeness_score >= 1`
  (`main.py:206`).
- **Trigger (verified, `enricher.py` + `main.py` called in the real order):**
  `phone = "1234"` (or `"ext 4"`, `"0"`, `"n/a"`) → `completeness_score` ≥ 1 →
  `qualified=True`, and the exported row has an **empty phone column**,
  `has_contact=False` (`main.py:142`), and no phone credit in `lead_score`
  (`enricher.py:292`). A row that qualifies for the "qualified leads" file purely on a
  phone number that the same run deleted.
- **Compounding effect across runs (verified):** `write_csv` writes the empty cell
  (`main.py:124-127`); `load_master` turns `""` into `None` (`main.py:87-89`). Because
  `all_businesses.csv` is the **cumulative master** reloaded at `main.py:150`, the original
  raw value (`"1234"`, `"ext 4"`) is destroyed from disk on the first run that touches it and
  is unrecoverable thereafter. The declared type `str | None` cannot distinguish
  *present-but-unusable* from *absent*, so `""` was always the only representable choice —
  and it is the wrong one.
- **Note on overlap:** `docs/audits/086-SYNTH-data-contract.md:23-25,109` already flags the
  *predicate* problem ("`qualified` can use a phone later normalized to empty"). The type-level
  mechanism is what's missing: the fix 086 implies (`""` → `None`) is only sound **once the
  sentinel is `None`**, which is the S1 finding above. Do them together or neither.
- **Fix:** `r["phone"] = normalize_phone(...) or None` at `main.py:194`, and recompute
  `completeness_score` in the same loop (or move the phone normalisation *before*
  `enrich()`).

### S2 — `"0000000"` → `"+961"`: the shared key is still there, and the corruption is not idempotent, so the row changes dedup key class between runs

- **Where:** `dedup.py:30` (`_MIN_DIGITS = 7`), `dedup.py:45` (length checked **before** any
  stripping), `dedup.py:50-52` (`00` strip), `dedup.py:63` (`digits.lstrip("0")`).
- **Breaks:** `_MIN_DIGITS` is checked on the raw digit string, *before* `00` is removed and
  *before* `lstrip("0")` collapses an all-zero string to `""`. Line 63 then happily returns
  `"+" + cc` — the **bare country code**, a key with zero subscriber digits that every
  zero-filled number in the corpus shares. `084-SYNTH-adversarial-fix-review.md:31-41`
  flagged the collapse; what is new here is the **multi-run behaviour**.
- **Trigger (verified):**
  | input | country | result |
  |---|---|---|
  | `"0000000"` | `LB` | `"+961"` |
  | `"00000000"` | `LB` | `"+961"` |
  | `"000 000 0000"` | `SA` | `"+966"` |
  | `"0" * 12` | `SA` | `"+966"` |
  `dedup([...3 unrelated rows with these phones...])` → **1 record**, survivor
  `Tripoli Auto Spares B` (the `str()` tiebreak at `dedup.py:184`, not a quality rule).
  Tests at `tests/test_lead_signal.py:106` cover `"---", "...", "  ", "n/a", "ext 4"` —
  **they omit `"0000000"`**, so CI would stay green.
- **The new part — key class changes between runs (verified).**
  `normalize_phone("0000000", "LB")` → `"+961"`, but `normalize_phone("+961", "LB")` →
  `""`. **`f(f(x)) != f(x)`: the function is not idempotent** for exactly the inputs that
  produce its worst output. `"+961"` is non-empty so it survives `write_csv`/`load_master`
  verbatim into `all_businesses.csv`. On the next run `dedup.py:205` computes `""`, the row
  falls out of `phone_index` into `name_index` (`dedup.py:211-218`), and from then on it is
  matched on `(normalize_name(name), city)` instead of on phone. **The same CSV row is
  matched by a different identity strategy on consecutive runs**, silently changing which
  other rows it merges with. `docs/audits/031-test-suite-design.md:467` already proposes an
  idempotency property test; it has not been written, and this is what it would catch.
- **Fix:** validate `digits` **after** stripping and before formatting — reject when the NSN
  is empty, and cap total length at 15 (S3 below).

### S2 — `str(phone)` at `dedup.py:42` silently swallows a `BusinessRecord` schema violation

- **Where:** `dedup.py:42`; `scrapers/base.py:13` (`phone: str | None`);
  `dedup.py:205` and `main.py:192` both gate on **truthiness** (`if raw_phone`), not on type.
- **Breaks:** `raw = str(phone)` accepts anything, so no caller ever has an exception path to
  handle and no caller ever learns the schema was violated. Verified:
  `normalize_phone(12345678901234, "LB")` → `'+96112345678901234'` (no error);
  `normalize_phone(None, "LB")`, `normalize_phone(0, "LB")`, `normalize_phone(True, "LB")`
  all → `''`. A record whose `phone` is a float, an `int`, or `0` is normalised and stored
  without complaint.
- **Why it is invisible here:** `dedup(records: list[dict]) -> list[dict]` (`dedup.py:197`)
  **erases `BusinessRecord` at the first stage boundary**. `main.py:179-180` passes
  `raw_filtered` (typed `list[BusinessRecord]`) and `master` (untyped `list[dict]` from
  `load_master`, `main.py:77`) into one `list[dict]`, and the return is `list[dict]`
  (`dedup.py:222-232`). From `main.py:183` onward everything is an untyped `dict`, so no type
  checker can see a shape mismatch — including the two `BusinessRecord` keys written by
  `enricher.py:340-348` that were never in the `TypedDict`'s required set in the first place
  (see S3).
- **Fix:** drop `str(phone)` and let the annotation be the check, or narrow explicitly with
  `isinstance` and route non-conforming rows to a quarantine list with a counter.

### S2 — Five consumption sites, one `str`, two structural breakages, five length rules

The lens asked for every place the return value is used in a way that breaks on an unexpected
shape. All five:

1. **`dedup.py:206` `if key:`** — *truthiness*. Any non-empty placeholder re-creates the
   historical mass-merge bug the docstring at `dedup.py:36-38` is about.
2. **`dedup.py:206-210` `phone_index[key]`** — *hashability*. A plausible refactor
   returning a parsed `(cc, nsn, ext)` **list** or **dict** raises
   `TypeError: unhashable type` at `dedup.py:210` and kills the run mid-dedup.
3. **`dedup.py:223, 228` `... in captured_phones`** — *hashability*, same requirement. Note
   this is a **third** re-derivation of the key from the merged record
   (`dedup.py:205`, `:228`, and `main.py:194`), each reading `record.get("country")`
   independently.
4. **`main.py:194` `r["phone"] = ...`** — *assignment into a typed field*. Only the `str` half
   of `base.py:13`'s `str | None` is reachable.
5. **`main.py:138` / `enricher.py:285`** — the verdict is **not consumed at all**; both
   re-derive their own length rule from the stored string.

That last point is its own finding: **five independent copies of the phone-length rule now
exist**, none agreeing on an upper bound.

| # | Location | Rule |
|---|---|---|
| 1 | `dedup.py:30` | `_MIN_DIGITS = 7`, on pre-strip digits |
| 2 | `main.py:142` | `len(phone) >= 7` on post-`\D`-strip digits |
| 3 | `enricher.py:292` | `len(phone) >= 7`, same rule, different module |
| 4 | `enricher.py:133` | `\d{7,15}` in `_WHATSAPP_RE` |
| 5 | `enricher.py:103` | bare truthiness — the only one that disagrees with the others on `"1234"` |

- **Fix:** one `PhoneKey` type consumed everywhere; delete rules 2-5 and read the verdict.

### S3 — "E.164" in the docstring (`dedup.py:34`) is an unbounded, unvalidated claim

- **Where:** `dedup.py:34`, `dedup.py:44`, `dedup.py:63`.
- **Breaks:** ITU-T E.164 §6.1 and Annex A.3.1.1 cap an international E.164 number at **15
  digits**; §7.2.1 caps the national significant number at **15 − len(country code)** = 12
  for Lebanon and Saudi Arabia. `normalize_phone` enforces neither, and `_MIN_DIGITS`
  (`dedup.py:30`) is a *lower* bound only. Verified:
  `normalize_phone("+961" + "1" * 22, "LB")` returns a **26-character** string, which is
  written straight into the master CSV and then into `sales_ready.csv`.
- **Fix:** reject `len(digits) > 15` and per-country NSN bounds, or replace the function
  wholesale with `phonenumbers`, which is **already declared** as an optional dependency at
  `pyproject.toml:30`.

### S3 — `phone` and `country` are co-determined, and nothing records which one won

- **Where:** `main.py:68-73` (`resolve_country` sniffs `phone` for `+966`/`00966`),
  `main.py:184` (`country` ← `resolve_country`), `main.py:194` (`phone` ← `normalize_phone`
  *using* `country`), `dedup.py:205`/`:228` (`country` read raw, pre-canonicalisation).
- **Breaks:** the dependency is circular — `country` is inferred from `phone`, then `phone` is
  normalised with `country` — and the resolution direction is discarded. There is no
  `country_source` / `phone_source` column, so no later audit can tell whether a number
  overrode a country label or a country label overrode a number. `086-SYNTH-data-contract.md:38`
  states `country` should be `LB`/`SA` after `resolve_country`; that is true of the CSV but
  **not** of the dedup key that produced it (S1 above).
- **Also here:** `main.py:194` uses `r.get("country") or _DEFAULT_COUNTRY`, but
  `r["country"]` was unconditionally overwritten at `main.py:184` two lines earlier and is
  always truthy. The `or` is dead code that reads as a safety net.
- **And the TypedDict's own optionality is wrong:** `scrapers/base.py:5` declares no
  `total=False`, so all keys are required — verified
  `BusinessRecord.__required_keys__` has **23** entries — yet `enricher.py:340-348` fills nine
  of them with `setdefault`, which only makes sense if they can be absent. The schema says
  required where the data is optional.
- **Fix:** add `total=False` (or split required vs. derived), and record the resolution
  direction.

---

## Not a bug, but worth knowing

- **`normalize_phone` never raises and always returns `str`.** Verified for `None`, `0`,
  `True` and an `int`. `-> str` is technically honoured. The whole contract failure is in
  the *meaning* of the returned value, not its Python type — which is exactly why no
  type checker would ever have caught it.
- **`_pick`'s country tiebreak cannot invalidate a phone key.** I tried to prove this as a
  bug and it is not one, so nobody re-derives it: if two records produce the same key, then
  either both numbers self-identify (the key is country-independent, `dedup.py:58-60`), or
  their effective `cc` matched (so `_pick` picking the other country's *label* still
  re-derives the identical key). Verified: `{"country":"LB","phone":"0112345678"}` +
  `{"country":"SA","phone":"961112345678"}` both key `+961112345678`, merge into one record,
  and `main.py:184`/`:194` re-key it to `+961112345678`. `_pick`'s country tiebreak *is*
  `max(str)` (`dedup.py:184`, `:187`) — `LB` vs `SA` → `SA`, verified — which is arbitrary,
  but harmless for phone keys.
- **`dedup.py:229` `continue` is unreachable today**, provably and for the reason given in
  the S1 above. It is dead code whose liveness is a property of the `""` sentinel.
- **Google's field is documented as human-readable national format, not E.164** — Places API
  `Place.nationalPhoneNumber` schema: *"A human-readable phone number in national format"*,
  `example: "+1 650-253-0000"`. This is the external reason the S1 `country` finding is a
  live S1 rather than a theoretical one.
- **A zero-filled phone is indistinguishable from a real one at the type level.** `"0000000"`
  and `"+961112345678"` are both `str`, and `normalize_phone` returns `str` for both. No
  amount of annotation on the current signature fixes that; the result type itself must
  change.
- **Scope.** This report is the *contract*. Trunk-zero retention, extension-digit
  concatenation, `_KNOWN_COUNTRY_CODES` ordering and the `"20"`-Egypt collision on Achrafieh
  landlines are covered by `001`, `055`, `084` and `085`; I did not re-derive them except
  where a numbering-plan error changes the contract's *shape* (S2 `+961`, S3 length bound).
- **Count correction.** `docs/audits/BRIEF.md:57-62` says "22 columns" and enumerates 23.
  `scrapers/base.py:5-28` declares 23 keys and `main.py:33-40` has 23 names; verified
  `set(BusinessRecord.__annotations__) == set(main.FIELDS)` is `True`, so there is **no
  schema gap** — only a wrong number in the brief. (`086` reached the same conclusion.)
- **`mypy`, `ruff` and `pytest` are configured but never run.** `pyproject.toml:20-28`
  declares all three as dev extras, `pyproject.toml:64-68` sets `strict = true`, and
  `tests/test_lead_signal.py:4` says *"no pytest needed"* — but `.github/workflows/scrape.yml:38-39`
  installs only `requirements.txt`. Under `strict = true` today, mypy would flag
  `dedup.py:205`, `dedup.py:228` and `main.py:194` on argument types alone.

## Recommended order of work

1. **Stop the two country normalizers disagreeing** (S1, first finding). Route every caller
   through `resolve_country` — or a `CountryCode` `Literal["LB","SA"]` — *before*
   `normalize_phone`, change the parameter to `str | None`, and **delete the `, "961"`
   default** so an unknown country is loud. This is what removes both the duplicate-lead rows
   and the Saudi-number-written-as-Lebanese rows.
2. **Make the two-state result representable** (S1, second and third findings). Return
   `str | None`, test `is not None`, write `or None` at `main.py:194`, and recompute
   `completeness_score` after the rewrite so `qualified_businesses.csv` is true of the row as
   exported. Land the "sentinel swap" regression test from this report alongside it.
3. **Validate before formatting** (S2, fourth finding). Reject an empty NSN and anything over
   15 digits *after* stripping; that alone closes the `+961` shared key, the non-idempotency,
   and the run-to-run key-class change.
4. **Then replace the function outright with `phonenumbers`** (already an optional dep at
   `pyproject.toml:30`) and return a `PhoneKey` carrying both the E.164 string and an
   explicit `usable` flag, so all five consumption contracts share one definition and the
   four duplicate length rules in `main.py`/`enricher.py` can be deleted.
5. **Turn on `mypy`** (configured `strict` at `pyproject.toml:64-68`, never invoked). It is
   the cheapest available contract test for this file and would have flagged all three
   argument-type violations on day one.