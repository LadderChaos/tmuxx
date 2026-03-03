"""tmuxx MCP server: LLM control of tmux via Model Context Protocol."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
from typing import Literal

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ImageContent, TextContent
except ImportError:
    raise SystemExit(
        'MCP dependencies not installed. Run: pip install ".[mcp]"'
    )

from tmux_core import GitBackend, Pane, Session, TmuxBackend, Window, Worktree, quote, slugify

# ── Validation ──────────────────────────────────────────────────────────────

# Tmux IDs are always like %0, @1, $2
_TMUX_ID_RE = re.compile(r"^[%@$]\d+$")

# Tmux key names: alphanumeric, hyphens, plus, backslash (C-\), space-separated
_TMUX_KEY_RE = re.compile(r"^[A-Za-z0-9\-_+\\ ]+$")


def _safe_id(tmux_id: str) -> str:
    """Validate and return a tmux ID, or raise on invalid format."""
    if not _TMUX_ID_RE.match(tmux_id):
        raise ValueError(f"Invalid tmux ID: {tmux_id!r}")
    return tmux_id


def _bound(value: int, lo: int, hi: int, name: str) -> int:
    """Clamp and validate a numeric parameter."""
    if value < lo or value > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


# ── Helpers ──────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _serialize_pane(p: Pane) -> dict:
    return {
        "pane_id": p.pane_id,
        "pane_index": p.pane_index,
        "width": p.width,
        "height": p.height,
        "current_command": p.current_command,
        "active": p.active,
    }


def _serialize_window(w: Window) -> dict:
    return {
        "window_id": w.window_id,
        "window_index": w.window_index,
        "name": w.name,
        "active": w.active,
        "panes": [_serialize_pane(p) for p in w.panes],
    }


def _serialize_session(s: Session) -> dict:
    return {
        "session_id": s.session_id,
        "name": s.name,
        "attached": s.attached,
        "windows": [_serialize_window(w) for w in s.windows],
    }


# ── MCP Server ───────────────────────────────────────────────────────────────

mcp = FastMCP("tmuxx")
backend = TmuxBackend()
git = GitBackend()


# ── Read / Introspection ─────────────────────────────────────────────────────


@mcp.tool()
async def list_sessions() -> list[dict]:
    """List all tmux sessions with their windows and panes as a JSON hierarchy."""
    sessions = await backend.get_hierarchy()
    return [_serialize_session(s) for s in sessions]


@mcp.tool()
async def capture_pane(pane_id: str, lines: int = 50) -> str:
    """Capture the visible text content of a tmux pane (ANSI codes stripped).

    Args:
        pane_id: The tmux pane ID (e.g. "%0", "%3").
        lines: Number of lines to capture from the bottom of scrollback (1-5000).
    """
    _bound(lines, 1, 5000, "lines")
    raw = await backend.capture_pane(_safe_id(pane_id), lines=lines)
    return _strip_ansi(raw)


@mcp.tool()
async def capture_window(window_id: str) -> dict[str, str]:
    """Capture the text content of all panes in a window (ANSI codes stripped).

    Args:
        window_id: The tmux window ID (e.g. "@0", "@2").

    Returns:
        A dict mapping pane_id to its captured text content.
    """
    window_id = _safe_id(window_id)
    sessions = await backend.get_hierarchy()
    target_panes: list[Pane] | None = None
    for s in sessions:
        for w in s.windows:
            if w.window_id == window_id:
                target_panes = w.panes
                break
        if target_panes is not None:
            break
    if target_panes is None:
        raise ValueError(f"Window {window_id} not found")
    captured = await backend.capture_window_panes(target_panes)
    return {pid: _strip_ansi(text) for pid, text in captured.items()}


# ── Screenshot Renderer ──────────────────────────────────────────────────────

# Standard 16-color ANSI palette
_ANSI_COLORS = {
    "black": (0, 0, 0), "red": (205, 0, 0), "green": (0, 205, 0),
    "brown": (205, 205, 0), "blue": (0, 0, 238), "magenta": (205, 0, 205),
    "cyan": (0, 205, 205), "white": (229, 229, 229), "default": (204, 204, 204),
}
_ANSI_BRIGHT = {
    "black": (127, 127, 127), "red": (255, 0, 0), "green": (0, 255, 0),
    "brown": (255, 255, 0), "blue": (92, 92, 255), "magenta": (255, 0, 255),
    "cyan": (0, 255, 255), "white": (255, 255, 255),
}
_BG_DEFAULT = (30, 30, 30)
_BORDER_COLOR = (80, 80, 80)
_CELL_W, _CELL_H = 7, 14
_BORDER_PX = 1


def _color_from_attr(color: str, bold: bool = False) -> tuple[int, int, int] | None:
    """Resolve a pyte character color attribute to an RGB tuple."""
    if isinstance(color, str):
        if bold and color in _ANSI_BRIGHT:
            return _ANSI_BRIGHT[color]
        return _ANSI_COLORS.get(color)
    return None


def _render_pane_image(ansi_text: str, cols: int, rows: int):
    """Parse ANSI text via pyte and render to a PIL Image."""
    import pyte
    from PIL import Image, ImageDraw

    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(ansi_text)

    img = Image.new("RGB", (cols * _CELL_W, rows * _CELL_H), _BG_DEFAULT)
    draw = ImageDraw.Draw(img)

    for y in range(rows):
        for x in range(cols):
            char = screen.buffer[y][x]
            # Background
            bg = _color_from_attr(char.bg) if char.bg != "default" else None
            if bg:
                draw.rectangle(
                    [x * _CELL_W, y * _CELL_H, (x + 1) * _CELL_W, (y + 1) * _CELL_H],
                    fill=bg,
                )
            # Foreground
            if char.data.strip():
                fg = _color_from_attr(char.fg, bold=char.bold) or (204, 204, 204)
                draw.text((x * _CELL_W, y * _CELL_H), char.data, fill=fg)

    return img


def _composite_window(panes: list[Pane], captured: dict[str, str]):
    """Composite all panes into a single window image with borders."""
    from PIL import Image, ImageDraw

    if not panes:
        img = Image.new("RGB", (200, 50), _BG_DEFAULT)
        return img

    min_l = min(p.left for p in panes)
    min_t = min(p.top for p in panes)
    max_r = max(p.left + p.width for p in panes)
    max_b = max(p.top + p.height for p in panes)
    total_cols = max_r - min_l
    total_rows = max_b - min_t

    img = Image.new(
        "RGB",
        (total_cols * _CELL_W + _BORDER_PX * 2, total_rows * _CELL_H + _BORDER_PX * 2),
        _BORDER_COLOR,
    )

    for pane in panes:
        ansi = captured.get(pane.pane_id, "")
        pane_img = _render_pane_image(ansi, pane.width, pane.height)
        px = (pane.left - min_l) * _CELL_W + _BORDER_PX
        py = (pane.top - min_t) * _CELL_H + _BORDER_PX
        img.paste(pane_img, (px, py))

    return img


def _image_to_base64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


@mcp.tool()
async def screenshot_window(window_id: str) -> list[TextContent | ImageContent]:
    """Take a screenshot of a tmux window showing all panes in their layout.

    Returns a PNG image of the full window with all panes composited in their
    actual positions with borders between them.

    Args:
        window_id: The tmux window ID (e.g. "@0", "@2").
    """
    window_id = _safe_id(window_id)
    sessions = await backend.get_hierarchy()
    target_win: Window | None = None
    for s in sessions:
        for w in s.windows:
            if w.window_id == window_id:
                target_win = w
                break
        if target_win is not None:
            break
    if target_win is None:
        raise ValueError(f"Window {window_id} not found")

    # Capture all panes with ANSI codes
    captured = await backend.capture_window_panes(target_win.panes)
    img = _composite_window(target_win.panes, captured)
    b64 = _image_to_base64(img)

    return [
        TextContent(
            type="text",
            text=f"Screenshot of window {window_id} ({target_win.name}): "
            f"{len(target_win.panes)} panes",
        ),
        ImageContent(type="image", data=b64, mimeType="image/png"),
    ]


# ── Session Management ────────────────────────────────────────────────────────


@mcp.tool()
async def create_session(name: str) -> str:
    """Create a new tmux session.

    Args:
        name: Name for the new session.
    """
    await backend.new_session(name)
    return f"Created session '{name}'"


@mcp.tool()
async def kill_session(name: str) -> str:
    """Kill (destroy) a tmux session. This is destructive and cannot be undone.

    Args:
        name: Name of the session to kill.
    """
    await backend.kill_session(name)
    return f"Killed session '{name}'"


@mcp.tool()
async def rename_session(old_name: str, new_name: str) -> str:
    """Rename an existing tmux session.

    Args:
        old_name: Current name of the session.
        new_name: New name for the session.
    """
    await backend.rename_session(old_name, new_name)
    return f"Renamed session '{old_name}' to '{new_name}'"


# ── Window Management ─────────────────────────────────────────────────────────


@mcp.tool()
async def create_window(session_name: str, name: str | None = None) -> str:
    """Create a new window in a tmux session.

    Args:
        session_name: Name of the session to add the window to.
        name: Optional name for the new window.
    """
    await backend.new_window(session_name, name)
    label = f" '{name}'" if name else ""
    return f"Created window{label} in session '{session_name}'"


@mcp.tool()
async def kill_window(window_id: str) -> str:
    """Kill (destroy) a tmux window. This is destructive and cannot be undone.

    Args:
        window_id: The tmux window ID (e.g. "@0").
    """
    await backend.kill_window(_safe_id(window_id))
    return f"Killed window {window_id}"


@mcp.tool()
async def rename_window(window_id: str, new_name: str) -> str:
    """Rename a tmux window.

    Args:
        window_id: The tmux window ID (e.g. "@0").
        new_name: New name for the window.
    """
    await backend.rename_window(_safe_id(window_id), new_name)
    return f"Renamed window {window_id} to '{new_name}'"


# ── Pane Management ───────────────────────────────────────────────────────────


@mcp.tool()
async def split_pane(pane_id: str, horizontal: bool = False) -> str:
    """Split a tmux pane.

    Args:
        pane_id: The tmux pane ID (e.g. "%0").
        horizontal: If True, split left/right. If False (default), split top/bottom.
    """
    await backend.split_pane(_safe_id(pane_id), horizontal=horizontal)
    direction = "horizontally" if horizontal else "vertically"
    return f"Split pane {pane_id} {direction}"


@mcp.tool()
async def kill_pane(pane_id: str) -> str:
    """Kill (destroy) a tmux pane. This is destructive and cannot be undone.

    Args:
        pane_id: The tmux pane ID (e.g. "%0").
    """
    await backend.kill_pane(_safe_id(pane_id))
    return f"Killed pane {pane_id}"


@mcp.tool()
async def resize_pane(
    pane_id: str,
    direction: Literal["up", "down", "left", "right"],
    amount: int = 5,
) -> str:
    """Resize a tmux pane.

    Args:
        pane_id: The tmux pane ID (e.g. "%0").
        direction: Direction to resize: "up", "down", "left", or "right".
        amount: Number of cells to resize by (1-200).
    """
    _bound(amount, 1, 200, "amount")
    await backend.resize_pane(_safe_id(pane_id), direction, amount)
    return f"Resized pane {pane_id} {direction} by {amount}"


# ── Command Execution ─────────────────────────────────────────────────────────


@mcp.tool()
async def send_command(pane_id: str, command: str) -> str:
    """Send a command to a tmux pane (appends Enter to execute it).

    Args:
        pane_id: The tmux pane ID (e.g. "%0").
        command: The command string to execute.
    """
    await backend.send_keys(_safe_id(pane_id), command)
    return f"Sent command to {pane_id}: {command}"


@mcp.tool()
async def send_keys(pane_id: str, keys: str) -> str:
    """Send raw keys to a tmux pane without appending Enter.

    Use this for special keys like C-c (Ctrl+C), C-\\ (Ctrl+backslash),
    Escape, Tab, Up, Down, etc.

    Args:
        pane_id: The tmux pane ID (e.g. "%0").
        keys: The keys to send (e.g. "C-c", "Escape", "Tab").
    """
    safe_id = _safe_id(pane_id)
    if not _TMUX_KEY_RE.match(keys):
        raise ValueError(f"Invalid key sequence: {keys!r}")
    # Pass keys directly via exec — no shell quoting needed
    await TmuxBackend._run("tmux", "send-keys", "-t", safe_id, keys)
    return f"Sent keys to {pane_id}: {keys}"


@mcp.tool()
async def run_and_capture(
    pane_id: str,
    command: str,
    wait_seconds: float = 1.0,
    lines: int = 50,
) -> str:
    """Send a command to a pane, wait for output, then capture the result.

    This is the most useful tool for running a command and seeing its output.

    Args:
        pane_id: The tmux pane ID (e.g. "%0").
        command: The command to execute.
        wait_seconds: Seconds to wait for the command to produce output (0-30).
        lines: Number of lines to capture from the pane (1-5000).
    """
    _bound(lines, 1, 5000, "lines")
    if wait_seconds < 0 or wait_seconds > 30:
        raise ValueError(f"wait_seconds must be between 0 and 30, got {wait_seconds}")
    safe_id = _safe_id(pane_id)
    await backend.send_keys(safe_id, command)
    await asyncio.sleep(wait_seconds)
    raw = await backend.capture_pane(safe_id, lines=lines)
    return _strip_ansi(raw)


# ── Agent / Worktree Tools ───────────────────────────────────────────────────


def _serialize_worktree(wt: Worktree) -> dict:
    return {
        "path": wt.path,
        "branch": wt.branch,
        "head": wt.head,
        "is_main": wt.is_main,
        "status": wt.status,
    }


async def _detect_worktree_status(worktrees: list[Worktree]) -> None:
    """Cross-reference worktree paths with pane commands to set status.

    - "running" if a pane in the worktree dir is running claude/node/python/etc.
    - "done" if a pane is in the worktree dir but at a shell prompt (idle command)
    - "idle" if no pane is in the worktree dir
    """
    sessions = await backend.get_hierarchy()
    idle_commands = {"bash", "zsh", "fish", "sh", "tmux", "login"}

    for wt in worktrees:
        if wt.is_main:
            continue
        wt_norm = os.path.normpath(wt.path)
        found_pane = False
        agent_running = False
        for s in sessions:
            for w in s.windows:
                for p in w.panes:
                    pane_path = os.path.normpath(p.current_path) if p.current_path else ""
                    if pane_path.startswith(wt_norm):
                        found_pane = True
                        if p.current_command not in idle_commands:
                            agent_running = True
        if agent_running:
            wt.status = "running"
        elif found_pane:
            wt.status = "done"
        # else stays "idle"


@mcp.tool()
async def list_worktrees() -> list[dict]:
    """List all git worktrees in the repository.

    Returns a JSON array of worktree objects with path, branch, head SHA,
    whether it is the main worktree, and agent status (running/done/idle).
    """
    wts = await git.list_worktrees()
    await _detect_worktree_status(wts)
    return [_serialize_worktree(wt) for wt in wts]


@mcp.tool()
async def diff_worktree(branch: str) -> str:
    """Show the diff of a worktree branch against main.

    Returns the output of ``git diff main...branch``, showing all changes
    made on the branch since it diverged from main.

    Args:
        branch: The worktree branch name to diff.
    """
    diff = await git.diff_worktree(branch)
    return diff or "(no changes)"


@mcp.tool()
async def launch_agent(
    session_name: str,
    prompt: str,
    branch: str | None = None,
    base_branch: str | None = None,
    agent_command: str = "claude -p",
) -> str:
    """Launch an agent task in a new git worktree with its own tmux window.

    Creates a worktree + branch, opens a new tmux window in that directory,
    and runs the agent command with the prompt inside it.

    Args:
        session_name: Tmux session to create the window in.
        prompt: The task prompt to pass to the agent.
        branch: Optional branch name (auto-generated from prompt if omitted).
        base_branch: Optional branch to base the worktree on (defaults to HEAD).
                     Use this to stack agents on top of another agent's work.
        agent_command: The CLI command prefix to run (default: "claude -p").
                       Examples: "claude -p", "gemini -p", "aider --message".
    """
    branch = branch or slugify(prompt)
    wt_path = await git.create_worktree(branch, base_branch=base_branch)
    await backend.new_window_in_dir(session_name, wt_path, branch)
    # Find the new window and send the agent command
    sessions = await backend.get_hierarchy()
    for s in sessions:
        if s.name == session_name:
            for w in s.windows:
                if w.name == branch and w.panes:
                    await backend.send_keys(
                        w.panes[0].pane_id, f"{agent_command} {quote(prompt)}"
                    )
                    return f"Agent launched on branch '{branch}' in {wt_path}"
    return f"Worktree created at {wt_path} but could not find new window"


async def _capture_agent_output(branch: str) -> str | None:
    """Capture pane output for a worktree branch and save to .worktrees/<branch>.log.

    Returns the log file path, or None if no matching pane was found.
    """
    root = await git.get_repo_root()
    wt_path = os.path.normpath(os.path.join(root, ".worktrees", branch))
    sessions = await backend.get_hierarchy()
    captured_lines: list[str] = []
    for s in sessions:
        for w in s.windows:
            for p in w.panes:
                pane_path = os.path.normpath(p.current_path) if p.current_path else ""
                if pane_path.startswith(wt_path):
                    raw = await backend.capture_pane(p.pane_id, lines=5000)
                    captured_lines.append(f"=== {p.pane_id} ({p.current_command}) ===")
                    captured_lines.append(_strip_ansi(raw))
    if not captured_lines:
        return None
    log_dir = os.path.join(root, ".worktrees")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{branch}.log")
    with open(log_path, "w") as f:
        f.write("\n".join(captured_lines))
    return log_path


@mcp.tool()
async def merge_worktree(
    branch: str,
    commit_message: str | None = None,
    test_command: str | None = None,
) -> str:
    """Merge a worktree branch into main and clean up.

    Captures agent output to .worktrees/<branch>.log before cleanup.
    Optionally runs a test command in the worktree before merging.
    Stages all changes, commits, merges into the main branch, then removes
    the worktree directory and deletes the branch.

    Args:
        branch: The worktree branch name to merge.
        commit_message: Optional commit message (defaults to "agent: <branch>").
        test_command: Optional shell command to run in the worktree before merging.
                      If it exits non-zero, the merge is aborted and the worktree is kept.
    """
    log_path = await _capture_agent_output(branch)
    await git.merge_worktree(branch, commit_message, test_command=test_command)
    msg = f"Merged '{branch}' into main and cleaned up"
    if log_path:
        msg += f"\nAgent output saved to {log_path}"
    return msg


@mcp.tool()
async def discard_worktree(branch: str) -> str:
    """Discard a worktree and force-delete its branch.

    Captures agent output to .worktrees/<branch>.log before cleanup.
    All uncommitted changes in the worktree will be lost.

    Args:
        branch: The worktree branch name to discard.
    """
    log_path = await _capture_agent_output(branch)
    await git.discard_worktree(branch)
    msg = f"Discarded worktree and branch '{branch}'"
    if log_path:
        msg += f"\nAgent output saved to {log_path}"
    return msg


@mcp.tool()
async def read_agent_log(branch: str) -> str:
    """Read the captured output log of a worktree agent.

    Returns the contents of .worktrees/<branch>.log, which is automatically
    saved when merging or discarding a worktree.

    Args:
        branch: The worktree branch name whose log to read.
    """
    root = await git.get_repo_root()
    log_path = os.path.join(root, ".worktrees", f"{branch}.log")
    if not os.path.exists(log_path):
        return f"No log found for branch '{branch}'"
    with open(log_path) as f:
        return f.read()


# ── Entry Point ───────────────────────────────────────────────────────────────


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
