"""Turning a feed's timestamp into an instant.

This is the module where the data quality problem actually lives. A feed row gives a
timestamp as text, and there are four shapes it arrives in, two of which are ambiguous in
ways that do not raise:

  2025-03-09T02:30:00-05:00   explicit offset -- unambiguous, no zone lookup needed
  2025-03-09T02:30:00Z        UTC -- same
  2025-03-09T02:30:00         naive -- means local time in the region's zone
  2025-03-09                  date only -- means local midnight

The naive forms are the problem. Local time is not a function of instants: on the day a
zone springs forward one wall-clock reading never happens, and on the day it falls back
another happens twice. Python does not raise for either -- it picks something plausible.
So this module detects both cases and records which applied, because the difference
between a timeline that is quietly an hour wrong and one that says so is the whole point.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

from lifecycle_tracker.models import Event, EventKind, Resolution
from lifecycle_tracker.regions import canonical_code, region


class ParseError(Exception):
    """A row could not be turned into an event."""


class Parsed(NamedTuple):
    instant: datetime
    local: datetime
    resolution: Resolution


#: `2025-03-09 02:30:00` with a space instead of a T. Every spreadsheet export does this.
_SPACE_SEPARATOR = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}(:\d{2})?(\.\d+)?)")

#: A trailing `Z`. `fromisoformat` accepts it from 3.11 onward, but a feed sometimes sends
#: `z` lowercase, which it does not.
_TRAILING_Z = re.compile(r"[zZ]$")


def parse_timestamp(raw: str, region_code: str) -> Parsed:
    """Resolve one timestamp against one region's clock.

    Returns the UTC instant, the local wall-clock reading, and how the mapping was made.
    Raises rather than guessing: a timestamp this cannot read would otherwise become an
    event at an invented time, and an invented time in a timeline is worse than a gap.
    """
    text = (raw or "").strip()
    if not text:
        raise ParseError("timestamp is empty")

    text = _SPACE_SEPARATOR.sub(r"\1T\2", text)
    had_z = bool(_TRAILING_Z.search(text))
    if had_z:
        text = _TRAILING_Z.sub("+00:00", text)

    zone = region(region_code).zone()

    try:
        naive_or_aware = datetime.fromisoformat(text)
    except ValueError:
        parsed_date = _try_date_only(text)
        if parsed_date is None:
            raise ParseError(
                f"cannot read timestamp {raw!r}. Expected ISO-8601, for example "
                f"2025-03-09T02:30:00, 2025-03-09T02:30:00Z, or 2025-03-09"
            ) from None
        # A date with no time means local midnight, which can itself be nonexistent or
        # ambiguous in a zone that transitions at midnight.
        return _resolve_naive(datetime.combine(parsed_date, datetime.min.time()), zone)

    if naive_or_aware.tzinfo is not None:
        # The feed told us the offset, so there is nothing to infer and neither DST case
        # can apply. The local reading is still expressed in the region's own zone, so a
        # report can show what a regional team would have seen on their own clock.
        instant = naive_or_aware.astimezone(UTC)
        return Parsed(
            instant, instant.astimezone(zone).replace(tzinfo=None), Resolution.OFFSET_GIVEN
        )

    return _resolve_naive(naive_or_aware, zone)


def _try_date_only(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _resolve_naive(naive: datetime, zone: Any) -> Parsed:
    """Map a wall-clock reading onto an instant, classifying the DST edge cases.

    PEP 495 gives `fold` to disambiguate a repeated wall-clock time, and the two edge
    cases are told apart like this:

      offsets for fold=0 and fold=1 agree              -> ordinary time, EXACT
      they disagree AND a round trip changes the clock -> the time never existed
      they disagree AND a round trip preserves it      -> the time happened twice

    The round trip is the discriminator because in a spring-forward gap Python maps the
    reading forward by the DST offset, so converting to UTC and back lands on a different
    wall clock. In a fall-back overlap both readings are real, so it lands on itself.
    """
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)

    if first.utcoffset() == second.utcoffset():
        instant = first.astimezone(UTC)
        return Parsed(instant, naive, Resolution.EXACT)

    round_tripped = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_tripped != naive:
        # The reading is inside a gap. Python has already shifted it forward, which is a
        # defensible choice, but it must not be presented as though the feed said so.
        instant = first.astimezone(UTC)
        return Parsed(instant, naive, Resolution.NONEXISTENT)

    # The reading happened twice. Resolved to the FIRST occurrence: it is the earlier
    # instant, so a timeline built from it can only under-state a duration, never
    # over-state one, and consistently choosing one end keeps the merge deterministic.
    instant = first.astimezone(UTC)
    return Parsed(instant, naive, Resolution.AMBIGUOUS)


#: Column names a feed might use for each field. Feeds disagree, and renaming a column
#: upstream is usually not an option for a historical export.
#:
#: `instant` is in the timestamp list because it is the column THIS tool writes. Without
#: it, the exporter produced a file the importer could not read -- 29 of 29 rows rejected
#: with "missing required field ['timestamp']" -- so `export` was a dead end. Each half
#: worked; only a round-trip test found it. It is listed LAST so a hand-written feed that
#: supplies both `timestamp` and `instant` still gets its own column preferred.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "product_id": ("product_id", "product", "id", "sku", "item_id"),
    "region": ("region", "marketplace", "market", "locale", "country"),
    "kind": ("kind", "event", "event_type", "type", "action"),
    "timestamp": ("timestamp", "time", "occurred_at", "event_time", "date", "when", "instant"),
}

#: Event names a feed might use. Mapped rather than rejected, because an unmapped kind
#: means the event is dropped and a dropped event silently shortens a timeline.
KIND_ALIASES: dict[str, EventKind] = {
    "created": EventKind.CREATED,
    "create": EventKind.CREATED,
    "new": EventKind.CREATED,
    "listed": EventKind.LISTED,
    "list": EventKind.LISTED,
    "published": EventKind.LISTED,
    "live": EventKind.LISTED,
    "suppressed": EventKind.SUPPRESSED,
    "suppress": EventKind.SUPPRESSED,
    "blocked": EventKind.SUPPRESSED,
    "reinstated": EventKind.REINSTATED,
    "reinstate": EventKind.REINSTATED,
    "restored": EventKind.REINSTATED,
    "unblocked": EventKind.REINSTATED,
    "delisted": EventKind.DELISTED,
    "delist": EventKind.DELISTED,
    "removed": EventKind.DELISTED,
    "archived": EventKind.ARCHIVED,
    "archive": EventKind.ARCHIVED,
    "purged": EventKind.ARCHIVED,
}


def _pick(row: dict[str, Any], field: str) -> Any:
    """Find a field in a row under any of its accepted names.

    Comparison is on casefolded, punctuation-stripped keys, because a CSV header can be
    `Product ID`, `product_id` or `productId` depending on who exported it.
    """
    normalised = {
        key.strip().casefold().replace(" ", "_").replace("-", "_"): value
        for key, value in row.items()
        if key
    }
    for candidate in FIELD_ALIASES[field]:
        if candidate in normalised:
            value = normalised[candidate]
            if value is not None and str(value).strip():
                return value
    return None


def parse_kind(raw: Any) -> EventKind:
    text = str(raw or "").strip().casefold().replace("-", "_")
    if text in KIND_ALIASES:
        return KIND_ALIASES[text]
    known = ", ".join(sorted({k.value for k in EventKind}))
    raise ParseError(f"unknown event kind {raw!r}. Known kinds: {known}")


def parse_row(row: dict[str, Any], source: str = "") -> Event:
    """Turn one feed row into an Event.

    Every failure names the field and the row, because these files arrive with tens of
    thousands of rows and "invalid input" is not a usable diagnostic.
    """
    missing = [field for field in FIELD_ALIASES if _pick(row, field) is None]
    if missing:
        available = ", ".join(sorted(str(k) for k in row if k))
        raise ParseError(
            f"missing required field(s) {missing}. Columns present: {available or 'none'}"
        )

    raw_region = str(_pick(row, "region"))
    code = canonical_code(raw_region)
    kind = parse_kind(_pick(row, "kind"))
    parsed = parse_timestamp(str(_pick(row, "timestamp")), code)

    return Event(
        product_id=str(_pick(row, "product_id")).strip(),
        region=code,
        kind=kind,
        instant=parsed.instant,
        local=parsed.local,
        resolution=parsed.resolution,
        source=source,
    )


def utc_offset_hours(instant: datetime, region_code: str) -> float:
    """The region's offset from UTC at a given instant, in hours.

    Used by reporting rather than by any comparison. Returned as a float because two of
    the sixteen regions are on half-hour offsets and an int would silently truncate them.
    """
    offset = instant.astimezone(region(region_code).zone()).utcoffset() or timedelta(0)
    return offset.total_seconds() / 3600
