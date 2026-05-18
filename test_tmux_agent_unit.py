"""Unit tests for tmuxx agent command resolution."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tmux_agent import (
    _build_agent_launch_command,
    _build_parser,
    _cmd_install_integration,
    _cmd_report_state,
    _cmd_status,
    _cmd_supervise,
    _detect_pane_statuses,
    _evaluate_watch_event,
    _format_supervise_prompt,
    _watch_until_match,
)
from tmux_core import DEFAULT_AGENT_COMMAND, Pane, Session, Window, Worktree, resolve_agent_command


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

    def test_report_state_parser_accepts_source_agent_state_and_message(self) -> None:
        args = _build_parser().parse_args(
            [
                "report-state",
                "%3",
                "--source",
                "tmuxx:codex",
                "--agent",
                "codex",
                "--state",
                "blocked",
                "--message",
                "approval needed",
            ]
        )
        self.assertEqual(args.pane_id, "%3")
        self.assertEqual(args.source, "tmuxx:codex")
        self.assertEqual(args.agent, "codex")
        self.assertEqual(args.state, "blocked")
        self.assertEqual(args.message, "approval needed")

    def test_install_integration_parser_accepts_codex(self) -> None:
        args = _build_parser().parse_args(["install-integration", "codex"])
        self.assertEqual(args.target, "codex")

    def test_launch_command_injects_tmuxx_hook_environment(self) -> None:
        command = _build_agent_launch_command("%1", "codex exec", "fix auth")
        self.assertEqual(
            command,
            "TMUXX_ENV=1 TMUXX_PANE_ID=%1 codex exec 'fix auth'",
        )

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
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
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

    def test_reported_state_overrides_status_detection(self) -> None:
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
                        [Pane("%1", 0, 80, 24, "codex", True)],
                    )
                ],
            )
        ]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("tmux_agent.backend.capture_pane", AsyncMock(return_value="$ ready")),
        ):
            args = _build_parser().parse_args(
                [
                    "report-state",
                    "%1",
                    "--source",
                    "tmuxx:codex",
                    "--agent",
                    "codex",
                    "--state",
                    "blocked",
                ]
            )
            asyncio.run(_cmd_report_state(args))
            asyncio.run(_detect_pane_statuses(sessions))

        pane = sessions[0].windows[0].panes[0]
        self.assertEqual(pane.status, "waiting_for_input")
        self.assertTrue(pane.needs_prompt)
        self.assertEqual(pane.agent, "codex")
        self.assertEqual(pane.state_source, "reported")

    def test_old_codex_node_pane_gets_heuristic_agent_label(self) -> None:
        sessions = [
            Session(
                "$1",
                "codex-w3w4",
                False,
                [
                    Window(
                        "@1",
                        0,
                        "node",
                        True,
                        [
                            Pane(
                                "%118",
                                0,
                                89,
                                59,
                                "node",
                                True,
                                pane_title="DT-Macbook-Pro.local",
                            )
                        ],
                    )
                ],
            )
        ]
        with patch("tmux_agent.backend.capture_pane", AsyncMock(return_value="› Implement {feature}\n\ngpt-5.5 xhigh · Waiting")):
            asyncio.run(_detect_pane_statuses(sessions))

        pane = sessions[0].windows[0].panes[0]
        self.assertEqual(pane.agent, "codex")
        self.assertEqual(pane.status, "idle")
        self.assertFalse(pane.needs_prompt)
        self.assertEqual(pane.state_source, "heuristic")

    def test_old_codex_node_ready_status_line_is_idle(self) -> None:
        sessions = [
            Session(
                "$1",
                "codex-w3w4",
                False,
                [
                    Window(
                        "@1",
                        0,
                        "node",
                        True,
                        [
                            Pane(
                                "%118",
                                0,
                                89,
                                59,
                                "node",
                                True,
                                pane_title="DT-Macbook-Pro.local",
                            )
                        ],
                    )
                ],
            )
        ]
        output = (
            "› Implement {feature}\n\n"
            "  gpt-5.5 xhigh · ~/GitHub/sooth-solana · feature/sooth_book-monaco-fork · "
            "Ready · Context 9% left · 5h 10m"
        )
        with patch("tmux_agent.backend.capture_pane", AsyncMock(return_value=output)):
            asyncio.run(_detect_pane_statuses(sessions))

        pane = sessions[0].windows[0].panes[0]
        self.assertEqual(pane.agent, "codex")
        self.assertEqual(pane.status, "idle")
        self.assertFalse(pane.needs_prompt)
        self.assertEqual(pane.state_source, "heuristic")

    def test_install_codex_integration_writes_hook_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            codex_dir = os.path.join(tmp, ".codex")
            os.makedirs(codex_dir)
            with open(os.path.join(codex_dir, "config.toml"), "w", encoding="utf-8") as handle:
                handle.write('model = "gpt-5.4"\n')

            result = asyncio.run(
                _cmd_install_integration(_build_parser().parse_args(["install-integration", "codex"]))
            )

            hook_path = os.path.join(codex_dir, "tmuxx-agent-state.sh")
            hooks_path = os.path.join(codex_dir, "hooks.json")
            config_path = os.path.join(codex_dir, "config.toml")
            self.assertEqual(result["target"], "codex")
            self.assertTrue(os.path.exists(hook_path))
            with open(hook_path, encoding="utf-8") as handle:
                hook_script = handle.read()
            self.assertIn("tmuxx agent report-state", hook_script)
            self.assertIn('pane_id="${TMUXX_PANE_ID:-${TMUX_PANE:-}}"', hook_script)
            self.assertIn('report-state "$pane_id"', hook_script)
            with open(hooks_path, encoding="utf-8") as handle:
                hooks = handle.read()
            self.assertIn("UserPromptSubmit", hooks)
            self.assertIn("PreToolUse", hooks)
            with open(config_path, encoding="utf-8") as handle:
                config = handle.read()
            self.assertIn('model = "gpt-5.4"', config)
            self.assertIn("hooks = true", config)
            self.assertNotIn("codex_hooks", config)

    def test_install_codex_integration_migrates_legacy_codex_hooks_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            codex_dir = os.path.join(tmp, ".codex")
            os.makedirs(codex_dir)
            config_path = os.path.join(codex_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write('model = "gpt-5.4"\n\n[features]\ncodex_hooks = true\nhooks = true\n')

            asyncio.run(
                _cmd_install_integration(_build_parser().parse_args(["install-integration", "codex"]))
            )

            with open(config_path, encoding="utf-8") as handle:
                config = handle.read()
            self.assertNotIn("codex_hooks", config)
            self.assertEqual(config.count("hooks = true"), 1)

    def test_install_claude_integration_writes_tmux_pane_aware_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            claude_dir = os.path.join(tmp, ".claude")
            os.makedirs(claude_dir)
            with open(os.path.join(claude_dir, "settings.json"), "w", encoding="utf-8") as handle:
                handle.write('{"hooks": {}}\n')

            result = asyncio.run(
                _cmd_install_integration(_build_parser().parse_args(["install-integration", "claude"]))
            )

            hook_path = os.path.join(claude_dir, "hooks", "tmuxx-agent-state.sh")
            settings_path = os.path.join(claude_dir, "settings.json")
            self.assertEqual(result["target"], "claude")
            self.assertTrue(os.path.exists(hook_path))
            with open(hook_path, encoding="utf-8") as handle:
                hook_script = handle.read()
            self.assertIn('pane_id="${TMUXX_PANE_ID:-${TMUX_PANE:-}}"', hook_script)
            self.assertIn('release-agent "$pane_id"', hook_script)
            with open(settings_path, encoding="utf-8") as handle:
                settings = handle.read()
            self.assertIn("PermissionRequest", settings)
            self.assertIn("tmuxx-agent-state.sh", settings)


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
