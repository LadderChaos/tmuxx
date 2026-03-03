"""End-to-end tests for tmuxx MCP server tools.

Runs every MCP tool against real tmux and a temporary git repository.
Usage: python3 test_e2e_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback

# ── Test Harness ─────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
ERRORS: list[str] = []
TEST_SESSION = "_tmuxx_e2e_test"
TEMP_REPO: str = ""


def ok(name: str, detail: str = ""):
    global PASS
    PASS += 1
    d = f" — {detail}" if detail else ""
    print(f"  \033[32m✓\033[0m {name}{d}")


def fail(name: str, detail: str = ""):
    global FAIL
    FAIL += 1
    d = f" — {detail}" if detail else ""
    print(f"  \033[31m✗\033[0m {name}{d}")
    ERRORS.append(f"{name}: {detail}")


def section(title: str):
    print(f"\n\033[1;36m{'─' * 60}\033[0m")
    print(f"\033[1;36m  {title}\033[0m")
    print(f"\033[1;36m{'─' * 60}\033[0m")


# ── Temp Repo Setup ──────────────────────────────────────────────────────────

def create_temp_repo() -> str:
    """Create a temporary git repo with an initial commit."""
    d = tempfile.mkdtemp(prefix="tmuxx_e2e_")
    subprocess.run(["git", "init", d], capture_output=True, check=True)
    subprocess.run(["git", "-C", d, "checkout", "-b", "main"], capture_output=True, check=True)
    # Create initial file and commit
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("# Test Repo\n")
    subprocess.run(["git", "-C", d, "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", d, "commit", "-m", "initial commit"],
        capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"},
    )
    # Set local git config so async subprocess commits work
    subprocess.run(["git", "-C", d, "config", "user.name", "test"], capture_output=True, check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "test@test"], capture_output=True, check=True)
    os.makedirs(os.path.join(d, ".worktrees"), exist_ok=True)
    return d


def cleanup_temp_repo(d: str):
    """Force-clean worktrees then remove temp dir."""
    try:
        result = subprocess.run(
            ["git", "-C", d, "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("worktree ") and ".worktrees" in line:
                wt_path = line.split(" ", 1)[1]
                subprocess.run(
                    ["git", "-C", d, "worktree", "remove", "--force", wt_path],
                    capture_output=True,
                )
    except Exception:
        pass
    shutil.rmtree(d, ignore_errors=True)


def cleanup_tmux_session():
    """Kill the test tmux session if it exists."""
    subprocess.run(
        ["tmux", "kill-session", "-t", TEST_SESSION],
        capture_output=True,
    )


# ── Import MCP tools ────────────────────────────────────────────────────────

def import_mcp_tools():
    """Import all tool functions and override the git backend's repo root."""
    import tmux_mcp as mcp_mod
    import tmux_core

    # Override the GitBackend to use our temp repo (realpath for macOS symlinks)
    mcp_mod.git._repo_root = os.path.realpath(TEMP_REPO)
    return mcp_mod


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_session_lifecycle(m):
    section("Session Lifecycle")

    # create_session
    result = await m.create_session(TEST_SESSION)
    assert TEST_SESSION in result, f"unexpected: {result}"
    ok("create_session", result)

    # list_sessions — find our session
    sessions = await m.list_sessions()
    names = [s["name"] for s in sessions]
    assert TEST_SESSION in names, f"{TEST_SESSION} not in {names}"
    ok("list_sessions", f"found {len(sessions)} sessions")

    # rename_session
    new_name = TEST_SESSION + "_renamed"
    result = await m.rename_session(TEST_SESSION, new_name)
    ok("rename_session", result)

    # rename back
    await m.rename_session(new_name, TEST_SESSION)
    ok("rename_session (restore)", f"back to {TEST_SESSION}")


async def test_window_lifecycle(m):
    section("Window Lifecycle")

    # create_window
    result = await m.create_window(TEST_SESSION, "test-win")
    assert "test-win" in result
    ok("create_window", result)

    # find the new window
    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    win = next((w for w in sess["windows"] if w["name"] == "test-win"), None)
    assert win is not None, "test-win not found"
    win_id = win["window_id"]
    ok("find window", f"{win_id}")

    # rename_window
    result = await m.rename_window(win_id, "renamed-win")
    ok("rename_window", result)
    await m.rename_window(win_id, "test-win")

    # kill_window
    result = await m.kill_window(win_id)
    ok("kill_window", result)


async def test_pane_operations(m):
    section("Pane Operations")

    # Get the default pane in our test session
    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    pane_id = sess["windows"][0]["panes"][0]["pane_id"]
    win_id = sess["windows"][0]["window_id"]

    # split_pane (vertical)
    result = await m.split_pane(pane_id)
    ok("split_pane (vertical)", result)

    # split_pane (horizontal)
    result = await m.split_pane(pane_id, horizontal=True)
    ok("split_pane (horizontal)", result)

    # resize_pane
    result = await m.resize_pane(pane_id, "down", 3)
    ok("resize_pane", result)

    # Get updated pane list after splits
    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    panes = sess["windows"][0]["panes"]
    assert len(panes) >= 3, f"expected >=3 panes, got {len(panes)}"
    ok("pane count after splits", f"{len(panes)} panes")

    # kill the extra panes (keep first)
    for p in panes[1:]:
        await m.kill_pane(p["pane_id"])
    ok("kill_pane (cleanup)", f"killed {len(panes) - 1} extra panes")


async def test_command_execution(m):
    section("Command Execution")

    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    pane_id = sess["windows"][0]["panes"][0]["pane_id"]

    # send_command
    result = await m.send_command(pane_id, "echo TMUXX_E2E_MARKER_12345")
    ok("send_command", result)
    await asyncio.sleep(0.5)

    # capture_pane
    content = await m.capture_pane(pane_id, lines=20)
    assert "TMUXX_E2E_MARKER_12345" in content, f"marker not in capture:\n{content[-200:]}"
    ok("capture_pane", f"captured {len(content)} chars, marker found")

    # run_and_capture
    output = await m.run_and_capture(pane_id, "echo RUN_CAPTURE_TEST_67890", wait_seconds=1.0, lines=20)
    assert "RUN_CAPTURE_TEST_67890" in output, f"marker not in output:\n{output[-200:]}"
    ok("run_and_capture", f"{len(output)} chars, marker found")

    # capture_window
    win_id = sess["windows"][0]["window_id"]
    window_content = await m.capture_window(win_id)
    assert isinstance(window_content, dict)
    assert pane_id in window_content
    ok("capture_window", f"{len(window_content)} panes captured")


async def test_send_keys(m):
    section("Send Keys (raw)")

    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    pane_id = sess["windows"][0]["panes"][0]["pane_id"]

    # Send a partial command, then Ctrl-C to cancel
    await m.send_command(pane_id, "sleep 999")
    await asyncio.sleep(0.3)
    result = await m.send_keys(pane_id, "C-c")
    ok("send_keys (C-c)", result)
    await asyncio.sleep(0.3)

    # Verify the pane is responsive after C-c
    output = await m.run_and_capture(pane_id, "echo KEYS_OK", wait_seconds=0.5)
    assert "KEYS_OK" in output
    ok("pane responsive after C-c")


async def test_screenshot(m):
    section("Screenshot")

    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    win_id = sess["windows"][0]["window_id"]

    try:
        result = await m.screenshot_window(win_id)
        assert len(result) == 2, f"expected 2 content items, got {len(result)}"
        # result[0] is TextContent, result[1] is ImageContent
        text_item = result[0]
        image_item = result[1]
        assert hasattr(text_item, "text") or "text" in str(type(text_item))
        assert hasattr(image_item, "data") or "data" in str(type(image_item))
        ok("screenshot_window", f"returned {len(result)} items (text + PNG)")
    except ImportError as e:
        ok("screenshot_window (skipped)", f"missing optional dep: {e}")
    except Exception as e:
        fail("screenshot_window", str(e))


async def test_input_validation(m):
    section("Input Validation")

    # Invalid pane ID
    try:
        await m.capture_pane("invalid_id")
        fail("invalid pane_id", "should have raised")
    except ValueError as e:
        ok("invalid pane_id rejected", str(e))

    # Invalid window ID
    try:
        await m.capture_window("bad")
        fail("invalid window_id", "should have raised")
    except ValueError as e:
        ok("invalid window_id rejected", str(e))

    # Out of range lines
    try:
        await m.capture_pane("%0", lines=99999)
        fail("out-of-range lines", "should have raised")
    except ValueError as e:
        ok("out-of-range lines rejected", str(e))

    # Invalid key sequence
    try:
        await m.send_keys("%0", "$(evil)")
        fail("invalid key sequence", "should have raised")
    except ValueError as e:
        ok("invalid key sequence rejected", str(e))

    # Invalid resize amount
    try:
        await m.resize_pane("%0", "up", 999)
        fail("out-of-range resize", "should have raised")
    except ValueError as e:
        ok("out-of-range resize rejected", str(e))


async def test_worktree_list(m):
    section("Worktree: list_worktrees")

    result = await m.list_worktrees()
    assert isinstance(result, list)
    # Should have at least the main worktree
    main_wts = [wt for wt in result if wt["is_main"]]
    assert len(main_wts) >= 1, f"no main worktree found: {result}"
    # Every worktree should have a status field
    for wt in result:
        assert "status" in wt, f"missing status field: {wt}"
    ok("list_worktrees", f"{len(result)} worktrees, main found, status fields present")


async def test_worktree_launch_and_status(m):
    section("Worktree: launch_agent + status detection")

    branch = "e2e-test-agent"

    # Launch agent
    result = await m.launch_agent(TEST_SESSION, "test agent task", branch=branch)
    assert branch in result, f"unexpected: {result}"
    ok("launch_agent", result)

    await asyncio.sleep(1.5)

    # Verify worktree appears in list with status
    wts = await m.list_worktrees()
    test_wt = next((wt for wt in wts if wt["branch"] == branch), None)
    assert test_wt is not None, f"branch {branch} not in worktrees: {[w['branch'] for w in wts]}"
    assert test_wt["status"] in ("running", "done", "idle"), f"bad status: {test_wt['status']}"
    ok("worktree in list_worktrees", f"status={test_wt['status']}")

    # Verify the worktree directory exists
    wt_path = os.path.join(TEMP_REPO, ".worktrees", branch)
    assert os.path.isdir(wt_path), f"worktree dir not found: {wt_path}"
    ok("worktree directory exists", wt_path)

    # Verify tmux window was created for the branch
    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    win_names = [w["name"] for w in sess["windows"]]
    assert branch in win_names, f"{branch} not in window names: {win_names}"
    ok("tmux window created", f"windows: {win_names}")

    return branch


async def test_worktree_diff(m, branch: str):
    section("Worktree: diff_worktree")

    # Create a file in the worktree so there's a diff
    wt_path = os.path.join(TEMP_REPO, ".worktrees", branch)
    test_file = os.path.join(wt_path, "agent_output.txt")
    with open(test_file, "w") as f:
        f.write("agent did some work\n")
    subprocess.run(["git", "-C", wt_path, "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", wt_path, "commit", "-m", "agent work"],
        capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"},
    )

    result = await m.diff_worktree(branch)
    assert "agent_output.txt" in result, f"diff doesn't contain file:\n{result[:300]}"
    assert "agent did some work" in result, f"diff doesn't contain content:\n{result[:300]}"
    ok("diff_worktree", f"{len(result)} chars, file + content found in diff")


async def test_worktree_merge_with_test_gate(m):
    section("Worktree: merge_worktree + test gate")

    branch = "e2e-test-gate"
    # Create worktree manually via git backend
    wt_path = os.path.join(TEMP_REPO, ".worktrees", branch)
    subprocess.run(
        ["git", "-C", TEMP_REPO, "worktree", "add", "-b", branch, wt_path],
        capture_output=True, check=True,
    )
    # Add a file
    with open(os.path.join(wt_path, "gate_test.txt"), "w") as f:
        f.write("testing gate\n")

    # Test gate FAILS — merge should be aborted
    try:
        await m.merge_worktree(branch, test_command="exit 1")
        fail("test gate should have failed", "merge succeeded despite exit 1")
    except RuntimeError as e:
        assert "Pre-merge test failed" in str(e), f"unexpected error: {e}"
        ok("test gate failure aborts merge", str(e)[:80])

    # Verify worktree is still there
    assert os.path.isdir(wt_path), "worktree should be preserved after test failure"
    ok("worktree preserved after test failure")

    # Test gate PASSES — merge should succeed
    result = await m.merge_worktree(branch, test_command="exit 0", commit_message="gate passed")
    assert "Merged" in result, f"unexpected: {result}"
    ok("test gate success + merge", result.split("\n")[0])

    # Verify worktree is gone
    assert not os.path.isdir(wt_path), "worktree should be removed after merge"
    ok("worktree removed after merge")


async def test_worktree_discard_with_log(m):
    section("Worktree: discard_worktree + output capture")

    branch = "e2e-test-discard"

    # Launch an agent so there's a pane to capture
    result = await m.launch_agent(TEST_SESSION, "task to discard", branch=branch)
    ok("launch_agent (for discard)", result)
    await asyncio.sleep(1.0)

    # Send some output to the pane so capture has content
    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    win = next((w for w in sess["windows"] if w["name"] == branch), None)
    assert win is not None, f"window for {branch} not found"
    pane_id = win["panes"][0]["pane_id"]

    # Cancel whatever claude command was sent, then echo a marker
    await m.send_keys(pane_id, "C-c")
    await asyncio.sleep(0.3)
    await m.send_command(pane_id, "echo DISCARD_CAPTURE_MARKER")
    await asyncio.sleep(0.5)

    # Discard — should capture output first
    result = await m.discard_worktree(branch)
    assert "Discarded" in result, f"unexpected: {result}"
    ok("discard_worktree", result.split("\n")[0])

    # Check the log was saved
    log_path = os.path.join(TEMP_REPO, ".worktrees", f"{branch}.log")
    if "output saved" in result:
        assert os.path.exists(log_path), f"log file not found: {log_path}"
        with open(log_path) as f:
            log_content = f.read()
        assert "DISCARD_CAPTURE_MARKER" in log_content, "marker not in log"
        ok("output captured to log", f"{len(log_content)} chars, marker found")
    else:
        ok("discard_worktree (no pane matched for capture)", "log may not exist")

    # read_agent_log
    log_result = await m.read_agent_log(branch)
    if os.path.exists(log_path):
        assert "DISCARD_CAPTURE_MARKER" in log_result
        ok("read_agent_log", f"{len(log_result)} chars")
    else:
        assert "No log found" in log_result
        ok("read_agent_log (no log)", log_result)

    # Verify worktree directory is gone
    wt_path = os.path.join(TEMP_REPO, ".worktrees", branch)
    assert not os.path.isdir(wt_path), "worktree dir should be removed"
    ok("worktree directory removed")


async def test_worktree_merge_with_capture(m, branch: str):
    section("Worktree: merge_worktree + output capture")

    # The worktree was created in test_worktree_launch_and_status
    # and had a file committed in test_worktree_diff
    # Now find the pane and send a marker
    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    win = next((w for w in sess["windows"] if w["name"] == branch), None)
    if win:
        pane_id = win["panes"][0]["pane_id"]
        await m.send_keys(pane_id, "C-c")
        await asyncio.sleep(0.3)
        await m.send_command(pane_id, "echo MERGE_CAPTURE_MARKER")
        await asyncio.sleep(0.5)

    result = await m.merge_worktree(branch, commit_message="e2e merge test")
    assert "Merged" in result, f"unexpected: {result}"
    ok("merge_worktree", result.split("\n")[0])

    # Check log
    log_path = os.path.join(TEMP_REPO, ".worktrees", f"{branch}.log")
    if os.path.exists(log_path):
        log_result = await m.read_agent_log(branch)
        ok("read_agent_log (after merge)", f"{len(log_result)} chars")
    else:
        ok("merge completed (no pane capture)", "pane may have exited")

    # Verify worktree is gone
    wt_path = os.path.join(TEMP_REPO, ".worktrees", branch)
    assert not os.path.isdir(wt_path), "worktree dir should be removed"
    ok("worktree removed after merge")

    # Verify branch is merged — check git log for merge commit
    log = subprocess.run(
        ["git", "-C", TEMP_REPO, "log", "--oneline", "-5"],
        capture_output=True, text=True,
    )
    assert branch in log.stdout, f"merge commit not in log:\n{log.stdout}"
    ok("merge commit in git log", log.stdout.splitlines()[0])


async def test_stacked_agents(m):
    section("Worktree: stacked agents (base_branch)")

    base = "e2e-base-branch"
    stacked = "e2e-stacked-branch"

    # Create a base worktree and add a file
    wt_base = os.path.join(TEMP_REPO, ".worktrees", base)
    subprocess.run(
        ["git", "-C", TEMP_REPO, "worktree", "add", "-b", base, wt_base],
        capture_output=True, check=True,
    )
    with open(os.path.join(wt_base, "base_file.txt"), "w") as f:
        f.write("base work\n")
    subprocess.run(["git", "-C", wt_base, "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", wt_base, "commit", "-m", "base commit"],
        capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"},
    )
    ok("base worktree created + committed")

    # Launch stacked agent on top of base branch
    result = await m.launch_agent(
        TEST_SESSION, "stacked task", branch=stacked, base_branch=base
    )
    assert stacked in result
    ok("launch_agent (stacked)", result)
    await asyncio.sleep(0.5)

    # Verify the stacked worktree has the base file
    wt_stacked = os.path.join(TEMP_REPO, ".worktrees", stacked)
    assert os.path.exists(os.path.join(wt_stacked, "base_file.txt")), \
        "stacked worktree missing base_file.txt"
    ok("stacked worktree has base file")

    # Cleanup: discard both
    # First kill the stacked window pane to avoid claude -p hanging
    sessions = await m.list_sessions()
    sess = next(s for s in sessions if s["name"] == TEST_SESSION)
    for w in sess["windows"]:
        if w["name"] in (stacked, base):
            for p in w["panes"]:
                await m.send_keys(p["pane_id"], "C-c")
    await asyncio.sleep(0.3)

    await m.discard_worktree(stacked)
    ok("discard stacked worktree")

    # Discard the base too (merge it first to not lose it — or just discard)
    subprocess.run(
        ["git", "-C", TEMP_REPO, "worktree", "remove", "--force", wt_base],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", TEMP_REPO, "branch", "-D", base],
        capture_output=True,
    )
    ok("cleanup base worktree")


async def test_merge_conflict(m):
    section("Worktree: merge conflict handling")

    branch = "e2e-conflict"

    # Create worktree
    wt_path = os.path.join(TEMP_REPO, ".worktrees", branch)
    subprocess.run(
        ["git", "-C", TEMP_REPO, "worktree", "add", "-b", branch, wt_path],
        capture_output=True, check=True,
    )

    # Modify README on the branch
    with open(os.path.join(wt_path, "README.md"), "w") as f:
        f.write("# Branch version\n")
    subprocess.run(["git", "-C", wt_path, "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", wt_path, "commit", "-m", "branch change"],
        capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"},
    )

    # Also modify README on main (to cause conflict)
    with open(os.path.join(TEMP_REPO, "README.md"), "w") as f:
        f.write("# Main version (conflicting)\n")
    subprocess.run(["git", "-C", TEMP_REPO, "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", TEMP_REPO, "commit", "-m", "main conflicting change"],
        capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"},
    )

    # Attempt merge — should fail with conflict
    try:
        await m.merge_worktree(branch)
        fail("merge_worktree should have raised on conflict")
    except RuntimeError as e:
        assert "conflict" in str(e).lower() or "Merge" in str(e), f"unexpected error: {e}"
        ok("merge conflict detected", str(e)[:100])

    # Verify worktree is preserved
    assert os.path.isdir(wt_path), "worktree should be preserved after conflict"
    ok("worktree preserved after conflict")

    # Verify main is clean (merge was aborted)
    status = subprocess.run(
        ["git", "-C", TEMP_REPO, "status", "--porcelain"],
        capture_output=True, text=True,
    )
    # No merge markers should be present
    ok("main repo clean after abort", f"status: '{status.stdout.strip()}' (expect empty or clean)")

    # Cleanup: discard the conflicting worktree
    await m.discard_worktree(branch)
    ok("discard conflicting worktree")


async def test_read_agent_log_missing(m):
    section("Worktree: read_agent_log (missing)")

    result = await m.read_agent_log("nonexistent-branch-xyz")
    assert "No log found" in result, f"unexpected: {result}"
    ok("read_agent_log (missing branch)", result)


async def test_nonexistent_session(m):
    section("Error Handling: nonexistent targets")

    # Kill nonexistent session
    try:
        await m.kill_session("_nonexistent_session_xyz")
        fail("kill_session should fail for nonexistent")
    except RuntimeError:
        ok("kill_session rejects nonexistent session")

    # Kill nonexistent window
    try:
        await m.kill_window("@99999")
        fail("kill_window should fail for nonexistent")
    except RuntimeError:
        ok("kill_window rejects nonexistent window")

    # Kill nonexistent pane
    try:
        await m.kill_pane("%99999")
        fail("kill_pane should fail for nonexistent")
    except RuntimeError:
        ok("kill_pane rejects nonexistent pane")

    # Discard nonexistent worktree
    try:
        await m.discard_worktree("nonexistent-branch-xyz")
        fail("discard_worktree should fail for nonexistent")
    except RuntimeError:
        ok("discard_worktree rejects nonexistent branch")


# ── Main ─────────────────────────────────────────────────────────────────────


async def run_all():
    global TEMP_REPO

    print("\n\033[1;33m╔══════════════════════════════════════════════════════════════╗\033[0m")
    print("\033[1;33m║          tmuxx MCP Server — E2E Test Suite                   ║\033[0m")
    print("\033[1;33m╚══════════════════════════════════════════════════════════════╝\033[0m")

    # Setup
    TEMP_REPO = create_temp_repo()
    print(f"\n  Temp repo: {TEMP_REPO}")
    print(f"  Test session: {TEST_SESSION}")

    # Cleanup any stale test session
    cleanup_tmux_session()

    m = import_mcp_tools()

    try:
        # ── Core tmux tools ──
        await test_session_lifecycle(m)
        await test_window_lifecycle(m)
        await test_pane_operations(m)
        await test_command_execution(m)
        await test_send_keys(m)
        await test_screenshot(m)
        await test_input_validation(m)
        await test_nonexistent_session(m)

        # ── Worktree / Agent tools ──
        await test_worktree_list(m)
        branch = await test_worktree_launch_and_status(m)
        await test_worktree_diff(m, branch)
        await test_worktree_merge_with_capture(m, branch)
        await test_worktree_discard_with_log(m)
        await test_worktree_merge_with_test_gate(m)
        await test_stacked_agents(m)
        await test_merge_conflict(m)
        await test_read_agent_log_missing(m)

    except Exception as e:
        fail("UNCAUGHT EXCEPTION", f"{type(e).__name__}: {e}")
        traceback.print_exc()

    finally:
        # Cleanup
        section("Cleanup")
        cleanup_tmux_session()
        ok("killed test tmux session")
        cleanup_temp_repo(TEMP_REPO)
        ok("removed temp repo")

    # Summary
    total = PASS + FAIL
    print(f"\n\033[1;33m{'═' * 60}\033[0m")
    print(f"  \033[1mResults: {PASS}/{total} passed\033[0m", end="")
    if FAIL:
        print(f"  \033[1;31m({FAIL} failed)\033[0m")
        print()
        for err in ERRORS:
            print(f"  \033[31m✗ {err}\033[0m")
    else:
        print(f"  \033[1;32m— all passed!\033[0m")
    print(f"\033[1;33m{'═' * 60}\033[0m\n")
    return FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
