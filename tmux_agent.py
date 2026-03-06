"""tmuxx agent CLI: deterministic, JSON-friendly tmuxx automation surface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import Any, Literal
from uuid import uuid4

from tmux_core import GitBackend, Pane, Session, TmuxBackend, Window, Worktree, quote, slugify, detect_needs_prompt


# Tmux IDs are always like %0, @1, $2
_TMUX_ID_RE = re.compile(r"^[%@$]\d+$")

# Tmux key names: alphanumeric, hyphens, plus, backslash (C-\), space-separated
_TMUX_KEY_RE = re.compile(r"^[A-Za-z0-9\-_+\\ ]+$")

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

backend = TmuxBackend()
git = GitBackend()


def _package_version() -> str:
    try:
        return pkg_version("tmuxx")
    except PackageNotFoundError:
        return "dev"


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


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _join_text_parts(parts: list[str], name: str) -> str:
    if parts and parts[0] == "--":
        parts = parts[1:]
    text = " ".join(parts).strip()
    if not text:
        raise ValueError(f"{name} cannot be empty")
    return text


def _extract_between_markers(text: str, start_marker: str, end_marker: str) -> str | None:
    lines = text.splitlines()
    start_line_idx: int | None = None
    end_line_idx: int | None = None

    for idx, line in enumerate(lines):
        if line.strip() == start_marker:
            start_line_idx = idx
            break

    if start_line_idx is None:
        return None

    for idx in range(start_line_idx + 1, len(lines)):
        if lines[idx].strip() == end_marker:
            end_line_idx = idx
            break

    if end_line_idx is None:
        return "\n".join(lines[start_line_idx + 1 :]).strip("\n")

    return "\n".join(lines[start_line_idx + 1 : end_line_idx]).strip("\n")


def _serialize_pane(p: Pane) -> dict[str, Any]:
    return {
        "pane_id": p.pane_id,
        "pane_index": p.pane_index,
        "width": p.width,
        "height": p.height,
        "current_command": p.current_command,
        "active": p.active,
        "status": p.status,
        "activity": p.activity,
        "needs_prompt": p.needs_prompt,
    }


def _serialize_window(w: Window) -> dict[str, Any]:
    return {
        "window_id": w.window_id,
        "window_index": w.window_index,
        "name": w.name,
        "active": w.active,
        "status": w.status,
        "panes": [_serialize_pane(p) for p in w.panes],
    }


def _serialize_session(s: Session) -> dict[str, Any]:
    return {
        "session_id": s.session_id,
        "name": s.name,
        "attached": s.attached,
        "windows": [_serialize_window(w) for w in s.windows],
    }


def _serialize_worktree(wt: Worktree) -> dict[str, Any]:
    return {
        "path": wt.path,
        "branch": wt.branch,
        "head": wt.head,
        "is_main": wt.is_main,
        "status": wt.status,
    }


async def _detect_pane_statuses(sessions: list[Session]) -> None:
    """
    Detect status and prompt needs for each pane by analyzing recent output.
    Sets pane.status to: "idle", "running", "waiting_for_input", or "error".
    Sets pane.needs_prompt to True if waiting for user input.
    """
    idle_commands = {"bash", "zsh", "fish", "sh", "tmux", "login"}
    
    for s in sessions:
        for w in s.windows:
            # Aggregate window status from panes
            window_has_running = False
            window_has_prompt = False
            
            for p in w.panes:
                # Capture recent output to detect status
                try:
                    recent_output = await backend.capture_pane(p.pane_id, lines=50)
                    p.recent_output = recent_output
                except Exception:
                    recent_output = ""
                
                # Detect if waiting for prompt
                p.needs_prompt = detect_needs_prompt(recent_output)
                
                # Determine pane status
                if p.needs_prompt:
                    p.status = "waiting_for_input"
                    window_has_prompt = True
                elif p.current_command in idle_commands:
                    p.status = "idle"
                else:
                    p.status = "running"
                    window_has_running = True
            
            # Aggregate window status from panes
            if window_has_prompt:
                w.status = "waiting_for_input"
            elif window_has_running:
                w.status = "running"
            else:
                w.status = "idle"


async def _detect_worktree_status(worktrees: list[Worktree]) -> None:
    """Cross-reference worktree paths with pane commands to set status."""
    try:
        sessions = await backend.get_hierarchy()
        # Detect pane-level statuses and prompts
        await _detect_pane_statuses(sessions)
    except Exception:
        return
    idle_commands = {"bash", "zsh", "fish", "sh", "tmux", "login"}

    for wt in worktrees:
        if wt.is_main:
            continue
        wt_norm = os.path.normpath(wt.path)
        found_pane = False
        agent_running = False
        has_prompt = False
        for s in sessions:
            for w in s.windows:
                for p in w.panes:
                    pane_path = os.path.normpath(p.current_path) if p.current_path else ""
                    if pane_path.startswith(wt_norm):
                        found_pane = True
                        if p.current_command not in idle_commands:
                            agent_running = True
                        if p.needs_prompt:
                            has_prompt = True
        if has_prompt:
            wt.status = "waiting_for_input"
        elif agent_running:
            wt.status = "running"
        elif found_pane:
            wt.status = "done"


async def _capture_agent_output(branch: str) -> str | None:
    """Capture pane output for a worktree branch and save to .worktrees/<branch>.log."""
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
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(captured_lines))
    return log_path


async def _find_window(window_id: str) -> Window:
    sessions = await backend.get_hierarchy()
    for s in sessions:
        for w in s.windows:
            if w.window_id == window_id:
                return w
    raise ValueError(f"Window {window_id} not found")


async def _find_panes(window_id: str) -> list[Pane]:
    return (await _find_window(window_id)).panes


def _render_pane_image(ansi_text: str, cols: int, rows: int):
    """Parse ANSI text via pyte and render to a PIL Image."""
    try:
        import pyte
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise RuntimeError(
            'screenshot-window requires optional deps. Run: pip install "tmuxx[mcp]"'
        ) from e

    # Standard 16-color ANSI palette
    ansi_colors = {
        "black": (0, 0, 0),
        "red": (205, 0, 0),
        "green": (0, 205, 0),
        "brown": (205, 205, 0),
        "blue": (0, 0, 238),
        "magenta": (205, 0, 205),
        "cyan": (0, 205, 205),
        "white": (229, 229, 229),
        "default": (204, 204, 204),
    }
    ansi_bright = {
        "black": (127, 127, 127),
        "red": (255, 0, 0),
        "green": (0, 255, 0),
        "brown": (255, 255, 0),
        "blue": (92, 92, 255),
        "magenta": (255, 0, 255),
        "cyan": (0, 255, 255),
        "white": (255, 255, 255),
    }
    bg_default = (30, 30, 30)
    cell_w, cell_h = 7, 14

    def color_from_attr(color: str, bold: bool = False) -> tuple[int, int, int] | None:
        if isinstance(color, str):
            if bold and color in ansi_bright:
                return ansi_bright[color]
            return ansi_colors.get(color)
        return None

    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(ansi_text)

    img = Image.new("RGB", (cols * cell_w, rows * cell_h), bg_default)
    draw = ImageDraw.Draw(img)

    for y in range(rows):
        line = screen.buffer.get(y, {})
        for x in range(cols):
            ch = line.get(x)
            if ch is None:
                continue
            fg = color_from_attr(getattr(ch, "fg", "default"), getattr(ch, "bold", False))
            bg = color_from_attr(getattr(ch, "bg", "default"))
            if bg and bg != bg_default:
                draw.rectangle(
                    [x * cell_w, y * cell_h, (x + 1) * cell_w, (y + 1) * cell_h], fill=bg
                )
            if ch.data != " ":
                draw.text((x * cell_w, y * cell_h), ch.data, fill=fg or ansi_colors["default"])
    return img


def _composite_window(panes: list[Pane], captured: dict[str, str]):
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise RuntimeError(
            'screenshot-window requires optional deps. Run: pip install "tmuxx[mcp]"'
        ) from e

    border_px = 1
    cell_w, cell_h = 7, 14
    border_color = (80, 80, 80)

    min_l = min((p.left for p in panes), default=0)
    min_t = min((p.top for p in panes), default=0)
    max_r = max((p.left + p.width for p in panes), default=80)
    max_b = max((p.top + p.height for p in panes), default=24)

    img_w = (max_r - min_l) * cell_w + border_px * 2
    img_h = (max_b - min_t) * cell_h + border_px * 2
    img = Image.new("RGB", (img_w, img_h), border_color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, img_w - 1, img_h - 1], outline=border_color)

    for pane in panes:
        ansi = captured.get(pane.pane_id, "")
        pane_img = _render_pane_image(ansi, pane.width, pane.height)
        px = (pane.left - min_l) * cell_w + border_px
        py = (pane.top - min_t) * cell_h + border_px
        img.paste(pane_img, (px, py))

    return img


async def _cmd_list_sessions(_: argparse.Namespace) -> list[dict[str, Any]]:
    sessions = await backend.get_hierarchy()
    # Detect pane-level statuses and prompt needs
    await _detect_pane_statuses(sessions)
    return [_serialize_session(s) for s in sessions]


async def _cmd_capture_pane(args: argparse.Namespace) -> str:
    _bound(args.lines, 1, 5000, "lines")
    raw = await backend.capture_pane(_safe_id(args.pane_id), lines=args.lines)
    return _strip_ansi(raw)


async def _cmd_capture_window(args: argparse.Namespace) -> dict[str, str]:
    panes = await _find_panes(_safe_id(args.window_id))
    captured = await backend.capture_window_panes(panes)
    return {pid: _strip_ansi(text) for pid, text in captured.items()}


async def _cmd_screenshot_window(args: argparse.Namespace) -> dict[str, Any]:
    window_id = _safe_id(args.window_id)
    win = await _find_window(window_id)
    captured = await backend.capture_window_panes(win.panes)
    img = _composite_window(win.panes, captured)

    output_path = args.output
    if not output_path:
        output_path = os.path.abspath(f"tmuxx-{window_id[1:]}.png")
    else:
        output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    img.save(output_path, format="PNG")

    return {
        "window_id": window_id,
        "window_name": win.name,
        "pane_count": len(win.panes),
        "path": output_path,
    }


async def _cmd_create_session(args: argparse.Namespace) -> str:
    await backend.new_session(args.name)
    return f"Created session '{args.name}'"


async def _cmd_kill_session(args: argparse.Namespace) -> str:
    await backend.kill_session(args.name)
    return f"Killed session '{args.name}'"


async def _cmd_rename_session(args: argparse.Namespace) -> str:
    await backend.rename_session(args.old_name, args.new_name)
    return f"Renamed session '{args.old_name}' to '{args.new_name}'"


async def _cmd_create_window(args: argparse.Namespace) -> str:
    await backend.new_window(args.session_name, args.name)
    label = f" '{args.name}'" if args.name else ""
    return f"Created window{label} in session '{args.session_name}'"


async def _cmd_kill_window(args: argparse.Namespace) -> str:
    await backend.kill_window(_safe_id(args.window_id))
    return f"Killed window {args.window_id}"


async def _cmd_rename_window(args: argparse.Namespace) -> str:
    await backend.rename_window(_safe_id(args.window_id), args.new_name)
    return f"Renamed window {args.window_id} to '{args.new_name}'"


async def _cmd_split_pane(args: argparse.Namespace) -> str:
    await backend.split_pane(_safe_id(args.pane_id), horizontal=args.horizontal)
    direction = "horizontally" if args.horizontal else "vertically"
    return f"Split pane {args.pane_id} {direction}"


async def _cmd_kill_pane(args: argparse.Namespace) -> str:
    await backend.kill_pane(_safe_id(args.pane_id))
    return f"Killed pane {args.pane_id}"


async def _cmd_resize_pane(args: argparse.Namespace) -> str:
    _bound(args.amount, 1, 200, "amount")
    direction: Literal["up", "down", "left", "right"] = args.direction
    await backend.resize_pane(_safe_id(args.pane_id), direction, args.amount)
    return f"Resized pane {args.pane_id} {direction} by {args.amount}"


async def _send_text(pane_id: str, text: str, press_enter: bool = False) -> None:
    # Use literal mode so shell builtins and arbitrary text are passed through unchanged.
    await TmuxBackend._run("tmux", "send-keys", "-t", pane_id, "-l", text)
    if press_enter:
        await TmuxBackend._run("tmux", "send-keys", "-t", pane_id, "Enter")


async def _cmd_send_command(args: argparse.Namespace) -> str:
    safe_id = _safe_id(args.pane_id)
    command = _join_text_parts(args.command_parts, "command")
    await _send_text(safe_id, command, press_enter=True)
    return f"Sent command to {args.pane_id}: {command}"


async def _cmd_send_keys(args: argparse.Namespace) -> str:
    safe_id = _safe_id(args.pane_id)
    key_text = _join_text_parts(args.keys_parts, "keys")
    if args.literal:
        await _send_text(safe_id, key_text, press_enter=False)
        return f"Sent literal text to {args.pane_id}: {key_text}"
    if not _TMUX_KEY_RE.match(key_text):
        raise ValueError(
            f"Invalid key sequence: {key_text!r}. Use --literal for arbitrary text."
        )
    await TmuxBackend._run("tmux", "send-keys", "-t", safe_id, *key_text.split())
    return f"Sent keys to {args.pane_id}: {key_text}"


async def _cmd_send_text(args: argparse.Namespace) -> str:
    safe_id = _safe_id(args.pane_id)
    text = _join_text_parts(args.text_parts, "text")
    await _send_text(safe_id, text, press_enter=False)
    return f"Sent text to {args.pane_id}: {text}"


async def _cmd_run_and_capture(args: argparse.Namespace) -> str:
    _bound(args.lines, 1, 5000, "lines")
    if args.wait_seconds < 0 or args.wait_seconds > 30:
        raise ValueError(
            f"wait_seconds must be between 0 and 30, got {args.wait_seconds}"
        )
    safe_id = _safe_id(args.pane_id)
    command = _join_text_parts(args.command_parts, "command")
    token = uuid4().hex
    start_marker = f"__TMUXX_RUN_START_{token}__"
    end_marker = f"__TMUXX_RUN_END_{token}__"

    # Bound output to this invocation by printing unique sentinels around the command.
    wrapped = f"printf '{start_marker}\\n'; {command}; printf '{end_marker}\\n'"
    await _send_text(safe_id, wrapped, press_enter=True)

    deadline = asyncio.get_event_loop().time() + args.wait_seconds
    poll_interval = 0.2
    latest = ""
    while True:
        raw = await backend.capture_pane(safe_id, lines=5000)
        latest = _strip_ansi(raw)
        if end_marker in latest or asyncio.get_event_loop().time() >= deadline:
            break
        await asyncio.sleep(poll_interval)

    scoped = _extract_between_markers(latest, start_marker, end_marker)
    if scoped is not None and scoped.strip():
        return scoped

    # Fallback: keep old behavior when sentinels are unavailable in scrollback.
    raw = await backend.capture_pane(safe_id, lines=args.lines)
    return _strip_ansi(raw)


async def _cmd_list_worktrees(_: argparse.Namespace) -> list[dict[str, Any]]:
    wts = await git.list_worktrees()
    await _detect_worktree_status(wts)
    return [_serialize_worktree(wt) for wt in wts]


async def _cmd_diff_worktree(args: argparse.Namespace) -> str:
    diff = await git.diff_worktree(args.branch)
    return diff or "(no changes)"


async def _launch_agent(
    session_name: str,
    prompt: str,
    branch: str | None,
    base_branch: str | None,
    agent_command: str,
) -> dict[str, Any]:
    branch_name = branch or slugify(prompt)
    wt_path = await git.create_worktree(branch_name, base_branch=base_branch)
    await backend.new_window_in_dir(session_name, wt_path, branch_name)
    sessions = await backend.get_hierarchy()
    for s in sessions:
        if s.name == session_name:
            for w in s.windows:
                if w.name == branch_name and w.panes:
                    await backend.send_keys(
                        w.panes[0].pane_id, f"{agent_command} {quote(prompt)}"
                    )
                    return {
                        "branch": branch_name,
                        "session_name": session_name,
                        "worktree_path": wt_path,
                        "launched": True,
                        "message": f"Agent launched on branch '{branch_name}' in {wt_path}",
                    }
    return {
        "branch": branch_name,
        "session_name": session_name,
        "worktree_path": wt_path,
        "launched": False,
        "message": f"Worktree created at {wt_path} but could not find new window",
    }


async def _cmd_launch_agent(args: argparse.Namespace) -> dict[str, Any]:
    return await _launch_agent(
        session_name=args.session_name,
        prompt=args.prompt,
        branch=args.branch,
        base_branch=args.base_branch,
        agent_command=args.agent_command,
    )


async def _merge_worktree(
    branch: str, commit_message: str | None, test_command: str | None
) -> dict[str, Any]:
    log_path = await _capture_agent_output(branch)
    await git.merge_worktree(branch, commit_message, test_command=test_command)
    message = f"Merged '{branch}' into main and cleaned up"
    if log_path:
        message += f"\nAgent output saved to {log_path}"
    return {"branch": branch, "log_path": log_path, "message": message}


async def _cmd_merge_worktree(args: argparse.Namespace) -> dict[str, Any]:
    return await _merge_worktree(
        branch=args.branch,
        commit_message=args.commit_message,
        test_command=args.test_command,
    )


async def _discard_worktree(branch: str) -> dict[str, Any]:
    log_path = await _capture_agent_output(branch)
    await git.discard_worktree(branch)
    message = f"Discarded worktree and branch '{branch}'"
    if log_path:
        message += f"\nAgent output saved to {log_path}"
    return {"branch": branch, "log_path": log_path, "message": message}


async def _cmd_discard_worktree(args: argparse.Namespace) -> dict[str, Any]:
    return await _discard_worktree(args.branch)


async def _cmd_read_agent_log(args: argparse.Namespace) -> str:
    root = await git.get_repo_root()
    log_path = os.path.join(root, ".worktrees", f"{args.branch}.log")
    if not os.path.exists(log_path):
        return f"No log found for branch '{args.branch}'"
    with open(log_path, encoding="utf-8") as f:
        return f.read()


async def _cmd_start_task(args: argparse.Namespace) -> dict[str, Any]:
    """Deterministic workflow: create worktree + window + run agent command."""
    return await _launch_agent(
        session_name=args.session_name,
        prompt=args.prompt,
        branch=args.branch,
        base_branch=args.base_branch,
        agent_command=args.agent_command,
    )


async def _cmd_complete_task(args: argparse.Namespace) -> dict[str, Any]:
    """Deterministic workflow: capture output + optional test + merge + cleanup."""
    return await _merge_worktree(
        branch=args.branch,
        commit_message=args.commit_message,
        test_command=args.test_command,
    )


async def _cmd_abort_task(args: argparse.Namespace) -> dict[str, Any]:
    """Deterministic workflow: capture output + discard worktree/branch."""
    return await _discard_worktree(args.branch)


async def _cmd_task_report(args: argparse.Namespace) -> dict[str, Any]:
    """
    Summarize one task branch with deep pane-level activity insights.
    Includes worktree status, pane statuses, and prompt notifications.
    """
    root = await git.get_repo_root()
    wts = await git.list_worktrees()
    await _detect_worktree_status(wts)
    wt = next((w for w in wts if w.branch == args.branch), None)
    diff = await git.diff_worktree(args.branch)
    log_path = os.path.join(root, ".worktrees", f"{args.branch}.log")
    
    # Get pane-level details for this worktree
    pane_details: list[dict[str, Any]] = []
    if wt:
        wt_norm = os.path.normpath(wt.path)
        try:
            sessions = await backend.get_hierarchy()
            await _detect_pane_statuses(sessions)
            
            for s in sessions:
                for w in s.windows:
                    for p in w.panes:
                        pane_path = os.path.normpath(p.current_path) if p.current_path else ""
                        if pane_path.startswith(wt_norm):
                            pane_details.append({
                                "pane_id": p.pane_id,
                                "window_name": w.name,
                                "command": p.current_command,
                                "status": p.status,
                                "needs_prompt": p.needs_prompt,
                            })
        except Exception:
            pass
    
    return {
        "branch": args.branch,
        "worktree": _serialize_worktree(wt) if wt else None,
        "pane_details": pane_details,
        "diff": diff or "(no changes)",
        "log_exists": os.path.exists(log_path),
        "log_path": log_path,
    }


def _add_json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmuxx agent",
        description="Deterministic tmuxx automation commands for agent workflows.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-sessions", help="List all tmux sessions/windows/panes")
    _add_json_flag(p)

    p = sub.add_parser("capture-pane", help="Capture text from a tmux pane")
    p.add_argument("pane_id")
    p.add_argument("--lines", type=int, default=50)
    _add_json_flag(p)

    p = sub.add_parser("capture-window", help="Capture text from all panes in a window")
    p.add_argument("window_id")
    _add_json_flag(p)

    p = sub.add_parser("screenshot-window", help="Render all panes in a window into a PNG")
    p.add_argument("window_id")
    p.add_argument("--output", help="Output PNG path (default: ./tmuxx-<id>.png)")
    _add_json_flag(p)

    p = sub.add_parser("create-session", help="Create a tmux session")
    p.add_argument("name")
    _add_json_flag(p)

    p = sub.add_parser("kill-session", help="Kill a tmux session")
    p.add_argument("name")
    _add_json_flag(p)

    p = sub.add_parser("rename-session", help="Rename a tmux session")
    p.add_argument("old_name")
    p.add_argument("new_name")
    _add_json_flag(p)

    p = sub.add_parser("create-window", help="Create a tmux window in a session")
    p.add_argument("session_name")
    p.add_argument("--name")
    _add_json_flag(p)

    p = sub.add_parser("kill-window", help="Kill a tmux window")
    p.add_argument("window_id")
    _add_json_flag(p)

    p = sub.add_parser("rename-window", help="Rename a tmux window")
    p.add_argument("window_id")
    p.add_argument("new_name")
    _add_json_flag(p)

    p = sub.add_parser("split-pane", help="Split a tmux pane")
    p.add_argument("pane_id")
    p.add_argument("--horizontal", action="store_true")
    _add_json_flag(p)

    p = sub.add_parser("kill-pane", help="Kill a tmux pane")
    p.add_argument("pane_id")
    _add_json_flag(p)

    p = sub.add_parser("resize-pane", help="Resize a tmux pane")
    p.add_argument("pane_id")
    p.add_argument("direction", choices=["up", "down", "left", "right"])
    p.add_argument("--amount", type=int, default=5)
    _add_json_flag(p)

    p = sub.add_parser("send-command", help="Send command + Enter to pane")
    p.add_argument("pane_id")
    p.add_argument(
        "command_parts",
        nargs="+",
        help="Command text to send. For robustness, pass command after '--'.",
    )
    _add_json_flag(p)

    p = sub.add_parser("send-keys", help="Send raw keys to pane (no Enter)")
    p.add_argument("pane_id")
    p.add_argument(
        "--literal",
        action="store_true",
        help="Treat keys as plain text instead of tmux key names.",
    )
    p.add_argument(
        "keys_parts",
        nargs="+",
        help="Key names (e.g. C-c Enter) or literal text with --literal.",
    )
    _add_json_flag(p)

    p = sub.add_parser("send-text", help="Send literal text to pane (no Enter)")
    p.add_argument("pane_id")
    p.add_argument(
        "text_parts",
        nargs="+",
        help="Literal text to send. For robustness, pass text after '--'.",
    )
    _add_json_flag(p)

    p = sub.add_parser("run-and-capture", help="Send command, wait, then capture output")
    p.add_argument("pane_id")
    p.add_argument("--wait-seconds", type=float, default=1.0)
    p.add_argument("--lines", type=int, default=50)
    p.add_argument(
        "command_parts",
        nargs="+",
        help="Command text to send. Put options before '--'.",
    )
    _add_json_flag(p)

    p = sub.add_parser("list-worktrees", help="List git worktrees with inferred status")
    _add_json_flag(p)

    p = sub.add_parser("diff-worktree", help="Show git diff of branch against main")
    p.add_argument("branch")
    _add_json_flag(p)

    p = sub.add_parser("launch-agent", help="Create worktree + window + launch agent command")
    p.add_argument("session_name")
    p.add_argument("prompt")
    p.add_argument("--branch")
    p.add_argument("--base-branch")
    p.add_argument("--agent-command", default="claude -p")
    _add_json_flag(p)

    p = sub.add_parser("merge-worktree", help="Merge worktree into main and clean up")
    p.add_argument("branch")
    p.add_argument("--commit-message")
    p.add_argument("--test-command")
    _add_json_flag(p)

    p = sub.add_parser("discard-worktree", help="Force-remove worktree and delete branch")
    p.add_argument("branch")
    _add_json_flag(p)

    p = sub.add_parser("read-agent-log", help="Read saved .worktrees/<branch>.log")
    p.add_argument("branch")
    _add_json_flag(p)

    # Deterministic workflow commands (recommended by skill)
    p = sub.add_parser("start-task", help="Workflow: launch a task in a new worktree")
    p.add_argument("session_name")
    p.add_argument("prompt")
    p.add_argument("--branch")
    p.add_argument("--base-branch")
    p.add_argument("--agent-command", default="claude -p")
    _add_json_flag(p)

    p = sub.add_parser("complete-task", help="Workflow: merge a finished task branch")
    p.add_argument("branch")
    p.add_argument("--commit-message")
    p.add_argument("--test-command")
    _add_json_flag(p)

    p = sub.add_parser("abort-task", help="Workflow: discard an unfinished task branch")
    p.add_argument("branch")
    _add_json_flag(p)

    p = sub.add_parser("task-report", help="Workflow: summarize branch status/diff/log")
    p.add_argument("branch")
    _add_json_flag(p)

    return parser


_COMMANDS: dict[str, Any] = {
    "list-sessions": _cmd_list_sessions,
    "capture-pane": _cmd_capture_pane,
    "capture-window": _cmd_capture_window,
    "screenshot-window": _cmd_screenshot_window,
    "create-session": _cmd_create_session,
    "kill-session": _cmd_kill_session,
    "rename-session": _cmd_rename_session,
    "create-window": _cmd_create_window,
    "kill-window": _cmd_kill_window,
    "rename-window": _cmd_rename_window,
    "split-pane": _cmd_split_pane,
    "kill-pane": _cmd_kill_pane,
    "resize-pane": _cmd_resize_pane,
    "send-command": _cmd_send_command,
    "send-keys": _cmd_send_keys,
    "send-text": _cmd_send_text,
    "run-and-capture": _cmd_run_and_capture,
    "list-worktrees": _cmd_list_worktrees,
    "diff-worktree": _cmd_diff_worktree,
    "launch-agent": _cmd_launch_agent,
    "merge-worktree": _cmd_merge_worktree,
    "discard-worktree": _cmd_discard_worktree,
    "read-agent-log": _cmd_read_agent_log,
    "start-task": _cmd_start_task,
    "complete-task": _cmd_complete_task,
    "abort-task": _cmd_abort_task,
    "task-report": _cmd_task_report,
}


def _print_output(result: Any, as_json: bool, command: str) -> None:
    if as_json:
        payload = {"ok": True, "command": command, "data": result}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if isinstance(result, str):
        print(result)
        return
    print(json.dumps(result, indent=2, ensure_ascii=False))


def _print_error(err: Exception, as_json: bool, command: str | None) -> None:
    if as_json:
        payload = {"ok": False, "command": command, "error": str(err)}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"Error: {err}", file=sys.stderr)


def run_agent_cli(argv: list[str]) -> int:
    """Parse and execute `tmuxx agent ...` commands."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", None)
    as_json = bool(getattr(args, "json", False))
    runner = _COMMANDS.get(command)
    if runner is None:
        _print_error(ValueError(f"Unknown command: {command}"), as_json, command)
        return 2
    try:
        result = asyncio.run(runner(args))
    except Exception as e:
        _print_error(e, as_json, command)
        return 1
    _print_output(result, as_json, command)
    return 0
