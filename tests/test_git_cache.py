"""Unit tests for the LRU caching mechanism in `git_workspace.cli.git`."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module-import fixture
# ---------------------------------------------------------------------------
# Importing `git_workspace.cli` eagerly evaluates
# `ROOT = git("rev-parse --show-toplevel")`, which shells out to real git.
# We import once from inside a real repo so the module loads cleanly, then
# every test mocks `subprocess.run` and clears the cache for isolation.


@pytest.fixture(scope="module")
def cli_module(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Import `git_workspace.cli` from inside a throwaway real git repo."""
    import os
    import sys

    bootstrap = tmp_path_factory.mktemp("bootstrap_repo")
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=bootstrap,
        capture_output=True, check=False,
    )
    # Fallback for old git
    if not (bootstrap / ".git").exists():
        subprocess.run(["git", "init"], cwd=bootstrap, check=True)

    prev_cwd = os.getcwd()
    os.chdir(bootstrap)
    try:
        # Make sure src/ is importable
        src = Path(__file__).resolve().parent.parent / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))

        # Force a fresh import so module-level git() runs in our bootstrap repo
        sys.modules.pop("git_workspace.cli", None)
        sys.modules.pop("git_workspace", None)
        import git_workspace.cli as cli  # noqa: WPS433
        return cli
    finally:
        os.chdir(prev_cwd)


@pytest.fixture
def cli(cli_module, monkeypatch: pytest.MonkeyPatch):
    """Per-test module handle with a clean cache and mocked subprocess.run."""
    cli_module._GIT_CACHE.clear()
    return cli_module


@pytest.fixture
def fake_run(cli, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace `subprocess.run` inside cli with a counting mock.

    Default behaviour: returncode=0, stdout="OUT", stderr="".
    Tests can mutate `fake_run.return_value` per call via side_effect.
    """
    mock = MagicMock(spec=subprocess.run)
    mock.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="OUT\n", stderr="",
    )
    monkeypatch.setattr(cli.subprocess, "run", mock)
    return mock


# ---------------------------------------------------------------------------
# Cache hit / miss for read-only subcommands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subcmd", [
    "for-each-ref",
    "log",
    "merge-base",
    "rev-parse",
    "show-ref",
    "show",
    "status",
])
def test_read_only_subcommand_is_cached(cli, fake_run, subcmd):
    """Read-only subcommands return cached result on the second call."""
    first = cli.git([subcmd, "--foo"])
    second = cli.git([subcmd, "--foo"])

    assert first == "OUT"
    assert second == "OUT"
    # Only one real subprocess invocation despite two calls
    assert fake_run.call_count == 1


def test_cache_miss_executes_subprocess(cli, fake_run):
    cli.git("rev-parse HEAD")
    assert fake_run.call_count == 1


def test_distinct_args_produce_distinct_cache_entries(cli, fake_run):
    fake_run.side_effect = [
        subprocess.CompletedProcess([], 0, "abc\n", ""),
        subprocess.CompletedProcess([], 0, "def\n", ""),
    ]
    a = cli.git("rev-parse HEAD")
    b = cli.git("rev-parse main")

    assert a == "abc"
    assert b == "def"
    assert fake_run.call_count == 2


def test_string_and_list_forms_share_cache_key(cli, fake_run):
    """`git("log --oneline")` and `git(["log", "--oneline"])` are equivalent."""
    cli.git("log --oneline")
    cli.git(["log", "--oneline"])
    assert fake_run.call_count == 1


def test_capture_flag_is_part_of_cache_key(cli, fake_run):
    cli.git("status", capture=True)
    cli.git("status", capture=False)
    assert fake_run.call_count == 2


def test_rc_map_is_part_of_cache_key(cli, fake_run):
    cli.git("status")
    cli.git("status", rc_map={0: "ok"})
    assert fake_run.call_count == 2


def test_rc_map_order_independent_in_cache_key(cli, fake_run):
    """`rc_map` items are sorted before hashing → dict order doesn't matter."""
    cli.git("status", rc_map={0: "a", 1: "b"})
    cli.git("status", rc_map={1: "b", 0: "a"})
    assert fake_run.call_count == 1


# ---------------------------------------------------------------------------
# Cache invalidation (`cache_nuke`) for write subcommands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("write_subcmd", [
    "commit",
    "checkout",
    "branch",
    "merge",
    "rebase",
    "worktree",
    "add",
    "push",
    "fetch",
])
def test_write_subcommand_clears_entire_cache(cli, fake_run, write_subcmd):
    # Prime the cache with two reads
    cli.git("rev-parse HEAD")
    cli.git("status")
    assert len(cli._GIT_CACHE) == 2

    # A write command nukes pre-existing entries before executing.
    # (The write's own result is then stored, leaving exactly 1 entry.)
    cli.git([write_subcmd])
    assert len(cli._GIT_CACHE) == 1
    cached_keys = list(cli._GIT_CACHE.keys())
    assert all(write_subcmd in key for key in cached_keys)


def test_write_subcommand_result_is_not_cached(cli, fake_run):
    """Write commands run every time, even with identical args.

    Each invocation nukes the cache (including any prior identical write
    result) before executing, so we always re-shell-out.
    """
    cli.git(["commit", "-m", "x"])
    cli.git(["commit", "-m", "x"])
    assert fake_run.call_count == 2


def test_read_after_write_re_executes(cli, fake_run):
    """A read cached before a write must be re-fetched after the write."""
    fake_run.side_effect = [
        subprocess.CompletedProcess([], 0, "first\n", ""),
        subprocess.CompletedProcess([], 0, "", ""),       # commit
        subprocess.CompletedProcess([], 0, "second\n", ""),
    ]
    assert cli.git("rev-parse HEAD") == "first"
    cli.git(["commit", "-m", "x"])
    assert cli.git("rev-parse HEAD") == "second"
    assert fake_run.call_count == 3


def test_subcommand_detection_skips_flags_but_not_flag_values(cli, fake_run):
    """Subcommand = first token not starting with `-`.

    This is naive: `git -C /tmp status` parses `/tmp` as the subcommand
    (not `status`), so it falls through to the cache-nuking branch.
    Documenting current behaviour rather than the ideal.
    """
    cli.git(["-C", "/tmp", "status"])
    cli.git(["-C", "/tmp", "status"])
    # `/tmp` isn't in the read-only allow-list → no caching.
    assert fake_run.call_count == 2


def test_subcommand_detection_with_pure_flag_prefix(cli, fake_run):
    """A flag without a value (e.g. `--no-pager`) is correctly skipped."""
    cli.git(["--no-pager", "status"])
    cli.git(["--no-pager", "status"])
    # `status` is detected as the subcommand → cacheable.
    assert fake_run.call_count == 1


# ---------------------------------------------------------------------------
# Error paths and edge cases
# ---------------------------------------------------------------------------

def test_failed_command_is_not_cached(cli, fake_run):
    import click

    fake_run.side_effect = [
        subprocess.CompletedProcess([], 1, "", "boom\n"),
        subprocess.CompletedProcess([], 0, "ok\n", ""),
    ]
    with pytest.raises(click.ClickException):
        cli.git("status")
    # Second call must hit subprocess again — failures aren't memoised
    assert cli.git("status") == "ok"
    assert fake_run.call_count == 2


def test_rc_map_suppresses_nonzero_exit(cli, fake_run):
    """When rc_map maps the actual return code, return the mapped value
    instead of raising ClickException."""
    fake_run.return_value = subprocess.CompletedProcess(
        [], 1, "", "ref not found\n",
    )
    result = cli.git(
        ["show-ref", "--verify", "--quiet", "refs/heads/missing"],
        rc_map={1: "missing"},
    )
    assert result == "missing"


def test_rc_map_nonmatching_code_still_raises(cli, fake_run):
    """rc_map only suppresses codes it explicitly maps; others still raise."""
    import click

    fake_run.return_value = subprocess.CompletedProcess(
        [], 2, "", "fatal\n",
    )
    with pytest.raises(click.ClickException):
        cli.git("status", rc_map={1: "ok"})


def test_rc_map_value_is_cached(cli, fake_run):
    """A successfully-mapped nonzero exit caches the mapped value."""
    fake_run.return_value = subprocess.CompletedProcess(
        [], 1, "", "missing\n",
    )
    first = cli.git("show-ref refs/heads/x", rc_map={1: "absent"})
    second = cli.git("show-ref refs/heads/x", rc_map={1: "absent"})
    assert first == second == "absent"
    assert fake_run.call_count == 1


def test_capture_false_caches_empty_string(cli, fake_run):
    out = cli.git("status", capture=False)
    assert out == ""
    # Second call returns the cached empty string without re-running
    assert cli.git("status", capture=False) == ""
    assert fake_run.call_count == 1


def test_lru_eviction_at_capacity(cli_module, monkeypatch: pytest.MonkeyPatch):
    """Cache evicts the least-recently-used entry once full."""
    from cachetools import LRUCache

    # Shrink the cache for a tractable test
    monkeypatch.setattr(cli_module, "_GIT_CACHE", LRUCache(2))
    fake = MagicMock(spec=subprocess.run)
    fake.return_value = subprocess.CompletedProcess([], 0, "X\n", "")
    monkeypatch.setattr(cli_module.subprocess, "run", fake)

    cli_module.git("rev-parse A")
    cli_module.git("rev-parse B")
    assert len(cli_module._GIT_CACHE) == 2

    # Inserting a third entry evicts the oldest (rev-parse A)
    cli_module.git("rev-parse C")
    assert len(cli_module._GIT_CACHE) == 2

    # Re-requesting A is now a miss
    cli_module.git("rev-parse A")
    assert fake.call_count == 4


def test_cache_returns_same_object_on_hit(cli, fake_run):
    """A cache hit returns the stored value verbatim (no re-execution)."""
    first = cli.git("show HEAD")
    fake_run.return_value = subprocess.CompletedProcess(
        [], 0, "DIFFERENT\n", "",
    )
    second = cli.git("show HEAD")
    # Despite the mock now returning something else, we get the cached value
    assert first == second == "OUT"
    assert fake_run.call_count == 1
