---
name: tmuxx
description: Deterministic tmuxx automation via a single binary (`tmuxx`). Use this skill for tmux orchestration, worktree task execution, and pane/session operations. Prefer workflow commands and JSON output. Avoid direct tmux CLI usage.
---

# tmuxx

Use this skill to control tmux and worktree-based agent tasks through `tmuxx agent`.

## Hard Rules

- Prefer `tmuxx agent` for session/window/worktree management. If pane command passthrough fails on a target environment, fall back to raw `tmux send-keys` for shell builtins.
- Always pass `--json` so outputs are machine-parseable.
- Prefer deterministic workflow commands over low-level primitives:
  - `start-task`
  - `task-report`
  - `complete-task`
  - `abort-task`
- Only use low-level commands (`split-pane`, `send-command`, etc.) when workflow commands cannot solve the request.

## Standard Workflow

### 1) Start task

```bash
tmuxx agent start-task <session_name> "<task prompt>" --json
```

Optional:

- `--branch <name>`
- `--base-branch <branch>`
- `--agent-command "claude -p"` (or other compatible command)

### Setup Workspace (common first operation)

```bash
tmuxx agent create-session dev --json
tmuxx agent create-window dev --name editor --json
tmuxx agent create-window dev --name logs --json
tmuxx agent list-sessions --json
```

### 2) Monitor task

```bash
tmuxx agent task-report <branch> --json
tmuxx agent list-worktrees --json
```

### 3) Complete task

```bash
tmuxx agent complete-task <branch> --test-command "<cmd>" --json
```

### 4) Abort task

```bash
tmuxx agent abort-task <branch> --json
```

## Diagnostics / Inspection

```bash
tmuxx agent list-sessions --json
tmuxx agent capture-pane %0 --lines 200 --json
tmuxx agent capture-window @0 --json
tmuxx agent read-agent-log <branch> --json
```

`run-and-capture` returns output scoped to the command you sent (not full pane history).

## Low-level Operations (Fallback)

```bash
tmuxx agent create-session <name> --json
tmuxx agent create-window <session> --name <name> --json
tmuxx agent split-pane %0 --horizontal --json
tmuxx agent send-command %0 --json -- <command text>
tmuxx agent send-text %0 --json -- <text>
tmuxx agent send-keys %0 C-c --json
tmuxx agent send-keys %0 --literal --json -- <text>
tmuxx agent run-and-capture %0 --wait-seconds 2 --lines 200 --json -- <command text>
tmuxx agent resize-pane %0 right --amount 10 --json
tmuxx agent kill-pane %0 --json
tmuxx agent kill-window @0 --json
tmuxx agent kill-session <name> --json
```

## Error Recovery

When a command fails:

1. Re-run with identical arguments once.
2. Run `tmuxx agent task-report <branch> --json` (if branch-based).
3. Run `tmuxx agent list-sessions --json` and `tmuxx agent list-worktrees --json`.
4. If still blocked, return the exact command, error text, and suggested next command.

## Known Limitations

- For historical binaries (`<=0.3.4`), pane command passthrough may fail or require awkward quoting for shell builtins (`cd`, `pwd`, `export`).
- If stuck on an older binary, use raw `tmux send-keys -t <pane> "<text>" Enter` as a temporary fallback.

## Notes

- `screenshot-window` may require optional dependencies (`pip install "tmuxx[mcp]"`).
- If using npm, `npm install -g tmuxx` installs only a wrapper. The Python `tmuxx` binary must still be available in `PATH` (`pipx install tmuxx` recommended).
- Use direct `tmux` only if `tmuxx` is not installed or is broken, and explicitly state the reason.
