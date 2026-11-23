"""Exporting timelines.

JSON Lines for piping, CSV for spreadsheets. Both formats carry the same fields, declared
once here so a column cannot exist in one and be missing from the other.

Every timestamp is written twice: the UTC instant and the local wall-clock reading. Writing
only the instant loses the ability to say "09:00 local", which is how a regional team talks
about its own events; writing only the local reading makes the file impossible to re-order
correctly. A file that can be re-ingested has to carry both, and the round trip is tested.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from lifecycle_tracker.ingest.parse import utc_offset_hours
from lifecycle_tracker.models import Timeline

EVENT_FIELDS = (
    "product_id",
    "region",
    "kind",
    "state_after",
    "instant",
    "local",
    "utc_offset_hours",
    "resolution",
    "source",
)

SPAN_FIELDS = (
    "product_id",
    "region",
    "state",
    "opened_by",
    "start",
    "end",
    "is_open",
    "duration_hours",
)

CONFLICT_FIELDS = ("product_id", "region", "reason", "detail")


def event_row(timeline_event: Any) -> dict[str, Any]:
    return {
        "product_id": timeline_event.product_id,
        "region": timeline_event.region,
        "kind": timeline_event.kind.value,
        "state_after": timeline_event.state_after.value,
        "instant": timeline_event.instant.isoformat(),
        "local": timeline_event.local.isoformat(),
        "utc_offset_hours": utc_offset_hours(timeline_event.instant, timeline_event.region),
        "resolution": timeline_event.resolution.value,
        "source": timeline_event.source,
    }


def span_row(span: Any) -> dict[str, Any]:
    duration = span.duration
    return {
        "product_id": span.product_id,
        "region": span.region,
        "state": span.state.value,
        "opened_by": span.opened_by.value,
        "start": span.start.isoformat(),
        # Empty rather than the string "None", so a spreadsheet shows a blank cell and a
        # JSON consumer gets null. An open span is the absence of an end, not a value.
        "end": span.end.isoformat() if span.end else None,
        "is_open": span.is_open,
        # Hours rather than seconds, because nothing here is measured that finely, and
        # None rather than 0 for an open span -- 0 would be indistinguishable from a real
        # zero-length span caused by two events at the same instant.
        "duration_hours": round(duration.total_seconds() / 3600, 3) if duration else None,
    }


def conflict_row(conflict: Any) -> dict[str, Any]:
    return {
        "product_id": conflict.product_id,
        "region": conflict.region,
        "reason": conflict.reason,
        "detail": conflict.detail,
    }


def _rows(timelines: dict[str, Timeline], what: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for product_id in sorted(timelines):
        timeline = timelines[product_id]
        if what == "events":
            rows.extend(event_row(event) for event in timeline.events)
        elif what == "spans":
            rows.extend(span_row(span) for span in timeline.spans)
        elif what == "conflicts":
            rows.extend(conflict_row(conflict) for conflict in timeline.conflicts)
        else:
            raise ValueError(f"unknown export target {what!r}; expected events, spans or conflicts")
    return rows


def write_jsonl(
    timelines: dict[str, Timeline],
    destination: Path | None = None,
    what: str = "spans",
) -> int:
    """Write one JSON object per line. `None` means stdout, for piping.

    Returns the number of rows written.

    Not streaming, despite the format: `_rows` materialises every row before the first is
    written, because the timelines are already fully in memory by this point and there is
    nothing to gain. The format is JSON Lines so the CONSUMER can stream -- piping into
    `jq` or a line-oriented reader works without loading the file -- which is the half of
    the benefit that actually costs nothing here. A genuinely streaming writer would need
    the whole pipeline to be lazy, and is not what this is.
    """
    rows = _rows(timelines, what)
    if destination is None:
        _dump_jsonl(rows, sys.stdout)
        return len(rows)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        _dump_jsonl(rows, handle)
    return len(rows)


def _dump_jsonl(rows: list[dict[str, Any]], handle: TextIO) -> None:
    for row in rows:
        handle.write(json.dumps(row, default=_json_default) + "\n")


def write_csv(timelines: dict[str, Timeline], directory: Path) -> list[Path]:
    """Write events.csv, spans.csv and conflicts.csv.

    All three are written every time, including when a section is empty. A missing file is
    ambiguous between "nothing to report" and "the run failed part way", and a
    header-only file is not.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for what, fields in (
        ("events", EVENT_FIELDS),
        ("spans", SPAN_FIELDS),
        ("conflicts", CONFLICT_FIELDS),
    ):
        path = directory / f"{what}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields))
            writer.writeheader()
            for row in _rows(timelines, what):
                writer.writerow({key: _csv_value(value) for key, value in row.items()})
        written.append(path)

    return written


def _csv_value(value: Any) -> Any:
    """None becomes an empty cell rather than the four characters `None`."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def summarise(timelines: dict[str, Timeline]) -> dict[str, Any]:
    from lifecycle_tracker.models import Resolution

    events = [event for timeline in timelines.values() for event in timeline.events]
    return {
        "products": len(timelines),
        "events": len(events),
        "spans": sum(len(timeline.spans) for timeline in timelines.values()),
        "regions": len({event.region for event in events}),
        "duplicates_removed": sum(t.duplicates_removed for t in timelines.values()),
        "conflicts": sum(len(timeline.conflicts) for timeline in timelines.values()),
        "dst_ambiguous": sum(1 for e in events if e.resolution is Resolution.AMBIGUOUS),
        "dst_nonexistent": sum(1 for e in events if e.resolution is Resolution.NONEXISTENT),
        "open_spans": sum(1 for t in timelines.values() for s in t.spans if s.is_open),
    }


def write_report(timelines: dict[str, Timeline], directory: Path) -> Path:
    """A single JSON file with the summary and everything in it."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summarise(timelines),
        "events": _rows(timelines, "events"),
        "spans": _rows(timelines, "spans"),
        "conflicts": _rows(timelines, "conflicts"),
    }
    path = directory / "report.json"
    path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    return path


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
