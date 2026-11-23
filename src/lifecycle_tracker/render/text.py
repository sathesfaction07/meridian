"""Rendering a timeline as text.

A text chart rather than only a picture, for two reasons: it works in a terminal and in a
CI log, and it can be asserted in a test. The matplotlib version in `chart.py` produces a
nicer artefact and cannot be checked by anything except a human looking at it.

The honesty problem in any timeline chart is the right-hand edge. The last span of every
region is OPEN -- the data says nothing about what happened after it. Drawing it as
finishing at the edge of the chart implies a delisting that was never reported; drawing
nothing implies the product vanished. This fills the open span to the edge and marks the
row with a trailing `>`, and `Timeline.time_in_state` still refuses to count open spans,
so the picture shows the last known state while the arithmetic stays conservative.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lifecycle_tracker.core.spans import state_at
from lifecycle_tracker.models import Resolution, State, Timeline
from lifecycle_tracker.regions import region

#: One character per state. Chosen so density reads correctly in a monospace font: an
#: active product is solid, a suppressed one is hatched, a dead one is faint.
STATE_CHAR: dict[State, str] = {
    State.DRAFT: "-",
    State.ACTIVE: "#",
    State.SUPPRESSED: "x",
    State.DELISTED: ".",
    State.ARCHIVED: "_",
}

#: Shown where a region has no data at all for a bucket, which is different from every
#: state above and must not look like one.
NO_DATA = " "


def gantt(timeline: Timeline, width: int = 64) -> list[str]:
    """Render one product as a row per region.

    Buckets are equal slices of the interval from the first event to the last, measured in
    INSTANTS. Slicing by local time instead would give each region a different axis, and
    the rows would no longer line up vertically -- which is the only thing a chart like
    this is for.
    """
    if not timeline.events:
        return [f"{timeline.product_id}: no events"]

    start = timeline.first_instant
    end = timeline.last_instant
    assert start is not None and end is not None  # non-empty events guarantees both

    span_of_time = end - start
    if span_of_time == timedelta(0):
        # Every event at one instant. There is no axis to draw, so say so rather than
        # dividing by zero or rendering a misleading single column.
        return [
            f"{timeline.product_id}: all {len(timeline.events)} event(s) at "
            f"{start.isoformat()}, so there is no interval to chart"
        ]

    width = max(8, width)
    lines: list[str] = []
    label_width = max(len(code) for code in timeline.regions) + 1

    for code in timeline.regions:
        spans = timeline.spans_for(code)
        row = []
        for index in range(width):
            # Sample the MIDPOINT of each bucket. Sampling the left edge makes a state
            # that begins mid-bucket disappear, and sampling both ends doubles the width.
            offset = span_of_time * (index + 0.5) / width
            state = state_at(spans, start + offset)
            row.append(STATE_CHAR[state] if state else NO_DATA)

        still_open = bool(spans) and spans[-1].is_open
        lines.append(f"  {code:<{label_width}}|{''.join(row)}|{'>' if still_open else ' '}")

    return lines


def axis(timeline: Timeline, width: int = 64) -> list[str]:
    """The date axis under a gantt, in UTC.

    Labelled in UTC and said so, because a chart of sixteen regions has no single local
    clock to label with, and quietly using the host's would make the same data render
    differently on two machines.
    """
    if not timeline.events or timeline.first_instant == timeline.last_instant:
        return []

    start, end = timeline.first_instant, timeline.last_instant
    assert start is not None and end is not None
    width = max(8, width)
    label_width = max(len(code) for code in timeline.regions) + 1

    left = f"{start:%Y-%m-%d}"
    right = f"{end:%Y-%m-%d}"
    middle_instant = start + (end - start) / 2
    middle = f"{middle_instant:%Y-%m-%d}"

    filler = width - len(left) - len(right) - len(middle)
    if filler < 2:
        bar = f"{left}{' ' * max(1, width - len(left) - len(right))}{right}"
    else:
        pad = filler // 2
        bar = f"{left}{' ' * pad}{middle}{' ' * (filler - pad)}{right}"

    return [f"  {' ' * label_width} {bar[:width]}", f"  {' ' * label_width} (UTC)"]


def legend(states_present: set[State] | None = None) -> list[str]:
    """The character key. Narrowed to the states actually drawn, when told which."""
    shown = [
        (state, char)
        for state, char in STATE_CHAR.items()
        if states_present is None or state in states_present
    ]
    pairs = "   ".join(f"{char} {state.value}" for state, char in shown)
    return [
        f"  {pairs}",
        "  > the final span is open: this is the last known state, not a confirmed one",
    ]


def summary_table(timeline: Timeline) -> list[str]:
    """Per-region current state, closed time active, and the local clock offset.

    The `active` column counts only CLOSED spans, per `Timeline.time_in_state`, so that
    the number does not change depending on when the report was run. That creates a
    reporting trap which this function has to defuse: a region that is active right now
    has an OPEN active span contributing nothing, so it shows `0` -- identical to a region
    that was never active at all. Those are opposite facts.

    A trailing `+` marks "and still accruing", so `0+` reads as active-since-the-last-event
    and a bare `0` means never. Without it, "live for ten months" and "never went live"
    render the same way, and the number looks perfectly reasonable in both cases.

    The offset is shown at the region's LAST event rather than at some fixed date, because
    a region that transitioned partway through the data has two, and the one that matters
    for reading its most recent local timestamp is the later one.
    """
    if not timeline.events:
        return []

    lines = [
        f"  {'region':<8}{'state':<12}{'events':>7}  {'active':>14}  {'offset':>7}  "
        f"local last event"
    ]
    any_open = False
    for code in timeline.regions:
        events = [event for event in timeline.events if event.region == code]
        spans = timeline.spans_for(code)
        current = timeline.current_state(code)
        active = timeline.time_in_state(code, State.ACTIVE)

        # The open final span contributes no duration, so mark it rather than letting
        # `0` mean both "never active" and "active right now".
        still_accruing = bool(spans) and spans[-1].is_open and spans[-1].state is State.ACTIVE
        any_open = any_open or still_accruing
        shown = _duration(active) + ("+" if still_accruing else "")

        last = events[-1]
        offset = last.instant.astimezone(region(code).zone()).utcoffset() or timedelta(0)
        hours = offset.total_seconds() / 3600
        lines.append(
            f"  {code:<8}{(current.value if current else '-'):<12}{len(events):>7}  "
            f"{shown:>14}  {hours:>+7.2f}  {last.local:%Y-%m-%d %H:%M}"
        )

    if any_open:
        lines.append(
            "  active counts CLOSED spans only; + means still active at the end of the data"
        )
    return lines


def _duration(delta: timedelta) -> str:
    """Days and hours. Never seconds -- nothing here is measured that finely."""
    if delta == timedelta(0):
        return "0"
    days, remainder = divmod(int(delta.total_seconds()), 86400)
    hours = remainder // 3600
    if days and hours:
        return f"{days}d {hours}h"
    if days:
        return f"{days}d"
    return f"{hours}h"


def flagged_events(timeline: Timeline) -> list[str]:
    """Events whose local time did not map cleanly onto an instant.

    Reported rather than buried, because each one is an hour of uncertainty that a reader
    would otherwise have no way of knowing about.
    """
    rows = [event for event in timeline.events if event.resolution is not Resolution.EXACT]
    interesting = [
        event
        for event in rows
        if event.resolution in (Resolution.AMBIGUOUS, Resolution.NONEXISTENT)
    ]
    if not interesting:
        return []

    lines = ["  dst-affected timestamps:"]
    for event in interesting:
        note = (
            "this local time happened twice; resolved to the first"
            if event.resolution is Resolution.AMBIGUOUS
            else "this local time never happened; shifted forward"
        )
        lines.append(
            f"    {event.region:<4}{event.kind.value:<12}{event.local:%Y-%m-%d %H:%M} local  "
            f"-> {event.instant:%Y-%m-%d %H:%M}Z  {note}"
        )
    return lines


def render(timeline: Timeline, width: int = 64, *, details: bool = True) -> str:
    """The whole text report for one product."""
    parts: list[str] = [f"{timeline.product_id}   {timeline.summary()}", ""]
    parts.extend(gantt(timeline, width))
    parts.extend(axis(timeline, width))
    parts.append("")

    states = {span.state for span in timeline.spans}
    parts.extend(legend(states))

    if details:
        parts.append("")
        parts.extend(summary_table(timeline))
        flagged = flagged_events(timeline)
        if flagged:
            parts.append("")
            parts.extend(flagged)
        if timeline.conflicts:
            parts.append("")
            parts.append("  conflicts:")
            for conflict in timeline.conflicts:
                parts.append(f"    {conflict.region:<4}{conflict.reason:<32}{conflict.detail}")

    return "\n".join(parts)


def now_utc() -> datetime:
    """Used only for report headers, never for arithmetic on spans."""
    return datetime.now(UTC)
