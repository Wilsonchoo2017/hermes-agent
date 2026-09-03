#!/usr/bin/env python3
"""youtube_to_arcana.py — run the whole scriptable YouTube→Arcana pipeline.

One command from a YouTube URL to filed Arcana notes. The youtube-content
pipeline has a hard seam: everything upstream of `book.json` is judgment
(distill → triage → match → author), everything downstream is deterministic
typography + filing. This orchestrator runs the deterministic tail and stops
cleanly at the seam.

    youtube_to_arcana.py <URL_OR_VIDEO_ID> [--dry] [--force-frames]

Steps (each idempotent, each skippable if already done):
  1. CAPTURE   capture_source.py <url>   -> $SRC/{meta,transcript,source}.md
  2. BUILD     build_book.py $SRC/book.json  -> part PDFs (needs book.json)
  3. FILE      file_book_to_arcana.py $SRC/book.json -> Arcana atomics + hub

The seam: if `$SRC/book.json` does not exist, step 2/3 are skipped and the
script prints exactly what is missing (the authored book) so a human/AI can
write it, then re-run. `--dry` previews the filing without POSTing.

DEPENDENCIES: python3 stdlib + curl + yt-dlp + ffmpeg + typst on PATH.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_STORE = Path.home() / "ownCloud" / "hermes" / "sources"


def run(script: str, args: list[str]) -> int:
    """Run a sibling script, streaming its output, return its exit code."""
    cmd = [sys.executable, str(HERE / script), *args]
    print(f"\n$ {' '.join(cmd)}", file=sys.stderr)
    r = subprocess.run(cmd)
    return r.returncode


def video_id_of(url: str) -> str:
    """Extract the 11-char id from a URL or accept a bare id."""
    url = url.strip()
    # IDs are [A-Za-z0-9_-]{11}: isalnum() would reject the many that carry
    # a hyphen or underscore, and they would then match no URL marker below.
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    # youtube.com/watch?v=ID, youtu.be/ID, shorts/ID, live/ID
    for marker in ("v=", "youtu.be/", "shorts/", "live/", "embed/"):
        if marker in url:
            return url.split(marker, 1)[1].split("&")[0].split("/")[0].split("?")[0]
    raise SystemExit(f"cannot extract video id from: {url}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="YouTube URL or 11-char video id")
    ap.add_argument("--dry", action="store_true",
                    help="preview the arcana filing without POSTing")
    ap.add_argument("--force-frames", action="store_true",
                    help="re-grab existing frames in the build step")
    ap.add_argument("--store", default=str(DEFAULT_STORE),
                    help="source store root (default ~/ownCloud/hermes/sources)")
    args = ap.parse_args()

    vid = video_id_of(args.url)
    src = Path(args.store) / "youtube" / vid
    book = src / "book.json"

    # 1. CAPTURE — idempotent, no-op if the source already exists.
    if run("capture_source.py", [args.url]) != 0:
        return 1

    # 2. BUILD — needs the authored book.json (the judgment seam).
    if not book.is_file():
        print(f"\n[seam] {book} does not exist.", file=sys.stderr)
        print("The book must be authored first (distill → triage → match → write", file=sys.stderr)
        print("book.json). This is the one non-scriptable step. Once it exists,", file=sys.stderr)
        print("re-run this command to build the PDFs and file the atomics.", file=sys.stderr)
        return 0

    if run("build_book.py", [str(book)] + (["--force-frames"] if args.force_frames else [])) != 0:
        return 1

    # 3. FILE — the atomics into Arcana (preview with --dry).
    file_args = [str(book)] + (["--dry"] if args.dry else [])
    if run("file_book_to_arcana.py", file_args) != 0:
        return 1

    print(f"\nDone. Source: {src}")
    print(f"PDFs:   {Path.home() / 'ownCloud' / 'hermes' / 'books'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
