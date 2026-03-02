# tmuxx

Your terminal, orchestrated. By you and your agents.

```
  _|
_|_|_|_|  _|_|_|  _|_|    _|    _|  _|    _|  _|    _|
  _|      _|    _|    _|  _|    _|    _|_|      _|_|
  _|      _|    _|    _|  _|    _|  _|    _|  _|    _|
    _|_|  _|    _|    _|    _|_|_|  _|    _|  _|    _|
```

TUI for humans. MCP server for AI agents. One interface to see, control, and automate tmux.

## Install

```bash
pip install tmuxx
```

Requires Python 3.12+ and [tmux](https://github.com/tmux/tmux).

## Usage

```bash
tmuxx
```

## Keybindings

| Key | Action |
|-----|--------|
| `n` | New session |
| `w` | New window |
| `h` | Split pane horizontally |
| `v` | Split pane vertically |
| `k` | Kill selected session/window/pane |
| `r` | Rename session or window |
| `c` | Send command to pane |
| `a` | Attach to session |
| `b` | Toggle sidebar |
| `?` | Show help menu |
| `R` | Force refresh |
| `+` / `-` | Resize pane up/down |
| `[` / `]` | Resize pane left/right |
| `q` | Quit |

## MCP Server

tmuxx includes an MCP (Model Context Protocol) server that lets LLMs observe and control tmux sessions via tool calls.

### Setup

```bash
pip install "tmuxx[mcp]"
```

This installs the `tmuxx-mcp` command, which runs a stdio-based MCP server.

### Add to Claude Code

```bash
claude mcp add tmuxx -- tmuxx-mcp
```

### Add to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tmuxx": {
      "command": "tmuxx-mcp"
    }
  }
}
```

### Tools

| Tool | Description |
|------|-------------|
| `list_sessions` | List all sessions/windows/panes as JSON |
| `capture_pane` | Capture text content of a pane |
| `capture_window` | Capture text content of all panes in a window |
| `create_session` | Create a new session |
| `kill_session` | Kill a session |
| `rename_session` | Rename a session |
| `create_window` | Create a new window |
| `kill_window` | Kill a window |
| `rename_window` | Rename a window |
| `split_pane` | Split a pane vertically or horizontally |
| `kill_pane` | Kill a pane |
| `resize_pane` | Resize a pane in a given direction |
| `send_command` | Send a command to a pane (appends Enter) |
| `send_keys` | Send raw keys to a pane (for Ctrl-C, Escape, etc.) |
| `run_and_capture` | Send a command, wait, then capture the output |
| `screenshot_window` | Take a PNG screenshot of a full window layout |

### Scenarios

**1. Dev environment setup**

> "Set up a dev environment for this project"

```
Agent: create_session("backend")
Agent: send_command(%0, "cd ~/project && cargo run")
Agent: create_window("backend", "logs")
Agent: send_command(%1, "tail -f /var/log/app.log")
Agent: create_session("frontend")
Agent: send_command(%2, "cd ~/project/web && npm run dev")
Agent: split_pane(%2, horizontal=True)
Agent: send_command(%3, "npm run test -- --watch")
```

You open tmuxx, see everything running. Agent sees the same.

**2. Debug a failing service**

> "The API server crashed, check what happened"

```
Agent: list_sessions()
Agent: capture_pane(%0)           → reads the error traceback
Agent: screenshot_window(@0)      → sees the full terminal layout
Agent: send_command(%0, "git log --oneline -5")
Agent: run_and_capture(%0, "curl localhost:8080/health", wait_seconds=2)
Agent: send_command(%0, "cargo run")
Agent: capture_pane(%0)           → confirms it's running again
```

You watch the agent diagnose and restart in real time.

**3. Multi-agent orchestration**

> "Run the test suite across three environments"

```
Agent: create_session("test-matrix")
Agent: send_command(%0, "docker run -e PG=14 ./test.sh")
Agent: split_pane(%0)
Agent: send_command(%1, "docker run -e PG=15 ./test.sh")
Agent: split_pane(%0, horizontal=True)
Agent: send_command(%2, "docker run -e PG=16 ./test.sh")
Agent: screenshot_window(@0)      → sees all three running side by side
# ...waits...
Agent: capture_window(@0)         → reads all results at once
```

**4. Pair programming**

You're working in tmux. Agent watches over your shoulder.

```
Agent: list_sessions()            → finds your active session
Agent: capture_pane(%0)           → reads what you're looking at
Agent: split_pane(%0)
Agent: send_command(%1, "rg 'TODO' --type rust")
Agent: capture_pane(%1)           → shares findings with you
```

You see the new pane appear. Both sides transparent.

### Test with MCP Inspector

```bash
mcp dev tmux_mcp.py
```

## License

MIT
