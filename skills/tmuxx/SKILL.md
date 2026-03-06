---
name: tmuxx
description: Deterministic tmuxx automation via a single binary (`tmuxx`) with pane-level activity insights. Use for tmux orchestration, worktree task execution, pane/session operations, and monitoring which agents need user input. Prefer workflow commands and JSON output. Avoid direct tmux CLI usage.
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

### 2) Monitor task — with pane-level insights

```bash
tmuxx agent task-report <branch> --json
tmuxx agent list-worktrees --json
tmuxx agent list-sessions --json
```

`task-report` now includes **pane-level details** for each task:
- `pane_details[].status`: "idle", "running", "waiting_for_input", "error"
- `pane_details[].needs_prompt`: True if pane is waiting for user input/approval (permission request, confirmation, etc.)
- `pane_details[].window_name`: Which window the pane is in
- `pane_details[].command`: What command is running

`list-sessions` also includes pane-level statuses for every session, so you can see at a glance:
- Which panes are actively running
- Which are idle
- **Which are waiting for user input** (permission wall, approval prompt, etc.)

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

## Deep Activity Insights (v0.3.7+)

tmuxx now tracks activities at the **pane level** to provide visibility into concurrent agent workflows (inspired by Superterm's "agentic attention" concept):

### Pane Status Types

- **`idle`**: Pane is waiting at a shell prompt (bash, zsh, etc.)
- **`running`**: Pane has an active process (agent, compiler, test runner)
- **`waiting_for_input`**: Pane is expecting user input (permission request, confirmation, etc.)
- **`error`**: Pane process exited with error

### Detecting "Needs Prompt"

The `needs_prompt` flag detects when a pane is blocked waiting for user action. Patterns detected:
- Permission requests: "permission needed", "Allow/Deny", etc.
- Input prompts: "[y/n]", "yes/no", "press any key"
- Approval walls: "waiting for approval", "confirm action"
- Tool prompts: "Tool: execute_bash" (Superterm-style)

### Example Output

```json
{
  "pane_details": [
    {
      "pane_id": "%0",
      "window_name": "editor",
      "command": "claude",
      "status": "running",
      "needs_prompt": false
    },
    {
      "pane_id": "%1",
      "window_name": "logs",
      "command": "tail",
      "status": "idle",
      "needs_prompt": false
    }
  ]
}
```

### Use Cases

- **Batch agent coordination**: Launch 8 agents, check `task-report` to see which ones are blocked on permissions
- **Deep session monitoring**: `list-sessions` now shows pane statuses across all sessions — no more tab-cycling
- **Prompt detection**: Automatically identify when agents hit permission walls or need user approval

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
