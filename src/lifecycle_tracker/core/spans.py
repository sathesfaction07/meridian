"""Turning a region's events into the spans between them.

One region at a time. That constraint is the whole reason this function takes a flat list
and the caller groups first: events from sixteen regions merged into one instant-ordered
stream look like a single history, and walking that stream to build spans would let a
Tokyo event close a Mexico City span. The result is plausible and wrong, which is the
worst combination.
"""

from __future__ import annotations

from datetime import datetime
from itertools import groupby

from lifecycle_tracker.models import (
    TERMINAL,
    Conflict,
    Event,
    EventKind,
    Span,
    State,
)


def build_spans(events: list[Event]) -> tuple[list[Span], list[Conflict]]:
    """Build the state spans for a single region's events.

    Every event opens a span and closes the previous one, so there are exactly as many
    spans as events and they tile the interval from the first event to the last with no
    gaps and no overlaps. The final span is left OPEN -- `end=None` -- because the data
    says nothing about what happened after it.

    Conflicts are reported, never silently repaired. An event that contradicts the state
    it arrives in is still applied, because the feed is the record of what was reported
    and dropping a row loses information that a human might need. Flagging it and applying
    it keeps both facts available.
    """
    if not events:
        return ([], [])

    regions = {event.region for event in events}
    if len(regions) > 1:
        raise ValueError(
            f"build_spans takes one region at a time, got {sorted(regions)}. "
            "Group by region before calling, or a foreign event will close a local span."
        )

    ordered = sorted(events, key=lambda event: event.sort_key)
    product_id = ordered[0].product_id
    region_code = ordered[0].region

    conflicts: list[Conflict] = []

    def flag(reason: str, detail: str) -> None:
        conflicts.append(
            Conflict(product_id=product_id, region=region_code, reason=reason, detail=detail)
        )

    if ordered[0].kind is not EventKind.CREATED:
        # The spec calls this out: products with no creation date are a known input
        # problem. The timeline still starts at the first event we have, and the report
        # says the beginning is missing rather than implying the product began there.
        flag(
            "no_creation_event",
            f"first event is {ordered[0].kind.value} at {ordered[0].instant.isoformat()}, "
            f"so anything before it is unknown",
        )

    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if earlier.instant == later.instant:
            # Same region, same instant, different kinds. Their relative order is decided
            # by the tiebreakers in sort_key, which is deterministic but arbitrary -- the
            # feed genuinely does not say which came first.
            flag(
                "simultaneous_events",
                f"{earlier.kind.value} and {later.kind.value} both at "
                f"{earlier.instant.isoformat()}; their order is arbitrary",
            )

    spans: list[Span] = []
    state: State | None = None

    for event in ordered:
        if state is not None:
            if state in TERMINAL:
                flag(
                    "event_after_terminal",
                    f"{event.kind.value} at {event.instant.isoformat()} follows "
                    f"{state.value}, which nothing should follow",
                )
            elif event.state_after is state:
                flag(
                    "redundant_transition",
                    f"{event.kind.value} at {event.instant.isoformat()} leaves the state "
                    f"at {state.value}",
                )
            elif event.kind is EventKind.REINSTATED and state is not State.SUPPRESSED:
                flag(
                    "reinstated_without_suppression",
                    f"reinstated at {event.instant.isoformat()} while {state.value}",
                )

        if spans:
            spans[-1] = spans[-1].model_copy(update={"end": event.instant})

        spans.append(
            Span(
                product_id=product_id,
                region=region_code,
                state=event.state_after,
                start=event.instant,
                end=None,
                opened_by=event.kind,
            )
        )
        state = event.state_after

    return (spans, conflicts)


def build_all_spans(events: list[Event]) -> tuple[list[Span], list[Conflict]]:
    """Build spans for every region present, grouped correctly.

    The grouping is the point. `groupby` needs sorted input, so this sorts by region
    first -- and then `build_spans` sorts by instant within each group, because the
    orderings are different and using one for both is how regions get interleaved.
    """
    spans: list[Span] = []
    conflicts: list[Conflict] = []

    for _, group in groupby(sorted(events, key=lambda e: e.region), key=lambda e: e.region):
        region_spans, region_conflicts = build_spans(list(group))
        spans.extend(region_spans)
        conflicts.extend(region_conflicts)

    # Sorted for output stability. Region first so a report reads one region at a time,
    # in the canonical west-to-east order rather than alphabetically.
    from lifecycle_tracker.regions import all_codes

    order = {code: index for index, code in enumerate(all_codes())}
    spans.sort(key=lambda span: (order.get(span.region, 99), span.start))
    return (spans, conflicts)


def state_at(spans: list[Span], instant: datetime) -> State | None:
    """The state at a given instant, or None if the instant is outside the data.

    Spans are half-open on the right: `start <= instant < end`. Without that, an instant
    landing exactly on a boundary would match two adjacent spans, and which one you got
    would depend on iteration order.

    An OPEN span (`end is None`) matches anything at or after its start. That is the last
    KNOWN state rather than a confirmed one, which is why `Timeline.time_in_state` refuses
    to count open spans even though this function will report them.
    """
    for span in spans:
        if instant < span.start:
            continue
        if span.end is None or instant < span.end:
            return span.state
    return None
