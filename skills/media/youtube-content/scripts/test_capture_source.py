"""Checks for capture_source's only non-trivial logic: stitching segments into
chapters. No network -- everything here is a pure function over fixtures.

Run: python3 test_capture_source.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capture_source import stitch, hms, trim_meta

CHAPTERS = [
    {"start_time": 0.0, "title": "Introduction"},
    {"start_time": 60.0, "title": "Main topic"},
    {"start_time": 120.0, "title": "Wrap up"},
]
SEGMENTS = [
    {"start": 0.0, "duration": 3.0, "text": "hello there"},
    {"start": 30.0, "duration": 3.0, "text": "still intro"},
    {"start": 60.0, "duration": 3.0, "text": "boundary segment"},
    {"start": 90.0, "duration": 3.0, "text": "middle of main"},
    {"start": 121.0, "duration": 3.0, "text": "the ending"},
]


def _section(md, title):
    """Body text under `## <ts> - <title>`, up to the next `## `."""
    start = md.index(f"— {title}")
    rest = md[start:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_every_chapter_gets_a_heading():
    md = stitch(SEGMENTS, CHAPTERS)
    for c in CHAPTERS:
        assert f"— {c['title']}" in md, c["title"]
    assert md.count("\n## ") == len(CHAPTERS)


def test_segment_lands_in_its_own_chapter():
    md = stitch(SEGMENTS, CHAPTERS)
    assert "still intro" in _section(md, "Introduction")
    assert "middle of main" in _section(md, "Main topic")
    assert "the ending" in _section(md, "Wrap up")


def test_segment_exactly_on_a_boundary_goes_to_the_later_chapter():
    # A chapter starting at 60.0 owns the segment starting at 60.0. Getting this
    # backwards silently misattributes one segment per chapter boundary, which
    # is exactly the citation a downstream note would get wrong.
    md = stitch(SEGMENTS, CHAPTERS)
    assert "boundary segment" in _section(md, "Main topic")
    assert "boundary segment" not in _section(md, "Introduction")


def test_no_chapters_falls_back_to_one_section():
    # Same file shape as the chaptered case, so consumers never branch.
    md = stitch(SEGMENTS, [])
    assert md.count("\n## ") == 1
    assert "Full transcript" in md
    for seg in SEGMENTS:
        assert seg["text"] in md


def test_no_text_is_dropped():
    md = stitch(SEGMENTS, CHAPTERS)
    for seg in SEGMENTS:
        assert seg["text"] in md, seg["text"]


def test_anchors_are_monotonic_and_well_formed():
    import re
    md = stitch(SEGMENTS, CHAPTERS)
    stamps = re.findall(r"\[(\d+:\d{2}:\d{2})\]", md)
    assert stamps, "no timestamp anchors emitted"
    assert stamps == sorted(stamps), stamps


def test_chapters_before_the_first_segment_are_still_emitted():
    # A chapter with no transcript under it (silence, music) must not vanish --
    # a missing heading would shift every downstream chapter reference by one.
    md = stitch([{"start": 200.0, "duration": 2.0, "text": "late"}], CHAPTERS)
    assert md.count("\n## ") == len(CHAPTERS)
    assert "late" in _section(md, "Wrap up")


def test_hms_formats_hours_minutes_seconds():
    assert hms(0) == "0:00:00"
    assert hms(59.9) == "0:00:59"
    assert hms(3661) == "1:01:01"
    assert hms(18954) == "5:15:54"


def test_trim_meta_keeps_the_fields_we_need_and_drops_the_rest():
    raw = {
        "id": "abc", "title": "T", "channel": "C", "upload_date": "20260826",
        "duration": 120, "webpage_url": "https://y/abc",
        "chapters": [{"start_time": 0.0, "end_time": 60.0, "title": "One", "extra": "x"}],
        "formats": [{"junk": True}] * 100, "automatic_captions": {"en": []},
    }
    meta = trim_meta(raw)
    assert meta["video_id"] == "abc"
    assert meta["upload_date"] == "2026-08-26"
    assert meta["duration_seconds"] == 120
    assert meta["url"] == "https://y/abc"
    assert meta["chapters"] == [{"start": 0.0, "end": 60.0, "title": "One"}]
    assert "formats" not in meta and "automatic_captions" not in meta


def test_trim_meta_survives_a_video_with_no_chapters():
    meta = trim_meta({"id": "abc", "title": "T", "duration": 5})
    assert meta["chapters"] == []
    assert meta["channel"] is None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
    print("FAILED" if failed else "all passed")
    sys.exit(1 if failed else 0)
