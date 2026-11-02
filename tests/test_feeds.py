from pathlib import Path

import httpx
import pytest

from watchkeep.core.plugin import SourcePlugin, SyncContext
from watchkeep.plugins import feeds
from watchkeep.plugins.feeds import FeedsSource, parse_feed, parse_urls

FIXTURES = Path(__file__).parent / "fixtures"
RSS = (FIXTURES / "feed_rss.xml").read_bytes()
ATOM = (FIXTURES / "feed_atom.xml").read_bytes()
BLOG, NOTES = "https://blog.example.com/feed", "https://notes.example.org/atom"


class FakeHttp:
    """Serves feeds by URL; answers 304 when the client sends back the ETag it was given."""

    def __init__(self, pages):
        self.pages = dict(pages)  # url -> bytes, or an exception to raise
        self.calls = []

    def get(self, url, *, headers=None):
        self.calls.append((url, headers or {}))
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        etag = f'"{hash(page)}"'
        status = 304 if (headers or {}).get("If-None-Match") == etag else 200
        return httpx.Response(status, content=b"" if status == 304 else page, headers={"etag": etag})


class FakeJudge:
    def __init__(self, picks=None, fail=False):
        self.picks = picks or {}  # title -> reason
        self.fail = fail
        self.calls = []

    def __call__(self, client, model, interests, posts):
        self.calls.append([p.title for p in posts])
        if self.fail:
            raise RuntimeError("model unavailable")
        return {n: (p.title in self.picks, self.picks.get(p.title, "off topic")) for n, p in enumerate(posts)}


@pytest.fixture
def judge(monkeypatch):
    fake = FakeJudge({"Agents and code review": "about AI agents in code review"})
    monkeypatch.setattr(feeds, "judge", fake)
    monkeypatch.setattr(feeds, "_client", lambda settings: object())
    return fake


def ctx(http, memory, urls=(BLOG, NOTES)):
    settings = {"feeds": " ".join(urls), "interests": "AI agents, software design"}
    return SyncContext(settings=settings, http=http, memory=memory)


def test_satisfies_the_contract():
    assert isinstance(FeedsSource(), SourcePlugin)


def test_parses_rss_into_plain_posts():
    site, posts = parse_feed(RSS)
    assert site == "Example Engineering"
    first = posts[0]
    assert first.id == "https://blog.example.com/retries/"
    assert first.title == "Retries without tears"
    assert first.text == "How we stopped retry storms in our queue consumers."
    assert first.published == "2026-10-24T18:00:00+00:00"
    assert first.author == "Ana" and first.site == "Example Engineering"


def test_parses_atom():
    site, [post] = parse_feed(ATOM)
    assert site == "Notes from Bea"
    assert post.id == "tag:notes.example.org,2026:post-7"
    assert post.url == "https://notes.example.org/agents-review"
    assert post.published == "2026-10-21T10:51:00+00:00"
    assert post.author == "Bea" and post.text == "What changes when an agent writes the diff."


def test_rejects_what_is_not_a_feed():
    with pytest.raises(ValueError, match="not a feed"):
        parse_feed(b"<html><body>hi")
    with pytest.raises(ValueError, match="not RSS or Atom"):
        parse_feed(b"<html><body>hi</body></html>")


def test_feed_urls_are_split_and_checked():
    assert parse_urls(f"{BLOG}, {NOTES}\n{BLOG}") == [BLOG, NOTES]
    with pytest.raises(ValueError, match="not a URL"):
        parse_urls("blog.example.com/feed")


def test_first_sync_only_remembers_what_is_there(judge):
    memory = {}
    assert FeedsSource().sync(ctx(FakeHttp({BLOG: RSS, NOTES: b"<feed xmlns='http://www.w3.org/2005/Atom'><title>Notes from Bea</title></feed>"}), memory)) == []
    assert judge.calls == []
    assert memory["feeds"][BLOG]["seen"] == ["https://blog.example.com/retries/", "https://blog.example.com/plants/"]


def test_new_posts_go_to_the_model_once_and_matches_become_items(judge):
    memory = {}
    empty_atom = b"<feed xmlns='http://www.w3.org/2005/Atom'><title>Notes from Bea</title></feed>"
    FeedsSource().sync(ctx(FakeHttp({BLOG: RSS, NOTES: empty_atom}), memory))

    [item] = FeedsSource().sync(ctx(FakeHttp({BLOG: RSS, NOTES: ATOM}), memory))
    assert judge.calls == [["Agents and code review"]]  # the RSS posts were already seen
    assert (item.title, item.repo, item.author, item.updated_at) == (
        "Agents and code review", "Notes from Bea", "Bea", "2026-10-21T10:51:00+00:00"
    )
    assert item.action == "about AI agents in code review"  # the row label and popup title

    again = FeedsSource().sync(ctx(FakeHttp({BLOG: RSS, NOTES: ATOM}), memory))
    assert [i.id for i in again] == [item.id] and len(judge.calls) == 1  # not judged twice


def test_unchanged_feeds_are_not_downloaded_again(judge):
    memory, http = {}, FakeHttp({BLOG: RSS})
    FeedsSource().sync(ctx(http, memory, urls=(BLOG,)))
    FeedsSource().sync(ctx(http, memory, urls=(BLOG,)))
    assert "If-None-Match" not in http.calls[0][1]
    assert http.calls[1][1]["If-None-Match"] == memory["feeds"][BLOG]["etag"]


def test_a_failed_model_call_raises_so_the_posts_are_judged_next_time(judge):
    memory = {"feeds": {NOTES: {"seen": []}}}
    judge.fail = True
    with pytest.raises(RuntimeError, match="model unavailable"):
        FeedsSource().sync(ctx(FakeHttp({NOTES: ATOM}), memory, urls=(NOTES,)))
    # the core throws this memory away when sync raises; with the old one the post is still new


def test_one_broken_feed_warns_and_the_rest_still_sync(judge):
    memory = {"feeds": {NOTES: {"seen": []}}}
    http = FakeHttp({BLOG: httpx.ConnectTimeout("slow"), NOTES: ATOM})
    c = ctx(http, memory)
    assert [i.title for i in FeedsSource().sync(c)] == ["Agents and code review"]
    assert c.warnings == ["blog.example.com: timeout"]
    assert BLOG not in memory["feeds"]  # never seen, so its first good sync is a baseline


def test_all_feeds_broken_is_an_error(judge):
    request = httpx.Request("GET", BLOG)
    http = FakeHttp({BLOG: httpx.HTTPStatusError("gone", request=request, response=httpx.Response(404, request=request))})
    with pytest.raises(RuntimeError, match="blog.example.com: HTTP 404"):
        FeedsSource().sync(ctx(http, {}, urls=(BLOG,)))


def test_matches_outlive_the_feed_until_they_expire(judge, monkeypatch):
    memory = {"feeds": {NOTES: {"seen": []}}}
    monkeypatch.setattr(feeds.time, "time", lambda: 1_000_000.0)
    FeedsSource().sync(ctx(FakeHttp({NOTES: ATOM}), memory, urls=(NOTES,)))
    gone = b"<feed xmlns='http://www.w3.org/2005/Atom'><title>Notes from Bea</title></feed>"
    assert len(FeedsSource().sync(ctx(FakeHttp({NOTES: gone}), memory, urls=(NOTES,)))) == 1
    monkeypatch.setattr(feeds.time, "time", lambda: 1_000_000.0 + feeds.KEEP_DAYS * 86400 + 1)
    assert FeedsSource().sync(ctx(FakeHttp({NOTES: gone}), memory, urls=(NOTES,))) == []


def test_removed_feeds_are_forgotten(judge):
    memory = {}
    FeedsSource().sync(ctx(FakeHttp({BLOG: RSS, NOTES: ATOM}), memory))
    FeedsSource().sync(ctx(FakeHttp({BLOG: RSS}), memory, urls=(BLOG,)))
    assert list(memory["feeds"]) == [BLOG]


def test_judge_sends_interests_and_numbered_posts_and_reads_the_json():
    class Block:
        type, text = "text", '{"verdicts": [{"n": 0, "match": false, "reason": "retries"}, {"n": 1, "match": true, "reason": "agents"}]}'

    class Response:
        stop_reason, content = "end_turn", [Block()]

    class Messages:
        def create(self, **kwargs):
            self.kwargs = kwargs
            return Response()

    class Client:
        messages = Messages()

    _, rss_posts = parse_feed(RSS)
    _, atom_posts = parse_feed(ATOM)
    client = Client()
    assert feeds.judge(client, "claude-haiku-5-5", "AI agents", rss_posts[:1] + atom_posts) == {
        0: (False, "retries"), 1: (True, "agents")
    }
    sent = client.messages.kwargs
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    prompt = sent["messages"][0]["content"]
    assert "<interests>\nAI agents\n</interests>" in prompt
    assert "[0] Retries without tears (Example Engineering)" in prompt
    assert "[1] Agents and code review (Notes from Bea)" in prompt


def test_rejected_posts_are_kept_with_the_reason_until_they_expire(judge, monkeypatch):
    memory = {"feeds": {BLOG: {"seen": []}}}
    monkeypatch.setattr(feeds.time, "time", lambda: 1_000_000.0)
    assert FeedsSource().sync(ctx(FakeHttp({BLOG: RSS}), memory, urls=(BLOG,))) == []
    rejected = memory["rejected"]["https://blog.example.com/retries/"]
    assert (rejected["title"], rejected["reason"], rejected["judged"]) == ("Retries without tears", "off topic", 1_000_000.0)
    monkeypatch.setattr(feeds.time, "time", lambda: 1_000_000.0 + feeds.KEEP_DAYS * 86400 + 1)
    FeedsSource().sync(ctx(FakeHttp({BLOG: RSS}), memory, urls=(BLOG,)))
    assert memory["rejected"] == {}


def test_a_post_the_model_skips_counts_as_rejected(monkeypatch):
    monkeypatch.setattr(feeds, "_client", lambda settings: object())
    monkeypatch.setattr(feeds, "judge", lambda *a: {1: (True, "agents")})
    _, posts = parse_feed(RSS)
    picked, turned_down = feeds.pick(posts, "AI agents", {})
    assert list(picked.values()) == ["agents"] and list(turned_down.values()) == [feeds.NO_VERDICT]
