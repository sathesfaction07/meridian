# LifecycleTracker — Technical Architecture

## Overview

Consolidates product lifecycle events from 16 regional feeds into one timeline per product, rendered as a text gantt, a PNG chart, JSON Lines, or CSV.

**Design principle:** an instant is the only thing you may compare or subtract; a local wall-clock reading is something you may only display. Every module here is arranged around keeping those two separate, because conflating them produces output that is wrong and looks right.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        CLI (lifecycle-tracker)                                │
│                                                                              │
│  show        text gantt + per-region table + DST notes + conflicts           │
│              exit 1 on any unparsed row OR any conflict                      │
│  jsonl       one object per line; stdout by default so `| jq` works,         │
│              diagnostics to stderr                                          │
│  export-all  events.csv, spans.csv, conflicts.csv, report.json               │
│  regions     all 16 with JANUARY AND JULY offsets -- one column would hide   │
│              every southern-hemisphere region's inverted phase               │
│  resolve     how one timestamp maps onto an instant. exit 1 if ambiguous     │
│              or nonexistent                                                 │
│  doctor      config, region coverage, whether the feeds parse                │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                             INGEST (ingest/)                                  │
│                                                                              │
│  parse.py    the data quality problem lives here                             │
│    four accepted shapes:                                                     │
│      2025-03-09T02:30:00-05:00   explicit offset -> OFFSET_GIVEN             │
│      2025-03-09T02:30:00Z        also Z or z                                 │
│      2025-03-09T02:30:00         naive -> local to the region's zone         │
│      2025-03-09                  date only -> local MIDNIGHT                 │
│    a space instead of a T is accepted (every spreadsheet export does it)      │
│                                                                              │
│    _resolve_naive() classifies the two DST edge cases:                        │
│      fold=0 and fold=1 offsets AGREE            -> EXACT                     │
│      they differ AND a round trip moves the clock -> NONEXISTENT             │
│      they differ AND a round trip preserves it  -> AMBIGUOUS                 │
│      ambiguous resolves to the FIRST occurrence, so a duration can only      │
│        be under-stated, and the merge stays deterministic                     │
│                                                                              │
│    an unreadable timestamp RAISES -- an invented time in a timeline is        │
│      worse than a gap                                                        │
│    column aliases (product/sku/id, marketplace/locale, event_type, ...)       │
│      `instant` is in the timestamp list because it is what WE export         │
│    kind aliases (published/live -> listed, blocked -> suppressed, ...)        │
│                                                                              │
│  feeds.py    one file per region, or a combined export -- region is a column │
│    a bad ROW is collected and reported with file:line                        │
│    a bad FILE raises                                                         │
│    utf-8-sig, because a spreadsheet BOM otherwise hides product_id           │
│    .csv .tsv .txt .json .jsonl .ndjson; a JSON array and one-object-per-line │
│      are told apart by content, not by extension                             │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              CORE (core/)                                     │
│                                                                              │
│  merge.py    three steps, in this order, and the order matters               │
│    1. deduplicate  keyed on (product, region, kind, INSTANT), so the same     │
│                    event from a regional and a global feed collapses          │
│    2. order        by (instant, region, kind) -- the tiebreakers stop two     │
│                    runs producing two different timelines                     │
│    3. build spans  PER REGION                                                │
│                                                                              │
│  spans.py    build_spans() takes ONE region and RAISES on more than one      │
│    the failure it prevents is silent: a Tokyo event closing a Mexico City    │
│    span produces contiguous spans, sensible durations, a chart that renders  │
│    -- describing a history that never happened                              │
│                                                                              │
│    every event opens a span and closes the previous one                       │
│    the LAST span is left OPEN (end=None), distinct from zero-length          │
│    conflicts are FLAGGED and the offending event is still APPLIED            │
│      no_creation_event | event_after_terminal | redundant_transition          │
│      reinstated_without_suppression | simultaneous_events                     │
│      single_event_region (cross-region, in merge.py)                          │
│                                                                              │
│    state_at() is half-open on the right, so a boundary instant belongs to    │
│      the later span rather than matching two                                  │
└───────┬──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                            RENDER (render/)                                   │
│                                                                              │
│  text.py     the one that can be tested                                      │
│    buckets are equal slices of the INSTANT range, so the 16 rows line up     │
│      vertically; slicing by local time gives each row its own axis           │
│    each bucket samples its MIDPOINT -- sampling the left edge makes a state  │
│      that begins mid-bucket disappear                                        │
│    axis labelled UTC, and says so: 16 regions have no single local clock     │
│    an open final span fills to the edge and the row is marked `>`            │
│    summary_table marks still-accruing time with `+`: see below               │
│                                                                              │
│  chart.py    matplotlib, OPTIONAL, deferred import, Agg backend              │
│    bars positioned by instant; open spans hatched at reduced opacity          │
│    a zero-length span is drawn as a tick rather than being invisible          │
│                                                                              │
│  export.py   JSON Lines for piping, CSV for spreadsheets, shared field lists │
│    every timestamp written TWICE: instant + local reading                     │
│    an open span writes end=null / blank, NOT the string "None", and           │
│      duration=null NOT 0 -- 0 is a real zero-length span                      │
│    all three CSVs written even when empty: a missing file cannot be told      │
│      apart from a run that failed part way                                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Regions (`regions.py`)

Sixteen regions with IANA zones, chosen to span the awkward cases rather than to be tidy. The properties that matter are asserted by tests that **derive them from tzdata** rather than trusting the docstring:

| Property | Regions | Why it matters |
|:--|:--|:--|
| half-hour offset | `in` (+05:30) | an integer hour count truncates it to +5 |
| southern hemisphere | `au`, `br` | opposite DST phase; Sydney is +11 in January, +10 in July |
| no DST at all | `mx`, `br`, `jp`, `sg`, `ae`, `in` | six of sixteen never move |
| one instant, two local readings | `uk`/`ie` vs `de`/`es` | 01:00 UTC on 2025-03-30 is 02:00 in London, 03:00 in Berlin |

The widest simultaneous spread is **17 hours** (`au` +11 against `mx` −06), derived by a test. That is more than enough for two events to appear in the wrong order if offsets are dropped.

`canonical_code()` raises on an unknown code rather than defaulting, because guessing a region means guessing a timezone and that is worth up to 17 hours of error. Eight aliases older feeds still emit (`usa`, `gb`, `uae`, `eu-central`, `apac-south`, …) are mapped as data, since a historical export cannot be rewritten and dropping its rows loses events.

### 2. Models (`models.py`)

`Event` carries `instant` (UTC, validated), `local` (naive, display only), and `resolution`. See ADR-001.

`STATE_AFTER` maps each `EventKind` to exactly one `State` — a table rather than a chain of `if`s, so adding a kind is a one-line data change and exhaustiveness is testable. There is a test asserting every `EventKind` appears.

`Span` has `start`, optional `end`, `state`, and `opened_by`. `duration` returns `None` for an open span rather than zero, because zero is a real value that a pair of simultaneous events produces.

`Timeline.time_in_state` counts closed spans only. See ADR-002 for the reporting trap that creates and how it is defused.

### 3. Parsing (`ingest/parse.py`)

The DST classifier is the heart of it. See ADR-001.

Worth noting separately: `parse_timestamp` **raises** on input it cannot read, while `load_file` **collects** row failures. Those are different decisions. An unreadable timestamp must not become an event at an invented time; but one bad row out of forty thousand should not cost you the other 39,999.

`utc_offset_hours` returns a `float`, because two of the sixteen regions are on half-hour offsets.

### 4. Merge (`core/merge.py`) and Spans (`core/spans.py`)

See ADR-002. The two guards worth reading the code for are `build_spans` refusing a mixed-region list and `build_timeline` refusing a mixed-product list — both prevent silent misattribution rather than trusting the caller.

### 5. Render (`render/`)

Three renderers with different purposes. `text.py` can be asserted in CI and needs no optional dependency, which is why it is the primary one. `chart.py` produces a better artefact and can only be verified by a human looking at it. `export.py` is machine-readable in two formats.

The text gantt samples the **midpoint** of each bucket. Sampling the left edge makes a state that begins mid-bucket disappear entirely; sampling both ends doubles the width for no gain.

---

## The Two Bugs This Codebase Had

### The exporter wrote a file the importer could not read

I wrote a round-trip test expecting it to pass. It came back with an empty result. The exported `events.csv` carries an `instant` column; the importer accepted `timestamp`, `time`, `occurred_at`, `event_time`, `date`, `when`. **Zero overlap — all 29 rows rejected** with `missing required field(s) ['timestamp']`.

Both halves worked in isolation and both had passing tests. Only a round trip finds it, and without one `export` was a dead end: you could produce a file and never feed it back. Fixed by adding `instant` as a timestamp alias, listed last so a hand-written feed supplying its own `timestamp` still wins. A test and a CI step now export, re-import, and compare the rebuilt spans.

### A region live right now reported the same as one that was never live

`time_in_state` counts only closed spans, deliberately — counting an open span as "up to now" makes the answer depend on when the report ran. The consequence is that a currently-active region contributes zero, rendering as `0`, identical to a region that was never active.

I found it reading my own output and not believing it, then checked whether both cases really occurred:

```
product/region    current     active(closed)  ever active?  spans
P-1001/uk         active      0               True          draft active(open)
P-1002/ie         draft       0               False         draft(open)
```

Opposite facts, same rendering. The metric was right; the presentation collapsed two meanings, and **the number looked perfectly reasonable in both cases**. Fixed with a trailing `+` for "still accruing", so `0+`, `0` and `31d+` are three distinguishable statements.

### And one documentation defect

`regions.py` justified including Ireland by claiming tzdata models Irish time with a negative DST offset. Probing found no negative DST anywhere in these sixteen zones — Dublin behaves exactly like London on this build. Unverifiable folklore in a place a reader would trust it. Replaced with the one-instant-two-readings property, which is sharper and is now asserted by a test.

---

## Data Flow

```
1.  lifecycle-tracker show -i data/feeds
2.  load_directory  -> 4 files, sorted by name for determinism
      regional_us_ca.csv  regional_eu.csv  regional_apac.jsonl  global_export.csv
3.  per row: parse_row
      region "gb"          -> canonical "uk"
      kind   "published"   -> LISTED
      "2025-03-30 01:30:00" in uk:
        space -> T
        fold=0 offset +00:00, fold=1 offset +01:00   -> they differ
        round trip gives 02:30 != 01:30              -> NONEXISTENT
        instant 2025-03-30T01:30:00Z, local 01:30, flagged
      -> 31 events, 0 problems
4.  consolidate -> group by product -> 3 timelines
5.  per timeline: deduplicate
      P-1001 us created appears in the regional feed as local 09:00 and in the
      global one as 14:00Z -- same instant, same key -> one survives
      -> 2 duplicates removed, 29 unique events
6.  sort by (instant, region, kind)
7.  build_all_spans: groupby region, then build_spans per region
      us: draft -> active -> suppressed -> active -> delisted(OPEN)
      conflicts: au redundant_transition, ie no_creation_event
8.  find_cross_region_conflicts -> ie single_event_region
9.  render: gantt buckets the instant range; summary_table marks 0+
10. exit 1, because conflicts were found
```

Observed:

```
     31 events -> 3 product(s)
     every resolution exercised: {'offset_given': 5, 'exact': 19,
                                  'ambiguous': 3, 'nonexistent': 4}
     order-independent across 10 shuffles
     supplying the same data twice is idempotent
     29 events exported and re-ingested; spans identical
```

---

## Configuration

`LT_`-prefixed environment variables or a `.env` file. No list-typed fields, since Pydantic Settings parses those as JSON and `LT_REGIONS=us,jp` would produce a decode error that reads like a bug in the tool.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LT_", env_file=".env", extra="ignore")

    feeds: Path = Path("data/feeds")
    output_dir: Path = Path("output")
    width: int = 64
    log_level: str = "INFO"
```

---

## Running

```bash
bash setup.sh                 # verifies behaviour, not just install
bash setup.sh --chart
source .venv/bin/activate

lifecycle-tracker doctor
lifecycle-tracker regions
lifecycle-tracker resolve 2025-03-09T02:30:00 us
lifecycle-tracker show -i data/feeds
lifecycle-tracker jsonl -i data/feeds --what spans | jq -r '.region' | sort -u
lifecycle-tracker export-all -i data/feeds -o out/
```

`setup.sh` checks tzdata is present before anything else, then asserts: every real 2025 DST transition in every region is classified correctly (20 found by walking tzdata); the widest spread is 17 hours; durations are 47/49/48 hours across spring-forward, fall-back and no-DST; the shipped feeds exercise all four resolutions; spans tile per region with only the last open; no span is bounded by a foreign event; the merge is order-independent across 10 shuffles and idempotent; an exported file re-ingests with identical spans; and `0+` renders differently from `0`.

---

## Testing

```bash
pytest                          # 320 tests, ~2.1s
pytest --cov=lifecycle_tracker --cov-report=term-missing
```

| File | Covers |
|:--|:--|
| `conftest.py` | the shipped feeds as fixtures; **DST transitions derived from tzdata at test time**; `event()` and `row()` builders |
| `test_regions_and_parse.py` | the region set; the four tz properties derived rather than trusted; alias resolution; every derived transition parametrized twice; all four timestamp shapes; column and kind aliases |
| `test_merge_and_spans.py` | the UTC invariant; dedup identity; tiling; the per-region guard; all six conflicts; `state_at` boundaries; 47/49/48-hour durations; the shipped-feed property suite incl. 12 shuffles and idempotence |
| `test_render_and_export.py` | gantt width/order/blanks; the `0+` regression class; feed loading errors; both export formats; **the export round trip**; chart PNG magic bytes |
| `integration/test_cli.py` | all six commands, exit codes, `jsonl` keeping stdout data-only, the export round trip through the CLI |

The DST cases being derived is the point: a hard-coded list of dates is a snapshot of what somebody believed at the time, and would silently stop covering a region whose rules changed. There is a guard asserting the derived set is non-empty, because a silent zero would make every case below it pass vacuously.

Not covered: nothing is mocked, because nothing here talks to a network. The uncovered paths are the `ZoneInfoNotFoundError` branch in `Region.zone()` (host-dependent) and the matplotlib-missing branch in `write_chart`, both marked `pragma: no cover`.
