"""Deduplication, ordering, span construction and conflict detection.

The property classes here are the probe I ran while building this, promoted into the suite.
They run against the real shipped feeds, so editing `data/feeds/` and breaking an invariant
fails the build.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from lifecycle_tracker.core.merge import (
    build_timeline,
    consolidate,
    deduplicate,
    find_cross_region_conflicts,
)
from lifecycle_tracker.core.spans import build_all_spans, build_spans, state_at
from lifecycle_tracker.ingest.parse import parse_timestamp
from lifecycle_tracker.models import STATE_AFTER, EventKind, Resolution, State
from tests.conftest import event


class TestStateMapping:
    def test_every_event_kind_maps_to_a_state(self) -> None:
        """A missing entry would raise KeyError at check time, on real data."""
        assert set(STATE_AFTER) == set(EventKind)

    def test_every_state_is_reachable(self) -> None:
        assert set(STATE_AFTER.values()) == set(State)

    def test_listed_and_reinstated_both_mean_active(self) -> None:
        assert STATE_AFTER[EventKind.LISTED] is STATE_AFTER[EventKind.REINSTATED] is State.ACTIVE


class TestEventInvariants:
    def test_a_naive_instant_is_refused(self) -> None:
        """One unconverted feed would poison every comparison downstream."""
        from lifecycle_tracker.models import Event

        with pytest.raises(ValueError, match="timezone-aware"):
            Event(
                product_id="P",
                region="us",
                kind=EventKind.CREATED,
                instant=datetime(2025, 1, 1),
                local=datetime(2025, 1, 1),
            )

    def test_a_non_utc_instant_is_refused(self) -> None:
        from zoneinfo import ZoneInfo

        from lifecycle_tracker.models import Event

        with pytest.raises(ValueError, match="must be UTC"):
            Event(
                product_id="P",
                region="jp",
                kind=EventKind.CREATED,
                instant=datetime(2025, 1, 1, tzinfo=ZoneInfo("Asia/Tokyo")),
                local=datetime(2025, 1, 1),
            )

    def test_sort_key_breaks_ties_deterministically(self) -> None:
        """Without tiebreakers, merging the same data twice can give two orderings."""
        a = event("listed", "2025-01-01T00:00:00+00:00", "us")
        b = event("created", "2025-01-01T00:00:00+00:00", "jp")
        assert sorted([a, b], key=lambda e: e.sort_key) == sorted([b, a], key=lambda e: e.sort_key)

    def test_the_dedup_key_ignores_how_the_time_was_written(self) -> None:
        """The same event from a regional and a global feed must collapse to one."""
        regional = event("created", "2025-03-01T09:00:00+09:00", "jp")
        global_feed = event("created", "2025-03-01T00:00:00+00:00", "jp")
        assert regional.key == global_feed.key


class TestDeduplicate:
    def test_identical_events_collapse(self) -> None:
        one = event("created", "2025-01-01T00:00:00+00:00")
        kept, removed = deduplicate([one, one, one])
        assert len(kept) == 1
        assert removed == 2

    def test_different_kinds_at_the_same_instant_are_kept(self) -> None:
        kept, removed = deduplicate(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-01-01T00:00:00+00:00"),
            ]
        )
        assert len(kept) == 2
        assert removed == 0

    def test_the_same_kind_in_different_regions_is_kept(self) -> None:
        kept, _ = deduplicate(
            [
                event("created", "2025-01-01T00:00:00+00:00", "us"),
                event("created", "2025-01-01T00:00:00+00:00", "jp"),
            ]
        )
        assert len(kept) == 2

    def test_the_first_occurrence_survives(self) -> None:
        first = event("created", "2025-01-01T00:00:00+00:00")
        second = first.model_copy(update={"source": "later.csv:1"})
        kept, _ = deduplicate([first, second])
        assert kept[0].source == first.source


class TestSpansTile:
    def test_each_event_opens_exactly_one_span(self) -> None:
        events = [
            event("created", "2025-01-01T00:00:00+00:00"),
            event("listed", "2025-02-01T00:00:00+00:00"),
            event("suppressed", "2025-03-01T00:00:00+00:00"),
        ]
        spans, _ = build_spans(events)
        assert len(spans) == len(events)

    def test_spans_are_contiguous(self) -> None:
        events = [
            event("created", "2025-01-01T00:00:00+00:00"),
            event("listed", "2025-02-01T00:00:00+00:00"),
            event("delisted", "2025-03-01T00:00:00+00:00"),
        ]
        spans, _ = build_spans(events)
        for earlier, later in zip(spans, spans[1:], strict=False):
            assert earlier.end == later.start

    def test_the_final_span_is_open(self) -> None:
        """The data says nothing about what happened after the last event."""
        spans, _ = build_spans([event("created", "2025-01-01T00:00:00+00:00")])
        assert spans[-1].is_open
        assert spans[-1].end is None
        assert spans[-1].duration is None

    def test_only_the_final_span_is_open(self) -> None:
        events = [
            event("created", "2025-01-01T00:00:00+00:00"),
            event("listed", "2025-02-01T00:00:00+00:00"),
        ]
        spans, _ = build_spans(events)
        assert [span.is_open for span in spans] == [False, True]

    def test_events_supplied_out_of_order_are_sorted(self) -> None:
        events = [
            event("delisted", "2025-03-01T00:00:00+00:00"),
            event("created", "2025-01-01T00:00:00+00:00"),
            event("listed", "2025-02-01T00:00:00+00:00"),
        ]
        spans, _ = build_spans(events)
        assert [span.state for span in spans] == [State.DRAFT, State.ACTIVE, State.DELISTED]

    def test_the_span_records_what_opened_it(self) -> None:
        spans, _ = build_spans([event("reinstated", "2025-01-01T00:00:00+00:00")])
        assert spans[0].opened_by is EventKind.REINSTATED

    def test_an_empty_list_gives_nothing(self) -> None:
        assert build_spans([]) == ([], [])


class TestSpansAreBuiltPerRegion:
    """The failure mode this guards is plausible and silent.

    Events from sixteen regions merged into one instant-ordered stream look like a single
    history. Walking that stream would let a Tokyo event close a Mexico City span, and the
    resulting chart would look entirely reasonable.
    """

    def test_a_mixed_region_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="one region at a time"):
            build_spans(
                [
                    event("created", "2025-01-01T00:00:00+00:00", "us"),
                    event("created", "2025-01-02T00:00:00+00:00", "jp"),
                ]
            )

    def test_build_all_spans_groups_correctly(self) -> None:
        events = [
            event("created", "2025-01-01T00:00:00+00:00", "us"),
            event("created", "2025-01-02T00:00:00+00:00", "jp"),
            event("listed", "2025-01-03T00:00:00+00:00", "us"),
            event("listed", "2025-01-04T00:00:00+00:00", "jp"),
        ]
        spans, _ = build_all_spans(events)
        assert len(spans) == 4
        for span in spans:
            assert span.end is None or span.end > span.start

    def test_no_span_is_bounded_by_a_foreign_event(self) -> None:
        events = [
            event("created", "2025-01-01T00:00:00+00:00", "us"),
            event("created", "2025-01-02T00:00:00+00:00", "jp"),
            event("listed", "2025-01-03T00:00:00+00:00", "us"),
        ]
        spans, _ = build_all_spans(events)
        instants = {"us": set(), "jp": set()}
        for item in events:
            instants[item.region].add(item.instant)
        for span in spans:
            assert span.start in instants[span.region]
            if span.end is not None:
                assert span.end in instants[span.region]

    def test_spans_come_out_in_the_canonical_region_order(self) -> None:
        """West to east, so a report reads left to right by clock rather than by name."""
        events = [
            event("created", "2025-01-01T00:00:00+00:00", "jp"),
            event("created", "2025-01-01T00:00:00+00:00", "us"),
            event("created", "2025-01-01T00:00:00+00:00", "au"),
        ]
        spans, _ = build_all_spans(events)
        assert [span.region for span in spans] == ["us", "jp", "au"]


class TestConflicts:
    def test_a_first_event_that_is_not_created_is_flagged(self) -> None:
        _, conflicts = build_spans([event("listed", "2025-01-01T00:00:00+00:00")])
        assert [c.reason for c in conflicts] == ["no_creation_event"]

    def test_a_normal_start_is_not_flagged(self) -> None:
        _, conflicts = build_spans([event("created", "2025-01-01T00:00:00+00:00")])
        assert conflicts == []

    def test_an_event_after_archived_is_flagged(self) -> None:
        _, conflicts = build_spans(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("archived", "2025-02-01T00:00:00+00:00"),
                event("listed", "2025-03-01T00:00:00+00:00"),
            ]
        )
        assert "event_after_terminal" in [c.reason for c in conflicts]

    def test_the_contradicting_event_is_still_applied(self) -> None:
        """Flag it and apply it: the feed is the record of what was reported."""
        spans, _ = build_spans(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("archived", "2025-02-01T00:00:00+00:00"),
                event("listed", "2025-03-01T00:00:00+00:00"),
            ]
        )
        assert spans[-1].state is State.ACTIVE

    def test_a_redundant_transition_is_flagged(self) -> None:
        _, conflicts = build_spans(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-02-01T00:00:00+00:00"),
                event("reinstated", "2025-03-01T00:00:00+00:00"),
            ]
        )
        assert "redundant_transition" in [c.reason for c in conflicts]

    def test_reinstated_without_suppression_is_flagged(self) -> None:
        _, conflicts = build_spans(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("reinstated", "2025-02-01T00:00:00+00:00"),
            ]
        )
        assert "reinstated_without_suppression" in [c.reason for c in conflicts]

    def test_simultaneous_events_are_flagged(self) -> None:
        """Their order comes from a tiebreaker; the feed genuinely does not say."""
        _, conflicts = build_spans(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-01-01T00:00:00+00:00"),
            ]
        )
        assert "simultaneous_events" in [c.reason for c in conflicts]

    def test_simultaneous_events_still_produce_a_zero_length_span(self) -> None:
        """Zero-length is different from open and must not be rendered as one."""
        spans, _ = build_spans(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-01-01T00:00:00+00:00"),
            ]
        )
        assert spans[0].duration == timedelta(0)
        assert spans[0].is_open is False
        assert spans[1].is_open is True

    def test_a_single_event_region_is_flagged(self) -> None:
        events = [
            event("created", "2025-01-01T00:00:00+00:00", "us"),
            event("listed", "2025-02-01T00:00:00+00:00", "us"),
            event("created", "2025-01-01T00:00:00+00:00", "jp"),
        ]
        conflicts = find_cross_region_conflicts(events)
        assert [(c.region, c.reason) for c in conflicts] == [("jp", "single_event_region")]

    def test_conflicts_carry_the_product_and_region(self) -> None:
        _, conflicts = build_spans([event("listed", "2025-01-01T00:00:00+00:00", "jp", "P-9")])
        assert conflicts[0].product_id == "P-9"
        assert conflicts[0].region == "jp"


class TestStateAt:
    @pytest.fixture
    def spans(self) -> list:
        built, _ = build_spans(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-02-01T00:00:00+00:00"),
                event("delisted", "2025-03-01T00:00:00+00:00"),
            ]
        )
        return built

    def test_before_the_data_is_none(self, spans: list) -> None:
        assert state_at(spans, datetime(2024, 1, 1, tzinfo=UTC)) is None

    def test_inside_a_span(self, spans: list) -> None:
        assert state_at(spans, datetime(2025, 1, 15, tzinfo=UTC)) is State.DRAFT

    def test_a_boundary_belongs_to_the_later_span(self, spans: list) -> None:
        """Half-open on the right, or an instant on a boundary matches two spans."""
        assert state_at(spans, datetime(2025, 2, 1, tzinfo=UTC)) is State.ACTIVE

    def test_the_instant_just_before_a_boundary(self, spans: list) -> None:
        moment = datetime(2025, 2, 1, tzinfo=UTC) - timedelta(microseconds=1)
        assert state_at(spans, moment) is State.DRAFT

    def test_after_the_last_event_reports_the_open_state(self, spans: list) -> None:
        assert state_at(spans, datetime(2030, 1, 1, tzinfo=UTC)) is State.DELISTED


class TestDurationsAreInstantBased:
    """Subtracting wall-clock readings would give 24 hours for a day that lasted 23.

    That error survives review because the number looks right.
    """

    def test_a_span_across_spring_forward_is_an_hour_short(self) -> None:
        start = parse_timestamp("2025-03-08T12:00:00", "us").instant
        end = parse_timestamp("2025-03-10T12:00:00", "us").instant
        assert end - start == timedelta(hours=47)

    def test_a_span_across_fall_back_is_an_hour_long(self) -> None:
        start = parse_timestamp("2025-11-01T12:00:00", "us").instant
        end = parse_timestamp("2025-11-03T12:00:00", "us").instant
        assert end - start == timedelta(hours=49)

    def test_the_southern_hemisphere_moves_the_other_way(self) -> None:
        forward_start = parse_timestamp("2025-10-04T12:00:00", "au").instant
        forward_end = parse_timestamp("2025-10-06T12:00:00", "au").instant
        assert forward_end - forward_start == timedelta(hours=47)

        back_start = parse_timestamp("2025-04-05T12:00:00", "au").instant
        back_end = parse_timestamp("2025-04-07T12:00:00", "au").instant
        assert back_end - back_start == timedelta(hours=49)

    def test_a_region_without_dst_is_exactly_48_hours(self) -> None:
        start = parse_timestamp("2025-03-08T12:00:00", "jp").instant
        end = parse_timestamp("2025-03-10T12:00:00", "jp").instant
        assert end - start == timedelta(hours=48)

    def test_the_span_duration_reflects_it(self) -> None:
        events = [
            event("created", parse_timestamp("2025-03-08T12:00:00", "us").instant.isoformat()),
            event("listed", parse_timestamp("2025-03-10T12:00:00", "us").instant.isoformat()),
        ]
        spans, _ = build_spans(events)
        assert spans[0].duration == timedelta(hours=47)


class TestTimeInState:
    def test_open_spans_contribute_nothing(self) -> None:
        """Counting "up to now" would make the answer depend on when the report ran."""
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-02-01T00:00:00+00:00"),
            ]
        )
        assert timeline.time_in_state("us", State.ACTIVE) == timedelta(0)
        assert timeline.current_state("us") is State.ACTIVE

    def test_closed_time_is_summed_across_separate_spans(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00"),
                event("listed", "2025-01-02T00:00:00+00:00"),
                event("suppressed", "2025-01-03T00:00:00+00:00"),
                event("reinstated", "2025-01-04T00:00:00+00:00"),
                event("delisted", "2025-01-05T00:00:00+00:00"),
            ]
        )
        assert timeline.time_in_state("us", State.ACTIVE) == timedelta(days=2)

    def test_a_region_with_no_data_reports_zero(self) -> None:
        timeline = build_timeline([event("created", "2025-01-01T00:00:00+00:00", "us")])
        assert timeline.time_in_state("jp", State.ACTIVE) == timedelta(0)
        assert timeline.current_state("jp") is None


class TestBuildTimeline:
    def test_regions_come_back_in_canonical_order(self) -> None:
        timeline = build_timeline(
            [
                event("created", "2025-01-01T00:00:00+00:00", "au"),
                event("created", "2025-01-01T00:00:00+00:00", "us"),
                event("created", "2025-01-01T00:00:00+00:00", "de"),
            ]
        )
        assert timeline.regions == ["us", "de", "au"]

    def test_first_and_last_instant_are_the_bounds(self) -> None:
        timeline = build_timeline(
            [
                event("listed", "2025-06-01T00:00:00+00:00"),
                event("created", "2025-01-01T00:00:00+00:00"),
                event("delisted", "2025-12-01T00:00:00+00:00"),
            ]
        )
        assert timeline.first_instant == datetime(2025, 1, 1, tzinfo=UTC)
        assert timeline.last_instant == datetime(2025, 12, 1, tzinfo=UTC)

    def test_mixing_products_is_refused(self) -> None:
        with pytest.raises(ValueError, match="one product at a time"):
            build_timeline(
                [
                    event("created", "2025-01-01T00:00:00+00:00", product_id="A"),
                    event("created", "2025-01-01T00:00:00+00:00", product_id="B"),
                ]
            )

    def test_an_empty_list_gives_an_empty_timeline(self) -> None:
        timeline = build_timeline([])
        assert timeline.events == []
        assert timeline.summary().startswith("0 event")

    def test_consolidate_groups_by_product(self) -> None:
        timelines = consolidate(
            [
                event("created", "2025-01-01T00:00:00+00:00", product_id="A"),
                event("created", "2025-01-01T00:00:00+00:00", product_id="B"),
                event("listed", "2025-02-01T00:00:00+00:00", product_id="A"),
            ]
        )
        assert sorted(timelines) == ["A", "B"]
        assert len(timelines["A"].events) == 2


class TestPropertiesOverTheShippedFeeds:
    """The probe from development, promoted into the suite.

    These run against `data/feeds/`, so editing the sample data and breaking an invariant
    fails the build rather than somebody's first run.
    """

    def test_the_shipped_feeds_parse_cleanly(self, shipped_events: list) -> None:
        assert len(shipped_events) >= 25

    def test_they_exercise_every_resolution(self, shipped_events: list) -> None:
        """Sample data that does not hit the interesting cases is not sample data."""
        seen = {item.resolution for item in shipped_events}
        assert seen == set(Resolution)

    def test_they_cover_several_regions_including_a_half_hour_offset(
        self, shipped_events: list
    ) -> None:
        regions = {item.region for item in shipped_events}
        assert len(regions) >= 8
        assert "in" in regions

    def test_spans_tile_contiguously_for_every_region(self, shipped_timelines: dict) -> None:
        for product_id, timeline in shipped_timelines.items():
            for code in timeline.regions:
                spans = timeline.spans_for(code)
                for earlier, later in zip(spans, spans[1:], strict=False):
                    assert earlier.end == later.start, f"{product_id}/{code}"
                    assert not earlier.is_open, f"{product_id}/{code}"
                assert spans[-1].is_open, f"{product_id}/{code}"

    def test_there_is_one_span_per_event_per_region(self, shipped_timelines: dict) -> None:
        for product_id, timeline in shipped_timelines.items():
            counts = Counter(item.region for item in timeline.events)
            for code, count in counts.items():
                assert len(timeline.spans_for(code)) == count, f"{product_id}/{code}"

    @pytest.mark.parametrize("seed", range(12))
    def test_shuffling_the_input_changes_nothing(self, shipped_events: list, seed: int) -> None:
        def fingerprint(timelines: dict) -> tuple:
            return tuple(
                (
                    product_id,
                    timelines[product_id].duplicates_removed,
                    tuple(
                        (e.region, e.kind.value, e.instant) for e in timelines[product_id].events
                    ),
                    tuple(
                        (s.region, s.state.value, s.start, s.end)
                        for s in timelines[product_id].spans
                    ),
                    tuple(sorted((c.region, c.reason) for c in timelines[product_id].conflicts)),
                )
                for product_id in sorted(timelines)
            )

        shuffled = list(shipped_events)
        random.Random(seed).shuffle(shuffled)
        assert fingerprint(consolidate(shuffled)) == fingerprint(consolidate(shipped_events))

    def test_supplying_the_data_twice_is_idempotent(
        self, shipped_events: list, shipped_timelines: dict
    ) -> None:
        doubled = consolidate(list(shipped_events) + list(shipped_events))
        for product_id, timeline in shipped_timelines.items():
            other = doubled[product_id]
            assert [e.key for e in other.events] == [e.key for e in timeline.events]
            assert [(s.state, s.start, s.end) for s in other.spans] == [
                (s.state, s.start, s.end) for s in timeline.spans
            ]
            raw = timeline.duplicates_removed + len(timeline.events)
            assert other.duplicates_removed == raw * 2 - len(timeline.events)

    def test_no_span_is_bounded_by_a_foreign_region_event(self, shipped_timelines: dict) -> None:
        for product_id, timeline in shipped_timelines.items():
            instants: dict[str, set] = {}
            for item in timeline.events:
                instants.setdefault(item.region, set()).add(item.instant)
            for span in timeline.spans:
                assert span.start in instants[span.region], f"{product_id}/{span.region}"
                if span.end is not None:
                    assert span.end in instants[span.region], f"{product_id}/{span.region}"

    def test_bounds_match_the_extremes(self, shipped_timelines: dict) -> None:
        for timeline in shipped_timelines.values():
            assert timeline.first_instant == min(e.instant for e in timeline.events)
            assert timeline.last_instant == max(e.instant for e in timeline.events)

    def test_every_instant_is_utc(self, shipped_events: list) -> None:
        for item in shipped_events:
            assert item.instant.utcoffset() == timedelta(0)
