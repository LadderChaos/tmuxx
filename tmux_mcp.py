"""tmuxx MCP server: LLM control of tmux via Model Context Protocol."""

from __future__ import annotations

import asyncio
import re
from typing import Literal

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    raise SystemExit(
        'MCP dependencies not installed. Run: pip install ".[mcp]"'
    )

from tmux_core import Pane, Session, TmuxBackend, Window, quote

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


# ── Entry Point ───────────────────────────────────────────────────────────────


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
