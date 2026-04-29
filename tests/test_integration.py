"""Integration tests for git-workspace.

Each test invokes the CLI via a subprocess against a freshly-initialized
git repo, exercising the user-facing commands end to end.
"""
from __future__ import annotations

from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Config / add / remove
# ---------------------------------------------------------------------------


def test_status_without_workspace_fails(gitws):
    result = gitws("status")
    assert result.returncode != 0
    assert "No workspace active" in result.stderr


def test_add_branch_writes_config(gitws, make_branch, repo: Path):
    make_branch("feature/a", "a.txt")
    make_branch("feature/b", "b.txt")

    assert gitws("add", "feature/a").returncode == 0
    assert gitws("add", "feature/b").returncode == 0

    cfg = yaml.safe_load((repo / ".gitws").read_text())
    assert cfg["branches"] == ["feature/a", "feature/b"]
    assert cfg["base"] == "main"
    assert cfg["name"] == "workspace"


def test_add_nonexistent_branch_fails(gitws):
    result = gitws("add", "does-not-exist")
    assert result.returncode != 0
    assert "not found" in result.stderr


def test_add_base_branch_fails(gitws):
    result = gitws("add", "main")
    assert result.returncode != 0
    assert "base" in result.stderr.lower()


def test_add_with_dash_b_creates_branch(gitws, run_git, repo: Path):
    result = gitws("add", "-b", "feature/new")
    assert result.returncode == 0, result.stderr

    branches = run_git("branch", "--list", "feature/new").stdout
    assert "feature/new" in branches

    cfg = yaml.safe_load((repo / ".gitws").read_text())
    assert "feature/new" in cfg["branches"]


def test_add_duplicate_fails(gitws, make_branch):
    make_branch("feature/a", "a.txt")
    assert gitws("add", "feature/a").returncode == 0

    result = gitws("add", "feature/a")
    assert result.returncode != 0
    assert "already in workspace" in result.stderr


def test_remove_branch(gitws, make_branch, repo: Path):
    make_branch("feature/a", "a.txt")
    make_branch("feature/b", "b.txt")
    gitws("add", "feature/a")
    gitws("add", "feature/b")

    result = gitws("remove", "feature/a")
    assert result.returncode == 0, result.stderr

    cfg = yaml.safe_load((repo / ".gitws").read_text())
    assert cfg["branches"] == ["feature/b"]


def test_remove_branch_not_in_workspace_fails(gitws):
    result = gitws("remove", "feature/missing")
    assert result.returncode != 0
    assert "not in the workspace" in result.stderr


# ---------------------------------------------------------------------------
# Up / down lifecycle
# ---------------------------------------------------------------------------


def test_up_requires_two_branches(gitws, make_branch):
    make_branch("feature/a", "a.txt")
    gitws("add", "feature/a")

    result = gitws("up")
    assert result.returncode != 0
    assert "Too few branches" in result.stderr


def test_full_up_down_cycle(gitws, make_branch, run_git, repo: Path):
    make_branch("feature/a", "a.txt", "alpha\n")
    make_branch("feature/b", "b.txt", "bravo\n")
    assert gitws("add", "feature/a").returncode == 0
    assert gitws("add", "feature/b").returncode == 0

    up = gitws("up")
    assert up.returncode == 0, up.stderr

    # On the workspace branch
    head = run_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == "workspace"

    # Both feature files present in the working tree
    assert (repo / "a.txt").read_text() == "alpha\n"
    assert (repo / "b.txt").read_text() == "bravo\n"

    # Marker commit exists exactly once
    marker_log = run_git(
        "log", "--all",
        "--grep=!!! git-ws workspace marker !!!",
        "--pretty=format:%H",
    ).stdout.strip().splitlines()
    assert len(marker_log) == 1

    # Status now succeeds
    status = gitws("status")
    assert status.returncode == 0, status.stderr

    # Down it
    down = gitws("down")
    assert down.returncode == 0, down.stderr

    branches = run_git("branch", "--list", "workspace").stdout.strip()
    assert branches == ""
    head = run_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == "main"


def test_up_is_idempotent(gitws, make_branch, run_git):
    make_branch("feature/a", "a.txt")
    make_branch("feature/b", "b.txt")
    gitws("add", "feature/a")
    gitws("add", "feature/b")

    assert gitws("up").returncode == 0
    second = gitws("up")
    assert second.returncode == 0, second.stderr

    branches = run_git("branch", "--list", "workspace").stdout
    assert "workspace" in branches


def test_down_when_not_up_is_noop(gitws):
    # Should not error even if no workspace is active.
    result = gitws("down")
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Rebase
# ---------------------------------------------------------------------------


def test_rebase_flow(gitws, make_branch, run_git, repo: Path):
    make_branch("feature/a", "a.txt", "alpha\n")
    make_branch("feature/b", "b.txt", "bravo\n")
    gitws("add", "feature/a")
    gitws("add", "feature/b")
    assert gitws("up").returncode == 0

    # Add a new commit on main while workspace is up.
    run_git("checkout", "main")
    (repo / "base.txt").write_text("base\n")
    run_git("add", "base.txt")
    run_git("commit", "-m", "base advance")
    run_git("checkout", "workspace")

    rebase = gitws("rebase")
    assert rebase.returncode == 0, rebase.stderr

    # The new base commit should be reachable through the rebuilt workspace.
    assert (repo / "base.txt").read_text() == "base\n"
    assert (repo / "a.txt").read_text() == "alpha\n"
    assert (repo / "b.txt").read_text() == "bravo\n"

    # Workspace is back up
    branches = run_git("branch", "--list", "workspace").stdout
    assert "workspace" in branches

    # Log file is cleaned up on success
    assert not (repo / "git-workspace.log").exists()


# ---------------------------------------------------------------------------
# Dirty working copy guard
# ---------------------------------------------------------------------------


def test_dirty_working_copy_blocks_add(gitws, make_branch, repo: Path):
    make_branch("feature/a", "a.txt")

    # Modify a tracked file (README.md is tracked from the initial commit)
    (repo / "README.md").write_text("dirty\n")

    result = gitws("add", "feature/a")
    assert result.returncode != 0
    assert "not clean" in result.stderr


def test_churn_cycle(gitws, make_branch, run_git, repo: Path):
    """Up, down, swap a branch, up again."""
    make_branch("feature/a", "a.txt")
    make_branch("feature/b", "b.txt")
    make_branch("feature/c", "c.txt")

    gitws("add", "feature/a")
    gitws("add", "feature/b")
    assert gitws("up").returncode == 0
    assert gitws("down").returncode == 0

    assert gitws("remove", "feature/a").returncode == 0
    assert gitws("add", "feature/c").returncode == 0

    assert gitws("up").returncode == 0

    assert (repo / "b.txt").exists()
    assert (repo / "c.txt").exists()
    assert not (repo / "a.txt").exists()
