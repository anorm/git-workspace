"""Shared fixtures for git-workspace integration tests."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """Hermetic env so tests don't read user/system git config."""
    home = tmp_path / "_home"
    home.mkdir()
    return {
        "PATH": _system_path(),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        # Make sure git doesn't try to be clever about default branch names
        "GIT_TERMINAL_PROMPT": "0",
    }


def _system_path() -> str:
    import os
    return os.environ.get("PATH", "/usr/bin:/bin")


def _run_git(
        args: list[str], cwd: Path, env: dict[str, str]
        ) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {proc.stderr}"
        )
    return proc


@pytest.fixture
def repo(tmp_path: Path, git_env: dict[str, str]) -> Path:
    """A fresh git repo with a single commit on `main`."""
    path = tmp_path / "repo"
    path.mkdir()

    # Try modern -b flag first; fall back for older git
    init = subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=path, env=git_env, capture_output=True, text=True,
    )
    if init.returncode != 0:
        _run_git(["init"], path, git_env)
        _run_git(["checkout", "-b", "main"], path, git_env)

    (path / "README.md").write_text("# test repo\n")
    _run_git(["add", "README.md"], path, git_env)
    _run_git(["commit", "-m", "initial"], path, git_env)
    return path


@pytest.fixture
def gitws(repo: Path, git_env: dict[str, str]):
    """Callable that runs the git-workspace CLI as a subprocess."""

    # Make sure the package is importable in the subprocess.
    src_dir = Path(__file__).resolve().parent.parent / "src"
    env = {**git_env, "PYTHONPATH": str(src_dir)}

    def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "git_workspace.cli", *args],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=check,
        )

    return _run


@pytest.fixture
def make_branch(repo: Path, git_env: dict[str, str]):
    """Create a branch off current HEAD with one commit, then return to main."""

    def _make(name: str, filename: str, content: str = "hello\n") -> None:
        _run_git(["checkout", "-b", name], repo, git_env)
        (repo / filename).write_text(content)
        _run_git(["add", filename], repo, git_env)
        _run_git(["commit", "-m", f"add {filename} on {name}"], repo, git_env)
        _run_git(["checkout", "main"], repo, git_env)

    return _make


@pytest.fixture
def run_git(repo: Path, git_env: dict[str, str]):
    """Generic git runner bound to the test repo."""

    def _run(*args: str) -> subprocess.CompletedProcess:
        return _run_git(list(args), repo, git_env)

    return _run
