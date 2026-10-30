"""A small line chart drawn as SVG on the server: no JavaScript, no chart library. One series per
chart, so reach and followers each get their own axis instead of sharing a dual-axis plot."""

from collections.abc import Sequence
from datetime import datetime

from markupsafe import Markup, escape

WIDTH, HEIGHT = 640, 180
LEFT, RIGHT, TOP, BOTTOM = 56, 16, 12, 28
GRID_LINES = 4


def _ticks(high: float) -> list[float]:
    step = high / GRID_LINES if high else 1
    return [step * i for i in range(GRID_LINES + 1)]


def _label(value: float) -> str:
    return f"{value / 1000:.1f}k" if value >= 10_000 else f"{value:,.0f}"


def line_chart(
    points: Sequence[tuple[datetime, int | float | None]], *, title: str, series: str
) -> Markup:
    """`points` oldest first; None values leave a gap. `series` picks the CSS class that colours
    the line (`primary` or `secondary`). Empty when there is nothing to draw."""
    values = [v for _, v in points if v is not None]
    if not values:
        return Markup("")
    high = max(values) or 1
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    step_x = plot_w / max(len(points) - 1, 1)

    def x(i: int) -> float:
        return LEFT + i * step_x

    def y(v: float) -> float:
        return TOP + plot_h - v / high * plot_h

    parts = [
        f'<svg class="chart chart--{escape(series)}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'role="img" aria-label="{escape(title)}">'
    ]
    for tick in _ticks(high):
        parts.append(
            f'<line class="chart-grid" x1="{LEFT}" x2="{WIDTH - RIGHT}" '
            f'y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>'
            f'<text class="chart-axis" x="{LEFT - 8}" y="{y(tick) + 4:.1f}" '
            f'text-anchor="end">{_label(tick)}</text>'
        )
    # First and last day under the axis; the table below the chart has every day.
    for i in {0, len(points) - 1}:
        anchor = "start" if i == 0 else "end"
        parts.append(
            f'<text class="chart-axis" x="{x(i):.1f}" y="{HEIGHT - 8}" '
            f'text-anchor="{anchor}">{points[i][0]:%d %b}</text>'
        )
    segment: list[str] = []
    segments: list[list[str]] = [segment]
    for i, (_, value) in enumerate(points):
        if value is None:
            segment = []
            segments.append(segment)
        else:
            segment.append(f"{x(i):.1f},{y(value):.1f}")
    for seg in segments:
        if seg:
            parts.append(f'<polyline class="chart-line" points="{" ".join(seg)}"/>')
    for i, (day, value) in enumerate(points):
        if value is not None:
            # A native tooltip per point: the hover layer without JavaScript.
            parts.append(
                f'<circle class="chart-point" cx="{x(i):.1f}" cy="{y(value):.1f}" r="3">'
                f"<title>{day:%Y-%m-%d}: {value:,}</title></circle>"
            )
    parts.append("</svg>")
    return Markup("".join(parts))
