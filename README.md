# Meridian

Consolidates product lifecycle events from 16 regional feeds into one timeline per product, then renders it as a text gantt, a PNG chart, JSON Lines, or CSV.

The hard part is not the merge. It is that **local time is not a function of instants**, and every naive implementation gets that wrong silently.

No credentials, no network, no model. This is a data quality problem solved by strict parsing.

---

## The Problem

Sixteen feeds, each reporting in its own local wall-clock time. Merging them means answering "which of these happened first", and there are three ways to get that wrong without anything raising an exception.

**Offsets span 17 hours.** Sydney in DST is UTC+11 while Mexico City is UTC−06. Two events written `09:00` in different feeds can be 17 hours apart, so comparing wall-clock strings inverts orders. Two of the sixteen regions are on **half-hour** offsets, so an integer hour count truncates them.

**One wall-clock reading per year never happens, and another happens twice.** On the day a zone springs forward, `02:30` does not exist. On the day it falls back, `01:30` happens twice — 60 minutes apart. Python raises for neither; it picks something plausible and moves on. A timeline built on that is quietly an hour wrong, and nothing in the output says so.

**Durations computed from wall clocks are wrong on transition days.** Subtracting `2025-03-08 12:00` from `2025-03-10 12:00` in New York gives 48 hours. The real elapsed time is **47**. That error survives review because the number looks right.

And the southern hemisphere runs the opposite phase, so any assumption about which month is "summer" is wrong for a quarter of the regions.

---

## How It Works

```
feed rows (16 regions, local wall-clock text)
        |
        v
parse    -> instant (UTC) + local reading + HOW it was resolved
        |     exact | offset_given | ambiguous | nonexistent
        v
dedupe   -> keyed on the resolved instant, so the same event from a regional
        |     and a global feed collapses to one
        v
order    -> by instant, with deterministic tiebreakers
        |
        v
spans    -> PER REGION. Every event opens a span and closes the previous
        |     one; the last is left OPEN
        v
render   -> text gantt | PNG | JSON Lines | CSV
```

Every event keeps **both** its UTC instant and the local reading it arrived as. Only the instant loses the ability to say "09:00 local", which is how a regional team talks about its own events. Only the local reading makes ordering impossible.

---

## Resolutions

Each timestamp records how it was mapped onto an instant:

| Resolution | Meaning |
|:--|:--|
| `exact` | an ordinary local reading, mapped unambiguously |
| `offset_given` | the feed supplied an explicit offset, so no zone lookup was needed |
| `ambiguous` | the local reading **happens twice**. Resolved to the first occurrence, and flagged |
| `nonexistent` | the local reading **never happens**. Python shifts it forward; the shift is flagged |

The last two are the point. `meridian resolve` shows them on any timestamp you like:

```
$ meridian resolve 2025-03-09T02:30:00 us
  local       2025-03-09T02:30:00
  instant     2025-03-09T07:30:00+00:00
  resolution  nonexistent
              this local reading never happens; shifted forward by the DST offset
```

---

## Running

```bash
bash setup.sh                 # verifies real behaviour, not just install
bash setup.sh --chart         # + matplotlib for PNG output
source .venv/bin/activate

meridian doctor
meridian regions
meridian show -i data/feeds
meridian jsonl -i data/feeds --what spans | jq -r '.region'
meridian export-all -i data/feeds -o out/
```

`meridian show -p P-1001` on the shipped feeds:

```
P-1001   22 event(s), 8 region(s), 2 dup(s) removed, 3 conflict(s), 10 dst-flagged

  us |----########xxxxxxx#############################################|>
  ca |----############################################xxxxxxxxxxxxxxxx|>
  uk |----------------################################################|>
  ie |                              ##################################|>
  de |----------------##############################################xx|>
  in |------##########################################################|>
  jp |-----###########################################################|>
  au | -----------------########################################xxxxxx|>
      2025-01-15                 2025-06-09                 2025-11-02
      (UTC)
  - draft   # active   x suppressed   . delisted
  > the final span is open: this is the last known state, not a confirmed one

  region  state        events          active   offset  local last event
  us      delisted          5         241d 2h    -4.00  2025-11-02 01:30
  ca      suppressed        3         198d 6h    -4.00  2025-08-20 16:45
  uk      active            2              0+    +1.00  2025-03-30 01:30
  de      suppressed        3        209d 23h    +2.00  2025-10-26 02:30
  in      active            2              0+    +5.50  2025-02-10 09:00
  au      suppressed        4         182d 1h   +11.00  2025-10-05 02:30
  active counts CLOSED spans only; + means still active at the end of the data

  dst-affected timestamps:
    us  suppressed  2025-03-09 02:30 local  -> 2025-03-09 07:30Z  this local time never happened
    au  listed      2025-04-06 02:30 local  -> 2025-04-05 15:30Z  this local time happened twice
    de  suppressed  2025-10-26 02:30 local  -> 2025-10-26 00:30Z  this local time happened twice
```

Rows line up vertically because the axis is in instants. The `+5.50` is India's half-hour offset; `+11.00` is Sydney in southern summer.

---

## Commands

| Command | Purpose | Exit |
|:--|:--|:--|
| `show` | Text gantt, per-region table, DST notes, conflicts | 1 if any row failed to parse or any conflict was found |
| `jsonl` | One JSON object per line, to a file or stdout for piping | 0 |
| `export-all` | `events.csv`, `spans.csv`, `conflicts.csv`, `report.json` | 0 |
| `regions` | All 16 with January **and** July offsets | 0 |
| `resolve` | How one timestamp maps onto an instant | 1 if ambiguous or nonexistent |
| `doctor` | Config, region coverage, whether the feeds parse | 0 |

`show` gating on conflicts is what makes it usable as a pipeline step: it fails on feed quality, not only on crashes. `jsonl` puts nothing but data on stdout, so `| jq` works, and diagnostics go to stderr.

---

## Conflicts

Reported, never silently repaired. A contradicting event is still applied, because the feed is the record of what was reported and dropping a row loses information.

| Reason | What it means |
|:--|:--|
| `no_creation_event` | the first event for a region is not `created`, so the start is unknown |
| `event_after_terminal` | something follows `archived`, which nothing should |
| `redundant_transition` | the event leaves the state unchanged |
| `reinstated_without_suppression` | reinstated while not suppressed |
| `simultaneous_events` | two events at the same instant in one region; their order is arbitrary |
| `single_event_region` | one event only, so the span stays open and no duration exists |

---

## Testing

```bash
pytest                          # 320 tests, ~2.1s
pytest --cov=lifecycle_tracker --cov-report=term-missing
```

**The DST tests are parametrized over transitions derived from the host's tzdata**, not over a hard-coded list of dates. A list in a test file is a snapshot of what somebody believed; tzdata is what the code actually consults. On this machine that finds **20 real transitions across 16 regions**, and all 20 are classified correctly.

The merge invariants run against the real shipped feeds, so editing `data/feeds/` and breaking one fails the build: spans tile contiguously per region with only the last open; one span per event; no span bounded by a foreign region's event; order-independent across 12 shuffles; idempotent when the same data is supplied twice.

`setup.sh` re-derives most of that on a fresh clone rather than asserting it.

---

## Configuration

```bash
LT_FEEDS=data/feeds
LT_OUTPUT_DIR=output
LT_WIDTH=64
```

---

## Tradeoffs and Decisions I Made Along the Way

**A docstring in this repo claimed something about tzdata that is not true.** `regions.py` justified including Ireland by saying the tz database models Irish time with a negative DST offset. I probed it: on this build, `Europe/Dublin` reports `dst()=0` in January and `+1:00` in July — identical to London. There is **no negative DST anywhere** in these sixteen zones.

That claim depends on which tzdata variant a distribution ships, so it was unverifiable folklore sitting in a docstring where a reader would trust it. Probing also turned up something better and checkable: **all of Europe transitions at one instant but at different local readings** — 01:00 UTC on 2025-03-30 is 02:00 in London and 03:00 in Berlin. That is a much sharper illustration of why local time cannot be the merge key, and it is now asserted by a test that derives it from tzdata. The lesson generalises: a comment that cannot be checked is a comment that drifts.

**The exporter wrote a file the importer could not read.** I wrote a round-trip test expecting it to pass. It failed with an empty result, and the cause was that `events.csv` carries an `instant` column while the importer only accepted `timestamp`, `time`, `occurred_at`, `event_time`, `date`, `when`. **Zero overlap. All 29 rows rejected.**

Both halves worked perfectly in isolation, and both had passing tests. Only a round trip finds this, and without one `export` was a dead end — you could produce a file and never feed it back. The fix was one alias, listed last so a hand-written feed supplying its own `timestamp` column still wins. There is now a test and a CI step that export, re-import, and compare the rebuilt spans.

**A region live right now reported the same as one that was never live.** `time_in_state` counts only CLOSED spans, deliberately: counting an open span as "up to now" makes the answer depend on when the report ran, which destroys comparability across two runs. The consequence is that a region whose final span is `active` contributes zero — rendering as `0`, identical to a region that was never active.

I found this reading my own output and not believing it, then checked whether both cases really occurred: `P-1001/uk` is active with `0`, and `P-1002/ie` is draft with `0` and never active. Opposite facts, same rendering. The metric was right; the presentation collapsed two meanings. A trailing `+` now marks "still accruing", so `0+` reads as active-since-the-last-event and a bare `0` means never. **The number looked perfectly reasonable in both cases**, which is what made it worth chasing.

**Spans are built strictly per region, and the function refuses a mixed list.** Sixteen regions merged into one instant-ordered stream looks like a single history, and walking it would let a Tokyo event close a Mexico City span. The output would be entirely plausible. `build_spans` raises on more than one region rather than trusting its caller, and there is a property test asserting no span is bounded by a foreign event.

**The last span of every region is open, and the two renderers say so differently.** The data says nothing about what happened after the last event. Drawing it as ending implies a delisting that was never reported; drawing nothing implies the product vanished. The text gantt fills to the edge and marks the row with `>`; the chart fills to the edge hatched and at reduced opacity. Meanwhile the arithmetic excludes open spans entirely. So the picture shows the last known state while the numbers stay conservative — and both behaviours are documented where a reader meets them.

**An ambiguous reading resolves to the first occurrence, not the second.** Either would be defensible. The first is the earlier instant, so a duration computed from it can only be under-stated, never over-stated, and consistently choosing one end is what keeps the merge deterministic. The choice is recorded on the event rather than being invisible.

**A timestamp that cannot be parsed raises; a row that cannot be parsed is collected.** Those are different. An unreadable timestamp must not become an event at an invented time, because an invented time in a timeline is worse than a gap. But one bad row out of forty thousand should not cost you the other 39,999, so `load_file` returns `(events, problems)` and names the file and line for each.

**Region codes raise rather than defaulting.** Guessing a region means guessing a timezone, which is worth up to 17 hours of error. Eight aliases that older feeds still emit are mapped as data, because a historical export cannot be rewritten and dropping its rows loses events.

**`utc_offset_hours` returns a float.** Two of the sixteen regions are on half-hour offsets, and an int would silently truncate India to +5.

**No model, and no web UI.** The project brief called for neither. The accuracy problem here is entirely about strict parsing and explicit handling of missing data — there is no judgement for a model to contribute, and putting one in the path would make a timeline unreproducible.

**The text renderer exists because the chart cannot be tested.** A PNG can only be checked by a human looking at it. The text gantt asserts in CI, works in a terminal, and needs no optional dependency. They exist for different reasons rather than one superseding the other.

**Ordering is total, and the tiebreakers matter more than they look.** Events sort by instant, then region, then kind. Without the tiebreakers, two events sharing an instant come out in whatever order the input happened to supply, so merging the same data twice can produce two different timelines — and nothing about either would look wrong. There is a property test that shuffles the input twelve ways and asserts the result is byte-identical.

---

## Stack

| Concern | Choice |
|:--|:--|
| Language | Python 3.11+ |
| Timezones | stdlib `zoneinfo` (IANA tzdata), PEP 495 `fold` |
| Models + config | Pydantic, Pydantic Settings |
| CLI | Click |
| Charts | matplotlib, optional, Agg backend |
| Output | text gantt, JSON Lines, CSV, PNG |
| Tests | pytest, DST cases derived from tzdata, Click's CliRunner |
| Lint | ruff |
| Models/AI | none, deliberately |
