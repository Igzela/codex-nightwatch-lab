from __future__ import annotations

import sys
import unittest
from pathlib import Path


PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))

from nightwatch.theme import (  # noqa: E402
    ANSI_RESET,
    ROUNDED_BOX,
    SEMANTIC_COLORS,
    Tone,
    badge_pill,
    colorize,
    rounded_box,
    smooth_gauge,
)


class ThemeTests(unittest.TestCase):
    def test_semantic_tokens_cover_ansi_and_curses_without_forcing_color(self):
        self.assertTrue(SEMANTIC_COLORS[Tone.SUCCESS].ansi.startswith("\x1b["))
        self.assertGreater(SEMANTIC_COLORS[Tone.SUCCESS].curses_pair, 0)
        self.assertEqual(colorize("DONE", Tone.SUCCESS), "DONE")
        colored = colorize("DONE", Tone.SUCCESS, enabled=True)
        self.assertTrue(colored.startswith("\x1b["))
        self.assertTrue(colored.endswith(ANSI_RESET))

    def test_badge_pill_collapses_untrusted_whitespace(self):
        self.assertEqual(badge_pill("  WAIT\n QUOTA  ", Tone.WARNING), "‹ WAIT QUOTA ›")

    def test_smooth_gauge_is_clamped_precise_and_fixed_width(self):
        self.assertEqual(smooth_gauge(-10, 4), "░░░░")
        self.assertEqual(smooth_gauge(1000, 4), "████")
        self.assertEqual(smooth_gauge(50, 4), "██░░")
        partial = smooth_gauge(12.5, 4)
        self.assertEqual(len(partial), 4)
        self.assertEqual(partial[0], "▌")

    def test_rounded_box_has_stable_geometry(self):
        rendered = rounded_box(["alpha", "beta"], width=12, title="RUNS")
        lines = rendered.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith(ROUNDED_BOX.top_left))
        self.assertTrue(lines[-1].endswith(ROUNDED_BOX.bottom_right))
        self.assertTrue(all(len(line) == 12 for line in lines))


if __name__ == "__main__":
    unittest.main()
