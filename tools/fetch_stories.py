"""Gather Romanian children's stories from Wikisource and teach them to A.R.S.

Alex asked for stories for his son. The web is full of them and most are somebody's
property, so this takes them from one place where they are not: `ro.wikisource.org`, whose
`Basme` and `Povești` collections are the Romanian canon — Ion Creangă (died 1889), Petre
Ispirescu (died 1887), and the folk tales they wrote down. Long out of copyright, in good
Romanian, and the stories a Romanian child's grandparents were told.

Every story keeps its title, its author where Wikisource records one, and the URL it came
from, so A.R.S can always say where a story is from. That is not bookkeeping: "where did
you get this" is a question a parent is entitled to ask about anything their child is
being read.

    uv run python tools/fetch_stories.py --list
    uv run python tools/fetch_stories.py --gateway http://127.0.0.1:8787 --limit 20

Polite by construction: one request at a time, a real User-Agent, and a pause between
fetches. Wikisource is a volunteer project and this is a personal assistant, not a crawler.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import unicodedata

import httpx
from selectolax.parser import HTMLParser

API = "https://ro.wikisource.org/w/api.php"
PAGE = "https://ro.wikisource.org/wiki/"
USER_AGENT = "ars-personal-assistant/0.1 (private; one reader; contact via github.com/alexccoin/ars)"

CATEGORIES = ("Categorie:Basme", "Categorie:Povești", "Categorie:Povestiri")
PAUSE_S = 1.5
"""Between requests. Wikimedia asks for serial, unhurried access and answers 429 when it
has had enough — which it did, at 0.4 s with two requests per story. This is a personal
assistant fetching a few dozen bedtime stories once, not a crawler, and it should cost
their volunteers nothing."""

MAX_ATTEMPTS = 4
BACKOFF_S = 5.0

MIN_CHARS = 900
"""Below this it is a stub, a disambiguation page or a fragment, not a story."""

MAX_CHARS = 60_000
"""A novel is not a bedtime story, and a 200-page text would dominate every search."""

SKIP = re.compile(r"^(Wikisource:|Categorie:|Discuție|Autor:|Index:|Pagină:)")


async def get(client: httpx.AsyncClient, params: dict) -> dict:
    """One API call, with the failure modes the API actually has.

    Every call used to be `(await client.get(...)).json()`, which turns a 429 into
    `JSONDecodeError: Expecting value: line 1 column 1` several frames from the cause. The
    server was saying "you are making too many requests" in plain English and the script
    reported malformed JSON. Read the status first, honour Retry-After, and say what
    happened.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = await client.get(API, params=params)
        if response.status_code == 429:
            wait = float(response.headers.get("retry-after") or BACKOFF_S * attempt)
            print(f"  (rate limited by wikisource; waiting {wait:.0f}s)", flush=True)
            await asyncio.sleep(wait)
            continue
        if response.status_code != 200:
            raise RuntimeError(f"wikisource returned {response.status_code}")
        if "json" not in response.headers.get("content-type", ""):
            raise RuntimeError(
                f"wikisource returned {response.headers.get('content-type')}, not JSON: "
                f"{response.text[:120]}"
            )
        return response.json()
    raise RuntimeError("wikisource kept rate limiting us; try again later")


async def titles(client: httpx.AsyncClient, limit: int) -> list[str]:
    found: list[str] = []
    for category in CATEGORIES:
        cursor: str | None = None
        while len(found) < limit:
            params = {
                "action": "query", "list": "categorymembers", "cmtitle": category,
                "cmlimit": "50", "format": "json",
            }
            if cursor:
                params["cmcontinue"] = cursor
            data = await get(client, params)
            for member in data.get("query", {}).get("categorymembers", []):
                title = member["title"]
                if not SKIP.match(title) and title not in found:
                    found.append(title)
            cursor = data.get("continue", {}).get("cmcontinue")
            if not cursor:
                break
            await asyncio.sleep(PAUSE_S)
    return found[:limit]


async def fetch(client: httpx.AsyncClient, title: str) -> tuple[str, str] | None:
    """The story's plain text and its author, or None if it is not a story.

    Rendered HTML rather than `prop=extracts`: TextExtracts is not installed on
    ro.wikisource, and asking for it returns a page that is not JSON at all — which
    failed as a decode error four calls deep rather than as "that API is not here".
    """
    data = await get(client, {
        # Pipe-separated, not a list: httpx encodes a list as repeated parameters and
        # MediaWiki reads only one of them, so the page came back with no text at all and
        # every story was reported as "too short to be a story".
        "action": "parse", "page": title, "prop": "text|categories",
        "format": "json", "formatversion": "2", "redirects": "1",
    })
    html = data.get("parse", {}).get("text", "")
    if isinstance(html, dict):  # formatversion 1 shape, in case the default changes
        html = html.get("*", "")
    if not html:
        return None

    tree = HTMLParser(html)
    # Editorial furniture, not the story: footnote markers, the licence tables, the
    # navigation header Wikisource puts above every text.
    for selector in ("sup", "table", "style", "script", ".mw-editsection",
                     ".reference", ".navbox", "#toc", ".mw-headline-number"):
        for node in tree.css(selector):
            node.decompose()
    lines = [line.strip() for line in tree.text(separator="\n").split("\n") if line.strip()]
    # The rendered page opens with a breadcrumb arrow, the title, the year and the author
    # on their own lines. They are recorded properly in the header this script writes, so
    # dropping them here stops the story starting with "← 1876 de".
    while lines and (len(lines[0]) < 40 or lines[0] in {"←", "→"}):
        lines.pop(0)
    text = "\n".join(lines)
    if len(text) < MIN_CHARS:
        return None
    text = unicodedata.normalize("NFC", text)[:MAX_CHARS]

    # The author comes from the SAME response as the text. It used to be a second call
    # per story, which doubled the request count against a server that was already asking
    # us to slow down.
    author = ""
    for category in data.get("parse", {}).get("categories", []):
        name = category.get("category", "") if isinstance(category, dict) else str(category)
        name = name.replace("_", " ")
        if name.startswith("Texte de "):
            author = name[len("Texte de "):]
            break
    return text, author


def as_document(title: str, author: str, text: str) -> tuple[str, bytes]:
    """A file A.R.S can learn, with its provenance in the text itself.

    The header is inside the document rather than only in metadata on purpose: the
    document tier answers by returning a passage verbatim, and a passage that has drifted
    away from its title should still be able to say what it belongs to.
    """
    header = f"POVESTE — {title}\n"
    if author:
        header += f"de {author}\n"
    header += f"Sursa: {PAGE}{title.replace(' ', '_')} (domeniu public, ro.wikisource.org)\n\n"
    slug = re.sub(r"[^a-z0-9]+", "-", _fold(title).lower()).strip("-")[:60]
    return f"poveste-{slug}.txt", (header + text).encode("utf-8")


def _fold(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="http://127.0.0.1:8787")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--list", action="store_true", help="show what would be fetched")
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        found = await titles(client, args.limit)
        print(f"{len(found)} candidate stories")
        if args.list:
            for title in found:
                print("  -", title)
            return 0

        learned = skipped = 0
        for title in found:
            await asyncio.sleep(PAUSE_S)
            story = await fetch(client, title)
            if story is None:
                print(f"  skip  {title} (too short to be a story)")
                skipped += 1
                continue
            text, author = story
            name, payload = as_document(title, author, text)
            try:
                response = await client.post(
                    f"{args.gateway}/api/documents",
                    files={"file": (name, payload, "text/plain")},
                )
                response.raise_for_status()
            except Exception as exc:
                print(f"  FAIL  {title}: {exc}")
                continue
            learned += 1
            by = f" de {author}" if author else ""
            print(f"  learned  {title}{by}  ({len(text):,} chars)")
        print(f"\n{learned} learned, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
