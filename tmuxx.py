"""tmux-tui: Terminal UI for managing tmux sessions, windows, and panes."""

from __future__ import annotations

import asyncio
import argparse
import copy
import json
import os
import re
import subprocess
import sys
import typing
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

from tmux_core import (
    GitBackend,
    Pane,
    Session,
    TmuxBackend,
    Window,
    classify_pane_status,
    quote,
    xdg_config_path,
)
from tmux_mission import load_latest_mission_state, summarize_mission

from rich.markup import escape
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.events import Key
from textual.widgets import (
    Footer,
    Input,
    Label,
    OptionList,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode


# ── ANSI Helpers ─────────────────────────────────────────────────────────────


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")

# Match ANSI SGR background codes (40-49, 100-107, 48;5;N, 48;2;R;G;B)
_BG_CODE_RE = re.compile(
    r"(?:4[0-9]|10[0-7]|48;5;\d+|48;2;\d+;\d+;\d+)"
)


def _strip_bg_ansi(text: str) -> str:
    """Strip background color codes from ANSI sequences, keeping foreground."""
    def _clean_sgr(m: re.Match) -> str:
        seq = m.group(0)
        # Only process SGR sequences (ending with 'm')
        if not seq.endswith("m"):
            return seq
        inner = seq[2:-1]  # strip \x1b[ and m
        if not inner:
            return seq
        codes = inner.split(";")
        # Rebuild without background codes
        kept: list[str] = []
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == "48" and i + 1 < len(codes):
                # 48;5;N or 48;2;R;G;B
                if codes[i + 1] == "5" and i + 2 < len(codes):
                    i += 3
                    continue
                elif codes[i + 1] == "2" and i + 4 < len(codes):
                    i += 5
                    continue
            if code in (
                "40", "41", "42", "43", "44", "45", "46", "47", "49",
                "100", "101", "102", "103", "104", "105", "106", "107",
            ):
                i += 1
                continue
            kept.append(code)
            i += 1
        if not kept:
            return ""
        return f"\x1b[{';'.join(kept)}m"
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", _clean_sgr, text)


def _ansi_to_text(content: str) -> Text:
    """Convert ANSI content to Rich Text, stripping background colors."""
    return Text.from_ansi(_strip_bg_ansi(content))


# ── Tree Widget ──────────────────────────────────────────────────────────────


class TmuxTree(Tree):
    """Tree widget showing tmux session hierarchy."""

    show_root = reactive(False)
    auto_expand = True

    INDICATOR = " ●"
    INDICATOR_DOT = "●"

    def __init__(self) -> None:
        super().__init__("Sessions", id="tmux-tree")
        self.sessions: list[Session] = []
        self.worktree_windows: dict[str, tuple[str, str]] = {}  # window_id → (branch, status)
        self._fingerprint: str = ""
        self._dot_node: TreeNode | None = None
        self._colors: dict[str, str] = {}

    def _resolve_colors(self) -> dict[str, str]:
        """Resolve theme CSS variables to hex colors for Rich markup."""
        try:
            variables = self.app.get_css_variables()
            return {
                "session": variables.get("accent", "#ffffff"),
                "window": variables.get("accent", "#ffffff"),
                "pane": variables.get("accent", "#ffffff"),
                "ok": variables.get("success", "#87d787"),
                "active": variables.get("warning", "#ffb300"),
            }
        except Exception:
            return {
                "session": "#ffffff",
                "window": "#ffffff",
                "pane": "#ffffff",
                "ok": "#87d787",
                "active": "#ffb300",
            }

    def get_component_rich_style(self, name: str, *, partial: bool = False) -> Style:
        """Disable Tree cursor styling so selection only uses the explicit white-dot marker."""
        if name == "tree--cursor":
            return Style()
        return super().get_component_rich_style(name, partial=partial)

    def _toggle_node(self, node: TreeNode) -> None:
        """Prevent collapsing — always keep nodes expanded."""
        if not node.is_expanded:
            node.expand()

    def watch_cursor_line(self, old: int, new: int) -> None:
        """Move the dot indicator when the cursor moves."""
        super().watch_cursor_line(old, new)
        self._update_dot()

    def _update_dot(self) -> None:
        """Add dot to current cursor node, remove from previous."""
        node = self.cursor_node
        if node is self._dot_node:
            return
        if self._dot_node is not None:
            old_label = self._dot_node.label
            if isinstance(old_label, Text):
                if old_label.plain.endswith(self.INDICATOR):
                    self._dot_node.set_label(old_label[:-len(self.INDICATOR)])
            else:
                label = str(old_label)
                if label.endswith(self.INDICATOR):
                    self._dot_node.set_label(label[:-len(self.INDICATOR)])
        if node is not None and node is not self.root:
            current_label = node.label
            if isinstance(current_label, Text):
                new_label = current_label.copy()
                new_label.append(self.INDICATOR, style="#ffffff")
                node.set_label(new_label)
            else:
                label = str(current_label)
                node.set_label(label + f"[#ffffff]{self.INDICATOR}[/]")
        self._dot_node = node

    @staticmethod
    def _make_fingerprint(sessions: list[Session]) -> str:
        """Structural fingerprint — changes when sessions/windows/panes or worktree associations change."""
        parts: list[str] = []
        for s in sessions:
            parts.append(s.session_id)
            for w in s.windows:
                parts.append(w.window_id)
                for p in w.panes:
                    parts.append(f"{p.pane_id}:{p.worktree_branch}")
        return "|".join(parts)

    def update_tree(self, sessions: list[Session]) -> None:
        """Update tree only if structure changed; skip rebuild otherwise to keep cursor stable."""
        self.sessions = sessions
        fp = self._make_fingerprint(sessions)
        if fp == self._fingerprint:
            self._update_labels()
            # Re-apply dot indicator after labels are refreshed
            self._dot_node = None
            self._update_dot()
        else:
            self._fingerprint = fp
            self._full_rebuild()
            # Dot re-applied after cursor restore in _full_rebuild's call_after_refresh

    def recolor(self) -> None:
        """Rebuild labels with current theme colors."""
        if self.sessions:
            self._update_labels()
            self._dot_node = None
            self._update_dot()

    # ── Shared label builders ──────────────────────────────────────────────
    # Used by both _full_rebuild and _update_labels so badges stay consistent.

    _STATUS_BADGE = {
        "running": " [#4da6ff]▶[/]",
        "waiting_for_input": " [#ff6b6b]⏸[/]",
    }

    @classmethod
    def _pane_label(cls, pane: "Pane", c: dict) -> str:
        active = f" [{c['active']}]●[/]" if pane.active else ""
        badge = cls._STATUS_BADGE.get(pane.status, "")
        return (
            f"[{c['pane']}]{escape(pane.current_command)}[/] "
            f"[dim]{pane.pane_id} {pane.width}x{pane.height}[/]{active}{badge}"
        )

    @classmethod
    def _win_label(cls, win: "Window", c: dict, is_worktree: bool) -> str:
        active = f" [{c['active']}]●[/]" if win.active else ""
        wt = f" [{c['ok']}]●[/]" if is_worktree else ""
        return (
            f"[bold {c['window']}]{escape(win.name)}[/] "
            f"[dim]:{win.window_index}[/]{active}{wt}"
        )

    @staticmethod
    def _sess_label(sess: "Session", c: dict) -> str:
        status = f" [{c['ok']}]attached[/]" if sess.attached else ""
        return f"[bold {c['session']}]{escape(sess.name)}[/]{status}"

    @staticmethod
    def _worktree_leaf_label(pane: "Pane") -> str:
        return f"[#87d787]⎇ {escape(pane.worktree_branch)}[/]"

    def _full_rebuild(self) -> None:
        # Save selected node's tmux ID for cursor restore
        saved_id = self._get_cursor_tmux_id()
        c = self._resolve_colors()

        self.clear()
        if not self.sessions:
            self.root.expand()
            return
        for sess in self.sessions:
            sess_node = self.root.add(self._sess_label(sess, c), data=("session", sess))
            for win in sess.windows:
                is_wt = win.window_id in self.worktree_windows
                win_node = sess_node.add(
                    self._win_label(win, c, is_wt), data=("window", win, sess)
                )
                for pane in win.panes:
                    if pane.worktree_branch:
                        pane_node = win_node.add(
                            self._pane_label(pane, c), data=("pane", pane, win, sess)
                        )
                        pane_node.add_leaf(
                            self._worktree_leaf_label(pane), data=("worktree", pane, win, sess)
                        )
                        pane_node.expand()
                    else:
                        win_node.add_leaf(
                            self._pane_label(pane, c), data=("pane", pane, win, sess)
                        )
                win_node.expand()
            sess_node.expand()
        self.root.expand()

        # Restore cursor after Textual recomputes tree lines
        def _restore(sid=saved_id):
            if sid:
                self._select_by_tmux_id(sid)
            self._dot_node = None
            self._update_dot()
        self.call_after_refresh(_restore)

    def _update_labels(self) -> None:
        """Update labels on existing nodes without rebuilding the tree."""
        c = self._resolve_colors()
        sess_idx = 0
        for sess_node in self.root.children:
            if sess_idx >= len(self.sessions):
                break
            sess = self.sessions[sess_idx]
            sess_node.set_label(self._sess_label(sess, c))
            sess_node.data = ("session", sess)
            win_idx = 0
            for win_node in sess_node.children:
                if win_idx >= len(sess.windows):
                    break
                win = sess.windows[win_idx]
                is_wt = win.window_id in self.worktree_windows
                win_node.set_label(self._win_label(win, c, is_wt))
                win_node.data = ("window", win, sess)
                pane_idx = 0
                for pane_node in win_node.children:
                    if pane_idx >= len(win.panes):
                        break
                    pane = win.panes[pane_idx]
                    pane_node.set_label(self._pane_label(pane, c))
                    pane_node.data = ("pane", pane, win, sess)
                    # Update worktree leaf if present
                    if pane.worktree_branch and pane_node.children:
                        wt_node = pane_node.children[0]
                        wt_node.set_label(self._worktree_leaf_label(pane))
                        wt_node.data = ("worktree", pane, win, sess)
                    pane_idx += 1
                win_idx += 1
            sess_idx += 1

    def _get_cursor_tmux_id(self) -> str | None:
        """Get the tmux ID of the currently selected node."""
        data = self.get_selected_data()
        if not data:
            return None
        kind = data[0]
        if kind == "session":
            return data[1].session_id
        if kind == "window":
            return data[1].window_id
        if kind == "pane":
            return data[1].pane_id
        return None

    def _select_by_tmux_id(self, target_id: str) -> None:
        """Walk tree and move cursor to node matching target tmux ID."""
        for node in self._walk(self.root):
            data = node.data
            if data is None:
                continue
            kind = data[0]
            obj = data[1]
            match_id = None
            if kind == "session":
                match_id = obj.session_id
            elif kind == "window":
                match_id = obj.window_id
            elif kind == "pane":
                match_id = obj.pane_id
            if match_id == target_id:
                self.move_cursor(node)
                return

    def _walk(self, node: TreeNode):
        yield node
        for child in node.children:
            yield from self._walk(child)

    def get_selected_data(self):
        node = self.cursor_node
        if node is None or node.data is None:
            return None
        return node.data

    def get_selected_pane_id(self) -> str | None:
        data = self.get_selected_data()
        if data is None:
            return None
        kind = data[0]
        if kind == "pane":
            return data[1].pane_id
        if kind == "window":
            win: Window = data[1]
            for p in win.panes:
                if p.active:
                    return p.pane_id
            return win.panes[0].pane_id if win.panes else None
        if kind == "session":
            sess: Session = data[1]
            for w in sess.windows:
                if w.active:
                    for p in w.panes:
                        if p.active:
                            return p.pane_id
                    return w.panes[0].pane_id if w.panes else None
            if sess.windows and sess.windows[0].panes:
                return sess.windows[0].panes[0].pane_id
        return None

    def get_selected_session(self) -> Session | None:
        data = self.get_selected_data()
        if data is None:
            return None
        kind = data[0]
        if kind == "session":
            return data[1]
        if kind == "window":
            return data[2]
        if kind == "pane":
            return data[3]
        return None


# ── Window Grid Compositor ───────────────────────────────────────────────────


def compose_window_grid(
    panes: list[Pane],
    captured: dict[str, str],
    max_cols: int = 0,
    accent_color: str = "green",
    border_active_style: str | None = None,
    border_inactive_style: str = "dim",
) -> Text:
    """Compose a styled text grid showing all panes with ANSI colors and borders."""
    if not panes:
        return Text("")

    # Offset to remove tmux's outer borders
    min_left = min(p.left for p in panes)
    min_top = min(p.top for p in panes)
    grid_w = max(p.left + p.width for p in panes) - min_left
    grid_h = max(p.top + p.height for p in panes) - min_top
    if max_cols > 0:
        grid_w = min(grid_w, max_cols)

    # Parse ANSI content into styled lines per pane
    pane_lines: dict[str, list[Text]] = {}
    for pane in panes:
        raw = captured.get(pane.pane_id, "")
        styled = _ansi_to_text(raw)
        pane_lines[pane.pane_id] = styled.split(allow_blank=True)

    # Build cell ownership map (which pane owns each cell)
    cell_owner: list[list[Pane | None]] = [[None] * grid_w for _ in range(grid_h)]
    for pane in panes:
        pl = pane.left - min_left
        pt = pane.top - min_top
        end_c = min(pl + pane.width, grid_w)
        end_r = min(pt + pane.height, grid_h)
        for r in range(pt, end_r):
            cell_owner[r][pl:end_c] = [pane] * (end_c - pl)

    # Some tmux/layout sources report pane rectangles as directly adjacent with
    # no empty separator cell. Preserve a visible frame by overlaying a seam on
    # the first cell of the right/lower pane in those cases.
    forced_h: list[list[bool]] = [[False] * grid_w for _ in range(grid_h)]
    forced_v: list[list[bool]] = [[False] * grid_w for _ in range(grid_h)]
    for a in panes:
        a_l = a.left - min_left
        a_t = a.top - min_top
        a_r = a_l + a.width
        a_b = a_t + a.height
        for b in panes:
            if a is b:
                continue
            b_l = b.left - min_left
            b_t = b.top - min_top
            b_r = b_l + b.width
            b_b = b_t + b.height
            if a_r == b_l and 0 <= b_l < grid_w:
                for r in range(max(a_t, b_t), min(a_b, b_b, grid_h)):
                    forced_v[r][b_l] = True
            if a_b == b_t and 0 <= b_t < grid_h:
                for c in range(max(a_l, b_l), min(a_r, b_r, grid_w)):
                    forced_h[b_t][c] = True

    def _nearest_owner(r: int, c: int, dr: int, dc: int) -> tuple[Pane | None, int | None]:
        rr = r + dr
        cc = c + dc
        dist = 1
        while 0 <= rr < grid_h and 0 <= cc < grid_w:
            owner = cell_owner[rr][cc]
            if owner is not None:
                return owner, dist
            rr += dr
            cc += dc
            dist += 1
        return None, None

    # First pass: infer horizontal and vertical border cells from nearest owners.
    base_h: list[list[bool]] = [[False] * grid_w for _ in range(grid_h)]
    base_v: list[list[bool]] = [[False] * grid_w for _ in range(grid_h)]
    for r in range(grid_h):
        for c in range(grid_w):
            if cell_owner[r][c] is not None:
                continue
            left_owner, _ = _nearest_owner(r, c, 0, -1)
            right_owner, _ = _nearest_owner(r, c, 0, 1)
            up_owner, _ = _nearest_owner(r, c, -1, 0)
            down_owner, _ = _nearest_owner(r, c, 1, 0)
            base_v[r][c] = (
                left_owner is not None
                and right_owner is not None
                and left_owner is not right_owner
            )
            base_h[r][c] = (
                up_owner is not None
                and down_owner is not None
                and up_owner is not down_owner
            )

    # Second pass: bridge line continuity across split intersections.
    h_map: list[list[bool]] = [row[:] for row in base_h]
    v_map: list[list[bool]] = [row[:] for row in base_v]
    for r in range(1, grid_h - 1):
        for c in range(1, grid_w - 1):
            if cell_owner[r][c] is not None:
                continue
            if not v_map[r][c] and v_map[r - 1][c] and v_map[r + 1][c]:
                v_map[r][c] = True
            if not h_map[r][c] and h_map[r][c - 1] and h_map[r][c + 1]:
                h_map[r][c] = True

    # Third pass: snap horizontal/vertical lines into tee/cross intersections.
    for r in range(1, grid_h - 1):
        for c in range(1, grid_w - 1):
            if cell_owner[r][c] is not None:
                continue
            if v_map[r][c] and (h_map[r][c - 1] or h_map[r][c + 1]):
                h_map[r][c] = True
            if h_map[r][c] and (v_map[r - 1][c] or v_map[r + 1][c]):
                v_map[r][c] = True

    def _h_at(r: int, c: int) -> bool:
        return 0 <= r < grid_h and 0 <= c < grid_w and (h_map[r][c] or forced_h[r][c])

    def _v_at(r: int, c: int) -> bool:
        return 0 <= r < grid_h and 0 <= c < grid_w and (v_map[r][c] or forced_v[r][c])

    def _is_border_cell(r: int, c: int) -> bool:
        return cell_owner[r][c] is None or forced_h[r][c] or forced_v[r][c]

    def _border_char(r: int, c: int) -> str:
        if not _is_border_cell(r, c):
            return " "
        is_h = _h_at(r, c)
        is_v = _v_at(r, c)
        if not is_h and not is_v:
            return " "
        if is_h and not is_v:
            return "─"
        if is_v and not is_h:
            return "│"

        up = _v_at(r - 1, c)
        down = _v_at(r + 1, c)
        left = _h_at(r, c - 1)
        right = _h_at(r, c + 1)
        if up and down and left and right:
            return "┼"
        if left and right and down:
            return "┬"
        if left and right and up:
            return "┴"
        if up and down and right:
            return "├"
        if up and down and left:
            return "┤"
        if down and right:
            return "┌"
        if down and left:
            return "┐"
        if up and right:
            return "└"
        if up and left:
            return "┘"
        return "┼"

    BORDER_ACTIVE = border_active_style or accent_color
    BORDER_INACTIVE = border_inactive_style

    def _border_style(r: int, c: int) -> str:
        owner = cell_owner[r][c]
        if owner is not None and owner.active:
            return BORDER_ACTIVE
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < grid_h and 0 <= nc < grid_w:
                owner = cell_owner[nr][nc]
                if owner is not None and owner.active:
                    return BORDER_ACTIVE
        return BORDER_INACTIVE

    # Build styled output row by row, segment by segment
    result = Text()
    for r in range(grid_h):
        if r > 0:
            result.append("\n")
        c = 0
        while c < grid_w:
            pane = cell_owner[r][c]
            if pane is not None and not _is_border_cell(r, c):
                # Pane content segment
                pl = pane.left - min_left
                pt = pane.top - min_top
                span_end = min(pl + pane.width, grid_w)
                while span_end > c and any(_is_border_cell(r, cc) for cc in range(c, span_end)):
                    span_end -= 1
                span = span_end - c
                row_offset = r - pt
                col_offset = c - pl
                lines = pane_lines.get(pane.pane_id, [])
                if row_offset < len(lines):
                    line = lines[row_offset]
                    pad = pane.width - line.cell_len
                    padded = line + Text(" " * pad) if pad > 0 else line
                    result.append(padded[col_offset:col_offset + span])
                else:
                    result.append(" " * span)
                c = span_end
            else:
                # Border segment — batch contiguous cells sharing the same style
                style = _border_style(r, c)
                chars: list[str] = [_border_char(r, c)]
                c += 1
                while c < grid_w and _is_border_cell(r, c) and _border_style(r, c) == style:
                    chars.append(_border_char(r, c))
                    c += 1
                result.append(Text("".join(chars), style=style))

    return result


# ── Pane Preview ─────────────────────────────────────────────────────────────


class PanePreview(Static):
    """Shows captured pane output."""

    _LOGO = [
        "  _|                                                    ",
        "_|_|_|_|  _|_|_|  _|_|    _|    _|  _|    _|  _|    _|  ",
        "  _|      _|    _|    _|  _|    _|    _|_|      _|_|    ",
        "  _|      _|    _|    _|  _|    _|  _|    _|  _|    _|  ",
        "    _|_|  _|    _|    _|    _|_|_|  _|    _|  _|    _|  ",
    ]
    _TAGLINE = "Your terminal, orchestrated. By you and your agents."
    _BODY = [
        "TUI for humans. Deterministic agent CLI for AI workflows.",
        "One interface to see, control, and automate tmux.",
    ]

    def __init__(self) -> None:
        super().__init__("", id="pane-preview", classes="intro")
        self._last_key: str = ""
        self._plain_text: str = ""

    def on_mount(self) -> None:
        self._show_intro()

    def _get_accent(self) -> str:
        try:
            return self.app.get_css_variables().get("accent", "#5fd7ff")
        except Exception:
            return "#5fd7ff"

    def _build_intro(self) -> str:
        accent = self._get_accent()
        lines: list[str] = []
        for line in self._LOGO:
            lines.append(f"[bold {accent}]{line}[/]")
        lines.append("")
        lines.append(f"[bold]{self._TAGLINE}[/]")
        lines.append("")
        for line in self._BODY:
            lines.append(f"[dim]{line}[/]" if line else "")
        return "\n".join(lines)

    def _show_intro(self) -> None:
        self._last_key = "INTRO"
        self.add_class("intro")
        self.update(self._build_intro())

    def _scroll_to_bottom(self) -> None:
        self.call_after_refresh(
            lambda: self.scroll_end(
                animate=False,
                immediate=True,
                x_axis=False,
                y_axis=True,
            )
        )

    def set_message(self, message: str) -> None:
        key = f"msg:{message}"
        if key == self._last_key:
            return
        self._last_key = key
        self._plain_text = message
        self.remove_class("intro")
        self.update(message)
        self._scroll_to_bottom()

    def set_content(self, pane: Pane, content: str) -> None:
        key = f"pane:{pane.pane_id}:{content}"
        if key == self._last_key:
            return
        self._last_key = key
        self.remove_class("intro")
        pane_status = " [#ffb300]●[/]" if pane.active else ""
        header = Text.from_markup(
            f"[bold]Preview: {pane.pane_id}[/bold] ({escape(pane.current_command)}) "
            f"{pane.width}x{pane.height}{pane_status}\n"
        )
        body = _ansi_to_text(content)
        combined = header + body
        self._plain_text = combined.plain
        self.update(combined)
        self._scroll_to_bottom()

    def set_window_content(self, win: Window, grid: Text) -> None:
        key = f"win:{win.window_id}:{grid.plain}"
        if key == self._last_key:
            return
        self._last_key = key
        self.remove_class("intro")
        min_l = min(p.left for p in win.panes) if win.panes else 0
        min_t = min(p.top for p in win.panes) if win.panes else 0
        grid_w = max(p.left + p.width for p in win.panes) - min_l if win.panes else 0
        grid_h = max(p.top + p.height for p in win.panes) - min_t if win.panes else 0
        win_status = " [#ffb300]●[/]" if win.active else ""
        header = Text.from_markup(
            f"[bold]Window: {escape(win.name)}[/bold] ({win.window_id}) "
            f"{grid_w}x{grid_h} ({len(win.panes)} panes){win_status}\n"
        )
        combined = header + grid
        self._plain_text = combined.plain
        self.update(combined)
        self._scroll_to_bottom()

    @staticmethod
    def _fmt_age(epoch: int) -> str:
        """Format a unix epoch as a human-readable age string."""
        import time
        if epoch <= 0:
            return "—"
        delta = int(time.time()) - epoch
        if delta < 60:
            return f"{delta}s"
        if delta < 3600:
            return f"{delta // 60}m"
        if delta < 86400:
            h, m = divmod(delta, 3600)
            return f"{h}h{m // 60:02d}m"
        d, rem = divmod(delta, 86400)
        return f"{d}d{rem // 3600}h"

    def set_session_content(self, sess: Session) -> None:
        total_panes = sum(len(w.panes) for w in sess.windows)
        attach_dot = "[green]●[/]" if sess.attached else "[dim]○[/]"
        uptime = self._fmt_age(sess.created)
        last_active = self._fmt_age(sess.activity)

        lines: list[str] = []
        lines.append(f"[bold]{escape(sess.name)}[/bold]  {attach_dot} {'attached' if sess.attached else '[dim]detached[/]'}")
        lines.append(f"[dim]{len(sess.windows)} windows  {total_panes} panes  uptime {uptime}  active {last_active} ago[/]")
        lines.append("")

        if not sess.windows:
            lines.append("[dim]No windows[/]")
        else:
            for w in sess.windows:
                active = "[#ffb300]●[/]" if w.active else "[dim]○[/]"
                w_age = self._fmt_age(w.activity)
                lines.append(f"  {active} [bold]{escape(w.name)}[/]  [dim]:{w.window_index}  active {w_age} ago[/]")
                for p in w.panes:
                    p_active = "[#ffb300]●[/]" if p.active else "[dim]○[/]"
                    path = p.current_path.replace(os.path.expanduser("~"), "~") if p.current_path else ""
                    lines.append(f"      {p_active} {p.pane_id}  [dim]{escape(p.current_command)}  {p.width}x{p.height}  {escape(path)}[/]")

        body = "\n".join(lines)
        key = f"sess:{sess.session_id}:{body}"
        if key == self._last_key:
            return

        self._last_key = key
        self._plain_text = Text.from_markup(body).plain
        self.remove_class("intro")
        self.update(Text.from_markup(body))
        self._scroll_to_bottom()

    def set_mission_content(self, summary: dict[str, typing.Any] | None) -> None:
        if not summary:
            self.set_message("No active mission found")
            return
        lines = [
            f"[bold]Mission:[/bold] {escape(str(summary.get('mission_id', '')))}",
            f"[dim]{escape(str(summary.get('goal', '')))}[/]",
            "",
            f"Status: [bold]{escape(str(summary.get('status', '')))}[/bold]  Next: {escape(str(summary.get('next_action', '')))}",
            f"Supervisor: {escape(str(summary.get('supervisor_pane', '')))}",
            "",
            "[bold]Workers[/bold]",
        ]
        for worker in summary.get("workers", []):
            pane_ids = ", ".join(worker.get("pane_ids") or []) or "<missing>"
            lines.append(
                f"  {escape(str(worker.get('role', '')))}  "
                f"[dim]{escape(str(worker.get('kind', '')))}:{escape(str(worker.get('target', '')))}[/]  "
                f"{escape(str(worker.get('status', '')))}  [dim]{escape(pane_ids)}[/]"
            )
        body = "\n".join(lines)
        key = f"mission:{summary.get('mission_id', '')}:{body}"
        if key == self._last_key:
            return
        self._last_key = key
        self._plain_text = Text.from_markup(body).plain
        self.remove_class("intro")
        self.update(Text.from_markup(body))
        self._scroll_to_bottom()

    def clear_preview(self) -> None:
        if self._last_key == "INTRO":
            return
        self._plain_text = ""
        self._show_intro()


class MissionPanel(Static):
    """Top mission status strip for the human-facing TUI."""

    _STATUS_STYLE = {
        "blocked": "#ff6b6b",
        "missing": "#ff6b6b",
        "running": "#4da6ff",
        "idle": "#87d787",
        "unassigned": "dim",
    }

    def __init__(self) -> None:
        super().__init__("", id="mission-panel")
        self._last_key = ""

    def set_summary(self, summary: dict[str, typing.Any] | None) -> None:
        if not summary:
            text = "[dim]mission[/]  none  [dim]start with `tmuxx agent mission start`[/]"
            if text != self._last_key:
                self._last_key = text
                self.update(Text.from_markup(text))
            return
        counts = summary.get("counts", {})
        status = str(summary.get("status", "unknown"))
        style = self._STATUS_STYLE.get(status, "dim")
        text = (
            f"[dim]mission[/] [bold]{escape(str(summary.get('mission_id', '')))}[/]  "
            f"[{style}]{escape(status)}[/]  "
            f"[dim]run[/] {counts.get('running', 0)}  "
            f"[dim]wait[/] {counts.get('waiting_for_input', 0)}  "
            f"[dim]idle[/] {counts.get('idle', 0)}  "
            f"[dim]missing[/] {counts.get('missing', 0)}  "
            f"[dim]supervisor[/] {escape(str(summary.get('supervisor_pane', '')))}"
        )
        if text != self._last_key:
            self._last_key = text
            self.update(Text.from_markup(text))


# ── Modals ───────────────────────────────────────────────────────────────────


class InputModal(ModalScreen[str | None]):
    """Modal for text input (create/rename)."""

    CSS = """
    InputModal {
        align: center middle;
    }
    #input-dialog {
        width: 50;
        height: auto;
        max-height: 12;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #input-dialog Label {
        margin-bottom: 1;
    }
    """

    def __init__(
        self,
        title: str,
        placeholder: str = "",
        initial: str = "",
        allow_empty: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._initial = initial
        self._allow_empty = allow_empty

    def compose(self) -> ComposeResult:
        with Vertical(id="input-dialog"):
            yield Label(self._title)
            yield Input(
                placeholder=self._placeholder,
                value=self._initial,
                id="modal-input",
            )

    def on_mount(self) -> None:
        self.query_one("#modal-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)
        elif self._allow_empty:
            self.dismiss("")
        else:
            self.dismiss(None)

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.prevent_default()
            self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Modal for confirming destructive actions."""

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False, priority=True),
        Binding("n", "cancel", "No", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-dialog {
        width: 40;
        height: auto;
        max-height: 8;
        border: tall $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-dialog Label {
        margin-bottom: 1;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._message)
            yield Label("[dim]y[/] confirm  [dim]n/esc[/] cancel")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


HELP_TEXT = """\
[bold]Navigate[/]
  [bold accent]a[/]       Attach to session via selected window/pane
  [bold accent]s[/]       Select (activate) window/pane in tmux
  [bold accent]m[/]       Show mission dashboard
  [bold accent]/[/]       Filter sessions/windows by name
  [bold accent]b[/]       Toggle tree sidebar
  [bold accent]<[/] [bold accent]>[/]     Resize tree panel
  [bold accent]R[/]       Force refresh

[bold]Act[/]
  [bold accent]c[/]       Send command to selected pane
  [bold accent]n[/]       New session
  [bold accent]w[/]       New window in current session
  [bold accent]h[/]       Split pane horizontally
  [bold accent]v[/]       Split pane vertically

[bold]Modify[/]
  [bold accent]k[/]       Kill selected session/window/pane
  [bold accent]r[/]       Rename session or window
  [bold accent]+ / -[/]   Resize pane up/down
  [bold accent][ / ][/]   Resize pane left/right

[bold]General[/]
  [bold accent]y[/]       Copy preview to clipboard
  [bold accent]?[/]       Show this help
  [bold accent]q[/]       Quit
"""

class HelpModal(ModalScreen[None]):
    """Modal for displaying keyboard shortcuts."""

    BINDINGS = [
        Binding("escape", "dismiss", "Dismiss", show=False, priority=True),
        Binding("q", "dismiss", "Dismiss", show=False, priority=True),
        Binding("question_mark", "dismiss", "Dismiss", show=False, priority=True),
    ]

    CSS = """
    HelpModal {
        align: center middle;
    }
    #help-dialog {
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #help-title-bar {
        height: 1;
        width: 100%;
    }
    #help-title {
        width: 1fr;
    }
    #help-esc {
        width: auto;
        text-align: right;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            with Horizontal(id="help-title-bar"):
                yield Static("[bold]Keyboard Shortcuts[/bold]", id="help-title")
                yield Static("[dim]Esc[/]", id="help-esc")
            yield Label(HELP_TEXT)

    def action_dismiss(self) -> None:
        self.dismiss(None)


# ── Main App ─────────────────────────────────────────────────────────────────

_CONFIG_PATH = xdg_config_path("config.json")


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(data: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")


_TMUXX_STATUS_TAG = "#[bg=colour214,fg=colour0,bold] ◀ BACK #[default] "
_TMUXX_STATUS_TAG_OLD = "#[fg=colour214,bold] [tmuxx] "


def _get_tmux_global_option(name: str) -> str | None:
    result = subprocess.run(
        ["tmux", "show-option", "-gv", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _tmux_style_to_rich_color(style: str | None, fallback: str) -> str:
    if not style:
        return fallback
    match = re.search(r"(?:^|[ ,])fg=([^, ]+)", style)
    if not match:
        return fallback
    color = match.group(1)
    if color in {"default", "terminal"}:
        return fallback
    if color.startswith("colour") and color[6:].isdigit():
        return f"color({color[6:]})"
    if color.startswith("color") and color[5:].isdigit():
        return f"color({color[5:]})"
    return color


def _tmux_pane_border_styles() -> tuple[str, str]:
    inactive = _tmux_style_to_rich_color(
        _get_tmux_global_option("pane-border-style"),
        "dim",
    )
    active = _tmux_style_to_rich_color(
        _get_tmux_global_option("pane-active-border-style"),
        inactive,
    )
    return inactive, active


def _install_tmux_integration() -> None:
    """Install tmuxx keybinding and status bar button into the running tmux server."""

    # Enable mouse support (required for status bar clicks)
    subprocess.run(["tmux", "set-option", "-g", "mouse", "on"], capture_output=True)

    # Status bar on top
    subprocess.run(["tmux", "set-option", "-g", "status-position", "top"], capture_output=True)

    # Mouse click on status-left -> detach (back to tmuxx TUI)
    subprocess.run(
        ["tmux", "bind-key", "-n", "MouseDown1StatusLeft", "detach-client"],
        capture_output=True,
    )

    # Clean up old [tmuxx] from status-right if present
    result_r = subprocess.run(
        ["tmux", "show-option", "-gv", "status-right"],
        capture_output=True, text=True,
    )
    current_r = result_r.stdout.strip()
    if "[tmuxx]" in current_r:
        cleaned_r = re.sub(r"#\[fg=colour214,bold\]\s*\[tmuxx\]\s*", "", current_r)
        subprocess.run(
            ["tmux", "set-option", "-g", "status-right", cleaned_r],
            capture_output=True,
        )
    if "BACK" in current_r:
        cleaned_r = current_r.replace(_TMUXX_STATUS_TAG, "")
        subprocess.run(
            ["tmux", "set-option", "-g", "status-right", cleaned_r],
            capture_output=True,
        )

    # Prepend BACK button to status-left if not already there
    result_l = subprocess.run(
        ["tmux", "show-option", "-gv", "status-left"],
        capture_output=True, text=True,
    )
    current_l = result_l.stdout.strip()
    # Clean up old [tmuxx] tag if present
    if "[tmuxx]" in current_l:
        current_l = re.sub(r"#\[fg=colour214,bold\]\s*\[tmuxx\]\s*", "", current_l)
    if "BACK" not in current_l:
        subprocess.run(
            ["tmux", "set-option", "-g", "status-left", _TMUXX_STATUS_TAG + current_l],
            capture_output=True,
        )

    # Fix status-bg/fg overrides that can clobber status-style background
    for opt in ("status-bg", "status-fg"):
        check = subprocess.run(
            ["tmux", "show-option", "-gv", opt],
            capture_output=True, text=True,
        )
        if check.stdout.strip() == "default":
            subprocess.run(
                ["tmux", "set-option", "-gu", opt],
                capture_output=True,
            )

    # Ensure status-left-length accommodates the BACK tag
    result_len = subprocess.run(
        ["tmux", "show-option", "-gv", "status-left-length"],
        capture_output=True, text=True,
    )
    try:
        current_len = int(result_len.stdout.strip())
    except ValueError:
        current_len = 10
    needed = max(current_len, 30)
    if current_len < needed:
        subprocess.run(
            ["tmux", "set-option", "-g", "status-left-length", str(needed)],
            capture_output=True,
        )



class TmuxTUI(App):
    """Main TUI application for tmux management."""

    TITLE = "tmuxx"
    ENABLE_COMMAND_PALETTE = True

    def get_system_commands(self, screen) -> typing.Iterable:
        for cmd in super().get_system_commands(screen):
            if "keys" not in cmd.title.lower():
                yield cmd

    CSS = """
    #app-header {
        dock: top;
        height: 1;
        background: $surface;
        color: $text-muted;
        content-align: center middle;
        text-align: center;
    }
    #mission-panel {
        dock: top;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
        border-bottom: solid $accent 20%;
    }
    #main-container {
        height: 1fr;
    }
    #tree-panel {
        width: 1fr;
        overflow-y: auto;
        scrollbar-size: 0 0;
        border-right: solid $accent 15%;
    }
    #preview-panel {
        width: 2fr;
        overflow-y: auto;
        overflow-x: auto;
        padding: 0 1;
        scrollbar-size: 0 0;
    }
    #pane-preview {
        width: auto;
        min-width: 100%;
        scrollbar-size: 0 0;
    }
    #pane-preview.intro {
        height: 1fr;
        content-align: center middle;
        text-align: center;
    }
    TmuxTree {
        width: 100%;
    }
    TmuxTree > .tree--cursor {
        background: transparent;
    }
    TmuxTree:focus > .tree--cursor {
        background: transparent;
    }
    TmuxTree > .tree--highlight-line {
        background: $accent 10%;
    }
    TmuxTree > .tree--guides {
        color: $accent 30%;
    }
    TmuxTree > .tree--guides-hover {
        color: $accent 30%;
    }
    TmuxTree > .tree--guides-selected {
        color: $accent 30%;
    }
    CommandPalette {
        align: center middle;
    }
    CommandPalette > Vertical {
        margin-top: 0;
        width: 60;
        max-height: 20;
    }
    Footer .footer-key--description {
        /* suppress hover tooltip popups */
    }
    .tooltip {
        display: none;
    }
    """

    BINDINGS = [
        # Navigation
        Binding("a", "attach", "Attach", priority=True),
        Binding("s", "activate", "Select", priority=True),
        Binding("slash", "search", "Search", key_display="/", priority=True),
        # Actions
        Binding("c", "send_command", "Cmd", priority=True),
        Binding("n", "new_session", "New", priority=True),
        Binding("w", "new_window", "Window", priority=True),
        Binding("k", "kill_selected", "Kill", priority=True),
        Binding("r", "rename", "Rename", priority=True),
        # Meta
        Binding("m", "mission_dashboard", "Mission", priority=True),
        Binding("question_mark", "help", "Help", key_display="?", priority=True),
        Binding("q", "quit", "Quit", priority=True),
        # Hidden but available
        Binding("h", "split_h", "Split H", show=False, priority=True),
        Binding("v", "split_v", "Split V", show=False, priority=True),
        Binding("y", "copy_preview", "Yank", show=False, priority=True),
        Binding("b", "toggle_sidebar", "Sidebar", show=False, priority=True),
        Binding("R", "force_refresh", "Refresh", key_display="R", show=False, priority=True),
        Binding("plus_sign", "resize('up')", "+Resize", key_display="+", show=False, priority=True),
        Binding("hyphen_minus", "resize('down')", "-Resize", key_display="-", show=False, priority=True),
        Binding("left_square_bracket", "resize('left')", "[Resize", key_display="[", show=False, priority=True),
        Binding("right_square_bracket", "resize('right')", "]Resize", key_display="]", show=False, priority=True),
        Binding("less_than_sign", "panel_resize('shrink')", "<Panel", key_display="<", show=False, priority=True),
        Binding("greater_than_sign", "panel_resize('grow')", ">Panel", key_display=">", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.backend = TmuxBackend()
        self.git = GitBackend()
        self._worktree_windows: dict[str, tuple[str, str]] = {}  # window_id → (branch, status)
        self._tree = TmuxTree()
        self._preview = PanePreview()
        self._mission_panel = MissionPanel()
        self._mission_summary: dict[str, typing.Any] | None = None
        self._session_count = 0
        self._window_count = 0
        self._pane_count = 0
        self._tree_fr = 1  # tree panel width in fr units (preview is always this * 2 initially)
        self._selection_kind: str = ""  # "session", "window", "pane", or ""
        self._search_filter: str = ""  # current search/filter string

    def compose(self) -> ComposeResult:
        yield Static(
            "[#ffb300]●[/] [dim]active[/]  [#ffffff]●[/] [dim]selected[/]  [#87d787]●[/] attached  "
            "[#4da6ff]▶[/] [dim]running[/]  [#ff6b6b]⏸[/] [dim]waiting[/]  "
            "[#87d787]⎇[/] [dim]worktree[/]",
            id="app-header",
        )
        yield self._mission_panel
        with Horizontal(id="main-container"):
            with Vertical(id="tree-panel"):
                yield self._tree
            with Vertical(id="preview-panel"):
                yield self._preview
        yield Footer()


    def on_mount(self) -> None:
        cfg = _load_config()
        saved_theme = cfg.get("theme")
        if saved_theme and saved_theme != self.theme:
            self.theme = saved_theme
        _install_tmux_integration()
        self.refresh_bindings()
        self.refresh_data()
        interval = float(cfg.get("refresh_interval", 2.0))
        self.set_interval(interval, self.refresh_data)


    async def _do_refresh(self) -> None:
        """Core refresh logic — called directly by actions for instant update."""
        try:
            sessions = await self.backend.get_hierarchy()
        except Exception:
            sessions = []

        # Detect pane-level statuses and prompt needs
        for s in sessions:
            for w in s.windows:
                window_has_running = False
                window_has_prompt = False
                for p in w.panes:
                    try:
                        recent_output = await self.backend.capture_pane(p.pane_id, lines=50)
                    except Exception:
                        recent_output = ""
                    p.recent_output = recent_output
                    p.status, p.needs_prompt = classify_pane_status(p.current_command, recent_output)
                    if p.status == "waiting_for_input":
                        window_has_prompt = True
                    elif p.status == "running":
                        window_has_running = True
                if window_has_prompt:
                    w.status = "waiting_for_input"
                elif window_has_running:
                    w.status = "running"
                else:
                    w.status = "idle"

        # Worktree discovery — auto-detect per-pane via git
        self._worktree_windows.clear()
        all_panes = [
            (p, w, s)
            for s in sessions for w in s.windows for p in w.panes
        ]
        detect_tasks = [
            GitBackend.detect_worktree_branch(p.current_path)
            for p, _, _ in all_panes
        ]
        branches = await asyncio.gather(*detect_tasks, return_exceptions=True)
        for (p, w, _s), branch in zip(all_panes, branches):
            if isinstance(branch, str) and branch:
                p.worktree_branch = branch
                status = "running" if p.status == "running" else "done"
                existing = self._worktree_windows.get(w.window_id)
                if not existing or (status == "running" and existing[1] != "running"):
                    self._worktree_windows[w.window_id] = (branch, status)

        await self._update_mission_panel(sessions)

        # Apply search filter
        if self._search_filter:
            q = self._search_filter.lower()
            filtered: list[Session] = []
            for s in sessions:
                matching_windows = [
                    w for w in s.windows
                    if q in w.name.lower() or q in s.name.lower()
                    or any(q in p.current_command.lower() or q in p.worktree_branch.lower() for p in w.panes)
                ]
                if matching_windows:
                    fs = copy.copy(s)
                    fs.windows = matching_windows
                    filtered.append(fs)
            sessions = filtered

        # Count totals
        self._session_count = len(sessions)
        self._window_count = sum(len(s.windows) for s in sessions)
        self._pane_count = sum(
            len(w.panes) for s in sessions for w in s.windows
        )

        self._tree.worktree_windows = self._worktree_windows
        self._tree.update_tree(sessions)

        await self._update_preview()

    async def _update_mission_panel(self, sessions: list[Session]) -> None:
        try:
            repo_root = await self.git.get_repo_root()
            mission = load_latest_mission_state(repo_root)
        except Exception:
            mission = None

        if not mission:
            self._mission_summary = None
            self._mission_panel.set_summary(None)
            return

        panes: list[dict[str, typing.Any]] = []
        for s in sessions:
            for w in s.windows:
                for p in w.panes:
                    panes.append(
                        {
                            "pane_id": p.pane_id,
                            "pane_index": p.pane_index,
                            "window_id": w.window_id,
                            "window_name": w.name,
                            "session_id": s.session_id,
                            "session_name": s.name,
                            "current_command": p.current_command,
                            "current_path": p.current_path,
                            "branch": p.worktree_branch,
                            "status": p.status,
                            "needs_prompt": p.needs_prompt,
                            "recent_output": p.recent_output,
                        }
                    )
        self._mission_summary = summarize_mission(mission, panes)
        self._mission_panel.set_summary(self._mission_summary)

    @work(exclusive=True)
    async def refresh_data(self) -> None:
        """Timer-driven refresh (runs as background worker)."""
        await self._do_refresh()

    async def _update_preview(self) -> None:
        data = self._tree.get_selected_data()
        if not data:
            self._preview.clear_preview()
            self._scroll_preview_to_bottom()
            return

        kind = data[0]

        if kind == "pane":
            pane: Pane = data[1]
            await self._show_pane_preview(pane)
        elif kind == "worktree":
            pane = data[1]
            await self._show_pane_preview(pane)
        elif kind == "window":
            win: Window = data[1]
            await self._show_window_preview(win)
        elif kind == "session":
            sess: Session = data[1]
            self._preview.set_session_content(sess)

        self._scroll_preview_to_bottom()

    def _scroll_preview_to_bottom(self) -> None:
        try:
            panel = self.query_one("#preview-panel")
        except Exception:
            return
        self.call_after_refresh(
            lambda: panel.scroll_end(
                animate=False,
                immediate=True,
                x_axis=False,
                y_axis=True,
                force=True,
            )
        )

    async def _show_pane_preview(self, pane: Pane) -> None:
        try:
            content = await self.backend.capture_pane(pane.pane_id)
        except Exception:
            content = "(capture failed)"
        self._preview.set_content(pane, content)

    async def _show_window_preview(self, win: Window) -> None:
        if not win.panes:
            self._preview.clear_preview()
            return

        try:
            captured = await self.backend.capture_window_panes(win.panes)
        except Exception:
            self._preview.set_message("(capture failed)")
            return

        try:
            avail_w = self._preview.size.width
        except Exception:
            avail_w = 0
        accent = self.get_css_variables().get("accent", "green")
        inactive_border, active_border = _tmux_pane_border_styles()
        grid_text = compose_window_grid(
            win.panes,
            captured,
            max_cols=avail_w,
            accent_color=accent,
            border_active_style=active_border,
            border_inactive_style=inactive_border,
        )
        self._preview.set_window_content(win, grid_text)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._update_selection_kind()
        self._refresh_preview()

    def _update_selection_kind(self) -> None:
        data = self._tree.get_selected_data()
        old_kind = self._selection_kind
        self._selection_kind = data[0] if data else ""
        if self._selection_kind != old_kind:
            self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Hide/disable actions that don't apply to the current selection."""
        # Block app-level actions when a modal is active;
        # the modal's own bindings (Escape, q→dismiss) handle keys instead.
        if len(self.screen_stack) > 1:
            return False

        k = self._selection_kind

        # Always available
        if action in ("quit", "help", "new_session", "toggle_sidebar", "force_refresh", "search"):
            return True
        if action == "mission_dashboard":
            return True

        # Session or window/pane
        if action == "new_window":
            return k in ("session", "window", "pane")
        if action == "kill_selected":
            return k in ("session", "window", "pane")
        if action == "rename":
            return k in ("session", "window")

        # Window or pane only
        if action in ("split_h", "split_v", "activate", "attach", "copy_preview", "resize", "send_command"):
            return k in ("window", "pane")

        return True

    @work(exclusive=True, group="preview")
    async def _refresh_preview(self) -> None:
        await self._update_preview()

    # ── Actions ──────────────────────────────────────────────────────────

    def action_new_session(self) -> None:
        self.push_screen(
            InputModal("New session name:", placeholder="my-session"),
            callback=self._on_new_session,
        )

    def _on_new_session(self, name: str | None) -> None:
        if name:
            self._do_new_session(name)

    @work
    async def _do_new_session(self, name: str) -> None:
        try:
            await self.backend.new_session(name)
        except Exception as e:
            self.notify(f"Create session failed: {e}", severity="error")
            return
        await self._do_refresh()

    def action_new_window(self) -> None:
        sess = self._tree.get_selected_session()
        if not sess:
            self.notify("Select a session, window, or pane first", severity="warning")
            return
        self._new_window_session = sess.name
        self.push_screen(
            InputModal("New window name (optional):", placeholder="window-name", allow_empty=True),
            callback=self._on_new_window,
        )

    def _on_new_window(self, name: str | None) -> None:
        if name is None:
            return
        self._do_new_window(self._new_window_session, name if name else None)

    @work
    async def _do_new_window(self, session: str, name: str | None) -> None:
        try:
            await self.backend.new_window(session, name)
        except Exception as e:
            self.notify(f"Create window failed: {e}", severity="error")
            return
        await self._do_refresh()

    async def action_split_h(self) -> None:
        pane_id = self._tree.get_selected_pane_id()
        if not pane_id:
            self.notify("Select a window or pane first", severity="warning")
            return
        try:
            await self.backend.split_pane(pane_id, horizontal=True)
        except Exception as e:
            self.notify(f"Split failed: {e}", severity="error")
            return
        await self._do_refresh()

    async def action_split_v(self) -> None:
        pane_id = self._tree.get_selected_pane_id()
        if not pane_id:
            self.notify("Select a window or pane first", severity="warning")
            return
        try:
            await self.backend.split_pane(pane_id, horizontal=False)
        except Exception as e:
            self.notify(f"Split failed: {e}", severity="error")
            return
        await self._do_refresh()

    def action_kill_selected(self) -> None:
        data = self._tree.get_selected_data()
        if not data:
            return
        kind = data[0]

        if kind == "session":
            sess: Session = data[1]
            is_last = self._session_count <= 1
            msg = f"Kill session '{sess.name}'?"
            if is_last:
                msg += "\nThis is the last session — tmux server will exit."
            self._kill_pending = ("session", sess.name, is_last)
            self.push_screen(ConfirmModal(msg), callback=self._on_kill_confirm)
        elif kind == "window":
            win: Window = data[1]
            self._kill_pending = ("window", win.window_id, False)
            self.push_screen(
                ConfirmModal(f"Kill window '{win.name}' ({win.window_id})?"),
                callback=self._on_kill_confirm,
            )
        elif kind == "pane":
            pane: Pane = data[1]
            self._kill_pending = ("pane", pane.pane_id, False)
            self.push_screen(
                ConfirmModal(f"Kill pane {pane.pane_id}?"),
                callback=self._on_kill_confirm,
            )

    def _on_kill_confirm(self, ok: bool) -> None:
        if ok and hasattr(self, "_kill_pending"):
            self._do_kill(*self._kill_pending)

    @work
    async def _do_kill(self, kind: str, target: str, is_last: bool) -> None:
        try:
            if kind == "session":
                await self.backend.kill_session(target)
            elif kind == "window":
                await self.backend.kill_window(target)
            elif kind == "pane":
                await self.backend.kill_pane(target)
        except Exception as e:
            if not is_last:
                self.notify(f"Kill failed: {e}", severity="error")
        await self._do_refresh()

    def action_rename(self) -> None:
        data = self._tree.get_selected_data()
        if not data:
            return
        kind = data[0]

        if kind == "session":
            sess: Session = data[1]
            self._rename_pending = ("session", sess.name)
            self.push_screen(
                InputModal("Rename session:", initial=sess.name),
                callback=self._on_rename,
            )
        elif kind == "window":
            win: Window = data[1]
            self._rename_pending = ("window", win.window_id, win.name)
            self.push_screen(
                InputModal("Rename window:", initial=win.name),
                callback=self._on_rename,
            )
        elif kind == "pane":
            self.notify("Panes cannot be renamed", severity="warning")

    def _on_rename(self, new_name: str | None) -> None:
        if not new_name or not hasattr(self, "_rename_pending"):
            return
        pending = self._rename_pending
        if pending[0] == "session" and new_name != pending[1]:
            self._do_rename_session(pending[1], new_name)
        elif pending[0] == "window" and new_name != pending[2]:
            self._do_rename_window(pending[1], new_name)

    @work
    async def _do_rename_session(self, old: str, new: str) -> None:
        try:
            await self.backend.rename_session(old, new)
        except Exception as e:
            self.notify(f"Rename failed: {e}", severity="error")
            return
        await self._do_refresh()

    @work
    async def _do_rename_window(self, window_id: str, new_name: str) -> None:
        try:
            await self.backend.rename_window(window_id, new_name)
        except Exception as e:
            self.notify(f"Rename failed: {e}", severity="error")
            return
        await self._do_refresh()


    def action_toggle_sidebar(self) -> None:
        panel = self.query_one("#tree-panel")
        panel.display = not panel.display

    def action_panel_resize(self, direction: str) -> None:
        tree = self.query_one("#tree-panel")
        preview = self.query_one("#preview-panel")
        if direction == "grow":
            self._tree_fr = min(self._tree_fr + 1, 8)
        else:
            self._tree_fr = max(self._tree_fr - 1, 1)
        tree.styles.width = f"{self._tree_fr}fr"
        preview.styles.width = f"{max(9 - self._tree_fr, 1)}fr"

    async def action_attach(self) -> None:
        data = self._tree.get_selected_data()
        if not data:
            return
        kind = data[0]
        if kind == "window":
            sess_name = data[2].name
        elif kind == "pane":
            sess_name = data[3].name
        else:
            self.notify("Session dashboard is kill-only; select a window or pane", severity="warning")
            return

        # Install status bar button before attaching
        _install_tmux_integration()
        # Suspend TUI and attach to tmux session
        with self.suspend():
            rc = os.system(f"tmux attach-session -t {quote(sess_name)}")
        if rc != 0:
            self.notify("Attach failed", severity="error")

    async def action_activate(self) -> None:
        data = self._tree.get_selected_data()
        if not data:
            return

        kind = data[0]
        try:
            if kind == "window":
                win: Window = data[1]
                await self.backend.select_window(win.window_id)
                pane_id = self._tree.get_selected_pane_id()
                if pane_id:
                    await self.backend.select_pane(pane_id)
            elif kind == "pane":
                pane: Pane = data[1]
                win: Window = data[2]
                await self.backend.select_window(win.window_id)
                await self.backend.select_pane(pane.pane_id)
            else:
                self.notify("Select a window or pane to activate", severity="warning")
                return
        except Exception as e:
            self.notify(f"Activate failed: {e}", severity="error")
            return

        await self._do_refresh()

    def watch_theme(self, old_theme: str, new_theme: str) -> None:
        self._tree.recolor()
        if self._preview._last_key == "INTRO":
            self._preview._show_intro()
        cfg = _load_config()
        cfg["theme"] = new_theme
        _save_config(cfg)

    def action_force_refresh(self) -> None:
        self._trigger_refresh()

    @work(exclusive=True, group="manual-refresh")
    async def _trigger_refresh(self) -> None:
        await self._do_refresh()

    async def action_resize(self, direction: str) -> None:
        pane_id = self._tree.get_selected_pane_id()
        if not pane_id:
            self.notify("Select a window or pane first", severity="warning")
            return
        try:
            await self.backend.resize_pane(pane_id, direction)
        except Exception as e:
            self.notify(f"Resize failed: {e}", severity="error")
            return
        await self._do_refresh()

    def action_copy_preview(self) -> None:
        plain = self._preview._plain_text
        if not plain.strip():
            self.notify("Nothing to copy", severity="warning")
            return
        self.copy_to_clipboard(plain)
        self.notify("Copied to clipboard")

    def action_mission_dashboard(self) -> None:
        self._preview.set_mission_content(self._mission_summary)

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_search(self) -> None:
        self.push_screen(
            InputModal("Filter (empty to clear):", placeholder="session or window name"),
            callback=self._on_search,
        )

    def _on_search(self, query: str | None) -> None:
        # Escape (None) or empty input both clear the filter
        self._search_filter = (query or "").strip()
        self._trigger_refresh()
        if self._search_filter:
            self.notify(f"Filter: {self._search_filter}")
        elif query is not None or self._search_filter == "":
            self.notify("Filter cleared")

    def action_send_command(self) -> None:
        pane_id = self._tree.get_selected_pane_id()
        if not pane_id:
            self.notify("Select a window or pane first", severity="warning")
            return
        self._send_cmd_pane = pane_id
        self.push_screen(
            InputModal(f"Command for {pane_id}:", placeholder="ls -la"),
            callback=self._on_send_command,
        )

    def _on_send_command(self, cmd: str | None) -> None:
        if cmd and hasattr(self, "_send_cmd_pane"):
            self._do_send_command(self._send_cmd_pane, cmd)

    @work
    async def _do_send_command(self, pane_id: str, cmd: str) -> None:
        try:
            await self.backend.send_keys(pane_id, cmd)
        except Exception as e:
            self.notify(f"Send failed: {e}", severity="error")
            return
        self.notify(f"Sent to {pane_id}")
        await asyncio.sleep(0.5)
        await self._do_refresh()


# ── Entry Point ──────────────────────────────────────────────────────────────


def _package_version() -> str:
    try:
        return pkg_version("tmuxx")
    except PackageNotFoundError:
        return "dev"


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmuxx",
        description="TUI for humans. Deterministic agent CLI for automation.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("tui", help="Launch the interactive tmuxx TUI")
    # Keep `agent` args opaque here; subcommand parser lives in tmux_agent.py.
    p_agent = sub.add_parser("agent", add_help=False, help="Run deterministic agent commands")
    p_agent.add_argument("agent_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if not argv or argv[0] == "tui":
        app = TmuxTUI()
        app.run()
        return 0

    if argv[0] == "setup":
        _install_tmux_integration()
        print("tmuxx tmux integration installed:")
        print("  • Click BACK button (top-left) → detach back to tmuxx")
        return 0

    if argv[0] == "agent":
        from tmux_agent import run_agent_cli

        return run_agent_cli(argv[1:])

    parser = _build_cli_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
