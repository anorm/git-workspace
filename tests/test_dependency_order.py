"""Unit tests for `git_workspace.cli.dependency_order`.

`dependency_order` topologically sorts workspace branches so that every
branch appears after the branch it is stacked on. It resolves the graph
in levels: on each pass it emits every branch whose base is either
`None` (rooted on the workspace base) or already emitted.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import click
import pytest

# ---------------------------------------------------------------------------
# Module-import fixture
# ---------------------------------------------------------------------------
# Importing `git_workspace.cli` eagerly evaluates
# `ROOT = git("rev-parse --show-toplevel")`, which shells out to real git,
# so the import has to happen from inside a git work tree. This repo is
# one, so chdir here for the duration of the import.


@pytest.fixture(scope="module")
def cli_module() -> Any:
    """Import `git_workspace.cli` from inside this repository."""
    root = Path(__file__).resolve().parent.parent
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    prev_cwd = os.getcwd()
    os.chdir(root)
    try:
        import git_workspace.cli as cli
        return cli
    finally:
        os.chdir(prev_cwd)


@pytest.fixture
def Branch(cli_module):
    return cli_module.Branch


@pytest.fixture
def dependency_order(cli_module):
    return cli_module.dependency_order


@pytest.fixture
def make_branches(Branch):
    """Build a branch list from `(name, base)` pairs or bare names."""
    def _make(*specs: str | tuple[str, str | None]):
        out = []
        for spec in specs:
            match spec:
                case str():
                    out.append(Branch(name=spec, base=None))
                case (name, base):
                    out.append(Branch(name=name, base=base))
        return out
    return _make


def names(branches) -> list[str]:
    return [b.name for b in branches]


# ---------------------------------------------------------------------------
# Trivial / root-only inputs
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty(dependency_order):
    assert dependency_order([]) == []


def test_single_root_branch(dependency_order, make_branches):
    branches = make_branches("feature/a")
    assert names(dependency_order(branches)) == ["feature/a"]


def test_root_branches_preserve_input_order(dependency_order, make_branches):
    branches = make_branches("feature/c", "feature/a", "feature/b")

    result = dependency_order(branches)

    assert names(result) == ["feature/c", "feature/a", "feature/b"]


def test_empty_string_base_is_not_treated_as_root(
        dependency_order, make_branches):
    """Only `None` marks a root; `""` is a (missing) branch name.

    `add` stores `None` when `--onto` is omitted, so this guards the
    distinction rather than a reachable code path.
    """
    branches = make_branches(("feature/a", ""))

    with pytest.raises(click.ClickException):
        dependency_order(branches)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_dependent_branch_follows_its_base(dependency_order, make_branches):
    branches = make_branches("feature/a", ("feature/b", "feature/a"))

    assert names(dependency_order(branches)) == ["feature/a", "feature/b"]


def test_out_of_order_input_is_sorted(dependency_order, make_branches):
    branches = make_branches(("feature/b", "feature/a"), "feature/a")

    assert names(dependency_order(branches)) == ["feature/a", "feature/b"]


def test_long_chain_is_fully_ordered(dependency_order, make_branches):
    branches = make_branches(
        ("d", "c"),
        ("b", "a"),
        ("c", "b"),
        "a",
    )

    assert names(dependency_order(branches)) == ["a", "b", "c", "d"]


def test_diamond_dependency(dependency_order, make_branches):
    """`b` and `c` both sit on `a`; `d` sits on `b`."""
    branches = make_branches(
        ("d", "b"),
        ("c", "a"),
        ("b", "a"),
        "a",
    )

    result = names(dependency_order(branches))

    assert result.index("a") < result.index("b")
    assert result.index("a") < result.index("c")
    assert result.index("b") < result.index("d")


def test_siblings_keep_relative_input_order(dependency_order, make_branches):
    """Branches resolved in the same pass stay in input order."""
    branches = make_branches(
        "a",
        ("z", "a"),
        ("y", "a"),
        ("x", "a"),
    )

    assert names(dependency_order(branches)) == ["a", "z", "y", "x"]


def test_independent_stacks_are_both_resolved(
        dependency_order, make_branches):
    branches = make_branches(
        ("b", "a"),
        ("d", "c"),
        "a",
        "c",
    )

    result = names(dependency_order(branches))

    assert sorted(result) == ["a", "b", "c", "d"]
    assert result.index("a") < result.index("b")
    assert result.index("c") < result.index("d")


def test_every_branch_appears_after_its_base(dependency_order, make_branches):
    branches = make_branches(
        ("e", "d"),
        ("c", "a"),
        "a",
        ("d", "b"),
        ("b", "a"),
        "f",
    )

    result = names(dependency_order(branches))
    positions = {name: i for i, name in enumerate(result)}

    assert len(result) == len(branches)
    for branch in branches:
        if branch.base is not None:
            assert positions[branch.base] < positions[branch.name]


# ---------------------------------------------------------------------------
# Result identity / input immutability
# ---------------------------------------------------------------------------


def test_returns_the_same_branch_objects(dependency_order, make_branches):
    branches = make_branches("a", ("b", "a"))

    result = dependency_order(branches)

    assert {id(b) for b in result} == {id(b) for b in branches}


def test_input_list_is_not_mutated(dependency_order, make_branches):
    branches = make_branches(("b", "a"), "a")
    before = list(branches)

    dependency_order(branches)

    assert branches == before
    assert names(branches) == ["b", "a"]


# ---------------------------------------------------------------------------
# Unresolvable graphs
# ---------------------------------------------------------------------------


def test_two_branch_cycle_raises(dependency_order, make_branches):
    branches = make_branches(("a", "b"), ("b", "a"))

    with pytest.raises(
            click.ClickException,
            match="Unable to resolve dependency order"):
        dependency_order(branches)


def test_self_referential_base_raises(dependency_order, make_branches):
    branches = make_branches(("a", "a"))

    with pytest.raises(
            click.ClickException,
            match="Unable to resolve dependency order"):
        dependency_order(branches)


def test_base_outside_the_workspace_raises(dependency_order, make_branches):
    """A base must itself be a workspace branch.

    The workspace base (e.g. `main`) is spelled as `None`, not by name.
    """
    branches = make_branches(("feature/a", "main"))

    with pytest.raises(
            click.ClickException,
            match="Unable to resolve dependency order"):
        dependency_order(branches)


def test_dangling_base_raises(dependency_order, make_branches):
    """A base naming a branch that is absent is unresolvable.

    `add` and `remove` both guard against writing such a config, so this
    covers a hand-edited or externally-produced `.gitws`.
    """
    branches = make_branches("a", ("c", "b"))

    with pytest.raises(
            click.ClickException,
            match="Unable to resolve dependency order"):
        dependency_order(branches)


def test_error_reports_only_the_unresolved_branches(
        dependency_order, make_branches):
    branches = make_branches("a", ("b", "a"), ("x", "y"), ("y", "x"))

    with pytest.raises(click.ClickException) as excinfo:
        dependency_order(branches)

    message = str(excinfo.value)
    assert "'x'" in message
    assert "'y'" in message
    assert "'a'" not in message
    assert "'b'" not in message


@pytest.mark.parametrize("cycle_length", [2, 3, 5])
def test_cycles_of_any_length_raise(
        dependency_order, make_branches, cycle_length):
    specs = [
        (f"b{i}", f"b{(i + 1) % cycle_length}")
        for i in range(cycle_length)
    ]
    branches = make_branches(*specs)

    with pytest.raises(
            click.ClickException,
            match="Unable to resolve dependency order"):
        dependency_order(branches)
