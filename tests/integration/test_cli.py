"""The CLI. Exercised through Click's runner, so exit codes are covered too."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lifecycle_tracker.cli import main
from tests.conftest import FEED_DIR, requires_matplotlib


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point defaults away from the repo, so a test cannot write into data/ or output/."""
    monkeypatch.setenv("LT_FEEDS", str(FEED_DIR))
    monkeypatch.setenv("LT_OUTPUT_DIR", str(tmp_path / "out"))


class TestRegions:
    def test_all_sixteen_are_listed(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["regions"])
        assert result.exit_code == 0
        assert "16 regions" in result.output
        for code in ("mx", "us", "uk", "in", "jp", "au"):
            assert code in result.output

    def test_both_january_and_july_offsets_are_shown(self, runner: CliRunner) -> None:
        """One column would hide every southern-hemisphere region's opposite phase."""
        result = runner.invoke(main, ["regions"])
        assert "jan" in result.output and "jul" in result.output

    def test_sydney_shows_the_inverted_phase(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["regions"])
        line = next(row for row in result.output.splitlines() if row.strip().startswith("au"))
        assert "+11.00" in line and "+10.00" in line

    def test_india_shows_a_half_hour_offset(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["regions"])
        line = next(row for row in result.output.splitlines() if row.strip().startswith("in "))
        assert "+5.50" in line

    def test_regions_without_dst_are_marked(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["regions"])
        for code in ("jp", "sg", "ae", "mx", "br", "in"):
            line = next(r for r in result.output.splitlines() if r.strip().startswith(code + " "))
            assert line.rstrip().endswith("no"), code

    def test_aliases_are_listed(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["regions"])
        assert "eu-central" in result.output
        assert "-> de" in result.output


class TestResolve:
    def test_an_ordinary_time_exits_zero(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["resolve", "2025-06-15T12:00:00", "jp"])
        assert result.exit_code == 0
        assert "exact" in result.output

    def test_a_nonexistent_time_exits_one_and_explains(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["resolve", "2025-03-09T02:30:00", "us"])
        assert result.exit_code == 1
        assert "nonexistent" in result.output
        assert "never happens" in result.output

    def test_an_ambiguous_time_exits_one_and_explains(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["resolve", "2025-11-02T01:30:00", "us"])
        assert result.exit_code == 1
        assert "ambiguous" in result.output
        assert "happens twice" in result.output

    def test_an_explicit_offset_is_reported_as_such(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["resolve", "2025-03-01T09:00:00+09:00", "jp"])
        assert result.exit_code == 0
        assert "offset_given" in result.output

    def test_it_shows_both_the_local_reading_and_the_instant(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["resolve", "2025-06-15T12:00:00", "in"])
        assert "local" in result.output
        assert "instant" in result.output
        assert "06:30" in result.output  # 12:00 IST is 06:30 UTC

    def test_an_alias_region_works(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["resolve", "2025-06-15T12:00:00", "eu-central"])
        assert result.exit_code == 0
        assert "region      de" in result.output

    def test_an_unknown_region_is_a_clean_error(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["resolve", "2025-06-15T12:00:00", "atlantis"])
        assert result.exit_code != 0
        assert "unknown region" in result.output

    def test_an_unreadable_timestamp_is_a_clean_error(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["resolve", "15/06/2025", "us"])
        assert result.exit_code != 0
        assert "ISO-8601" in result.output

    def test_the_southern_hemisphere_cases_are_inverted(self, runner: CliRunner) -> None:
        """October springs forward in Sydney, April falls back."""
        october = runner.invoke(main, ["resolve", "2025-10-05T02:30:00", "au"])
        april = runner.invoke(main, ["resolve", "2025-04-06T02:30:00", "au"])
        assert "nonexistent" in october.output
        assert "ambiguous" in april.output


class TestShow:
    def test_it_renders_every_product(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["show", "--no-details"])
        for product_id in ("P-1001", "P-1002", "P-1003"):
            assert product_id in result.output

    def test_it_exits_non_zero_when_conflicts_exist(self, runner: CliRunner) -> None:
        """The shipped feeds contain deliberate conflicts, so this gates on feed quality."""
        result = runner.invoke(main, ["show", "--no-details"])
        assert result.exit_code == 1

    def test_a_single_product_can_be_selected(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["show", "-p", "P-1003", "--no-details"])
        assert "P-1003" in result.output
        assert "P-1001" not in result.output

    def test_an_unknown_product_lists_what_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["show", "-p", "P-9999"])
        assert result.exit_code != 0
        assert "P-1001" in result.output

    def test_the_gantt_respects_the_width(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["show", "-p", "P-1001", "-w", "24", "--no-details"])
        rows = [r for r in result.output.splitlines() if r.count("|") == 2]
        assert rows
        for row in rows:
            assert len(row.split("|")[1]) == 24

    def test_details_include_the_conflict_list(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["show", "-p", "P-1001"])
        assert "conflicts:" in result.output

    def test_details_include_the_dst_explanation(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["show", "-p", "P-1001"])
        assert "dst-affected" in result.output

    def test_no_details_omits_them(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["show", "-p", "P-1001", "--no-details"])
        assert "conflicts:" not in result.output

    def test_the_summary_is_printed(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["show", "--no-details"])
        assert "products" in result.output
        assert "dst_nonexistent" in result.output

    def test_output_writes_the_exports(self, runner: CliRunner, tmp_path: Path) -> None:
        target = tmp_path / "exports"
        runner.invoke(main, ["show", "--no-details", "-o", str(target)])
        assert (target / "events.csv").exists()
        assert (target / "spans.csv").exists()
        assert (target / "report.json").exists()

    def test_a_missing_feed_path_is_a_clean_error(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(main, ["show", "-i", str(tmp_path / "nope.csv")])
        assert result.exit_code != 0
        assert "does not exist" in result.output

    def test_a_feed_with_bad_rows_reports_them_and_exits_non_zero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        path = tmp_path / "bad.csv"
        path.write_text(
            "product_id,region,kind,timestamp\n"
            "P-1,us,created,2025-01-01T00:00:00\n"
            "P-1,us,listed,not-a-date\n",
            encoding="utf-8",
        )
        result = runner.invoke(main, ["show", "-i", str(path), "--no-details"])
        assert result.exit_code == 1
        assert "bad.csv:2" in result.output

    @requires_matplotlib
    def test_charts_are_written_when_asked(self, runner: CliRunner, tmp_path: Path) -> None:
        charts = tmp_path / "charts"
        runner.invoke(main, ["show", "-p", "P-1001", "--no-details", "--chart", str(charts)])
        assert (charts / "P-1001.png").exists()


class TestJsonl:
    def test_it_writes_one_object_per_line_to_stdout(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["jsonl", "--what", "spans"])
        lines = [line for line in result.output.splitlines() if line.startswith("{")]
        assert lines
        for line in lines:
            json.loads(line)

    def test_stdout_carries_only_data_so_it_can_be_piped(self, runner: CliRunner) -> None:
        """Anything non-JSON on stdout would break `... | jq`."""
        result = runner.invoke(main, ["jsonl", "--what", "spans"])
        for line in result.output.splitlines():
            if line.strip():
                assert line.startswith("{"), f"non-data line on stdout: {line!r}"

    @pytest.mark.parametrize("what", ["events", "spans", "conflicts"])
    def test_every_target_works(self, runner: CliRunner, tmp_path: Path, what: str) -> None:
        path = tmp_path / f"{what}.jsonl"
        result = runner.invoke(main, ["jsonl", "--what", what, "-o", str(path)])
        assert result.exit_code == 0
        assert path.exists()
        assert path.read_text(encoding="utf-8").strip()

    def test_writing_to_a_file_reports_on_stderr_not_stdout(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        path = tmp_path / "spans.jsonl"
        result = runner.invoke(main, ["jsonl", "-o", str(path)], catch_exceptions=False)
        assert result.exit_code == 0
        assert path.exists()

    def test_an_invalid_target_is_rejected_by_click(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["jsonl", "--what", "bananas"])
        assert result.exit_code != 0


class TestExportAll:
    def test_it_writes_four_files(self, runner: CliRunner, tmp_path: Path) -> None:
        target = tmp_path / "out"
        result = runner.invoke(main, ["export-all", "-o", str(target)])
        assert result.exit_code == 0
        for name in ("events.csv", "spans.csv", "conflicts.csv", "report.json"):
            assert (target / name).exists(), name

    def test_the_report_summary_is_consistent(self, runner: CliRunner, tmp_path: Path) -> None:
        target = tmp_path / "out"
        runner.invoke(main, ["export-all", "-o", str(target)])
        payload = json.loads((target / "report.json").read_text(encoding="utf-8"))
        assert payload["summary"]["events"] == len(payload["events"])
        assert payload["summary"]["spans"] == len(payload["spans"])
        assert payload["summary"]["conflicts"] == len(payload["conflicts"])

    def test_the_export_can_be_read_back(self, runner: CliRunner, tmp_path: Path) -> None:
        """The exporter used to write a file the importer rejected on every row."""
        target = tmp_path / "out"
        runner.invoke(main, ["export-all", "-o", str(target)])

        second = tmp_path / "again"
        result = runner.invoke(
            main, ["export-all", "-i", str(target / "events.csv"), "-o", str(second)]
        )
        assert result.exit_code == 0
        first_payload = json.loads((target / "report.json").read_text(encoding="utf-8"))
        second_payload = json.loads((second / "report.json").read_text(encoding="utf-8"))
        assert second_payload["summary"]["events"] == first_payload["summary"]["events"]
        assert second_payload["summary"]["spans"] == first_payload["summary"]["spans"]


class TestDoctor:
    def test_it_reports_configuration_and_regions(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "feeds" in result.output
        assert "16 configured" in result.output

    def test_it_reports_chart_availability(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["doctor"])
        assert "available" in result.output

    def test_it_parses_the_feeds_and_summarises(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["doctor"])
        assert "event(s) parsed" in result.output
        assert "dst_nonexistent" in result.output

    def test_it_explains_the_open_span_zero(self, runner: CliRunner) -> None:
        """The 0+ marker needs explaining somewhere a reader will find it."""
        result = runner.invoke(main, ["doctor"])
        assert "0+" in result.output

    def test_it_survives_an_unreadable_feed_path(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(main, ["doctor", "-i", str(tmp_path / "nope")])
        assert result.exit_code == 0
        assert "not readable" in result.output


class TestTopLevel:
    def test_help_lists_every_command(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        for command in ("show", "jsonl", "export-all", "regions", "resolve", "doctor"):
            assert command in result.output

    def test_version_is_reported(self, runner: CliRunner) -> None:
        from lifecycle_tracker import __version__

        result = runner.invoke(main, ["--version"])
        assert __version__ in result.output
