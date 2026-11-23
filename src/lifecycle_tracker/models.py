"""Domain models.

The central decision is that an event carries **both** its resolved UTC instant and the
local wall-clock time it was reported as. Keeping only the instant loses the ability to say
"this happened at 09:00 local", which is how regional teams talk about their own events.
Keeping only the local time makes cross-region ordering impossible. See ADR-001.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventKind(StrEnum):
    """What happened. A closed set, because each kind maps to exactly one state."""

    CREATED = "created"
    LISTED = "listed"
    SUPPRESSED = "suppressed"
    REINSTATED = "reinstated"
    DELISTED = "delisted"
    ARCHIVED = "archived"


class State(StrEnum):
    """What the product was, between one event and the next."""

    DRAFT = "draft"
    ACTIVE = "active"
    SUPPRESSED = "suppressed"
    DELISTED = "delisted"
    ARCHIVED = "archived"


#: Each event kind puts the product into exactly one state. A mapping rather than a chain
#: of if-statements, so adding a kind is a one-line data change and the exhaustiveness is
#: checkable -- there is a test asserting every EventKind appears here.
STATE_AFTER: dict[EventKind, State] = {
    EventKind.CREATED: State.DRAFT,
    EventKind.LISTED: State.ACTIVE,
    EventKind.SUPPRESSED: State.SUPPRESSED,
    EventKind.REINSTATED: State.ACTIVE,
    EventKind.DELISTED: State.DELISTED,
    EventKind.ARCHIVED: State.ARCHIVED,
}

#: States a product cannot leave. An event after one of these is a data problem worth
#: reporting rather than silently applying.
TERMINAL: frozenset[State] = frozenset({State.ARCHIVED})


class Resolution(StrEnum):
    """How a local timestamp was mapped onto an instant.

    Most events are `exact`. The other two exist because local time is not a function of
    instants: one wall-clock reading per year happens twice, and one never happens at all.
    Recording which case applied is the difference between a timeline that is quietly an
    hour wrong and one that says so.
    """

    EXACT = "exact"

    #: The local time occurs twice, on the day a zone falls back. Resolved to the FIRST
    #: occurrence, and flagged.
    AMBIGUOUS = "ambiguous"

    #: The local time does not exist, on the day a zone springs forward. Python maps it
    #: forward by the DST offset rather than raising, so it is flagged.
    NONEXISTENT = "nonexistent"

    #: The feed supplied an explicit UTC offset, so no zone lookup was needed and neither
    #: of the above can apply.
    OFFSET_GIVEN = "offset_given"


class Event(BaseModel):
    """One lifecycle event, in one region."""

    model_config = ConfigDict(extra="forbid")

    product_id: str
    region: str
    kind: EventKind

    #: The authoritative ordering key. Always timezone-aware and always UTC.
    instant: datetime

    #: What the feed said, as a wall-clock reading in the region's own zone. Kept for
    #: reporting; never used for ordering or arithmetic.
    local: datetime

    resolution: Resolution = Resolution.EXACT

    #: The row this came from, so a duplicate or a conflict can be traced back.
    source: str = ""

    @field_validator("instant")
    @classmethod
    def _must_be_utc(cls, value: datetime) -> datetime:
        """Refuse a naive or non-UTC instant.

        This is the invariant the whole module rests on. Allowing a naive datetime here
        would let one unconverted feed poison every comparison downstream, and the symptom
        would be a plausible-looking timeline in the wrong order rather than an error.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("instant must be timezone-aware")
        if value.utcoffset() != timedelta(0):
            raise ValueError(f"instant must be UTC, got offset {value.utcoffset()}")
        return value

    @property
    def state_after(self) -> State:
        return STATE_AFTER[self.kind]

    @property
    def key(self) -> tuple[str, str, str, datetime]:
        """Identity for deduplication.

        Keyed on the resolved instant rather than the reported string, so the same event
        arriving as `2025-03-01T09:00:00+09:00` from a regional feed and as
        `2025-03-01T00:00:00Z` from a global one is recognised as one event.
        """
        return (self.product_id, self.region, self.kind.value, self.instant)

    @property
    def sort_key(self) -> tuple[datetime, str, str]:
        """Total order.

        Instant first, then region and kind, so events sharing an instant come out in a
        stable order rather than one that depends on input order. Without the tiebreakers,
        merging the same data twice can produce two different timelines.
        """
        return (self.instant, self.region, self.kind.value)


class Span(BaseModel):
    """A period during which a product was in one state, in one region."""

    model_config = ConfigDict(extra="forbid")

    product_id: str
    region: str
    state: State
    start: datetime
    #: None means still open at the end of the data, which is different from a zero-length
    #: span and must not be rendered as one.
    end: datetime | None = None

    #: The event kind that opened this span, so a report can name the cause.
    opened_by: EventKind

    @property
    def duration(self) -> timedelta | None:
        """Elapsed time, or None if the span is still open.

        Computed from instants, so a span crossing a DST boundary reports the true elapsed
        time. Subtracting local wall-clock readings would give 24 hours for a day that
        actually lasted 23, which is the kind of error that survives review because the
        number looks right.
        """
        if self.end is None:
            return None
        return self.end - self.start

    @property
    def is_open(self) -> bool:
        return self.end is None


class Conflict(BaseModel):
    """Something about the input that a human should look at."""

    model_config = ConfigDict(extra="forbid")

    product_id: str
    region: str
    reason: str
    detail: str = ""


class Timeline(BaseModel):
    """The consolidated history of one product across all regions that reported it."""

    model_config = ConfigDict(extra="forbid")

    product_id: str
    events: list[Event] = Field(default_factory=list)
    spans: list[Span] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)

    #: Exact duplicates removed during the merge, counted rather than listed. The count is
    #: what tells you a feed is double-reporting; the individual rows are rarely useful.
    duplicates_removed: int = 0

    @property
    def regions(self) -> list[str]:
        """Regions present, in the canonical west-to-east order."""
        from lifecycle_tracker.regions import all_codes

        seen = {event.region for event in self.events}
        return [code for code in all_codes() if code in seen]

    @property
    def first_instant(self) -> datetime | None:
        return self.events[0].instant if self.events else None

    @property
    def last_instant(self) -> datetime | None:
        return self.events[-1].instant if self.events else None

    def spans_for(self, region: str) -> list[Span]:
        return [span for span in self.spans if span.region == region]

    def current_state(self, region: str) -> State | None:
        spans = self.spans_for(region)
        return spans[-1].state if spans else None

    def time_in_state(self, region: str, state: State) -> timedelta:
        """Total closed time spent in a state. Open spans contribute nothing.

        Counting an open span as "up to now" would make the answer depend on when the
        report was generated, which is exactly what makes a metric impossible to compare
        across two runs.
        """
        total = timedelta(0)
        for span in self.spans_for(region):
            if span.state is state and span.duration is not None:
                total += span.duration
        return total

    def summary(self) -> str:
        parts = [f"{len(self.events)} event(s)", f"{len(self.regions)} region(s)"]
        if self.duplicates_removed:
            parts.append(f"{self.duplicates_removed} dup(s) removed")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} conflict(s)")
        flagged = sum(1 for e in self.events if e.resolution is not Resolution.EXACT)
        if flagged:
            parts.append(f"{flagged} dst-flagged")
        return ", ".join(parts)
