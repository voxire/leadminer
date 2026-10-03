# 044 — Refactor `main()` into a Typed Pipeline

## Verdict

`main()` currently owns the entire run lifecycle: master-file IO, concurrent scraping, filtering, deduplication, enrichment, classification/scoring, export, and reporting (`main.py:93-182`). Replace it with a small composition root and explicit typed stages, timed centrally by the runner. The first behavior fix should be to assign industry priority before calculating `lead_score` and make exactly one stage responsible for the final score: `enrich()` currently scores before priority exists (`enricher.py:331-342`), and `main()` assigns priority later and scores again (`main.py:131-138`).

## Findings

### S1 — Score only after all score inputs are finalized
- **Where:** `enricher.py:331-342`; `main.py:131-138`; scoring reads `industry_priority` at `enricher.py:297-302`.
- **Breaks:** `enrich()` writes an initial score while `industry_priority` is unset or stale. In the normal main path the second calculation overwrites it, so the duplicate work is mostly hidden; any caller using `enrich()` directly gets a different score, and the ordering makes the intended source of truth unclear. The second score also masks the first-stage defect rather than fixing the ownership boundary.
- **Trigger:** A high-priority restaurant with no existing `industry_priority` enters `enrich()`: its first score omits the high-priority points. `main()` later sets `industry_priority="high"` and recomputes, producing a different value.
- **Fix:** Make enrichment responsible for inferred region, website/contact data, defaults, and completeness only; calculate `industry_priority`, then `recommended_service`, then `lead_score` in one classification/scoring stage after enrichment.

### S2 — The orchestration function mixes policies with side effects
- **Where:** `main.py:94-113` (master IO and scraper lifecycle), `main.py:117-137` (business transformations), `main.py:139-182` (partitioning, exports, and summaries).
- **Breaks:** The main path is difficult to test without patching globals and running multiple concerns together. The three scraper errors are caught and printed (`main.py:105-114`), but are not retained as structured run data for a final report; export and summary logic likewise depend on local variables created earlier in the function.
- **Trigger:** To test the sales-ready predicate or region summary, a test must currently exercise or extract logic from `main()`; to test a scraper failure's effect on the run report, there is no result object carrying source outcomes.
- **Fix:** Make `main()` construct dependencies and invoke a typed pipeline; move transformation, partition, export, and report behavior into small stages with explicit input/output values.

### S3 — Stage duration and outcome are not observable as a run-level record
- **Where:** `main.py:105-114`, `main.py:128-182`.
- **Breaks:** Progress prints report some counts, but there is no uniform per-stage elapsed time or structured outcome to identify whether a slow run is in scraping, website checks, CSV writes, or report generation.
- **Trigger:** A run spending most of its time in website requests has no comparable stage duration against scraper acquisition or export.
- **Fix:** Time every stage in the pipeline runner with an injectable monotonic clock, and attach stage name, elapsed duration, and success/failure to the run result.

## Proposed contract

Keep `BusinessRecord` as the shared record shape initially, but make its lifecycle explicit. `scrapers/base.py:5-28` currently declares all 22 fields as required even though scraper outputs and early pipeline records can be partial (`main.py:99-103`). Before using it as a stage boundary, define an input/partial record type (e.g. `BusinessInput`, `TypedDict(total=False)`) and a normalized `BusinessRecord` whose required fields are guaranteed after enrichment. This avoids claiming that a raw scrape already has `lead_score`, `completeness_score`, or all optional contact fields.

Use a generic stage protocol and return a value rather than mutating implicit runner state:

```python
InT = TypeVar("InT")
OutT = TypeVar("OutT")

class Stage(Protocol[InT, OutT]):
    name: str
    def run(self, data: InT, context: RunContext) -> OutT: ...

@dataclass
class RunContext:
    config: RunConfig                 # paths, website worker count, run options
    ports: RunPorts                   # scraper factory, master store, checker, writer
    clock: Clock                      # monotonic_ns callable, injectable in tests
    timings: list[StageTiming]        # runner-owned timing records
    logger: Logger
```

`RunPorts` should be narrow injected interfaces, not module globals: `MasterStore.load()/save` (or initially just `load`), `Scraper.scrape()`, `WebsiteChecker.check(records)`, and `CsvWriter.write(path, records)`. Production adapters can wrap the existing functions/classes; tests provide in-memory fakes. Keep `RunContext` for configuration and infrastructure only. Stage data belongs in typed inputs/outputs, not arbitrary keys in a mutable context dictionary.

The runner should measure each call with `clock.monotonic_ns()` in a `try/finally`, append `StageTiming(name, elapsed_ms, outcome)` on both success and failure, then re-raise failures unless a stage's contract explicitly represents a partial result. Use a fake clock in tests; don't put timing code inside business stages. The acquisition stage can represent each source as `SourceOutcome(name, records, error)` so current best-effort scraper behavior remains explicit and reportable rather than being reduced to stderr text.

## Stage graph and interfaces

Run these stages in order; each `run(input, context) -> output` is independently callable. Wrapper dataclasses make the transitions and side information visible without repeatedly passing bare `list[dict]` values.

| Stage | Typed boundary | Responsibility |
|---|---|---|
| `AcquireStage` | `None -> AcquiredData` | Load the existing master and run the three scraper adapters concurrently. Return `master_records` plus per-source records/errors and counts. Keep scraper partial-failure policy here. |
| `WhitelistStage` | `AcquiredData -> FilteredData` | Apply `is_business_category()` to newly scraped rows only; pass master rows through unchanged, matching `main.py:117-125` where existing master data bypasses the whitelist. |
| `DeduplicateStage` | `FilteredData -> DeduplicatedData` | Combine filtered new rows with master rows, then call `dedup()` once, preserving current accumulation semantics (`main.py:123-126`). |
| `EnrichStage` | `DeduplicatedData -> EnrichedData` | Infer missing regions, check websites/extract contacts, set nullable defaults, and compute completeness. Do **not** assign industry priority, recommended service, or lead score. |
| `ClassifyAndScoreStage` | `EnrichedData -> ScoredData` | In order: set `industry_priority`; compute `recommended_service` using the enriched fields and completeness; compute `lead_score` exactly once after priority and website status are final. |
| `PartitionStage` | `ScoredData -> ExportSets` | Build master, qualified, with-website, without-website, and sales-ready sets, plus social subset and summary counts. Keep the current predicates from `main.py:139-146` explicit and unit-testable. |
| `ExportStage` | `ExportSets -> ExportedData` | Ensure the output directory exists and write the five CSVs. Return paths and row counts rather than printing from `write_csv` as its only observable result. |
| `ReportStage` | `ExportedData -> RunSummary` | Render the user-facing summary (live/dead/unknown, region counts, sales-ready service counts, source outcomes, stage timings). A text reporter can preserve today's console output. |

Suggested data wrappers: `AcquiredData(master_records, sources)`, `FilteredData(master_records, new_records, filter_counts, sources)`, `DeduplicatedData(records, counts, sources)`, `EnrichedData(records, counts, sources)`, `ScoredData(records, sources)`, `ExportSets(all_records, qualified, with_websites, without_websites, with_social, sales_ready, counts, sources)`, and `ExportedData(paths_and_counts, export_sets, sources)`. For a first incremental refactor, wrappers can hold `list[BusinessRecord]`; the important contract is that each stage receives and returns a named value with its lifecycle guarantees documented.

### Required ordering invariant

`EnrichStage` must finish website/contact extraction and completeness before classification, because `recommend_service()` reads `website_live` and `completeness_score` (`pitch_recommender.py:31-53, 68-74`). Within `ClassifyAndScoreStage`, assign `industry_priority` before calling `lead_score()`, since the scorer reads it (`enricher.py:297-302`). Remove the score assignment from `enrich()` (`enricher.py:342`) and remove the duplicate call in the current loop (`main.py:137`) as part of that ownership change. No intermediate stage should emit a `ScoredData` value with a stale or missing score.

## Target module layout

```text
main.py                         # thin entry point: config + adapters + Pipeline.run()
pipeline/
  __init__.py
  contracts.py                  # RunContext, RunConfig, ports, Stage protocol, timings
  models.py                     # lifecycle TypedDicts/dataclasses and stage wrappers
  runner.py                     # ordered stage execution, timing, failure propagation
  stages/
    __init__.py
    acquire.py                  # master load + scraper fan-out/source outcomes
    filter.py                   # whitelist new scrape records
    deduplicate.py               # merge existing + filtered rows
    enrich.py                    # inference, website/contact enrichment, completeness
    classify.py                  # industry priority, service recommendation, one score
    partition.py                 # five export sets and aggregates
    export.py                    # CSV write adapter / output manifest
    report.py                    # console/report rendering from RunSummary
  adapters/
    csv_store.py                 # master CSV load and CSV writer
    scraper_factory.py           # existing scraper construction
    website_checker.py           # adapter around existing enrichment HTTP checker
```

This is a target, not a requirement to move every helper in one change. Keep scraper implementations, `dedup.py`, `enricher.py`, and `pitch_recommender.py` as domain modules initially; stages should call them through narrow adapters. Avoid a large all-at-once rewrite of their policies while extracting orchestration.

## Independent test plan

1. Unit-test pure stages with in-memory records: whitelist preserves master rows, dedup gets the combined filtered+master set, classification assigns priority before the single score call, and partitions match existing thresholds/predicates.
2. Unit-test IO stages with fakes: acquisition captures source errors without network, enrichment uses a fake website checker, and export records five requested paths/row sets without writing the real `data/` directory.
3. Test the runner with tiny fake stages and a deterministic clock: assert stage order, recorded durations on success/failure, and that a raised fatal stage stops later stages.
4. Add one offline end-to-end pipeline test with fixture rows and fake adapters. Assert one final score calculation per output record and confirm a high-priority lead's score includes industry points.

## Recommended order of work

1. Add the lifecycle data contracts and `RunContext`/ports without changing run behavior; type the raw scraper/master boundary as partial records.
2. Extract pure filter, classify/score, partition, and summary functions. Fix score ownership and ordering now; assert the scoring function is invoked once per record.
3. Extract the timed runner and stages, then wire existing scraper/enrichment/CSV functions through adapters. Preserve existing CSV names, columns, and filter semantics.
4. Add fake-adapter tests and compare a fixture run's five output sets and summary against current behavior, allowing only the intended final-score correction.
5. Leave CLI/config expansion, retries, persistence changes, and unrelated policy redesign for separate work; they are not prerequisites to making this pipeline typed, timed, and independently testable.
