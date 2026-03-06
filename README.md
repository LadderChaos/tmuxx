# tmuxx

Your terminal, orchestrated. By you and your agents.

```
  _|
_|_|_|_|  _|_|_|  _|_|    _|    _|  _|    _|  _|    _|
  _|      _|    _|    _|  _|    _|    _|_|      _|_|
  _|      _|    _|    _|  _|    _|  _|    _|  _|    _|
    _|_|  _|    _|    _|    _|_|_|  _|    _|  _|    _|
```

TUI for humans. Deterministic agent CLI for AI workflows. One interface to see, control, and automate tmux.

## Install

```bash
pip install tmuxx
# or, on managed systems (Debian/Ubuntu):
pipx install tmuxx
# optional Node wrapper (expects tmuxx binary in PATH):
npm install -g tmuxx
```

Requires Python 3.10+ and [tmux](https://github.com/tmux/tmux).
The npm package is a thin wrapper that forwards to the `tmuxx` binary.

## Usage

```bash
# default: interactive TUI with pane activity indicators
tmuxx

# explicit TUI mode
tmuxx tui

# deterministic agent automation mode
tmuxx agent --help

# binary version
tmuxx --version
```

### TUI Features

The **interactive TUI** displays **pane-level activity status** with color-rendered preview:
- `▶` = **running** (blue) — agent actively processing
- `⏸` = **waiting** (red) — agent blocked on permission/input
- `⎇` = **worktree** (green) — 4th-level tree node showing git worktree branch

**Header legend** shows all status indicators at a glance. Preview panel renders full ANSI terminal colors.

## Keybindings

| Key | Action |
|-----|--------|
| `n` | New session |
| `w` | New window |
| `h` | Split pane horizontally |
| `v` | Split pane vertically |
| `k` | Kill selected session/window/pane |
| `r` | Rename session or window |
| `s` | Activate selected window/pane |
| `a` | Attach to session |
| `y` | Yank (copy) preview to clipboard |
| `b` | Toggle sidebar |
| `?` | Show help menu |
| `R` | Force refresh |
| `+` / `-` | Resize pane up/down |
| `[` / `]` | Resize pane left/right |
| `q` | Quit |

## Agent Orchestration

Run parallel AI agents in isolated git worktrees, each with its own branch and tmux window. Monitor all agents with **pane-level activity visibility** — see which ones are running, idle, or blocked on user input.

### Deterministic Workflow Commands

```bash
tmuxx agent start-task <session_name> "<prompt>" [--branch ...] [--base-branch ...] [--agent-command ...]
tmuxx agent task-report <branch>
tmuxx agent complete-task <branch> [--test-command ...] [--commit-message ...]
tmuxx agent abort-task <branch>
```

Recommended command flow for skills:

1. `start-task` creates worktree + tmux window and runs the agent command.
2. `task-report` provides branch status, diff, and log presence with stable fields.
3. `complete-task` or `abort-task` performs capture + cleanup in one operation.

### JSON-first Mode

All `tmuxx agent` commands support `--json` for machine-safe parsing:

```bash
tmuxx agent list-worktrees --json
tmuxx agent start-task dev "fix login bug" --json
tmuxx agent task-report fix-login-bug --json
tmuxx agent complete-task fix-login-bug --test-command "pytest -q" --json
```

`run-and-capture` is scoped to the command you send (it returns command-local output, not full pane scrollback).

### Full Command Surface

```bash
# introspection
tmuxx agent list-sessions
tmuxx agent capture-pane %1 --lines 200
tmuxx agent capture-window @2
tmuxx agent screenshot-window @2 --output ./window.png

# session/window/pane operations
tmuxx agent create-session dev
tmuxx agent create-window dev --name logs
tmuxx agent split-pane %3 --horizontal
tmuxx agent send-command %3 -- npm test
tmuxx agent send-text %3 -- "draft note in shell"
tmuxx agent send-keys %3 C-c
tmuxx agent send-keys %3 --literal -- "echo hello"
tmuxx agent run-and-capture %3 --wait-seconds 2 --lines 300 -- pytest -q

# worktree operations
tmuxx agent launch-agent dev "add auth tests" --base-branch feat-auth
tmuxx agent list-worktrees
tmuxx agent diff-worktree feat-auth-tests
tmuxx agent merge-worktree feat-auth-tests --test-command "pytest -q"
tmuxx agent discard-worktree feat-auth-tests
tmuxx agent read-agent-log feat-auth-tests
```

## Legacy MCP Compatibility (Optional)

`tmuxx` is now single-binary first. If you still need MCP for external clients, the legacy module remains in source.

```bash
pip install "tmuxx[mcp]"
python tmux_mcp.py
```

## License

MIT
