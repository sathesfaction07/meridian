"""Command line interface."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from lifecycle_tracker import __version__
from lifecycle_tracker.config import Settings
from lifecycle_tracker.core.merge import consolidate
from lifecycle_tracker.ingest.feeds import FeedError, load
from lifecycle_tracker.ingest.parse import parse_timestamp
from lifecycle_tracker.models import Resolution
from lifecycle_tracker.regions import ALIASES, REGIONS, RegionError, all_codes, canonical_code
from lifecycle_tracker.render import chart as chart_module
from lifecycle_tracker.render import export, text


def _settings(**overrides: object) -> Settings:
    # Click passes None for every option the user did not give. Dropping them lets the
    # environment and .env supply the value instead of being overwritten with None.
    return Settings(**{key: value for key, value in overrides.items() if value is not None})


def _load(target: Path) -> tuple[list, list[str]]:
    try:
        return load(target)
    except FeedError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
@click.version_option(__version__, prog_name="meridian")
def main() -> None:
    """Consolidate product lifecycle events from regional feeds into one timeline.

    Every comparison is made on UTC instants; local wall-clock readings are kept alongside
    for reporting. Timestamps that fall in a daylight-saving gap or overlap are flagged
    rather than silently resolved.
    """


@main.command()
@click.option(
    "-i", "--input", "feeds", type=click.Path(path_type=Path), help="Feed file or directory."
)
@click.option("-p", "--product", help="Only this product id.")
@click.option("-w", "--width", type=int, help="Width of the text chart, in characters.")
@click.option(
    "--details/--no-details", default=True, help="Show the per-region table and conflicts."
)
@click.option(
    "-o", "--output", "output_dir", type=click.Path(path_type=Path), help="Also write exports here."
)
@click.option(
    "--chart", "chart_dir", type=click.Path(path_type=Path), help="Write a PNG per product here."
)
def show(
    feeds: Path | None,
    product: str | None,
    width: int | None,
    details: bool,
    output_dir: Path | None,
    chart_dir: Path | None,
) -> None:
    """Render timelines as text.

    Exits non-zero if any row failed to parse or any conflict was found, so this composes
    into a pipeline step that gates on feed quality.
    """
    settings = _settings(feeds=feeds, width=width, output_dir=output_dir)
    events, problems = _load(settings.feeds)

    for problem in problems:
        click.secho(f"  {problem}", fg="yellow")
    if problems:
        click.echo("")

    if not events:
        raise click.ClickException("no events could be read")

    timelines = consolidate(events)
    if product:
        if product not in timelines:
            known = ", ".join(sorted(timelines))
            raise click.ClickException(f"unknown product {product!r}. Present: {known}")
        timelines = {product: timelines[product]}

    for product_id in sorted(timelines):
        click.echo(text.render(timelines[product_id], settings.width, details=details))
        click.echo("")

    summary = export.summarise(timelines)
    for label, value in summary.items():
        click.echo(f"  {label:<20}{value}")

    if output_dir is not None:
        written = export.write_csv(timelines, settings.output_dir)
        written.append(export.write_report(timelines, settings.output_dir))
        click.echo("")
        for path in written:
            click.echo(f"  wrote {path}")

    if chart_dir is not None:
        ready, why = chart_module.available()
        if not ready:
            click.secho(f"  charts skipped: {why}", fg="yellow")
        else:
            click.echo("")
            for product_id in sorted(timelines):
                try:
                    path = chart_module.write_chart(
                        timelines[product_id], Path(chart_dir) / f"{product_id}.png"
                    )
                except chart_module.ChartError as exc:
                    click.secho(f"  chart skipped for {product_id}: {exc}", fg="yellow")
                else:
                    click.echo(f"  wrote {path}")

    if problems or summary["conflicts"]:
        sys.exit(1)


@main.command()
@click.option(
    "-i", "--input", "feeds", type=click.Path(path_type=Path), help="Feed file or directory."
)
@click.option(
    "-o",
    "--output",
    "destination",
    type=click.Path(path_type=Path),
    help="Output file. Omit for stdout.",
)
@click.option(
    "--what",
    type=click.Choice(["events", "spans", "conflicts"]),
    default="spans",
    show_default=True,
)
def jsonl(feeds: Path | None, destination: Path | None, what: str) -> None:
    """Write JSON Lines, one object per line, for piping.

    Nothing but the data goes to stdout when no output file is given, so
    `meridian jsonl | jq ...` works. Diagnostics go to stderr for the same reason.
    """
    settings = _settings(feeds=feeds)
    events, problems = _load(settings.feeds)
    for problem in problems:
        click.secho(f"{problem}", fg="yellow", err=True)
    if not events:
        raise click.ClickException("no events could be read")

    timelines = consolidate(events)
    count = export.write_jsonl(timelines, destination, what)
    if destination is not None:
        click.echo(f"wrote {count} {what} row(s) to {destination}", err=True)


@main.command()
@click.option(
    "-i", "--input", "feeds", type=click.Path(path_type=Path), help="Feed file or directory."
)
@click.option(
    "-o", "--output", "output_dir", type=click.Path(path_type=Path), help="Results directory."
)
def export_all(feeds: Path | None, output_dir: Path | None) -> None:
    """Write events.csv, spans.csv, conflicts.csv and report.json."""
    settings = _settings(feeds=feeds, output_dir=output_dir)
    events, problems = _load(settings.feeds)
    for problem in problems:
        click.secho(f"  {problem}", fg="yellow")
    if not events:
        raise click.ClickException("no events could be read")

    timelines = consolidate(events)
    written = export.write_csv(timelines, settings.output_dir)
    written.append(export.write_report(timelines, settings.output_dir))

    for label, value in export.summarise(timelines).items():
        click.echo(f"  {label:<20}{value}")
    click.echo("")
    for path in written:
        click.echo(f"  wrote {path}")


@main.command()
def regions() -> None:
    """List the regions, their zones, and their offsets in January and July.

    Two columns because the offsets differ: showing one date would hide every
    southern-hemisphere region's opposite phase, which is the thing most likely to be
    assumed wrongly.
    """
    from datetime import UTC, datetime

    january = datetime(2025, 1, 15, 12, tzinfo=UTC)
    july = datetime(2025, 7, 15, 12, tzinfo=UTC)

    click.echo(f"  {'code':<6}{'name':<24}{'zone':<22}{'jan':>7}{'jul':>7}   dst")
    for item in REGIONS:
        zone = item.zone()
        jan = january.astimezone(zone).utcoffset().total_seconds() / 3600
        jul = july.astimezone(zone).utcoffset().total_seconds() / 3600
        moves = "yes" if jan != jul else "no"
        click.echo(
            f"  {item.code:<6}{item.name:<24}{item.timezone:<22}{jan:>+7.2f}{jul:>+7.2f}   {moves}"
        )

    click.echo("")
    click.echo(f"  {len(REGIONS)} regions. Aliases accepted on input:")
    for alias, target in sorted(ALIASES.items()):
        click.echo(f"    {alias:<12}-> {target}")


@main.command()
@click.argument("timestamp")
@click.argument("region_code")
def resolve(timestamp: str, region_code: str) -> None:
    """Show how one timestamp maps onto an instant in one region.

    Exists because the DST cases are the part people do not believe until they see it on
    their own data. Exits non-zero when the reading was ambiguous or nonexistent.
    """
    try:
        code = canonical_code(region_code)
    except RegionError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        parsed = parse_timestamp(timestamp, code)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    colour = {
        Resolution.EXACT: "green",
        Resolution.OFFSET_GIVEN: "green",
        Resolution.AMBIGUOUS: "yellow",
        Resolution.NONEXISTENT: "red",
    }[parsed.resolution]

    click.echo(f"  input       {timestamp}")
    click.echo(f"  region      {code}")
    click.echo(f"  local       {parsed.local.isoformat()}")
    click.echo(f"  instant     {parsed.instant.isoformat()}")
    click.echo("  resolution  ", nl=False)
    click.secho(parsed.resolution.value, fg=colour, bold=True)

    if parsed.resolution is Resolution.AMBIGUOUS:
        click.echo("              this local reading happens twice; resolved to the first")
    elif parsed.resolution is Resolution.NONEXISTENT:
        click.echo(
            "              this local reading never happens; shifted forward by the DST offset"
        )

    if parsed.resolution in (Resolution.AMBIGUOUS, Resolution.NONEXISTENT):
        sys.exit(1)


@main.command()
@click.option(
    "-i", "--input", "feeds", type=click.Path(path_type=Path), help="Feed file or directory."
)
def doctor(feeds: Path | None) -> None:
    """Show configuration, region coverage, and whether the feeds parse."""
    settings = _settings(feeds=feeds)

    click.secho("configuration", fg="cyan", bold=True)
    click.echo(f"  feeds       {settings.feeds}")
    click.echo(f"  output_dir  {settings.output_dir}")
    click.echo(f"  width       {settings.width}")

    click.echo("")
    click.secho("regions", fg="cyan", bold=True)
    click.echo(f"  {len(REGIONS)} configured: {', '.join(all_codes())}")
    click.echo(f"  {len(ALIASES)} input aliases accepted")

    click.echo("")
    click.secho("charts", fg="cyan", bold=True)
    ready, why = chart_module.available()
    click.echo(f"  {'available' if ready else 'unavailable'}  {why}")

    click.echo("")
    click.secho("feeds", fg="cyan", bold=True)
    try:
        events, problems = load(settings.feeds)
    except FeedError as exc:
        click.secho(f"  not readable: {exc}", fg="red")
        return

    click.echo(f"  {len(events)} event(s) parsed, {len(problems)} row(s) rejected")
    for problem in problems[:5]:
        click.secho(f"    {problem}", fg="yellow")
    if len(problems) > 5:
        click.echo(f"    ... and {len(problems) - 5} more")

    if events:
        timelines = consolidate(events)
        click.echo("")
        for label, value in export.summarise(timelines).items():
            click.echo(f"  {label:<20}{value}")

    click.echo("")
    click.echo("  dst_ambiguous   a local reading that happens twice; resolved to the first")
    click.echo("  dst_nonexistent a local reading that never happens; shifted forward")
    click.echo("  open_spans      the last span per region. Durations exclude these, so a")
    click.echo("                  currently-active region reports 0 closed time, shown as 0+")


if __name__ == "__main__":  # pragma: no cover
    main()
