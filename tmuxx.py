"""tmux-tui: Terminal UI for managing tmux sessions, windows, and panes."""

from __future__ import annotations

import asyncio
import argparse
import os
import re
import sys
from dataclasses import dataclass, field

from tmux_core import GitBackend

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
    Header,
    Input,
    Label,
    OptionList,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class Pane:
    pane_id: str
    pane_index: int
    width: int
    height: int
    current_command: str
    active: bool
    left: int = 0
    top: int = 0
    current_path: str = ""
    pid: int = 0


@dataclass
class Window:
    window_id: str
    window_index: int
    name: str
    active: bool
    panes: list[Pane] = field(default_factory=list)
    activity: int = 0


@dataclass
class Session:
    session_id: str
    name: str
    attached: bool
    windows: list[Window] = field(default_factory=list)
    created: int = 0
    activity: int = 0


# ── Tmux Backend ─────────────────────────────────────────────────────────────


class TmuxBackend:
    """Async interface to tmux CLI."""

    @staticmethod
    async def _run(cmd: str) -> str:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            message = stderr.decode().strip() or f"Command failed with exit {proc.returncode}"
            raise RuntimeError(message)
        return stdout.decode().strip()

    async def get_hierarchy(self) -> list[Session]:
        sessions_raw, windows_raw, panes_raw = await asyncio.gather(
            self._run(
                "tmux list-sessions -F '#{session_id}:#{session_name}:#{session_attached}:#{session_created}:#{session_activity}'"
            ),
            self._run(
                "tmux list-windows -a -F '#{session_id}:#{window_id}:#{window_index}:#{window_name}:#{window_active}:#{window_activity}'"
            ),
            self._run(
                "tmux list-panes -a -F '#{window_id}:#{pane_id}:#{pane_index}:#{pane_width}:#{pane_height}:#{pane_current_command}:#{pane_active}:#{pane_left}:#{pane_top}:#{pane_current_path}:#{pane_pid}'"
            ),
        )

        # Parse panes
        pane_map: dict[str, list[Pane]] = {}
        for line in panes_raw.splitlines():
            if not line:
                continue
            parts = line.split(":")
            if len(parts) < 9:
                continue
            win_id = parts[0]
            pane = Pane(
                pane_id=parts[1],
                pane_index=int(parts[2]),
                width=int(parts[3]),
                height=int(parts[4]),
                current_command=parts[5],
                active=parts[6] == "1",
                left=int(parts[7]),
                top=int(parts[8]),
                current_path=parts[9] if len(parts) > 9 else "",
                pid=int(parts[10] or 0) if len(parts) > 10 else 0,
            )
            pane_map.setdefault(win_id, []).append(pane)

        # Parse windows
        win_map: dict[str, list[Window]] = {}
        for line in windows_raw.splitlines():
            if not line:
                continue
            parts = line.split(":")
            if len(parts) < 5:
                continue
            sess_id = parts[0]
            win = Window(
                window_id=parts[1],
                window_index=int(parts[2]),
                name=parts[3],
                active=parts[4] == "1",
                panes=sorted(pane_map.get(parts[1], []), key=lambda p: p.pane_index),
                activity=int(parts[5] or 0) if len(parts) > 5 else 0,
            )
            win_map.setdefault(sess_id, []).append(win)

        # Parse sessions
        sessions: list[Session] = []
        for line in sessions_raw.splitlines():
            if not line:
                continue
            parts = line.split(":")
            if len(parts) < 5:
                continue
            sess = Session(
                session_id=parts[0],
                name=parts[1],
                attached=parts[2] == "1",
                windows=sorted(
                    win_map.get(parts[0], []), key=lambda w: w.window_index
                ),
                created=int(parts[3] or 0),
                activity=int(parts[4] or 0),
            )
            sessions.append(sess)
        return sorted(sessions, key=lambda s: s.name)

    async def capture_pane(self, pane_id: str, lines: int = 40) -> str:
        return await self._run(f"tmux capture-pane -t {pane_id} -e -p -S -{lines}")

    async def new_session(self, name: str) -> None:
        await self._run(f"tmux new-session -d -s {_q(name)}")

    async def kill_session(self, name: str) -> None:
        await self._run(f"tmux kill-session -t {_q(name)}")

    async def rename_session(self, old: str, new: str) -> None:
        await self._run(f"tmux rename-session -t {_q(old)} {_q(new)}")

    async def new_window(self, session: str, name: str | None = None) -> None:
        cmd = f"tmux new-window -t {_q(session)}"
        if name:
            cmd += f" -n {_q(name)}"
        await self._run(cmd)

    async def kill_window(self, window_id: str) -> None:
        await self._run(f"tmux kill-window -t {window_id}")

    async def rename_window(self, window_id: str, new_name: str) -> None:
        await self._run(f"tmux rename-window -t {window_id} {_q(new_name)}")

    async def split_pane(self, pane_id: str, horizontal: bool = False) -> None:
        flag = "-h" if horizontal else "-v"
        await self._run(f"tmux split-window {flag} -t {pane_id}")

    async def kill_pane(self, pane_id: str) -> None:
        await self._run(f"tmux kill-pane -t {pane_id}")

    async def send_keys(self, pane_id: str, keys: str) -> None:
        await self._run(f"tmux send-keys -t {pane_id} {_q(keys)} Enter")

    async def resize_pane(self, pane_id: str, direction: str, amount: int = 5) -> None:
        flag_map = {"up": "-U", "down": "-D", "left": "-L", "right": "-R"}
        flag = flag_map.get(direction, "-U")
        await self._run(f"tmux resize-pane -t {pane_id} {flag} {amount}")

    async def capture_window_panes(self, panes: list[Pane]) -> dict[str, str]:
        """Capture visible content of all panes in a window in parallel (with ANSI)."""
        async def _cap(p: Pane) -> tuple[str, str]:
            content = await self._run(
                f"tmux capture-pane -t {p.pane_id} -e -p"
            )
            return p.pane_id, content

        results = await asyncio.gather(*[_cap(p) for p in panes])
        return dict(results)

    async def select_window(self, window_id: str) -> None:
        await self._run(f"tmux select-window -t {window_id}")

    async def select_pane(self, pane_id: str) -> None:
        await self._run(f"tmux select-pane -t {pane_id}")

    async def new_window_in_dir(
        self, session: str, directory: str, name: str | None = None
    ) -> None:
        cmd = f"tmux new-window -t {_q(session)} -c {_q(directory)}"
        if name:
            cmd += f" -n {_q(name)}"
        await self._run(cmd)


def _q(s: str) -> str:
    """Shell-quote a string."""
    return "'" + s.replace("'", "'\\''") + "'"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


def _strip_ansi(text: str) -> str:
    """Strip all ANSI escape sequences for clean plain-text preview."""
    return _ANSI_RE.sub("", text)


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
        """Structural fingerprint — only changes when sessions/windows/panes are added/removed."""
        parts: list[str] = []
        for s in sessions:
            parts.append(s.session_id)
            for w in s.windows:
                parts.append(w.window_id)
                for p in w.panes:
                    parts.append(p.pane_id)
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

    def _full_rebuild(self) -> None:
        # Save selected node's tmux ID for cursor restore
        saved_id = self._get_cursor_tmux_id()
        c = self._resolve_colors()

        self.clear()
        if not self.sessions:
            # Add placeholder so tree is never truly empty (avoids Textual focus issues)
            self.root.add_leaf("[dim]No sessions — press [bold]n[/] to create one[/]", data=None)
            self.root.expand()
            return
        for sess in self.sessions:
            status = f" [{c['ok']}]attached[/]" if sess.attached else ""
            sess_label = f"[bold {c['session']}]{escape(sess.name)}[/]{status}"
            sess_node = self.root.add(sess_label, data=("session", sess))
            for win in sess.windows:
                win_status = f" [{c['active']}]●[/]" if win.active else ""
                wt_info = self.worktree_windows.get(win.window_id)
                wt_badge = f" [{c['ok']}]●[/]" if wt_info else ""
                win_label = (
                    f"[bold {c['window']}]{escape(win.name)}[/] "
                    f"[dim]:{win.window_index}[/]{win_status}{wt_badge}"
                )
                win_node = sess_node.add(win_label, data=("window", win, sess))
                for pane in win.panes:
                    pane_status = f" [{c['active']}]●[/]" if pane.active else ""
                    pane_label = (
                        f"[{c['pane']}]{escape(pane.current_command)}[/] "
                        f"[dim]{pane.pane_id} {pane.width}x{pane.height}[/]{pane_status}"
                    )
                    win_node.add_leaf(pane_label, data=("pane", pane, win, sess))
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
            status = f" [{c['ok']}]attached[/]" if sess.attached else ""
            sess_node.set_label(f"[bold {c['session']}]{escape(sess.name)}[/]{status}")
            sess_node.data = ("session", sess)
            win_idx = 0
            for win_node in sess_node.children:
                if win_idx >= len(sess.windows):
                    break
                win = sess.windows[win_idx]
                win_status = f" [{c['active']}]●[/]" if win.active else ""
                wt_info = self.worktree_windows.get(win.window_id)
                wt_badge = f" [{c['ok']}]●[/]" if wt_info else ""
                win_node.set_label(
                    f"[bold {c['window']}]{escape(win.name)}[/] [dim]:{win.window_index}[/]{win_status}{wt_badge}"
                )
                win_node.data = ("window", win, sess)
                pane_idx = 0
                for pane_node in win_node.children:
                    if pane_idx >= len(win.panes):
                        break
                    pane = win.panes[pane_idx]
                    pane_status = f" [{c['active']}]●[/]" if pane.active else ""
                    pane_node.set_label(
                        f"[{c['pane']}]{escape(pane.current_command)}[/] "
                        f"[dim]{pane.pane_id} {pane.width}x{pane.height}[/]{pane_status}"
                    )
                    pane_node.data = ("pane", pane, win, sess)
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


def compose_window_grid(panes: list[Pane], captured: dict[str, str], max_cols: int = 0) -> Text:
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
        styled = Text(_strip_ansi(raw))
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

    def _border_char(r: int, c: int) -> str:
        if cell_owner[r][c] is not None:
            return " "
        is_h = h_map[r][c]
        is_v = v_map[r][c]
        if not is_h and not is_v:
            return " "
        if is_h and not is_v:
            return "─"
        if is_v and not is_h:
            return "│"

        up = r > 0 and v_map[r - 1][c]
        down = r < grid_h - 1 and v_map[r + 1][c]
        left = c > 0 and h_map[r][c - 1]
        right = c < grid_w - 1 and h_map[r][c + 1]
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

    # Build styled output row by row, segment by segment
    result = Text()
    for r in range(grid_h):
        if r > 0:
            result.append("\n")
        c = 0
        while c < grid_w:
            pane = cell_owner[r][c]
            if pane is not None:
                # Pane content segment
                pl = pane.left - min_left
                pt = pane.top - min_top
                span_end = min(pl + pane.width, grid_w)
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
                # Border segment — batch contiguous border cells
                chars: list[str] = []
                while c < grid_w and cell_owner[r][c] is None:
                    chars.append(_border_char(r, c))
                    c += 1
                result.append(Text("".join(chars), style="dim"))

    return result


# ── Worktree Footer ──────────────────────────────────────────────────────────


class WorktreeLabel(Static):
    """A clickable worktree branch label."""

    DEFAULT_CSS = """
    WorktreeLabel {
        width: auto;
        height: 1;
        padding: 0 1;
        color: #87d787;
    }
    WorktreeLabel:hover {
        background: $accent 20%;
    }
    """

    def __init__(self, branch: str, window_id: str) -> None:
        super().__init__(f"● {branch}")
        self.branch = branch
        self.window_id = window_id

    def on_click(self) -> None:
        tree = self.app.query_one("TmuxTree", TmuxTree)
        tree._select_by_tmux_id(self.window_id)
        tree.focus()


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
        body = Text(_strip_ansi(content))
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

    def clear_preview(self) -> None:
        if self._last_key == "INTRO":
            return
        self._plain_text = ""
        self._show_intro()


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

    def __init__(self, title: str, placeholder: str = "", initial: str = "") -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._initial = initial

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
        self.dismiss(value if value else None)

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.prevent_default()
            self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Modal for confirming destructive actions."""

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
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
[bold]Navigation[/]
  [bold accent]a[/]       Attach via selected window or pane
  [bold accent]s[/]       Activate selected window/pane in tmux
  [bold accent]b[/]       Toggle tree sidebar
  [bold accent]<[/] [bold accent]>[/]     Resize tree panel
  [bold accent]R[/]       Force refresh the tree

[bold]Creation[/]
  [bold accent]n[/]       New session
  [bold accent]w[/]       New window in selected window/pane session
  [bold accent]h[/]       Split pane horizontally
  [bold accent]v[/]       Split pane vertically

[bold]Modification[/]
  [bold accent]k[/]       Kill selected session, window, or pane
  [bold accent]r[/]       Rename selected window
  [bold accent]+ / -[/]   Resize pane up/down
  [bold accent][ / ][/]   Resize pane left/right

[bold]General[/]
  [bold accent]y[/]       Copy preview to clipboard
  [bold accent]?[/]       Show this help menu
  [bold accent]q[/]       Quit the application
"""

class HelpModal(ModalScreen[None]):
    """Modal for displaying keyboard shortcuts."""

    BINDINGS = [
        Binding("escape", "dismiss", "Dismiss", show=False),
        Binding("q", "dismiss", "Dismiss", show=False),
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
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Label("[bold]Keyboard Shortcuts[/bold]\n")
            yield Label(HELP_TEXT)

    def action_dismiss(self) -> None:
        self.dismiss(None)


# ── Main App ─────────────────────────────────────────────────────────────────

class TmuxTUI(App):
    """Main TUI application for tmux management."""

    TITLE = "tmuxx"
    ENABLE_COMMAND_PALETTE = True

    CSS = """
    #main-container {
        height: 1fr;
    }
    #tree-panel {
        width: 1fr;
        overflow-y: auto;
        scrollbar-size: 0 0;
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
    #tree-header {
        dock: top;
        height: 1;
        content-align: center middle;
        color: $text-muted;
        background: $surface;
    }
    #tree-footer {
        dock: bottom;
        height: auto;
        max-height: 8;
        background: $surface;
    }
    CommandPalette {
        align: center middle;
    }
    CommandPalette > Vertical {
        margin-top: 0;
        width: 60;
        max-height: 20;
    }
    """

    BINDINGS = [
        Binding("question_mark", "help", "Help", key_display="?", priority=True),
        Binding("n", "new_session", "New Session", tooltip="Create a new tmux session", priority=True),
        Binding("w", "new_window", "New Window", tooltip="Create a new window in selected window/pane session", priority=True),
        Binding("h", "split_h", "Split H", tooltip="Split active pane of selected window", priority=True),
        Binding("v", "split_v", "Split V", tooltip="Split active pane of selected window", priority=True),
        Binding("k", "kill_selected", "Kill", tooltip="Kill selected session, window, or pane", priority=True),
        Binding("r", "rename", "Rename", tooltip="Rename selected window", priority=True),
        Binding("s", "activate", "Activate", tooltip="Activate selected window/pane in tmux", priority=True),
        Binding("a", "attach", "Attach", tooltip="Attach to selected window/pane session", priority=True),
        Binding("y", "copy_preview", "Yank", tooltip="Copy (yank) preview content to clipboard", priority=True),
        Binding("b", "toggle_sidebar", "Sidebar", tooltip="Toggle tree sidebar", priority=True),
        Binding("R", "force_refresh", "Refresh", key_display="R", tooltip="Force refresh the tree", show=False, priority=True),
        Binding("plus_sign", "resize('up')", "+Resize", key_display="+", tooltip="Resize pane up", show=False, priority=True),
        Binding("hyphen_minus", "resize('down')", "-Resize", key_display="-", tooltip="Resize pane down", show=False, priority=True),
        Binding("left_square_bracket", "resize('left')", "[Resize", key_display="[", tooltip="Resize pane left", show=False, priority=True),
        Binding("right_square_bracket", "resize('right')", "]Resize", key_display="]", tooltip="Resize pane right", show=False, priority=True),
        Binding("less_than_sign", "panel_resize('shrink')", "<Panel", key_display="<", tooltip="Shrink tree panel", show=False, priority=True),
        Binding("greater_than_sign", "panel_resize('grow')", ">Panel", key_display=">", tooltip="Grow tree panel", show=False, priority=True),
        Binding("q", "quit", "Quit", tooltip="Quit the application", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.backend = TmuxBackend()
        self.git = GitBackend()
        self._worktree_windows: dict[str, tuple[str, str]] = {}  # window_id → (branch, status)
        self._tree = TmuxTree()
        self._preview = PanePreview()
        self._session_count = 0
        self._window_count = 0
        self._pane_count = 0
        self._tree_fr = 1  # tree panel width in fr units (preview is always this * 2 initially)
        self._selection_kind: str = ""  # "session", "window", "pane", or ""

    def compose(self) -> ComposeResult:
        yield Header(icon="[palette]")
        with Horizontal(id="main-container"):
            with Vertical(id="tree-panel"):
                yield Static(
                    "[#ffb300]●[/] [dim]active[/]  [#87d787]●[/] [dim]worktree[/]  [#ffffff]●[/] [dim]selected[/]",
                    id="tree-header",
                )
                yield self._tree
                yield Vertical(id="tree-footer")
            with Vertical(id="preview-panel"):
                yield self._preview
        yield Footer()


    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)


    async def _do_refresh(self) -> None:
        """Core refresh logic — called directly by actions for instant update."""
        try:
            sessions = await self.backend.get_hierarchy()
        except Exception:
            sessions = []

        # Worktree discovery — cross-reference pane paths with git worktrees
        idle_commands = {"bash", "zsh", "fish", "sh", "tmux", "login"}
        try:
            worktrees = await self.git.list_worktrees()
            wt_paths = {
                os.path.normpath(wt.path): wt.branch
                for wt in worktrees if not wt.is_main
            }
            # Reset to re-detect status each refresh
            self._worktree_windows.clear()
            for s in sessions:
                for w in s.windows:
                    for p in w.panes:
                        norm = os.path.normpath(p.current_path) if p.current_path else ""
                        for wt_p, branch in wt_paths.items():
                            if norm.startswith(wt_p):
                                agent_running = p.current_command not in idle_commands
                                status = "running" if agent_running else "done"
                                # Prefer "running" over "done" if multiple panes
                                existing = self._worktree_windows.get(w.window_id)
                                if not existing or (status == "running" and existing[1] != "running"):
                                    self._worktree_windows[w.window_id] = (branch, status)
                                break
        except Exception:
            pass

        # Count totals
        self._session_count = len(sessions)
        self._window_count = sum(len(s.windows) for s in sessions)
        self._pane_count = sum(
            len(w.panes) for s in sessions for w in s.windows
        )
        self.sub_title = (
            f"{self._session_count} sessions, "
            f"{self._window_count} windows, "
            f"{self._pane_count} panes"
        )

        self._tree.worktree_windows = self._worktree_windows
        self._tree.update_tree(sessions)

        # Update worktree footer with clickable labels
        footer = self.query_one("#tree-footer", Vertical)
        footer.remove_children()
        # Deduplicate: branch → first window_id
        seen: dict[str, str] = {}
        for wid, (branch, _status) in self._worktree_windows.items():
            if branch not in seen:
                seen[branch] = wid
        if seen:
            try:
                repo_root = await self.git.get_repo_root()
                repo_name = os.path.basename(repo_root)
            except Exception:
                repo_name = "repo"
            for branch in sorted(seen):
                footer.mount(WorktreeLabel(f"{repo_name}:{branch}", seen[branch]))

        await self._update_preview()

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
        grid_text = compose_window_grid(win.panes, captured, max_cols=avail_w)
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
        # Suppress app bindings when a modal is active so modal keys work
        if len(self.screen_stack) > 1:
            return False

        k = self._selection_kind

        # Always available
        if action in ("quit", "help", "new_session", "toggle_sidebar", "force_refresh"):
            return True

        # Session or window/pane
        if action == "new_window":
            return k in ("session", "window", "pane")
        if action == "kill_selected":
            return k in ("session", "window", "pane")
        if action == "rename":
            return k in ("session", "window")

        # Window or pane only
        if action in ("split_h", "split_v", "activate", "attach", "copy_preview", "resize"):
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
            InputModal("New window name (optional):", placeholder="window-name"),
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

        # Suspend TUI and attach to tmux session
        with self.suspend():
            rc = os.system(f"tmux attach-session -t {_q(sess_name)}")
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
            elif kind == "pane":
                pane: Pane = data[1]
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

    def action_help(self) -> None:
        self.push_screen(HelpModal())


# ── Entry Point ──────────────────────────────────────────────────────────────


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmuxx",
        description="TUI for humans. Deterministic agent CLI for automation.",
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

    if argv[0] == "agent":
        from tmux_agent import run_agent_cli

        return run_agent_cli(argv[1:])

    parser = _build_cli_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
