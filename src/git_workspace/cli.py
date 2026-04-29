import os
import subprocess

import click
import yaml
from pydantic import BaseModel, Field


MARKER = "!!! git-ws workspace marker !!!"
ROOT: str
VERBOSE = False


class Config(BaseModel):
    branches: list[str] = Field(default_factory=list)
    base: str = "main"
    name: str = "workspace"
    remote: str = ""


def printable_command(cmd: list[str], masking_opts=["-m"]):
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


def git(params: str | list[str], *, capture: bool = True):
    if isinstance(params, str):
        cmd = ["git", *params.split(" ")]
    else:
        cmd = ["git", *params]

    if VERBOSE:
        click.secho(printable_command(cmd), fg="yellow")
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if result.returncode:
        raise click.ClickException(result.stderr)
    if capture:
        return result.stdout.rstrip()
    else:
        return ""


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


def find_megamerge_hash() -> str | None:
    raw = git(["log", "--all", f'--grep={MARKER}', '--pretty=format:%H'])
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
        raise click.ClickException("Multiple workspace commits found")
    return commits[0]


def load_config():
    try:
        with open(f"{ROOT}/.gitws", "r") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        cfg = Config()

    return Config.model_validate(cfg)


def save_config(config):
    with open(f"{ROOT}/.gitws", "wb") as f:
        yaml.dump(config.model_dump(), f, encoding="utf-8")


def is_clean(include_untracked=False):
    if include_untracked:
        return git("status --porcelain") == ""
    else:
        return git("status --porcelain -uno") == ""


def fail_if_dirty():
    if not is_clean():
        raise click.ClickException("The working copy is not clean")


def is_up():
    cfg = load_config()
    return cfg.name in list_git_branches()


@click.group(invoke_without_command=True)
@click.option("--verbose", is_flag=True, envvar="GIT_WS_VERBOSE")
@click.pass_context
def cli(ctx, verbose):
    global VERBOSE
    if verbose:
        VERBOSE = True
    if ctx.invoked_subcommand is None:
        ctx.invoke(status)


@cli.command
@click.option("-b", "is_new_branch", is_flag=True)
@click.argument("branch", type=str)
def add(is_new_branch: bool, branch: str):
    """Add a branch to the workspace

    Add an exising branch to the workspace. The base branch can not be
    added. If invoked with -b, a new branch is created and added. The
    new branch will be based on the workspace base"""
    fail_if_dirty()
    cfg = load_config()
    branches = set(cfg.branches)
    if branch in branches:
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

    branches.add(branch)
    cfg.branches = sorted(list(branches))
    save_config(cfg)

    if is_new_branch:
        real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
        git(["branch", "--no-track", branch, real_base])


@cli.command
@click.argument("branch", type=str)
def remove(branch):
    """Remove a branch from the workspace"""
    fail_if_dirty()
    cfg = load_config()
    if branch not in cfg.branches:
        raise click.ClickException(
            f"Branch '{branch}' is not in the workspace")
    cfg.branches.remove(branch)
    save_config(cfg)


@cli.command
def status():
    """Prints a status of the workspace

    Shows commit list for all branches and a short git status
    for the working copy"""
    cfg = load_config()
    merge_commit = find_megamerge_hash()
    workspace_commit = find_branch_hash(cfg.name)
    if not merge_commit or not workspace_commit:
        raise click.ClickException("No workspace active")
    real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
    for branch in cfg.branches:
        git(f"log --oneline --graph {real_base}..{branch}", capture=False)
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
    if is_up():
        return

    cfg = load_config()
    if len(cfg.branches) < 2:
        raise click.ClickException("Too few branches added")
    real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
    real_base_hash = find_branch_hash(real_base)
    if not real_base_hash:
        raise click.ClickException("Unable to find base branch hash")
    git(f"checkout --no-track -b {cfg.name} {real_base}")
    message = f"git-ws managed branch\n\n{MARKER}"
    git(["merge", "--no-ff", "-m", message, *cfg.branches])
    if find_branch_hash(cfg.name) == real_base_hash:
        git(["commit", "--allow-empty", "-m", message])


@cli.command
def down():
    """Removes the workspace

    Deletes the workspace branch. If destructive, a confirmation is
    shown"""
    fail_if_dirty()
    if not is_up():
        return

    cfg = load_config()
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
    import tempfile
    from pathlib import Path
    tempdir = Path(tempfile.mkdtemp())
    workdir = tempdir / f"{Path(ROOT).name}/{branch}".replace("/", "-")
    try:
        git(f"worktree add {workdir} {branch}")
        env = os.environ
        click.secho("="*80, fg="yellow")
        click.secho(f"git-ws shell for branch {branch}", fg="yellow")
        click.secho("="*80, fg="yellow")
        if tmux_pane:
            subprocess.run([
                "tmux", "select-pane", "-t", tmux_pane, "-P", "bg=#381018"])
        subprocess.run(["zsh", "-i"], cwd=workdir, env=env)
        if tmux_pane:
            subprocess.run([
                "tmux", "select-pane", "-t", tmux_pane, "-P", "bg=default"])
        click.secho("*"*80, fg="yellow")
    finally:
        import shutil
        shutil.rmtree(tempdir)
        git("worktree prune")


@cli.command
@click.pass_context
def rebase(ctx):
    """Rebases all workspace branches on the base

    Effectvely down's the workspace, then rebase each of the workspace
    branches on base. Finally, up's the workspace"""
    fail_if_dirty()

    try:
        with open("git-workspace.log", "x") as f:
            f.write("Before 'git workspace rebase':\n")
            f.write(git("for-each-ref"))
            f.write("\n")
    except FileExistsError:
        raise click.ClickException("git-workspace.log already exists")

    msg = " Taking workspace DOWN "
    click.secho(f"\n{msg:=^80}")
    ctx.invoke(down)

    cfg = load_config()
    real_base = f"{cfg.remote}/{cfg.base}" if cfg.remote else cfg.base
    for branch in cfg.branches:
        if not git_branch_is_local(branch):
            click.secho(f"Branch '{branch}' is not local. Skipping...")
            continue
        msg = f" Rebasing {branch} onto {real_base} "
        click.secho(f"\n{msg:=^80}")
        git(f"rebase {real_base} {branch}", capture=False)

    msg = " Taking workspace UP "
    click.secho(f"\n{msg:=^80}")
    ctx.invoke(up)

    os.unlink("git-workspace.log")


if __name__ == "__main__":
    cli()
