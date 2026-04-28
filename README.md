# git-workspace

A Git extension for working on multiple feature branches simultaneously by 
merging them into a single ephemeral "workspace" branch.

## What it is

`git-workspace` (invoked as `git-workspace` or `git workspace`) lets you 
maintain a set of in-flight branches and combine them into one workspace 
branch on demand. This is useful when you have several independent 
changes you want to test together, develop against in parallel, or keep 
checked out as a single working copy without polluting any of the 
underlying feature branches.

The workspace is created by merging all configured branches with 
`--no-ff` into a freshly-branched workspace branch. The merge commit is 
tagged with a marker so the tool can manage it safely. Configuration is 
stored in a `.gitws` YAML file at the repo root.

Key features:
- Add/remove branches from the workspace set
- Bring the workspace `up` (merge all branches) or `down` (delete the 
  workspace branch)
- Rebase all member branches onto the base in one shot, automatically 
  tearing down and rebuilding the workspace
- Spawn a temporary worktree shell into any branch
- Status overview showing per-branch commits ahead of base

## Installation

Requires Python 3.14+ and [`uv`](https://github.com/astral-sh/uv).

```sh
uv tool install git+https://github.com/anorm/git-workspace
```

Or run directly from a clone:

```sh
uv run git-workspace
```

## How to use it

Typical workflow:

```sh
# Add branches you want to combine
git workspace add feature/foo
git workspace add feature/bar
git workspace add -b feature/new   # create and add a new branch off base

# Bring the workspace up (creates 'workspace' branch with all merged)
git workspace up

# See what's in the workspace
git workspace status

# Drop into a temp worktree shell for one branch
git workspace shell feature/foo

# Rebase every member branch onto base, then rebuild the workspace
git workspace rebase

# Tear down the workspace
git workspace down

# Remove a branch from the set
git workspace remove feature/bar
```

Run with `--verbose` (or set `GIT_WS_VERBOSE=1`) to see every git command 
executed.

### Commands

| Command | Description |
| --- | --- |
| `add [-b] BRANCH` | Add an existing branch (or create with `-b`) to the workspace |
| `remove BRANCH` | Remove a branch from the workspace |
| `up` | Create the workspace branch by merging all member branches |
| `down` | Delete the workspace branch |
| `status` | Show per-branch commits and working copy status |
| `rebase` | Down, rebase each member branch on base, then up |
| `shell BRANCH` | Open an interactive shell in a temp worktree for `BRANCH` |

## How to contribute

Contributions are welcome.

1. Fork the repo and create a feature branch
2. Set up the dev environment:
   ```sh
   uv sync
   ```
3. Make your changes. Keep edits focused; the entire CLI lives in 
   `src/git_workspace/cli.py`
4. Verify the tool still works against a scratch repo:
   ```sh
   uv run git-workspace --verbose status
   ```
5. Run tests:
   ```sh
   uv run pytest
   ```
6. Open a pull request with a clear description of the problem being solved 
   and any behavioral changes

When reporting bugs, please include:
- The command you ran (with `--verbose` output)
- Your `.gitws` contents
- The relevant `git log --oneline --all --graph` excerpt

## License

See [LICENSE](LICENSE).
