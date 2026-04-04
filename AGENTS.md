# AGENTS.md

## UI Changes

- **Never guess what a UI element is.** If the user references something by name ("keys panel", "cmd bar"), ask for clarification or a screenshot before acting.
- **Make the smallest possible change.** "Remove tooltip" means remove the tooltip, not the entry. "Remove keys panel" means remove that specific panel, not the entire command palette.
- **Don't disable/remove entire features when asked to modify a detail.** One wrong assumption compounds into 10 rounds of fixes.
- **Follow instructions literally.** Do exactly what was asked, nothing more.
- **If unsure, ask.** One clarifying question saves 10 correction rounds.

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
