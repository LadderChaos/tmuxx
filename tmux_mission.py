"""Mission orchestration primitives for tmuxx agent harness workflows."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from tmux_core import slugify


_ROLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def mission_state_dir(repo_root: str) -> Path:
    """Return the repo-local mission state directory."""
    return Path(repo_root) / ".tmuxx" / "missions"


def mission_state_path(repo_root: str, mission_id: str) -> Path:
    """Return the JSON state file for a mission id."""
    return mission_state_dir(repo_root) / f"{mission_id}.json"


def parse_worker_spec(spec: str) -> dict[str, str]:
    """
    Parse a worker role binding.

    Accepted forms:
    - dev:%1
    - qa=@2
    - reviewer:branch:fix-login
    - claude:session:claude
    - codex:codex
    """
    if ":" in spec:
        role, target = spec.split(":", 1)
    elif "=" in spec:
        role, target = spec.split("=", 1)
    else:
        raise ValueError(
            "worker spec must look like role:%pane, role:@window, role:session, or role:branch:name"
        )

    role = role.strip().lower()
    target = target.strip()
    if not _ROLE_RE.match(role):
        raise ValueError(f"invalid worker role: {role!r}")
    if not target:
        raise ValueError(f"worker {role!r} target cannot be empty")

    kind = "session"
    value = target
    if target.startswith("%"):
        kind = "pane"
    elif target.startswith("@"):
        kind = "window"
    elif target.startswith("$"):
        kind = "session"
    elif target.startswith("branch:"):
        kind = "branch"
        value = target.split(":", 1)[1].strip()
    elif target.startswith("session:"):
        kind = "session"
        value = target.split(":", 1)[1].strip()
    elif target.startswith("window:"):
        kind = "window"
        value = target.split(":", 1)[1].strip()
    elif target.startswith("pane:"):
        kind = "pane"
        value = target.split(":", 1)[1].strip()
    if not value:
        raise ValueError(f"worker {role!r} target cannot be empty")
    return {"role": role, "kind": kind, "target": value}


def create_mission_state(
    goal: str,
    supervisor_pane: str,
    worker_specs: list[str],
    *,
    mission_id: str | None = None,
    created_at: int | None = None,
) -> dict[str, Any]:
    """Build a serializable mission state document."""
    clean_goal = goal.strip()
    if not clean_goal:
        raise ValueError("mission goal cannot be empty")
    if not supervisor_pane.startswith("%"):
        raise ValueError("supervisor pane must be a tmux pane id such as %1")
    if not worker_specs:
        raise ValueError("at least one --worker role binding is required")

    created = int(created_at if created_at is not None else time.time())
    mid = mission_id or f"{slugify(clean_goal, 36)}-{uuid4().hex[:6]}"
    return {
        "schema": 1,
        "mission_id": mid,
        "goal": clean_goal,
        "supervisor_pane": supervisor_pane,
        "workers": [parse_worker_spec(spec) for spec in worker_specs],
        "created_at": created,
        "updated_at": created,
        "status": "active",
    }


def save_mission_state(repo_root: str, mission: dict[str, Any]) -> Path:
    """Persist a mission state document."""
    mission["updated_at"] = int(time.time())
    path = mission_state_path(repo_root, str(mission["mission_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mission, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_mission_state(repo_root: str, mission_id: str) -> dict[str, Any]:
    """Load a mission state document by id."""
    path = mission_state_path(repo_root, mission_id)
    if not path.exists():
        raise FileNotFoundError(f"mission not found: {mission_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_latest_mission_state(repo_root: str) -> dict[str, Any] | None:
    """Load the most recently updated mission state document, if one exists."""
    state_dir = mission_state_dir(repo_root)
    if not state_dir.exists():
        return None
    files = sorted(state_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    return json.loads(files[0].read_text(encoding="utf-8"))


def worker_matches_pane(worker: dict[str, str], pane: dict[str, Any]) -> bool:
    """Return True when a pane belongs to a mission worker binding."""
    kind = worker.get("kind", "")
    target = worker.get("target", "")
    if kind == "pane":
        return pane.get("pane_id") == target
    if kind == "window":
        return target in {pane.get("window_id"), pane.get("window_name")}
    if kind == "session":
        return target in {pane.get("session_id"), pane.get("session_name")}
    if kind == "branch":
        return pane.get("branch") == target
    return False


def _aggregate_status(matches: list[dict[str, Any]]) -> str:
    statuses = {str(match.get("status", "")) for match in matches}
    if not matches:
        return "missing"
    if "waiting_for_input" in statuses:
        return "waiting_for_input"
    if "running" in statuses:
        return "running"
    if statuses == {"idle"} or "idle" in statuses:
        return "idle"
    return "unknown"


def summarize_mission(mission: dict[str, Any], panes: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize a mission against the current tmux pane snapshot."""
    workers: list[dict[str, Any]] = []
    for worker in mission.get("workers", []):
        matches = [pane for pane in panes if worker_matches_pane(worker, pane)]
        workers.append(
            {
                **worker,
                "status": _aggregate_status(matches),
                "needs_prompt": any(bool(match.get("needs_prompt")) for match in matches),
                "pane_ids": [str(match.get("pane_id", "")) for match in matches],
                "panes": matches,
            }
        )

    supervisor_matches = [
        pane for pane in panes if pane.get("pane_id") == mission.get("supervisor_pane")
    ]
    counts = {
        "workers": len(workers),
        "running": sum(1 for worker in workers if worker["status"] == "running"),
        "waiting_for_input": sum(
            1 for worker in workers if worker["status"] == "waiting_for_input"
        ),
        "idle": sum(1 for worker in workers if worker["status"] == "idle"),
        "missing": sum(1 for worker in workers if worker["status"] == "missing"),
    }

    if counts["missing"]:
        status = "missing"
        next_action = "repair_worker_targets"
    elif counts["waiting_for_input"]:
        status = "blocked"
        next_action = "prompt_supervisor"
    elif counts["running"]:
        status = "running"
        next_action = "monitor_workers"
    elif workers:
        status = "idle"
        next_action = "review_or_complete"
    else:
        status = "unassigned"
        next_action = "assign_workers"

    return {
        "mission_id": mission.get("mission_id", ""),
        "goal": mission.get("goal", ""),
        "status": status,
        "next_action": next_action,
        "supervisor_pane": mission.get("supervisor_pane", ""),
        "supervisor": supervisor_matches[0] if supervisor_matches else None,
        "counts": counts,
        "workers": workers,
        "created_at": mission.get("created_at", 0),
        "updated_at": mission.get("updated_at", 0),
    }


def mission_needs_handoff(summary: dict[str, Any], *, seen_busy: bool = True) -> bool:
    """Return True when a mission should wake the supervisor."""
    status = summary.get("status")
    if status in {"blocked", "missing"}:
        return True
    if status == "idle" and seen_busy:
        return True
    return False


def format_mission_handoff(summary: dict[str, Any]) -> str:
    """Build the prompt sent to the user-representative supervisor agent."""
    lines = [
        "tmuxx mission handoff",
        "",
        "You represent the user. Monitor the worker agents, unblock them, and drive the mission to completion.",
        "",
        f"Mission: {summary.get('mission_id', '')}",
        f"Goal: {summary.get('goal', '')}",
        f"Overall status: {summary.get('status', '')}",
        f"Next action: {summary.get('next_action', '')}",
        "",
        "Workers:",
    ]
    for worker in summary.get("workers", []):
        pane_ids = ", ".join(worker.get("pane_ids") or []) or "<missing>"
        lines.append(
            f"- {worker.get('role', '')}: {worker.get('kind', '')}:{worker.get('target', '')} "
            f"status={worker.get('status', '')} panes={pane_ids}"
        )
        for pane in worker.get("panes", [])[:2]:
            output = str(pane.get("recent_output", "")).rstrip()
            if output:
                tail = "\n".join(output.splitlines()[-8:])
                lines.append("  recent output:")
                lines.extend(f"    {line}" for line in tail.splitlines())

    lines.extend(
        [
            "",
            "Instructions:",
            "1. Inspect any blocked worker panes first.",
            "2. Prompt workers with concrete next steps, or ask the user only when required.",
            "3. Coordinate dev, review, and QA agents so their work converges on the mission goal.",
            "4. Do not declare the mission complete until implementation and verification evidence cover the goal.",
        ]
    )
    return "\n".join(lines)
