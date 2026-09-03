#!/usr/bin/env python3
"""Capture a YouTube video as a durable, chapter-segmented source artifact.

The point is to fetch once and process many times. A five-hour podcast is slow
to fetch and its transcript is the same every time, so the transcript is pulled
into a directory on disk and everything downstream -- atomic notes, summaries,
search, whatever comes later -- reads that directory instead of YouTube.

    <store>/youtube/<video_id>/
        meta.json        trimmed metadata + chapter list
        transcript.json  lossless segment array, exactly as YouTube served it
        source.md        transcript stitched under one `##` per chapter

`source.md` is the processing surface: chapter-segmented with a timestamp
anchor every ~30s, so any claim drawn from it can cite a real position in the
video. `transcript.json` is kept so the source can be re-segmented some other
way later (by speaker turn, by fixed window) without going back to YouTube.

Usage:
    capture_source.py "https://www.youtube.com/watch?v=NYFGCESmikA"
    capture_source.py NYFGCESmikA --lang en --force
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_transcript import extract_video_id, fetch_transcript

DEFAULT_STORE = Path.home() / "ownCloud" / "hermes" / "sources"
ANCHOR_EVERY_S = 30  # a citable timestamp roughly twice a minute
YTDLP_TIMEOUT_S = 180


def hms(seconds: float) -> str:
    """`5:15:54`. Always hour-prefixed: these are long videos and a bare M:SS
    is ambiguous once past an hour."""
    total = int(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------
def trim_meta(raw: dict) -> dict:
    """Keep the handful of yt-dlp fields worth storing.

    yt-dlp's dump is ~200 fields and several megabytes of format listings and
    caption URLs that expire. Storing it whole makes the artifact look durable
    when most of it rots within hours.
    """
    date = raw.get("upload_date") or ""
    return {
        "video_id": raw.get("id"),
        "title": raw.get("title"),
        "channel": raw.get("channel") or raw.get("uploader"),
        "upload_date": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else None,
        "duration_seconds": raw.get("duration"),
        "duration": hms(raw.get("duration") or 0),
        "url": raw.get("webpage_url"),
        "chapters": [
            {"start": c.get("start_time"), "end": c.get("end_time"), "title": c.get("title")}
            for c in (raw.get("chapters") or [])
        ],
    }


def fetch_cover(raw: dict, target: Path) -> str | None:
    """Save the largest still thumbnail as the book's cover.

    Skipped silently on failure -- a missing cover is a cosmetic loss, and it
    must never cost a transcript that took minutes to fetch. webp is ignored
    because Typst cannot place it.
    """
    stills = [t for t in (raw.get("thumbnails") or [])
              if t.get("url", "").endswith((".jpg", ".jpeg"))]
    if not stills:
        return None
    best = max(stills, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    try:
        with urllib.request.urlopen(best["url"], timeout=30) as response:
            (target / "cover.jpg").write_bytes(response.read())
    except Exception:
        return None
    return "cover.jpg"


def probe(url_or_id: str) -> dict:
    """yt-dlp metadata for a video. Chapters come from here; the transcript API
    does not expose them."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--skip-download", "--dump-json", url_or_id],
            capture_output=True, text=True, timeout=YTDLP_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise SystemExit("yt-dlp not found on PATH. Install it: brew install yt-dlp")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"yt-dlp timed out after {YTDLP_TIMEOUT_S}s")
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise SystemExit("yt-dlp failed:\n  " + "\n  ".join(tail))
    return json.loads(result.stdout)


# --------------------------------------------------------------------------
# Stitching
# --------------------------------------------------------------------------
def stitch(segments: list[dict], chapters: list[dict]) -> str:
    """Render segments as markdown, one `##` section per chapter.

    A segment belongs to the last chapter that started at or before it, so a
    segment landing exactly on a boundary belongs to the chapter it opens.
    Chapters with no segments still get a heading -- dropping an empty one
    would shift every later chapter reference by one.
    """
    bounds = [(c.get("start_time") or 0.0, c.get("title") or "Untitled")
              for c in chapters] or [(0.0, "Full transcript")]
    bounds.sort(key=lambda b: b[0])

    buckets: list[list[dict]] = [[] for _ in bounds]
    starts = [b[0] for b in bounds]
    for seg in segments:
        index = 0
        for i, start in enumerate(starts):
            if seg["start"] >= start:
                index = i
            else:
                break
        buckets[index].append(seg)

    out = []
    for (start, title), held in zip(bounds, buckets):
        out.append(f"\n## {hms(start)} — {title}\n")
        out.append(_paragraphs(held) if held else "_(no transcript in this range)_\n")
    return "".join(out)


def _paragraphs(segments: list[dict]) -> str:
    """Segment text joined into readable paragraphs, each opening with a
    timestamp anchor. YouTube's segments are ~3s fragments; a wall of them is
    unreadable and gives a citation granularity nobody wants."""
    out, buffer, anchor = [], [], None
    for seg in segments:
        if anchor is None:
            anchor = seg["start"]
        buffer.append(seg["text"].strip())
        if seg["start"] - anchor >= ANCHOR_EVERY_S:
            out.append(f"[{hms(anchor)}] " + " ".join(buffer))
            buffer, anchor = [], None
    if buffer:
        out.append(f"[{hms(anchor)}] " + " ".join(buffer))
    return "\n\n".join(out) + "\n"


def render_source(meta: dict, segments: list[dict], chapters: list[dict]) -> str:
    head = (
        "---\n"
        f"title: {json.dumps(meta.get('title') or '')}\n"
        f"channel: {json.dumps(meta.get('channel') or '')}\n"
        f"url: {meta.get('url')}\n"
        f"video_id: {meta.get('video_id')}\n"
        f"published: {meta.get('upload_date')}\n"
        f"duration: {meta.get('duration')}\n"
        f"chapters: {len(chapters)}\n"
        f"segments: {len(segments)}\n"
        f"captured: {datetime.now(timezone.utc).date().isoformat()}\n"
        "---\n"
    )
    return head + stitch(segments, chapters)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def capture(url_or_id: str, store: Path, languages: list[str] | None = None,
            force: bool = False) -> Path:
    video_id = extract_video_id(url_or_id)
    target = store / "youtube" / video_id
    if target.exists() and not force:
        return target

    raw = probe(url_or_id)
    meta = trim_meta(raw)
    chapters = raw.get("chapters") or []
    segments = fetch_transcript(video_id, languages)
    if not segments:
        raise SystemExit(f"No transcript segments for {video_id}; nothing written.")

    # Assemble in a temp dir and rename, so an interrupted fetch never leaves a
    # half-written artifact that a later run would happily skip as "done".
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{video_id}-"))
    try:
        (staging / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        (staging / "transcript.json").write_text(json.dumps(
            {"video_id": video_id, "segment_count": len(segments), "segments": segments},
            indent=2, ensure_ascii=False))
        (staging / "source.md").write_text(render_source(meta, segments, chapters))
        meta["cover"] = fetch_cover(raw, staging)
        (staging / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="YouTube URL or 11-character video ID")
    ap.add_argument("--lang", "-l", default=None,
                    help="Comma-separated language codes (e.g. en,tr). Default: auto")
    ap.add_argument("--store", default=str(DEFAULT_STORE),
                    help=f"Root of the source store (default: {DEFAULT_STORE})")
    ap.add_argument("--force", action="store_true",
                    help="Re-capture even if the artifact already exists")
    args = ap.parse_args()

    languages = [l.strip() for l in args.lang.split(",")] if args.lang else None
    target = capture(args.url, Path(args.store).expanduser(), languages, args.force)
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
