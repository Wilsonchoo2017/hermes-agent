---
name: repo-maintenance
description: "Fast-forward a shared main checkout, collect landed worktrees, and discard edits proven to be upstream -- without authoring in main."
version: 1.1.1
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  requires: [git, python3]
---

# Repo maintenance on a shared main checkout

## Why this exists

The hard rule is that agents author in linked worktrees and never in a repo's
main checkout, because the main checkout is shared -- another session may have
files mid-edit. `worktree-gate.js` enforces that by blocking `git commit`,
`push`, `merge`, `checkout`, `branch -D` and friends whenever a command targets
a main worktree.

The rule is right and the enforcement is blunt. Three chores that are *not*
authoring get caught by it:

- **Fast-forwarding main** after a PR merges. This moves a pointer to a commit
  that is already on the remote. It decides nothing and writes no content.
- **Clearing away worktrees and branches whose work has landed.** Also just
  pointers.
- **Discarding a local edit that has since landed upstream** by another route.
  This one does destroy something, so it has to prove itself first.

All three were blocked, so all three ended up as command lists pasted to a
human. That is not safety; it is the same operation with a slower, less careful
executor.

`repo_maint.py` does these chores and refuses everything else. It is not a
way around the gate's intent -- it is a more precise statement of it, because a
script can check a precondition that a regex over a command string cannot.

## The verbs

```bash
SKILL_DIR/scripts/repo_maint.py sync <repo> [--remote origin]
SKILL_DIR/scripts/repo_maint.py gc   <repo> [--remote origin] [--dry-run]
SKILL_DIR/scripts/repo_maint.py drop <repo> <path>... [--dry-run]
```

**`sync`** fast-forwards the main checkout. It refuses unless *all* hold:

- the path is a main worktree, not a linked one
- HEAD is on the remote's default branch, not detached
- no tracked file has staged or unstaged changes (untracked files are fine --
  they do not block a fast-forward)
- HEAD is an ancestor of the remote branch, so the move is genuinely a
  fast-forward and no local commit is lost

**`gc`** removes worktrees and branches that have already landed. Per worktree,
it refuses unless *all* hold:

- it is not the main worktree, and not the one you are standing in
- `git status --porcelain` is empty, **untracked files included**
- merging the branch into `<remote>/<default>` would change nothing

That last check needs care, and two obvious ways to write it are both wrong:

- **`git branch --merged`** misses squash-merges. Squashing rewrites history,
  so a branch that landed cleanly still reports as unmerged.
- **`git diff <upstream> <branch>`** is symmetric. It also reports everything
  upstream gained since the branch forked, so a landed branch stops passing the
  moment anything *else* merges. It fails closed -- nothing is destroyed -- but
  `gc` quietly stops collecting anything, which on a busy repo is always.

What the check uses instead is `git merge-tree --write-tree <upstream>
<branch>`, comparing the resulting tree with upstream's. Equal means the branch
adds nothing, however far upstream has moved on. The question is not "was this
merged?" but "would merging this change anything?"

**`drop`** discards a local edit to a tracked file, but only one that has
already reached upstream by another route -- typically because someone else
committed the same work, or it was rescued into a PR and merged. It refuses
unless *all* hold:

- the path is inside the repo and **tracked** (an untracked file is somebody's
  new work, never ours to bin)
- the file actually has a local change
- merging the local file into the upstream file, with HEAD as the base, yields
  the upstream file byte for byte

It distinguishes what it found, because these are not the same situation and
collapsing them makes the tool untrustworthy: `PROVEN` (droppable), `SKIP` (no
local change -- a no-op, so re-running `drop` is safe and exits 0), a conflict
(someone changed the same lines upstream; a person reconciles it, nobody
discards it), and diverged (the change exists nowhere else -- find its owner).

It writes a recovery ref (`refs/backup/drop-<timestamp>`) before discarding,
built with `git stash create`, which produces a commit object without touching
the working tree. Recover with `git checkout <ref> -- <path>`.

The containment test is a three-way merge, and getting there took two wrong
turns worth recording:

- **Line membership** -- "does every added line appear somewhere upstream?"
  passes by coincidence. A line can occur in an unrelated part of the file and
  say nothing about whether *this edit* landed. The self-check has a case for
  exactly this, and the naive version fails it.
- **Reverse-applying the local patch to upstream.** Sound in principle, brittle
  in practice: a diff carries three lines of context, so an unrelated insertion
  two lines away makes a landed edit look unlanded.

Direct content comparison cannot work at all, which is what makes this awkward:
upstream is normally HEAD's file *plus* your edit *plus* other people's later
work, so it is equal to neither side.

Use `--dry-run` first when you have not looked at the repo yet.

## What it will not do

There is no verb that writes file content, resolves a conflict, or creates a
commit. That is deliberate. If you need any of those, you are authoring, and
authoring belongs in a worktree:

```bash
git worktree add -b <branch> /tmp/<repo>-<task> origin/main
```

`drop` is the only verb that destroys anything, and it will not discard work
that is merely *probably* safe -- it has to prove containment or it refuses. A
conflict is reported as a conflict, never treated as grounds to discard.

If `sync` refuses because the tree is dirty and `drop` refuses because the
change is not upstream, that combination is the tool telling you something
real: somebody's work is sitting in a shared checkout and is not saved
anywhere. **Do not reach for `git stash`** -- the owner may be mid-edit, and
stashing yanks the file out from under them. Find them, or rescue the work into
a branch and a PR, which is what makes it droppable afterwards.

## Working with the gate

Two things about `worktree-gate.js` that cost time until they are known:

**It judges the whole command string.** A compound command is blocked if *any*
part of it is, so chaining a blocked op with safe ones blocks the lot:

```bash
# blocked entirely -- because of `branch -D`, though `worktree remove` is fine
git -C ~/repo worktree remove /tmp/wt && git -C ~/repo branch -D feat/x

# run them separately and the first one succeeds
```

**It already allows more than it looks like.** Read-only commands and mutations
of *untracked* files in main are permitted. `git worktree add`, `git fetch`,
`git status`, `git log`, `git diff` all work. Only the state-mutating
subcommands are blocked.

So before concluding you are stuck: unchain the command and try the parts.

## Self-check

```bash
SKILL_DIR/scripts/repo_maint.py --self-check
```

Builds throwaway repos in a temp dir and asserts the refusals actually fire:
a dirty tree blocks `sync`, an untracked file does not, diverged history is
refused rather than merged, `gc` leaves alone a worktree that holds unlanded
commits, untracked-only work or uncommitted changes, and `drop` discards a
landed edit while refusing an unlanded one, refusing an untracked file, and
refusing a line that merely appears elsewhere upstream.

Those assertions are the security property. If you change a precondition,
change its test in the same commit.
