"""Shared fixtures.

Two sources of truth are used deliberately:

  the REAL shipped feeds in data/feeds -- so editing them and breaking something fails the
  build rather than somebody's first run

  transitions DERIVED FROM TZDATA at test time -- so the DST assertions cannot go stale
  when a zone changes its rules, and cannot be wrong because somebody misremembered a date
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from lifecycle_tracker.core.merge import consolidate
from lifecycle_tracker.ingest.feeds import load_directory
from lifecycle_tracker.models import Event, EventKind, Resolution
from lifecycle_tracker.regions import REGIONS, region

REPO_ROOT = Path(__file__).resolve().parents[1]
FEED_DIR = REPO_ROOT / "data" / "feeds"

requires_matplotlib = pytest.mark.skipif(
    importlib.util.find_spec("matplotlib") is None,
    reason="matplotlib is optional: pip install 'lifecycle-tracker[chart]'",
)


class Transition(NamedTuple):
    """A real DST transition, found in the host's tzdata."""

    code: str
    #: The local wall-clock reading that is affected.
    local: datetime
    #: What `_resolve_naive` must say about it.
    expected: Resolution


def _find_transitions(year: int = 2025) -> list[Transition]:
    """Walk each zone hour by hour and record where its UTC offset changes.

    Derived rather than hard-coded. A list of dates in a test file is a list of things that
    were true when somebody wrote them; tzdata is the thing the code actually consults.
    """
    found: list[Transition] = []
    for item in REGIONS:
        zone = item.zone()
        previous = None
        moment = datetime(year, 1, 1, tzinfo=UTC)
        while moment < datetime(year + 1, 1, 1, tzinfo=UTC):
            offset = moment.astimezone(zone).utcoffset()
            if previous is not None and offset != previous:
                local_after = moment.astimezone(zone)
                if offset > previous:
                    # Sprang forward: the half hour before the new reading never happened.
                    probe = (local_after - timedelta(minutes=30)).replace(tzinfo=None)
                    expected = Resolution.NONEXISTENT
                else:
                    # Fell back: the half hour after the new reading happens twice.
                    probe = (local_after + timedelta(minutes=30)).replace(tzinfo=None)
                    expected = Resolution.AMBIGUOUS
                found.append(Transition(item.code, probe, expected))
            previous = offset
            moment += timedelta(hours=1)
    return found


#: Computed once at import. Every region with DST contributes two entries.
TRANSITIONS: tuple[Transition, ...] = tuple(_find_transitions())

#: Regions whose offset never moves during 2025, also derived.
NO_DST_CODES: tuple[str, ...] = tuple(
    item.code for item in REGIONS if not any(t.code == item.code for t in TRANSITIONS)
)


@pytest.fixture(scope="session")
def shipped_events() -> list[Event]:
    events, problems = load_directory(FEED_DIR)
    assert not problems, f"the shipped feeds should parse cleanly, got: {problems}"
    return events


@pytest.fixture(scope="session")
def shipped_timelines(shipped_events: list[Event]) -> dict[str, Any]:
    return consolidate(shipped_events)


def event(
    kind: EventKind | str = EventKind.CREATED,
    instant: str = "2025-01-01T00:00:00+00:00",
    region_code: str = "us",
    product_id: str = "P-1",
    local: str | None = None,
    resolution: Resolution = Resolution.EXACT,
) -> Event:
    """An Event built directly, bypassing the parser.

    Used where the test is about merging or spans rather than about parsing, so a change to
    the parser cannot make a span test fail for an unrelated reason.
    """
    moment = datetime.fromisoformat(instant)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    wall = (
        datetime.fromisoformat(local)
        if local
        else moment.astimezone(region(region_code).zone()).replace(tzinfo=None)
    )
    return Event(
        product_id=product_id,
        region=region_code,
        kind=EventKind(kind) if isinstance(kind, str) else kind,
        instant=moment,
        local=wall,
        resolution=resolution,
    )


def row(
    product_id: str = "P-1",
    region_code: str = "us",
    kind: str = "created",
    timestamp: str = "2025-01-01T00:00:00",
    **extra: Any,
) -> dict[str, Any]:
    """A feed row as a dict, for the parser tests."""
    base = {
        "product_id": product_id,
        "region": region_code,
        "kind": kind,
        "timestamp": timestamp,
    }
    base.update(extra)
    return base
