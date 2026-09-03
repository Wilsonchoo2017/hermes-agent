#!/usr/bin/env python3
"""Pull still frames out of a YouTube video at given timestamps.

Every atomic page in a book already cites the moment it came from, so the
frame is not decoration -- it is the thing being quoted. This grabs one still
per citation.

The video is never downloaded whole. yt-dlp fetches a two-second section at
the requested moment and ffmpeg takes the first frame of it, so a still from
the fifth hour of a five-hour video costs a few seconds and ~300KB.

    fetch_frames.py NYFGCESmikA 0:07:47 1:44:11
    fetch_frames.py --book path/to/book.json          # every cited moment

Frames land in <store>/youtube/<id>/frames/<hh-mm-ss>.jpg. Requires yt-dlp and
ffmpeg on PATH.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_STORE = Path.home() / "ownCloud" / "hermes" / "sources"
FRAME_TIMEOUT_S = 180
FORMAT = "best[height<=720]/best"
SECTION_S = 2

# YouTube signs media URLs against the client that asked for them, so handing
# a bare URL to ffmpeg gets a 403 -- yt-dlp has to do the fetching. Which
# client works is unstable upstream: at the time of writing `web` offers only
# images and `tv` demands a reload, so the list is tried in order and the
# winner is reused for the rest of the run.
# Order matters: the first client that yields video wins and is reused for the
# run. YouTube signs media URLs against the requesting client, so a bare fetch
# gets 403 and yt-dlp has to do the fetching. Checked 2026-09-01: `ios` now
# demands a GVS PO token, `web` and `tv` return no usable format. `tv_embedded`
# is first because it is the only client tested that returns 720p -- the others
# cap at 360p, which is soft for a source whose diagrams carry the meaning.
CLIENTS = ("tv_embedded", "mweb", "android", "web_embedded")


def parse_timestamp(text: str) -> int:
    """`1:44:11`, `44:11` or `2651` to seconds."""
    text = text.strip()
    if ":" not in text:
        return int(float(text))
    parts = [float(p) for p in text.split(":")]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return int(seconds)


def slug_for(seconds: int) -> str:
    return f"{seconds // 3600:02d}-{seconds % 3600 // 60:02d}-{seconds % 60:02d}"


def start_of(cite: str) -> str:
    """The first timestamp in a citation like `0:07:47-0:08:23`.

    Citations use an en dash, which is not a hyphen -- splitting on the wrong
    one silently yields the whole string and a nonsense seek.
    """
    return re.split(r"[–—-]", cite.strip())[0].strip()


def _hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def grab(video_id: str, seconds: int, out_path: Path, client: str,
         scratch: Path) -> bool:
    """One frame: fetch a short section, take its first picture."""
    section = scratch / f"sect-{slug_for(seconds)}.mp4"
    for stale in scratch.glob(f"sect-{slug_for(seconds)}.*"):
        stale.unlink(missing_ok=True)

    pull = subprocess.run(
        ["yt-dlp", "--extractor-args", f"youtube:player_client={client}",
         "-f", FORMAT, "--download-sections",
         f"*{_hms(seconds)}-{_hms(seconds + SECTION_S)}",
         "-o", str(scratch / f"sect-{slug_for(seconds)}.%(ext)s"), "--quiet",
         f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, timeout=FRAME_TIMEOUT_S,
    )
    got = next(iter(scratch.glob(f"sect-{slug_for(seconds)}.*")), None)
    if pull.returncode != 0 or got is None:
        return False
    try:
        shot = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(got),
             "-frames:v", "1", "-q:v", "3", str(out_path), "-y"],
            capture_output=True, text=True, timeout=FRAME_TIMEOUT_S,
        )
    finally:
        got.unlink(missing_ok=True)
    return shot.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0


def working_client(video_id: str, seconds: int, out_path: Path,
                   scratch: Path) -> str | None:
    """First client that actually yields a frame."""
    for client in CLIENTS:
        if grab(video_id, seconds, out_path, client, scratch):
            return client
    return None


def cited_moments(book_path: Path) -> list[str]:
    book = json.loads(book_path.read_text())
    return [start_of(page["cite"])
            for section in book.get("sections", [])
            for page in section.get("pages", [])
            if page.get("cite")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_id", nargs="?", help="11-character video ID")
    ap.add_argument("timestamps", nargs="*", help="e.g. 0:07:47 1:44:11")
    ap.add_argument("--book", help="take every cited moment from this book.json")
    ap.add_argument("--store", default=str(DEFAULT_STORE))
    ap.add_argument("--force", action="store_true", help="re-grab existing frames")
    args = ap.parse_args()

    stamps = list(args.timestamps)
    video_id = args.video_id
    if args.book:
        book_path = Path(args.book).expanduser()
        stamps += cited_moments(book_path)
        video_id = video_id or json.loads(book_path.read_text())["meta"].get("video_id")
    if not video_id:
        raise SystemExit("need a video_id (or a book.json carrying meta.video_id)")
    if not stamps:
        raise SystemExit("no timestamps given")

    frames_dir = Path(args.store).expanduser() / "youtube" / video_id / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    wanted = {}
    for stamp in stamps:
        seconds = parse_timestamp(stamp)
        path = frames_dir / f"{slug_for(seconds)}.jpg"
        if args.force or not path.is_file():
            wanted[seconds] = path

    if not wanted:
        print(frames_dir)
        return 0

    scratch = frames_dir / ".scratch"
    scratch.mkdir(exist_ok=True)
    ordered = sorted(wanted.items())

    first_seconds, first_path = ordered[0]
    client = working_client(video_id, first_seconds, first_path, scratch)
    if not client:
        shutil.rmtree(scratch, ignore_errors=True)
        raise SystemExit(f"no yt-dlp player client could fetch a frame; tried {', '.join(CLIENTS)}")
    print(f"  {slug_for(first_seconds)}  {first_path.name}  (client: {client})", file=sys.stderr)

    failed = 0
    for seconds, path in ordered[1:]:
        if grab(video_id, seconds, path, client, scratch):
            print(f"  {slug_for(seconds)}  {path.name}", file=sys.stderr)
        else:
            failed += 1
            print(f"  {slug_for(seconds)}  FAILED", file=sys.stderr)
    shutil.rmtree(scratch, ignore_errors=True)
    if failed:
        print(f"warning: {failed} of {len(wanted)} frames failed", file=sys.stderr)
    print(frames_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
