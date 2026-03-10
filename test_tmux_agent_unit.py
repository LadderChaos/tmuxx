"""Unit tests for tmuxx agent command resolution."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tmux_agent import _build_parser
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


if __name__ == "__main__":
    unittest.main()
