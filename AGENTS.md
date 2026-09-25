# AGENTS.md

## UI Changes

- If the user names a UI element ("keys panel", "cmd bar") and the code doesn't make clear which one is meant, ask or request a screenshot before editing; a wrong guess here has cost many correction rounds.
- Change only the named detail: "remove tooltip" removes the tooltip, not the entry; "remove keys panel" removes that panel, not the command palette. Don't disable or remove a feature when asked to modify part of it.

## Release Workflow

- Bump version in `pyproject.toml`
- `rm -rf dist/ && python3 -m build`
- `python3 -m twine upload dist/*`
- `git add` only changed files (never `git add -A`)
- `git commit -m 'v<version>: <summary>'`
- `git push`

## Local Testing

- `pipx install --force /Users/danieltang/GitHub/tmuxx` for local install
- `pipx install --force tmuxx` for PyPI install
- Run `python3 -m unittest test_tmux_agent_unit test_tmux_core_unit -v` before releasing

## Architecture

- `tmux_core.py` — shared models (Pane, Window, Session, Worktree), TmuxBackend, GitBackend, helpers
- `tmuxx.py` — TUI app (imports models/backend from tmux_core)
- `tmux_agent.py` — CLI agent commands
- `tmux_mcp.py` — legacy MCP server
- Do NOT duplicate data classes or backends across files
