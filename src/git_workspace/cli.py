import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Hashable, Mapping
from pathlib import Path
from typing import Literal, overload

import click
import yaml
from cachetools import LRUCache
from cachetools.keys import hashkey
from pydantic import BaseModel, Field, model_serializer, model_validator

MARKER = "!!! git-ws workspace marker !!!"
ROOT: str
VERBOSE = 0


class Branch(BaseModel):
    name: str
    base: str | None

    @model_validator(mode="before")
    @classmethod
    def migrate_old_branches_schema(cls, value):
        match value:
            case str():
                return {"name": value, "base": None}
            case _:
                return value

    @model_serializer(mode="wrap")
    def serialize_model(self, handler):
        if self.base is None:
            return self.name

        return handler(self)


class Config(BaseModel):
    branches: list[Branch] = Field(default_factory=list)
    base: str = "main"
    name: str = "workspace"
    remote: str = ""


class StepProgress:
    def __init__(self, total):
        self.total = total
        self.current = 0

    @staticmethod
    def echo(message):
        click.secho(
            f"\n{message}",
            fg="bright_white",
            bold=True,
            underline=True)

    def step(self, message):
        self.current += 1
        StepProgress.echo(f"Step {self.current}/{self.total}: {message}")


def printable_command(cmd: list[str], masking_opts=("-m",)):
    ret = []
    mask_next = False
    for c in cmd:
        if mask_next:
            c = "'...'"
            mask_next = False
        if " " in c:
            ret.append(f"'{c}'")
        else:
            ret.append(c)
        if c in masking_opts:
            mask_next = True
    return " ".join(ret)


_GIT_CACHE = LRUCache(1000)


@overload
def git(params: str | list[str], *,
    capture: Literal[False],
    rc_map: None = None) -> Literal[""]: ...
@overload
def git(params: str | list[str], *,
    capture: bool = True,
    rc_map: None = None) -> str: ...
@overload
def git[T](params: str | list[str], *,
    capture: bool = True,
    rc_map: Mapping[int, T]) -> T: ...


def git(params: str | list[str], *, capture: bool = True,
        rc_map: Mapping[int, Hashable] | None = None):
    if isinstance(params, str):
        cmd = ["git", *params.split(" ")]
    else:
        cmd = ["git", *params]

    subcommand = next(p for p in cmd[1:] if not p.startswith("-"))
    is_mutating = subcommand not in [
        "for-each-ref",
        "log",
        "merge-base",
        "rev-parse",
        "show-ref",
        "show",
        "status",
        ]
    
    cache_key = hashkey(*cmd, capture, tuple(sorted((rc_map or {}).items())))
    if is_mutating:
        _GIT_CACHE.clear()
    else:
        if cache_key in _GIT_CACHE:
            return _GIT_CACHE[cache_key]

    if is_mutating and VERBOSE >= 0:
        click.secho("+" + printable_command(cmd), fg="yellow")
    elif not is_mutating and VERBOSE >= 1:
        click.secho(" " + printable_command(cmd), fg="yellow")
    result = subprocess.run(cmd, capture_output=capture, text=True)
    rc = result.returncode
    if rc_map:
        try:
            ret = rc_map[rc]
        except KeyError as e:
            sys.stderr.write(
                f"Failed while executing: {printable_command(cmd)}\n")
            sys.stderr.write("STDOUT:\n")
            sys.stderr.write(result.stdout)
            sys.stderr.write("\n")
            sys.stderr.write("STDERR:\n")
            sys.stderr.write(result.stderr)
            sys.stderr.write("\n")
            raise click.ClickException("Error while running git") from e
    else:
        if rc:
            raise click.ClickException(result.stderr)
        if capture:
            ret = result.stdout.rstrip()
        else:
            ret = ""

    _GIT_CACHE[cache_key] = ret
    return ret


def git_branch_is_local(branch: str) -> bool:
    try:
        git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        return True
    except click.ClickException:
        return False


ROOT = git("rev-parse --show-toplevel")


def list_git_branches():
    raw = git("for-each-ref --format=%(refname:short) refs/heads/")
    return [
        b.strip()
        for b in raw.split("\n") if b]


def find_megamerge_hash(cfg: Config, *, check_workspace=True) -> str | None:
    branches = [b.name for b in cfg.branches]
    if check_workspace:
        branches.append(cfg.name)
    raw = git(
        ["log", f'--grep={MARKER}', '--pretty=format:%H', *branches, "--"])
    commits = [b.strip() for b in raw.split("\n") if b]
    if not commits:
        return None
    if len(commits) > 1:
        raise click.ClickException("Multiple workspace commits found")
    return commits[0]


def find_branch_hash(branch: str) -> str | None:
    raw = git(f"rev-parse --revs-only {branch}")
    commits = [b.strip() for b in raw.split("\n") if b]
    if not commits:
        return None
    if len(commits) > 1:
        raise RuntimeError("Shouldn't happen")
    return commits[0]


def load_config():
    try:
        with open(f"{ROOT}/.gitws") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        return Config()

    return Config.model_validate(cfg)


def save_config(config):
    with open(f"{ROOT}/.gitws", "w") as f:
        yaml.dump(config.model_dump(), f)


def is_clean(include_untracked=False):
    if include_untracked:
        return git("status --porcelain") == ""
    else:
        return git("status --porcelain -uno") == ""


def fail_if_dirty():
    if not is_clean():
        raise click.ClickException("The working copy is not clean")


def is_up(cfg: Config):
    has_branch = cfg.name in list_git_branches()
    has_merge = find_megamerge_hash(cfg, check_workspace=has_branch) is not None
    if has_merge != has_branch:
        raise click.ClickException("Workspace is in inconsistent state")
    return has_branch


def validate_branch_dependencies(cfg: Config):
    ret_status = True
    real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
    for branch in cfg.branches:
        is_ancestor = git(
            ["merge-base", "--is-ancestor", real_base, branch.name],
            rc_map={0: True, 1: False})
        if not is_ancestor:
            click.secho(
                f"ERROR: {branch.name} is not a decendant of {real_base}")
            ret_status = False

        if branch.base:
            if branch.base not in [b.name for b in cfg.branches]:
                click.secho(
                    f"ERROR: Branch {branch.name!r} has base branch "
                    f"{branch.base!r} which is not in workspace", fg="red")
                ret_status = False
            is_ancestor = git(
                ["merge-base", "--is-ancestor", branch.base, branch.name],
                rc_map={0: True, 1: False})
            if not is_ancestor:
                click.secho(
                    f"ERROR: {branch.name} is not a decendant of "
                    f"{branch.base}")
                ret_status = False
    return ret_status


def dependency_order(branches: list[Branch]) -> list[Branch]:
    complete = set()
    remaining = list(branches)
    result = []

    while remaining:
        ready = [b for b in remaining if b.base is None or b.base in complete]

        if not ready:
            raise click.ClickException(
                "Unable to resolve dependency order. "
                f"Remaining: {[b.name for b in remaining]}")

        result.extend(ready)
        complete.update(b.name for b in ready)
        remaining = [b for b in remaining if b.name not in complete]

    return result


@click.group(invoke_without_command=True)
@click.option("--silent", "-s", count=True,
    help="The opposite of --verbose")
@click.option("--verbose", "-v", count=True, envvar="GIT_WS_VERBOSE",
    help="Increase verbosity (repeat max 2 times)")
@click.pass_context
def cli(ctx, silent: int, verbose: int):
    global VERBOSE
    if verbose:
        VERBOSE = max(-1, min(1, verbose - silent))
    if ctx.invoked_subcommand is None:
        ctx.invoke(status)


@cli.command
def validate():
    validate_branch_dependencies(load_config())


@cli.command
def show():
    """Show the workspace contents"""
    cfg = load_config()
    real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
    click.secho("Name: ", fg="yellow", nl=False)
    click.echo(cfg.name)
    click.secho("Base: ", fg="yellow", nl=False)
    click.echo(real_base)
    click.secho("Branches:", fg="yellow")
    for branch in cfg.branches:
        if branch.base:
            base_str = click.style(f"(on {branch.base})", dim=True)
            click.echo(f"- {branch.name} {base_str}")
        else:
            click.echo(f"- {branch.name}")


@cli.command
@click.option("-b", "is_new_branch", is_flag=True)
@click.option("--onto", type=str, metavar="BASE_BRANCH")
@click.argument("branch", type=str)
def add(is_new_branch: bool, onto: str, branch: str):
    """Add a branch to the workspace

    Add an existing branch to the workspace. The base branch can not be added.
    If invoked with -b, a new branch is created and added. If --onto
    BASE_BRANCH is supplied, the new branch will be based on BASE_BRANCH,
    otherwise it will be based on the workspace base."""
    fail_if_dirty()
    cfg = load_config()
    workspace_branches = set(b.name for b in cfg.branches)
    if branch in workspace_branches:
        raise click.ClickException(
            f"Branch '{branch}' is already in workspace")
    if not is_new_branch and branch not in list_git_branches():
        raise click.ClickException(
            f"Branch '{branch}' not found")
    elif is_new_branch and branch in list_git_branches():
        raise click.ClickException(
            f"Branch '{branch}' already exists (and -b specified)")
    if branch == cfg.base:
        raise click.ClickException(
            f"Branch '{branch}' is workspace base and cannot be added")
    if onto and onto not in workspace_branches:
        raise click.ClickException(
            f"Base branch '{onto}' is not in workspace")

    cfg.branches.append(Branch(name=branch, base=onto))
    save_config(cfg)

    if is_new_branch:
        real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
        git(["branch", "--no-track", branch, onto or real_base])


@cli.command
@click.argument("branch", type=str)
def remove(branch):
    """Remove a branch from the workspace"""
    fail_if_dirty()
    cfg = load_config()
    if branch not in set(b.name for b in cfg.branches):
        raise click.ClickException(
            f"Branch '{branch}' is not in the workspace")

    dependants = [b.name for b in cfg.branches if b.base == branch]
    if dependants:
        listed = ", ".join(f"'{name}'" for name in dependants)
        raise click.ClickException(
            f"Branch '{branch}' is the base of {listed} and cannot be removed")

    cfg.branches = [b for b in cfg.branches if b.name != branch]
    save_config(cfg)


@cli.command
def status():
    """Prints a status of the workspace

    Shows commit list for all branches and a short git status
    for the working copy"""
    cfg = load_config()
    if not is_up(cfg):
        raise click.ClickException("No workspace active")
    workspace_commit = find_branch_hash(cfg.name)
    merge_commit = find_megamerge_hash(cfg)
    real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
    for branch in cfg.branches:
        base = branch.base or real_base
        git(f"log --oneline --graph {base}..{branch.name}", capture=False)
        print()

    if workspace_commit != merge_commit:
        click.secho("WARNING: Workspace has extra commits:", fg="red")
        git(f"log --oneline --graph {merge_commit}..{cfg.name}", capture=False)
        print()

    if is_clean(include_untracked=True):
        click.echo("Working copy is ", nl=False)
        click.secho("clean", fg='green', bold=True)
    else:
        git("status -bs", capture=False)


@cli.command
def up():
    """Creates the workspace

    Creates the workspace branch and merges all added brances into it."""
    fail_if_dirty()
    cfg = load_config()
    if is_up(cfg):
        return
    if len(cfg.branches) < 2:
        raise click.ClickException("Too few branches added")
    real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
    real_base_hash = find_branch_hash(real_base)
    if not real_base_hash:
        raise click.ClickException("Unable to find base branch hash")
    git(f"checkout --no-track -b {cfg.name} {real_base}")
    message = f"git-ws managed branch\n\n{MARKER}"
    branch_names = [b.name for b in cfg.branches]
    git(["merge", "--no-ff", "-m", message, *branch_names])
    if find_branch_hash(cfg.name) == real_base_hash:
        git(["commit", "--allow-empty", "-m", message])


@cli.command
def down():
    """Removes the workspace

    Deletes the workspace branch. If destructive, a confirmation is
    shown"""
    fail_if_dirty()
    cfg = load_config()
    if not is_up(cfg):
        return

    # TODO: Find commits after marker and check if they are
    # cherry-picked onto the other branches
    if MARKER not in git(f"show -s --format=%B {cfg.name}"):
        click.secho(
            f"There are commits on '{cfg.name}' that will be lost",
            fg="red")
        click.confirm("Are you sure?", abort=True)
    git(f"checkout {cfg.base}")
    git(f"branch -D {cfg.name}")


@cli.command
@click.argument("branch", type=str)
def shell(branch: str):
    """Create an interactive shell into a given branch

    Create a new temporary worktree for the given branch
    and launch a new shell in it.

    NOTE: contents of the worktree will be deleted on exit"""
    if branch not in list_git_branches():
        raise click.ClickException(f"Branch '{branch}' not found")
    tmux_pane = os.getenv("TMUX_PANE")
    tempdir = Path(tempfile.mkdtemp())
    workdir = tempdir / f"{Path(ROOT).name}/{branch}".replace("/", "-")
    try:
        git(f"worktree add {workdir} {branch}")
        env = os.environ
        click.secho("=" * 80, fg="yellow")
        click.secho(f"git-ws shell for branch {branch}", fg="yellow")
        click.secho("=" * 80, fg="yellow")
        if tmux_pane:
            subprocess.run([
                "tmux", "select-pane", "-t", tmux_pane, "-P", "bg=#381018"])
        shell = os.environ.get("SHELL", "/bin/sh")
        subprocess.run([shell], cwd=workdir, env=env)
    finally:
        if tmux_pane:
            subprocess.run([
                "tmux", "select-pane", "-t", tmux_pane, "-P", "bg=default"])
        click.secho("=" * 80, fg="yellow")
        shutil.rmtree(tempdir)
        git("worktree prune")


@cli.command
@click.pass_context
def rebase(ctx):
    """Rebases all workspace branches on the base

    Effectively down's the workspace, then rebase each of the workspace
    branches on base. Finally, up's the workspace"""
    fail_if_dirty()

    log_path = Path(f"{ROOT}/git-workspace.log")
    try:
        with log_path.open("x") as f:
            f.write("Before 'git workspace rebase':\n")
            f.write(git("for-each-ref"))
            f.write("\n")
    except FileExistsError as err:
        raise click.ClickException(f"{log_path} already exists") from err

    logfile_unlinked = False
    try:
        cfg = load_config()

        old_hashes = {b.name: find_branch_hash(b.name) for b in cfg.branches}

        progress = StepProgress(len(cfg.branches) + 2)
        progress.step("Tear down workspace")
        try:
            ctx.invoke(down)
        except click.Abort:
            log_path.unlink()
            logfile_unlinked = True
            raise

        real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
        for branch in dependency_order(cfg.branches):
            base = branch.base or real_base
            progress.step(f"Rebase {branch.name} onto {base}")
            if not git_branch_is_local(branch.name):
                click.secho(f"Branch '{branch.name}' is not local. Skipping...")
                continue
            if branch.base:
                # TODO: stacked branches should use:
                #       git rebase --onto base old_base branch
                git(f"rebase --onto {base} {old_hashes[base]} {branch.name}",
                    capture=False)
            else:
                git(f"rebase {base} {branch.name}", capture=False)

        progress.step("Bring up workspace")
        ctx.invoke(up)

        log_path.unlink()
        logfile_unlinked = True
    finally:
        if not logfile_unlinked:
            click.secho(
                "Something went wrong. Original branch state stored in "
                f"{log_path}", fg="red")


@cli.command
def ls_branches():
    cfg = load_config()
    workspace_branches = set(b.name for b in cfg.branches)
    git_branches = set(list_git_branches())
    for branch in sorted(git_branches.difference(workspace_branches)):
        click.echo(branch)


if __name__ == "__main__":
    cli()
