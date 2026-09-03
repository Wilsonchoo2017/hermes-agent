#!/usr/bin/env python3
"""file_book_to_arcana.py — file a youtube-book's atomic pages into Arcana.

Reusable tail of the youtube-content pipeline. After Wilson approves a book's
atomic pages (the book is the reading; these are the durable knowledge), this
script files each `atomic` page into his Arcana Zettelkasten as a note:

  - creates a note per atomic page, in the JD area mapped for its chapter
  - opens with `Source: [[<hub-slug>]] (<show>, <cite>)`
  - allocates the next free JD under each area (flat siblings)
  - stamps every filed atomic `uncertain=true` (+28d review rail) via the
    status API
  - appends the filed notes to the source hub's "Atomic notes" section

Synthesis and `yours` pages stay in the book; only atomics (and the hub) become
notes.

USAGE:
  python3 file_book_to_arcana.py <path/to/book.json> [--dry]
     --dry   render + allocate but do not POST; print what would happen

It reads the placement map from --conf (see DEFAULT_CONF) and the hub slug from
--hub, or a `meta.arcana_hub` field in book.json. Requires the `arcana-post-note`
helper (arcana-note-system skill) on the write path; reads the corpus via the
list API to allocate JD numbers.

DEPENDENCIES: python3 stdlib + curl (self-signed TLS handled with -k).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

DEFAULT_BASE = "https://dxp.tail83c2f.ts.net:9651"
HEADERS = ["-H", "Content-Type: application/json", "-H", "x-arcana-agent: mcp"]

# Chapter title -> (home area folder, extra-tags). This is the only part that
# needs a human's judgement: where each chapter's ideas live in the JD scheme.
DEFAULT_CONF = [
    # (section-title substring, area slug, [tags])
    ("How software will change", "20-ai", []),
    ("AI impact on open source", "20-ai", []),
    ("Building Omarchy", "20-ai", []),
    ("Vibe coding vs agentic", "20-ai", ["vibe-coding"]),
    ("End of manual programming", "20-ai", []),
    ("Voice prompting", "20-ai", ["interface"]),
    ("Linux will win", "20-ai", ["os"]),
    ("Future of programming", "20-ai", []),
    ("Surviving Internet Hate", "33-character-inner-life", ["resilience"]),
    ("Fatherhood", "33-character-inner-life", ["family"]),
    ("PewDiePie", "33-character-inner-life", ["role-models"]),
    ("Longevity", "33-character-inner-life", ["finitude"]),
    ("Eternal recurrence", "33-character-inner-life", ["optimism"]),
    ("Politics", "15-macro-geopolitics", ["immigration", "politics"]),
    ("AI video generation and filmmaking", "31-frames-attention", ["media", "creativity"]),
]

BASE_TAGS = ["dhh", "agentic-engineering"]
SHOW = "Lex Fridman Podcast #501"
HUB_BASE = "02-literature"
HUB_SLUG = "02.01-lex-501-dhh-agentic-engineering"


def api(path: str, method: str = "GET", payload: dict | None = None,
        base: str = DEFAULT_BASE) -> dict:
    cmd = ["curl", "-sk", "-m", "30", "-X", method, base + path, *HEADERS]
    if payload is not None:
        cmd += ["--data-binary", json.dumps(payload, ensure_ascii=False)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"API {method} {path} failed: rc={r.returncode} {r.stderr}")
    return json.loads(r.stdout)


def corpus_list(base=DEFAULT_BASE) -> list[dict]:
    return api("/api/notes/list", base=base).get("notes", [])


def next_freed_js(area: str, notes: list[dict]) -> list[str]:
    """Every free leaf JD under `area`, in order, e.g. area=20-ai -> 20.04, 20.18...

    Must skip *each* occupied number, not scan forward to the first gap and
    then run on contiguously. Areas have holes -- 20-ai is missing 20.04 while
    20.05-20.17 are taken -- so a contiguous run from the first gap hands back
    slugs that already belong to other notes, and the write is a blind
    overwrite of somebody's note.
    """
    area_nums = set()
    for n in notes:
        slug = n.get("slug", "")
        if slug.split("/")[0] == area:
            jd = str(n.get("jd") or "")
            if re.match(rf"^{re.escape(area.split('-')[0])}\.\d+$", jd):
                area_nums.add(int(jd.split(".")[1]))
    base_num = int(area.split("-")[0])
    # Two digits: the corpus is uniformly zero-padded (20.04, never 20.4).
    return [f"{base_num}.{i:02d}" for i in range(1, 10000) if i not in area_nums]


def slug_from(jd: str, title: str) -> str:
    leaf = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    leaf = leaf[:80].rstrip("-")
    return f"{jd}-{leaf}"


def normalise_title(t: str) -> str:
    t = t.strip().strip('"').strip()
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def render_mdx(page: dict, hub_slug: str, jd: str, tags: list[str],
               created: str, related: str = "") -> str:
    """Turn a book atomic page into an Arcana MDX note, matching the harness-note format."""
    area_num = jd.split(".")[0]
    claim = page.get("claim", "")
    title = page.get("title", "").strip().strip('"')
    body = page.get("body", [])

    # Description: the claim as a one- to two-sentence lede.
    desc = claim
    # Body paragraphs: keep quotes as markdown blockquotes.
    md = []
    for para in body:
        p = para.strip()
        if p.startswith('"') and p.endswith('"'):
            md.append("> " + p)
        elif '"' in p and p.count('"') >= 2:
            # a paragraph that embeds a quote mid-text -> keep as prose
            md.append(p)
        else:
            md.append(p)
    body_md = "\n\n".join(md)

    fm = (
        "---\n"
        f'title: "{escape_fm(title)}"\n'
        f'description: "{escape_fm(desc)}"\n'
        f'jd: "{jd}"\n'
        f'luhmann: "{jd}"\n'
        f'parent: "{area_num}"\n'
        f"created: {created}\n"
        f"tags: [{', '.join(tags)}]\n"
    )
    if related:
        fm += "related:\n" + related
    fm += "---\n"

    return (
        fm
        + f"Source: [[{hub_slug}]] ({SHOW}, {page.get('cite', '')}).\n\n"
        + body_md
        + "\n\n## Why it matters\n\n"
        + page.get("so_what", "")
        + "\n"
    )


def escape_fm(s: str) -> str:
    return s.replace('"', '\\"').replace("\n", " ")


def post_note(slug: str, mdx: str, base=DEFAULT_BASE, dry=False):
    """POST a note via the page endpoint (create:true). Returns (slug, ok)."""
    if dry:
        return slug, True
    cmd = ["curl", "-sk", "-m", "30", "-X", "POST",
           f"{base}/api/notes/page/{slug}", *HEADERS,
           "--data-binary", json.dumps({"raw": mdx, "create": True, "autoResolve": False},
                                       ensure_ascii=False)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    ok = r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower()
    if not ok:
        print(f"  ! POST {slug} -> rc={r.returncode} {r.stdout[:200]} {r.stderr[:200]}",
              file=sys.stderr)
    return slug, ok


def stamp_uncertain(slug: str, base=DEFAULT_BASE, dry=False):
    if dry:
        return
    payload = {"slug": slug, "uncertain": True}
    cmd = ["curl", "-sk", "-m", "30", "-X", "POST", f"{base}/api/notes/status", *HEADERS,
           "--data-binary", json.dumps(payload)]
    subprocess.run(cmd, capture_output=True, text=True)


def patch_hub(hub_slug: str, notes_by_section: list[tuple[str, list[dict]]],
              base=DEFAULT_BASE, dry=False):
    """Append/replace the 'Atomic notes' listing on the source hub."""
    raw = api(f"/api/notes/page/{hub_slug}", base=base).get("raw", "")
    if "## Atomic notes from this source" in raw:
        raw = raw.split("## Atomic notes from this source")[0].rstrip() + "\n"
    listing = ["## Atomic notes from this source", ""]
    for _, (sec_title, pages) in enumerate(notes_by_section):
        if not pages:
            continue
        listing.append(f"### {sec_title}")
        for p in pages:
            listing.append(f"- [[{p['slug']}]] — {p['title']}")
        listing.append("")
    new_raw = raw.rstrip() + "\n\n" + "\n".join(listing).rstrip() + "\n\n"
    if dry:
        print("  [dry] would patch hub", hub_slug)
        return
    payload = {"raw": new_raw, "create": False, "autoResolve": False}
    cmd = ["curl", "-sk", "-m", "30", "-X", "POST", f"{base}/api/notes/page/{hub_slug}",
           *HEADERS, "--data-binary", json.dumps(payload, ensure_ascii=False)]
    subprocess.run(cmd, capture_output=True, text=True)


def match_conf(title: str, conf: list[tuple[str, str, list]]):
    for sub, area, tags in conf:
        if sub.lower() in title.lower():
            return area, tags
    return "20-ai", []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book", help="path to book.json")
    ap.add_argument("--dry", action="store_true", help="render+build, do not POST")
    ap.add_argument("--base", default=os.environ.get("ARCANA_BASE_URL", DEFAULT_BASE))
    args = ap.parse_args()

    book = json.loads(Path(args.book).expanduser().read_text())
    hub_slug = (book.get("meta", {}) or {}).get("arcana_hub") or HUB_SLUG
    conf = DEFAULT_CONF

    notes = corpus_list(args.base)
    # allocate JD pools per area lazily
    pools: dict[str, list[str]] = {}
    jd_used: set[str] = set()
    # Corpus-wide normalised titles: an already-filed atomic is skipped regardless
    # of which area it landed in (the smoke-test atomics span 20-ai + 31-frames).
    all_titles = {normalise_title(n.get("title", "")) for n in notes}
    # Belt and braces: the allocator should never produce an occupied slug,
    # but the write is destructive (create:true, no expectedVersion), so the
    # slug is checked again immediately before use.
    existing_slugs = {n.get("slug", "") for n in notes}
    filed: list[tuple[str, list[dict]]] = []
    created = _dt.date.today().isoformat()

    total = 0
    skipped = 0
    for section in book.get("sections", []):
        sec_title = section.get("title", "")
        area, extra = match_conf(sec_title, conf)
        if area not in pools:
            pools[area] = next_freed_js(area, notes)
        area_pages = []
        for page in section.get("pages", []):
            if page.get("kind") != "atomic":
                continue
            # skip if this title is already in the corpus (idempotent re-run)
            norm = normalise_title(page.get("title", ""))
            if norm and norm in all_titles:
                skipped += 1
                continue
            total += 1
            jd = pools[area].pop(0)
            while jd in jd_used:
                jd = pools[area].pop(0)
            jd_used.add(jd)
            full = slug_from(jd, page.get("title", "id"))
            slug = f"{area}/{full}"
            if slug in existing_slugs or any(
                    e.startswith(f"{area}/{jd}-") for e in existing_slugs):
                raise SystemExit(
                    f"refusing to write {slug}: JD {jd} is already taken.\n"
                    "  The allocator produced an occupied slug -- this would "
                    "overwrite an existing note. Nothing written.")
            existing_slugs.add(slug)
            tags = BASE_TAGS + extra
            mdx = render_mdx(page, hub_slug, jd, tags, created)
            if not args.dry:
                api_base = f"{args.base}/api/notes/page/{slug}"
                # route through a file to avoid shell escaping (skill pitfall)
                with tempfile.NamedTemporaryFile("w", suffix=".mdx", delete=False) as fh:
                    fh.write(mdx)
                    tmp = fh.name
                cmd = ["curl", "-sk", "-m", "30", "-X", "POST", api_base, *HEADERS,
                       "--data-binary", "@" + tmp]
                r = subprocess.run(cmd, capture_output=True, text=True)
                ok = r.returncode == 0
                if not ok:
                    print(f"  ! POST {slug} rc={r.returncode} {r.stdout[:160]} {r.stderr[:160]}")
                    continue
                os.unlink(tmp)
                stamp_uncertain(slug, args.base)
            else:
                print(f"  [dry] {area}/{jd} {full}")
            area_pages.append({"slug": slug, "title": page.get("title", "").strip('"')})
        if area_pages:
            filed.append((sec_title, area_pages))

    print(f"\n{total} atomic pages processed.")
    if not args.dry:
        patch_hub(hub_slug, filed, args.base)
        print(f"hub updated: {hub_slug}")
    else:
        print("dry run complete — nothing written.")

    # per-area summary
    counts = Counter()
    for _, ps in filed:
        for p in ps:
            counts[p["slug"].split("/")[0]] += 1
    for a, c in counts.items():
        print(f"  {a}: {c}")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
