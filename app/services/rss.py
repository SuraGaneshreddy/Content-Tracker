"""
Minimal, dependency-free RSS/Atom reader.

Used for the News, Coding and Sports update pages and for update detection on
items whose URL points at a feed.  Parsing is deliberately tolerant: a broken
or partial feed returns whatever could be read instead of raising.

All remote text is untrusted and is sanitised by ``metadata.clean_text``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional

from ..config import settings
from ..utils.safe_http import FetchError, ThrottledError, UnsafeUrlError, safe_get
from .metadata import clean_text, clean_url

# Known public feeds, used by the category update pages.
NEWS_FEEDS = {
    "world": [("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml")],
    "technology": [
        ("Hacker News", "https://hnrss.org/frontpage"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ],
    "business": [("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml")],
    "entertainment": [("BBC Entertainment", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml")],
    "sports": [("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml")],
    "science": [("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml")],
    "other": [("Reuters World", "https://feeds.reuters.com/Reuters/worldNews")],
}

CODING_FEEDS = [
    ("Hacker News", "https://hnrss.org/frontpage"),
    ("GitHub Blog", "https://github.blog/feed/"),
    ("Python Insider", "https://blog.python.org/feeds/posts/default"),
    ("Changelog", "https://changelog.com/feed"),
    ("Lobsters", "https://lobste.rs/rss"),
]

SPORTS_FEEDS = {
    "cricket": [("BBC Cricket", "https://feeds.bbci.co.uk/sport/cricket/rss.xml")],
    "football": [("BBC Football", "https://feeds.bbci.co.uk/sport/football/rss.xml")],
    "basketball": [("BBC Basketball", "https://feeds.bbci.co.uk/sport/basketball/rss.xml")],
    "tennis": [("BBC Tennis", "https://feeds.bbci.co.uk/sport/tennis/rss.xml")],
    "f1": [("BBC Formula 1", "https://feeds.bbci.co.uk/sport/formula1/rss.xml")],
    "other": [("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml")],
}

MAX_ENTRIES = 40


@dataclass
class FeedEntry:
    title: str
    link: Optional[str]
    published: Optional[str]
    summary: Optional[str]
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "link": self.link,
            "published": self.published,
            "summary": self.summary,
            "source": self.source,
        }


def _text(element: Optional[ET.Element]) -> Optional[str]:
    if element is None:
        return None
    return clean_text(element.text or "", 600)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_text: str, source: str = "") -> List[FeedEntry]:
    """Parse RSS 2.0 or Atom into FeedEntry objects."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        # Some feeds ship a stray BOM or leading whitespace/newlines.
        cleaned = xml_text.strip().lstrip("\ufeff")
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            return []

    entries: List[FeedEntry] = []
    root_tag = _strip_ns(root.tag)

    if root_tag == "rss":
        for item in root.iter():
            if _strip_ns(item.tag) != "item":
                continue
            fields = {_strip_ns(child.tag): child for child in item}
            title = _text(fields.get("title"))
            if not title:
                continue
            entries.append(
                FeedEntry(
                    title=title[:300],
                    link=clean_url(_text(fields.get("link"))),
                    published=_text(fields.get("pubdate") or fields.get("date")),
                    summary=_text(fields.get("description")),
                    source=source,
                )
            )
    elif root_tag == "feed":  # Atom
        ns = {"a": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        for entry in root.iter():
            if _strip_ns(entry.tag) != "entry":
                continue
            title_el = link_el = updated_el = summary_el = None
            for child in entry:
                tag = _strip_ns(child.tag)
                if tag == "title":
                    title_el = child
                elif tag == "link" and (child.get("rel") in (None, "alternate")):
                    link_el = child
                elif tag in ("updated", "published"):
                    updated_el = updated_el or child
                elif tag in ("summary", "content"):
                    summary_el = summary_el or child
            title = _text(title_el)
            if not title:
                continue
            entries.append(
                FeedEntry(
                    title=title[:300],
                    link=clean_url(link_el.get("href") if link_el is not None else None),
                    published=_text(updated_el),
                    summary=_text(summary_el),
                    source=source,
                )
            )
    return entries[:MAX_ENTRIES]


def fetch_feed(url: str, *, source: str = "", force: bool = False) -> List[FeedEntry]:
    """Fetch and parse a feed URL. Returns [] on any failure."""
    if not settings.rss_enabled and not force:
        return []
    try:
        result = safe_get(
            url,
            accept="application/rss+xml, application/atom+xml, application/xml, text/xml",
            force=force,
            max_bytes=512 * 1024,
        )
    except (UnsafeUrlError, FetchError, ThrottledError):
        return []
    if result.status_code >= 400:
        return []
    return parse_feed(result.text, source=source or result.final_url or url)


def topic_feed(topic: str) -> List[FeedEntry]:
    """Aggregate the configured feeds for a news/sports topic."""
    feeds = NEWS_FEEDS.get(topic) or SPORTS_FEEDS.get(topic) or NEWS_FEEDS["world"]
    entries: List[FeedEntry] = []
    for name, url in feeds:
        entries.extend(fetch_feed(url, source=name, force=True))
    return entries[:MAX_ENTRIES]


def coding_feed() -> List[FeedEntry]:
    entries: List[FeedEntry] = []
    for name, url in CODING_FEEDS:
        entries.extend(fetch_feed(url, source=name, force=True))
    return entries[:MAX_ENTRIES]
