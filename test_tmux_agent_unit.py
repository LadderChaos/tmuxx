"""Unit tests for tmuxx agent command resolution."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tmux_agent import (
    _build_parser,
    _cmd_mission_start,
    _cmd_mission_status,
    _cmd_mission_supervise,
    _cmd_status,
    _cmd_supervise,
    _evaluate_watch_event,
    _format_supervise_prompt,
    _watch_until_match,
)
from tmux_core import DEFAULT_AGENT_COMMAND, Pane, Session, Window, Worktree, resolve_agent_command
from tmux_mission import create_mission_state, format_mission_handoff, parse_worker_spec, summarize_mission


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

    def test_copilot_command_family_is_detected_for_nested_guard(self) -> None:
        with patch.dict(os.environ, {"COPILOT_AGENT_SESSION": "1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "nested 'gh copilot suggest'"):
                resolve_agent_command("gh copilot suggest")


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

    def test_mission_parser_supports_nested_start_status_and_supervise(self) -> None:
        start = _build_parser().parse_args(
            [
                "mission",
                "start",
                "ship tmuxx 0.4.0",
                "--supervisor-pane",
                "%9",
                "--worker",
                "dev:%1",
                "--worker",
                "qa:session:claude",
                "--json",
            ]
        )
        self.assertEqual(start.command, "mission-start")
        self.assertEqual(start.supervisor_pane, "%9")
        self.assertEqual(start.worker, ["dev:%1", "qa:session:claude"])
        self.assertTrue(start.json)

        status = _build_parser().parse_args(["mission", "status", "m1"])
        self.assertEqual(status.command, "mission-status")
        self.assertEqual(status.mission_id, "m1")

        supervise = _build_parser().parse_args(
            ["mission", "supervise", "m1", "--continuous", "--max-handoffs", "2"]
        )
        self.assertEqual(supervise.command, "mission-supervise")
        self.assertTrue(supervise.continuous)
        self.assertEqual(supervise.max_handoffs, 2)

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


class StatusCommandTests(unittest.TestCase):
    def test_status_uses_shared_pane_classifier(self) -> None:
        sessions = [
            Session(
                "$1",
                "dev",
                False,
                [
                    Window(
                        "@1",
                        0,
                        "agent",
                        True,
                        [
                            Pane(
                                "%1",
                                0,
                                80,
                                24,
                                "2.1.119",
                                True,
                                current_path="/repo/.worktrees/feature",
                            )
                        ],
                    )
                ],
            )
        ]
        worktrees = [
            Worktree("/repo", "main", "abc1234", True),
            Worktree("/repo/.worktrees/feature", "feature", "def5678", False),
        ]
        with (
            patch("tmux_agent.git.list_worktrees", AsyncMock(return_value=worktrees)),
            patch("tmux_agent.backend.get_hierarchy", AsyncMock(return_value=sessions)),
            patch("tmux_agent.backend.capture_pane", AsyncMock(return_value="~/repo feature ❯")),
        ):
            result = asyncio.run(_cmd_status(_build_parser().parse_args(["status"])))

        self.assertEqual(result[0]["panes"][0]["status"], "idle")
        self.assertFalse(result[0]["panes"][0]["needs_prompt"])

    def test_status_does_not_match_worktree_sibling_prefix(self) -> None:
        sessions = [
            Session(
                "$1",
                "dev",
                False,
                [
                    Window(
                        "@1",
                        0,
                        "agent",
                        True,
                        [
                            Pane(
                                "%1",
                                0,
                                80,
                                24,
                                "bash",
                                True,
                                current_path="/repo/.worktrees/feature-old",
                            )
                        ],
                    )
                ],
            )
        ]
        worktrees = [
            Worktree("/repo", "main", "abc1234", True),
            Worktree("/repo/.worktrees/feature", "feature", "def5678", False),
        ]
        with (
            patch("tmux_agent.git.list_worktrees", AsyncMock(return_value=worktrees)),
            patch("tmux_agent.backend.get_hierarchy", AsyncMock(return_value=sessions)),
            patch("tmux_agent.backend.capture_pane", AsyncMock(return_value="~/repo feature-old ❯")),
        ):
            result = asyncio.run(_cmd_status(_build_parser().parse_args(["status"])))

        self.assertEqual(result[0]["panes"], [])


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


class MissionCommandTests(unittest.TestCase):
    def test_parse_worker_spec_supports_pane_session_and_branch_targets(self) -> None:
        self.assertEqual(parse_worker_spec("dev:%1"), {"role": "dev", "kind": "pane", "target": "%1"})
        self.assertEqual(parse_worker_spec("qa:session:claude"), {"role": "qa", "kind": "session", "target": "claude"})
        self.assertEqual(parse_worker_spec("review:branch:feat-auth"), {"role": "review", "kind": "branch", "target": "feat-auth"})

    def test_summarize_mission_counts_worker_states_and_next_action(self) -> None:
        mission = create_mission_state(
            "finish release",
            "%9",
            ["dev:%1", "qa:session:claude"],
            mission_id="m1",
            created_at=100,
        )
        summary = summarize_mission(
            mission,
            [
                {
                    "pane_id": "%1",
                    "session_name": "codex",
                    "window_name": "dev",
                    "status": "running",
                    "needs_prompt": False,
                    "recent_output": "working",
                },
                {
                    "pane_id": "%2",
                    "session_name": "claude",
                    "window_name": "qa",
                    "status": "waiting_for_input",
                    "needs_prompt": True,
                    "recent_output": "new task?",
                },
            ],
        )
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["next_action"], "prompt_supervisor")
        self.assertEqual(summary["counts"]["running"], 1)
        self.assertEqual(summary["counts"]["waiting_for_input"], 1)

    def test_format_mission_handoff_instructs_supervisor_to_represent_user(self) -> None:
        mission = create_mission_state("ship release", "%9", ["dev:%1"], mission_id="m1", created_at=100)
        summary = summarize_mission(
            mission,
            [
                {
                    "pane_id": "%1",
                    "session_name": "codex",
                    "window_name": "dev",
                    "status": "waiting_for_input",
                    "needs_prompt": True,
                    "recent_output": "Need approval",
                }
            ],
        )
        prompt = format_mission_handoff(summary)
        self.assertIn("You represent the user", prompt)
        self.assertIn("Goal: ship release", prompt)
        self.assertIn("Need approval", prompt)

    def test_mission_start_persists_repo_local_state(self) -> None:
        args = _build_parser().parse_args(
            [
                "mission",
                "start",
                "ship release",
                "--mission-id",
                "m1",
                "--supervisor-pane",
                "%9",
                "--worker",
                "dev:%1",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("tmux_agent.git.get_repo_root", AsyncMock(return_value=tmp)):
                result = asyncio.run(_cmd_mission_start(args))
            path = result["path"]
            with open(path, encoding="utf-8") as f:
                saved = json.loads(f.read())
        self.assertEqual(saved["mission_id"], "m1")
        self.assertEqual(saved["supervisor_pane"], "%9")
        self.assertEqual(saved["workers"][0]["role"], "dev")

    def test_mission_status_summarizes_latest_mission(self) -> None:
        mission = create_mission_state("ship release", "%9", ["dev:%1"], mission_id="m1", created_at=100)
        snapshot = [
            {
                "pane_id": "%1",
                "session_name": "codex",
                "window_name": "dev",
                "status": "idle",
                "needs_prompt": False,
                "recent_output": "done",
            }
        ]
        args = _build_parser().parse_args(["mission", "status"])
        with (
            patch("tmux_agent.git.get_repo_root", AsyncMock(return_value="/repo")),
            patch("tmux_agent.load_latest_mission_state", return_value=mission),
            patch("tmux_agent._collect_watch_snapshot", AsyncMock(return_value=snapshot)),
        ):
            result = asyncio.run(_cmd_mission_status(args))
        self.assertEqual(result["summary"]["mission_id"], "m1")
        self.assertEqual(result["summary"]["status"], "idle")

    def test_mission_supervise_sends_handoff_to_supervisor(self) -> None:
        mission = create_mission_state("ship release", "%9", ["dev:%1"], mission_id="m1", created_at=100)
        snapshot = [
            {
                "pane_id": "%1",
                "session_name": "codex",
                "window_name": "dev",
                "status": "waiting_for_input",
                "needs_prompt": True,
                "recent_output": "Need approval",
            }
        ]
        args = _build_parser().parse_args(["mission", "supervise", "m1", "--timeout", "1"])
        with (
            patch("tmux_agent.git.get_repo_root", AsyncMock(return_value="/repo")),
            patch("tmux_agent.load_mission_state", return_value=mission),
            patch("tmux_agent._collect_watch_snapshot", AsyncMock(return_value=snapshot)),
            patch("tmux_agent._send_text", AsyncMock()) as send_text,
        ):
            result = asyncio.run(_cmd_mission_supervise(args))
        send_text.assert_awaited_once()
        self.assertEqual(send_text.await_args.args[0], "%9")
        self.assertIn("tmuxx mission handoff", send_text.await_args.args[1])
        self.assertTrue(send_text.await_args.kwargs["press_enter"])
        self.assertTrue(result["prompt_sent"])


if __name__ == "__main__":
    unittest.main()
