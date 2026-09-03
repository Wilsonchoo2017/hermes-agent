"""Checks for render_book's escaping and page assembly. No typst, no network.

Escaping is the part that silently breaks a build: an unescaped `#` or `@` in
a quotation is a Typst syntax error, and the failure surfaces as a compile
error in generated source rather than anywhere near the note that caused it.

Run: python3 test_render_book.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import io, json, tempfile
from pathlib import Path

import render_book
from render_book import (esc, rich, _is_quote, render_part, part_maps, render_page, render_front,
                         validate_ids, render_index, score_section,
                         rank_sections, part_size_warnings, _range_minutes,
                         section_areas, area_density, dead_signals, density_for)

BOOK = {
    "meta": {"title": "T", "subtitle": "S", "source_show": "Show",
             "duration": "1:00:00", "published": "2026-01-01",
             "captured": "2026-01-02", "note": "n"},
    "sections": [{
        "title": "Sec", "hook": "hook", "ranges": ["0:00–1:00"],
        "pages": [
            {"kind": "atomic", "id": "a1", "title": "A", "claim": "c",
             "cite": "0:01–0:02", "body": ["one"]},
            {"kind": "reprise", "id": "r1", "title": "R",
             "slug": "20-ai/20.05-x", "body": ["two"]},
            {"kind": "synthesis", "id": "s1", "title": "S",
             "parents": ["a1", "r1"], "stance": "agree", "body": ["three"]},
        ],
    }],
    "sources": [{"title": "Src", "url": "https://x", "note": "n"}],
}


def _render(book, part_no=1):
    locals_, home = part_maps(book)
    return render_part(book, part_no, locals_[part_no - 1], home)


def test_book_dir_is_platform_channel_code():
    book = {"meta": {"code": "DHH501", "channel": "Lex Fridman",
                     "source_url": "https://www.youtube.com/watch?v=NYFGCESmikA"}}
    assert str(render_book.book_dir(book)) == "youtube/lex-fridman/DHH501"


def test_book_dir_accepts_the_short_youtube_host():
    book = {"meta": {"code": "F", "channel": "3Blue1Brown",
                     "source_url": "https://youtu.be/spUNpyF58BY"}}
    assert str(render_book.book_dir(book)) == "youtube/3blue1brown/F"


def test_channel_slug_overrides_the_captured_channel_name():
    book = {"meta": {"code": "DHH501", "channel": "Lex Fridman", "channel_slug": "lex",
                     "source_url": "https://youtu.be/x"}}
    assert str(render_book.book_dir(book)) == "youtube/lex/DHH501"


def test_book_dir_buckets_an_ad_hoc_video_with_no_channel():
    book = {"meta": {"code": "TALK1", "source_url": "https://youtu.be/x"}}
    assert str(render_book.book_dir(book)) == "youtube/misc/TALK1"


def test_book_dir_buckets_an_unknown_host_rather_than_inventing_a_folder():
    book = {"meta": {"code": "TALK1", "channel": "Acquired", "source_url": "https://vimeo.com/12345"}}
    assert str(render_book.book_dir(book)) == "other/acquired/TALK1"


def test_book_dir_keeps_code_case_but_lowercases_the_channel():
    # DHH501 is printed on every page as-is; folders read better lowercased.
    book = {"meta": {"code": "DHH501", "channel": "Lex Fridman", "source_url": "https://youtu.be/x"}}
    parts = render_book.book_dir(book).parts
    assert parts[1] == "lex-fridman" and parts[2] == "DHH501"


def test_book_dir_falls_back_to_the_title_when_there_is_no_code():
    book = {"meta": {"title": "The Harness Was the Discontinuity", "channel": "Lex Fridman",
                     "source_url": "https://youtu.be/x"}}
    assert str(render_book.book_dir(book)) == "youtube/lex-fridman/The-Harness-Was-the-Discontinuity"


def test_book_dir_survives_a_book_with_no_meta_at_all():
    assert str(render_book.book_dir({})) == "other/misc/book"


def test_book_dir_never_escapes_the_output_root():
    # Both segments are hand-authored; neither may add a level or traverse up.
    book = {"meta": {"code": "../../etc", "channel": "a/b/../c", "source_url": "https://youtu.be/x"}}
    assert str(render_book.book_dir(book)) == "youtube/a-b-c/etc"


def _book_with_two_kinds():
    def part(title, ident):
        return {"title": title, "hook": "h", "ranges": ["0:00:00-0:10:00"], "pages": [
            {"kind": "atomic", "id": f"{ident}-a", "title": "T", "claim": "C", "body": ["b"]},
            {"kind": "synthesis", "id": f"{ident}-s", "title": "T", "claim": "C",
             "body": ["b"], "parents": [f"{ident}-a"], "stance": "agree"}]}
    return {"meta": {"title": "B", "code": "X", "source_url": "https://youtu.be/x"},
            "sections": [part("One", "p1"), part("Two", "p2")]}


def test_every_kind_has_a_mark_defined_in_the_preamble():
    # A kind with no mark would silently render an empty box.
    for kind, symbol in render_book.KIND_MARK.items():
        assert kind in render_book.KIND_LABEL
        assert f"#let {symbol} =" in render_book.PREAMBLE or \
               f"#let {symbol}(" in render_book.PREAMBLE, symbol


def test_kicker_carries_the_mark_for_its_kind():
    out = render_book.kicker_inline("SYNTHESIS", "mark-synthesis")
    assert "mark-synthesis" in out and "SYNTHESIS" in out


def test_kicker_without_a_mark_is_just_the_label():
    out = render_book.kicker_inline("THE PARTS")
    assert "box[#" not in out and "THE PARTS" in out


def test_legend_lists_only_the_kinds_this_part_uses():
    section = {"pages": [{"kind": "atomic"}, {"kind": "atomic"}, {"kind": "synthesis"}]}
    out = render_book.legend(section)
    assert "mark-source" in out and "mark-synthesis" in out
    assert "mark-yours" not in out and "mark-notes" not in out


def test_legend_is_empty_when_a_part_is_all_one_kind():
    # Nothing to tell apart, so the key is noise.
    assert render_book.legend({"pages": [{"kind": "atomic"}, {"kind": "atomic"}]}) == ""


def test_legend_treats_a_page_with_no_kind_as_atomic():
    section = {"pages": [{}, {"kind": "yours"}]}
    out = render_book.legend(section)
    assert "mark-source" in out and "mark-yours" in out


def test_legend_appears_on_every_part_title_page():
    book = _book_with_two_kinds()
    for part_no in (1, 2):
        out = _render(book, part_no)
        assert "mark-source" in out, f"part {part_no} lost its legend"


def test_sowhat_is_labelled_so_it_does_not_read_as_a_summary():
    page = {"kind": "atomic", "id": "a", "title": "T", "claim": "C",
            "body": ["b"], "so_what": "Therefore X."}
    out = render_book.render_page(page, {"a": "<pg-a>"})
    assert "#sowhat[" in out and "SO WHAT" in render_book.PREAMBLE


def test_a_page_without_so_what_renders_no_block():
    # so_what is optional: most pages are stronger ending on their evidence.
    page = {"kind": "atomic", "id": "a", "title": "T", "claim": "C", "body": ["b"]}
    assert "#sowhat[" not in render_book.render_page(page, {"a": "<pg-a>"})


def test_esc_escapes_every_typst_special():
    for ch in "#$*_`<>@\\[]":
        assert esc(f"a{ch}b") == f"a\\{ch}b", ch


def test_esc_leaves_ordinary_prose_alone():
    text = "He said it was 100% fine, and it was."
    assert esc(text) == text


def test_rich_keeps_bold_and_escapes_around_it():
    out = rich("plain **bold #hash** tail@x")
    assert "*bold \\#hash*" in out
    assert "tail\\@x" in out
    assert "**" not in out


def test_rich_escapes_a_bare_asterisk_that_is_not_bold():
    # A lone `*` in prose would open emphasis and swallow the rest of the page.
    assert rich("2 * 3 = 6") == "2 \\* 3 = 6"


def test_is_quote_detects_a_fully_quoted_paragraph():
    long_quote = '"' + "word " * 30 + 'end."'
    assert _is_quote(long_quote)


def test_is_quote_ignores_prose_that_merely_contains_a_quote():
    assert not _is_quote('He said "hello" and left, which was the whole point of it all.')
    assert not _is_quote('"short"')


def test_render_page_emits_a_pagebreak_so_one_note_is_one_page():
    index = {"a1": "<pg-a1>"}
    out = render_page(BOOK["sections"][0]["pages"][0], index)
    assert out.startswith("#pagebreak")
    assert "<pg-a1>" in out


def test_synthesis_page_resolves_parents_to_page_numbers():
    index = {"a1": "<pg-a1>", "r1": "<pg-r1>", "s1": "<pg-s1>"}
    out = render_page(BOOK["sections"][0]["pages"][2], index)
    # The whole point of the labels: "see p.4", not "see the note about X".
    assert "counter(page).at(<pg-a1>)" in out
    assert "counter(page).at(<pg-r1>)" in out
    assert "These agree" in out


def test_render_produces_a_label_for_every_page():
    out = _render(BOOK)
    for page_id in ("a1", "r1", "s1"):
        assert f"<pg-{page_id}>" in out, page_id


def test_part_one_carries_the_map_hooks_and_back_matter():
    out = _render(BOOK)
    assert "The parts" in out     # the map of the whole, part 1 only
    assert "hook" in out          # the browse surface
    assert "Sources" in out
    assert "https://x" in out


def test_render_survives_specials_in_every_user_supplied_field():
    import copy
    book = copy.deepcopy(BOOK)
    book["meta"]["title"] = "C# @ 100% [draft]"
    book["sections"][0]["pages"][0]["body"] = ["a #b @c *d* _e_ `f` <g> [h]"]
    out = _render(book)
    assert "C\\# \\@ 100% \\[draft\\]" in out


def test_so_what_renders_last_and_in_its_own_block():
    index = {"a1": "<pg-a1>"}
    page = dict(BOOK["sections"][0]["pages"][0], so_what="what it means")
    out = render_page(page, index)
    assert "#sowhat[what it means]" in out
    # Last thing before the page's own end-stamp -- so_what must sit after the
    # body, in the stress position, not floating up among the evidence.
    body_at = out.index("one")
    assert out.index("#sowhat[") > body_at
    assert out.index("#sowhat[") > out.index('edge: "start"')
    assert out.index("#sowhat[") < out.index('edge: "end"')


def test_page_without_so_what_still_renders():
    out = render_page(BOOK["sections"][0]["pages"][0], {"a1": "<pg-a1>"})
    assert "#sowhat" not in out


def test_recall_renders_above_the_body_with_its_slug():
    page = {"kind": "synthesis", "id": "s", "title": "T", "parents": [],
            "recall": {"slug": "20-ai/20.05-x", "text": "recap text"},
            "body": ["the argument"]}
    out = render_page(page, {"s": "<pg-s>"})
    assert "#recall[" in out
    assert "20-ai/20.05-x" in out
    assert out.index("#recall[") < out.index("the argument")


def test_page_without_recall_is_unchanged():
    out = render_page(BOOK["sections"][0]["pages"][2], {"a1": "<a>", "r1": "<r>", "s1": "<s>"})
    assert "#recall[" not in out


def test_every_page_prints_its_reference():
    # The ref is the whole point: an idea you cannot name is one you cannot
    # ask about.
    out = render_page(BOOK["sections"][0]["pages"][0], {"a1": "<pg-a1>"}, code="DHH501")
    assert "DHH501/a1" in out


def test_reference_falls_back_to_bare_id_without_a_book_code():
    out = render_page(BOOK["sections"][0]["pages"][0], {"a1": "<pg-a1>"})
    assert "idref[a1]" in out


def test_validate_ids_rejects_duplicates():
    import copy
    book = copy.deepcopy(BOOK)
    book["sections"][0]["pages"][1]["id"] = "a1"
    try:
        validate_ids(book)
    except SystemExit as e:
        assert "duplicate id: a1" in str(e)
    else:
        raise AssertionError("duplicate id was accepted")


def test_validate_ids_rejects_ids_that_cannot_be_typed():
    import copy
    for bad in ("Not Kebab", "has_underscore", "trailing-", "UPPER"):
        book = copy.deepcopy(BOOK)
        book["sections"][0]["pages"][0]["id"] = bad
        try:
            validate_ids(book)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"accepted unusable id: {bad}")


def test_validate_ids_accepts_the_real_book_shape():
    validate_ids(BOOK)


def test_index_lists_every_reference():
    index = {"a1": "<pg-a1>", "r1": "<pg-r1>", "s1": "<pg-s1>"}
    out = render_index(BOOK, index, "DHH501")
    for page_id in ("a1", "r1", "s1"):
        assert f"DHH501/{page_id}" in out


def test_index_truncates_a_long_claim():
    import copy
    book = copy.deepcopy(BOOK)
    book["sections"][0]["pages"][0]["claim"] = " ".join(["word"] * 60)
    out = render_index(book, {"a1": "<a>", "r1": "<r>", "s1": "<s>"}, "C")
    assert "…" in out


def _sec(title, **kw):
    base = {"title": title, "hook": "h", "ranges": [], "pages": []}
    base.update(kw)
    return base


def test_range_minutes_spans_en_dashed_timestamps():
    assert _range_minutes(["0:02:56–0:11:29"]) == pytest_approx(8.55)
    assert _range_minutes([]) == 0.0
    assert _range_minutes(["garbage"]) == 0.0


def pytest_approx(value, tol=0.01):
    class _A:
        def __eq__(self, other): return abs(other - value) < tol
    return _A()


def test_friction_with_your_notes_outranks_source_length():
    # A short part that collides with a note you hold should beat a long part
    # that touches nothing -- the whole point of the ordering.
    friction = _sec("short but sharp", ranges=["0:00:00–0:02:00"],
                    pages=[{"stance": "collide", "recall": {"slug": "x", "text": "t"}}])
    lengthy = _sec("long but inert", ranges=["0:00:00–1:00:00"], pages=[{}])
    ranked = rank_sections({"sections": [lengthy, friction]})
    assert ranked[0]["title"] == "short but sharp"


def test_your_own_thinking_lifts_a_part():
    plain = _sec("plain", pages=[{}])
    yours = _sec("yours", pages=[{"kind": "yours"}])
    ranked = rank_sections({"sections": [plain, yours]})
    assert ranked[0]["title"] == "yours"


def test_pin_beats_the_heuristic():
    strong = _sec("strong", pages=[{"stance": "collide"}, {"kind": "yours"}])
    pinned = _sec("pinned", pin=1, pages=[{}])
    ranked = rank_sections({"sections": [strong, pinned]})
    assert ranked[0]["title"] == "pinned"


def test_ranking_is_stable_for_equal_scores():
    a, b_ = _sec("a", pages=[{}]), _sec("b", pages=[{}])
    ranked = rank_sections({"sections": [a, b_]})
    assert [s["title"] for s in ranked] == ["a", "b"]


def test_connecting_to_nothing_scores_zero_not_negative():
    # Novelty is deliberately unusable as a signal; it must not be inferred.
    score, signals = score_section(_sec("orphan", pages=[{}, {}]))
    assert score == 0
    assert signals["collide"] == 0 and signals["recall"] == 0


def test_a_collision_outranks_the_same_part_agreeing():
    # The collide weight is the heaviest one; nothing in the corpus currently
    # sets stance: collide, so this is the only place the path is exercised.
    agree = _sec("agree", pages=[{"kind": "synthesis", "stance": "agree"}])
    collide = _sec("collide", pages=[{"kind": "synthesis", "stance": "collide"}])
    ranked = rank_sections({"sections": [agree, collide]})
    assert ranked[0]["title"] == "collide"
    assert ranked[0]["_score"] > ranked[1]["_score"]


def test_section_areas_are_read_off_the_notes_a_part_recalls():
    section = _sec("s", pages=[
        {"recall": {"slug": "20.07 — Prompt-engineering principles"}},
        {"kind": "reprise", "slug": "31-work/31.09-why-so-long"},
        {"kind": "atomic"},
        {"recall": {"slug": "20.11 — capped by measurability"}},   # 20 not repeated
    ])
    assert section_areas(section) == ["20", "31"]


def test_explicit_areas_win_and_a_part_touching_nothing_has_none():
    assert section_areas(_sec("s", areas=["64"], pages=[
        {"recall": {"slug": "20.07 — x"}}])) == ["64"]
    # The real book writes areas as folder names; the density map is keyed by
    # JD number, so `20-ai` has to find the notes filed under 20.
    assert section_areas(_sec("s", areas=["20-ai", "misc"])) == ["20", "misc"]
    assert section_areas(_sec("s", pages=[{"kind": "atomic"}])) == []


def test_density_lifts_a_part_in_an_area_you_have_written_in():
    thin = _sec("thin", pages=[{"recall": {"slug": "20.01 — a"}}])
    thick = _sec("thick", pages=[{"recall": {"slug": "12.01 — b"}}])
    ranked = rank_sections({"sections": [thin, thick]}, {"12": 1.0, "20": 0.03})
    assert ranked[0]["title"] == "thick"
    assert ranked[0]["_signals"]["density"] == 1.0


def test_density_is_zero_when_the_corpus_is_unavailable():
    # A PKM server being down must cost the ranking a signal, never a render.
    score, signals = score_section(_sec("s", pages=[{"recall": {"slug": "12.01 — b"}}]), None)
    assert signals["density"] == 0
    assert score == 2.0     # the recall signal, unchanged


def _fake_corpus(monkey_notes):
    def urlopen(request, timeout=None, context=None):
        assert request.headers.get("X-arcana-agent") == "mcp"
        return io.BytesIO(json.dumps({"notes": monkey_notes}).encode())
    return urlopen


def _with_fake_urlopen(notes, fn):
    original = render_book.urllib.request.urlopen
    render_book.urllib.request.urlopen = _fake_corpus(notes)
    try:
        return fn()
    finally:
        render_book.urllib.request.urlopen = original


NOTES = ([{"jd": "12.%d" % i} for i in range(10)]
         + [{"jd": "64.%d" % i} for i in range(5)]
         + [{"jd": None}, {"slug": "no-jd"}])


def test_area_density_normalises_against_the_densest_area():
    # Raw counts would swamp every other signal; 0..1 keeps density comparable
    # with collide, recall and yours.
    with tempfile.TemporaryDirectory() as tmp:
        got = _with_fake_urlopen(NOTES, lambda: area_density(Path(tmp) / "d.json"))
    assert got == {"12": 1.0, "64": 0.5}


def test_area_density_caches_to_disk_and_reads_it_back():
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "d.json"
        _with_fake_urlopen(NOTES, lambda: area_density(cache))
        assert json.loads(cache.read_text()) == {"12": 1.0, "64": 0.5}
        # No fake installed: a second call must not touch the network at all.
        assert area_density(cache) == {"12": 1.0, "64": 0.5}


def test_area_density_refetches_over_a_corrupt_cache():
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "d.json"
        cache.write_text("{ not json")
        got = _with_fake_urlopen(NOTES, lambda: area_density(cache))
    assert got == {"12": 1.0, "64": 0.5}


def test_area_density_of_an_empty_corpus_is_empty_not_a_zero_divide():
    with tempfile.TemporaryDirectory() as tmp:
        got = _with_fake_urlopen([], lambda: area_density(Path(tmp) / "d.json"))
    assert got == {}


def test_an_unreachable_corpus_costs_a_signal_not_the_render():
    def boom(*a, **kw):
        raise OSError("connection refused")
    original = render_book.urllib.request.urlopen
    render_book.urllib.request.urlopen = boom
    try:
        with tempfile.TemporaryDirectory() as tmp:
            assert density_for({}, Path(tmp) / "book.json", fetch=True) is None
    finally:
        render_book.urllib.request.urlopen = original


def test_density_is_not_fetched_without_the_flag_or_when_the_book_carries_it():
    def boom(*a, **kw):
        raise AssertionError("must not touch the network")
    original = render_book.urllib.request.urlopen
    render_book.urllib.request.urlopen = boom
    try:
        assert density_for({}, Path("/nope/book.json"), fetch=False) is None
        assert density_for({"density": {"12": 1.0}}, Path("/nope/book.json"),
                           fetch=True) == {"12": 1.0}
    finally:
        render_book.urllib.request.urlopen = original


def test_dead_signals_name_what_is_ordering_nothing():
    book = {"sections": [_sec("a", pages=[{"recall": {"slug": "20.01 — x"}}])]}
    rank_sections(book)
    dead = dead_signals(book)
    assert "collide: 0 in every part -- signal inactive" in dead
    assert "density: 0 in every part -- signal inactive" in dead
    assert not any(d.startswith("recall:") for d in dead)


def test_a_live_signal_is_not_reported_dead():
    book = {"sections": [_sec("a", pages=[{"stance": "collide"}]), _sec("b", pages=[{}])]}
    rank_sections(book)
    assert not any(d.startswith("collide:") for d in dead_signals(book))
    assert dead_signals({"sections": []}) == []


def test_part_size_warnings_flag_both_extremes():
    big = _sec("big", pages=[{}] * 20)
    small = _sec("small", pages=[{}] * 2)
    warnings = part_size_warnings({"sections": [big, small]})
    assert any("over 12" in w for w in warnings)
    assert any("under 8" in w for w in warnings)


def test_cross_part_parent_is_named_by_reference_not_page_number():
    # Typst labels do not cross documents, so a synthesis whose parent lives in
    # another part must name it rather than promise a page number that will
    # silently resolve to nothing.
    import copy
    book = copy.deepcopy(BOOK)
    book["meta"]["code"] = "DHH501"
    book["sections"] = [
        {"title": "one", "hook": "h", "ranges": [],
         "pages": [{"kind": "atomic", "id": "a1", "title": "A", "body": ["x"]}]},
        {"title": "two", "hook": "h", "ranges": [],
         "pages": [{"kind": "synthesis", "id": "s1", "title": "S",
                    "parents": ["a1"], "stance": "agree", "body": ["y"]}]},
    ]
    out = _render(book, part_no=2)
    assert "DHH501/a1 (Part 1)" in out
    assert "counter(page).at(<pg-a1>)" not in out


def test_same_part_parent_still_resolves_to_a_page_number():
    import copy
    book = copy.deepcopy(BOOK)
    book["sections"] = [{"title": "one", "hook": "h", "ranges": [], "pages": [
        {"kind": "atomic", "id": "a1", "title": "A", "body": ["x"]},
        {"kind": "synthesis", "id": "s1", "title": "S", "parents": ["a1"],
         "stance": "agree", "body": ["y"]},
    ]}]
    out = _render(book)
    assert "counter(page).at(<pg-a1>)" in out


def test_front_matter_and_part_map_appear_only_in_part_one():
    import copy
    book = copy.deepcopy(BOOK)
    book["front"] = [{"kicker": "The guest", "title": "Who is X", "body": ["bio"]}]
    book["sections"] = [
        {"title": "one", "hook": "h", "ranges": [], "pages": [
            {"kind": "atomic", "id": "a1", "title": "A", "body": ["x"]}]},
        {"title": "two", "hook": "h", "ranges": [], "pages": [
            {"kind": "atomic", "id": "b1", "title": "B", "body": ["y"]}]},
    ]
    first, second = _render(book, 1), _render(book, 2)
    assert "Who is X" in first and "The parts" in first
    assert "Who is X" not in second and "The parts" not in second


def test_each_part_is_labelled_with_its_position():
    import copy
    book = copy.deepcopy(BOOK)
    book["sections"] = [
        {"title": "one", "hook": "h", "ranges": [], "pages": [
            {"kind": "atomic", "id": "a1", "title": "A", "body": ["x"]}]},
        {"title": "two", "hook": "h", "ranges": [], "pages": [
            {"kind": "atomic", "id": "b1", "title": "B", "body": ["y"]}]},
    ]
    assert "PART 2 OF 2" in _render(book, 2)


def test_claim_renders_for_every_page_kind_that_has_one():
    # The governing thought belongs to the page, not to one kind. Gating it on
    # `atomic` dropped it from synthesis and reader pages without any error.
    for kind in ("atomic", "reprise", "synthesis", "yours"):
        page = {"kind": kind, "id": "x", "title": "T", "claim": "the answer",
                "body": ["b"], "parents": []}
        out = render_page(page, {"x": "<pg-x>"})
        assert "#claim[the answer]" in out, kind


def test_front_matter_renders_before_the_contents():
    # Front matter exists to brief a reader who knows nothing about the guest,
    # so it has to come before the browse surface, not after it.
    import copy
    book = copy.deepcopy(BOOK)
    book["front"] = [{"kicker": "The guest", "title": "Who is X", "body": ["bio"]}]
    out = _render(book)
    assert out.index("Who is X") < out.index("The parts")
    assert "The guest" in out


def test_render_front_escapes_and_defaults_its_kicker():
    out = render_front({"title": "Who is C#", "body": ["ran 100% @ speed"]})
    assert "Before you start" in out
    assert "C\\#" in out and "\\@" in out


def test_a_book_without_front_matter_still_renders():
    out = _render(BOOK)
    assert "The parts" in out


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
