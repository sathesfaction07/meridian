# ADR 002: Spans are built per region, the last one stays open, and open spans are marked in output

## Status

Accepted

## Context

A timeline is more useful as *intervals* than as points. "Suppressed for 32 days" answers a question; "suppressed on 9 March, reinstated on 10 April" makes the reader do arithmetic — arithmetic that is wrong if they do it on wall clocks.

So events become spans: each event opens a span with its resulting state and closes the previous one. Three decisions in that sentence turned out to matter.

### Grouping

The merge produces one instant-ordered event stream per product, across up to sixteen regions. Walking that stream to build spans is the obvious implementation and it is wrong: a Tokyo event would close a Mexico City span. The output looks entirely reasonable — contiguous spans, sensible durations, a chart that renders — and it describes a history that never happened.

### The end of the data

The last event in each region has nothing after it. The data says nothing about what happened next. Closing that span at the last event implies a delisting that was never reported; leaving it out implies the product vanished.

### Durations across a DST boundary

`2025-03-10 12:00` minus `2025-03-08 12:00` in New York is 48 hours by wall clock and **47** in reality. Computing from local readings gives an answer that looks right and is not.

## Decision

**Spans are built one region at a time, and the function refuses anything else.**

```python
regions = {event.region for event in events}
if len(regions) > 1:
    raise ValueError(
        f"build_spans takes one region at a time, got {sorted(regions)}. "
        "Group by region before calling, or a foreign event will close a local span."
    )
```

A guard rather than a docstring, because the failure it prevents is silent. `build_all_spans` does the grouping, sorting by region for `groupby` and letting `build_spans` sort by instant within each group — the two orderings are different, and using one for both is precisely how regions get interleaved.

**Every event opens exactly one span, and the last is left open.** `Span.end is None` means "still open at the end of the data", which is deliberately distinct from a zero-length span. Zero-length spans are real: two events at the same instant in one region produce one, and that is reported as a `simultaneous_events` conflict rather than being smoothed away.

**Durations come from instants.** `Span.duration` subtracts `start` from `end`, both UTC. A span crossing a transition therefore reports 23 or 25 hours.

**`time_in_state` counts closed spans only.**

```python
for span in self.spans_for(region):
    if span.state is state and span.duration is not None:
        total += span.duration
```

Counting an open span as "up to now" would make the answer depend on when the report was generated, which makes the metric impossible to compare across two runs. That is a real cost — see the bug below — and it is still the right trade, because a metric that silently changes between runs is worse than one that under-reports predictably.

**Conflicts are reported and the offending event is still applied.** The feed is the record of what was reported; dropping a row loses information a human may need. Flagging and applying keeps both facts available. Six reasons are detected: `no_creation_event`, `event_after_terminal`, `redundant_transition`, `reinstated_without_suppression`, `simultaneous_events`, `single_event_region`.

## The bug this fixes

Excluding open spans is correct and it created a reporting trap I did not anticipate.

A region whose final span is `active` contributes **zero** closed active time. Rendered plainly, that is `0` — which is exactly what a region that was never active also renders. I noticed it reading my own output and not believing it, then checked whether both cases genuinely occurred in the sample data:

| | current state | active (closed) | ever active? |
|:--|:--|:--|:--|
| `P-1001/uk` | active | `0` | **True** — live right now, unbounded |
| `P-1002/ie` | draft | `0` | **False** — never live at all |

Opposite facts, identical rendering. A reader cannot tell "this product has been live for months" from "this product never went live", and **the number looks perfectly reasonable in both cases**, so nothing prompts them to check.

The metric was right. The presentation collapsed two meanings. The fix is in the render layer, where the ambiguity is:

```python
still_accruing = bool(spans) and spans[-1].is_open and spans[-1].state is state
shown = _duration(active) + ("+" if still_accruing else "")
```

`0+` now reads as active-since-the-last-event, a bare `0` means never, and `31d+` means both. A footnote appears when anything is open, and `doctor` explains the marker.

The same honesty problem appears in both renderers and is solved consistently: the text gantt fills an open span to the right edge and marks the row with `>`; the chart fills to the edge hatched and at reduced opacity. The picture shows the last **known** state while the arithmetic stays conservative.

## Validation

- `TestSpansAreBuiltPerRegion` — the mixed-region guard, correct grouping, and a property asserting no span is bounded by an event from another region
- `TestSpansTile` — one span per event, contiguity, only the final span open, out-of-order input sorted
- `TestDurationsAreInstantBased` — 47h / 49h / 48h, including Sydney's inverted phases
- `TestSummaryTableDistinguishesNeverFromStillGoing` — the regression, including an assertion that the two rows differ
- `TestPropertiesOverTheShippedFeeds` — tiling, span counts, no foreign bounding, order-independence across 12 shuffles, idempotence
- `setup.sh` re-derives the tiling and no-foreign-bounding invariants, and asserts that `0+` and `0` render differently

## Consequences

**Good**

- A region's history is queryable as intervals, and `state_at` answers "what was it on this date" in one call
- The interleaving bug cannot happen by accident; it raises instead
- Durations are correct across DST boundaries, which is the whole reason the instant is the ordering key
- `0+` versus `0` is a two-character fix for a genuinely misleading report
- Contradictory feeds are visible rather than smoothed away, and `show` exits non-zero so a pipeline can gate on feed quality

**Bad**

- `time_in_state` under-reports for any region that is currently in the state being measured, which is most of them. The `+` marks it but a reader skimming a column of numbers still sees mostly zeroes
- Spans are rebuilt from scratch on every call; there is no incremental update. Fine for a catalogue this size, wrong for a live feed
- Adjacent spans with the same state are not merged. `listed` then `reinstated` both mean `active`, so a chart shows two touching bars of the same colour. Merging them would hide that an event happened; not merging them looks like a rendering artefact. I chose the former and it is arguable
- Six conflict reasons is a lot of vocabulary, and `single_event_region` fires on any partial export, so it is the noisiest by far

**Neutral**

- A zero-length span survives into output rather than being dropped. The chart draws it as a tick and the CSV gives `duration_hours: 0`, which is distinguishable from an open span's `null`
- `state_at` is half-open on the right, so an instant exactly on a boundary belongs to the later span. Without that, a boundary instant matches two spans and which one you get depends on iteration order
- The text and chart renderers duplicate the open-span decision rather than sharing it. Two short implementations were clearer than one parameterised one
