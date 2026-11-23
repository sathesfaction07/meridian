"""Reading feed files.

One file per region is the common shape, but a combined export works too, because the
region is a column rather than a filename. Bad rows are collected and reported rather
than raised on: these files arrive with tens of thousands of rows, and one unreadable
timestamp should not cost you the other 40,000 events.

What is NOT tolerated is a row that cannot be attributed. A row with no product id or no
region cannot be placed on any timeline, so it is an error rather than a warning.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from lifecycle_tracker.ingest.parse import ParseError, parse_row
from lifecycle_tracker.models import Event
from lifecycle_tracker.regions import RegionError


class FeedError(Exception):
    """A feed file could not be read at all."""


#: Extensions treated as feeds. `.txt` is included because exports get renamed.
FEED_SUFFIXES = frozenset({".csv", ".tsv", ".txt", ".json", ".jsonl", ".ndjson"})


def load_file(path: Path) -> tuple[list[Event], list[str]]:
    """Read one feed file. Returns (events, row problems).

    The problems list is per row and names the row number, because "3 rows failed" without
    saying which is not actionable on a file this size.
    """
    if not path.exists():
        raise FeedError(f"{path} does not exist")

    suffix = path.suffix.lower()
    if suffix in (".json", ".jsonl", ".ndjson"):
        rows = _read_json(path)
    elif suffix in (".csv", ".tsv", ".txt"):
        rows = _read_csv(path)
    else:
        raise FeedError(
            f"{path.name}: unrecognised extension {suffix!r}. "
            f"Expected one of {', '.join(sorted(FEED_SUFFIXES))}"
        )

    events: list[Event] = []
    problems: list[str] = []
    for number, row in enumerate(rows, start=1):
        try:
            events.append(parse_row(row, source=f"{path.name}:{number}"))
        except (ParseError, RegionError) as exc:
            problems.append(f"{path.name}:{number}: {exc}")
        except ValueError as exc:
            # A model validator rejected it -- most likely the UTC invariant on `instant`.
            problems.append(f"{path.name}:{number}: {exc}")

    return (events, problems)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    # utf-8-sig because a spreadsheet export carries a BOM, which otherwise becomes part
    # of the first column's name and makes `product_id` unfindable.
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        rows = [row for row in reader if any((value or "").strip() for value in row.values())]
    if not rows:
        raise FeedError(f"{path.name}: no data rows")
    return rows


def _read_json(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise FeedError(f"{path.name}: file is empty")

    # A `.json` file holding one array and a `.jsonl` file holding one object per line are
    # both common, and the extension is not a reliable guide to which you have.
    if text.lstrip().startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FeedError(f"{path.name}: invalid JSON ({exc.msg} at line {exc.lineno})") from exc
    elif text.lstrip().startswith("{") and "\n" in text and not text.rstrip().endswith("]"):
        data = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise FeedError(f"{path.name}:{number}: invalid JSON ({exc.msg})") from exc
    else:
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FeedError(f"{path.name}: invalid JSON ({exc.msg} at line {exc.lineno})") from exc
        data = loaded if isinstance(loaded, list) else [loaded]

    if isinstance(data, dict):
        data = data.get("events", data.get("items", []))
    if not isinstance(data, list) or not data:
        raise FeedError(f"{path.name}: expected a non-empty list of event objects")

    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise FeedError(f"{path.name}: item {index} is not an object")
    return data


def load_directory(directory: Path) -> tuple[list[Event], list[str]]:
    """Read every feed file in a directory.

    Sorted by name so that two runs over the same directory produce the same order, which
    matters because deduplication keeps the first occurrence.
    """
    if not directory.is_dir():
        raise FeedError(f"{directory} is not a directory")

    paths = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in FEED_SUFFIXES
    )
    if not paths:
        raise FeedError(f"{directory} contains no feed files ({', '.join(sorted(FEED_SUFFIXES))})")

    events: list[Event] = []
    problems: list[str] = []
    for path in paths:
        file_events, file_problems = load_file(path)
        events.extend(file_events)
        problems.extend(file_problems)
    return (events, problems)


def load(target: Path) -> tuple[list[Event], list[str]]:
    """Read a file or a directory, whichever was given."""
    return load_directory(target) if target.is_dir() else load_file(target)
