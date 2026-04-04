"""Unit tests for tmux_core shared utilities."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tmux_core import (
    detect_needs_prompt,
    quote,
    slugify,
    xdg_config_path,
)


class SlugifyTests(unittest.TestCase):
    def test_simple(self) -> None:
        self.assertEqual(slugify("Fix login bug"), "fix-login-bug")

    def test_special_chars(self) -> None:
        self.assertEqual(slugify("Add auth! @tests"), "add-auth-tests")

    def test_empty(self) -> None:
        self.assertEqual(slugify(""), "agent-task")

    def test_long_input(self) -> None:
        result = slugify("a" * 100)
        self.assertLessEqual(len(result), 50)

    def test_trailing_hyphens(self) -> None:
        result = slugify("hello---")
        self.assertFalse(result.endswith("-"))

    def test_whitespace(self) -> None:
        self.assertEqual(slugify("  spaced out  "), "spaced-out")

    def test_max_len(self) -> None:
        result = slugify("word " * 20, max_len=20)
        self.assertLessEqual(len(result), 20)
        self.assertFalse(result.endswith("-"))


class DetectNeedsPromptTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertFalse(detect_needs_prompt(""))

    def test_no_prompt(self) -> None:
        self.assertFalse(detect_needs_prompt("$ ls\nfoo.py\nbar.py\n$"))

    def test_yn_prompt(self) -> None:
        self.assertTrue(detect_needs_prompt("Do you want to continue? (y/n)"))

    def test_bracket_yn(self) -> None:
        self.assertTrue(detect_needs_prompt("Proceed? [Y/n]"))

    def test_yes_no(self) -> None:
        self.assertTrue(detect_needs_prompt("Continue? [yes/no]"))

    def test_are_you_sure(self) -> None:
        self.assertTrue(detect_needs_prompt("Are you sure you want to delete?"))

    def test_old_output_not_matched(self) -> None:
        # prompt on line 1 but 10 lines of normal output follow
        lines = ["Are you sure? (y/n)"] + ["normal output"] * 10
        self.assertFalse(detect_needs_prompt("\n".join(lines)))

    def test_recent_prompt_matched(self) -> None:
        lines = ["normal"] * 10 + ["Continue? [yes/no]"]
        self.assertTrue(detect_needs_prompt("\n".join(lines)))

    def test_permission_log_not_matched(self) -> None:
        # "permission denied" in old output should NOT trigger
        lines = ["permission denied for user foo"] + ["all good now"] * 10
        self.assertFalse(detect_needs_prompt("\n".join(lines)))

    def test_press_enter(self) -> None:
        self.assertTrue(detect_needs_prompt("Press Enter to continue"))


class QuoteTests(unittest.TestCase):
    def test_simple(self) -> None:
        self.assertEqual(quote("hello"), "hello")

    def test_spaces(self) -> None:
        result = quote("hello world")
        self.assertIn("hello world", result)

    def test_single_quotes(self) -> None:
        result = quote("it's")
        # shlex.quote handles single quotes
        self.assertIn("it", result)
        self.assertIn("s", result)

    def test_empty(self) -> None:
        result = quote("")
        self.assertEqual(result, "''")


class XdgConfigPathTests(unittest.TestCase):
    def test_default_path(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_CONFIG_HOME", None)
            p = xdg_config_path("config.json")
            self.assertTrue(str(p).endswith("tmuxx/config.json"))
            self.assertIn(".config", str(p))

    def test_xdg_override(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}):
            p = xdg_config_path("config.json")
            self.assertEqual(p, Path("/tmp/xdg/tmuxx/config.json"))

    def test_no_parts(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}):
            p = xdg_config_path()
            self.assertEqual(p, Path("/tmp/xdg/tmuxx"))


if __name__ == "__main__":
    unittest.main()
