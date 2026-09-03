#!/usr/bin/env python3
"""Render a finished book.json: fetch its frames, then typeset the parts.

This is the deterministic tail of the youtube-book workflow. Everything
upstream of book.json is judgment and stays in the session; from here down it
is two subprocesses in a fixed order, which is exactly the kind of thing a
human gets wrong at 1am and a script never does.

The one thing it adds beyond ordering: a spilled note fails the build.
`render_book.py` reports a note that ran past its page as a warning on stderr
and still exits 0, so a book with a truncated argument in it looks like a
clean run. A spill is a content bug -- the fix is cutting words upstream --
so it has to stop the pipeline loudly rather than scroll past.

    build_book.py <book.json> [--out DIR] [--force-frames]
    build_book.py --self-check

Requires: yt-dlp, ffmpeg, typst on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_transcript import extract_video_id

HERE = Path(__file__).resolve().parent
SPILL = re.compile(r"^warning: .*spilled past one page.*$", re.MULTILINE)


def spill_warnings(stderr: str) -> list[str]:
    """The spill lines in a render's stderr, verbatim.

    Matched on the message, not on a page id, because the other warnings the
    renderer emits (part too long, part too short) are advisory and must not
    fail the build.
    """
    return [line.strip() for line in SPILL.findall(stderr)]


def run(script: str, args: list[str]) -> str:
    """Run a sibling script, echo its stderr, return that stderr.

    stdout is left alone -- both scripts print the paths they wrote, and those
    belong to the caller.
    """
    result = subprocess.run([sys.executable, str(HERE / script), *args],
                            capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        raise SystemExit(f"{script} failed with exit code {result.returncode}")
    return result.stderr


def has_citations(book: dict) -> bool:
    return any(page.get("cite")
               for section in book.get("sections", [])
               for page in section.get("pages", []))


def video_id_of(book: dict) -> str:
    """The video the frames come from.

    `meta.video_id` is optional in practice and absent from the first book
    written, so fall back to the URL that is always there. fetch_frames.py only
    reads meta.video_id, which is why it cannot do this itself.
    """
    meta = book.get("meta", {})
    if meta.get("video_id"):
        return meta["video_id"]
    url = meta.get("source_url")
    if not url:
        raise SystemExit("book.meta needs a video_id or a source_url")
    return extract_video_id(url)


def build(book_path: Path, out: str | None, force_frames: bool) -> int:
    book = json.loads(book_path.read_text())
    if has_citations(book):
        frame_args = [video_id_of(book), "--book", str(book_path)]
        if force_frames:
            frame_args.append("--force")
        run("fetch_frames.py", frame_args)
    else:
        print("no cited moments; skipping frames", file=sys.stderr)

    render_args = [str(book_path)] + (["--out", out] if out else [])
    spills = spill_warnings(run("render_book.py", render_args))
    if spills:
        print("\nBUILD FAILED: " + f"{len(spills)} note(s) spilled past one page.",
              file=sys.stderr)
        for line in spills:
            print(f"  {line}", file=sys.stderr)
        print("Cut words on those pages (~200 plain, ~185 with a block quote, "
              "~135 with a frame) and re-run.", file=sys.stderr)
        return 1
    return 0


def self_check() -> int:
    spill = "warning: part 2: 'harness-not-model' spilled past one page\n"
    size = "warning: part 'What actually changed' has 12 pages (over 10); split it\n"
    assert spill_warnings("") == []
    assert spill_warnings(size) == []
    assert spill_warnings(spill) == [spill.strip()]
    assert len(spill_warnings(size + spill + spill)) == 2
    # A spill line buried mid-stream still counts: the renderer interleaves
    # per-frame progress and per-part warnings on the same stream.
    assert len(spill_warnings("  00-07-47  ok\n" + spill + "  00-08-23  ok\n")) == 1

    url = "https://www.youtube.com/watch?v=NYFGCESmikA"
    assert video_id_of({"meta": {"source_url": url}}) == "NYFGCESmikA"
    assert video_id_of({"meta": {"video_id": "abc", "source_url": url}}) == "abc"
    assert not has_citations({"sections": [{"pages": [{"id": "a"}]}]})
    assert has_citations({"sections": [{"pages": [{"cite": "0:01:00"}]}]})
    print("self-check ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book", nargs="?", help="path to book.json")
    ap.add_argument("--out", default=None, help="output dir for the PDFs")
    ap.add_argument("--force-frames", action="store_true", help="re-grab existing frames")
    ap.add_argument("--self-check", action="store_true", help="run the assertions and exit")
    args = ap.parse_args()

    if args.self_check:
        return self_check()
    if not args.book:
        ap.error("book.json is required (or pass --self-check)")
    return build(Path(args.book).expanduser(), args.out, args.force_frames)


if __name__ == "__main__":
    sys.exit(main())
