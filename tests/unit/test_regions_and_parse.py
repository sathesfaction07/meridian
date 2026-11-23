"""Regions, timezone facts, and timestamp parsing.

The DST tests are parametrized over transitions DERIVED from the host's tzdata, not over a
hard-coded list of dates. A hard-coded list is a snapshot of what somebody believed; this
asserts against the same data the code consults.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lifecycle_tracker.ingest.parse import (
    ParseError,
    parse_kind,
    parse_row,
    parse_timestamp,
    utc_offset_hours,
)
from lifecycle_tracker.models import EventKind, Resolution
from lifecycle_tracker.regions import (
    ALIASES,
    REGIONS,
    RegionError,
    all_codes,
    canonical_code,
    is_alias,
    region,
)
from tests.conftest import NO_DST_CODES, TRANSITIONS, Transition, row


class TestTheRegionSet:
    def test_there_are_sixteen(self) -> None:
        assert len(REGIONS) == 16

    def test_codes_are_unique(self) -> None:
        codes = [item.code for item in REGIONS]
        assert len(codes) == len(set(codes))

    def test_every_zone_resolves_on_this_host(self) -> None:
        """A missing zone would raise at check time, not at import."""
        for item in REGIONS:
            assert item.zone() is not None

    def test_no_alias_shadows_a_real_code(self) -> None:
        assert not set(ALIASES) & {item.code for item in REGIONS}

    def test_every_alias_points_at_a_real_code(self) -> None:
        real = {item.code for item in REGIONS}
        for alias, target in ALIASES.items():
            assert target in real, f"{alias} -> {target}"

    def test_all_codes_is_the_declared_order_not_alphabetical(self) -> None:
        """West-to-east is the order charts use, so it must not be sorted."""
        assert all_codes() == [item.code for item in REGIONS]
        assert all_codes() != sorted(all_codes())


class TestTimezoneFactsThisSetWasChosenFor:
    """The regions docstring makes claims. These derive them from tzdata instead.

    An earlier version of that docstring asserted that tzdata models Irish time with a
    negative DST offset. It does not, on this build -- Dublin behaves exactly like London.
    A comment that cannot be checked is a comment that drifts, so the interesting
    properties are now asserted here.
    """

    def test_india_is_on_a_half_hour_offset(self) -> None:
        moment = datetime(2025, 6, 15, 12, tzinfo=UTC)
        assert utc_offset_hours(moment, "in") == 5.5

    def test_the_half_hour_offset_is_not_lost_to_integer_truncation(self) -> None:
        assert isinstance(utc_offset_hours(datetime(2025, 6, 15, tzinfo=UTC), "in"), float)

    def test_sydney_runs_the_opposite_phase_to_the_north(self) -> None:
        january = datetime(2025, 1, 15, 12, tzinfo=UTC)
        july = datetime(2025, 7, 15, 12, tzinfo=UTC)
        assert utc_offset_hours(january, "au") > utc_offset_hours(july, "au")
        assert utc_offset_hours(january, "us") < utc_offset_hours(july, "us")

    @pytest.mark.parametrize("code", ["mx", "br", "jp", "sg", "ae", "in"])
    def test_these_regions_do_not_move_their_clocks_in_2025(self, code: str) -> None:
        assert code in NO_DST_CODES

    def test_the_widest_simultaneous_spread_is_seventeen_hours(self) -> None:
        """Claimed in the regions docstring. Derived here rather than trusted."""
        widest = timedelta(0)
        moment = datetime(2025, 1, 1, tzinfo=UTC)
        while moment < datetime(2026, 1, 1, tzinfo=UTC):
            offsets = [moment.astimezone(item.zone()).utcoffset() for item in REGIONS]
            widest = max(widest, max(offsets) - min(offsets))
            moment += timedelta(days=1)
        assert widest == timedelta(hours=17)

    def test_europe_transitions_at_one_instant_but_different_local_readings(self) -> None:
        """Also claimed in the docstring: this is why local time cannot be the merge key."""
        instant = datetime(2025, 3, 30, 1, 0, tzinfo=UTC)
        london = instant.astimezone(region("uk").zone()).strftime("%H:%M")
        berlin = instant.astimezone(region("de").zone()).strftime("%H:%M")
        assert london == "02:00"
        assert berlin == "03:00"
        assert london != berlin


class TestCanonicalCode:
    def test_a_real_code_passes_through(self) -> None:
        assert canonical_code("us") == "us"

    @pytest.mark.parametrize("given", ["US", " us ", "Us", "uS"])
    def test_case_and_whitespace_are_ignored(self, given: str) -> None:
        assert canonical_code(given) == "us"

    @pytest.mark.parametrize(
        "alias,expected",
        [("usa", "us"), ("gb", "uk"), ("uae", "ae"), ("eu-central", "de"), ("apac-south", "in")],
    )
    def test_aliases_resolve(self, alias: str, expected: str) -> None:
        assert canonical_code(alias) == expected

    def test_underscores_are_treated_as_hyphens(self) -> None:
        assert canonical_code("eu_central") == "de"

    def test_an_unknown_code_raises_rather_than_defaulting(self) -> None:
        """Guessing a region means guessing a timezone, worth up to 17 hours of error."""
        with pytest.raises(RegionError, match="unknown region"):
            canonical_code("zz")

    def test_the_error_lists_what_is_accepted(self) -> None:
        with pytest.raises(RegionError, match="Known:.*Aliases:"):
            canonical_code("zz")

    def test_is_alias(self) -> None:
        assert is_alias("gb") is True
        assert is_alias("uk") is False


class TestDstClassificationAgainstTzdata:
    """Every real 2025 transition in every configured region.

    Parametrized over data found by walking tzdata, so adding a region or a rule change
    upstream is covered automatically.
    """

    def test_the_derived_set_is_not_empty(self) -> None:
        """A silent zero would make every test below vacuously pass."""
        assert len(TRANSITIONS) >= 20
        assert {t.expected for t in TRANSITIONS} == {
            Resolution.AMBIGUOUS,
            Resolution.NONEXISTENT,
        }

    @pytest.mark.parametrize("transition", TRANSITIONS, ids=lambda t: f"{t.code}-{t.local:%m%d}")
    def test_each_transition_is_classified_correctly(self, transition: Transition) -> None:
        parsed = parse_timestamp(transition.local.isoformat(), transition.code)
        assert parsed.resolution is transition.expected

    @pytest.mark.parametrize("transition", TRANSITIONS, ids=lambda t: f"{t.code}-{t.local:%m%d}")
    def test_the_local_reading_is_preserved_even_when_it_did_not_exist(
        self, transition: Transition
    ) -> None:
        """The report has to be able to show what the feed said, not only what we made of it."""
        parsed = parse_timestamp(transition.local.isoformat(), transition.code)
        assert parsed.local == transition.local

    @pytest.mark.parametrize("code", NO_DST_CODES)
    def test_a_region_without_dst_never_flags_anything(self, code: str) -> None:
        for month in range(1, 13):
            parsed = parse_timestamp(f"2025-{month:02d}-15T02:30:00", code)
            assert parsed.resolution is Resolution.EXACT

    def test_an_ambiguous_reading_resolves_to_the_earlier_instant(self) -> None:
        """Consistently choosing one end is what keeps the merge deterministic."""
        ambiguous = next(t for t in TRANSITIONS if t.expected is Resolution.AMBIGUOUS)
        parsed = parse_timestamp(ambiguous.local.isoformat(), ambiguous.code)
        zone = region(ambiguous.code).zone()
        first = ambiguous.local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        second = ambiguous.local.replace(tzinfo=zone, fold=1).astimezone(UTC)
        assert first < second
        assert parsed.instant == first


class TestParseTimestamp:
    def test_a_naive_reading_is_local_to_the_region(self) -> None:
        parsed = parse_timestamp("2025-06-15T12:00:00", "jp")
        assert parsed.instant == datetime(2025, 6, 15, 3, 0, tzinfo=UTC)
        assert parsed.resolution is Resolution.EXACT

    def test_an_explicit_offset_is_believed_and_flagged_as_such(self) -> None:
        parsed = parse_timestamp("2025-03-01T09:00:00+09:00", "jp")
        assert parsed.instant == datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
        assert parsed.resolution is Resolution.OFFSET_GIVEN

    def test_an_explicit_offset_cannot_be_dst_affected(self) -> None:
        """There is no zone lookup, so neither edge case can apply."""
        parsed = parse_timestamp("2025-03-09T02:30:00-05:00", "us")
        assert parsed.resolution is Resolution.OFFSET_GIVEN

    def test_the_local_reading_for_an_offset_timestamp_is_in_the_region_zone(self) -> None:
        parsed = parse_timestamp("2025-03-01T00:00:00Z", "jp")
        assert parsed.local == datetime(2025, 3, 1, 9, 0)

    @pytest.mark.parametrize("suffix", ["Z", "z"])
    def test_both_cases_of_the_z_suffix_work(self, suffix: str) -> None:
        parsed = parse_timestamp(f"2025-06-01T12:00:00{suffix}", "uk")
        assert parsed.instant == datetime(2025, 6, 1, 12, 0, tzinfo=UTC)

    def test_a_space_separator_is_accepted(self) -> None:
        """Every spreadsheet export writes a space instead of a T."""
        with_space = parse_timestamp("2025-06-15 12:00:00", "jp")
        with_t = parse_timestamp("2025-06-15T12:00:00", "jp")
        assert with_space.instant == with_t.instant

    def test_a_date_with_no_time_means_local_midnight(self) -> None:
        parsed = parse_timestamp("2025-05-02", "ie")
        assert parsed.local == datetime(2025, 5, 2, 0, 0)
        assert parsed.instant == datetime(2025, 5, 1, 23, 0, tzinfo=UTC)

    def test_seconds_are_optional(self) -> None:
        assert parse_timestamp("2025-06-15 12:00", "jp").local == datetime(2025, 6, 15, 12, 0)

    def test_fractional_seconds_are_accepted(self) -> None:
        parsed = parse_timestamp("2025-06-15T12:00:00.250", "jp")
        assert parsed.local.microsecond == 250000

    @pytest.mark.parametrize(
        "bad", ["", "   ", "not a date", "15/06/2025", "2025-13-01", "June 2025"]
    )
    def test_an_unreadable_timestamp_raises_rather_than_guessing(self, bad: str) -> None:
        """An invented time in a timeline is worse than a missing row."""
        with pytest.raises(ParseError):
            parse_timestamp(bad, "us")

    def test_the_error_shows_an_accepted_shape(self) -> None:
        with pytest.raises(ParseError, match="ISO-8601"):
            parse_timestamp("15/06/2025", "us")

    def test_the_instant_is_always_utc(self) -> None:
        for code in ("us", "in", "au", "jp"):
            parsed = parse_timestamp("2025-06-15T12:00:00", code)
            assert parsed.instant.utcoffset() == timedelta(0)

    def test_the_local_reading_is_always_naive(self) -> None:
        """It is a wall-clock string, not an instant, and attaching a zone invites reuse."""
        for code in ("us", "in", "au"):
            assert parse_timestamp("2025-06-15T12:00:00", code).local.tzinfo is None


class TestParseKind:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("created", EventKind.CREATED),
            ("CREATED", EventKind.CREATED),
            ("new", EventKind.CREATED),
            ("published", EventKind.LISTED),
            ("live", EventKind.LISTED),
            ("blocked", EventKind.SUPPRESSED),
            ("restored", EventKind.REINSTATED),
            ("removed", EventKind.DELISTED),
            ("purged", EventKind.ARCHIVED),
        ],
    )
    def test_synonyms_map(self, given: str, expected: EventKind) -> None:
        assert parse_kind(given) is expected

    def test_an_unknown_kind_raises_rather_than_being_dropped(self) -> None:
        """A dropped event silently shortens a timeline."""
        with pytest.raises(ParseError, match="unknown event kind"):
            parse_kind("relisted-maybe")

    def test_every_event_kind_is_reachable_from_some_alias(self) -> None:
        from lifecycle_tracker.ingest.parse import KIND_ALIASES

        assert set(KIND_ALIASES.values()) == set(EventKind)


class TestParseRow:
    def test_a_plain_row_parses(self) -> None:
        parsed = parse_row(row())
        assert parsed.product_id == "P-1"
        assert parsed.region == "us"
        assert parsed.kind is EventKind.CREATED

    @pytest.mark.parametrize("alias", ["product", "id", "sku", "item_id"])
    def test_product_id_column_aliases(self, alias: str) -> None:
        data = {alias: "X-9", "region": "us", "kind": "created", "timestamp": "2025-01-01"}
        assert parse_row(data).product_id == "X-9"

    @pytest.mark.parametrize("alias", ["marketplace", "market", "locale", "country"])
    def test_region_column_aliases(self, alias: str) -> None:
        data = {"product_id": "X", alias: "jp", "kind": "created", "timestamp": "2025-01-01"}
        assert parse_row(data).region == "jp"

    @pytest.mark.parametrize("alias", ["event", "event_type", "type", "action"])
    def test_kind_column_aliases(self, alias: str) -> None:
        data = {"product_id": "X", "region": "us", alias: "listed", "timestamp": "2025-01-01"}
        assert parse_row(data).kind is EventKind.LISTED

    @pytest.mark.parametrize("alias", ["time", "occurred_at", "event_time", "date", "when"])
    def test_timestamp_column_aliases(self, alias: str) -> None:
        data = {"product_id": "X", "region": "us", "kind": "created", alias: "2025-01-01"}
        assert parse_row(data).instant is not None

    @pytest.mark.parametrize("header", ["Product ID", "product-id", "PRODUCT_ID", " product_id "])
    def test_header_punctuation_and_case_are_normalised(self, header: str) -> None:
        data = {header: "X-1", "region": "us", "kind": "created", "timestamp": "2025-01-01"}
        assert parse_row(data).product_id == "X-1"

    def test_a_missing_field_names_what_was_present(self) -> None:
        with pytest.raises(ParseError, match="Columns present"):
            parse_row({"product_id": "X", "region": "us"})

    def test_a_blank_field_counts_as_missing(self) -> None:
        with pytest.raises(ParseError, match="missing required field"):
            parse_row(row(product_id="   "))

    def test_an_unknown_region_is_reported(self) -> None:
        with pytest.raises(RegionError):
            parse_row(row(region_code="atlantis"))

    def test_the_source_is_recorded_for_traceability(self) -> None:
        assert parse_row(row(), source="feed.csv:7").source == "feed.csv:7"

    def test_an_alias_region_is_canonicalised_on_the_event(self) -> None:
        assert parse_row(row(region_code="eu-central")).region == "de"
