#!/usr/bin/env bash
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $1"; }
ok()   { echo -e "${GREEN} ok ${NC} $1"; }
warn() { echo -e "${YELLOW}warn${NC} $1"; }
fail() { echo -e "${RED}fail${NC} $1"; exit 1; }

WITH_CHART=0
for arg in "$@"; do
    case "$arg" in
        --chart|--all) WITH_CHART=1 ;;
        -h|--help)
            cat <<'USAGE'
Usage: bash setup.sh [options]

  --chart   also install matplotlib, for PNG gantt charts
  --all     everything
  -h        this message

Everything except the PNG chart works without the extra. There is no hosted-model option:
this is a data quality problem solved by strict parsing, not by a model.
USAGE
            exit 0 ;;
        *) fail "unknown option: $arg (try --help)" ;;
    esac
done

info "Checking Python"
command -v python3 >/dev/null 2>&1 || {
    echo "python3 was not found. Install it first:"
    echo "  macOS:         brew install python@3.12"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv"
    fail "python3 missing"
}
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [ "$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')" != "1" ]; then
    echo "Python 3.11 or newer is required (StrEnum, datetime.UTC). Found $PY_VER."
    fail "python $PY_VER too old"
fi
ok "Python $PY_VER"

info "Checking that the timezone database is available"
python3 - <<'PYCHECK' || fail "no usable tzdata"
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Every assertion below depends on real tzdata. A slim container often has none, and the
# failure would otherwise surface as a confusing region error much later.
try:
    ZoneInfo("Australia/Sydney")
    ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    print("     the IANA timezone database is not available.")
    print("     On a slim Linux image: pip install tzdata")
    print("     On Debian/Ubuntu:      sudo apt install tzdata")
    sys.exit(1)
PYCHECK
ok "tzdata present"

info "Creating virtual environment"
[ -d .venv ] || python3 -m venv .venv || fail "could not create .venv (Debian/Ubuntu: sudo apt install python3-venv)"
ok ".venv ready"

EXTRAS="dev"
[ "$WITH_CHART" = "1" ] && EXTRAS="$EXTRAS,chart"

info "Installing dependencies [$EXTRAS]"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[$EXTRAS]" || fail "pip install failed"
ok "dependencies installed"

info "Verifying imports"
.venv/bin/python - <<'PYCHECK' || fail "core imports failed"
import click                                                    # noqa: F401
from pydantic_settings import BaseSettings                      # noqa: F401
from lifecycle_tracker.core.merge import consolidate            # noqa: F401
from lifecycle_tracker.ingest.parse import parse_timestamp      # noqa: F401
from lifecycle_tracker.render.text import render                # noqa: F401
PYCHECK
ok "imports resolve"

info "Deriving each region's DST transitions from tzdata and checking the classifier"
.venv/bin/python - <<'PYCHECK' || fail "a DST transition was classified wrongly"
from datetime import UTC, datetime, timedelta

from lifecycle_tracker.ingest.parse import parse_timestamp
from lifecycle_tracker.models import Resolution
from lifecycle_tracker.regions import REGIONS

checked = 0
without_dst = []

for item in REGIONS:
    zone = item.zone()
    previous = None
    transitions = 0
    moment = datetime(2025, 1, 1, tzinfo=UTC)
    while moment < datetime(2026, 1, 1, tzinfo=UTC):
        offset = moment.astimezone(zone).utcoffset()
        if previous is not None and offset != previous:
            transitions += 1
            local_after = moment.astimezone(zone)
            if offset > previous:
                probe = (local_after - timedelta(minutes=30)).replace(tzinfo=None)
                expected = Resolution.NONEXISTENT
            else:
                probe = (local_after + timedelta(minutes=30)).replace(tzinfo=None)
                expected = Resolution.AMBIGUOUS
            got = parse_timestamp(probe.isoformat(), item.code).resolution
            assert got is expected, f"{item.code} {probe}: expected {expected}, got {got}"
            checked += 1
        previous = offset
        moment += timedelta(hours=1)
    if transitions == 0:
        without_dst.append(item.code)

assert checked >= 20, f"only found {checked} transitions; tzdata may be incomplete"
print(f"     {checked} real transitions across {len(REGIONS)} regions, all classified correctly")
print(f"     {len(without_dst)} regions never move their clocks: {', '.join(without_dst)}")

# The two facts the region set was chosen to exercise, derived rather than trusted.
widest = timedelta(0)
moment = datetime(2025, 1, 1, tzinfo=UTC)
while moment < datetime(2026, 1, 1, tzinfo=UTC):
    offsets = [moment.astimezone(r.zone()).utcoffset() for r in REGIONS]
    widest = max(widest, max(offsets) - min(offsets))
    moment += timedelta(days=1)
assert widest == timedelta(hours=17), widest
print(f"     widest simultaneous spread: {widest} (enough to invert an event order)")
PYCHECK
ok "DST classification is correct"

info "Checking that durations are measured in instants, not wall clocks"
.venv/bin/python - <<'PYCHECK' || fail "a duration was computed from wall-clock readings"
from datetime import timedelta

from lifecycle_tracker.ingest.parse import parse_timestamp

cases = [
    ("us", "2025-03-08T12:00:00", "2025-03-10T12:00:00", 47, "spring forward"),
    ("us", "2025-11-01T12:00:00", "2025-11-03T12:00:00", 49, "fall back"),
    ("au", "2025-10-04T12:00:00", "2025-10-06T12:00:00", 47, "southern spring forward"),
    ("au", "2025-04-05T12:00:00", "2025-04-07T12:00:00", 49, "southern fall back"),
    ("jp", "2025-03-08T12:00:00", "2025-03-10T12:00:00", 48, "no DST"),
]
for code, start, end, hours, label in cases:
    a = parse_timestamp(start, code).instant
    b = parse_timestamp(end, code).instant
    assert b - a == timedelta(hours=hours), f"{code} {label}: got {b - a}"
    note = "" if hours == 48 else "  (the wall clock says 48h)"
    print(f"     {code} {label:<24}{hours}h{note}")
PYCHECK
ok "durations are instant-based"

info "Loading the shipped feeds and checking the merge invariants"
.venv/bin/python - <<'PYCHECK' || fail "a merge invariant was violated"
import random
from collections import Counter
from pathlib import Path

from lifecycle_tracker.core.merge import consolidate
from lifecycle_tracker.ingest.feeds import load_directory
from lifecycle_tracker.models import Resolution

events, problems = load_directory(Path("data/feeds"))
assert not problems, problems
timelines = consolidate(events)
print(f"     {len(events)} events -> {len(timelines)} product(s)")

seen = {e.resolution for e in events}
assert seen == set(Resolution), f"the sample feeds miss {set(Resolution) - seen}"
counts = Counter(e.resolution.value for e in events)
print(f"     every resolution exercised: {dict(counts)}")

# Spans must tile contiguously per region, with only the last one open.
for product_id, timeline in timelines.items():
    for code in timeline.regions:
        spans = timeline.spans_for(code)
        assert len(spans) == sum(1 for e in timeline.events if e.region == code)
        for earlier, later in zip(spans, spans[1:], strict=False):
            assert earlier.end == later.start, f"{product_id}/{code} has a gap"
            assert not earlier.is_open, f"{product_id}/{code} has a non-final open span"
        assert spans[-1].is_open, f"{product_id}/{code} final span is closed"

# No span may be bounded by an event from a different region.
for product_id, timeline in timelines.items():
    by_region: dict[str, set] = {}
    for e in timeline.events:
        by_region.setdefault(e.region, set()).add(e.instant)
    for span in timeline.spans:
        assert span.start in by_region[span.region]
        assert span.end is None or span.end in by_region[span.region]

def fingerprint(built):
    return tuple(
        (
            pid,
            built[pid].duplicates_removed,
            tuple((s.region, s.state.value, s.start, s.end) for s in built[pid].spans),
        )
        for pid in sorted(built)
    )

expected = fingerprint(timelines)
for seed in range(10):
    shuffled = list(events)
    random.Random(seed).shuffle(shuffled)
    assert fingerprint(consolidate(shuffled)) == expected, f"seed {seed} changed the timeline"
print("     order-independent across 10 shuffles")

doubled = consolidate(list(events) + list(events))
for pid, timeline in timelines.items():
    assert [e.key for e in doubled[pid].events] == [e.key for e in timeline.events]
print("     supplying the same data twice is idempotent")
PYCHECK
ok "merge invariants hold"

info "Checking that an exported file can be read back in"
.venv/bin/python - <<'PYCHECK' || fail "the export is not re-ingestable"
import tempfile
from pathlib import Path

from lifecycle_tracker.core.merge import consolidate
from lifecycle_tracker.ingest.feeds import load_directory, load_file
from lifecycle_tracker.render import export

# This is the regression: the exporter wrote an `instant` column while the importer only
# accepted `timestamp`, so every one of the 29 rows was rejected. Each half worked; the
# round trip did not.
events, _ = load_directory(Path("data/feeds"))
original = consolidate(events)

tmp = Path(tempfile.mkdtemp())
export.write_csv(original, tmp)

reloaded, problems = load_file(tmp / "events.csv")
assert not problems, f"the export could not be re-read: {problems[:2]}"
assert len(reloaded) == sum(len(t.events) for t in original.values())

rebuilt = consolidate(reloaded)
assert sorted(rebuilt) == sorted(original)
for pid, timeline in original.items():
    assert [(s.region, s.state, s.start, s.end) for s in rebuilt[pid].spans] == [
        (s.region, s.state, s.start, s.end) for s in timeline.spans
    ]
print(f"     {len(reloaded)} events exported and re-ingested; spans identical")
PYCHECK
ok "export round-trips"

info "Checking that a still-active region is not reported as never active"
.venv/bin/python - <<'PYCHECK' || fail "the open-span reporting trap is back"
from lifecycle_tracker.core.merge import build_timeline
from lifecycle_tracker.models import Event, EventKind, State
from datetime import UTC, datetime

def make(kind, day):
    moment = datetime(2025, day // 100, day % 100, tzinfo=UTC)
    return Event(
        product_id="P", region="us", kind=EventKind(kind),
        instant=moment, local=moment.replace(tzinfo=None),
    )

from lifecycle_tracker.render.text import summary_table

# Active right now: the open span contributes no closed time, so this reports 0.
still_active = build_timeline([make("created", 101), make("listed", 201)])
# Never active at all: also 0.
never_active = build_timeline([make("created", 101)])

assert still_active.time_in_state("us", State.ACTIVE).total_seconds() == 0
assert never_active.time_in_state("us", State.ACTIVE).total_seconds() == 0

active_row = next(r for r in summary_table(still_active) if r.strip().startswith("us"))
never_row = next(r for r in summary_table(never_active) if r.strip().startswith("us"))
assert "0+" in active_row, active_row
assert "0+" not in never_row, never_row
assert active_row.split() != never_row.split()
print("     'live now' renders 0+ and 'never live' renders 0 -- they are distinguishable")
PYCHECK
ok "open spans are marked"

if [ "$WITH_CHART" = "1" ]; then
    info "Rendering a chart"
    .venv/bin/python - <<'PYCHECK' || fail "the chart could not be written"
import tempfile
from pathlib import Path

from lifecycle_tracker.core.merge import consolidate
from lifecycle_tracker.ingest.feeds import load_directory
from lifecycle_tracker.render.chart import write_chart

events, _ = load_directory(Path("data/feeds"))
target = Path(tempfile.mkdtemp()) / "P-1001.png"
written = write_chart(consolidate(events)["P-1001"], target)
assert written.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
print(f"     wrote a {written.stat().st_size} byte PNG")
PYCHECK
    ok "charts work"
else
    warn "matplotlib not installed, so PNG charts are unavailable (bash setup.sh --chart)"
fi

info "Running the test suite"
.venv/bin/python -m pytest -q || fail "tests failed"
ok "tests pass"

cat <<'DONE'

Setup complete.

  source .venv/bin/activate

  lifecycle-tracker doctor
  lifecycle-tracker regions
  lifecycle-tracker resolve 2025-03-09T02:30:00 us      # a local time that never happened
  lifecycle-tracker resolve 2025-11-02T01:30:00 us      # one that happened twice

  lifecycle-tracker show -i data/feeds
  lifecycle-tracker jsonl -i data/feeds --what spans | head
  lifecycle-tracker export-all -i data/feeds -o out/

`show` exits non-zero if any row failed to parse or any conflict was found, so it composes
into a pipeline step that gates on feed quality. `resolve` exits non-zero for a timestamp
that was ambiguous or nonexistent.

Durations come from instants, so a span across a DST boundary is 23 or 25 hours, not 24.
A region that is active right now shows 0+ closed time, not 0.
DONE
