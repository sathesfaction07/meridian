"""Merging events from many regional feeds into one timeline per product.

Three things happen here, in this order, and the order matters:

  1. deduplicate  -- the same event often arrives from both a regional and a global feed
  2. order        -- by instant, with deterministic tiebreakers
  3. build spans  -- per region, never across the merged stream

Deduplication comes first because it is keyed on the resolved instant. Two rows saying
`2025-03-01T09:00:00+09:00` and `2025-03-01T00:00:00Z` are the same event described from
two clocks, and only a merge that has already resolved both to instants can see that.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from lifecycle_tracker.core.spans import build_all_spans
from lifecycle_tracker.models import Conflict, Event, Timeline


def deduplicate(events: Iterable[Event]) -> tuple[list[Event], int]:
    """Drop exact duplicates, keeping the first occurrence.

    Identity is (product, region, kind, instant) -- see `Event.key`. Two rows that agree
    on all four describe the same happening, however differently they were written down.

    "First occurrence" means first in the order supplied, which makes this stable but not
    order-independent in one narrow respect: the surviving event's `source` and `local`
    fields come from whichever row arrived first. Those are reporting fields, never used
    for ordering or arithmetic, so a timeline built from either is identical.
    """
    seen: set[tuple[str, str, str, object]] = set()
    kept: list[Event] = []
    removed = 0

    for event in events:
        if event.key in seen:
            removed += 1
            continue
        seen.add(event.key)
        kept.append(event)

    return (kept, removed)


def find_cross_region_conflicts(events: list[Event]) -> list[Conflict]:
    """Problems only visible once regions are compared.

    Per-region checks live in `build_spans`. This is for the things that need the whole
    picture, and there is exactly one of them worth reporting: a region that reported
    nothing but a terminal event, which usually means a partial export rather than a real
    history.
    """
    conflicts: list[Conflict] = []
    if not events:
        return conflicts

    product_id = events[0].product_id
    per_region: Counter[str] = Counter(event.region for event in events)

    for region_code, count in sorted(per_region.items()):
        if count == 1:
            only = next(event for event in events if event.region == region_code)
            conflicts.append(
                Conflict(
                    product_id=product_id,
                    region=region_code,
                    reason="single_event_region",
                    detail=(
                        f"only one event ({only.kind.value}) was reported for this region, "
                        f"so its span stays open and no duration can be computed"
                    ),
                )
            )
    return conflicts


def consolidate(events: Iterable[Event]) -> dict[str, Timeline]:
    """Group events by product and build one Timeline each.

    Returns a dict keyed by product id, in sorted key order so two runs over the same
    input produce the same output ordering.
    """
    by_product: dict[str, list[Event]] = {}
    for event in events:
        by_product.setdefault(event.product_id, []).append(event)

    timelines: dict[str, Timeline] = {}
    for product_id in sorted(by_product):
        timelines[product_id] = build_timeline(by_product[product_id])
    return timelines


def build_timeline(events: Iterable[Event]) -> Timeline:
    """Deduplicate, order and span one product's events."""
    unique, duplicates_removed = deduplicate(events)
    if not unique:
        return Timeline(product_id="", events=[], spans=[], conflicts=[])

    product_ids = {event.product_id for event in unique}
    if len(product_ids) > 1:
        raise ValueError(
            f"build_timeline takes one product at a time, got {sorted(product_ids)}. "
            "Use consolidate() to group first."
        )

    # Sorted once, here. Everything downstream relies on `Timeline.events` being in
    # instant order -- `first_instant` and `last_instant` index the ends of this list
    # rather than scanning, so an unsorted list would report the wrong bounds.
    ordered = sorted(unique, key=lambda event: event.sort_key)

    spans, conflicts = build_all_spans(ordered)
    conflicts.extend(find_cross_region_conflicts(ordered))

    return Timeline(
        product_id=ordered[0].product_id,
        events=ordered,
        spans=spans,
        conflicts=conflicts,
        duplicates_removed=duplicates_removed,
    )
