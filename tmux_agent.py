"""tmuxx agent CLI: deterministic, JSON-friendly tmuxx automation surface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import textwrap
import time
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import Any, Literal
from uuid import uuid4

from tmux_core import (
    GitBackend,
    Pane,
    Session,
    TmuxBackend,
    Window,
    Worktree,
    classify_pane_status,
    path_within,
    quote,
    resolve_agent_command,
    slugify,
)
from tmux_mission import (
    create_mission_state,
    format_mission_handoff,
    load_latest_mission_state,
    load_mission_state,
    mission_needs_handoff,
    mission_state_path,
    save_mission_state,
    summarize_mission,
)


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

                # Determine pane status
                p.status, p.needs_prompt = classify_pane_status(p.current_command, recent_output)
                if p.status == "waiting_for_input":
                    window_has_prompt = True
                elif p.status == "running":
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
                    if path_within(pane_path, wt_norm):
                        found_pane = True
                        if p.status == "running":
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
                if path_within(pane_path, wt_path):
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
    agent_command: str | None,
) -> dict[str, Any]:
    branch_name = branch or slugify(prompt)
    resolved_agent_command = resolve_agent_command(agent_command)
    wt_path = await git.create_worktree(branch_name, base_branch=base_branch)
    await backend.new_window_in_dir(session_name, wt_path, branch_name)
    sessions = await backend.get_hierarchy()
    for s in sessions:
        if s.name == session_name:
            for w in s.windows:
                if w.name == branch_name and w.panes:
                    await backend.send_keys(
                        w.panes[0].pane_id,
                        f"{resolved_agent_command} {quote(prompt)}",
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
                        if path_within(pane_path, wt_norm):
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


async def _cmd_status(_: argparse.Namespace) -> list[dict[str, Any]]:
    """Unified status of all running agents across worktrees and sessions."""
    wts = await git.list_worktrees()
    await _detect_worktree_status(wts)
    try:
        sessions = await backend.get_hierarchy()
        await _detect_pane_statuses(sessions)
    except Exception:
        sessions = []

    result: list[dict[str, Any]] = []
    for wt in wts:
        if wt.is_main:
            continue
        wt_norm = os.path.normpath(wt.path)
        panes: list[dict[str, Any]] = []
        for s in sessions:
            for w in s.windows:
                for p in w.panes:
                    pane_path = os.path.normpath(p.current_path) if p.current_path else ""
                    if path_within(pane_path, wt_norm):
                        try:
                            recent = await backend.capture_pane(p.pane_id, lines=5)
                            last_line = _strip_ansi(recent).rstrip().splitlines()[-1] if recent.strip() else ""
                        except Exception:
                            last_line = ""
                        panes.append({
                            "pane_id": p.pane_id,
                            "command": p.current_command,
                            "status": p.status,
                            "needs_prompt": p.needs_prompt,
                            "last_line": last_line,
                        })
        result.append({
            **_serialize_worktree(wt),
            "panes": panes,
        })
    return result


def _match_target(filter_value: str | None, tmux_id: str, name: str) -> bool:
    if not filter_value:
        return True
    return filter_value == tmux_id or filter_value == name


def _watch_scope(args: argparse.Namespace) -> dict[str, str]:
    return {
        "session": args.session or "",
        "window": args.window or "",
        "pane": args.pane or "",
        "branch": args.branch or "",
    }


def _busy_status(status: str | None) -> bool:
    return status in {"running", "waiting_for_input"}


def _attention_terminal_status(status: str | None) -> bool:
    return status in {"idle", "waiting_for_input"}


def _compile_watch_pattern(args: argparse.Namespace) -> re.Pattern[str] | None:
    return (
        re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
        if args.pattern
        else None
    )


def _validate_watch_args(args: argparse.Namespace) -> None:
    if args.pane:
        args.pane = _safe_id(args.pane)
    _bound(int(args.interval * 1000), 100, 3600 * 1000, "interval_ms")
    if args.timeout is not None and args.timeout < 0:
        raise ValueError("timeout must be >= 0")
    if args.event == "text" and not args.pattern:
        raise ValueError("--pattern is required when --event=text")


def _make_watch_payload(
    args: argparse.Namespace,
    event: str,
    matches: list[dict[str, Any]],
    started_at: float,
    iterations: int,
) -> dict[str, Any]:
    return {
        "event": event,
        "matched": True,
        "matched_at": int(time.time()),
        "elapsed_seconds": round(time.time() - started_at, 3),
        "iterations": iterations,
        "filters": _watch_scope(args),
        "match_count": len(matches),
        "matches": matches,
    }


def _watch_match_signature(matches: list[dict[str, Any]]) -> str:
    normalized: list[dict[str, Any]] = []
    for match in matches:
        normalized.append(
            {
                "pane_id": match.get("pane_id", ""),
                "status": match.get("status", ""),
                "needs_prompt": bool(match.get("needs_prompt", False)),
                "recent_output_tail": (match.get("recent_output") or "")[-1000:],
            }
        )
    normalized.sort(key=lambda item: str(item["pane_id"]))
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


async def _remaining_timeout(deadline: float | None, label: str) -> float:
    if deadline is None:
        return 0.0
    remaining = deadline - asyncio.get_event_loop().time()
    if remaining <= 0:
        raise TimeoutError(f"{label} timed out")
    return remaining


async def _watch_until_match(args: argparse.Namespace) -> dict[str, Any]:
    _validate_watch_args(args)
    compiled_pattern = _compile_watch_pattern(args)
    deadline = None if not args.timeout else (asyncio.get_event_loop().time() + args.timeout)
    started_at = time.time()
    seen_busy = bool(getattr(args, "assume_busy", False))
    iterations = 0

    while True:
        iterations += 1
        panes = await _collect_watch_snapshot(args)
        matched, matches, seen_busy = _evaluate_watch_event(
            args.event,
            panes,
            compiled_pattern,
            seen_busy,
        )
        if matched:
            payload = _make_watch_payload(args, args.event, matches, started_at, iterations)
            if getattr(args, "notify", False):
                payload["notification"] = await _run_watch_notification(payload)
            if getattr(args, "exec_command", None):
                payload["exec"] = await _run_watch_exec(args.exec_command, payload)
            return payload

        if deadline is not None and asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"watch timed out waiting for event '{args.event}' with filters {_watch_scope(args)}"
            )

        await asyncio.sleep(args.interval)


def _build_supervise_watch_args(
    args: argparse.Namespace,
    *,
    event: str | None = None,
    timeout: float | None = None,
    notify: bool | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        session=args.worker_session,
        window=args.worker_window,
        pane=args.worker_pane,
        branch=args.worker_branch,
        event=event or args.event,
        pattern=args.pattern,
        ignore_case=args.ignore_case,
        interval=args.interval,
        timeout=args.timeout if timeout is None else timeout,
        capture_lines=args.capture_lines,
        notify=args.notify if notify is None else notify,
        exec_command=None,
        assume_busy=args.assume_busy,
    )


async def _wait_for_supervise_trigger(
    args: argparse.Namespace,
    deadline: float | None,
) -> dict[str, Any]:
    return await _watch_until_match(
        _build_supervise_watch_args(
            args,
            timeout=await _remaining_timeout(deadline, "supervise") if deadline is not None else 0.0,
        )
    )


async def _wait_for_supervise_rearm(
    args: argparse.Namespace,
    previous_trigger: dict[str, Any],
    deadline: float | None,
) -> dict[str, Any] | None:
    watch_args = _build_supervise_watch_args(args, timeout=0.0, notify=False)
    _validate_watch_args(watch_args)
    compiled_pattern = _compile_watch_pattern(watch_args)
    previous_signature = _watch_match_signature(previous_trigger.get("matches") or [])
    started_at = time.time()
    iterations = 0

    while True:
        if deadline is not None:
            await _remaining_timeout(deadline, "supervise")
        iterations += 1
        panes = await _collect_watch_snapshot(watch_args)
        matched, matches, _ = _evaluate_watch_event(
            args.event,
            panes,
            compiled_pattern,
            True,
        )
        if not matched:
            return None
        if _watch_match_signature(matches) != previous_signature:
            return _make_watch_payload(watch_args, args.event, matches, started_at, iterations)
        await asyncio.sleep(args.interval)


def _format_supervise_prompt(trigger: dict[str, Any], goal: str | None) -> str:
    filters = trigger.get("filters") or {}
    lines = [
        "tmuxx supervision handoff",
        "",
        f"Detected event: {trigger.get('event', '')}",
        (
            "Worker filters: "
            f"session={filters.get('session', '') or '*'} "
            f"window={filters.get('window', '') or '*'} "
            f"pane={filters.get('pane', '') or '*'} "
            f"branch={filters.get('branch', '') or '*'}"
        ),
    ]
    if goal:
        lines.append(f"Original goal: {goal}")
    lines.extend([
        "",
        "Matched worker panes:",
    ])

    matches = trigger.get("matches") or []
    for match in matches:
        lines.extend(
            [
                f"- pane: {match.get('pane_id', '')}",
                f"  session: {match.get('session_name', '')}",
                f"  window: {match.get('window_name', '')}",
                f"  branch: {match.get('branch', '')}",
                f"  status: {match.get('status', '')}",
                f"  needs_prompt: {bool(match.get('needs_prompt', False))}",
                "  recent output:",
                textwrap.indent((match.get("recent_output") or "<no captured output>").rstrip() or "<no captured output>", "    "),
            ]
        )

    lines.extend(
        [
            "",
            "Please inspect the worker pane, decide whether it is blocked or incomplete, and keep driving it until the original goal is fully done.",
        ]
    )
    return "\n".join(lines)


async def _send_supervise_handoff(
    supervisor_pane: str,
    trigger: dict[str, Any],
    goal: str | None,
) -> dict[str, Any]:
    matches = trigger.get("matches") or []
    if any(m.get("pane_id") == supervisor_pane for m in matches):
        raise ValueError("supervisor-pane cannot match the watched worker pane")
    prompt = _format_supervise_prompt(trigger, goal)
    await _send_text(supervisor_pane, prompt, press_enter=True)
    return {
        "event": trigger.get("event"),
        "matched": True,
        "match_count": trigger.get("match_count", 0),
        "supervisor_pane": supervisor_pane,
        "worker_panes": [m.get("pane_id") for m in matches],
        "prompt_sent": True,
        "trigger": trigger,
    }


def _build_supervise_result(
    supervisor_pane: str,
    handoffs: list[dict[str, Any]],
    continuous: bool,
) -> dict[str, Any]:
    last = handoffs[-1]
    result = {
        "event": last.get("event"),
        "matched": True,
        "match_count": last.get("match_count", 0),
        "supervisor_pane": supervisor_pane,
        "worker_panes": last.get("worker_panes", []),
        "prompt_sent": True,
        "trigger": last.get("trigger"),
        "continuous": continuous,
        "handoff_count": len(handoffs),
    }
    if continuous:
        result["handoffs"] = handoffs
    return result


async def _cmd_supervise(args: argparse.Namespace) -> dict[str, Any]:
    supervisor_pane = _safe_id(args.supervisor_pane)
    if args.max_handoffs < 0:
        raise ValueError("max_handoffs must be >= 0")
    deadline = None if not args.timeout else (asyncio.get_event_loop().time() + args.timeout)
    handoffs: list[dict[str, Any]] = []
    trigger = await _wait_for_supervise_trigger(args, deadline)

    while True:
        handoff = await _send_supervise_handoff(supervisor_pane, trigger, args.goal)
        handoffs.append(handoff)
        if not args.continuous or (args.max_handoffs and len(handoffs) >= args.max_handoffs):
            return _build_supervise_result(supervisor_pane, handoffs, bool(args.continuous))

        rearmed_trigger = await _wait_for_supervise_rearm(args, trigger, deadline)
        if rearmed_trigger is not None:
            trigger = rearmed_trigger
        else:
            trigger = await _wait_for_supervise_trigger(args, deadline)


def _build_all_panes_snapshot_args(capture_lines: int) -> argparse.Namespace:
    return argparse.Namespace(
        session=None,
        window=None,
        pane=None,
        branch=None,
        event="attention",
        pattern=None,
        ignore_case=True,
        interval=2.0,
        timeout=0.0,
        capture_lines=capture_lines,
        notify=False,
        exec_command=None,
        assume_busy=True,
    )


async def _load_mission_for_args(
    mission_id: str | None,
) -> tuple[str, dict[str, Any], str]:
    repo_root = await git.get_repo_root()
    if mission_id:
        mission = load_mission_state(repo_root, mission_id)
    else:
        mission = load_latest_mission_state(repo_root)
        if mission is None:
            raise ValueError("no mission found; run `tmuxx agent mission start ...` first")
    path = mission_state_path(repo_root, str(mission["mission_id"]))
    return repo_root, mission, str(path)


async def _mission_summary_for_args(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], str]:
    _, mission, path = await _load_mission_for_args(getattr(args, "mission_id", None))
    panes = await _collect_watch_snapshot(
        _build_all_panes_snapshot_args(getattr(args, "capture_lines", 120))
    )
    return mission, summarize_mission(mission, panes), path


def _mission_signature(summary: dict[str, Any]) -> str:
    normalized = {
        "status": summary.get("status"),
        "workers": [
            {
                "role": worker.get("role"),
                "status": worker.get("status"),
                "pane_ids": worker.get("pane_ids", []),
                "needs_prompt": worker.get("needs_prompt", False),
            }
            for worker in summary.get("workers", [])
        ],
    }
    return json.dumps(normalized, sort_keys=True)


async def _cmd_mission_start(args: argparse.Namespace) -> dict[str, Any]:
    supervisor_pane = _safe_id(args.supervisor_pane)
    repo_root = await git.get_repo_root()
    mission = create_mission_state(
        args.goal,
        supervisor_pane,
        args.worker,
        mission_id=args.mission_id,
    )
    path = save_mission_state(repo_root, mission)
    return {
        "mission": mission,
        "path": str(path),
        "message": (
            f"Mission '{mission['mission_id']}' started with supervisor "
            f"{mission['supervisor_pane']} and {len(mission['workers'])} workers"
        ),
    }


async def _cmd_mission_status(args: argparse.Namespace) -> dict[str, Any]:
    mission, summary, path = await _mission_summary_for_args(args)
    return {
        "mission": mission,
        "summary": summary,
        "path": path,
    }


async def _cmd_mission_supervise(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_handoffs < 0:
        raise ValueError("max_handoffs must be >= 0")
    if args.interval < 0.1:
        raise ValueError("interval must be >= 0.1")

    deadline = None if not args.timeout else (asyncio.get_event_loop().time() + args.timeout)
    seen_busy = bool(args.assume_busy)
    handoffs: list[dict[str, Any]] = []
    last_signature = ""

    while True:
        if deadline is not None and asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError("mission supervise timed out")

        mission, summary, path = await _mission_summary_for_args(args)
        counts = summary.get("counts", {})
        seen_busy = seen_busy or bool(counts.get("running") or counts.get("waiting_for_input"))
        signature = _mission_signature(summary)

        if mission_needs_handoff(summary, seen_busy=seen_busy) and signature != last_signature:
            supervisor_pane = _safe_id(str(mission["supervisor_pane"]))
            for worker in summary.get("workers", []):
                if supervisor_pane in (worker.get("pane_ids") or []):
                    raise ValueError("supervisor pane cannot also be a mission worker")
            prompt = format_mission_handoff(summary)
            await _send_text(supervisor_pane, prompt, press_enter=True)
            handoff = {
                "mission_id": mission["mission_id"],
                "supervisor_pane": supervisor_pane,
                "prompt_sent": True,
                "summary": summary,
                "path": path,
            }
            handoffs.append(handoff)
            last_signature = signature
            if not args.continuous or (args.max_handoffs and len(handoffs) >= args.max_handoffs):
                return {
                    "mission_id": mission["mission_id"],
                    "prompt_sent": True,
                    "handoff_count": len(handoffs),
                    "continuous": bool(args.continuous),
                    "handoffs": handoffs,
                }

        await asyncio.sleep(args.interval)


async def _collect_watch_snapshot(args: argparse.Namespace) -> list[dict[str, Any]]:
    sessions = await backend.get_hierarchy()
    await _detect_pane_statuses(sessions)
    worktrees = await git.list_worktrees()
    worktree_prefixes = [
        (os.path.normpath(wt.path), wt.branch)
        for wt in worktrees
    ]

    panes: list[dict[str, Any]] = []
    capture_lines = _bound(args.capture_lines, 1, 5000, "capture_lines")

    for s in sessions:
        if not _match_target(args.session, s.session_id, s.name):
            continue
        for w in s.windows:
            if not _match_target(args.window, w.window_id, w.name):
                continue
            for p in w.panes:
                if args.pane and p.pane_id != args.pane:
                    continue

                branch = ""
                if p.current_path:
                    best_len = -1
                    for prefix, candidate_branch in worktree_prefixes:
                        if path_within(p.current_path, prefix) and len(prefix) > best_len:
                            branch = candidate_branch
                            best_len = len(prefix)
                if args.branch and branch != args.branch:
                    continue

                recent_output = p.recent_output
                if capture_lines != 50:
                    try:
                        recent_output = await backend.capture_pane(p.pane_id, lines=capture_lines)
                    except Exception:
                        recent_output = ""

                status, needs_prompt = classify_pane_status(p.current_command, recent_output)

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
                        "branch": branch,
                        "status": status,
                        "needs_prompt": needs_prompt,
                        "recent_output": _strip_ansi(recent_output or ""),
                    }
                )
    return panes


async def _run_watch_exec(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    env = os.environ.copy()
    env["TMUXX_WATCH_EVENT"] = str(payload.get("event", ""))
    env["TMUXX_WATCH_PAYLOAD"] = json.dumps(payload, ensure_ascii=False)
    matches = payload.get("matches") or []
    if matches:
        first = matches[0]
        env["TMUXX_WATCH_PANE_ID"] = str(first.get("pane_id", ""))
        env["TMUXX_WATCH_WINDOW_ID"] = str(first.get("window_id", ""))
        env["TMUXX_WATCH_WINDOW_NAME"] = str(first.get("window_name", ""))
        env["TMUXX_WATCH_SESSION_ID"] = str(first.get("session_id", ""))
        env["TMUXX_WATCH_SESSION_NAME"] = str(first.get("session_name", ""))
        env["TMUXX_WATCH_BRANCH"] = str(first.get("branch", ""))
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    return {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": stdout.decode().strip(),
        "stderr": stderr.decode().strip(),
    }


async def _run_watch_notification(payload: dict[str, Any]) -> dict[str, Any]:
    matches = payload.get("matches") or []
    first = matches[0] if matches else {}
    title = f"tmuxx watch: {payload.get('event', 'match')}"
    target = first.get("pane_id") or first.get("window_name") or first.get("session_name") or "tmux"
    session_name = first.get("session_name") or ""
    branch = first.get("branch") or ""
    details = f"{target} matched"
    if session_name:
        details += f" in {session_name}"
    if branch:
        details += f" [{branch}]"

    if sys.platform == "darwin" and shutil.which("osascript"):
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            f"display notification {json.dumps(details)} with title {json.dumps(title)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    elif sys.platform.startswith("linux") and shutil.which("notify-send"):
        proc = await asyncio.create_subprocess_exec(
            "notify-send",
            title,
            details,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        return {
            "sent": False,
            "reason": "No supported desktop notification backend found",
            "title": title,
            "message": details,
        }

    stdout, stderr = await proc.communicate()
    return {
        "sent": proc.returncode == 0,
        "title": title,
        "message": details,
        "exit_code": proc.returncode,
        "stdout": stdout.decode().strip(),
        "stderr": stderr.decode().strip(),
    }


def _evaluate_watch_event(
    event: str,
    panes: list[dict[str, Any]],
    pattern: re.Pattern[str] | None,
    seen_busy: bool,
) -> tuple[bool, list[dict[str, Any]], bool]:
    if event == "needs_prompt":
        matches = [p for p in panes if p.get("needs_prompt")]
        return bool(matches), matches, seen_busy
    if event == "running":
        matches = [p for p in panes if p.get("status") == "running"]
        return bool(matches), matches, seen_busy
    if event == "idle":
        matches = [p for p in panes if p.get("status") == "idle"]
        return bool(matches), matches, seen_busy
    if event == "text":
        matches = [
            p for p in panes
            if pattern and pattern.search(p.get("recent_output", ""))
        ]
        return bool(matches), matches, seen_busy
    if event == "completed":
        busy_now = any(_busy_status(p.get("status")) for p in panes)
        seen_busy = seen_busy or busy_now
        triggered = bool(panes) and seen_busy and all(
            p.get("status") == "idle" for p in panes
        )
        return triggered, panes if triggered else [], seen_busy
    if event == "attention":
        busy_now = any(_busy_status(p.get("status")) for p in panes)
        seen_busy = seen_busy or busy_now
        triggered = bool(panes) and seen_busy and all(
            _attention_terminal_status(p.get("status")) for p in panes
        ) and (
            any(p.get("status") == "waiting_for_input" for p in panes)
            or all(p.get("status") == "idle" for p in panes)
        )
        return triggered, panes if triggered else [], seen_busy
    raise ValueError(f"Unsupported watch event: {event}")


async def _cmd_watch(args: argparse.Namespace) -> dict[str, Any]:
    return await _watch_until_match(args)


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
    p.add_argument(
        "--agent-command",
        help=(
            "Agent CLI prefix to run. Defaults to $TMUXX_AGENT_COMMAND, "
            "otherwise 'claude -p' outside agent sessions."
        ),
    )
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
    p.add_argument(
        "--agent-command",
        help=(
            "Agent CLI prefix to run. Defaults to $TMUXX_AGENT_COMMAND, "
            "otherwise 'claude -p' outside agent sessions."
        ),
    )
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

    p = sub.add_parser("status", help="Unified status of all running agents")
    _add_json_flag(p)

    p = sub.add_parser("watch", help="Wait until a tmuxx condition matches")
    p.add_argument(
        "--event",
        choices=["needs_prompt", "running", "idle", "completed", "attention", "text"],
        default="needs_prompt",
        help="Condition to wait for (default: needs_prompt)",
    )
    p.add_argument("--session", help="Filter by session name or $session_id")
    p.add_argument("--window", help="Filter by window name or @window_id")
    p.add_argument("--pane", help="Filter by %%pane_id")
    p.add_argument("--branch", help="Filter by git worktree branch")
    p.add_argument("--pattern", help="Regex required for --event=text")
    p.add_argument("--ignore-case", action="store_true", help="Case-insensitive regex matching")
    p.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    p.add_argument("--timeout", type=float, default=0.0, help="Timeout in seconds (0 = wait forever)")
    p.add_argument("--capture-lines", type=int, default=50, help="Lines of pane output to inspect")
    p.add_argument(
        "--assume-busy",
        action="store_true",
        help="Treat completed/attention watches as already having seen a busy worker so current terminal states can match immediately",
    )
    p.add_argument("--notify", action="store_true", help="Send a desktop notification when matched")
    p.add_argument("--exec", dest="exec_command", help="Shell command to run when matched")
    _add_json_flag(p)

    p = sub.add_parser("supervise", help="Wait for a worker condition, then wake a supervisor pane")
    p.add_argument("--supervisor-pane", required=True, help="Pane that should receive the supervision handoff")
    p.add_argument(
        "--event",
        choices=["needs_prompt", "running", "idle", "completed", "attention", "text"],
        default="attention",
        help="Worker condition that should wake the supervisor (default: attention)",
    )
    p.add_argument("--worker-session", help="Filter worker by session name or $session_id")
    p.add_argument("--worker-window", help="Filter worker by window name or @window_id")
    p.add_argument("--worker-pane", help="Filter worker by %%pane_id")
    p.add_argument("--worker-branch", help="Filter worker by git worktree branch")
    p.add_argument("--pattern", help="Regex required for --event=text")
    p.add_argument("--ignore-case", action="store_true", help="Case-insensitive regex matching")
    p.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    p.add_argument("--timeout", type=float, default=0.0, help="Timeout in seconds (0 = wait forever)")
    p.add_argument("--capture-lines", type=int, default=120, help="Lines of worker pane output to inspect")
    p.add_argument(
        "--assume-busy",
        action="store_true",
        help="Treat completed/attention supervision as already having seen a busy worker so current terminal states can match immediately",
    )
    p.add_argument("--goal", help="Optional original user goal to include in the supervisor handoff")
    p.add_argument("--continuous", action="store_true", help="Keep re-arming after each handoff instead of exiting after the first one")
    p.add_argument("--max-handoffs", type=int, default=0, help="Stop after this many handoffs in continuous mode (0 = unlimited)")
    p.add_argument("--notify", action="store_true", help="Send a desktop notification when the worker condition matches")
    _add_json_flag(p)

    mission = sub.add_parser("mission", help="Mission harness for multi-agent collaboration")
    mission_sub = mission.add_subparsers(dest="mission_action", required=True)

    p = mission_sub.add_parser("start", help="Create a mission binding supervisor and worker panes")
    p.set_defaults(command="mission-start")
    p.add_argument("goal", help="User goal the supervisor should drive to completion")
    p.add_argument("--mission-id", help="Stable mission id (default: slug + suffix)")
    p.add_argument("--supervisor-pane", required=True, help="Pane representing the user/supervisor agent")
    p.add_argument(
        "--worker",
        action="append",
        default=[],
        help=(
            "Worker binding such as dev:%%1, qa=@2, review:session:claude, "
            "or qa:branch:feature. Repeat for each worker."
        ),
    )
    _add_json_flag(p)

    p = mission_sub.add_parser("status", help="Summarize a mission against current tmux panes")
    p.set_defaults(command="mission-status")
    p.add_argument("mission_id", nargs="?", help="Mission id (default: latest mission)")
    p.add_argument("--capture-lines", type=int, default=120, help="Lines of pane output to inspect")
    _add_json_flag(p)

    p = mission_sub.add_parser("supervise", help="Wake the mission supervisor when workers need attention")
    p.set_defaults(command="mission-supervise")
    p.add_argument("mission_id", nargs="?", help="Mission id (default: latest mission)")
    p.add_argument("--capture-lines", type=int, default=120, help="Lines of worker pane output to inspect")
    p.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    p.add_argument("--timeout", type=float, default=0.0, help="Timeout in seconds (0 = wait forever)")
    p.add_argument(
        "--assume-busy",
        action="store_true",
        help="Allow an already-idle mission to wake the supervisor immediately",
    )
    p.add_argument("--continuous", action="store_true", help="Keep re-arming after each handoff")
    p.add_argument("--max-handoffs", type=int, default=0, help="Stop after this many handoffs in continuous mode (0 = unlimited)")
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
    "status": _cmd_status,
    "watch": _cmd_watch,
    "supervise": _cmd_supervise,
    "mission-start": _cmd_mission_start,
    "mission-status": _cmd_mission_status,
    "mission-supervise": _cmd_mission_supervise,
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
