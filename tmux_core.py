"""tmux_core: Shared tmux domain models and async backend."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import sys
from dataclasses import dataclass, field

# Separator for tmux format strings — tab avoids conflicts with session/window names
_SEP = "\t"


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


@dataclass
class Window:
    window_id: str
    window_index: int
    name: str
    active: bool
    panes: list[Pane] = field(default_factory=list)


@dataclass
class Session:
    session_id: str
    name: str
    attached: bool
    windows: list[Window] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────


def quote(s: str) -> str:
    """Shell-quote a string using shlex."""
    return shlex.quote(s)


def check_tmux() -> None:
    """Exit with a message if tmux is not found in PATH."""
    if not shutil.which("tmux"):
        print("Error: tmux not found in PATH. Install it first.", file=sys.stderr)
        sys.exit(1)


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
            raise RuntimeError(
                f"tmux command failed (exit {proc.returncode}): {stderr.decode().strip()}"
            )
        return stdout.decode().strip()

    async def get_hierarchy(self) -> list[Session]:
        sep = _SEP
        sessions_raw, windows_raw, panes_raw = await asyncio.gather(
            self._run(
                "tmux", "list-sessions", "-F",
                f"#{{session_id}}{sep}#{{session_name}}{sep}#{{session_attached}}",
            ),
            self._run(
                "tmux", "list-windows", "-a", "-F",
                f"#{{session_id}}{sep}#{{window_id}}{sep}#{{window_index}}{sep}#{{window_name}}{sep}#{{window_active}}",
            ),
            self._run(
                "tmux", "list-panes", "-a", "-F",
                f"#{{window_id}}{sep}#{{pane_id}}{sep}#{{pane_index}}{sep}#{{pane_width}}{sep}#{{pane_height}}{sep}#{{pane_current_command}}{sep}#{{pane_active}}{sep}#{{pane_left}}{sep}#{{pane_top}}",
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
            )
            sessions.append(sess)
        return sorted(sessions, key=lambda s: s.name)

    async def capture_pane(self, pane_id: str, lines: int = 40) -> str:
        return await self._run("tmux", "capture-pane", "-t", pane_id, "-e", "-p", "-S", f"-{lines}")

    async def new_session(self, name: str) -> None:
        await self._run("tmux", "new-session", "-d", "-s", name)

    async def kill_session(self, name: str) -> None:
        await self._run("tmux", "kill-session", "-t", name)

    async def rename_session(self, old: str, new: str) -> None:
        await self._run("tmux", "rename-session", "-t", old, new)

    async def new_window(self, session: str, name: str | None = None) -> None:
        args = ["tmux", "new-window", "-t", session]
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
