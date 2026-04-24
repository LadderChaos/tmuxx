"""Unit tests for tmuxx agent command resolution."""

from __future__ import annotations

import asyncio
import os
import re
import unittest
from unittest.mock import AsyncMock, patch

from tmux_agent import _build_parser, _cmd_supervise, _evaluate_watch_event, _format_supervise_prompt, _watch_until_match
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
                "--assume-busy",
            ]
        )
        self.assertEqual(args.event, "text")
        self.assertEqual(args.session, "claude")
        self.assertEqual(args.pane, "%3")
        self.assertEqual(args.pattern, "Pushed")
        self.assertTrue(args.ignore_case)
        self.assertTrue(args.notify)
        self.assertEqual(args.exec_command, "python3 watcher.py")
        self.assertTrue(args.assume_busy)

    def test_supervise_parser_supports_worker_filters_and_goal(self) -> None:
        args = _build_parser().parse_args(
            [
                "supervise",
                "--supervisor-pane",
                "%9",
                "--worker-session",
                "claude",
                "--worker-branch",
                "feat-auth-tests",
                "--goal",
                "finish the task",
                "--continuous",
                "--max-handoffs",
                "2",
            ]
        )
        self.assertEqual(args.event, "attention")
        self.assertEqual(args.supervisor_pane, "%9")
        self.assertEqual(args.worker_session, "claude")
        self.assertEqual(args.worker_branch, "feat-auth-tests")
        self.assertEqual(args.goal, "finish the task")
        self.assertTrue(args.continuous)
        self.assertEqual(args.max_handoffs, 2)
        self.assertFalse(args.assume_busy)

    def test_watch_attention_can_assume_busy(self) -> None:
        args = _build_parser().parse_args(
            [
                "watch",
                "--event",
                "attention",
                "--pane",
                "%3",
                "--assume-busy",
                "--timeout",
                "1",
            ]
        )
        snapshot = [{"pane_id": "%3", "status": "waiting_for_input", "needs_prompt": True, "recent_output": "new task? /clear"}]
        with patch("tmux_agent._collect_watch_snapshot", AsyncMock(return_value=snapshot)):
            result = asyncio.run(_watch_until_match(args))
        self.assertTrue(result["matched"])
        self.assertEqual(result["event"], "attention")
        self.assertEqual(result["matches"][0]["pane_id"], "%3")


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

    def test_attention_event_matches_busy_then_waiting_for_input(self) -> None:
        first_matched, _, seen_busy = _evaluate_watch_event(
            "attention",
            [{"pane_id": "%1", "status": "running"}],
            None,
            False,
        )
        second_matched, matches, second_seen_busy = _evaluate_watch_event(
            "attention",
            [{"pane_id": "%1", "status": "waiting_for_input", "needs_prompt": True}],
            None,
            seen_busy,
        )
        self.assertFalse(first_matched)
        self.assertTrue(second_matched)
        self.assertTrue(second_seen_busy)
        self.assertEqual(matches[0]["pane_id"], "%1")


class SuperviseCommandTests(unittest.TestCase):
    def test_format_supervise_prompt_includes_goal_and_pane(self) -> None:
        prompt = _format_supervise_prompt(
            {
                "event": "attention",
                "filters": {"session": "claude", "window": "", "pane": "%1", "branch": "main"},
                "matches": [
                    {
                        "pane_id": "%1",
                        "window_name": "worker",
                        "session_name": "claude",
                        "branch": "main",
                        "status": "waiting_for_input",
                        "needs_prompt": True,
                        "recent_output": "Need approval",
                    }
                ],
            },
            "finish the task",
        )
        self.assertIn("Original goal: finish the task", prompt)
        self.assertIn("- pane: %1", prompt)
        self.assertIn("Need approval", prompt)

    def test_supervise_command_waits_and_sends_prompt(self) -> None:
        args = _build_parser().parse_args(
            [
                "supervise",
                "--supervisor-pane",
                "%9",
                "--worker-session",
                "claude",
                "--goal",
                "finish the task",
            ]
        )
        trigger = {
            "event": "attention",
            "match_count": 1,
            "matches": [
                {
                    "pane_id": "%1",
                    "window_name": "worker",
                    "session_name": "claude",
                    "branch": "main",
                    "status": "waiting_for_input",
                    "needs_prompt": True,
                    "recent_output": "Need approval",
                }
            ],
        }
        with (
            patch("tmux_agent._wait_for_supervise_trigger", AsyncMock(return_value=trigger)),
            patch("tmux_agent._send_text", AsyncMock()) as send_text,
        ):
            result = asyncio.run(_cmd_supervise(args))
        send_text.assert_awaited_once()
        self.assertEqual(send_text.await_args.args[0], "%9")
        self.assertIn("Original goal: finish the task", send_text.await_args.args[1])
        self.assertTrue(send_text.await_args.kwargs["press_enter"])
        self.assertTrue(result["prompt_sent"])
        self.assertEqual(result["worker_panes"], ["%1"])

    def test_supervise_command_continuous_rearms_after_worker_resumes(self) -> None:
        args = _build_parser().parse_args(
            [
                "supervise",
                "--supervisor-pane",
                "%9",
                "--worker-session",
                "claude",
                "--goal",
                "finish the task",
                "--continuous",
                "--max-handoffs",
                "2",
            ]
        )
        first_trigger = {
            "event": "attention",
            "match_count": 1,
            "matches": [
                {
                    "pane_id": "%1",
                    "window_name": "worker",
                    "session_name": "claude",
                    "branch": "main",
                    "status": "waiting_for_input",
                    "needs_prompt": True,
                    "recent_output": "Need approval",
                }
            ],
        }
        second_trigger = {
            "event": "attention",
            "match_count": 1,
            "matches": [
                {
                    "pane_id": "%1",
                    "window_name": "worker",
                    "session_name": "claude",
                    "branch": "main",
                    "status": "idle",
                    "needs_prompt": False,
                    "recent_output": "Task complete",
                }
            ],
        }
        with (
            patch("tmux_agent._wait_for_supervise_trigger", AsyncMock(side_effect=[first_trigger, second_trigger])) as wait_trigger,
            patch("tmux_agent._wait_for_supervise_rearm", AsyncMock(return_value=None)) as wait_rearm,
            patch("tmux_agent._send_text", AsyncMock()) as send_text,
        ):
            result = asyncio.run(_cmd_supervise(args))
        self.assertEqual(wait_trigger.await_count, 2)
        wait_rearm.assert_awaited_once()
        self.assertEqual(send_text.await_count, 2)
        self.assertTrue(result["continuous"])
        self.assertEqual(result["handoff_count"], 2)
        self.assertEqual(len(result["handoffs"]), 2)
        self.assertEqual(result["trigger"]["matches"][0]["recent_output"], "Task complete")


if __name__ == "__main__":
    unittest.main()
