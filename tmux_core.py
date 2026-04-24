"""tmux_core: Shared tmux domain models and async backend."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

# Separator for tmux format strings — tab avoids conflicts with session/window names
_SEP = "\t"
DEFAULT_AGENT_COMMAND = "claude -p"
IDLE_COMMANDS = frozenset({"bash", "zsh", "fish", "sh", "tmux", "login"})
_AGENT_SESSION_ENV_VARS: dict[str, tuple[str, ...]] = {
    "codex": (
        "CODEX_THREAD_ID",
        "CODEX_TUI_SESSION_LOG_PATH",
    ),
    "claude": (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_SESSION_ID",
    ),
    "gemini": (
        "GEMINI_SANDBOX",
        "GEMINI_CLI_ACTIVITY_LOG_TARGET",
        "GEMINI_CLI_NO_RELAUNCH",
    ),
}


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class Pane:
    pane_id: str
    pane_index: int
    width: int
    height: int
    current_command: str
    active: bool
    left: int = 0
    top: int = 0
    current_path: str = ""
    pid: int = 0
    status: str = "idle"  # "idle", "running", "waiting_for_input", "error"
    activity: int = 0  # unix timestamp of last activity
    needs_prompt: bool = False  # true if waiting for user input/approval
    recent_output: str = ""  # last N lines of pane output for prompt detection
    worktree_branch: str = ""  # non-empty if pane is in a git worktree


@dataclass
class Window:
    window_id: str
    window_index: int
    name: str
    active: bool
    panes: list[Pane] = field(default_factory=list)
    activity: int = 0
    status: str = "idle"  # aggregate of pane statuses


@dataclass
class Session:
    session_id: str
    name: str
    attached: bool
    windows: list[Window] = field(default_factory=list)
    created: int = 0
    activity: int = 0


@dataclass
class Worktree:
    path: str       # absolute path
    branch: str     # branch name
    head: str       # short SHA
    is_main: bool
    status: str = "idle"  # "running", "done", "idle", or "waiting_for_input"


# ── Helpers ──────────────────────────────────────────────────────────────────


def quote(s: str) -> str:
    """Shell-quote a string using shlex."""
    return shlex.quote(s)


def xdg_config_path(*parts: str) -> Path:
    """Return a path under $XDG_CONFIG_HOME (or ~/.config) for tmuxx config files."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "tmuxx" / Path(*parts) if parts else Path(base) / "tmuxx"
    return Path.home() / ".config" / "tmuxx" / Path(*parts) if parts else Path.home() / ".config" / "tmuxx"


def check_tmux() -> None:
    """Exit with a message if tmux is not found in PATH."""
    if not shutil.which("tmux"):
        print("Error: tmux not found in PATH. Install it first.", file=sys.stderr)
        sys.exit(1)


def slugify(text: str, max_len: int = 50) -> str:
    """Convert a prompt string to a git-safe branch name."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "agent-task"


def detect_needs_prompt(output: str) -> bool:
    """
    Heuristically detect if pane is waiting for user input/approval.
    Only examines the last 5 lines of output to reduce false positives.
    """
    if not output:
        return False

    # Only look at the last 5 lines to avoid matching old log output
    lines = output.rstrip().splitlines()
    tail = "\n".join(lines[-5:]) if len(lines) > 5 else output

    prompt_patterns = [
        r"(?:allow|approve|deny|reject)\s*\[",
        r"press\s+(?:any\s+key|enter)",
        r"waiting\s+for\s+(?:user|input|approval|confirmation)",
        r"\(y/n\)",
        r"\[y/n\]",
        r"\[Y/n\]",
        r"\[yes/no\]",
        r"Do you want to proceed",
        r"Are you sure",
        r"tool:\s+\w+(?=\n)",
        r"new task\?(?:\s|$)",
    ]

    text_lower = tail.lower()
    for pattern in prompt_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False


def detect_shell_prompt(output: str) -> bool:
    """Heuristically detect whether the pane tail ends at a shell-style prompt."""
    if not output:
        return False

    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if not lines:
        return False

    last_line = lines[-1]
    return bool(re.search(r"(?:❯|[$#%])\s*$", last_line))


def classify_pane_status(current_command: str, recent_output: str) -> tuple[str, bool]:
    """Infer pane status from the active command plus recent pane output."""
    needs_prompt = detect_needs_prompt(recent_output)
    if needs_prompt:
        return "waiting_for_input", True
    if current_command in IDLE_COMMANDS or detect_shell_prompt(recent_output):
        return "idle", False
    return "running", False


def detect_agent_session_family() -> Literal["claude", "codex", "gemini"] | None:
    """Return the active agent family for the current shell, if known."""
    for family, env_vars in _AGENT_SESSION_ENV_VARS.items():
        if any(os.getenv(name) for name in env_vars):
            return cast(Literal["claude", "codex", "gemini"], family)
    return None


def running_inside_agent_session() -> bool:
    """Return True when tmuxx is running inside a known agent shell."""
    return detect_agent_session_family() is not None


def _command_family(agent_command: str) -> Literal["claude", "codex", "gemini"] | None:
    """Infer the target agent family from the command executable name."""
    try:
        parts = shlex.split(agent_command)
    except ValueError:
        parts = agent_command.split()
    if not parts:
        return None
    executable = os.path.basename(parts[0]).lower()
    if executable.startswith("claude"):
        return "claude"
    if executable.startswith("codex"):
        return "codex"
    if executable.startswith("gemini"):
        return "gemini"
    return None


def resolve_agent_command(agent_command: str | None) -> str:
    """Resolve the agent command from explicit args, env override, or default."""
    session_family = detect_agent_session_family()
    explicit = (agent_command or "").strip()
    if explicit:
        resolved = explicit
    else:
        env_override = os.getenv("TMUXX_AGENT_COMMAND", "").strip()
        if env_override:
            resolved = env_override
        else:
            if session_family:
                raise RuntimeError(
                    "No safe default agent command is available inside an existing agent session. "
                    "Pass --agent-command or set TMUXX_AGENT_COMMAND."
                )
            resolved = DEFAULT_AGENT_COMMAND

    command_family = _command_family(resolved)
    if session_family and command_family == session_family:
        raise RuntimeError(
            f"Refusing to launch nested '{resolved}' from inside an existing {session_family} session. "
            "Pass a different --agent-command."
        )

    return resolved


# ── Tmux Backend ─────────────────────────────────────────────────────────────


class TmuxBackend:
    """Async interface to tmux CLI."""

    @staticmethod
    async def _run(*args: str) -> str:
        """Run a tmux command. Raises RuntimeError on non-zero exit."""
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            msg = stderr.decode().strip()
            # Strip verbose tmux prefixes for cleaner error messages
            for prefix in ("error: ", "tmux: "):
                if msg.lower().startswith(prefix):
                    msg = msg[len(prefix):]
            raise RuntimeError(msg or f"exit code {proc.returncode}")
        return stdout.decode().strip()

    async def get_hierarchy(self) -> list[Session]:
        sep = _SEP
        sessions_raw, windows_raw, panes_raw = await asyncio.gather(
            self._run(
                "tmux", "list-sessions", "-F",
                f"#{{session_id}}{sep}#{{session_name}}{sep}#{{session_attached}}{sep}#{{session_created}}{sep}#{{session_activity}}",
            ),
            self._run(
                "tmux", "list-windows", "-a", "-F",
                f"#{{session_id}}{sep}#{{window_id}}{sep}#{{window_index}}{sep}#{{window_name}}{sep}#{{window_active}}{sep}#{{window_activity}}",
            ),
            self._run(
                "tmux", "list-panes", "-a", "-F",
                f"#{{window_id}}{sep}#{{pane_id}}{sep}#{{pane_index}}{sep}#{{pane_width}}{sep}#{{pane_height}}{sep}#{{pane_current_command}}{sep}#{{pane_active}}{sep}#{{pane_left}}{sep}#{{pane_top}}{sep}#{{pane_current_path}}{sep}#{{pane_pid}}",
            ),
        )

        # Parse panes
        pane_map: dict[str, list[Pane]] = {}
        for line in panes_raw.splitlines():
            if not line:
                continue
            parts = line.split(sep)
            if len(parts) < 9:
                continue
            win_id = parts[0]
            pane = Pane(
                pane_id=parts[1],
                pane_index=int(parts[2]),
                width=int(parts[3]),
                height=int(parts[4]),
                current_command=parts[5],
                active=parts[6] == "1",
                left=int(parts[7]),
                top=int(parts[8]),
                current_path=parts[9] if len(parts) > 9 else "",
                pid=int(parts[10]) if len(parts) > 10 and parts[10].isdigit() else 0,
            )
            pane_map.setdefault(win_id, []).append(pane)

        # Parse windows
        win_map: dict[str, list[Window]] = {}
        for line in windows_raw.splitlines():
            if not line:
                continue
            parts = line.split(sep)
            if len(parts) < 5:
                continue
            sess_id = parts[0]
            win = Window(
                window_id=parts[1],
                window_index=int(parts[2]),
                name=parts[3],
                active=parts[4] == "1",
                panes=sorted(pane_map.get(parts[1], []), key=lambda p: p.pane_index),
                activity=int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0,
            )
            win_map.setdefault(sess_id, []).append(win)

        # Parse sessions
        sessions: list[Session] = []
        for line in sessions_raw.splitlines():
            if not line:
                continue
            parts = line.split(sep)
            if len(parts) < 3:
                continue
            sess = Session(
                session_id=parts[0],
                name=parts[1],
                attached=parts[2] == "1",
                windows=sorted(
                    win_map.get(parts[0], []), key=lambda w: w.window_index
                ),
                created=int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0,
                activity=int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0,
            )
            sessions.append(sess)
        return sorted(sessions, key=lambda s: s.name)

    async def capture_pane(self, pane_id: str, lines: int = 40) -> str:
        return await self._run("tmux", "capture-pane", "-t", pane_id, "-e", "-p", "-S", f"-{lines}")

    async def new_session(self, name: str) -> None:
        await self._run("tmux", "new-session", "-d", "-s", name, "-x", "200", "-y", "50")

    async def kill_session(self, name: str) -> None:
        await self._run("tmux", "kill-session", "-t", name)

    async def rename_session(self, old: str, new: str) -> None:
        await self._run("tmux", "rename-session", "-t", old, new)

    async def new_window(self, session: str, name: str | None = None) -> None:
        args = ["tmux", "new-window", "-t", f"{session}:"]
        if name:
            args += ["-n", name]
        await self._run(*args)

    async def kill_window(self, window_id: str) -> None:
        await self._run("tmux", "kill-window", "-t", window_id)

    async def rename_window(self, window_id: str, new_name: str) -> None:
        await self._run("tmux", "rename-window", "-t", window_id, new_name)

    async def split_pane(self, pane_id: str, horizontal: bool = False) -> None:
        flag = "-h" if horizontal else "-v"
        await self._run("tmux", "split-window", flag, "-t", pane_id)

    async def kill_pane(self, pane_id: str) -> None:
        await self._run("tmux", "kill-pane", "-t", pane_id)

    async def send_keys(self, pane_id: str, keys: str) -> None:
        await self._run("tmux", "send-keys", "-t", pane_id, keys, "Enter")

    async def resize_pane(self, pane_id: str, direction: str, amount: int = 5) -> None:
        flag_map = {"up": "-U", "down": "-D", "left": "-L", "right": "-R"}
        flag = flag_map.get(direction, "-U")
        await self._run("tmux", "resize-pane", "-t", pane_id, flag, str(amount))

    async def capture_window_panes(self, panes: list[Pane]) -> dict[str, str]:
        """Capture visible content of all panes in a window in parallel (with ANSI)."""
        async def _cap(p: Pane) -> tuple[str, str]:
            content = await self._run("tmux", "capture-pane", "-t", p.pane_id, "-e", "-p")
            return p.pane_id, content

        results = await asyncio.gather(*[_cap(p) for p in panes])
        return dict(results)

    async def new_window_in_dir(
        self, session: str, directory: str, name: str | None = None
    ) -> None:
        """Create a new window with working directory set to *directory*."""
        args = ["tmux", "new-window", "-t", f"{session}:", "-c", directory]
        if name:
            args += ["-n", name]
        await self._run(*args)

    async def select_window(self, window_id: str) -> None:
        await self._run("tmux", "select-window", "-t", window_id)

    async def select_pane(self, pane_id: str) -> None:
        await self._run("tmux", "select-pane", "-t", pane_id)


# ── Git Backend ──────────────────────────────────────────────────────────────


class GitBackend:
    """Async interface to git CLI for worktree orchestration."""

    def __init__(self) -> None:
        self._repo_root: str | None = None

    @staticmethod
    async def _run(*args: str, cwd: str | None = None) -> str:
        """Run a git command. Raises RuntimeError on non-zero exit."""
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            msg = stderr.decode().strip() or stdout.decode().strip()
            raise RuntimeError(msg or f"git exit code {proc.returncode}")
        return stdout.decode().strip()

    @staticmethod
    async def detect_worktree_branch(path: str) -> str:
        """Return the branch name if *path* is inside a git worktree (not main), else ''."""
        if not path or not os.path.isdir(path):
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--git-common-dir", "--git-dir", "--abbrev-ref", "HEAD",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=path,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return ""
            lines = stdout.decode().strip().splitlines()
            if len(lines) < 3:
                return ""
            common_dir, git_dir, branch = lines[0], lines[1], lines[2]
            # If git-common-dir == git-dir, it's the main checkout, not a worktree
            if os.path.realpath(common_dir) == os.path.realpath(git_dir):
                return ""
            return branch if branch != "HEAD" else ""
        except Exception:
            return ""

    async def get_repo_root(self) -> str:
        """Discover and cache the repository root (symlinks resolved)."""
        if self._repo_root is None:
            raw = await self._run("git", "rev-parse", "--show-toplevel")
            self._repo_root = os.path.realpath(raw)
        return self._repo_root

    async def list_worktrees(self) -> list[Worktree]:
        """Parse ``git worktree list --porcelain`` into Worktree objects."""
        root = await self.get_repo_root()
        raw = await self._run("git", "worktree", "list", "--porcelain", cwd=root)
        worktrees: list[Worktree] = []
        path = branch = head = ""
        is_main = False
        for line in raw.splitlines() + [""]:
            if line.startswith("worktree "):
                path = line.split(" ", 1)[1]
            elif line.startswith("HEAD "):
                head = line.split(" ", 1)[1][:7]
            elif line.startswith("branch "):
                branch = line.split(" ", 1)[1].replace("refs/heads/", "")
            elif line == "bare":
                is_main = True
            elif line == "":
                if path:
                    worktrees.append(Worktree(
                        path=path, branch=branch, head=head,
                        is_main=(os.path.realpath(path) == os.path.realpath(root)),
                    ))
                path = branch = head = ""
                is_main = False
        return worktrees

    async def create_worktree(self, branch: str, base_branch: str | None = None) -> str:
        """Create a worktree under ``.worktrees/<branch>`` and return its path.

        Args:
            branch: Name for the new branch.
            base_branch: Optional branch to base the worktree on (defaults to HEAD).
        """
        root = await self.get_repo_root()
        wt_path = os.path.join(root, ".worktrees", branch)
        cmd = ["git", "worktree", "add", "-b", branch, wt_path]
        if base_branch:
            cmd.append(base_branch)
        await self._run(*cmd, cwd=root)
        return wt_path

    async def merge_worktree(
        self, branch: str, msg: str | None = None, test_command: str | None = None
    ) -> None:
        """Commit changes in the worktree, optionally test, merge to main, and clean up."""
        root = await self.get_repo_root()
        wt_path = os.path.join(root, ".worktrees", branch)

        # Stage and commit in the worktree
        await self._run("git", "add", "-A", cwd=wt_path)
        commit_msg = msg or f"agent: {branch}"
        try:
            await self._run("git", "commit", "-m", commit_msg, cwd=wt_path)
        except RuntimeError as e:
            if "nothing to commit" not in str(e):
                raise

        # Pre-merge test gate
        if test_command:
            try:
                await self._run("sh", "-c", test_command, cwd=wt_path)
            except RuntimeError as e:
                raise RuntimeError(
                    f"Pre-merge test failed for '{branch}': {e}\n"
                    f"Worktree kept at {wt_path} — fix and retry."
                )

        # Merge into main branch from the repo root
        try:
            await self._run(
                "git", "merge", "--no-ff", "-m", f"Merge {branch}", branch, cwd=root
            )
        except RuntimeError as e:
            # Abort the failed merge so the repo stays clean
            try:
                await self._run("git", "merge", "--abort", cwd=root)
            except RuntimeError:
                pass
            raise RuntimeError(
                f"Merge conflict on '{branch}': {e}\n"
                f"Worktree kept at {wt_path} — resolve manually or discard."
            )

        # Clean up worktree and branch
        await self._run("git", "worktree", "remove", wt_path, cwd=root)
        await self._run("git", "branch", "-d", branch, cwd=root)

    async def diff_worktree(self, branch: str) -> str:
        """Return the diff of a worktree branch against main."""
        root = await self.get_repo_root()
        return await self._run("git", "diff", f"main...{branch}", cwd=root)

    async def discard_worktree(self, branch: str) -> None:
        """Force-remove a worktree and delete its branch."""
        root = await self.get_repo_root()
        wt_path = os.path.join(root, ".worktrees", branch)
        await self._run("git", "worktree", "remove", "--force", wt_path, cwd=root)
        try:
            await self._run("git", "branch", "-D", branch, cwd=root)
        except RuntimeError:
            pass  # branch may already be gone
