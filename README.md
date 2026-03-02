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

### Test with MCP Inspector

```bash
mcp dev tmux_mcp.py
```

## License

MIT
