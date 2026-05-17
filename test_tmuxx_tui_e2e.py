"""End-to-end Pilot tests covering full user journeys.

These tests drive the cockpit the way a user does — click chips and buttons,
type into modals, wait for timers — and assert what the user actually sees
or what the backend ends up being asked to do. They are deliberately
journey-shaped, not widget-position-shaped.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Label, RichLog, Static

from tmux_core import Pane, Session, Window
from tmuxx import ClickCell, ConfirmModal, HelpModal, InputModal, TmuxTUI


# ─── Fixture builders ──────────────────────────────────────────────────────────


def _two_sessions() -> list[Session]:
    """Convoke (attached, 2 windows, 3 panes) + tmuxx (1 window, 1 pane)."""
    return [
        Session("$1", "convoke", True, [
            Window("@1", 0, "codex", True, [
                Pane("%1", 0, 80, 24, "codex", True),
                Pane("%2", 1, 80, 24, "zsh", False),
            ]),
            Window("@2", 1, "server", False, [
                Pane("%3", 0, 100, 28, "vite", True),
            ]),
        ]),
        Session("$2", "tmuxx", False, [
            Window("@3", 0, "tests", True, [
                Pane("%4", 0, 80, 24, "pytest", True),
            ]),
        ]),
    ]


def _agent_session() -> list[Session]:
    """One session with one pane running claude — marked as actively
    producing output (recent activity) so the spinner fires."""
    import time
    p = Pane("%1", 0, 80, 24, "claude", True)
    p.activity = int(time.time())
    return [Session("$1", "convoke", True, [
        Window("@1", 0, "agent", True, [p]),
    ])]


def _branch_session() -> list[Session]:
    p = Pane("%1", 0, 120, 32, "codex", True)
    p.worktree_branch = "feature/executor-cockpit-foundation"
    shell = Pane("%2", 1, 120, 32, "zsh", False)
    return [Session("$1", "lc-trading-build", False, [
        Window("@1", 1, "executor-cockpit", True, [p, shell]),
    ])]


# ─── Common scaffolding ────────────────────────────────────────────────────────


class _Harness:
    """Spin up TmuxTUI under run_test with mockable backend hooks."""

    def __init__(self, sessions=None, *, classify=None):
        self.sessions = sessions if sessions is not None else _two_sessions()
        self.classify = classify
        self.send_keys = AsyncMock()
        self.split_pane = AsyncMock()
        self.kill_pane = AsyncMock()
        self.kill_window = AsyncMock()
        self.kill_session = AsyncMock()
        self.new_session = AsyncMock()
        self.new_window = AsyncMock()
        self.rename_session = AsyncMock()
        self.rename_window = AsyncMock()
        self.get_hierarchy = AsyncMock(return_value=self.sessions)
        self.capture_pane = AsyncMock(return_value="$ ready")
        self.capture_window_panes = AsyncMock(return_value={
            p.pane_id: f"out:{p.pane_id}"
            for s in self.sessions for w in s.windows for p in w.panes
        })

    def __enter__(self):
        self._stack = ExitStack().__enter__()
        tmp = self._stack.enter_context(tempfile.TemporaryDirectory())
        self._stack.enter_context(patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}))
        self._stack.enter_context(patch("tmuxx._install_tmux_integration"))
        for target, mock in (
            ("tmuxx.TmuxBackend.get_hierarchy", self.get_hierarchy),
            ("tmuxx.TmuxBackend.capture_pane", self.capture_pane),
            ("tmuxx.TmuxBackend.capture_window_panes", self.capture_window_panes),
            ("tmuxx.TmuxBackend.send_keys", self.send_keys),
            ("tmuxx.TmuxBackend.split_pane", self.split_pane),
            ("tmuxx.TmuxBackend.kill_pane", self.kill_pane),
            ("tmuxx.TmuxBackend.kill_window", self.kill_window),
            ("tmuxx.TmuxBackend.kill_session", self.kill_session),
            ("tmuxx.TmuxBackend.new_session", self.new_session),
            ("tmuxx.TmuxBackend.new_window", self.new_window),
            ("tmuxx.TmuxBackend.rename_session", self.rename_session),
            ("tmuxx.TmuxBackend.rename_window", self.rename_window),
            ("tmuxx.GitBackend.detect_worktree_branch", AsyncMock(return_value="")),
            ("tmuxx._tmux_pane_border_styles", lambda: ("dim", "green")),
        ):
            if isinstance(mock, AsyncMock):
                self._stack.enter_context(patch(target, mock))
            elif callable(mock):
                self._stack.enter_context(patch(target, side_effect=mock))
            else:
                self._stack.enter_context(patch(target, mock))
        if self.classify is not None:
            def classify_pane_status(cmd, out, **_kwargs):
                return self.classify(cmd, out)

            self._stack.enter_context(
                patch("tmux_core.classify_pane_status", side_effect=classify_pane_status)
            )
        return self

    def __exit__(self, *exc):
        self._stack.__exit__(*exc)


async def _settle(pilot, ticks=3):
    """Pump the event loop a few times so async render work completes."""
    for _ in range(ticks):
        await pilot.pause()


# ─── Journey 1: Cross-session window click flips visual + state ────────────────


class CrossSessionWindowClickJourney(unittest.IsolatedAsyncioTestCase):
    async def test_clicking_window_in_other_session_updates_marker_breadcrumb_preview(self) -> None:
        with _Harness() as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)
                # Initial: convoke @1 selected.
                self.assertEqual(app._selected_session_id, "$1")
                self.assertIn("active", app.query_one("#window-1", ClickCell).classes)

                # Click window @3 which lives in session "tmuxx" ($2).
                await pilot.click("#window-3")
                await _settle(pilot)

                # State changes
                self.assertEqual(app._selected_session_id, "$2")
                self.assertEqual(app._selected_window_id, "@3")
                self.assertEqual(app._preview_mode, "window")

                # Visual: .active class moved from @1 to @3.
                self.assertNotIn(
                    "active",
                    app.query_one("#window-1", ClickCell).classes,
                    "stale active class on @1",
                )
                self.assertIn(
                    "active",
                    app.query_one("#window-3", ClickCell).classes,
                    "missing active class on @3",
                )

                # Preview body now shows only captured pane output (no header).
                # The window switch should make the new window's pane output
                # visible — out:%4 is the mocked capture for window @3's pane.
                preview_text = app._preview._plain_text
                self.assertIn("out:%4", preview_text)


# ─── Journey 1b: Selected-pane branch context ────────────────────────────────


class BranchContextJourney(unittest.IsolatedAsyncioTestCase):
    async def test_selected_pane_branch_renders_centered_on_cockpit_border(self) -> None:
        branch = "feature/executor-cockpit-foundation"
        with _Harness(sessions=_branch_session()) as h:
            app = TmuxTUI()
            async with app.run_test(size=(160, 40)) as pilot:
                await _settle(pilot)

                window_text = str(app.query_one("#window-1", ClickCell).render())
                self.assertNotIn("feature/", window_text)
                self.assertNotIn("⎇", window_text)

                pane_text = str(app.query_one("#pane-1", ClickCell).render())
                self.assertNotIn(branch, pane_text)

                cockpit_frame = app.query_one("#cockpit-frame", Vertical)
                self.assertIsNone(cockpit_frame._border_subtitle)
                branch_context = app.query_one("#branch-context", Static)
                self.assertEqual(
                    branch_context.region.y,
                    cockpit_frame.region.y + cockpit_frame.region.height - 1,
                )
                branch_line = str(branch_context.render())
                label = f"[@{branch}]"
                self.assertIn("visible", branch_context.classes)
                self.assertEqual(branch_context.styles.background.hex.lower(), "#0e1411")
                self.assertIn(label, branch_line)
                self.assertNotIn(f" {label}", branch_line)
                self.assertNotIn(f"{label} ", branch_line)
                self.assertTrue(branch_line.startswith("─────"))
                self.assertTrue(branch_line.endswith("─────"))
                left_fill = branch_line.index(label)
                right_fill = len(branch_line) - left_fill - len(label)
                self.assertEqual(right_fill, left_fill + 2)

                await pilot.click("#pane-2")
                await _settle(pilot)
                self.assertNotIn("visible", branch_context.classes)
                self.assertEqual(str(branch_context.render()), "")

                app._search_filter = "executor-cockpit-foundation"
                await app._render_click_layers()
                await _settle(pilot)
                cards = list(app.query("#window-rail ClickCell"))
                self.assertEqual(len(cards), 1)
                self.assertEqual(cards[0].id, "window-1")


# ─── Journey 2: Send-keys end-to-end ───────────────────────────────────────────


class SendKeysJourney(unittest.IsolatedAsyncioTestCase):
    async def test_clicking_pane_then_send_keys_typing_and_enter_calls_backend(self) -> None:
        with _Harness() as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)

                # Click a pane to scope.
                await pilot.click("#pane-2")
                await _settle(pilot)
                self.assertEqual(app._selected_pane_id, "%2")

                # Click Send Keys to open the modal.
                send_cell = next(
                    c for c in app.query("#pane-actions .command-cell")
                    if c.label_text == "Send Msg"
                )
                await pilot.click(send_cell)
                await _settle(pilot)

                # Modal is on top of the screen stack.
                self.assertIsInstance(app.screen, InputModal)

                # Type a command and submit.
                modal_input = app.screen.query_one("#modal-input", Input)
                modal_input.value = "echo hi"
                await pilot.press("enter")
                await _settle(pilot)

                # Modal dismissed; backend invoked once with expected args.
                self.assertNotIsInstance(app.screen, InputModal)
                h.send_keys.assert_called_once_with("%2", "echo hi")

    async def test_send_keys_disabled_when_no_pane_selected(self) -> None:
        with _Harness(sessions=[]) as h:  # empty world → no pane
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)
                send_cell = next(
                    c for c in app.query("#pane-actions .command-cell")
                    if c.label_text == "Send Msg"
                )
                self.assertTrue(send_cell.disabled)
                # Clicking a disabled cell must NOT open a modal.
                await pilot.click(send_cell)
                await _settle(pilot)
                self.assertNotIsInstance(app.screen, InputModal)
                h.send_keys.assert_not_called()


# ─── Journey 3: Rename session ─────────────────────────────────────────────────


class RenameSessionJourney(unittest.IsolatedAsyncioTestCase):
    async def test_rename_modal_prefills_current_name_and_calls_backend(self) -> None:
        with _Harness() as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)

                # Selection defaults to convoke / @1; click Rename in session row.
                rename_cell = next(
                    c for c in app.query("#session-actions .command-cell")
                    if c.label_text == "Rename"
                )
                await pilot.click(rename_cell)
                await _settle(pilot)
                self.assertIsInstance(app.screen, InputModal)

                # Pre-filled with current selection's name.
                modal_input = app.screen.query_one("#modal-input", Input)
                # Default selection_kind isn't "session" (it's "window"), so the
                # rename modal targets the window. That's fine — verify the
                # input is pre-populated with whichever we are renaming.
                self.assertTrue(modal_input.value)

                modal_input.value = "renamed"
                await pilot.press("enter")
                await _settle(pilot)

                self.assertNotIsInstance(app.screen, InputModal)
                # Either a session or window rename should have been called once
                # depending on selection_kind. Both are acceptable evidence.
                rename_calls = h.rename_session.call_count + h.rename_window.call_count
                self.assertEqual(rename_calls, 1)

    async def test_rename_session_after_explicit_session_select_calls_rename_session(self) -> None:
        with _Harness() as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)
                # Force selection_kind to session by clicking the session pill.
                await pilot.click("#session-1")
                await _settle(pilot)
                self.assertEqual(app._selection_kind, "session")

                rename_cell = next(
                    c for c in app.query("#session-actions .command-cell")
                    if c.label_text == "Rename"
                )
                await pilot.click(rename_cell)
                await _settle(pilot)

                modal_input = app.screen.query_one("#modal-input", Input)
                self.assertEqual(modal_input.value, "convoke")
                modal_input.value = "convoke-2"
                await pilot.press("enter")
                await _settle(pilot)

                h.rename_session.assert_called_once_with("convoke", "convoke-2")
                h.rename_window.assert_not_called()


# ─── Journey 4: Kill confirmation flow ─────────────────────────────────────────


class KillConfirmJourney(unittest.IsolatedAsyncioTestCase):
    async def test_kill_window_confirms_then_calls_backend(self) -> None:
        with _Harness() as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)

                # Select window @2 explicitly.
                await pilot.click("#window-2")
                await _settle(pilot)
                self.assertEqual(app._selection_kind, "window")

                kill_cell = next(
                    c for c in app.query("#window-actions .command-cell")
                    if c.label_text == "Kill"
                )
                await pilot.click(kill_cell)
                await _settle(pilot)
                self.assertIsInstance(app.screen, ConfirmModal)

                # Press 'y' to confirm.
                await pilot.press("y")
                await _settle(pilot)

                self.assertNotIsInstance(app.screen, ConfirmModal)
                h.kill_window.assert_called_once_with("@2")

    async def test_kill_escape_cancels_and_does_not_call_backend(self) -> None:
        with _Harness() as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)
                await pilot.click("#window-2")
                await _settle(pilot)

                kill_cell = next(
                    c for c in app.query("#window-actions .command-cell")
                    if c.label_text == "Kill"
                )
                await pilot.click(kill_cell)
                await _settle(pilot)
                self.assertIsInstance(app.screen, ConfirmModal)

                await pilot.press("escape")
                await _settle(pilot)

                self.assertNotIsInstance(app.screen, ConfirmModal)
                h.kill_window.assert_not_called()


# ─── Journey 5: Status transitions across refreshes ────────────────────────────


class StatusTransitionJourney(unittest.IsolatedAsyncioTestCase):
    async def test_pane_running_to_waiting_swaps_glyph_and_class(self) -> None:
        # First refresh: pytest pane is running. Second: it transitions to
        # waiting_for_input.
        statuses = iter(["running", "waiting_for_input"])

        def classifier(cmd, out):
            if cmd == "vite":  # %3 in the fixture
                return next(statuses), False
            return "idle", False

        with _Harness(sessions=_two_sessions(), classify=classifier) as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)

                # Switch to window @2 so its pane (%3, pytest) is on the rail.
                await pilot.click("#window-2")
                await _settle(pilot)

                pane3_first = str(app.query_one("#pane-3", ClickCell).render())
                # Plain "running" no longer renders a glyph (too noisy when
                # everything is "running"). Only attention-worthy states do.
                self.assertNotIn("◉", pane3_first)
                self.assertNotIn("waiting", " ".join(
                    app.query_one("#pane-3", ClickCell).classes
                ))

                # Trigger a second refresh — classifier next returns waiting.
                await app._do_refresh()
                await _settle(pilot)

                pane3_second = str(app.query_one("#pane-3", ClickCell).render())
                self.assertIn("◉", pane3_second)
                self.assertIn(
                    "waiting",
                    " ".join(app.query_one("#pane-3", ClickCell).classes),
                )


# ─── Journey 6: Lost pane during refresh recovers gracefully ──────────────────


class LostPaneJourney(unittest.IsolatedAsyncioTestCase):
    async def test_selected_pane_disappearing_does_not_crash(self) -> None:
        first = _two_sessions()
        # Second hierarchy snapshot drops %2 from window @1.
        second = _two_sessions()
        second[0].windows[0].panes = [second[0].windows[0].panes[0]]  # only %1

        h = _Harness(sessions=first)
        h.get_hierarchy = AsyncMock(side_effect=[first, second, second])
        with h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)
                await pilot.click("#pane-2")
                await _settle(pilot)
                self.assertEqual(app._selected_pane_id, "%2")

                # Drive a refresh — %2 is gone in the new snapshot.
                await app._do_refresh()
                await _settle(pilot)

                # Selection falls back without crashing; %2 widget is gone.
                self.assertEqual(len(list(app.query("#pane-2"))), 0)
                self.assertNotEqual(app._selected_pane_id, "%2")


# ─── Journey 7: Search filter ─────────────────────────────────────────────────


class SearchFilterJourney(unittest.IsolatedAsyncioTestCase):
    async def test_search_filters_window_rail_to_matching_only(self) -> None:
        with _Harness() as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)
                self.assertEqual(len(list(app.query("#window-rail ClickCell"))), 3)

                # Open search modal via utility-actions Search button.
                search_cell = next(
                    c for c in app.query("#utility-actions .command-cell")
                    if c.label_text == "Search"
                )
                await pilot.click(search_cell)
                await _settle(pilot)
                self.assertIsInstance(app.screen, InputModal)

                modal_input = app.screen.query_one("#modal-input", Input)
                modal_input.value = "codex"
                await pilot.press("enter")
                await _settle(pilot)
                # Refresh runs in a worker; pump until filter visibly applied.
                for _ in range(10):
                    await pilot.pause()
                    if len(list(app.query("#window-rail ClickCell"))) <= 1:
                        break

                cards = list(app.query("#window-rail ClickCell"))
                self.assertEqual(len(cards), 1)
                self.assertEqual(cards[0].id, "window-1")


# ─── Journey 8: Spinner advances on real wall clock ───────────────────────────


class SpinnerWallClockJourney(unittest.IsolatedAsyncioTestCase):
    async def test_spinner_frame_advances_when_timer_actually_fires(self) -> None:
        def classifier(cmd, out):
            return ("running", False) if cmd == "claude" else ("idle", False)

        with _Harness(sessions=_agent_session(), classify=classifier) as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)
                self.assertIn("pane-1", app._spinner_targets)

                start_frame = app._spinner_frame
                # Spinner timer fires every 100ms; wait ~500ms.
                await asyncio.sleep(0.55)
                await _settle(pilot)

                # At least 3 frames should have advanced.
                advanced = (app._spinner_frame - start_frame) % 10
                self.assertGreaterEqual(advanced, 3, f"only {advanced} frames in 0.5s")


# ─── Journey 9: Help modal opens and dismisses ────────────────────────────────


class HelpModalJourney(unittest.IsolatedAsyncioTestCase):
    async def test_question_mark_opens_help_modal_and_escape_dismisses(self) -> None:
        with _Harness() as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)

                await pilot.press("question_mark")
                await _settle(pilot)
                self.assertIsInstance(app.screen, HelpModal)

                await pilot.press("escape")
                await _settle(pilot)
                self.assertNotIsInstance(app.screen, HelpModal)


# ─── Journey 10: Empty state ───────────────────────────────────────────────────


class EmptyStateJourney(unittest.IsolatedAsyncioTestCase):
    async def test_zero_sessions_disables_dependent_actions_only_plus_session_active(self) -> None:
        with _Harness(sessions=[]) as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)

                # No nav cells in any rail.
                self.assertEqual(len(list(app.query("#session-rail ClickCell"))), 0)
                self.assertEqual(len(list(app.query("#window-rail ClickCell"))), 0)
                self.assertEqual(len(list(app.query("#pane-rail ClickCell"))), 0)

                # Each rail should have a placeholder Static.
                for rail_id in ("#session-rail", "#window-rail", "#pane-rail"):
                    placeholders = list(app.query(f"{rail_id} Static"))
                    self.assertGreaterEqual(
                        len(placeholders), 1,
                        f"{rail_id} missing empty-state placeholder"
                    )

                # Only "+ Session" should be enabled across the entire cockpit.
                enabled_create = [
                    c.label_text for c in app.query(".command-cell")
                    if not c.disabled and c.label_text.startswith("+")
                ]
                self.assertEqual(enabled_create, ["+ Session"])


# ─── Journey 11: Disabled-button clicks are no-ops ────────────────────────────


class DisabledButtonClickJourney(unittest.IsolatedAsyncioTestCase):
    async def test_clicking_disabled_send_keys_does_not_open_modal(self) -> None:
        with _Harness(sessions=[]) as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)

                send_cell = next(
                    c for c in app.query("#pane-actions .command-cell")
                    if c.label_text == "Send Msg"
                )
                self.assertTrue(send_cell.disabled)

                # Click the disabled cell.
                try:
                    await pilot.click(send_cell)
                except Exception:
                    pass  # Pilot.click on disabled may or may not raise.
                await _settle(pilot)

                # No modal should have opened, no backend send_keys.
                self.assertNotIsInstance(app.screen, InputModal)
                h.send_keys.assert_not_called()


# ─── Journey 12: Refresh races don't double-mount ──────────────────────────────


class RefreshRaceJourney(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_refreshes_do_not_duplicate_widgets(self) -> None:
        with _Harness() as h:
            app = TmuxTUI()
            async with app.run_test(size=(200, 50)) as pilot:
                await _settle(pilot)
                expected = len(list(app.query("#window-rail ClickCell")))

                # Fire several refreshes back-to-back.
                await asyncio.gather(
                    app._do_refresh(),
                    app._do_refresh(),
                    app._do_refresh(),
                )
                await _settle(pilot)

                self.assertEqual(
                    len(list(app.query("#window-rail ClickCell"))),
                    expected,
                    "duplicate window cards after concurrent refreshes",
                )


if __name__ == "__main__":
    unittest.main()
