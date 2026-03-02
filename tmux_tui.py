"""tmux-tui: Terminal UI for managing tmux sessions, windows, and panes."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field

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


@dataclass
class Window:
    window_id: str
    window_index: int
    name: str
    active: bool
    panes: list[Pane] = field(default_factory=list)


@dataclass
class Session:
    session_id: str
    name: str
    attached: bool
    windows: list[Window] = field(default_factory=list)


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
                "tmux list-sessions -F '#{session_id}:#{session_name}:#{session_attached}'"
            ),
            self._run(
                "tmux list-windows -a -F '#{session_id}:#{window_id}:#{window_index}:#{window_name}:#{window_active}'"
            ),
            self._run(
                "tmux list-panes -a -F '#{window_id}:#{pane_id}:#{pane_index}:#{pane_width}:#{pane_height}:#{pane_current_command}:#{pane_active}:#{pane_left}:#{pane_top}'"
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
            )
            win_map.setdefault(sess_id, []).append(win)

        # Parse sessions
        sessions: list[Session] = []
        for line in sessions_raw.splitlines():
            if not line:
                continue
            parts = line.split(":")
            if len(parts) < 3:
                continue
            sess = Session(
                session_id=parts[0],
                name=parts[1],
                attached=parts[2] == "1",
                windows=sorted(
                    win_map.get(parts[0], []), key=lambda w: w.window_index
                ),
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


def _q(s: str) -> str:
    """Shell-quote a string."""
    return "'" + s.replace("'", "'\\''") + "'"


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
        else:
            self._fingerprint = fp
            self._full_rebuild()
        # Re-apply dot indicator after labels are refreshed
        self._dot_node = None
        self._update_dot()

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
        for sess in self.sessions:
            status = f" [{c['ok']}]attached[/]" if sess.attached else ""
            sess_label = f"[bold {c['session']}]{escape(sess.name)}[/]{status}"
            sess_node = self.root.add(sess_label, data=("session", sess))
            for win in sess.windows:
                win_status = f" [{c['active']}]●[/]" if win.active else ""
                win_label = (
                    f"[bold {c['window']}]{escape(win.name)}[/] "
                    f"[dim]:{win.window_index}[/]{win_status}"
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

        # Restore cursor
        if saved_id:
            self._select_by_tmux_id(saved_id)

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
                win_node.set_label(
                    f"[bold {c['window']}]{escape(win.name)}[/] [dim]:{win.window_index}[/]{win_status}"
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
        """Walk tree and select node matching target tmux ID."""
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
                self.select_node(node)
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
        return None


# ── Window Grid Compositor ───────────────────────────────────────────────────


def compose_window_grid(panes: list[Pane], captured: dict[str, str]) -> Text:
    """Compose a styled text grid showing all panes with ANSI colors and borders."""
    if not panes:
        return Text("")

    # Offset to remove tmux's outer borders
    min_left = min(p.left for p in panes)
    min_top = min(p.top for p in panes)
    grid_w = max(p.left + p.width for p in panes) - min_left
    grid_h = max(p.top + p.height for p in panes) - min_top

    # Parse ANSI content into styled lines per pane
    pane_lines: dict[str, list[Text]] = {}
    for pane in panes:
        raw = captured.get(pane.pane_id, "")
        styled = Text.from_ansi(raw)
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
                    pad = pane.width - len(line)
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
        "TUI for humans. MCP server for AI agents.",
        "One interface to see, control, and automate tmux.",
    ]

    def __init__(self) -> None:
        super().__init__("", id="pane-preview", classes="intro")
        self._last_key: str = ""

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
        body = Text.from_ansi(content)
        self.update(header + body)
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
        self.update(header + grid)
        self._scroll_to_bottom()

    def set_session_content(self, sess: Session) -> None:
        total_panes = sum(len(w.panes) for w in sess.windows)

        active_win = None
        for w in sess.windows:
            if w.active:
                active_win = w
                break
        if not active_win and sess.windows:
            active_win = sess.windows[0]

        active_pane = None
        if active_win:
            for p in active_win.panes:
                if p.active:
                    active_pane = p
                    break
            if not active_pane and active_win.panes:
                active_pane = active_win.panes[0]

        lines: list[str] = []
        attach_label = "[green]attached[/]" if sess.attached else "[dim]detached[/]"
        lines.append(
            f"[bold]Session: {escape(sess.name)}[/bold] ({sess.session_id}) {attach_label}"
        )
        lines.append(f"[dim]Windows:[/] {len(sess.windows)}  [dim]Panes:[/] {total_panes}")

        if active_win:
            lines.append(
                f"[dim]Active window:[/] {escape(active_win.name)} ({active_win.window_id}) :{active_win.window_index}"
            )
        else:
            lines.append("[dim]Active window:[/] none")

        if active_pane:
            lines.append(
                f"[dim]Active pane:[/] {active_pane.pane_id} ({escape(active_pane.current_command)})"
            )
        else:
            lines.append("[dim]Active pane:[/] none")

        lines.append("")
        lines.append("[bold]Windows[/bold]")
        if not sess.windows:
            lines.append("[dim]No windows in this session[/]")
        else:
            for w in sess.windows:
                marker = "[#ffb300]●[/]" if w.active else " "
                w_active_pane = None
                for p in w.panes:
                    if p.active:
                        w_active_pane = p
                        break
                pane_suffix = f", active pane {w_active_pane.pane_id}" if w_active_pane else ""
                lines.append(
                    f"{marker} [bold]{escape(w.name)}[/] [dim]({w.window_id}) :{w.window_index}[/] - {len(w.panes)} panes{pane_suffix}"
                )

        lines.append("")
        lines.append("[dim]Session action:[/] k (kill session)")

        body = "\n".join(lines)
        key = f"sess:{sess.session_id}:{body}"
        if key == self._last_key:
            return

        self._last_key = key
        self.remove_class("intro")
        self.update(Text.from_markup(body))
        self._scroll_to_bottom()

    def clear_preview(self) -> None:
        if self._last_key == "INTRO":
            return
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


class SendCommandModal(ModalScreen[str | None]):
    """Modal for sending a command to a pane."""

    CSS = """
    SendCommandModal {
        align: center middle;
    }
    #cmd-dialog {
        width: 60;
        height: auto;
        max-height: 12;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #cmd-dialog Label {
        margin-bottom: 1;
    }
    """

    def __init__(self, pane_id: str) -> None:
        super().__init__()
        self._pane_id = pane_id

    def compose(self) -> ComposeResult:
        with Vertical(id="cmd-dialog"):
            yield Label(f"Send command to {self._pane_id}")
            yield Input(placeholder="command...", id="cmd-input")

    def on_mount(self) -> None:
        self.query_one("#cmd-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.prevent_default()
            self.dismiss(None)


HELP_TEXT = """\
[bold]Navigation[/]
  [bold accent]a[/]       Attach via selected window or pane
  [bold accent]s[/]       Activate selected window/pane in tmux
  [bold accent]b[/]       Toggle tree sidebar
  [bold accent]R[/]       Force refresh the tree

[bold]Creation[/]
  [bold accent]n[/]       New session
  [bold accent]w[/]       New window in selected window/pane session
  [bold accent]h[/]       Split pane horizontally
  [bold accent]v[/]       Split pane vertically

[bold]Modification[/]
  [bold accent]k[/]       Kill selected session, window, or pane
  [bold accent]r[/]       Rename selected window
  [bold accent]c[/]       Send command to selected window/pane
  [bold accent]+ / -[/]   Resize pane up/down
  [bold accent][ / ][/]   Resize pane left/right

[bold]General[/]
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
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("n", "new_session", "New Session", tooltip="Create a new tmux session"),
        Binding("w", "new_window", "New Window", tooltip="Create a new window in selected window/pane session"),
        Binding("h", "split_h", "Split H", tooltip="Split active pane of selected window"),
        Binding("v", "split_v", "Split V", tooltip="Split active pane of selected window"),
        Binding("k", "kill_selected", "Kill", tooltip="Kill selected session, window, or pane"),
        Binding("r", "rename", "Rename", tooltip="Rename selected window"),
        Binding("c", "send_command", "Cmd", tooltip="Send a command to selected window/pane"),
        Binding("s", "activate", "Activate", tooltip="Activate selected window/pane in tmux"),
        Binding("a", "attach", "Attach", tooltip="Attach to selected window/pane session"),
        Binding("b", "toggle_sidebar", "Sidebar", tooltip="Toggle tree sidebar"),
        Binding("R", "force_refresh", "Refresh", key_display="R", tooltip="Force refresh the tree", show=False),
        Binding("plus_sign", "resize('up')", "+Resize", key_display="+", tooltip="Resize pane up", show=False),
        Binding("hyphen_minus", "resize('down')", "-Resize", key_display="-", tooltip="Resize pane down", show=False),
        Binding("left_square_bracket", "resize('left')", "[Resize", key_display="[", tooltip="Resize pane left", show=False),
        Binding("right_square_bracket", "resize('right')", "]Resize", key_display="]", tooltip="Resize pane right", show=False),
        Binding("q", "quit", "Quit", tooltip="Quit the application"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.backend = TmuxBackend()
        self._tree = TmuxTree()
        self._preview = PanePreview()
        self._session_count = 0
        self._window_count = 0
        self._pane_count = 0

    def compose(self) -> ComposeResult:
        yield Header(icon="[palette]")
        with Horizontal(id="main-container"):
            with Vertical(id="tree-panel"):
                yield Static(
                    "[dim]session : window : pane[/]",
                    id="tree-header",
                )
                yield self._tree
            with Vertical(id="preview-panel"):
                yield self._preview
        yield Footer()


    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)


    @work(exclusive=True)
    async def refresh_data(self) -> None:
        try:
            sessions = await self.backend.get_hierarchy()
        except Exception:
            return

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

        self._tree.update_tree(sessions)
        await self._update_preview()

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

        grid_text = compose_window_grid(win.panes, captured)
        self._preview.set_window_content(win, grid_text)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._refresh_preview()

    @work(exclusive=True, group="preview")
    async def _refresh_preview(self) -> None:
        await self._update_preview()

    # ── Actions ──────────────────────────────────────────────────────────

    def action_new_session(self) -> None:
        self._do_new_session()

    @work
    async def _do_new_session(self) -> None:
        name = await self.push_screen_wait(
            InputModal("New session name:", placeholder="my-session")
        )
        if name:
            try:
                await self.backend.new_session(name)
            except Exception as e:
                self.notify(f"Create session failed: {e}", severity="error")
                return
            self.refresh_data()

    def action_new_window(self) -> None:
        self._do_new_window()

    @work
    async def _do_new_window(self) -> None:
        data = self._tree.get_selected_data()
        if not data:
            self.notify("Select a window or pane first", severity="warning")
            return
        kind = data[0]
        if kind == "window":
            sess = data[2]
        elif kind == "pane":
            sess = data[3]
        else:
            self.notify("Session dashboard is kill-only; select a window or pane", severity="warning")
            return

        name = await self.push_screen_wait(
            InputModal("New window name (optional):", placeholder="window-name")
        )
        try:
            await self.backend.new_window(sess.name, name)
        except Exception as e:
            self.notify(f"Create window failed: {e}", severity="error")
            return
        self.refresh_data()

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
        self.refresh_data()

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
        self.refresh_data()

    def action_kill_selected(self) -> None:
        self._do_kill_selected()

    @work
    async def _do_kill_selected(self) -> None:
        data = self._tree.get_selected_data()
        if not data:
            return
        kind = data[0]

        try:
            if kind == "session":
                sess: Session = data[1]
                ok = await self.push_screen_wait(
                    ConfirmModal(f"Kill session '{sess.name}'?")
                )
                if ok:
                    await self.backend.kill_session(sess.name)
            elif kind == "window":
                win: Window = data[1]
                ok = await self.push_screen_wait(
                    ConfirmModal(f"Kill window '{win.name}' ({win.window_id})?")
                )
                if ok:
                    await self.backend.kill_window(win.window_id)
            elif kind == "pane":
                pane: Pane = data[1]
                ok = await self.push_screen_wait(
                    ConfirmModal(f"Kill pane {pane.pane_id}?")
                )
                if ok:
                    await self.backend.kill_pane(pane.pane_id)
        except Exception as e:
            self.notify(f"Kill failed: {e}", severity="error")
            return

        self.refresh_data()

    def action_rename(self) -> None:
        self._do_rename()

    @work
    async def _do_rename(self) -> None:
        data = self._tree.get_selected_data()
        if not data:
            return
        kind = data[0]

        if kind == "window":
            win: Window = data[1]
            new_name = await self.push_screen_wait(
                InputModal("Rename window:", initial=win.name)
            )
            if new_name and new_name != win.name:
                try:
                    await self.backend.rename_window(win.window_id, new_name)
                except Exception as e:
                    self.notify(f"Rename failed: {e}", severity="error")
                    return
        elif kind == "session":
            self.notify("Session dashboard is kill-only; select a window to rename", severity="warning")
            return
        elif kind == "pane":
            self.notify("Panes cannot be renamed", severity="warning")
            return

        self.refresh_data()

    def action_send_command(self) -> None:
        self._do_send_command()

    @work
    async def _do_send_command(self) -> None:
        pane_id = self._tree.get_selected_pane_id()
        if not pane_id:
            self.notify("Select a window or pane first", severity="warning")
            return
        cmd = await self.push_screen_wait(SendCommandModal(pane_id))
        if cmd:
            try:
                await self.backend.send_keys(pane_id, cmd)
            except Exception as e:
                self.notify(f"Send command failed: {e}", severity="error")
                return
            await asyncio.sleep(0.3)
            self.refresh_data()

    def action_toggle_sidebar(self) -> None:
        panel = self.query_one("#tree-panel")
        panel.display = not panel.display

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

        self.refresh_data()

    def watch_theme(self, old_theme: str, new_theme: str) -> None:
        self._tree.recolor()
        if self._preview._last_key == "INTRO":
            self._preview._show_intro()

    def action_force_refresh(self) -> None:
        self.refresh_data()

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
        self.refresh_data()

    def action_help(self) -> None:
        self.push_screen(HelpModal())


# ── Entry Point ──────────────────────────────────────────────────────────────


def main():
    app = TmuxTUI()
    app.run()


if __name__ == "__main__":
    main()
