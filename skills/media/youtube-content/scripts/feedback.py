#!/usr/bin/env python3
"""Append and read the durable feedback history for a book.

Books are written in rounds, driven by Wilson's feedback. This script makes
each round's feedback a permanent, timestamped, version-bound record rather
than something that lives only in a chat session -- so a future AI can
reconstruct what Wilson liked, disliked, and was learning toward, and against
which exact revision of the book.

Each book source directory (sources/youtube/<video_id>/) is its own git repo.
The record is `feedback.jsonl`, one JSON object per line (JSONL), tracked in
git alongside book.json. A line is appended once and never rewritten:

    {"ts":"2026-09-02T05:40:00+08:00","book":"DHH501","version":"3c923a8",
     "kind":"good","target":"harness-not-model","text":"..."}

`version` is the short SHA of the book repo commit the feedback refers to,
captured at write time -- so feedback is always pinned to the revision that
inspired it. `ts` is ISO 8601 with the local offset.

Usage:

    feedback.py log <book.json> --kind good|bad|learn|fix [--target ID] -m "text"
    feedback.py list <book.json>            # newest first, with git markers
    feedback.py --self-check

`book.json` resolves the book code and the repo root (its parent dir). Kinds:

    good -- I like this; keep it / keep refining in this direction
    bad  -- I dislike this; rethink or cut
    learn-- this is what I'm moving toward / what I take away
    fix  -- a specific change requested; usually names an id or part

`--target` is optional: a page `<id>`, a part title, or "whole".
Use `-m` once; newlines are folded to spaces so the file stays one-object-per-line.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def git_root(repo: Path) -> Path:
    """The .git root of the source dir, discovered upward. Fails loudly if none."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise SystemExit(
            f"not a git repo (or git unavailable) at {repo}: {e.stderr.strip()}")
    return Path(out.stdout.strip())


def short_sha(repo: Path) -> str:
    """The repo's current HEAD short SHA. Missing/unborn HEAD => '@no-commit'."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except subprocess.CalledProcessError:
        return "@no-commit"


def resolve(book_path: Path) -> tuple[Path, str]:
    """Return (repo_root, book_code). Exits with a message on bad input."""
    book = json.loads(book_path.read_text())
    root = git_root(book_path.parent)
    code = (book.get("meta") or {}).get("code") or root.name
    return root, code


def record_path(root: Path) -> Path:
    return root / "feedback.jsonl"


def append(root: Path, code: str, kind: str, target: str, text: str) -> Path:
    line = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "book": code,
        "version": short_sha(root),
        "kind": kind,
        "target": target,
        "text": text,
    }
    p = record_path(root)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return p


def list_feedback(root: Path) -> list[dict]:
    p = record_path(root)
    if not p.exists():
        return []
    rows = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            # Never let one corrupt line hide the rest of the history.
            rows.append({"ts": "?", "kind": "?",
                         "target": "?", "text": f"(unparseable line: {ln[:80]}…)"})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    lp = sub.add_parser("log", help="append one feedback record")
    lp.add_argument("book", help="path to book.json")
    lp.add_argument("--kind", required=True,
                    choices=["good", "bad", "learn", "fix"],
                    help="what kind of feedback this is")
    lp.add_argument("--target", default="whole",
                    help="page <id>, part title, or 'whole' (default)")
    lp.add_argument("-m", "--message", required=True, dest="text",
                    help="the feedback, one line; newlines folded to spaces")

    sp = sub.add_parser("list", help="print the feedback timeline, newest first")
    sp.add_argument("book", help="path to book.json")

    ap.add_argument("--self-check", action="store_true",
                    help="run the assertions and exit")
    args = ap.parse_args()

    if args.self_check:
        return self_check()

    if not args.cmd:
        ap.print_help()
        return 2

    book_path = Path(args.book).expanduser().resolve()
    root, _code = resolve(book_path)

    if args.cmd == "log":
        text = " ".join(args.text.split())  # fold newlines/spaces -> single line
        p = append(root, _code, args.kind, args.target, text)
        print(f"recorded -> {p}")
        print(f"  book    {_code}  version {short_sha(root)}  kind {args.kind}  target {args.target}")
        print(f"  text    {text}")
    else:
        rows = list_feedback(root)
        if not rows:
            print("no feedback recorded yet for this book")
            return 0
        print(f"{len(rows)} record(s), newest first:\n")
        for r in reversed(rows):
            print(f"  [{r.get('ts')}]  v{r.get('version')}  {r.get('kind')}  -> {r.get('target')}")
            print(f"      {r.get('text')}")
    return 0


def self_check() -> int:
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "book"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        # unborn HEAD exercises the @no-commit path
        assert short_sha(root) == "@no-commit"
        p = append(root, "TEST99", "good", "whole", "first  good impression")
        assert p.name == "feedback.jsonl"
        append(root, "TEST99", "bad", "s-harness", "stale claim, revise")
        rows = list_feedback(root)
        assert len(rows) == 2
        assert rows[0]["kind"] == "good" and rows[0]["version"] == "@no-commit"
        assert rows[1]["target"] == "s-harness"
        # a corrupt line must not hide later rows
        with p.open("a") as f:
            f.write("not json at all\n")
        append(root, "TEST99", "learn", "whole", "still works after junk")
        rows = list_feedback(root)
        assert len(rows) == 4
        assert rows[2]["kind"] == "?"
        assert rows[3]["text"] == "still works after junk"
    print("self-check ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
