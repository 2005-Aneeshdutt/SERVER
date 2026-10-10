"""Cost/quality chart as a standalone SVG (no plotting dependency)."""

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from eval.metrics import Point

W, H = 760, 480
LEFT, RIGHT, TOP, BOTTOM = 72, 24, 56, 64
INK, MUTED, GRID = "#1f2937", "#6b7280", "#e5e7eb"


@dataclass(frozen=True)
class Series:
    name: str
    points: Sequence[Point]
    color: str
    dash: str = ""


@dataclass(frozen=True)
class Marker:
    label: str
    strong_share: float
    quality: float
    color: str


def _envelope(points: Sequence[Point]) -> list[tuple[float, float]]:
    best, out = float("-inf"), []
    for p in sorted(points, key=lambda p: p.strong_share):
        best = max(best, p.quality)
        out.append((p.strong_share, best))
    return out


def render(
    title: str,
    subtitle: str,
    series: Sequence[Series],
    markers: Sequence[Marker],
    cheap_quality: float,
) -> str:
    y_min = max(0.0, (int(cheap_quality * 10) / 10) - 0.05)

    def sx(share: float) -> float:
        return LEFT + share * (W - LEFT - RIGHT)

    def sy(quality: float) -> float:
        return TOP + (1 - (quality - y_min) / (1 - y_min)) * (H - TOP - BOTTOM)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" font-family="system-ui, -apple-system, Segoe UI, sans-serif">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
        f'<text x="{LEFT}" y="26" font-size="17" font-weight="600" fill="{INK}">'
        f"{escape(title)}</text>",
        f'<text x="{LEFT}" y="44" font-size="12" fill="{MUTED}">{escape(subtitle)}</text>',
    ]
    for i in range(11):  # x grid: % calls to the strong model
        x = sx(i / 10)
        parts.append(
            f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{H - BOTTOM}" stroke="{GRID}"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{H - BOTTOM + 18}" font-size="11" fill="{MUTED}" '
            f'text-anchor="middle">{i * 10}%</text>'
        )
    steps = round((1 - y_min) / 0.05)
    for i in range(steps + 1):  # y grid: quality
        q = y_min + i * 0.05
        y = sy(q)
        parts.append(
            f'<line x1="{LEFT}" y1="{y:.1f}" x2="{W - RIGHT}" y2="{y:.1f}" stroke="{GRID}"/>'
        )
        parts.append(
            f'<text x="{LEFT - 8}" y="{y + 4:.1f}" font-size="11" fill="{MUTED}" '
            f'text-anchor="end">{q * 100:.0f}%</text>'
        )
    parts.append(
        f'<text x="{(LEFT + W - RIGHT) / 2}" y="{H - 20}" font-size="12" fill="{INK}" '
        'text-anchor="middle">Share of requests sent to the strong model (cost)</text>'
    )
    parts.append(
        f'<text transform="translate(18 {(TOP + H - BOTTOM) / 2}) rotate(-90)" font-size="12" '
        f'fill="{INK}" text-anchor="middle">Answers as good as the strong model</text>'
    )
    for s in series:
        coords = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in _envelope(s.points))
        dash = f' stroke-dasharray="{s.dash}"' if s.dash else ""
        parts.append(
            f'<polyline points="{coords}" fill="none" stroke="{s.color}" stroke-width="2.5"'
            f' stroke-linejoin="round"{dash}/>'
        )
    for m in markers:
        x, y = sx(m.strong_share), sy(m.quality)
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#ffffff" stroke="{m.color}" '
            'stroke-width="2.5"/>'
        )
        parts.append(
            f'<text x="{x + 9:.1f}" y="{y + 15:.1f}" font-size="11" fill="{INK}">'
            f"{escape(m.label)}</text>"
        )
    legend_y = TOP + 12
    for i, s in enumerate(series):
        y = legend_y + i * 20
        x = W - RIGHT - 230
        dash = f' stroke-dasharray="{s.dash}"' if s.dash else ""
        parts.append(
            f'<line x1="{x}" y1="{y}" x2="{x + 26}" y2="{y}" stroke="{s.color}" '
            f'stroke-width="2.5"{dash}/>'
        )
        parts.append(
            f'<text x="{x + 34}" y="{y + 4}" font-size="12" fill="{INK}">{escape(s.name)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
