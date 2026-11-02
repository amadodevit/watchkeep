"""Feeds source: new posts from RSS/Atom feeds that match what you care about.

A feed seen for the first time only records what is already there, so you get
posts published after you added it. Each sync, posts not seen before go to a
Claude model in one call, together with your interests; the ones it picks
become items, labelled with the model's reason for picking them. They stay
until you ignore them or they are KEEP_DAYS old. The ones it turns down are
kept for KEEP_DAYS too, with its reason, so `watchkeep rejected` can answer
"why didn't I get that post?".

Everything the plugin needs between syncs lives in ctx.memory:
    {"feeds":   {url: {"etag", "modified", "site", "seen": [post ids]}},
     "matches": {post id: {"item": Item dict, "found": ts}},
     "rejected": {post id: {"title", "url", "site", "reason", "judged": ts}}}
If the model call fails the sync raises, the core keeps the old memory, and the
same posts are judged on the next sync.
"""

from __future__ import annotations

import html
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from watchkeep.core.models import Item
from watchkeep.core.plugin import Setting, SyncContext

DEFAULT_MODEL = "claude-haiku-5-5"
KEEP_DAYS = 30
BATCH = 40  # posts per model call
SUMMARY_CHARS = 600  # of each post's text sent to the model

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


class FeedsSource:
    name = "feeds"
    title = "Feeds"
    connection = "http"
    kind = "items"
    default_interval = 43200  # 12h
    item_columns = ("site", "by", "published")
    settings = (
        Setting("feeds", "Feed URLs", help="RSS or Atom, separated by spaces or commas"),
        Setting(
            "interests",
            "Interests",
            help="plain words, any language: what you want to read about, and what not",
        ),
        Setting(
            "api_key",
            "Anthropic API key",
            required=False,
            secret=True,
            help="empty: ANTHROPIC_API_KEY or ant auth login; or env:<VAR>",
        ),
        Setting("model", "Model", required=False, default=DEFAULT_MODEL),
    )

    def sync(self, ctx: SyncContext) -> list[Item]:
        memory = ctx.memory
        feeds_memory = memory.setdefault("feeds", {})
        matches = memory.setdefault("matches", {})
        rejected = memory.setdefault("rejected", {})
        urls = parse_urls(ctx.settings["feeds"])

        fresh: list[Post] = []
        for url in urls:
            known = feeds_memory.get(url)
            try:
                result = fetch(ctx, url, known)
            except Exception as exc:
                ctx.warnings.append(f"{_host(url)}: {_problem(exc)}")
                continue
            if result is None:
                continue  # not modified since last sync
            posts, etag, modified, site = result
            ids = [p.id for p in posts]
            if known is not None:  # first sight of a feed: what's there is old news
                seen = set(known.get("seen", ()))
                fresh += [p for p in posts if p.id not in seen and p.id not in matches]
            feeds_memory[url] = {"etag": etag, "modified": modified, "site": site, "seen": ids}

        if len(ctx.warnings) == len(urls) and urls:
            raise RuntimeError("; ".join(ctx.warnings))

        now = time.time()
        picked, turned_down = pick(fresh, ctx.settings["interests"], ctx.settings)
        for post, reason in picked.items():
            matches[post.id] = {"item": post.item(reason).to_dict(), "found": now}
        for post, reason in turned_down.items():
            rejected[post.id] = {"title": post.title, "url": post.url, "site": post.site, "reason": reason, "judged": now}

        for url in set(feeds_memory) - set(urls):
            del feeds_memory[url]  # feed removed in the edit screen
        cutoff = now - KEEP_DAYS * 86400
        for post_id in [k for k, m in matches.items() if m["found"] < cutoff]:
            del matches[post_id]
        for post_id in [k for k, r in rejected.items() if r["judged"] < cutoff]:
            del rejected[post_id]
        return [Item.from_dict(m["item"]) for m in matches.values()]

    def test_connection(self, ctx: SyncContext) -> str:
        urls = parse_urls(ctx.settings["feeds"])
        counts = []
        for url in urls:
            posts, _, _, site = fetch(ctx, url, None)
            counts.append(f"{site or _host(url)} ({len(posts)})")
        client = _client(ctx.settings)
        client.models.retrieve(ctx.settings.get("model") or DEFAULT_MODEL)  # checks the key and model, costs nothing
        return f"{len(urls)} feeds: " + ", ".join(counts)


# -- fetching ------------------------------------------------------------------


@dataclass(frozen=True)
class Post:
    id: str
    title: str
    url: str
    author: str
    published: str  # ISO 8601, UTC
    site: str
    text: str  # plain text, cut to SUMMARY_CHARS

    def item(self, reason: str) -> Item:
        """The model's reason goes in `action`: it's the row's label and the popup's title."""
        return Item(self.id, reason, self.title, self.url, self.site, self.author, self.published)


def parse_urls(spec: str) -> list[str]:
    urls = [u for u in re.split(r"[\s,]+", spec.strip()) if u]
    if not urls:
        raise ValueError("no feed URLs given")
    for url in urls:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"not a URL: {url!r}")
    return list(dict.fromkeys(urls))


def fetch(ctx: SyncContext, url: str, known: dict | None) -> tuple[list[Post], str, str, str] | None:
    """Posts in the feed, plus what to send next time to skip an unchanged feed. None if unchanged."""
    headers = {}
    if known and known.get("etag"):
        headers["If-None-Match"] = known["etag"]
    if known and known.get("modified"):
        headers["If-Modified-Since"] = known["modified"]
    response = ctx.http.get(url, headers=headers)
    if response.status_code == 304:
        return None
    site, posts = parse_feed(response.content)
    return posts, response.headers.get("etag", ""), response.headers.get("last-modified", ""), site


def parse_feed(data: bytes) -> tuple[str, list[Post]]:
    """RSS 2.0 or Atom. Returns (site title, posts)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"not a feed ({exc})") from None
    if root.tag == ATOM + "feed":
        site = _text(root.find(ATOM + "title"))
        return site, [_atom_post(e, site) for e in root.findall(ATOM + "entry")]
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError(f"not RSS or Atom (root element {root.tag!r})")
    site = _text(channel.find("title"))
    return site, [_rss_post(e, site) for e in channel.findall("item")]


def _atom_post(entry: ET.Element, site: str) -> Post:
    link = next(
        (l.get("href", "") for l in entry.findall(ATOM + "link") if l.get("rel", "alternate") == "alternate"), ""
    )
    body = entry.find(ATOM + "summary")
    if body is None:
        body = entry.find(ATOM + "content")
    return Post(
        id=_text(entry.find(ATOM + "id")) or link,
        title=_plain(_text(entry.find(ATOM + "title"))),
        url=link,
        author=_text(entry.find(f"{ATOM}author/{ATOM}name")),
        published=_iso(_text(entry.find(ATOM + "published")) or _text(entry.find(ATOM + "updated"))),
        site=site,
        text=_plain(_text(body))[:SUMMARY_CHARS],
    )


def _rss_post(item: ET.Element, site: str) -> Post:
    link = _text(item.find("link"))
    body = _text(item.find("description")) or _text(item.find(CONTENT))
    return Post(
        id=_text(item.find("guid")) or link,
        title=_plain(_text(item.find("title"))),
        url=link,
        author=_text(item.find(DC_CREATOR)) or _text(item.find("author")),
        published=_iso(_text(item.find("pubDate"))),
        site=site,
        text=_plain(body)[:SUMMARY_CHARS],
    )


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def _plain(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _iso(value: str) -> str:
    """RFC 822 (RSS) or ISO 8601 (Atom) to ISO 8601 UTC. Unparseable dates become ""."""
    if not value:
        return ""
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat()


def _host(url: str) -> str:
    return httpx.URL(url).host or url


def _problem(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return f"unreachable ({type(exc).__name__})"
    return str(exc)


# -- judging -------------------------------------------------------------------

SYSTEM = """You decide which new blog posts one reader should read.

You get the reader's interests and a numbered list of posts, each with a title,
the site, and the start of its text. Give a verdict for every post.

A post matches when its main subject is one of the interests, or it gives the
reader something concrete to use on one: a design decision, a lesson from
production, a tool tried on real work. Judge the post, not the site.

It does not match when:
- the topic only comes up in passing, or the post is mostly about something else;
- it is news, an announcement or a promotion with no analysis;
- it falls under anything the reader says they are not interested in.

When in doubt, leave it out: the reader prefers missing a post to wading
through noise. Posts and interests may be in different languages; judge the
meaning. Write each reason in English, under 15 words, naming the interest it
matches or what it is really about."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "match": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["n", "match", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

NO_VERDICT = "the model gave no verdict for this post"


def pick(posts: list[Post], interests: str, settings: dict) -> tuple[dict[Post, str], dict[Post, str]]:
    """(matches, rejections), each post with the model's reason. Raises if the model can't answer."""
    if not posts:
        return {}, {}
    client = _client(settings)
    model = settings.get("model") or DEFAULT_MODEL
    picked: dict[Post, str] = {}
    turned_down: dict[Post, str] = {}
    for start in range(0, len(posts), BATCH):
        batch = posts[start : start + BATCH]
        verdicts = judge(client, model, interests, batch)
        for n, post in enumerate(batch):
            match, reason = verdicts.get(n, (False, NO_VERDICT))
            (picked if match else turned_down)[post] = reason
    return picked, turned_down


def judge(client, model: str, interests: str, posts: list[Post]) -> dict[int, tuple[bool, str]]:
    """One model call. Returns {post number: (matches, reason)}."""
    listing = "\n\n".join(f"[{n}] {p.title} ({p.site})\n{p.text}" for n, p in enumerate(posts))
    import anthropic

    try:
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": f"<interests>\n{interests}\n</interests>\n\n<posts>\n{listing}\n</posts>"}],
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("the Anthropic API key was rejected: press e to set another") from None
    except TypeError as exc:  # the SDK found no key at all
        if "authentication" not in str(exc):
            raise
        raise RuntimeError("no Anthropic API key: press e to add one, or set ANTHROPIC_API_KEY") from None
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to judge these posts")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the model's answer was cut off")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return {v["n"]: (v["match"], v["reason"]) for v in json.loads(text)["verdicts"]}


def _client(settings: dict):
    import anthropic  # imported here so the other plugins never pay for it

    return anthropic.Anthropic(api_key=settings.get("api_key") or None, max_retries=2, timeout=120.0)
