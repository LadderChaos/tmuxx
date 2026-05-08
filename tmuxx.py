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
    Input,
    Label,
    RichLog,
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


class PanePreview(RichLog):
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
        super().__init__(
            id="pane-preview",
            classes="intro",
            markup=True,
            wrap=False,
            highlight=False,
            auto_scroll=True,
            min_width=80,
        )
        self._last_key: str = ""
        self._plain_text: str = ""

    def on_mount(self) -> None:
        self._show_intro()

    # Cockpit-aligned accent colors (matches the ClickCell amber primary).
    _INTRO_ACCENT = "#e0b148"
    _INTRO_TAGLINE_COLOR = "#dce8df"

    def _get_accent(self) -> str:
        return self._INTRO_ACCENT

    def _build_intro(self) -> str:
        lines: list[str] = []
        for line in self._LOGO:
            lines.append(f"[bold {self._INTRO_ACCENT}]{line}[/]")
        lines.append("")
        lines.append(f"[bold {self._INTRO_TAGLINE_COLOR}]{self._TAGLINE}[/]")
        lines.append("")
        for line in self._BODY:
            lines.append(f"[#8da095]{line}[/]" if line else "")
        return "\n".join(lines)

    def _show_intro(self) -> None:
        self._last_key = "INTRO"
        self.add_class("intro")
        self.clear()

        # Center content both vertically and horizontally inside the preview.
        try:
            avail_w = max(20, self.size.width - 4)
            avail_h = max(0, self.size.height - 2)
        except Exception:
            avail_w, avail_h = 80, 24

        raw_lines = self._build_intro().split("\n")
        # Compute display width using the plain (un-markup) text.
        plain_lines = [Text.from_markup(line).plain for line in raw_lines]
        widest = max((len(p) for p in plain_lines), default=0)

        top_pad = max(0, (avail_h - len(raw_lines)) // 2)
        for _ in range(top_pad):
            self.write(Text(""), scroll_end=False)

        for raw, plain in zip(raw_lines, plain_lines):
            left_pad = max(0, (avail_w - len(plain)) // 2)
            self.write(Text(" " * left_pad) + Text.from_markup(raw), scroll_end=False)

    def _scroll_to_bottom(self) -> None:
        self.call_after_refresh(
            lambda: self.scroll_end(
                animate=False,
                immediate=True,
                x_axis=False,
                y_axis=True,
            )
        )

    def _scroll_to_top(self) -> None:
        self.call_after_refresh(
            lambda: self.scroll_home(
                animate=False,
                immediate=True,
                x_axis=False,
                y_axis=True,
                force=True,
            )
        )

    def set_message(self, message: str) -> None:
        key = f"msg:{message}"
        if key == self._last_key:
            return
        self._last_key = key
        self._plain_text = message
        self.remove_class("intro")
        self.clear()
        self.write(message, scroll_end=True)
        self._scroll_to_bottom()

    def set_content(self, pane: Pane, content: str) -> None:
        # Header line dropped — the cockpit's pane chip already shows
        # `<window>/<id> <command>` so repeating it here was redundant.
        key = f"pane:{pane.pane_id}:{content}"
        if key == self._last_key:
            return
        self._last_key = key
        self.remove_class("intro")
        body = _ansi_to_text(content)
        self._plain_text = body.plain
        self.clear()
        self.write(body, scroll_end=True)
        self._scroll_to_bottom()

    def set_window_content(self, win: Window, grid: Text) -> None:
        # Header line dropped — the cockpit's window card already shows
        # session/window/pane count.
        key = f"win:{win.window_id}:{grid.plain}"
        if key == self._last_key:
            return
        self._last_key = key
        self.remove_class("intro")
        self._plain_text = grid.plain
        self.clear()
        self.write(grid, scroll_end=False)
        self._scroll_to_top()

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
        self.clear()
        self.write(Text.from_markup(body), scroll_end=True)
        self._scroll_to_bottom()

    def clear_preview(self) -> None:
        if self._last_key == "INTRO":
            return
        self._plain_text = ""
        self._show_intro()


class ClickCell(Static, can_focus=True):
    """A Textual-native click target without Button's boxed rendering."""

    def __init__(
        self,
        label: str,
        *,
        id: str | None = None,
        classes: str = "",
        action: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
        disabled: bool = False,
    ) -> None:
        css = f"click-cell {classes}".strip()
        super().__init__(
            label,
            id=id,
            classes=css,
            disabled=disabled,
            markup=True,
            expand=False,
            shrink=True,
        )
        self.label_text = Text.from_markup(label).plain
        self._action = action
        self._target_kind = target_kind
        self._target_id = target_id

    async def _activate(self) -> None:
        if self.disabled:
            return
        if self._target_kind and self._target_id:
            self.app.select_click_target(self._target_kind, self._target_id)
            return
        if self._action:
            await self.app.run_action(self._action)

    async def on_click(self, event) -> None:
        event.stop()
        await self._activate()

    async def on_key(self, event: Key) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            await self._activate()


# ── Modals ───────────────────────────────────────────────────────────────────


class InputModal(ModalScreen[str | None]):
    """Modal for text input (create/rename)."""

    CSS = """
    InputModal {
        align: center middle;
        background: transparent;
    }
    /* Outer dialog frame is muted so it doesn't compete with the focused
       input — the input's amber border is the visual anchor. */
    #input-dialog {
        width: 50;
        height: auto;
        max-height: 12;
        border: round #5a7263;
        background: #15201b;
        padding: 1 2;
    }
    #input-dialog Label {
        margin-bottom: 1;
    }
    #modal-input {
        background: #15201b;
        border: tall #2c3a32;
    }
    #modal-input:focus {
        border: tall #e0b148;
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
        background: transparent;
    }
    #confirm-dialog {
        width: 40;
        height: auto;
        max-height: 8;
        border: round #d97959;
        background: #15201b;
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
            yield Label("[#e0b148]y[/] confirm  [#e0b148]n[/]/[#e0b148]esc[/] cancel")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


HELP_TEXT = """\
[bold]Cockpit rows[/]
  [#e0b148]Sessions[/]   pills for every tmux session.
              Click switches.  Actions: + Session · Rename · Kill.
  [#e0b148]Windows[/]    cards from EVERY session, prefixed [dim]<session>/<idx> <name>[/].
              Click switches both session and window.
              Actions: + Window · Rename · Attach · Kill.
  [#e0b148]Panes[/]      chips for EVERY pane across all windows, prefixed [dim]<window>/<id>[/].
              Click switches preview to that pane.
              Actions: + Pane H/V · Send Msg · History · Kill.

[bold]Footer utilities[/]
  Home       return to the landing screen
  Refresh    force an immediate hierarchy refetch
  Search     filter windows + panes by name / command / branch
  Copy       copy preview body to clipboard
  Skill      tmuxx CLI surface for agent workflows
  Help       this dialog
  Quit       exit tmuxx

[bold]Attention banner[/]
  Centered footer chip lights up when any pane is in waiting_for_input
  state, listing up to 3 affected windows.  Hidden when nothing's
  blocked.

[bold]Status glyphs (panes only)[/]
  [#e0b148 blink]◉[/] waiting for input (regex on recent output:
              y/n prompts, "Press Enter", "Are you sure", agent prompts)
  [#77b9cd]{SPIN}[/] agent thinking (claude / codex / gemini / copilot /
              aider / gh-copilot, running)
  🤖         agent badge prefix on every recognized agent pane.
              The command name itself is tinted by family —
              [#77b9cd]cyan[/] = claude (process name is the version like
              2.1.128), [#c084fc]magenta[/] = codex (pane title contains
              codex-<uuid>). Followed inline by token count for
              claude (e.g. 645k) or approval mode for codex
              (suggest / auto-edit / full-auto).
  [dim](no glyph)[/] idle or plain running shell

[bold]Keyboard[/]
  [#e0b148]a[/]          attach (window-scope; jumps tmux client to current window)
  [#e0b148]s[/]          send msg to selected pane
  [#e0b148]h[/]          history (full scrollback for selected pane)
  [#e0b148]q[/]          quit
  [#e0b148]?[/]          open this help
  Ctrl+P     command palette (system commands)
  [#e0b148]Esc[/]        dismiss any modal
  Enter      submit modal input
  [#e0b148]y[/] / [#e0b148]n[/]      confirm / cancel destructive prompts
"""

CLI_INTRO_TEXT = """\
The cockpit handles point-and-click tmux ops. The CLI is for
workflows that don't fit a button: agent worktrees, event watches,
supervisor loops, and machine-readable snapshots.

[bold]Worktree task lifecycle[/]
  tmuxx agent start-task <session> "<prompt>" --json
                  [--branch <name>] [--base-branch <branch>]
                  [--agent-command "claude -p"]
  tmuxx agent task-report <branch> --json
  tmuxx agent complete-task <branch> --test-command "<cmd>" --json
  tmuxx agent abort-task <branch> --json

[bold]Watch for events[/]
  tmuxx agent watch --session <name> --event needs_prompt --notify
  tmuxx agent watch --pane %3 --event attention --assume-busy
  tmuxx agent watch --branch <branch> --event completed
  tmuxx agent watch --event text --pattern "<regex>" --exec ./hook.py

[bold]Supervise a worker[/]
  tmuxx agent supervise --supervisor-pane %9 \\
                        --worker-session claude --goal "ship X"

[bold]Status snapshots (--json everywhere)[/]
  tmuxx agent status                # full hierarchy + pane statuses
  tmuxx agent list-sessions
  tmuxx agent list-worktrees
  tmuxx agent capture-pane %0 --lines 200
  tmuxx agent read-agent-log <branch>

[bold]Lower-level passthrough[/]
  tmuxx agent send-text %0 -- <text>
  tmuxx agent send-keys %0 C-c
  tmuxx agent split-pane %0 --horizontal
  tmuxx agent create-session <name>

See `tmuxx --help` and skills/tmuxx/SKILL.md for the full surface.
"""


class CliIntroModal(ModalScreen[None]):
    """Modal listing the tmuxx CLI surface for agent workflows."""

    BINDINGS = [
        Binding("escape", "dismiss", "Dismiss", show=False, priority=True),
        Binding("q", "dismiss", "Dismiss", show=False, priority=True),
    ]

    CSS = """
    CliIntroModal {
        align: center middle;
        background: transparent;
    }
    #cli-dialog {
        width: 86;
        height: auto;
        max-height: 90%;
        border: round #e0b148;
        background: #15201b;
        padding: 1 2;
    }
    #cli-title-bar {
        height: 1;
        width: 100%;
    }
    #cli-title {
        width: 1fr;
    }
    #cli-esc {
        width: auto;
        text-align: right;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="cli-dialog"):
            with Horizontal(id="cli-title-bar"):
                yield Static("[bold]tmuxx · cli surface[/bold]", id="cli-title")
                yield Static("[#e0b148]Esc[/]", id="cli-esc")
            yield Static(" ")
            yield Static(CLI_INTRO_TEXT, markup=True)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class HistoryModal(ModalScreen[None]):
    """Full scrollback for a pane — useful when output scrolls past the preview."""

    BINDINGS = [
        Binding("escape", "dismiss", "Dismiss", show=False, priority=True),
        Binding("q", "dismiss", "Dismiss", show=False, priority=True),
    ]

    CSS = """
    HistoryModal {
        align: center middle;
        background: transparent;
    }
    #history-dialog {
        width: 90%;
        height: 90%;
        border: round #e0b148;
        background: #0e1411;
        padding: 0 1;
        layout: vertical;
    }
    #history-title-bar {
        height: 1;
        layout: horizontal;
    }
    #history-title { width: 1fr; color: #e0b148; text-style: bold; }
    #history-esc { width: auto; text-align: right; color: #e0b148; }
    #history-log {
        height: 1fr;
        background: #0e1411;
        color: #dce8df;
        border: none;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        scrollbar-color: #d6b86a;
        scrollbar-background: #15201b;
    }
    """

    def __init__(self, pane_id: str, content: str) -> None:
        super().__init__()
        self._pane_id = pane_id
        self._content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="history-dialog"):
            with Horizontal(id="history-title-bar"):
                yield Static(f"[bold]History · {self._pane_id}[/]", id="history-title")
                yield Static("[#e0b148]Esc[/]", id="history-esc")
            yield RichLog(id="history-log", highlight=False, markup=False, wrap=False)

    def on_mount(self) -> None:
        log = self.query_one("#history-log", RichLog)
        log.write(_ansi_to_text(self._content))
        log.scroll_end(animate=False)

    def action_dismiss(self) -> None:
        self.dismiss(None)


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
        background: transparent;
    }
    #help-dialog {
        width: 78;
        height: auto;
        max-height: 90%;
        border: round #e0b148;
        background: #15201b;
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

    _SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            with Horizontal(id="help-title-bar"):
                yield Static("[bold]tmuxx · keymap[/bold]", id="help-title")
                yield Static("[#e0b148]Esc[/]", id="help-esc")
            yield Static(" ")
            yield Static(HELP_TEXT, markup=True, id="help-body")

    def on_mount(self) -> None:
        self._frame = 0
        self._tick_spinner()
        self.set_interval(0.1, self._tick_spinner)

    def _tick_spinner(self) -> None:
        self._frame = (self._frame + 1) % len(self._SPINNER_FRAMES)
        char = self._SPINNER_FRAMES[self._frame]
        try:
            body = self.query_one("#help-body", Static)
        except Exception:
            return
        body.update(HELP_TEXT.replace("{SPIN}", char))

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


def _tmux_widget_id(prefix: str, tmux_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", tmux_id).strip("-")
    return f"{prefix}-{safe or 'item'}"


def _status_token(status: str) -> str:
    if status == "waiting_for_input":
        return "WAIT"
    if status == "running":
        return "RUN"
    if status == "error":
        return "ERR"
    return "IDLE"


def _status_human(status: str) -> str:
    if status == "waiting_for_input":
        return "needs input"
    if status == "running":
        return "running"
    if status == "error":
        return "error"
    return "idle"


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
        # Hide built-ins that don't apply to tmuxx's owned palette:
        #  - "keys"  → shows app's keybindings; ours has its own modal.
        #  - "theme" → cockpit colors are hardcoded; theme picker is a no-op.
        for cmd in super().get_system_commands(screen):
            title = cmd.title.lower()
            if "keys" in title or "theme" in title:
                continue
            yield cmd

    CSS = """
    /* Five-tier surface system (console editorial). Theme cycling doesn't
       affect these — the cockpit owns its palette. */
    $bg:           #0a0e0d;
    $panel:        #0e1411;
    $surface:      #15201b;
    $raised:       #1d2a23;
    $active:       #243329;
    $border-dim:   #1f2c25;
    $border-mid:   #2c3a32;
    $border-strong: #5a7263;
    $text:         #dce8df;
    $muted:        #8da095;
    $faint:        #5b6e64;
    $amber:        #e0b148;
    $amber-soft:   #1f1d12;
    $cyan:         #77b9cd;
    $green:        #75c28e;
    $red:          #d97959;
    $red-soft:     #1f1311;

    Screen { background: $bg; color: $text; }
    #tmuxx-board { height: 1fr; background: $bg; }

    /* Attention banner — lives in the footer, just left of the TMUXX brand
       tag. Visible only when any pane is waiting_for_input. */
    #attention-banner {
        display: none;
        height: 1;
        width: auto;
        background: $amber-soft;
        color: $amber;
        text-style: bold;
        padding: 0 1;
        content-align: right middle;
    }
    #attention-banner.alerting {
        display: block;
    }

    .kicker {
        width: auto;
        min-width: 10;
        color: $faint;
        text-style: bold;
        content-align: left middle;
        padding: 0 1 0 0;
    }
    .spacer { width: 1fr; }
    .context-label { color: $faint; content-align: left middle; }

    /* Outer cockpit frame wraps the three nav sections.
       Inner sections have no individual border; horizontal dividers only. */
    #cockpit-frame {
        height: auto;
        layout: vertical;
        background: $panel;
        border: round $border-mid;
    }
    #cockpit-frame:focus-within { border: round $border-strong; }

    #sessions-section,
    #windows-section,
    #panes-section {
        padding: 0 1;
        layout: vertical;
        background: $panel;
    }
    /* Dividers between every section. */
    #sessions-section,
    #windows-section { border-bottom: solid $border-mid; }

    #sessions-section { height: 3; }   /* action + chip + border */
    #windows-section  { height: 3; }   /* action + 1-line cards + border */
    #panes-section    { height: 2; }   /* action + chip */

    #session-actions,
    #window-actions,
    #pane-actions {
        height: 1;
        layout: horizontal;
        content-align: left middle;
    }
    #session-rail,
    #pane-rail {
        height: 1;
        width: 1fr;
        layout: horizontal;
    }
    #window-rail {
        height: 1fr;
        width: 1fr;
        layout: horizontal;
        align: left top;
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size: 0 0;
    }
    #session-rail,
    #pane-rail {
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size: 0 0;
    }

    /* Preview — frameless, blends with cockpit. Lazygit-style scrollbar
       in a muted accent so it shows but doesn't dominate. */
    #preview-panel {
        height: 1fr;
        layout: vertical;
        background: $panel;
    }
    #pane-preview {
        height: 1fr;
        background: $panel;
        color: $text;
        border: none;
        padding: 0 1;
        /* Vertical scrollbar only; horizontal hidden because terminal
           output is line-wrapped or accepted as-is. */
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        scrollbar-color: #d6b86a;
        scrollbar-background: $surface;
        scrollbar-color-hover: $amber;
        scrollbar-background-hover: $surface;
        scrollbar-color-active: $amber;
        scrollbar-background-active: $surface;
    }

    /* Footer: three equal-width slots — utilities on the left, attention
       chip centered, brand tag on the right. */
    #breadcrumb-bar {
        height: 1;
        padding: 0 2;
        layout: horizontal;
        background: $panel;
    }
    #utility-actions {
        width: 1fr;
        layout: horizontal;
        height: 1;
        align: left middle;
    }
    #attention-slot {
        width: 1fr;
        layout: horizontal;
        height: 1;
        align: center middle;
    }
    #brand-slot {
        width: 1fr;
        layout: horizontal;
        height: 1;
        align: right middle;
    }
    #brand-tag {
        width: auto;
        content-align: right middle;
    }

    /* ClickCell tier ladder. */
    ClickCell {
        height: 1;
        min-width: 0;
        width: auto;
        margin: 0 1 0 0;
        padding: 0 1;
        background: transparent;
        color: $muted;
        border: none;
    }
    ClickCell:last-of-type { margin: 0; }
    /* Hover/focus brighten ONLY for command buttons (action affordance).
       Nav cells (sessions / windows / panes) skip this so the mouse passing
       over them on the way to a click doesn't flash them white. */
    ClickCell.command-cell:hover,
    ClickCell.command-cell:focus { background: $raised; color: $text; }
    ClickCell:disabled { color: $faint; background: transparent; }
    .nav-cell.active, .session-pill.active {
        background: $amber-soft;
        color: $amber;
        text-style: bold;
    }
    .command-cell.primary {
        background: $amber-soft;
        color: $amber;
        text-style: bold;
    }
    .command-cell.primary:hover { background: $amber-soft; color: $amber; }
    .command-cell.danger:hover, .nav-cell.danger:hover {
        background: $red-soft;
        color: $red;
    }

    /* Window cards: 2-line, cross-session. */
    .window-card {
        height: 1;
        min-width: 22;
        width: auto;
        margin: 0 1 0 0;
        padding: 0 1;
        background: $surface;
        color: $muted;
    }
    /* No hover-brighten on window cards either — same flash-on-mouse-travel
       issue; selection state is the only thing that should change a card's
       look. */
    .window-card.active {
        background: $amber-soft;
        color: $amber;
        text-style: bold;
    }
    .window-card.waiting,
    .pane-chip.waiting,
    .session-pill.waiting { color: $amber; }

    .tooltip { display: none; }

    /* Toast notifications styled with the cockpit's amber accent. */
    Toast {
        background: $surface;
        color: $text;
        border-left: outer $amber;
    }
    Toast.-information {
        border-left: outer $amber;
    }
    Toast.-information .toast--title {
        color: $amber;
    }
    Toast.-warning {
        border-left: outer $amber;
    }
    Toast.-warning .toast--title {
        color: $amber;
    }
    Toast.-error {
        border-left: outer $red;
    }
    Toast.-error .toast--title {
        color: $red;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False, priority=True),
        Binding("question_mark", "help", "Help", key_display="?", show=False, priority=True),
        Binding("a", "attach", "Attach", show=False, priority=True),
        Binding("s", "send_command", "Send Msg", show=False, priority=True),
        Binding("h", "pane_history", "History", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.backend = TmuxBackend()
        self.git = GitBackend()
        self._worktree_windows: dict[str, tuple[str, str]] = {}  # window_id → (branch, status)
        self._preview = PanePreview()
        self._sessions: list[Session] = []
        self._selected_session_id: str = ""
        self._selected_window_id: str = ""
        self._selected_pane_id: str = ""
        self._preview_mode: str = "window"
        self._session_count = 0
        self._window_count = 0
        self._pane_count = 0
        self._selection_kind: str = ""  # "session", "window", "pane", or ""
        self._search_filter: str = ""  # current search/filter string
        self._syncing_click_widgets = False
        self._spinner_frame: int = 0
        self._spinner_targets: dict[str, str] = {}
        self._render_lock = asyncio.Lock()
        # Per-rail fingerprints — skip remount when content didn't change.
        self._rail_fingerprints: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="tmuxx-board"):
            with Vertical(id="cockpit-frame"):
                with Vertical(id="sessions-section"):
                    yield Horizontal(id="session-actions")
                    yield Horizontal(id="session-rail")
                with Vertical(id="windows-section"):
                    yield Horizontal(id="window-actions")
                    yield Horizontal(id="window-rail")
                with Vertical(id="panes-section"):
                    yield Horizontal(id="pane-actions")
                    yield Horizontal(id="pane-rail")
            with Vertical(id="preview-panel"):
                yield self._preview
            with Horizontal(id="breadcrumb-bar"):
                yield Horizontal(id="utility-actions")
                with Horizontal(id="attention-slot"):
                    yield Static("", id="attention-banner")
                with Horizontal(id="brand-slot"):
                    yield Static(
                        f"[#e0b148]TMUXX[/] [#8da095]v{_package_version()}[/]",
                        id="brand-tag",
                    )


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
        self.set_interval(0.1, self._tick_spinner)


    def _command_button(
        self,
        label: str,
        action: str,
        *,
        classes: str = "",
        disabled: bool = False,
        id: str | None = None,
    ) -> ClickCell:
        css = f"command-cell {classes}".strip()
        return ClickCell(label, id=id, action=action, classes=css, disabled=disabled)

    def _nav_button(
        self,
        label: str,
        *,
        id: str,
        classes: str = "",
        disabled: bool = False,
        target_kind: str,
        target_id: str,
    ) -> ClickCell:
        css = f"nav-cell {classes}".strip()
        return ClickCell(
            label,
            id=id,
            classes=css,
            target_kind=target_kind,
            target_id=target_id,
            disabled=disabled,
        )

    _SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    # Family-specific glyph color (only families with confirmed terminal
    # behavior get a tint; others fall through to the generic agent style).
    _AGENT_FAMILY_COLOR = {
        "claude": "#77b9cd",  # cyan
        "codex":  "#c084fc",  # magenta
    }

    # Detection: claude renames its foreground process to its version
    # (e.g. "2.1.128"); codex runs as `node` but always sets a pane title
    # of the form "<session> · <task> · codex-<uuid>".
    _CLAUDE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[.-].+)?$")
    _CODEX_TITLE_RE = re.compile(r"\bcodex-[0-9a-f]{4,}", re.IGNORECASE)

    @classmethod
    def _agent_family(cls, cmd: str, title: str = "") -> str | None:
        """Return the agent family for a pane, looking at both command and title."""
        if cmd:
            first = os.path.basename(cmd.strip().split()[0]).lower()
            if first.startswith("claude"):
                return "claude"
            if first.startswith("codex"):
                return "codex"
            if first.startswith(("gemini", "copilot", "gh-copilot", "aider")):
                return first.split("-")[0]
            # claude renames its process to its version number.
            if cls._CLAUDE_VERSION_RE.match(first):
                return "claude"
        # Codex installed via npm runs under `node`; identify via the
        # session-* · codex-<uuid> title that codex always sets.
        if title and cls._CODEX_TITLE_RE.search(title):
            return "codex"
        return None

    @classmethod
    def _is_agent_command(cls, cmd: str, title: str = "") -> bool:
        return cls._agent_family(cmd, title) is not None

    def _agent_badge(self, cmd: str, title: str = "") -> str:
        """🤖 prefix on agent panes. Empty when not a recognized agent.

        Emoji renderers ignore foreground color overrides, so the
        family hint comes from the colored command name in the chip
        label instead (see `_agent_command_color`).
        """
        if self._agent_family(cmd, title) is None:
            return ""
        return "🤖"

    def _agent_command_color(self, cmd: str, title: str = "") -> str | None:
        """Color hex for the pane's command-name text, by agent family.

        Returns None for non-agent panes so the default styling is kept.
        """
        family = self._agent_family(cmd, title)
        if not family:
            return None
        return self._AGENT_FAMILY_COLOR.get(family)

    # Patterns for extracting per-family runtime info from pane capture.
    _CLAUDE_TOKEN_RE = re.compile(
        r"\b(\d+(?:\.\d+)?)\s*([kKmM])?\s*tokens?\b"
    )
    _CODEX_MODE_RE = re.compile(
        r"\b(suggest|auto[- ]?edit|full[- ]?auto|read[- ]?only)\b",
        re.IGNORECASE,
    )

    def _agent_runtime_info(self, family: str | None, recent_output: str) -> str:
        """Family-specific quick-info badge from a pane's recent capture.

        Returns a short string ("645k", "auto-edit") or "" if nothing matched.
        """
        if not family or not recent_output:
            return ""
        # Scan the most recent ~50 lines so we don't waste regex on the whole
        # backlog and pick stale numbers from earlier in the session.
        tail = "\n".join(recent_output.splitlines()[-50:])
        if family == "claude":
            m = None
            for m in self._CLAUDE_TOKEN_RE.finditer(tail):
                pass  # take the LAST match — most recent number
            if m:
                num, suffix = m.group(1), (m.group(2) or "").lower()
                return f"{num}{suffix}" if suffix else f"{num}"
        elif family == "codex":
            m = None
            for m in self._CODEX_MODE_RE.finditer(tail):
                pass
            if m:
                return m.group(1).lower().replace(" ", "-")
        return ""

    def _spinner_char(self) -> str:
        return self._SPINNER_FRAMES[self._spinner_frame % len(self._SPINNER_FRAMES)]

    # An agent pane is "thinking" only when output was produced very recently.
    # Otherwise the pane is at its own prompt waiting for the user — same
    # tmux status ("running" because no shell prompt visible) but should
    # NOT animate a spinner.
    _AGENT_THINKING_WINDOW_SEC = 3

    def _status_glyph(
        self,
        status: str,
        command: str = "",
        title: str = "",
        activity: int = 0,
    ) -> str:
        # Only render a glyph when there's something the user should notice.
        # Plain "running" (shell prompt absent) and "idle" are the default —
        # showing a dot for them everywhere drowns out the signals that matter.
        if status == "running" and self._is_agent_command(command, title):
            import time
            if activity and (time.time() - activity) < self._AGENT_THINKING_WINDOW_SEC:
                return "{SPIN}"
            # Agent at prompt, not actively producing — no glyph.
            return ""
        if status == "waiting_for_input":
            return "[#e0b148 blink]◉[/]"
        if status == "error":
            return "[#d97959]✕[/]"
        return ""

    def _tick_spinner(self) -> None:
        if not self._spinner_targets:
            return
        self._spinner_frame = (self._spinner_frame + 1) % len(self._SPINNER_FRAMES)
        char = f"[#77b9cd]{self._spinner_char()}[/]"
        for widget_id, template in list(self._spinner_targets.items()):
            try:
                w = self.query_one(f"#{widget_id}", ClickCell)
            except Exception:
                self._spinner_targets.pop(widget_id, None)
                continue
            w.update(template.replace("{SPIN}", char))

    def _find_session(self, session_id: str | None = None) -> Session | None:
        target = session_id if session_id is not None else self._selected_session_id
        if target:
            for sess in self._sessions:
                if sess.session_id == target or sess.name == target:
                    return sess
        return None

    def _find_window(self, window_id: str | None = None) -> tuple[Window, Session] | tuple[None, None]:
        target = window_id if window_id is not None else self._selected_window_id
        if target:
            for sess in self._sessions:
                for win in sess.windows:
                    if win.window_id == target:
                        return win, sess
        return None, None

    def _find_pane(self, pane_id: str | None = None) -> tuple[Pane, Window, Session] | tuple[None, None, None]:
        target = pane_id if pane_id is not None else self._selected_pane_id
        if target:
            for sess in self._sessions:
                for win in sess.windows:
                    for pane in win.panes:
                        if pane.pane_id == target:
                            return pane, win, sess
        return None, None, None

    def _get_selected_session(self) -> Session | None:
        sess = self._find_session()
        if sess:
            return sess
        win, win_sess = self._find_window()
        if win and win_sess:
            return win_sess
        pane, _win, pane_sess = self._find_pane()
        if pane and pane_sess:
            return pane_sess
        return None

    def _get_selected_window(self) -> Window | None:
        win, _sess = self._find_window()
        if win:
            return win
        pane, pane_win, _sess = self._find_pane()
        if pane and pane_win:
            return pane_win
        sess = self._get_selected_session()
        if sess and sess.windows:
            active = next((w for w in sess.windows if w.active), None)
            return active or sess.windows[0]
        return None

    def _get_selected_pane(self) -> Pane | None:
        pane, _win, _sess = self._find_pane()
        if pane:
            return pane
        win = self._get_selected_window()
        if win and win.panes:
            active = next((p for p in win.panes if p.active), None)
            return active or win.panes[0]
        return None

    def _get_selected_pane_id(self) -> str | None:
        pane = self._get_selected_pane()
        return pane.pane_id if pane else None

    def _get_selected_data(self):
        sess = self._get_selected_session()
        win = self._get_selected_window()
        pane = self._get_selected_pane()
        if self._selection_kind == "pane" and pane and win and sess:
            return ("pane", pane, win, sess)
        if self._selection_kind == "session" and sess:
            return ("session", sess)
        if win and sess:
            return ("window", win, sess)
        if sess:
            return ("session", sess)
        return None

    def _reconcile_selection(self) -> None:
        if not self._sessions:
            self._selected_session_id = ""
            self._selected_window_id = ""
            self._selected_pane_id = ""
            self._selection_kind = ""
            self._preview_mode = "home"
            return

        sess = self._find_session()
        if not sess:
            sess = next((s for s in self._sessions if s.attached), None) or self._sessions[0]
            self._selected_session_id = sess.session_id

        win, _ = self._find_window()
        if not win or win not in sess.windows:
            win = next((w for w in sess.windows if w.active), None)
            win = win or (sess.windows[0] if sess.windows else None)
            self._selected_window_id = win.window_id if win else ""

        if win:
            pane, pane_win, _ = self._find_pane()
            if not pane or pane_win is not win:
                pane = next((p for p in win.panes if p.active), None)
                pane = pane or (win.panes[0] if win.panes else None)
                self._selected_pane_id = pane.pane_id if pane else ""
        else:
            self._selected_pane_id = ""

        if not self._selection_kind:
            self._selection_kind = "window" if self._selected_window_id else "session"
        if self._preview_mode not in {"home", "window", "pane", "session"}:
            self._preview_mode = "window"
        if self._preview_mode == "pane" and not self._selected_pane_id:
            self._preview_mode = "window"

    @staticmethod
    def _compact_path(path: str, max_len: int = 36) -> str:
        if not path:
            return ""
        home = os.path.expanduser("~")
        compact = path.replace(home, "~", 1) if path.startswith(home) else path
        if len(compact) <= max_len:
            return compact
        keep = max(8, max_len - 3)
        return "..." + compact[-keep:]

    def _target_from_tab_id(self, prefix: str, tab_id: str | None) -> str:
        if not tab_id:
            return ""
        if prefix == "session":
            for sess in self._sessions:
                if _tmux_widget_id(prefix, sess.session_id) == tab_id:
                    return sess.session_id
        if prefix == "window":
            for sess in self._sessions:
                for win in sess.windows:
                    if _tmux_widget_id(prefix, win.window_id) == tab_id:
                        return win.window_id
        if prefix == "pane":
            for sess in self._sessions:
                for win in sess.windows:
                    for pane in win.panes:
                        if _tmux_widget_id(prefix, pane.pane_id) == tab_id:
                            return pane.pane_id
        return ""

    # Strip rotating spinner characters so they don't perturb the rail
    # fingerprint — the spinner advances on its own timer in place.
    _SPINNER_CHARS_RE = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]")

    @classmethod
    def _label_fingerprint(cls, w) -> str:
        text = getattr(w, "label_text", "")
        return cls._SPINNER_CHARS_RE.sub("·", text)

    async def _replace_rail(
        self,
        rail: Horizontal,
        widgets: list[ClickCell | Static],
        empty_label: str,
    ) -> None:
        # Fingerprint based on widget IDs + label text (spinner-stripped).
        # If unchanged since last call, skip the remove/mount cycle entirely
        # so timer-driven refreshes don't visibly flash the rail.
        fp_key = rail.id or str(id(rail))
        fp = "|".join(
            f"{getattr(w, 'id', '')}:{self._label_fingerprint(w)}"
            for w in widgets
        ) or f"empty:{empty_label}"
        if self._rail_fingerprints.get(fp_key) == fp:
            return
        self._rail_fingerprints[fp_key] = fp
        await rail.remove_children()
        if widgets:
            await rail.mount(*widgets)
        else:
            await rail.mount(Static(empty_label, classes="context-label"))

    async def _replace_cells(self, container: Horizontal, cells: list[ClickCell]) -> None:
        # Same fingerprint trick for action clusters.
        fp_key = container.id or id(container)
        fp = "|".join(f"{c.label_text}:{c.disabled}:{','.join(c.classes)}" for c in cells)
        if self._rail_fingerprints.get(fp_key) == fp:
            return
        self._rail_fingerprints[fp_key] = fp
        await container.remove_children()
        await container.mount(*cells)

    async def _render_click_layers(self) -> None:
        async with self._render_lock:
            await self._render_click_layers_locked()

    async def _render_click_layers_locked(self) -> None:
        try:
            session_rail = self.query_one("#session-rail", Horizontal)
            session_actions = self.query_one("#session-actions", Horizontal)
            window_actions = self.query_one("#window-actions", Horizontal)
            window_rail = self.query_one("#window-rail", Horizontal)
            pane_rail = self.query_one("#pane-rail", Horizontal)
            pane_actions = self.query_one("#pane-actions", Horizontal)
            utility_actions = self.query_one("#utility-actions", Horizontal)
            attention_banner = self.query_one("#attention-banner", Static)
        except Exception:
            return

        self._syncing_click_widgets = True
        self._spinner_targets.clear()
        try:
            sess = self._get_selected_session()
            win = self._get_selected_window()
            pane = self._get_selected_pane()

            # ── Attention banner ───────────────────────────
            waiting_panes: list[tuple[Session, Window, Pane]] = [
                (s, w, p)
                for s in self._sessions
                for w in s.windows
                for p in w.panes
                if p.status == "waiting_for_input"
            ]
            if waiting_panes:
                count = len(waiting_panes)
                phrase = "1 pane needs input" if count == 1 else f"{count} panes need input"
                summary = " · ".join(
                    f"{escape(s.name)}/{w.window_index} {escape(w.name)}"
                    for s, w, _ in waiting_panes[:3]
                )
                if count > 3:
                    summary += f" · +{count - 3} more"
                attention_banner.update(
                    f"[#e0b148 blink]◉[/] {phrase} · {summary}"
                )
                attention_banner.add_class("alerting")
            else:
                attention_banner.update("")
                attention_banner.remove_class("alerting")

            # ── Sessions rail ───────────────────────────────
            session_widgets: list[ClickCell] = []
            for item in self._sessions:
                total_panes = sum(len(w.panes) for w in item.windows)
                attach_glyph = "[#dce8df]▸[/]" if item.attached else ""
                active = item.session_id == self._selected_session_id
                # Sessions stay glyph-free — only pane chips carry status info.
                label = f"{escape(item.name)} {attach_glyph} [#5b6e64]{len(item.windows)}w/{total_panes}p[/]"
                wid = _tmux_widget_id("session", item.session_id)
                session_widgets.append(
                    self._nav_button(
                        label,
                        id=wid,
                        classes=f"session-pill {'active' if active else ''}".strip(),
                        target_kind="session",
                        target_id=item.session_id,
                    )
                )
            await self._replace_rail(session_rail, session_widgets, "no sessions")

            # Attach belongs to the window row only — sessions just create /
            # rename / kill. Press `a` for window attach from anywhere.
            await self._replace_cells(session_actions, [
                self._command_button("+ Session", "new_session", classes="primary"),
                self._command_button("Rename", "rename", disabled=sess is None),
                self._command_button("Kill", "kill_selected", classes="danger", disabled=sess is None),
            ])

            # ── Windows section: cards from ALL sessions ───
            window_widgets: list[ClickCell] = []
            q = self._search_filter.lower() if self._search_filter else ""
            for owner_sess in self._sessions:
                for item in owner_sess.windows:
                    if q:
                        haystack = " ".join([
                            owner_sess.name, item.name,
                            *(p.current_command for p in item.panes),
                            *(p.worktree_branch for p in item.panes),
                        ]).lower()
                        if q not in haystack:
                            continue
                    # Match selection regardless of preview_mode so the active
                    # window card stays highlighted while drilling into a pane.
                    active = (
                        item.window_id == self._selected_window_id
                        and owner_sess.session_id == self._selected_session_id
                    )
                    # Single-line card; status info is shown only on pane chips.
                    # Worktree branch (when present) tags the end of the line.
                    # "/" stays muted so the active state's amber doesn't
                    # tint the separator. No spaces around it for compactness.
                    label = (
                        f"[#5b6e64]{escape(owner_sess.name)}[/]"
                        f"[#8da095]/[/]"
                        f"{item.window_index} [bold]{escape(item.name)}[/]"
                    )
                    wt = self._worktree_windows.get(item.window_id)
                    if wt:
                        branch, _wt_status = wt
                        label += f" [#5b6e64]⎇ {branch}[/]"
                    wid = _tmux_widget_id("window", item.window_id)
                    klass = "window-card"
                    if item.status == "waiting_for_input":
                        klass += " waiting"
                    if active:
                        klass += " active"
                    window_widgets.append(
                        self._nav_button(
                            label,
                            id=wid,
                            classes=klass,
                            target_kind="window",
                            target_id=item.window_id,
                        )
                    )
            await self._replace_rail(window_rail, window_widgets, "no windows")

            await self._replace_cells(window_actions, [
                self._command_button("+ Window", "new_window", classes="primary", disabled=sess is None),
                self._command_button("Rename", "rename", disabled=win is None),
                # Attach is always clickable — handler resolves the active
                # window of the selected session at fire time, so it works
                # even when no specific window was clicked yet.
                self._command_button("[#e0b148]A[/]ttach", "attach"),
                self._command_button("Kill", "kill_selected", classes="danger", disabled=win is None),
            ])

            # ── Panes rail ─────────────────────────────────
            # ── Panes rail: ALL panes from ALL windows of ALL sessions ──
            # Honors the same search filter as the window rail.
            pane_widgets: list[ClickCell] = []
            for owner_sess in self._sessions:
                for owner_win in owner_sess.windows:
                    if q:
                        haystack = " ".join([
                            owner_sess.name, owner_win.name,
                            *(p.current_command for p in owner_win.panes),
                            *(p.worktree_branch for p in owner_win.panes),
                        ]).lower()
                        if q not in haystack:
                            continue
                    for item in owner_win.panes:
                        glyph = self._status_glyph(
                            item.status, item.current_command, item.pane_title, item.activity
                        )
                        # Highlight every pane currently inside the preview:
                        #  - preview_mode "pane"    → only the matching chip
                        #  - preview_mode "window"  → every pane in the window
                        #  - preview_mode "session" → every pane in the session
                        if self._preview_mode == "pane":
                            active = item.pane_id == self._selected_pane_id
                        elif self._preview_mode == "window":
                            active = (
                                owner_win.window_id == self._selected_window_id
                                and owner_sess.session_id == self._selected_session_id
                            )
                        elif self._preview_mode == "session":
                            active = owner_sess.session_id == self._selected_session_id
                        else:
                            active = False
                        # Agent panes get a 🤖 prefix and a family-tinted
                        # window-name prefix (cyan claude / magenta codex).
                        # Command name and runtime info keep neutral styling.
                        badge = self._agent_badge(item.current_command, item.pane_title)
                        family = self._agent_family(item.current_command, item.pane_title)
                        prefix_color = self._agent_command_color(item.current_command, item.pane_title) or "#5b6e64"
                        runtime = self._agent_runtime_info(family, item.recent_output)
                        runtime_part = f" [#8da095]{escape(runtime)}[/]" if runtime else ""
                        # Wrap "/" in an explicit muted color so the active
                        # state (which paints everything amber) doesn't
                        # tint the separator.
                        label = (
                            f"{badge}[{prefix_color}]{escape(owner_win.name)}[/]"
                            f"[#8da095]/[/]"
                            f"{item.pane_id} [bold]{escape(item.current_command)}[/]"
                            f"{runtime_part} {glyph}"
                        )
                        wid = _tmux_widget_id("pane", item.pane_id)
                        if "{SPIN}" in label:
                            self._spinner_targets[wid] = label
                            label = label.replace("{SPIN}", f"[#77b9cd]{self._spinner_char()}[/]")
                        klass = "pane-chip"
                        if item.status == "waiting_for_input":
                            klass += " waiting"
                        if active:
                            klass += " active"
                        pane_widgets.append(
                            self._nav_button(
                                label,
                                id=wid,
                                classes=klass,
                                target_kind="pane",
                                target_id=item.pane_id,
                            )
                        )
            await self._replace_rail(pane_rail, pane_widgets, "no panes")

            # Pane row drops Attach (window-scope only) — split / send / kill.
            await self._replace_cells(pane_actions, [
                self._command_button("+ Pane H", "split_h", classes="primary", disabled=pane is None),
                self._command_button("+ Pane V", "split_v", classes="primary", disabled=pane is None),
                self._command_button("[#e0b148]S[/]end Msg", "send_command", disabled=pane is None),
                self._command_button("[#e0b148]H[/]istory", "pane_history", disabled=pane is None),
                self._command_button("Kill", "kill_selected", classes="danger", disabled=pane is None),
            ])

            # ── Utility actions in footer ──────────────────
            search_label = (
                f"Search: {self._search_filter}"
                if self._search_filter
                else "Search"
            )
            search_classes = "primary" if self._search_filter else ""
            home_active = self._preview_mode == "home"
            await self._replace_cells(utility_actions, [
                self._command_button(
                    "Home",
                    "home",
                    classes="primary" if home_active else "",
                ),
                self._command_button("Refresh", "force_refresh"),
                self._command_button(search_label, "search", classes=search_classes),
                self._command_button("Copy", "copy_preview"),
                self._command_button("Skill", "cli_intro"),
                self._command_button("Help", "help"),
                self._command_button("[#e0b148]Q[/]uit", "quit"),
            ])

        finally:
            self._syncing_click_widgets = False

    def select_click_target(self, kind: str, target_id: str) -> None:
        old_window = self._selected_window_id
        if kind == "session":
            self._selected_session_id = target_id
            self._selected_window_id = ""
            self._selected_pane_id = ""
            self._selection_kind = "session"
            self._preview_mode = "window"
        elif kind == "window":
            win, sess = self._find_window(target_id)
            if win and sess:
                self._selected_session_id = sess.session_id
                self._selected_window_id = win.window_id
                active = next((p for p in win.panes if p.active), None)
                self._selected_pane_id = (active or win.panes[0]).pane_id if win.panes else ""
                self._selection_kind = "window"
                self._preview_mode = "window"
        elif kind == "pane":
            pane, win, sess = self._find_pane(target_id)
            if pane and win and sess:
                self._selected_session_id = sess.session_id
                self._selected_window_id = win.window_id
                self._selected_pane_id = pane.pane_id
                self._selection_kind = "pane"
                self._preview_mode = "pane"
        self._reconcile_selection()
        self.refresh_bindings()

        # Click-driven selection changes never alter the hierarchy contents,
        # only which cells should look active. Toggle classes in place — no
        # remove/mount cycle, no flash on unrelated rails.
        self._apply_active_classes()
        self._mark_home_active(False)
        self._refresh_preview_only()

    def _apply_active_classes(self) -> None:
        """Toggle .active on existing widgets without rebuilding the rails."""
        # For pane chips, highlight every pane inside the current preview, not
        # just the literal selection — same logic the full renderer uses.
        sel_window_panes: set[str] = set()
        sel_session_panes: set[str] = set()
        if self._preview_mode in {"window", "session"}:
            for s in self._sessions:
                if s.session_id != self._selected_session_id:
                    continue
                for w in s.windows:
                    for p in w.panes:
                        sel_session_panes.add(p.pane_id)
                        if w.window_id == self._selected_window_id:
                            sel_window_panes.add(p.pane_id)

        for cell in self.query(ClickCell):
            kind = getattr(cell, "_target_kind", None)
            tid = getattr(cell, "_target_id", None)
            if not kind or not tid:
                continue
            if kind == "session":
                want = tid == self._selected_session_id
            elif kind == "window":
                want = tid == self._selected_window_id
            elif kind == "pane":
                if self._preview_mode == "pane":
                    want = tid == self._selected_pane_id
                elif self._preview_mode == "window":
                    want = tid in sel_window_panes
                elif self._preview_mode == "session":
                    want = tid in sel_session_panes
                else:
                    want = False
            else:
                continue
            if want:
                cell.add_class("active")
            else:
                cell.remove_class("active")

    @work(exclusive=True, group="preview")
    async def _refresh_preview_only(self) -> None:
        await self._update_preview()

    @work(exclusive=True, group="selection")
    async def _refresh_selection_view(self) -> None:
        await self._render_click_layers()
        await self._update_preview()


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

        # Search filter is no longer applied to the source hierarchy here —
        # the renderer applies it to the window-rail only, so sessions and
        # panes stay navigable while you narrow the windows view.

        # Count totals
        self._session_count = len(sessions)
        self._window_count = sum(len(s.windows) for s in sessions)
        self._pane_count = sum(
            len(w.panes) for s in sessions for w in s.windows
        )

        self._sessions = sessions
        self._reconcile_selection()
        self.refresh_bindings()
        await self._render_click_layers()

        await self._update_preview()

    @work(exclusive=True)
    async def refresh_data(self) -> None:
        """Timer-driven refresh (runs as background worker)."""
        await self._do_refresh()

    async def _update_preview(self) -> None:
        if self._preview_mode == "home":
            self._preview.clear_preview()
            self._scroll_preview_to_bottom()
            return

        if not self._sessions:
            self._preview.set_message("No tmux sessions found")
            self._scroll_preview_to_bottom()
            return

        data = self._get_selected_data()
        if not data:
            self._preview.clear_preview()
            self._scroll_preview_to_bottom()
            return

        kind = "pane" if self._preview_mode == "pane" else data[0]

        if kind == "pane":
            pane = self._get_selected_pane()
            if pane:
                await self._show_pane_preview(pane)
            else:
                self._preview.set_message("No pane selected")
        elif kind == "worktree":
            pane = data[1]
            await self._show_pane_preview(pane)
        elif kind == "window":
            win = self._get_selected_window()
            if win:
                await self._show_window_preview(win)
            else:
                self._preview.set_message("No window selected")
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
        data = self._get_selected_data()
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

        # Session or window/pane
        if action == "new_window":
            return k in ("session", "window", "pane")
        if action == "kill_selected":
            return k in ("session", "window", "pane")
        if action == "rename":
            return k in ("session", "window")

        # Attach now resolves to the session's active window when no
        # specific window/pane is selected, so it's valid in session
        # scope too.
        if action == "attach":
            return k in ("session", "window", "pane")

        # Window or pane only
        if action in ("split_h", "split_v", "activate", "copy_preview", "resize", "send_command"):
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
        sess = self._get_selected_session()
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
        pane_id = self._get_selected_pane_id()
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
        pane_id = self._get_selected_pane_id()
        if not pane_id:
            self.notify("Select a window or pane first", severity="warning")
            return
        try:
            await self.backend.split_pane(pane_id, horizontal=False)
        except Exception as e:
            self.notify(f"Split failed: {e}", severity="error")
            return
        await self._do_refresh()

    def action_home(self) -> None:
        self._preview_mode = "home"
        self._selection_kind = "session" if self._get_selected_session() else ""
        # Just swap the preview body; rails don't need to rebuild.
        self._apply_active_classes()
        self._mark_home_active(True)
        self._refresh_preview_only()

    def _mark_home_active(self, active: bool) -> None:
        for cell in self.query("#utility-actions .command-cell"):
            if cell.label_text == "Home":
                if active:
                    cell.add_class("primary")
                else:
                    cell.remove_class("primary")
                break

    def action_rename_session(self) -> None:
        sess = self._get_selected_session()
        if not sess:
            self.notify("Select a session first", severity="warning")
            return
        self._rename_pending = ("session", sess.name)
        self.push_screen(
            InputModal("Rename session:", initial=sess.name),
            callback=self._on_rename,
        )

    def action_rename_window(self) -> None:
        win = self._get_selected_window()
        if not win:
            self.notify("Select a window first", severity="warning")
            return
        self._rename_pending = ("window", win.window_id, win.name)
        self.push_screen(
            InputModal("Rename window:", initial=win.name),
            callback=self._on_rename,
        )

    def action_kill_session(self) -> None:
        sess = self._get_selected_session()
        if not sess:
            self.notify("Select a session first", severity="warning")
            return
        is_last = self._session_count <= 1
        msg = f"Kill session '{sess.name}'?"
        if is_last:
            msg += "\nThis is the last session — tmux server will exit."
        self._kill_pending = ("session", sess.name, is_last)
        self.push_screen(ConfirmModal(msg), callback=self._on_kill_confirm)

    def action_kill_window(self) -> None:
        win = self._get_selected_window()
        if not win:
            self.notify("Select a window first", severity="warning")
            return
        self._kill_pending = ("window", win.window_id, False)
        self.push_screen(
            ConfirmModal(f"Kill window '{win.name}' ({win.window_id})?"),
            callback=self._on_kill_confirm,
        )

    def action_kill_pane(self) -> None:
        pane = self._get_selected_pane()
        if not pane:
            self.notify("Select a pane first", severity="warning")
            return
        self._kill_pending = ("pane", pane.pane_id, False)
        self.push_screen(
            ConfirmModal(f"Kill pane {pane.pane_id}?"),
            callback=self._on_kill_confirm,
        )

    def action_kill_selected(self) -> None:
        data = self._get_selected_data()
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
        data = self._get_selected_data()
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
        self.notify("Tree sidebar has been replaced by click navigation", severity="information")

    def action_panel_resize(self, direction: str) -> None:
        self.notify("Panel resizing is not used in the click layout", severity="information")

    async def _attach_target(
        self,
        sess_name: str,
        *,
        window_id: str | None = None,
        pane_id: str | None = None,
    ) -> None:
        """Switch the tmux client to the most specific target available.

        Prefers `pane_id` > `window_id` > `sess_name`. Targeting by
        window or pane id is atomic — it selects the right scope AND
        switches the session in one call.
        """
        _install_tmux_integration()
        target = pane_id or window_id or sess_name
        if os.environ.get("TMUX"):
            try:
                await self.backend._run("tmux", "switch-client", "-t", target)
            except Exception as e:
                self.notify(f"switch-client failed: {e}", severity="error", timeout=8)
            return
        with self.suspend():
            rc = subprocess.run(["tmux", "attach-session", "-t", target]).returncode
        if rc != 0:
            self.notify(f"attach-session failed (rc={rc})", severity="error", timeout=8)

    # Back-compat shim — older callers still pass session name only.
    async def _attach_session_name(self, sess_name: str) -> None:
        await self._attach_target(sess_name)

    async def action_attach_window(self) -> None:
        # Fall back to the active window in the selected session if no
        # specific window was clicked. tmux always has an "active" window
        # per session, and that's what the cockpit visually highlights —
        # so attach should always work even from a session-only selection.
        win = self._get_selected_window()
        sess = self._get_selected_session()
        if not win or not sess:
            self.notify("No session available to attach", severity="warning")
            return
        await self._attach_target(sess.name, window_id=win.window_id)

    async def action_attach_pane(self) -> None:
        pane, win, sess = self._find_pane(self._selected_pane_id)
        if not pane or not sess:
            self.notify("Select a pane first", severity="warning")
            return
        await self._attach_target(
            sess.name,
            window_id=win.window_id if win else None,
            pane_id=pane.pane_id,
        )

    async def action_attach(self) -> None:
        # `a` always attaches: pick the most specific scope already
        # selected (pane > window > session-active-window) and land
        # there. No more "select a window first" dead end.
        await self.action_attach_window()

    async def action_activate(self) -> None:
        data = self._get_selected_data()
        if not data:
            return

        kind = data[0]
        try:
            if kind == "window":
                win: Window = data[1]
                await self.backend.select_window(win.window_id)
                pane_id = self._get_selected_pane_id()
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
        pane_id = self._get_selected_pane_id()
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

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_cli_intro(self) -> None:
        self.push_screen(CliIntroModal())

    @work
    async def action_pane_history(self) -> None:
        pane_id = self._get_selected_pane_id()
        if not pane_id:
            self.notify("Select a pane first", severity="warning")
            return
        try:
            content = await self.backend.capture_pane(pane_id, lines=3000)
        except Exception as e:
            self.notify(f"Capture failed: {e}", severity="error")
            return
        self.push_screen(HistoryModal(pane_id, content))

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
        pane_id = self._get_selected_pane_id()
        if not pane_id:
            self.notify("Select a window or pane first", severity="warning")
            return
        pane, win, sess = self._find_pane(pane_id)
        running = pane.current_command if pane else "?"
        # Build a maximally specific target string so user can verify what's
        # being sent where (avoids the "always sending to first pane"
        # confusion when a window had multiple panes and selection was
        # implicit).
        scope = f"{sess.name}/" if sess else ""
        scope += f"{win.name}/" if win else ""
        scope += pane_id
        self._send_cmd_pane = pane_id
        self.push_screen(
            InputModal(
                f"Send → {scope} (running: {running})",
                placeholder="text or shell command — Enter sends + Return",
            ),
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
            self.notify(f"Send to {pane_id} FAILED: {e}", severity="error", timeout=10)
            return
        # Echo what was sent so the user has confirmation even if the
        # destination program ate the keys silently.
        preview = cmd if len(cmd) <= 40 else cmd[:37] + "…"
        self.notify(f"Sent → {pane_id}: {preview}", timeout=5)
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
