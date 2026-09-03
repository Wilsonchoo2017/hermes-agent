#!/usr/bin/env python3
"""Render a book.json into a PDF via Typst.

The seam this file sits on: everything upstream is judgment (what ideas are in
the source, which of your notes they touch, what to say about the collision),
everything here is typography. The renderer never decides what goes in the
book, so it can be tested against a fixture with no model in the loop, and the
thinking can be re-run without touching layout.

One page per note, always. Budget on A5 is roughly 215 words, 185 with a block
quote, and 150 with a frame -- block count costs as much as word count, since
each one adds leading. A short note leaves white space rather than sharing
a spread -- the page is the unit you read and annotate, and a stable
one-note-one-page rule is what lets a synthesis cite "see p. 12".

    render_book.py <book.json> [--out DIR] [--keep-typ] [--explain] [--density]

Requires: typst on PATH (brew install typst).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

DEFAULT_OUT = Path.home() / "ownCloud" / "hermes" / "books"
TYPST_TIMEOUT_S = 120
NOTES_URL = "https://dxp.tail83c2f.ts.net:9651/api/notes/list"
NOTES_TIMEOUT_S = 20

KIND_LABEL = {
    "atomic": "FROM THE SOURCE",
    "reprise": "FROM YOUR NOTES",
    "synthesis": "SYNTHESIS",
    # A reader's own thought, captured in the book and attributed to them.
    # Distinct from synthesis: the book wrote that, you wrote this.
    "yours": "YOUR THINKING",
}
# Which drawn mark introduces each kind. Defined in the Typst preamble.
KIND_MARK = {
    "atomic": "mark-source",
    "reprise": "mark-notes",
    "synthesis": "mark-synthesis",
    "yours": "mark-yours",
}
STANCE_LABEL = {
    "agree": "These agree",
    "collide": "These collide",
    "extend": "This extends your note",
}


# --------------------------------------------------------------------------
# Typst escaping
# --------------------------------------------------------------------------
_SPECIAL = re.compile(r"([#$*_`<>@\\\[\]])")


def esc(text: str) -> str:
    """Escape Typst markup. Everything in book.json is prose, not markup --
    an unescaped `#` or `@` in a quote is a syntax error, not emphasis."""
    return _SPECIAL.sub(r"\\\1", text)


def rich(text: str) -> str:
    """Escape prose but honour **bold**. Split first, escape the parts, then
    re-wrap -- escaping the whole string would eat the markers too."""
    out = []
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text, flags=re.S)):
        out.append(f"*{esc(part)}*" if i % 2 else esc(part))
    return "".join(out)


def _is_quote(para: str) -> bool:
    """A paragraph that is entirely a quotation gets block treatment."""
    stripped = para.strip()
    return len(stripped) > 80 and stripped.startswith('"') and stripped.rstrip(".").endswith('"')


# --------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------
PREAMBLE = r"""
#set document(title: {title}, author: "Arcana")
#set page(
  paper: "a5",
  margin: (inside: 2.0cm, outside: 1.6cm, top: 2.0cm, bottom: 1.8cm),
  numbering: "1",
  number-align: center,
)
#set text(font: ("Iowan Old Style", "Palatino", "New York"), size: 10pt, lang: "en")
#set par(justify: true, leading: 0.72em, first-line-indent: 1.2em)
#show heading: set text(weight: "semibold", hyphenate: false)
#show heading: set par(justify: false)
#set heading(numbering: none)

// A mark per page kind, so a reader can tell at a glance whether a page is
// reported, recalled, argued or their own -- the four things this book mixes
// and the distinction it would be easiest to lose.
//
// Drawn from primitives rather than set as Unicode geometric characters:
// the body font is a text face with no guarantee of covering them, and a
// missing glyph renders as a notdef box, which is worse than no mark at all.
#let _ink = rgb("#7a7a7a")
#let _disc = circle(radius: 2.0pt, fill: _ink, stroke: none)
#let _ring = circle(radius: 1.7pt, fill: none, stroke: 0.7pt + _ink)

// filled: taken from outside          hollow: already yours
#let mark-source = box(baseline: -0.5pt, _disc)
#let mark-notes = box(baseline: -0.5pt, _ring)
// two of them, because that is what a synthesis is
#let mark-synthesis = box(baseline: -0.5pt, height: 4pt, width: 8.4pt)[
  #place(dx: 0pt, dy: 0.3pt, _ring)
  #place(dx: 4.4pt, dy: 0pt, _disc)
]
// a square on its corner: yours, and deliberately not the same family
#let mark-yours = box(baseline: -0.5pt, rotate(45deg, rect(
  width: 3.4pt, height: 3.4pt, fill: _ink, stroke: none,
)))

#let kicker(body) = block(
  below: 1.1em,
  text(size: 7pt, weight: "bold", tracking: 0.14em, fill: rgb("#7a7a7a"), upper(body)),
)
#let cite-line(body) = block(
  above: 0.4em, below: 1.2em,
  text(size: 8pt, style: "italic", fill: rgb("#6a6a6a"), body),
)
#let claim(body) = block(
  width: 100%, inset: (x: 0pt, y: 0.7em), below: 1.3em,
  stroke: (top: 0.5pt + rgb("#d8d8d8"), bottom: 0.5pt + rgb("#d8d8d8")),
  text(size: 10pt, style: "italic", body),
)
// Labelled, because an unlabelled grey block under a rule at the foot of a
// page reads as a summary by convention -- and this is the opposite of a
// summary. It is the consequence: what the page changes, not what it said.
#let sowhat(body) = block(
  width: 100%, inset: (top: 0.6em), above: 0.9em,
  stroke: (top: 0.5pt + rgb("#d8d8d8")),
  // Run-in rather than on its own line: a standalone label plus its leading
  // costs about a line of vertical space, and sixteen pages sit close enough
  // to the ceiling that it pushed every one of them onto a second page.
  text(size: 9.5pt, fill: rgb("#333333"))[
    #text(size: 7pt, weight: "bold", tracking: 0.14em, fill: rgb("#9a9a9a"))[SO WHAT]#h(0.7em)#body
  ],
)
#let recall(head, body) = block(
  width: 100%, fill: rgb("#f4f3f1"), inset: 0.75em, radius: 2pt, below: 1.0em,
  [
    #text(size: 7pt, weight: "bold", tracking: 0.12em, fill: rgb("#8a8a8a"))[#upper(head)]
    #v(0.45em)
    #text(size: 9pt, fill: rgb("#3a3a3a"))[#body]
  ],
)
#let idref(body) = text(
  size: 7pt, font: ("Menlo", "DejaVu Sans Mono"),
  fill: rgb("#a8a8a8"), body,
)
#let pullquote(body) = block(
  width: 100%, inset: (left: 1.0em, rest: 0pt), above: 1.0em, below: 1.0em,
  stroke: (left: 1.5pt + rgb("#c9c9c9")),
  text(size: 10pt, style: "italic", fill: rgb("#333333"), body),
)
"""


def render_page(page: dict, index: dict, code: str = "",
                elsewhere: dict | None = None) -> str:
    kind = page.get("kind", "atomic")
    ref = f"{code}/{page['id']}" if code else page["id"]
    # The reference is printed on every page so an idea can be quoted back --
    # to a person or to an AI -- without describing it. Semantic rather than
    # hashed: `s-no-lead` survives being half-remembered, `K7F2` does not.
    out = ["#pagebreak(weak: true)",
           "#block(below: 1.1em)[#grid(columns: (1fr, auto), gutter: 0.6em,"
           f"  {kicker_inline(KIND_LABEL.get(kind, kind), KIND_MARK.get(kind, ''))},"
           f"  idref[{esc(ref)}],"
           ")]"]
    out.append(f'#heading(level: 2)[{rich(page["title"])}] {index[page["id"]]}')
    # Record where this note actually landed, so overflow is detectable after
    # the fact. One note is meant to be one page; a spill is invisible in the
    # exit code and only shows up as an orphan line on the following page.
    out.append('#context [#metadata((id: "%s", edge: "start", page: here().page())) <pgstamp>]'
               % page["id"])

    # A claim is the governing thought, not a property of one page kind --
    # gating it on `atomic` silently dropped it from every other kind.
    if page.get("claim"):
        out.append(f'#claim[{rich(page["claim"])}]')

    if kind == "atomic":
        if page.get("cite"):
            out.append(f'#cite-line[Source: {esc(page["cite"])}]')
        # A still from the cited moment. Not decoration: the citation already
        # pins the instant, so the frame shows what is being quoted.
        if page.get("frame"):
            out.append(f'#block(below: 1.1em)[#image("{page["frame"]}", width: 40%)]')
    elif kind == "reprise":
        if page.get("slug"):
            out.append(f'#cite-line[Your note: {esc(page["slug"])}]')
    # A recap of the reader's existing note, inline above the synthesis that
    # uses it. On its own page it was 80 words of white space, and it made the
    # reader carry the recap across a page turn to the argument that needed it.
    if page.get("recall"):
        rec = page["recall"]
        head = f'From your notes — {rec.get("slug", "")}'
        out.append(f'#recall[{esc(head)}][{rich(rec["text"])}]')

    if kind == "synthesis":
        # A page in this part resolves to a page number. One in another part
        # cannot -- Typst labels do not cross documents -- so it is named by
        # its reference instead, which is what references are for.
        pieces = []
        for parent in page.get("parents", []):
            if parent in index:
                pieces.append(f"p.#context counter(page).at({index[parent]}).first()")
            elif elsewhere and parent in elsewhere:
                ref = f"{code}/{parent}" if code else parent
                pieces.append(f'{esc(ref)} (Part {elsewhere[parent]})')
        stance = STANCE_LABEL.get(page.get("stance", ""), "")
        line = f'{esc(stance)} — see {" and ".join(pieces)}' if pieces else esc(stance)
        out.append(f"#cite-line[{line}]")

    for para in page.get("body", []):
        out.append(f"#pullquote[{rich(para)}]" if _is_quote(para) else rich(para))

    # The consequence goes last, in its own block. Readers emphasise what
    # arrives at the end (Gopen & Swan's stress position), so the page should
    # close on what it means for you rather than trailing off into evidence.
    # Making it a field rather than a convention means a page cannot quietly
    # ship without one.
    if page.get("so_what"):
        out.append(f'#sowhat[{rich(page["so_what"])}]')

    out.append('#context [#metadata((id: "%s", edge: "end", page: here().page())) <pgstamp>]'
               % page["id"])
    return "\n\n".join(out)


def legend(section: dict) -> str:
    """The key to the marks, for the foot of a part's title page.

    Only the kinds this part actually contains: a reader opening part 3 has
    never seen the marks explained, and explaining one that never appears is
    noise. Empty when a part is all one kind, since there is nothing to tell
    apart.
    """
    seen = [k for k in KIND_MARK if any(
        page.get("kind", "atomic") == k for page in section.get("pages", []))]
    if len(seen) < 2:
        return ""
    cells = " + h(1.1em) + ".join(
        f'box[#{KIND_MARK[k]}#h(0.4em)] + text[{esc(KIND_LABEL[k].lower())}]' for k in seen)
    return (f'#align(center)[#text(size: 7.5pt, fill: rgb("#999999"))[#({cells})]]'
            "\n#v(0.9em)")


def kicker_inline(body: str, mark: str = "") -> str:
    """The kicker as a grid cell. `mark` is a Typst symbol defined in the
    preamble, drawn before the label."""
    label = ('text(size: 7pt, weight: "bold", tracking: 0.14em, fill: rgb("#7a7a7a"))'
             f'[{esc(body.upper())}]')
    return f"box[#{mark}#h(0.45em)] + {label}" if mark else label


def render_front(entry: dict) -> str:
    """A front-matter page: who the guest is, how to read this, and so on.

    Read before the contents, by someone who may know nothing about the
    source, so these are the one place in the book that assumes no context.
    """
    out = ["#pagebreak(weak: true)", f'#kicker[{esc(entry.get("kicker", "Before you start"))}]']
    out.append(f'#heading(level: 2)[{rich(entry["title"])}]')
    out.extend(rich(para) for para in entry.get("body", []))
    return "\n\n".join(out)


_ID_SHAPE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Parts are ordered by how likely they are to matter to this reader, not by
# where they fell in the source. A blend: friction with notes already held
# leads, but speaker emphasis and corpus density can still lift a topic the
# reader has written nothing about yet.
WEIGHTS = {"collide": 3.0, "recall": 2.0, "yours": 3.0, "density": 1.0, "minutes": 1.0}
PART_PAGES = (8, 12)      # a sitting you finish, not one you abandon
MINUTES_FULL = 30.0       # source time at which the emphasis signal saturates


def _range_minutes(ranges: list[str]) -> float:
    """Minutes of source covered by `0:02:56–0:11:29` style ranges."""
    def seconds(stamp: str) -> float:
        total = 0.0
        for part in stamp.strip().split(":"):
            total = total * 60 + float(part or 0)
        return total
    span = 0.0
    for entry in ranges or []:
        bounds = re.split(r"[–—]", entry)
        if len(bounds) == 2:
            span += max(0.0, seconds(bounds[1]) - seconds(bounds[0]))
    return span / 60.0


# A Johnny Decimal area is the two-digit prefix of a note's id: `20.07` is
# area 20. Both a reprise slug (`20-ai/20.05-x`) and a recall slug
# (`20.07 — Prompt-engineering principles`) carry one.
_AREA = re.compile(r"\b(\d{2})\.\d")


def _area_key(token: str) -> str:
    """Areas are written both ways in book.json -- `20` and `20-ai`. The
    density map is keyed by JD number, so both have to land on `20`."""
    match = re.match(r"(\d{2})", token.strip())
    return match.group(1) if match else token.strip()


def section_areas(section: dict) -> list[str]:
    """JD areas this part touches. An explicit `areas` list wins; otherwise
    read them off the notes the part recalls -- recalling a note is exactly
    what "this part touches that area" means."""
    if section.get("areas"):
        return [_area_key(a) for a in section["areas"]]
    found = []
    for page in section.get("pages", []):
        slug = (page.get("recall") or {}).get("slug") or page.get("slug") or ""
        match = _AREA.search(slug)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def area_density(cache: Path | None = None, url: str = NOTES_URL) -> dict:
    """Notes per JD area in the Arcana corpus, normalised to 0..1.

    Revealed preference: an area you have written 306 notes in matters more to
    you than one with 3. Normalised against the densest area rather than used
    raw, because a raw count would swamp every other signal -- collide, recall,
    yours and minutes all sit in roughly 0..3.

    Cached on disk, since the ordering of a book does not move with one new
    note. Raises on any network or parse failure; the caller decides whether a
    book should fail to render because a PKM server is down (it should not).
    """
    if cache and cache.is_file():
        try:
            return json.loads(cache.read_text())
        except (json.JSONDecodeError, OSError):
            pass  # a corrupt cache is a refetch, not a failure
    request = urllib.request.Request(url, headers={"x-arcana-agent": "mcp"})
    # The host serves a self-signed cert on the tailnet, where the tailnet is
    # already the authentication.
    with urllib.request.urlopen(request, timeout=NOTES_TIMEOUT_S,
                                context=ssl._create_unverified_context()) as response:
        notes = json.load(response).get("notes", [])
    counts = Counter(str(n.get("jd") or "").split(".")[0]
                     for n in notes if n.get("jd"))
    top = max(counts.values(), default=0)
    density = {area: round(n / top, 4) for area, n in counts.items()} if top else {}
    if cache:
        cache.write_text(json.dumps(density, indent=0, sort_keys=True))
    return density


def density_for(book: dict, book_path: Path, fetch: bool) -> dict | None:
    """Density to rank with, or None. Never raises: a PKM server being down
    costs the ranking one signal, it does not stop a book being rendered."""
    if book.get("density") or not fetch:
        return book.get("density")
    try:
        return area_density(book_path.with_name("area-density.json"))
    except Exception as exc:  # any failure here -- network, TLS, JSON -- is the same failure
        print(f"warning: corpus density unavailable ({exc}); ranking without it",
              file=sys.stderr)
        return None


def dead_signals(book: dict) -> list[str]:
    """Signals that are zero in every part, and so are not ordering anything.

    A signal can be dead because the data was never written (no synthesis has
    carried `stance: collide`) rather than because the code is wrong. Printing
    it makes the difference visible instead of leaving a weight silently
    multiplying nothing.
    """
    sections = book.get("sections", [])
    if not sections:
        return []
    return [f"{key}: 0 in every part -- signal inactive"
            for key in WEIGHTS
            if all(not s.get("_signals", {}).get(key) for s in sections)]


def score_section(section: dict, density: dict | None = None) -> tuple[float, dict]:
    """Score one part, and return the inputs so the ordering can be argued with.

    Deliberately ignores "connects to nothing you hold". An idea touching no
    existing note is either the most valuable thing in the book or noise, and
    nothing computable here tells those apart -- ranking on it would reliably
    push junk to the front.
    """
    pages = section.get("pages", [])
    signals = {
        "collide": sum(1 for p in pages if p.get("stance") == "collide"),
        "recall": sum(1 for p in pages if p.get("recall")),
        "yours": sum(1 for p in pages if p.get("kind") == "yours"),
        "minutes": min(_range_minutes(section.get("ranges", [])), MINUTES_FULL) / MINUTES_FULL,
        "density": max([(density or {}).get(a, 0) for a in section_areas(section)], default=0),
    }
    score = sum(WEIGHTS[k] * v for k, v in signals.items())
    return score, signals


def rank_sections(book: dict, density: dict | None = None) -> list[dict]:
    """Parts, best first. An explicit `pin` always beats the heuristic --
    on your own book your judgement should win."""
    scored = []
    for position, section in enumerate(book.get("sections", [])):
        score, signals = score_section(section, density)
        section["_score"], section["_signals"] = round(score, 2), signals
        pin = section.get("pin")
        scored.append(((0, pin) if pin is not None else (1, -score), position, section))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in scored]


def part_size_warnings(book: dict) -> list[str]:
    low, high = PART_PAGES
    out = []
    for section in book.get("sections", []):
        count = len(section.get("pages", []))
        if count > high:
            out.append(f"part '{section['title']}' has {count} pages (over {high}); split it")
        elif count < low:
            out.append(f"part '{section['title']}' has {count} pages (under {low}); consider merging")
    return out


# Hosts we can name a platform for. Anything else lands under `other`, which
# keeps the tree stable rather than inventing a folder per domain.
_PLATFORMS = {"youtube.com": "youtube", "youtu.be": "youtube"}


def _path_slug(name: str, lower: bool = True) -> str:
    """One path segment: no separators, no traversal, never empty.

    `meta.code` and `meta.channel` are hand-authored, so a stray `/` or `..`
    must become part of the name rather than a level in the tree.
    """
    text = re.sub(r"[^A-Za-z0-9]+", "-", str(name)).strip("-")
    return (text.lower() if lower else text) or "unknown"


def book_dir(book: dict) -> Path:
    """Output folder for one book: `<platform>/<channel>/<code>`.

    Mirrors the source store, which is already `sources/youtube/<video_id>/`,
    and groups by channel because one channel produces many books while an
    ad-hoc video produces one. The leaf prefers `meta.code` -- the handle
    printed on every page as `DHH501/<id>` -- so the folder matches the
    reference readers see in the text.

    Channel comes from `meta.channel` (copied from the capture's meta.json) and
    can be overridden with `meta.channel_slug` when the real channel name makes
    a poor folder.
    """
    meta = book.get("meta", {})
    host = urllib.parse.urlparse(str(meta.get("source_url") or "")).netloc.lower()
    platform = _PLATFORMS.get(host.removeprefix("www."), "other")
    channel = meta.get("channel_slug") or meta.get("channel") or "misc"
    code = meta.get("code") or meta.get("title") or "book"
    return Path(platform) / _path_slug(channel) / _path_slug(code, lower=False)


def validate_ids(book: dict) -> None:
    """Refuse to render a book whose ids are not usable as references.

    An id is a public handle -- printed on the page, quoted back by a human,
    grepped by an AI. A duplicate silently makes two pages answer to one name,
    and a stray capital or space makes a ref that cannot be typed. Both are
    cheap to prevent here and expensive to notice in a PDF.
    """
    seen, problems = set(), []
    for section in book.get("sections", []):
        for page in section.get("pages", []):
            page_id = page.get("id")
            if not page_id:
                problems.append(f"page '{page.get('title', '?')}' has no id")
            elif page_id in seen:
                problems.append(f"duplicate id: {page_id}")
            elif not _ID_SHAPE.match(page_id):
                problems.append(f"id not kebab-case: {page_id}")
            else:
                seen.add(page_id)
    if problems:
        raise SystemExit("book.json ids are unusable as references:\n  "
                         + "\n  ".join(problems))


def render_index(book: dict, index: dict, code: str) -> str:
    """Back-of-book table: every ref, what it claims, and where it is.

    This is the lookup surface. Given "look at s-no-lead" the reader finds the
    page; given the same string an assistant finds the entry in book.json.
    """
    out = ['#pagebreak(weak: true)', '#kicker[Index of ideas]',
           '#text(size: 8.5pt, style: "italic", fill: rgb("#777777"))[Quote any '
           'reference below to point at one idea precisely.]', '#v(0.6em)']
    for section in book.get("sections", []):
        for page in section.get("pages", []):
            ref = f"{code}/{page['id']}" if code else page["id"]
            gist = page.get("claim") or page.get("title", "")
            words = gist.split()
            if len(words) > 22:
                gist = " ".join(words[:22]) + "…"
            out.append(f"""
#block(below: 0.9em)[
  #grid(columns: (1fr, auto), gutter: 0.6em,
    idref[{esc(ref)}],
    text(size: 8pt, fill: rgb("#999999"))[p.#context counter(page).at({index[page["id"]]}).first()],
  )
  #text(size: 8.5pt, fill: rgb("#444444"))[{rich(gist)}]
]""")
    return "\n".join(out)


def render_part(book: dict, part_no: int, local: dict, elsewhere: dict) -> str:
    """One part, as a standalone document.

    A part is a separate PDF you can finish in a sitting, not a chapter you
    scroll past. Part 1 carries the front matter and the map of all parts;
    later parts open straight on their first idea, because a reader who has
    reached part 3 does not need to be told who the guest is again.
    """
    meta = book.get("meta", {})
    code = meta.get("code", "")
    section = book["sections"][part_no - 1]
    total = len(book["sections"])
    is_first = part_no == 1

    doc = [PREAMBLE.replace("{title}", json.dumps(
        f'{meta.get("title", "Book")} — Part {part_no}'))]

    cover = meta.get("cover_path")
    cover_block = f'#block(below: 1.6em)[#image("{cover}", width: 55%)]' if cover else ""
    doc.append(f"""
#set page(numbering: none)
#v(2.2cm)
#align(center)[
  #set par(justify: false, first-line-indent: 0em)
  {cover_block}
  #text(size: 8pt, tracking: 0.2em, fill: rgb("#999999"))[PART {part_no} OF {total}]
  #v(0.8em)
  #block(width: 82%)[#text(size: 19pt, weight: "semibold", hyphenate: false)[{rich(section["title"])}]]
  #v(0.9em)
  #block(width: 78%)[#text(size: 9.5pt, style: "italic", fill: rgb("#666666"))[{rich(section.get("hook", ""))}]]
  #v(2.4em)
  #text(size: 9pt, fill: rgb("#666666"))[
    {rich(meta.get("title", ""))} \\
    {esc(meta.get("source_show", ""))} · {esc(section.get("ranges", [""])[0] if section.get("ranges") else "")}
  ]
]
#v(1fr)
{legend(section)}
#align(center)[#text(size: 7.5pt, style: "italic", fill: rgb("#999999"))[{esc(meta.get("note", ""))}]]
#pagebreak()
""")

    if is_first:
        for entry in book.get("front", []):
            doc.append(render_front(entry))

        # The map of the whole thing lives in part 1 only -- it is the one a
        # reader opens first, and repeating it in every part is noise.
        doc.append('#pagebreak(weak: true)\n#kicker[The parts]\n')
        for number, other in enumerate(book["sections"], 1):
            here = " (this one)" if number == part_no else ""
            doc.append(f"""
#block(below: 1.3em)[
  #text(size: 11pt, weight: "semibold")[{number}. {rich(other["title"])}{esc(here)}]
  #v(0.25em)
  #text(size: 9pt, style: "italic", fill: rgb("#666666"))[{rich(other.get("hook", ""))}]
  #v(0.2em)
  #text(size: 7.5pt, fill: rgb("#a0a0a0"))[{esc(" · ".join(other.get("ranges", [])))} · {len(other["pages"])} pages]
]""")

    doc.append('\n#set page(numbering: "1")\n#counter(page).update(1)\n')

    for page in section["pages"]:
        doc.append(render_page(page, local, code, elsewhere))

    doc.append(render_index({"sections": [section]}, local, code))

    if book.get("sources"):
        doc.append('#pagebreak(weak: true)\n#kicker[Sources & further reading]\n')
        for src in book["sources"]:
            bits = [b for b in (src.get("show"), src.get("published"), src.get("duration")) if b]
            doc.append(f"""
#block(below: 1.2em)[
  #text(weight: "semibold")[{rich(src["title"])}]
  #linebreak() #text(size: 8.5pt, fill: rgb("#777777"))[{esc(" · ".join(bits))}]
  #linebreak() #text(size: 8pt, fill: rgb("#999999"))[{esc(src.get("url", ""))}]
  #linebreak() #text(size: 8.5pt, style: "italic")[{rich(src.get("note", ""))}]
]""")
    return "\n\n".join(doc)


def part_maps(book: dict) -> tuple[list[dict], dict]:
    """Per-part label maps, plus which part every id lives in.

    A Typst label only resolves inside its own document, so a synthesis whose
    parent sits in another part cannot say "see p.7". It says
    "see DHH501/y-metric-gradient (Part 1)" instead -- which is the whole
    reason the ids are printed.
    """
    locals_, home = [], {}
    for number, section in enumerate(book.get("sections", []), 1):
        ids = {p["id"]: f"<pg-{p['id']}>" for p in section.get("pages", []) if "id" in p}
        locals_.append(ids)
        for page_id in ids:
            home[page_id] = number
    return locals_, home

def check_overflow(typ_path: Path) -> list[str]:
    """Names of notes that spilled past one page.

    Uses here().page() -- the physical sheet -- not counter(page), which is the
    displayed number and is reset after the contents, so the two disagree by a
    fixed offset and every comparison against it is quietly wrong.

    Each note stamps its own first and last page, so a note is compared only
    against itself. Comparing against the next note instead would count an
    intervening section-opener page as a spill and would never check the last
    note at all. Word counts do not work here either: a block quote eats far
    more vertical space than its length suggests.
    """
    result = subprocess.run(
        ["typst", "query", str(typ_path), "<pgstamp>", "--field", "value"],
        capture_output=True, text=True, timeout=TYPST_TIMEOUT_S,
    )
    if result.returncode != 0:
        return []
    starts, spilled = {}, []
    for stamp in json.loads(result.stdout):
        if stamp["edge"] == "start":
            starts[stamp["id"]] = stamp["page"]
        elif stamp["page"] > starts.get(stamp["id"], stamp["page"]):
            spilled.append(stamp["id"])
    return spilled


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book", help="path to book.json")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"output dir (default: {DEFAULT_OUT})")
    ap.add_argument("--keep-typ", action="store_true", help="keep the generated .typ next to the PDF")
    ap.add_argument("--explain", action="store_true",
                    help="show why the parts were ordered the way they were")
    ap.add_argument("--density", action="store_true",
                    help="weight parts by how many notes you hold in the areas they touch "
                         "(fetches once from Arcana, then reads a cache beside book.json)")
    args = ap.parse_args()

    book_path = Path(args.book).expanduser()
    book = json.loads(book_path.read_text())
    validate_ids(book)
    book["sections"] = rank_sections(book, density_for(book, book_path, args.density))
    # One folder per book. Parts, frames and the cover are a set, and a flat
    # directory mixes them with every other book's set.
    out_dir = Path(args.out).expanduser() / book_dir(book)
    out_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-z0-9]+", "-", book["meta"].get("title", "book").lower()).strip("-")

    # Typst resolves paths relative to the document's own directory and refuses
    # to escape it, so the cover has to sit beside the .typ rather than being
    # referenced back in the source store. A cover that has gone missing is
    # dropped instead of failing the build -- it is decoration.
    for section in book.get("sections", []):
        for page in section.get("pages", []):
            frame_src = page.get("frame")
            if not frame_src:
                continue
            source = Path(frame_src).expanduser()
            if source.is_file():
                local = f"frame-{source.stem}{source.suffix}"
                shutil.copyfile(source, out_dir / local)
                page["frame"] = local
            else:
                page.pop("frame")

    cover_src = book["meta"].get("cover_path")
    if cover_src:
        source = Path(cover_src).expanduser()
        if source.is_file():
            local = f"cover{source.suffix}"
            shutil.copyfile(source, out_dir / local)
            book["meta"]["cover_path"] = local
        else:
            book["meta"].pop("cover_path")

    for warning in part_size_warnings(book):
        print(f"warning: {warning}", file=sys.stderr)
    if args.explain:
        for section in book["sections"]:
            print(f"  {section['_score']:>6}  {section['title'][:44]}  {section['_signals']}",
                  file=sys.stderr)
        for dead in dead_signals(book):
            print(f"  {dead}", file=sys.stderr)

    locals_, home = part_maps(book)
    for part_no, section in enumerate(book["sections"], 1):
        part_slug = re.sub(r"[^a-z0-9]+", "-", section["title"].lower()).strip("-")[:48]
        typ_path = out_dir / f"{part_no}-{part_slug}.typ"
        pdf_path = out_dir / f"{part_no}-{part_slug}.pdf"
        typ_path.write_text(render_part(book, part_no, locals_[part_no - 1], home))

        try:
            result = subprocess.run(["typst", "compile", str(typ_path), str(pdf_path)],
                                    capture_output=True, text=True, timeout=TYPST_TIMEOUT_S)
        except FileNotFoundError:
            raise SystemExit("typst not found on PATH. Install it: brew install typst")
        if result.returncode != 0:
            # Keep the .typ on failure -- the error points at a line in it.
            raise SystemExit(f"typst failed (source kept at {typ_path}):\n{result.stderr}")

        for note_id in check_overflow(typ_path):
            print(f"warning: part {part_no}: '{note_id}' spilled past one page",
                  file=sys.stderr)
        if not args.keep_typ:
            typ_path.unlink(missing_ok=True)
        print(pdf_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
