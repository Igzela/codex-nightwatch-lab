from __future__ import annotations

import textwrap
from collections.abc import Sequence

from .theme import rounded_box


def _clip(value: object, width: int) -> str:
    text = str(value)
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _wrap_lines(lines: Sequence[object], width: int) -> list[str]:
    result: list[str] = []
    for value in lines or [""]:
        source = str(value)
        result.extend(
            textwrap.wrap(
                source,
                max(1, width),
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    return result


def render_header(
    version: str,
    run_count: int,
    *,
    integrity_errors: int = 0,
    width: int = 100,
) -> str:
    """Pure compact header for the dashboard."""
    width = max(40, int(width))
    error_note = f" · {integrity_errors} trusted run failed integrity validation" if integrity_errors else ""
    return "\n".join(
        [
            _clip(f"Nightwatch {version} · MULTI-THREAD CONTROL", width),
            _clip(
                f"Runs {run_count}{error_note} · ↑/↓ select · a adopt · r run · s status · d dashboard · ? help · / commands",
                width,
            ),
        ]
    )


def render_panel(title: str, lines: Sequence[object], width: int) -> str:
    """Render one bounded panel without reading product state."""
    width = max(24, int(width))
    inner_width = width - 2
    rendered_lines = _wrap_lines(lines, inner_width) or [""]
    return rounded_box(rendered_lines, width=width, title=title)


def render_dual_panel(
    left_lines: Sequence[object],
    right_lines: Sequence[object],
    *,
    width: int = 100,
    left_title: str = "RUNS",
    right_title: str = "SELECTED RUN",
    breakpoint: int = 96,
) -> str:
    """Render side-by-side panels when space permits, otherwise stack them."""
    width = max(24, int(width))
    if width < breakpoint:
        return "\n".join(
            [
                render_panel(left_title, left_lines, width),
                render_panel(right_title, right_lines, width),
            ]
        )

    gap = 1
    left_width = min(54, max(46, round(width * 0.44)))
    right_width = max(24, width - left_width - gap)
    left = render_panel(left_title, left_lines, left_width).splitlines()
    right = render_panel(right_title, right_lines, right_width).splitlines()
    rows = max(len(left), len(right))
    left.extend([" " * left_width] * (rows - len(left)))
    right.extend([" " * right_width] * (rows - len(right)))
    return "\n".join(f"{left[index]:<{left_width}} {right[index]}" for index in range(rows))


def render_modal(
    title: str,
    body: str = "",
    items: Sequence[tuple[str, str, str]] = (),
    *,
    selected: int = 0,
    width: int = 80,
    height: int = 16,
) -> str:
    """Pure rounded modal renderer with a bounded selectable item window."""
    width = max(32, int(width))
    height = max(5, int(height))
    inner_width = width - 2
    lines = _wrap_lines(body.splitlines(), inner_width) if body else []
    room = max(1, height - len(lines) - 2)
    if items:
        selected = min(max(0, selected), len(items) - 1)
        start = max(0, selected - room + 1)
        for index, (key, item_title, detail) in enumerate(items[start : start + room], start=start):
            marker = "▶" if index == selected else " "
            suffix = f"  {detail}" if detail else ""
            lines.append(_clip(f"{marker} {key:<12} {item_title}{suffix}", inner_width))
    return rounded_box(lines or [""], width=width, title=title)


def render_dashboard_view(
    version: str,
    run_count: int,
    left_lines: Sequence[object],
    right_lines: Sequence[object],
    *,
    integrity_errors: int = 0,
    width: int = 100,
) -> str:
    """Compose the pure header and responsive dashboard panels."""
    header = render_header(version, run_count, integrity_errors=integrity_errors, width=width)
    body = (
        render_panel("RUNS", left_lines, width=width)
        if run_count == 0
        else render_dual_panel(left_lines, right_lines, width=width)
    )
    return f"{header}\n{body}"


render_dashboard = render_dashboard_view
