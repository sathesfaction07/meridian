"""Text rendering, chart generation and exports."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lifecycle_tracker.core.merge import build_timeline, consolidate
from lifecycle_tracker.ingest.feeds import FeedError, load_directory, load_file
from lifecycle_tracker.models import Resolution, State
from lifecycle_tracker.render import export
from lifecycle_tracker.render.chart import ChartError, available, write_chart
from lifecycle_tracker.render.text import (
    NO_DATA,
    STATE_CHAR,
    _duration,
    axis,
    flagged_events,
    gantt,
    legend,
    render,
    summary_table,
)
from tests.conftest import FEED_DIR, event, requires_matplotlib


@pytest.fixture
def simple() -> object:
    return build_timeline(
        [
            event("created", "2025-01-01T00:00:00+00:00", "us"),
            event("listed", "2025-02-01T00:00:00+00:00", "us"),
            event("suppressed", "2025-03-01T00:00:00+00:00", "us"),
            event("delisted", "2025-04-01T00:00:00+00:00", "us"),
        ]
    )


class TestGantt:
    def test_one_row_per_region(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00", "us"),
                event("listed", "2025-02-01T00:00:00+00:00", "us"),
                event("created", "2025-01-01T00:00:00+00:00", "jp"),
                event("listed", "2025-02-01T00:00:00+00:00", "jp"),
            ]
        )
        assert len(gantt(timeline, 32)) == 2

    def test_the_row_is_the_requested_width(self, simple: object) -> None:
        line = gantt(simple, 40)[0]
        assert line.count("|") == 2
        body = line.split("|")[1]
        assert len(body) == 40

    def test_a_very_small_width_is_clamped(self, simple: object) -> None:
        body = gantt(simple, 1)[0].split("|")[1]
        assert len(body) == 8

    def test_states_appear_in_order(self, simple: object) -> None:
        body = gantt(simple, 60)[0].split("|")[1]
        first_positions = {char: body.index(char) for char in set(body) if char in body}
        assert first_positions[STATE_CHAR[State.DRAFT]] < first_positions[STATE_CHAR[State.ACTIVE]]
        assert (
            first_positions[STATE_CHAR[State.ACTIVE]]
            < first_positions[STATE_CHAR[State.SUPPRESSED]]
        )

    def test_an_open_final_span_is_marked(self, simple: object) -> None:
        """The trailing `>` says this is the last known state, not a confirmed one."""
        assert gantt(simple, 32)[0].rstrip().endswith(">")

    def test_a_region_with_no_data_early_on_shows_blanks(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00", "us"),
                event("listed", "2025-12-01T00:00:00+00:00", "us"),
                event("created", "2025-11-01T00:00:00+00:00", "jp"),
            ]
        )
        rows = {line.split("|")[0].strip(): line.split("|")[1] for line in gantt(timeline, 40)}
        assert rows["jp"].startswith(NO_DATA)
        assert not rows["us"].startswith(NO_DATA)

    def test_no_events_says_so(self) -> None:
        assert "no events" in gantt(build_timeline([]), 32)[0]

    def test_all_events_at_one_instant_says_so_rather_than_dividing_by_zero(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-01-01T00:00:00+00:00"),
            ]
        )
        assert "no interval to chart" in gantt(timeline, 32)[0]


class TestAxis:
    def test_it_is_labelled_utc(self, simple: object) -> None:
        """Sixteen regions have no single local clock to label with."""
        assert any("(UTC)" in line for line in axis(simple, 48))

    def test_it_shows_the_first_and_last_date(self, simple: object) -> None:
        text = "\n".join(axis(simple, 60))
        assert "2025-01-01" in text
        assert "2025-04-01" in text

    def test_a_degenerate_timeline_has_no_axis(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-01-01T00:00:00+00:00"),
            ]
        )
        assert axis(timeline, 40) == []


class TestLegend:
    def test_it_explains_the_open_marker(self) -> None:
        assert any("last known state" in line for line in legend())

    def test_it_narrows_to_the_states_present(self) -> None:
        text = "\n".join(legend({State.ACTIVE}))
        assert "active" in text
        assert "archived" not in text

    def test_every_state_has_a_distinct_character(self) -> None:
        assert len(set(STATE_CHAR.values())) == len(State)

    def test_no_state_character_collides_with_the_no_data_blank(self) -> None:
        assert NO_DATA not in STATE_CHAR.values()


class TestSummaryTableDistinguishesNeverFromStillGoing:
    """The reporting bug this class exists for.

    `time_in_state` counts only CLOSED spans, so a region that is active right now reports
    zero. That is identical to a region that was never active -- opposite facts rendering
    the same way. The `+` marker is what tells them apart.
    """

    def test_a_region_active_right_now_is_marked(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-02-01T00:00:00+00:00"),
            ]
        )
        row = next(line for line in summary_table(timeline) if line.strip().startswith("us"))
        assert timeline.time_in_state("us", State.ACTIVE) == timedelta(0)
        assert "0+" in row

    def test_a_region_that_was_never_active_is_not_marked(self) -> None:
        timeline = build_timeline([event("created", "2025-01-01T00:00:00+00:00")])
        row = next(line for line in summary_table(timeline) if line.strip().startswith("us"))
        assert "0+" not in row
        assert "0" in row

    def test_the_two_cases_render_differently(self) -> None:
        """The whole point: these must not be indistinguishable."""
        still_active = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-02-01T00:00:00+00:00"),
            ]
        )
        never_active = build_timeline([event("created", "2025-01-01T00:00:00+00:00")])
        active_row = next(r for r in summary_table(still_active) if r.strip().startswith("us"))
        never_row = next(r for r in summary_table(never_active) if r.strip().startswith("us"))
        assert active_row.split() != never_row.split()

    def test_closed_time_plus_an_open_span_gets_both(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-01-02T00:00:00+00:00"),
                event("suppressed", "2025-01-05T00:00:00+00:00"),
                event("reinstated", "2025-01-06T00:00:00+00:00"),
            ]
        )
        row = next(line for line in summary_table(timeline) if line.strip().startswith("us"))
        assert "3d+" in row

    def test_a_closed_timeline_has_no_marker_and_no_footnote(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-01-02T00:00:00+00:00"),
                event("delisted", "2025-01-05T00:00:00+00:00"),
            ]
        )
        lines = summary_table(timeline)
        assert not any("still active" in line for line in lines)
        assert "3d+" not in "\n".join(lines)

    def test_the_footnote_appears_when_something_is_open(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-02-01T00:00:00+00:00"),
            ]
        )
        assert any("CLOSED spans only" in line for line in summary_table(timeline))

    def test_the_offset_column_shows_a_half_hour(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-06-01T00:00:00+00:00", "in"),
                event("listed", "2025-07-01T00:00:00+00:00", "in"),
            ]
        )
        assert "+5.50" in "\n".join(summary_table(timeline))


class TestDurationFormatting:
    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(0), "0"),
            (timedelta(hours=5), "5h"),
            (timedelta(days=2), "2d"),
            (timedelta(days=2, hours=3), "2d 3h"),
            (timedelta(minutes=30), "0h"),
        ],
    )
    def test_it_reads_naturally(self, delta: timedelta, expected: str) -> None:
        assert _duration(delta) == expected


class TestFlaggedEvents:
    def test_dst_affected_events_are_listed(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event(
                    "listed",
                    "2025-03-09T07:30:00+00:00",
                    local="2025-03-09T02:30:00",
                    resolution=Resolution.NONEXISTENT,
                ),
            ]
        )
        text = "\n".join(flagged_events(timeline))
        assert "never happened" in text
        assert "02:30" in text

    def test_an_ambiguous_event_says_it_happened_twice(self) -> None:
        timeline = build_timeline(
            [
                event(
                    "created",
                    "2025-11-02T05:30:00+00:00",
                    local="2025-11-02T01:30:00",
                    resolution=Resolution.AMBIGUOUS,
                )
            ]
        )
        assert "happened twice" in "\n".join(flagged_events(timeline))

    def test_an_offset_given_event_is_not_flagged(self) -> None:
        """It needed no zone lookup, so neither edge case can apply."""
        timeline = build_timeline(
            [event("created", "2025-01-01T00:00:00+00:00", resolution=Resolution.OFFSET_GIVEN)]
        )
        assert flagged_events(timeline) == []

    def test_a_clean_timeline_produces_nothing(self, simple: object) -> None:
        assert flagged_events(simple) == []


class TestRender:
    def test_it_includes_every_section(self, shipped_timelines: dict) -> None:
        text = render(shipped_timelines["P-1001"], 48)
        assert "P-1001" in text
        assert "(UTC)" in text
        assert "region" in text
        assert "dst-affected" in text
        assert "conflicts" in text

    def test_no_details_omits_the_table(self, shipped_timelines: dict) -> None:
        text = render(shipped_timelines["P-1001"], 48, details=False)
        assert "conflicts" not in text
        assert "(UTC)" in text


class TestFeedLoading:
    def test_the_shipped_directory_loads_cleanly(self) -> None:
        events, problems = load_directory(FEED_DIR)
        assert events
        assert problems == []

    def test_csv_and_jsonl_are_both_read(self) -> None:
        csv_events, _ = load_file(FEED_DIR / "regional_us_ca.csv")
        jsonl_events, _ = load_file(FEED_DIR / "regional_apac.jsonl")
        assert csv_events and jsonl_events

    def test_a_bad_row_is_reported_without_losing_the_others(self, tmp_path: Path) -> None:
        path = tmp_path / "mixed.csv"
        path.write_text(
            "product_id,region,kind,timestamp\n"
            "P-1,us,created,2025-01-01T00:00:00\n"
            "P-1,us,created,not-a-date\n"
            "P-1,us,listed,2025-02-01T00:00:00\n",
            encoding="utf-8",
        )
        events, problems = load_file(path)
        assert len(events) == 2
        assert len(problems) == 1
        assert "mixed.csv:2" in problems[0]

    def test_an_unknown_region_is_a_row_problem_not_a_file_failure(self, tmp_path: Path) -> None:
        path = tmp_path / "r.csv"
        path.write_text(
            "product_id,region,kind,timestamp\nP-1,atlantis,created,2025-01-01\n", encoding="utf-8"
        )
        events, problems = load_file(path)
        assert events == []
        assert "unknown region" in problems[0]

    def test_a_bom_does_not_hide_the_first_column(self, tmp_path: Path) -> None:
        path = tmp_path / "bom.csv"
        path.write_text(
            "\ufeffproduct_id,region,kind,timestamp\nP-1,us,created,2025-01-01\n", encoding="utf-8"
        )
        events, problems = load_file(path)
        assert len(events) == 1
        assert not problems

    def test_a_json_array_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        path.write_text(
            json.dumps(
                [{"product_id": "P", "region": "us", "kind": "created", "timestamp": "2025-01-01"}]
            ),
            encoding="utf-8",
        )
        assert len(load_file(path)[0]) == 1

    def test_blank_csv_rows_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "gaps.csv"
        path.write_text(
            "product_id,region,kind,timestamp\nP-1,us,created,2025-01-01\n,,,\n", encoding="utf-8"
        )
        events, problems = load_file(path)
        assert len(events) == 1
        assert not problems

    def test_a_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FeedError, match="does not exist"):
            load_file(tmp_path / "nope.csv")

    def test_an_unknown_extension_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "feed.xml"
        path.write_text("<x/>", encoding="utf-8")
        with pytest.raises(FeedError, match="unrecognised extension"):
            load_file(path)

    def test_an_empty_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FeedError, match="no feed files"):
            load_directory(tmp_path)

    def test_an_empty_csv_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "e.csv"
        path.write_text("product_id,region,kind,timestamp\n", encoding="utf-8")
        with pytest.raises(FeedError, match="no data rows"):
            load_file(path)


class TestExport:
    @pytest.fixture
    def timelines(self, shipped_events: list) -> dict:
        return consolidate(shipped_events)

    def test_csv_writes_all_three_files(self, timelines: dict, tmp_path: Path) -> None:
        written = export.write_csv(timelines, tmp_path)
        assert {path.name for path in written} == {"events.csv", "spans.csv", "conflicts.csv"}

    def test_an_empty_section_is_still_written_with_a_header(self, tmp_path: Path) -> None:
        """A missing file cannot be told apart from a run that failed part way."""
        # Two events, because a lone event in a region trips `single_event_region`.
        clean = consolidate(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-02-01T00:00:00+00:00"),
            ]
        )
        export.write_csv(clean, tmp_path)
        text = (tmp_path / "conflicts.csv").read_text(encoding="utf-8")
        assert text.strip() == ",".join(export.CONFLICT_FIELDS)

    def test_the_event_csv_has_the_declared_columns(self, timelines: dict, tmp_path: Path) -> None:
        export.write_csv(timelines, tmp_path)
        with (tmp_path / "events.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert list(rows[0]) == list(export.EVENT_FIELDS)

    def test_an_open_span_writes_a_blank_end_not_the_word_none(
        self, timelines: dict, tmp_path: Path
    ) -> None:
        export.write_csv(timelines, tmp_path)
        with (tmp_path / "spans.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        open_rows = [row for row in rows if row["is_open"] == "true"]
        assert open_rows
        for row in open_rows:
            assert row["end"] == ""
            assert row["duration_hours"] == ""

    def test_jsonl_writes_one_object_per_line(self, timelines: dict, tmp_path: Path) -> None:
        path = tmp_path / "spans.jsonl"
        count = export.write_jsonl(timelines, path, "spans")
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == count
        for line in lines:
            json.loads(line)

    def test_jsonl_open_spans_carry_null_not_a_string(
        self, timelines: dict, tmp_path: Path
    ) -> None:
        path = tmp_path / "spans.jsonl"
        export.write_jsonl(timelines, path, "spans")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            if row["is_open"]:
                assert row["end"] is None
                assert row["duration_hours"] is None

    def test_jsonl_to_stdout(self, timelines: dict, capsys: pytest.CaptureFixture) -> None:
        count = export.write_jsonl(timelines, None, "conflicts")
        captured = capsys.readouterr().out.strip().splitlines()
        assert len(captured) == count

    @pytest.mark.parametrize("what", ["events", "spans", "conflicts"])
    def test_every_target_exports(self, timelines: dict, tmp_path: Path, what: str) -> None:
        assert export.write_jsonl(timelines, tmp_path / f"{what}.jsonl", what) > 0

    def test_an_unknown_target_is_refused(self, timelines: dict, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown export target"):
            export.write_jsonl(timelines, tmp_path / "x.jsonl", "bananas")

    def test_the_event_row_carries_both_clocks(self, timelines: dict) -> None:
        """Only the instant loses "09:00 local"; only local makes re-ordering impossible."""
        row = export.event_row(timelines["P-1001"].events[0])
        assert row["instant"].endswith("+00:00")
        assert "+" not in row["local"][10:]
        assert isinstance(row["utc_offset_hours"], float)

    def test_the_report_summarises(self, timelines: dict, tmp_path: Path) -> None:
        path = export.write_report(timelines, tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        summary = payload["summary"]
        assert summary["products"] == len(timelines)
        assert summary["dst_nonexistent"] > 0
        assert summary["open_spans"] > 0

    def test_the_summary_counts_match_the_data(self, timelines: dict) -> None:
        summary = export.summarise(timelines)
        assert summary["events"] == sum(len(t.events) for t in timelines.values())
        assert summary["spans"] == sum(len(t.spans) for t in timelines.values())


class TestExportRoundTrip:
    """An exported file must be re-ingestable, or the export is a dead end."""

    def test_events_survive_a_round_trip(self, shipped_events: list, tmp_path: Path) -> None:
        timelines = consolidate(shipped_events)
        path = tmp_path / "events.csv"
        export.write_csv(timelines, tmp_path)

        reloaded, problems = load_file(path)
        assert not problems
        assert len(reloaded) == sum(len(t.events) for t in timelines.values())

    def test_the_instants_are_identical_after_a_round_trip(
        self, shipped_events: list, tmp_path: Path
    ) -> None:
        """The exported `instant` column carries an explicit offset, so nothing is inferred."""
        timelines = consolidate(shipped_events)
        export.write_csv(timelines, tmp_path)
        reloaded, _ = load_file(tmp_path / "events.csv")

        before = sorted(e.instant for t in timelines.values() for e in t.events)
        after = sorted(e.instant for e in reloaded)
        assert before == after

    def test_a_round_tripped_file_rebuilds_the_same_spans(
        self, shipped_events: list, tmp_path: Path
    ) -> None:
        original = consolidate(shipped_events)
        export.write_csv(original, tmp_path)
        reloaded, _ = load_file(tmp_path / "events.csv")
        rebuilt = consolidate(reloaded)

        assert sorted(rebuilt) == sorted(original)
        for product_id, timeline in original.items():
            assert [(s.region, s.state, s.start, s.end) for s in rebuilt[product_id].spans] == [
                (s.region, s.state, s.start, s.end) for s in timeline.spans
            ]


class TestChart:
    def test_availability_is_reported(self) -> None:
        ready, why = available()
        assert isinstance(ready, bool)
        assert ready or "matplotlib" in why

    @requires_matplotlib
    def test_a_png_is_written(self, shipped_timelines: dict, tmp_path: Path) -> None:
        path = write_chart(shipped_timelines["P-1001"], tmp_path / "P-1001.png")
        assert path.exists()
        assert path.stat().st_size > 5000
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    @requires_matplotlib
    def test_the_parent_directory_is_created(self, shipped_timelines: dict, tmp_path: Path) -> None:
        path = write_chart(shipped_timelines["P-1001"], tmp_path / "deep" / "nested" / "x.png")
        assert path.exists()

    @requires_matplotlib
    def test_an_empty_timeline_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ChartError, match="no events"):
            write_chart(build_timeline([]), tmp_path / "x.png")

    @requires_matplotlib
    def test_a_degenerate_timeline_is_refused(self, tmp_path: Path) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-01-01T00:00:00+00:00"),
            ]
        )
        with pytest.raises(ChartError, match="no interval"):
            write_chart(timeline, tmp_path / "x.png")

    @requires_matplotlib
    def test_every_state_has_a_colour(self) -> None:
        from lifecycle_tracker.render.chart import STATE_COLOUR

        assert set(STATE_COLOUR) == set(State)
        assert len(set(STATE_COLOUR.values())) == len(State)


class TestNowIsUtc:
    def test_the_header_clock_is_utc(self) -> None:
        from lifecycle_tracker.render.text import now_utc

        assert now_utc().utcoffset() == timedelta(0)
        assert abs((now_utc() - datetime.now(UTC)).total_seconds()) < 5
