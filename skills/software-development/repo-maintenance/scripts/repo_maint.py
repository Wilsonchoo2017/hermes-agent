#!/usr/bin/env python3
"""Non-authoring maintenance on a git repo's MAIN checkout.

The main checkout is shared: another session may have files mid-edit, so
agents author in linked worktrees and never commit there. But a few chores
still have to happen in main -- fast-forwarding it, clearing away worktrees
and branches that have already landed, and discarding a local edit that has
since landed upstream by another route. None of them is authoring.

This script is where that distinction gets enforced. Each verb refuses
unless its precondition holds, so the guarantee is a property of the code
rather than of the caller's good intentions:

    sync  fast-forward only, and only from a clean tree
    gc    remove worktrees/branches only when proven contained upstream
    drop  discard a local edit only when proven contained upstream

Nothing here writes file content, resolves a conflict, or creates a commit.
`drop` is the only verb that destroys anything, and it refuses unless it can
prove the change already exists upstream -- and saves a recovery ref anyway.

Usage:

    repo_maint.py sync <repo> [--remote origin]
    repo_maint.py gc   <repo> [--remote origin] [--dry-run]
    repo_maint.py drop <repo> <path>... [--remote origin] [--dry-run]
    repo_maint.py --self-check
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BULLET = "  "

# Set the moment anything is actually changed on disk. `gc` walks several
# worktrees, so a failure partway through must not claim nothing happened --
# that is the one line a reader trusts when deciding not to investigate.
CHANGED: list[str] = []


def changed(what: str) -> None:
    CHANGED.append(what)


class Refused(SystemExit):
    """A precondition failed. Reports honestly whether anything changed."""

    def __init__(self, reason: str, detail: list[str] | None = None):
        lines = [f"REFUSED: {reason}"]
        lines += [BULLET + d for d in (detail or [])]
        if CHANGED:
            lines.append(BULLET + f"Stopped after {len(CHANGED)} change(s):")
            lines += [BULLET + BULLET + c for c in CHANGED]
        else:
            lines.append(BULLET + "Nothing done.")
        super().__init__("\n".join(lines))


def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run git in `repo`, returning stripped stdout."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed",
                      [proc.stderr.strip() or proc.stdout.strip()])
    return proc.stdout.strip()


def ok(repo: Path, *args: str) -> bool:
    """True when the git command exits 0. For predicates, never for effects."""
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode == 0


def repo_root(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise Refused(f"no such path: {p}")
    root = git(p, "rev-parse", "--show-toplevel")
    return Path(root)


def is_main_worktree(repo: Path) -> bool:
    """A linked worktree's gitdir lives under <common>/worktrees/."""
    gitdir = git(repo, "rev-parse", "--absolute-git-dir")
    return "/worktrees/" not in gitdir


def default_branch(repo: Path, remote: str) -> str:
    """The remote's HEAD branch, falling back to main then master."""
    head = git(repo, "symbolic-ref", f"refs/remotes/{remote}/HEAD", check=False)
    if head:
        return head.rsplit("/", 1)[-1]
    for guess in ("main", "master"):
        if ok(repo, "rev-parse", "--verify", f"refs/remotes/{remote}/{guess}"):
            return guess
    raise Refused(f"cannot determine {remote}'s default branch")


def dirty_tracked(repo: Path) -> list[str]:
    """Tracked files with staged or unstaged changes. Untracked are ignored."""
    out = git(repo, "status", "--porcelain", "--untracked-files=no")
    return [ln for ln in out.splitlines() if ln.strip()]


def dirty_any(repo: Path) -> list[str]:
    """Anything at all in the tree, untracked included.

    `sync` can ignore untracked files -- they do not block a fast-forward.
    `gc` cannot: a worktree whose only content is untracked files is still
    somebody's work in progress, and its branch looks identical to main
    precisely because nothing has been committed yet.
    """
    out = git(repo, "status", "--porcelain")
    return [ln for ln in out.splitlines() if ln.strip()]


def contained(repo: Path, ref: str, upstream: str) -> bool:
    """True when merging `ref` into `upstream` would change nothing.

    Not ancestry: a squash-merge rewrites history, so `--merged` reports a
    landed branch as unmerged.

    Not a plain `git diff upstream ref` either, which is the trap this
    replaced. That diff is symmetric -- it also reports everything upstream
    gained since the branch forked, so a branch that landed cleanly starts
    failing the check the moment anything else merges. It fails closed, so
    nothing is lost, but it quietly stops cleaning anything up.

    merge-tree asks the question we actually mean: build the merge result
    and compare it with upstream's tree. Equal means the branch adds
    nothing, no matter how far upstream has moved on.
    """
    merged = git(repo, "merge-tree", "--write-tree", upstream, ref, check=False)
    if not merged:
        return False        # conflict, or git too old for --write-tree
    head = git(repo, "rev-parse", f"{upstream}^{{tree}}")
    return merged.splitlines()[0].strip() == head


# --- verbs -----------------------------------------------------------------

def cmd_sync(repo: Path, remote: str) -> int:
    if not is_main_worktree(repo):
        raise Refused("this is a linked worktree, not a main checkout",
                      ["Use git directly here; the gate does not block it."])

    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise Refused("HEAD is detached", ["Check out a branch first."])

    base = default_branch(repo, remote)
    if branch != base:
        raise Refused(f"on '{branch}', not the default branch '{base}'",
                      ["sync only fast-forwards the default branch."])

    dirty = dirty_tracked(repo)
    if dirty:
        raise Refused("working tree dirty",
                      dirty + ["A peer may be mid-edit."])

    git(repo, "fetch", remote, "--quiet")
    old = git(repo, "rev-parse", "--short", "HEAD")
    target = f"{remote}/{base}"
    new = git(repo, "rev-parse", "--short", target)

    if old == new:
        print(f"{BULLET}{old}  already up to date")
        return 0
    if not ok(repo, "merge-base", "--is-ancestor", "HEAD", target):
        raise Refused(f"HEAD is not an ancestor of {target}",
                      ["A fast-forward would lose local commits.",
                       "Rebase or merge deliberately, in a worktree."])

    git(repo, "merge", "--ff-only", target)
    changed(f"fast-forwarded {base} {old}..{new}")
    print(f"{BULLET}{old}..{new}  ff-only  OK")
    return 0


def worktrees(repo: Path) -> list[dict]:
    """Parse `git worktree list --porcelain` into dicts."""
    out = git(repo, "worktree", "list", "--porcelain")
    entries, cur = [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val
    if cur:
        entries.append(cur)
    return entries


def cmd_gc(repo: Path, remote: str, dry_run: bool) -> int:
    if not is_main_worktree(repo):
        raise Refused("run gc from the main checkout")

    base = default_branch(repo, remote)
    git(repo, "fetch", remote, "--quiet", "--prune")
    upstream = f"{remote}/{base}"
    here = Path.cwd().resolve()
    acted, pruned = 0, False

    for wt in worktrees(repo):
        path = Path(wt.get("worktree", ""))
        branch = wt.get("branch", "").replace("refs/heads/", "")
        if not branch or branch == base or path == repo:
            continue

        if not path.exists():
            print(f"{BULLET}worktree {path}  gone from disk  pruning")
            if not dry_run and not pruned:
                git(repo, "worktree", "prune")   # prunes every stale entry at once
                changed("pruned stale worktree entries")
                pruned = True
            acted += 1
            continue

        if path == here or here.is_relative_to(path):
            print(f"{BULLET}worktree {path}  SKIPPED  you are standing in it")
            continue

        local_dirty = dirty_any(path)
        if local_dirty:
            print(f"{BULLET}worktree {path}  SKIPPED  uncommitted changes")
            for d in local_dirty:
                print(f"{BULLET}{BULLET}{d}")
            continue

        if not contained(repo, branch, upstream):
            print(f"{BULLET}worktree {path}  SKIPPED  holds work not in {upstream}")
            continue

        sha = git(repo, "rev-parse", "--short", branch)
        print(f"{BULLET}worktree {path}  tree-identical to {base}  "
              f"{'would remove' if dry_run else 'removed'}")
        if not dry_run:
            git(repo, "worktree", "remove", str(path))
            changed(f"removed worktree {path}")
            git(repo, "branch", "-D", branch)
            changed(f"deleted branch {branch} (was {sha})")
        print(f"{BULLET}branch   {branch}  contained at {sha}  "
              f"{'would delete' if dry_run else 'deleted'}")
        acted += 1

    if not acted:
        print(f"{BULLET}nothing to clean")
    elif dry_run:
        print(f"{BULLET}dry run -- re-run without --dry-run to act")
    return 0


def upstream_blob(repo: Path, ref: str, rel: str) -> str | None:
    """The file's content at `ref`, or None when it does not exist there."""
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{rel}"],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def change_is_upstream(repo: Path, ref: str, rel: str) -> tuple[str, str]:
    """Classify the local edit to `rel` against `ref`.

    Returns one of: proven, no-change, absent, conflict, diverged. These
    are genuinely different situations and the caller must not collapse
    them -- "there is nothing to discard" is not "this holds unsaved work".

    The test is a three-way merge, the same shape `contained()` uses for a
    branch: merge the local file into the upstream file using HEAD as the
    base. If the result is byte-identical to upstream, the local edit
    contributes nothing upstream does not already have.

    Two cheaper tests were tried and rejected:

    - **Line membership** -- does every added line appear somewhere in the
      upstream file? Passes by coincidence. A line can occur in an
      unrelated part of the file and prove nothing about the edit.
    - **Reverse-applying the local patch to upstream.** Too brittle: a
      diff carries three lines of context, so an unrelated insertion two
      lines away makes a landed edit look unlanded.

    Comparing content directly cannot work at all. Upstream is typically
    HEAD's file plus this local edit plus other people's later work, so it
    equals neither side.
    """
    if not git(repo, "diff", "HEAD", "--", rel).strip():
        return "no-change", "no local change -- nothing to discard"

    up = upstream_blob(repo, ref, rel)
    if up is None:
        return "absent", f"file does not exist at {ref}"
    base = upstream_blob(repo, "HEAD", rel)
    if base is None:
        return "absent", "file does not exist at HEAD"

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "local").write_text((repo / rel).read_text())
        (td / "base").write_text(base)
        (td / "up").write_text(up)
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--quiet",
             str(td / "local"), str(td / "base"), str(td / "up")],
            capture_output=True, text=True)

    if proc.returncode < 0:
        return "absent", "merge failed"
    if proc.returncode > 0:
        return "conflict", f"conflicts with {ref}"
    if proc.stdout == up:
        return "proven", f"merging it into {ref} changes nothing"
    return "diverged", f"local change is NOT in {ref} -- it would be lost"


def cmd_drop(repo: Path, remote: str, paths: list[str], dry_run: bool) -> int:
    if not is_main_worktree(repo):
        raise Refused("run drop from the main checkout")

    base = default_branch(repo, remote)
    git(repo, "fetch", remote, "--quiet")
    ref = f"{remote}/{base}"

    plan = []
    # Resolve both sides: on macOS the repo root comes back as /var/... while
    # a resolved path is /private/var/..., and relative_to would reject it.
    root = repo.resolve()
    for raw in paths:
        abs_path = Path(raw).expanduser().resolve()
        try:
            rel = str(abs_path.relative_to(root))
        except ValueError:
            raise Refused(f"{abs_path} is not inside {root}")
        if not ok(repo, "ls-files", "--error-unmatch", "--", rel):
            raise Refused(f"{rel} is not tracked",
                          ["drop only discards edits to tracked files.",
                           "An untracked file is somebody's new work."])
        status, why = change_is_upstream(repo, ref, rel)
        label = {"proven": "PROVEN", "no-change": "SKIP"}.get(status, "REFUSED")
        print(f"{BULLET}{rel}")
        print(f"{BULLET}{BULLET}{label}  {why}")

        if status == "proven":
            plan.append(rel)
        elif status == "no-change":
            continue          # already clean; re-running drop is a no-op
        elif status == "conflict":
            raise Refused(f"{rel} conflicts with {ref}",
                          ["Someone changed the same lines upstream.",
                           "This needs a person to reconcile, not a discard."])
        elif status == "diverged":
            raise Refused(f"{rel} holds work not in {ref}",
                          ["Find its owner, or land it. Never stash it:",
                           "a peer may be mid-edit in this shared checkout."])
        else:
            raise Refused(f"{rel}: {why}")

    if not plan:
        print(f"{BULLET}nothing to discard")
        return 0
    if dry_run:
        print(f"{BULLET}dry run -- re-run without --dry-run to act")
        return 0

    # `stash create` builds a commit object without touching the working
    # tree, so the recovery ref costs nothing and changes nothing.
    stash = git(repo, "stash", "create", check=False)
    if stash:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        git(repo, "update-ref", f"refs/backup/drop-{ts}", stash)
        print(f"{BULLET}recovery ref  refs/backup/drop-{ts}  ({stash[:9]})")

    git(repo, "checkout", "--", *plan)
    for rel in plan:
        changed(f"discarded local edit to {rel}")
        print(f"{BULLET}{rel}  discarded")
    return 0


# --- self-check ------------------------------------------------------------

def self_check() -> int:

    def run(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, check=True)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        origin, clone = td / "origin.git", td / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                       check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        run(clone, "config", "user.email", "t@t")
        run(clone, "config", "user.name", "t")
        (clone / "a.txt").write_text("one\n")
        run(clone, "add", "a.txt")
        run(clone, "commit", "-qm", "first")
        run(clone, "push", "-q", "origin", "main")

        assert is_main_worktree(clone)
        assert default_branch(clone, "origin") == "main"

        # sync with nothing to do
        assert cmd_sync(clone, "origin") == 0

        # a dirty tracked file must refuse, and must not fetch or merge
        (clone / "a.txt").write_text("edited\n")
        try:
            cmd_sync(clone, "origin")
            raise AssertionError("dirty tree should have refused")
        except Refused as e:
            assert "dirty" in str(e) and "Nothing done." in str(e)
        (clone / "a.txt").write_text("one\n")

        # an untracked file must NOT block a sync
        (clone / "scratch.tmp").write_text("junk\n")
        assert cmd_sync(clone, "origin") == 0
        (clone / "scratch.tmp").unlink()

        # a real fast-forward
        other = td / "other"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
        run(other, "config", "user.email", "t@t")
        run(other, "config", "user.name", "t")
        (other / "b.txt").write_text("two\n")
        run(other, "add", "b.txt")
        run(other, "commit", "-qm", "second")
        run(other, "push", "-q", "origin", "main")
        before = git(clone, "rev-parse", "HEAD")
        assert cmd_sync(clone, "origin") == 0
        assert git(clone, "rev-parse", "HEAD") != before
        assert (clone / "b.txt").exists()

        # a local commit makes it non-fast-forward: refuse rather than merge
        (clone / "c.txt").write_text("three\n")
        run(clone, "add", "c.txt")
        run(clone, "commit", "-qm", "local only")
        (other / "d.txt").write_text("four\n")
        run(other, "add", "d.txt")
        run(other, "commit", "-qm", "remote only")
        run(other, "push", "-q", "origin", "main")
        try:
            cmd_sync(clone, "origin")
            raise AssertionError("diverged history should have refused")
        except Refused as e:
            assert "not an ancestor" in str(e)
        run(clone, "reset", "-q", "--hard", "origin/main")
        assert cmd_sync(clone, "origin") == 0

        # gc: a branch holding real work is kept
        wt = td / "wt-live"
        run(clone, "worktree", "add", "-q", "-b", "live", str(wt))
        (wt / "e.txt").write_text("five\n")
        run(wt, "config", "user.email", "t@t")
        run(wt, "config", "user.name", "t")
        run(wt, "add", "e.txt")
        run(wt, "commit", "-qm", "unlanded work")
        cmd_gc(clone, "origin", dry_run=False)
        assert wt.exists(), "gc removed a worktree holding unlanded work"

        # gc: a branch whose tree matches main is removed
        wt2 = td / "wt-landed"
        run(clone, "worktree", "add", "-q", "-b", "landed", str(wt2))
        cmd_gc(clone, "origin", dry_run=True)
        assert wt2.exists(), "dry run must not act"
        cmd_gc(clone, "origin", dry_run=False)
        assert not wt2.exists(), "gc kept a branch identical to main"
        assert "landed" not in git(clone, "branch", "--list", "landed")

        # gc: a landed branch stays collectable after main moves on.
        # This is the regression that the old two-dot diff check failed.
        wt5 = td / "wt-old"
        run(clone, "worktree", "add", "-q", "-b", "old", str(wt5))
        (other / "f.txt").write_text("six\n")
        run(other, "add", "f.txt")
        run(other, "commit", "-qm", "upstream moves on")
        run(other, "push", "-q", "origin", "main")
        run(clone, "fetch", "-q", "origin")
        assert contained(clone, "old", "origin/main"), \
            "a landed branch stopped being collectable once main advanced"
        cmd_gc(clone, "origin", dry_run=False)
        assert not wt5.exists(), "gc kept a branch that adds nothing to main"

        # gc: untracked-only work still counts as work
        wt4 = td / "wt-untracked"
        run(clone, "worktree", "add", "-q", "-b", "fresh", str(wt4))
        (wt4 / "new.py").write_text("in progress\n")
        cmd_gc(clone, "origin", dry_run=False)
        assert wt4.exists(), "gc removed a worktree holding untracked work"

        # gc: uncommitted work in a worktree is never removed
        wt3 = td / "wt-dirty"
        run(clone, "worktree", "add", "-q", "-b", "dirty", str(wt3))
        (wt3 / "a.txt").write_text("mid-edit\n")
        cmd_gc(clone, "origin", dry_run=False)
        assert wt3.exists(), "gc removed a worktree with uncommitted changes"

        # drop: the real case -- upstream holds our edit plus other work.
        # base + local edit, where upstream has base + someone else's
        # section + the same local edit. Neither side equals the other.
        run(clone, "reset", "-q", "--hard", "origin/main")
        (clone / "doc.md").write_text("intro\nmiddle\nend\n")
        run(clone, "add", "doc.md")
        run(clone, "commit", "-qm", "doc base")
        run(clone, "push", "-q", "origin", "main")
        run(other, "pull", "-q", "origin", "main")
        (other / "doc.md").write_text("intro\nSOMEONE ELSE\nmiddle\nMY EDIT\nend\n")
        run(other, "add", "doc.md")
        run(other, "commit", "-qm", "both changes land upstream")
        run(other, "push", "-q", "origin", "main")
        run(clone, "fetch", "-q", "origin")
        (clone / "doc.md").write_text("intro\nmiddle\nMY EDIT\nend\n")
        status, why = change_is_upstream(clone, "origin/main", "doc.md")
        assert status == "proven", f"landed edit should be droppable: {why}"
        assert cmd_drop(clone, "origin", [str(clone / "doc.md")], False) == 0
        assert (clone / "doc.md").read_text() == "intro\nmiddle\nend\n"
        assert git(clone, "for-each-ref", "refs/backup"), "no recovery ref"

        # drop: a coincidence must not count as containment. "MY EDIT"
        # exists upstream, but not where this patch puts it.
        (clone / "doc.md").write_text("intro\nMY EDIT\nmiddle\nend\n")
        status, why = change_is_upstream(clone, "origin/main", "doc.md")
        assert status != "proven", \
            "a line matching elsewhere was accepted as contained"
        try:
            cmd_drop(clone, "origin", [str(clone / "doc.md")], False)
            raise AssertionError("uncontained edit should have refused")
        except Refused as e:
            # a conflict must be reported as a conflict, not as grounds
            # to discard, and never as "nothing to do"
            msg = str(e)
            assert "conflicts with origin/main" in msg, msg
            assert "needs a person to reconcile" in msg
        assert (clone / "doc.md").read_text() == "intro\nMY EDIT\nmiddle\nend\n"

        # drop on a clean file is a no-op, not a scary refusal: re-running
        # a maintenance verb must not report unsaved work that is not there
        run(clone, "checkout", "-q", "--", "doc.md")
        status, why = change_is_upstream(clone, "origin/main", "doc.md")
        assert status == "no-change", status
        assert cmd_drop(clone, "origin", [str(clone / "doc.md")], False) == 0

        # drop: untracked files are somebody's new work, never ours to bin
        (clone / "brand-new.txt").write_text("mine\n")
        try:
            cmd_drop(clone, "origin", [str(clone / "brand-new.txt")], False)
            raise AssertionError("untracked file should have refused")
        except Refused as e:
            assert "not tracked" in str(e)
        assert (clone / "brand-new.txt").exists()
        (clone / "brand-new.txt").unlink()
        run(clone, "checkout", "-q", "--", "doc.md")

        # a refusal after real work must not claim nothing happened
        CHANGED.clear()
        assert "Nothing done." in str(Refused("x"))
        changed("removed worktree /tmp/example")
        msg = str(Refused("y"))
        assert "Nothing done." not in msg, "refusal lied about a partial run"
        assert "Stopped after 1 change(s)" in msg
        assert "/tmp/example" in msg
        CHANGED.clear()

    print("self-check ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-check", action="store_true",
                    help="run the assertions and exit")
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("sync", help="fast-forward the main checkout")
    sp.add_argument("repo")
    sp.add_argument("--remote", default="origin")

    gp = sub.add_parser("gc", help="remove worktrees/branches already landed")
    gp.add_argument("repo")
    gp.add_argument("--remote", default="origin")
    gp.add_argument("--dry-run", action="store_true",
                    help="print the plan without acting")

    dp = sub.add_parser("drop",
                        help="discard local edits already landed upstream")
    dp.add_argument("repo")
    dp.add_argument("paths", nargs="+", help="tracked files to discard")
    dp.add_argument("--remote", default="origin")
    dp.add_argument("--dry-run", action="store_true",
                    help="prove containment without discarding")

    args = ap.parse_args()
    if args.self_check:
        return self_check()
    if not args.cmd:
        ap.print_help()
        return 2

    repo = repo_root(args.repo)
    if args.cmd == "sync":
        return cmd_sync(repo, args.remote)
    if args.cmd == "drop":
        return cmd_drop(repo, args.remote, args.paths, args.dry_run)
    return cmd_gc(repo, args.remote, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
