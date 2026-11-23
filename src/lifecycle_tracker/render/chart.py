"""Rendering a timeline as a matplotlib chart.

Optional. The import is deferred so that everything else -- ingest, merge, text rendering,
export -- works on a machine without matplotlib, which is most CI images.

The text renderer in `text.py` is the one that gets asserted in tests. This produces a
better artefact and can only be checked by a human looking at it, so the two exist for
different reasons rather than one superseding the other.
"""

from __future__ import annotations

from pathlib import Path

from lifecycle_tracker.models import State, Timeline
from lifecycle_tracker.regions import all_codes


class ChartError(Exception):
    """The chart could not be produced."""


#: Colour per state. Ordered by severity rather than by hue, so a wall of green reads as
#: healthy and a wall of red does not need the legend to be understood.
STATE_COLOUR: dict[State, str] = {
    State.DRAFT: "#b0b7c3",
    State.ACTIVE: "#2e8b57",
    State.SUPPRESSED: "#c8442c",
    State.DELISTED: "#7a6a99",
    State.ARCHIVED: "#4a4a4a",
}


def write_chart(timeline: Timeline, path: Path, *, width: float = 12.0) -> Path:
    """Write a gantt-style chart for one product.

    Every bar is positioned by INSTANT, so the sixteen regions share one axis. Positioning
    by local time would give each row its own axis and make vertical comparison -- the
    only reason to draw the rows together -- meaningless.

    An OPEN span is drawn to the right edge -- the last event across all regions -- but
    hatched and at reduced opacity, because the data says nothing about what happened
    after it. This is the same choice the text renderer makes with a trailing `>`: show
    the last KNOWN state, and mark that it is not confirmed. Stopping the bar short would
    read as a delisting that was never reported.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")  # no display needed, and none available in CI
        import matplotlib.dates as mdates
        import matplotlib.patches as mpatches
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ChartError(
            "charts need matplotlib. Install it with: pip install 'meridian[chart]'"
        ) from exc

    if not timeline.events:
        raise ChartError(f"{timeline.product_id}: no events to chart")

    start, end = timeline.first_instant, timeline.last_instant
    if start == end:
        raise ChartError(
            f"{timeline.product_id}: every event is at {start}, so there is no interval to draw"
        )

    order = {code: index for index, code in enumerate(all_codes())}
    regions = sorted(timeline.regions, key=lambda code: order.get(code, 99))

    height = max(2.0, 0.45 * len(regions) + 1.4)
    figure, axes = plt.subplots(figsize=(width, height))

    for row, code in enumerate(regions):
        for span in timeline.spans_for(code):
            # An open span runs to the chart edge, hatched -- see the docstring.
            finish = span.end if span.end is not None else end
            length = mdates.date2num(finish) - mdates.date2num(span.start)
            if length <= 0:
                # Two events at the same instant produce a zero-length span. Drawn as a
                # visible tick so it is not silently invisible; the merge also reports it
                # as a `simultaneous_events` conflict.
                axes.plot(
                    [mdates.date2num(span.start)],
                    [row],
                    marker="|",
                    color=STATE_COLOUR[span.state],
                    markersize=10,
                )
                continue
            axes.barh(
                row,
                length,
                left=mdates.date2num(span.start),
                height=0.6,
                color=STATE_COLOUR[span.state],
                edgecolor="white",
                linewidth=0.5,
                hatch="///" if span.is_open else None,
                alpha=0.55 if span.is_open else 1.0,
            )

    axes.set_yticks(range(len(regions)))
    axes.set_yticklabels(regions)
    axes.invert_yaxis()  # west at the top, matching the canonical order
    axes.xaxis_date()
    axes.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    figure.autofmt_xdate()

    axes.set_xlabel("UTC")
    axes.set_title(f"{timeline.product_id}   {timeline.summary()}")
    axes.grid(axis="x", linestyle=":", alpha=0.4)

    present = {span.state for span in timeline.spans}
    handles = [
        mpatches.Patch(color=STATE_COLOUR[state], label=state.value)
        for state in STATE_COLOUR
        if state in present
    ]
    handles.append(
        mpatches.Patch(facecolor="white", edgecolor="grey", hatch="///", label="open (last known)")
    )
    axes.legend(
        handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.28), ncol=6, fontsize=8
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    figure.tight_layout()
    figure.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(figure)
    return path


def available() -> tuple[bool, str]:
    """Whether charts can be produced here, and why not if they cannot."""
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return (False, "matplotlib is not installed - run: pip install 'meridian[chart]'")
    return (True, "")
