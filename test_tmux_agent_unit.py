"""Unit tests for tmuxx agent command resolution."""

from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from tmux_agent import _build_parser, _evaluate_watch_event
from tmux_core import DEFAULT_AGENT_COMMAND, resolve_agent_command


class ResolveAgentCommandTests(unittest.TestCase):
    def test_explicit_command_wins(self) -> None:
        with patch.dict(os.environ, {"TMUXX_AGENT_COMMAND": "gemini -p", "CODEX_THREAD_ID": "1"}, clear=True):
            self.assertEqual(resolve_agent_command("aider --message"), "aider --message")

    def test_env_override_wins_when_flag_is_missing(self) -> None:
        with patch.dict(os.environ, {"TMUXX_AGENT_COMMAND": "gemini -p"}, clear=True):
            self.assertEqual(resolve_agent_command(None), "gemini -p")

    def test_default_command_is_used_outside_agent_sessions(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_agent_command(None), DEFAULT_AGENT_COMMAND)

    def test_default_command_is_blocked_inside_agent_sessions(self) -> None:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-123"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TMUXX_AGENT_COMMAND"):
                resolve_agent_command(None)

    def test_explicit_codex_command_is_blocked_inside_codex_sessions(self) -> None:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-123"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "nested 'codex exec'"):
                resolve_agent_command("codex exec")

    def test_explicit_gemini_command_is_blocked_inside_gemini_sessions(self) -> None:
        with patch.dict(os.environ, {"GEMINI_SANDBOX": "1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "nested 'gemini -p'"):
                resolve_agent_command("gemini -p")


class AgentParserTests(unittest.TestCase):
    def test_start_task_parser_uses_optional_agent_command(self) -> None:
        args = _build_parser().parse_args(["start-task", "dev", "ship it"])
        self.assertIsNone(args.agent_command)

    def test_launch_agent_parser_accepts_explicit_agent_command(self) -> None:
        args = _build_parser().parse_args(
            ["launch-agent", "dev", "ship it", "--agent-command", "gemini -p"]
        )
        self.assertEqual(args.agent_command, "gemini -p")

    def test_watch_parser_supports_filters_and_callbacks(self) -> None:
        args = _build_parser().parse_args(
            [
                "watch",
                "--event",
                "text",
                "--session",
                "claude",
                "--pane",
                "%3",
                "--pattern",
                "Pushed",
                "--ignore-case",
                "--notify",
                "--exec",
                "python3 watcher.py",
            ]
        )
        self.assertEqual(args.event, "text")
        self.assertEqual(args.session, "claude")
        self.assertEqual(args.pane, "%3")
        self.assertEqual(args.pattern, "Pushed")
        self.assertTrue(args.ignore_case)
        self.assertTrue(args.notify)
        self.assertEqual(args.exec_command, "python3 watcher.py")


class WatchEventTests(unittest.TestCase):
    def test_text_event_matches_recent_output(self) -> None:
        matched, matches, seen_busy = _evaluate_watch_event(
            "text",
            [{"pane_id": "%1", "recent_output": "Committed and pushed"}],
            re.compile("pushed", re.IGNORECASE),
            False,
        )
        self.assertTrue(matched)
        self.assertEqual(matches[0]["pane_id"], "%1")
        self.assertFalse(seen_busy)

    def test_completed_event_requires_busy_then_idle_transition(self) -> None:
        first_matched, _, seen_busy = _evaluate_watch_event(
            "completed",
            [{"pane_id": "%1", "status": "running"}],
            None,
            False,
        )
        second_matched, matches, second_seen_busy = _evaluate_watch_event(
            "completed",
            [{"pane_id": "%1", "status": "idle"}],
            None,
            seen_busy,
        )
        self.assertFalse(first_matched)
        self.assertTrue(second_matched)
        self.assertTrue(second_seen_busy)
        self.assertEqual(matches[0]["pane_id"], "%1")


if __name__ == "__main__":
    unittest.main()
