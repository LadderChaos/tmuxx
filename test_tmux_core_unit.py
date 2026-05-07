"""Unit tests for tmux_core shared utilities."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from textual.widgets import RichLog

from tmux_core import (
    GitBackend,
    Pane,
    Session,
    Window,
    classify_pane_status,
    detect_needs_prompt,
    detect_shell_prompt,
    path_within,
    quote,
    slugify,
    xdg_config_path,
)
from tmuxx import (
    ClickCell,
    InputModal,
    TmuxTUI,
    _install_tmux_integration,
    _tmux_pane_border_styles,
    _tmux_style_to_rich_color,
    compose_window_grid,
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

    def test_claude_new_task_prompt(self) -> None:
        lines = ["summary line", "new task? /clear to save 645.3k tokens"]
        self.assertTrue(detect_needs_prompt("\n".join(lines)))


class PaneStatusClassificationTests(unittest.TestCase):
    def test_detect_shell_prompt(self) -> None:
        self.assertTrue(detect_shell_prompt("~/GitHub/sooth-alpha main ❯"))

    def test_versioned_agent_command_with_shell_prompt_is_idle(self) -> None:
        status, needs_prompt = classify_pane_status("2.1.119", "~/GitHub/sooth-alpha main ❯")
        self.assertEqual(status, "idle")
        self.assertFalse(needs_prompt)

    def test_versioned_agent_command_with_new_task_prompt_needs_attention(self) -> None:
        status, needs_prompt = classify_pane_status("2.1.119", "summary\nnew task? /clear to save 645.3k tokens")
        self.assertEqual(status, "waiting_for_input")
        self.assertTrue(needs_prompt)


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


class PathWithinTests(unittest.TestCase):
    def test_rejects_sibling_prefix_collision(self) -> None:
        self.assertFalse(path_within("/repo/.worktrees/foo-old", "/repo/.worktrees/foo"))

    def test_accepts_exact_path_and_descendant(self) -> None:
        self.assertTrue(path_within("/repo/.worktrees/foo", "/repo/.worktrees/foo"))
        self.assertTrue(path_within("/repo/.worktrees/foo/src/app.py", "/repo/.worktrees/foo"))


class GitBackendMergeTests(unittest.TestCase):
    def test_merge_worktree_switches_to_main_before_merge(self) -> None:
        git = GitBackend()
        git._repo_root = "/repo"
        calls: list[tuple[tuple[str, ...], str | None]] = []

        async def fake_run(*args: str, cwd: str | None = None) -> str:
            calls.append((args, cwd))
            if args == ("git", "branch", "--show-current"):
                return "feature"
            if args[:3] == ("git", "commit", "-m"):
                raise RuntimeError("nothing to commit")
            return ""

        with patch.object(GitBackend, "_run", AsyncMock(side_effect=fake_run)):
            asyncio.run(git.merge_worktree("task"))

        switch_idx = calls.index((("git", "switch", "main"), "/repo"))
        merge_idx = calls.index((("git", "merge", "--no-ff", "-m", "Merge task", "task"), "/repo"))
        self.assertLess(switch_idx, merge_idx)


class InputModalTests(unittest.TestCase):
    def test_empty_submission_defaults_to_cancel(self) -> None:
        modal = InputModal("Name:")
        with patch.object(modal, "dismiss") as dismiss:
            modal.on_input_submitted(SimpleNamespace(value="   "))
        dismiss.assert_called_once_with(None)

    def test_empty_submission_can_be_allowed(self) -> None:
        modal = InputModal("Name:", allow_empty=True)
        with patch.object(modal, "dismiss") as dismiss:
            modal.on_input_submitted(SimpleNamespace(value="   "))
        dismiss.assert_called_once_with("")


class ClickFirstTUITests(unittest.IsolatedAsyncioTestCase):
    def _sessions(self) -> list[Session]:
        return [
            Session(
                "$1",
                "convoke",
                True,
                [
                    Window(
                        "@1",
                        0,
                        "codex",
                        True,
                        [
                            Pane("%1", 0, 80, 24, "codex", True),
                            Pane("%2", 1, 80, 24, "zsh", False),
                        ],
                    ),
                    Window(
                        "@2",
                        1,
                        "server",
                        False,
                        [
                            Pane("%3", 0, 100, 28, "vite", True),
                        ],
                    ),
                ],
            )
        ]

    async def test_click_first_layout_replaces_header_tree_and_footer_shortcuts(self) -> None:
        app = TmuxTUI()
        sessions = self._sessions()

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("tmuxx._install_tmux_integration"),
            patch("tmuxx.TmuxBackend.get_hierarchy", AsyncMock(return_value=sessions)),
            patch("tmuxx.TmuxBackend.capture_pane", AsyncMock(return_value="$ ready")),
            patch("tmuxx.TmuxBackend.capture_window_panes", AsyncMock(return_value={
                "%1": "codex output",
                "%2": "shell output",
                "%3": "server output",
            })),
            patch("tmuxx.GitBackend.detect_worktree_branch", AsyncMock(return_value="")),
            patch("tmuxx._tmux_pane_border_styles", return_value=("dim", "green")),
        ):
            async with app.run_test(size=(132, 38)) as pilot:
                await pilot.pause()

                self.assertEqual(len(list(app.query("#app-header"))), 0)
                self.assertEqual(len(list(app.query("#tree-panel"))), 0)
                self.assertEqual(len(list(app.query("#main-container"))), 0)
                self.assertEqual(len(list(app.query("#session-tabs"))), 0)
                self.assertEqual(len(list(app.query("#window-tabs"))), 0)
                self.assertEqual(len(list(app.query("#pane-tabs"))), 0)
                self.assertEqual(len(list(app.query("#window-zone"))), 0)
                self.assertEqual(len(list(app.query("#pane-zone"))), 0)
                self.assertEqual(len(list(app.query("#footer-command-bar"))), 0)
                self.assertIsNotNone(app.query_one("#top-bar"))
                self.assertIsNotNone(app.query_one("#focus-panel"))
                self.assertIsNotNone(app.query_one("#utility-actions"))
                self.assertIsInstance(app.query_one("#session-rail"), object)
                self.assertIsInstance(app.query_one("#window-rail"), object)
                self.assertIsInstance(app.query_one("#pane-rail"), object)
                self.assertGreaterEqual(len(list(app.query("#session-rail .nav-cell"))), 1)
                self.assertGreaterEqual(len(list(app.query("#window-rail .nav-cell"))), 2)
                self.assertGreaterEqual(len(list(app.query("#pane-rail .nav-cell"))), 1)
                self.assertIsInstance(app.query_one("#window-2"), ClickCell)
                self.assertIsInstance(app.query_one("#pane-1"), ClickCell)
                self.assertEqual(len(list(app.query("#pane-table"))), 0)
                self.assertIsInstance(app.query_one("#pane-preview"), RichLog)
                self.assertIsNotNone(app.query_one("#command-dock"))
                self.assertEqual(len(list(app.query(".command-row-label"))), 0)
                self.assertLessEqual(app.query_one("#pane-preview").region.y, 11)
                self.assertLessEqual(app.query_one("#command-zone").region.height, 3)

                utility_text = " ".join(cell.label_text for cell in app.query("#utility-actions .command-cell"))
                self.assertIn("Home", utility_text)
                self.assertIn("Refresh", utility_text)
                self.assertIn("Search", utility_text)
                self.assertNotIn("New Window", utility_text)
                self.assertNotIn("Split H", utility_text)

                dock_text = " ".join(cell.label_text for cell in app.query("#command-dock .command-cell"))
                self.assertIn("New Window", dock_text)
                self.assertIn("Attach Window", dock_text)
                self.assertIn("Split H", dock_text)
                self.assertIn("Send Keys", dock_text)
                for cell in app.query("#command-dock .command-cell"):
                    self.assertLessEqual(cell.region.right, app.size.width)

    async def test_clicking_window_and_pane_changes_preview_focus(self) -> None:
        app = TmuxTUI()
        sessions = self._sessions()

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("tmuxx._install_tmux_integration"),
            patch("tmuxx.TmuxBackend.get_hierarchy", AsyncMock(return_value=sessions)),
            patch("tmuxx.TmuxBackend.capture_pane", AsyncMock(return_value="server pane output")),
            patch("tmuxx.TmuxBackend.capture_window_panes", AsyncMock(return_value={
                "%1": "codex output",
                "%2": "shell output",
                "%3": "server output",
            })),
            patch("tmuxx.GitBackend.detect_worktree_branch", AsyncMock(return_value="")),
            patch("tmuxx._tmux_pane_border_styles", return_value=("dim", "green")),
        ):
            async with app.run_test(size=(132, 38)) as pilot:
                await pilot.pause()

                await pilot.click("#window-2")
                await pilot.pause()
                self.assertEqual(app._selected_window_id, "@2")
                self.assertEqual(app._preview_mode, "window")
                self.assertIn("Window: server", app._preview._plain_text)

                await pilot.click("#pane-3")
                await pilot.pause()
                self.assertEqual(app._selected_pane_id, "%3")
                self.assertEqual(app._preview_mode, "pane")
                self.assertIn("Preview: %3", app._preview._plain_text)


class TmuxIntegrationTests(unittest.TestCase):
    def test_does_not_override_tmux_pane_border_theme(self) -> None:
        def fake_run(args: list[str], **kwargs):
            if args[:3] == ["tmux", "show-option", "-gv"]:
                return SimpleNamespace(stdout="default\n")
            return SimpleNamespace(stdout="")

        with patch("tmuxx.subprocess.run", side_effect=fake_run) as run:
            _install_tmux_integration()

        calls = [call.args[0] for call in run.call_args_list]
        changed_options = [
            call
            for call in calls
            if call[:3] == ["tmux", "set-option", "-g"]
        ]
        changed_option_names = {call[3] for call in changed_options if len(call) > 3}
        self.assertNotIn("pane-border-style", changed_option_names)
        self.assertNotIn("pane-active-border-style", changed_option_names)

    def test_reads_tmux_pane_border_theme_for_preview(self) -> None:
        values = {
            "pane-border-style": "fg=colour238,bg=default",
            "pane-active-border-style": "fg=colour39,bg=default",
        }

        def fake_run(args: list[str], **kwargs):
            if args[:3] == ["tmux", "show-option", "-gv"]:
                return SimpleNamespace(returncode=0, stdout=values[args[3]] + "\n")
            return SimpleNamespace(returncode=0, stdout="")

        with patch("tmuxx.subprocess.run", side_effect=fake_run):
            inactive, active = _tmux_pane_border_styles()

        self.assertEqual(inactive, "color(238)")
        self.assertEqual(active, "color(39)")

    def test_tmux_style_parser_handles_common_foreground_forms(self) -> None:
        self.assertEqual(_tmux_style_to_rich_color("fg=colour39,bg=default", "dim"), "color(39)")
        self.assertEqual(_tmux_style_to_rich_color("fg=#5fd7ff,bg=default", "dim"), "#5fd7ff")
        self.assertEqual(_tmux_style_to_rich_color("fg=default,bg=default", "dim"), "dim")


class ComposeWindowGridBorderStyleTests(unittest.TestCase):
    """Border cells adjacent to an active pane use the accent_color, others 'dim'."""

    ACCENT = "#5fd7ff"

    def _make_pane(self, pid: str, left: int, top: int, w: int, h: int, active: bool) -> Pane:
        return Pane(
            pane_id=pid, pane_index=0, width=w, height=h,
            current_command="bash", active=active, left=left, top=top,
        )

    def _border_styles(self, result: "Text") -> set[str]:
        styles: set[str] = set()
        plain = result.plain
        for start, end, style in result._spans:
            if "│" in plain[start:end] or "─" in plain[start:end]:
                styles.add(str(style))
        return styles

    def test_active_border_uses_explicit_tmux_style_when_provided(self) -> None:
        p1 = self._make_pane("%1", left=0, top=0, w=3, h=2, active=True)
        p2 = self._make_pane("%2", left=4, top=0, w=3, h=2, active=False)
        captured = {"%1": "aaa\naaa", "%2": "bbb\nbbb"}

        result = compose_window_grid(
            [p1, p2],
            captured,
            accent_color=self.ACCENT,
            border_active_style="color(39)",
            border_inactive_style="color(238)",
        )
        border_styles = self._border_styles(result)

        self.assertIn("color(39)", border_styles)
        self.assertNotIn(self.ACCENT, border_styles)

    def test_active_border_falls_back_to_accent_color(self) -> None:
        p1 = self._make_pane("%1", left=0, top=0, w=3, h=2, active=True)
        p2 = self._make_pane("%2", left=4, top=0, w=3, h=2, active=False)
        captured = {"%1": "aaa\naaa", "%2": "bbb\nbbb"}

        result = compose_window_grid([p1, p2], captured, accent_color=self.ACCENT)
        border_styles = self._border_styles(result)

        self.assertIn(self.ACCENT, border_styles)

    def test_inactive_only_borders_get_dim_style(self) -> None:
        p1 = self._make_pane("%1", left=0, top=0, w=3, h=2, active=False)
        p2 = self._make_pane("%2", left=4, top=0, w=3, h=2, active=False)
        captured = {"%1": "aaa\naaa", "%2": "bbb\nbbb"}

        result = compose_window_grid([p1, p2], captured, accent_color=self.ACCENT)
        border_styles = self._border_styles(result)

        self.assertTrue(
            all("dim" in s for s in border_styles),
            f"All borders should be dim when no pane is active, got: {border_styles}",
        )
        self.assertNotIn(self.ACCENT, border_styles)

    def test_default_accent_is_green(self) -> None:
        p1 = self._make_pane("%1", left=0, top=0, w=3, h=2, active=True)
        p2 = self._make_pane("%2", left=4, top=0, w=3, h=2, active=False)
        captured = {"%1": "aaa\naaa", "%2": "bbb\nbbb"}

        result = compose_window_grid([p1, p2], captured)
        border_styles = self._border_styles(result)

        self.assertIn("green", border_styles,
                       "Default accent_color should be 'green'")

    def test_touching_panes_still_render_separator(self) -> None:
        p1 = self._make_pane("%1", left=0, top=0, w=3, h=2, active=True)
        p2 = self._make_pane("%2", left=3, top=0, w=3, h=2, active=False)
        captured = {"%1": "aaa\naaa", "%2": "bbb\nbbb"}

        result = compose_window_grid([p1, p2], captured)

        self.assertIn("│", result.plain)

    def test_touching_pane_t_junction_is_connected(self) -> None:
        p1 = self._make_pane("%1", left=0, top=0, w=4, h=4, active=True)
        p2 = self._make_pane("%2", left=4, top=0, w=4, h=2, active=False)
        p3 = self._make_pane("%3", left=4, top=2, w=4, h=2, active=False)
        captured = {"%1": "aaaa\naaaa\naaaa\naaaa", "%2": "bbbb\nbbbb", "%3": "cccc\ncccc"}

        result = compose_window_grid([p1, p2, p3], captured)

        self.assertIn("├", result.plain)


if __name__ == "__main__":
    unittest.main()
