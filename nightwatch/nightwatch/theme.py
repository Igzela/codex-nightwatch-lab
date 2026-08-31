from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class Tone(StrEnum):
    """Semantic display roles shared by ANSI and curses frontends."""

    PRIMARY = "primary"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"
    INFO = "info"
    MUTED = "muted"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class SemanticColor:
    ansi: str
    curses_pair: int


SEMANTIC_COLORS: dict[Tone, SemanticColor] = {
    Tone.PRIMARY: SemanticColor("\x1b[36m", 1),
    Tone.SUCCESS: SemanticColor("\x1b[32m", 2),
    Tone.WARNING: SemanticColor("\x1b[33m", 3),
    Tone.DANGER: SemanticColor("\x1b[31m", 4),
    Tone.INFO: SemanticColor("\x1b[34m", 5),
    Tone.MUTED: SemanticColor("\x1b[2m", 6),
    Tone.NEUTRAL: SemanticColor("", 0),
}
ANSI_RESET = "\x1b[0m"

# Convenient named tokens keep call sites readable while the mapping remains
# the single source of truth for terminal and curses presentation.
PRIMARY = SEMANTIC_COLORS[Tone.PRIMARY]
SUCCESS = SEMANTIC_COLORS[Tone.SUCCESS]
WARNING = SEMANTIC_COLORS[Tone.WARNING]
DANGER = SEMANTIC_COLORS[Tone.DANGER]
INFO = SEMANTIC_COLORS[Tone.INFO]
MUTED = SEMANTIC_COLORS[Tone.MUTED]
NEUTRAL = SEMANTIC_COLORS[Tone.NEUTRAL]


@dataclass(frozen=True)
class BoxDrawing:
    top_left: str = "╭"
    top_right: str = "╮"
    bottom_left: str = "╰"
    bottom_right: str = "╯"
    horizontal: str = "─"
    vertical: str = "│"


ROUNDED_BOX = BoxDrawing()
_PARTIAL_BLOCKS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")


def colorize(text: object, tone: Tone | str = Tone.NEUTRAL, *, enabled: bool = False) -> str:
    """Apply a semantic ANSI token only when a caller explicitly enables it."""
    value = str(text)
    try:
        token = SEMANTIC_COLORS[Tone(tone)]
    except (ValueError, TypeError):
        token = NEUTRAL
    if not enabled or not token.ansi:
        return value
    return f"{token.ansi}{value}{ANSI_RESET}"


def badge_pill(label: object, tone: Tone | str = Tone.NEUTRAL, *, ansi: bool = False) -> str:
    """Render a compact rounded badge without introducing hidden control text."""
    clean = " ".join(str(label).split())
    return colorize(f"‹ {clean} ›", tone, enabled=ansi)


def smooth_gauge(percent: float, width: int = 18, *, empty: str = "░") -> str:
    """Render a clamped fixed-width gauge with eighth-cell UTF-8 precision."""
    width = max(1, int(width))
    try:
        value = float(percent)
    except (TypeError, ValueError):
        value = 0.0
    value = min(100.0, max(0.0, value))
    eighths = min(width * 8, max(0, round(value / 100.0 * width * 8)))
    full, partial = divmod(eighths, 8)
    cells = "█" * full
    if partial and full < width:
        cells += _PARTIAL_BLOCKS[partial]
    return cells + empty * (width - len(cells))


def rounded_box(
    lines: Iterable[object],
    *,
    width: int | None = None,
    title: object | None = None,
    glyphs: BoxDrawing = ROUNDED_BOX,
) -> str:
    """Render bounded text in a rounded box using deterministic character widths."""
    values = [str(item) for item in lines] or [""]
    title_text = " ".join(str(title).split()) if title is not None else ""
    natural = max([len(item) for item in values] + ([len(title_text) + 2] if title_text else [0])) + 2
    outer_width = max(4, natural if width is None else int(width))
    inner_width = outer_width - 2
    top_fill = glyphs.horizontal * inner_width
    if title_text:
        label = f" {title_text} "[:inner_width]
        top_fill = label + glyphs.horizontal * (inner_width - len(label))
    rendered = [f"{glyphs.top_left}{top_fill}{glyphs.top_right}"]
    for value in values:
        clipped = value[:inner_width]
        rendered.append(f"{glyphs.vertical}{clipped:<{inner_width}}{glyphs.vertical}")
    rendered.append(f"{glyphs.bottom_left}{glyphs.horizontal * inner_width}{glyphs.bottom_right}")
    return "\n".join(rendered)


# Small aliases make the primitives discoverable under the product language.
badge = badge_pill
gauge = smooth_gauge
