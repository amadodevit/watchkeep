import json
from pathlib import Path

from watchkeep.core.plugin import SyncContext
from watchkeep.plugins.github import GitHubSource, api_to_html, parse_notification, parse_review_search

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, *, headers=None, params=None):
        self.calls.append((url, headers))
        return self.routes[url]


def test_parse_review_search():
    items = parse_review_search(load("review_search.json"))
    first = items[0]
    assert first.id == "review:https://github.com/acme/widgets/pull/42"
    assert first.title == "Fix the wobbly widget"  # stripped
    assert first.repo == "acme/widgets"
    assert first.author == "alice"
    assert first.action == "review requested"


def test_parse_notification_with_comment_links_to_comment():
    thread = load("notifications.json")[0]
    item = parse_notification(thread, load("comment_555.json"))
    assert item.action == "commented"
    assert item.author == "carol"
    assert item.url.endswith("#issuecomment-555")
    assert item.id == "commented:https://github.com/acme/widgets/issues/9"


def test_parse_notification_ignores_other_reasons():
    assert parse_notification(load("notifications.json")[2], None) is None


def test_api_to_html():
    assert api_to_html("https://api.github.com/repos/a/b/pulls/3") == "https://github.com/a/b/pull/3"
    assert api_to_html("https://api.github.com/repos/a/b/issues/3") == "https://github.com/a/b/issues/3"
    assert api_to_html(None) is None


def test_sync_combines_reviews_and_comments_and_sends_token():
    http = FakeHttp(routes())
    items = GitHubSource().sync(SyncContext(settings={"account": "me", "token": "t0k"}, http=http))
    assert [i.action for i in items] == ["review requested", "review requested", "commented", "mentioned"]
    # same PR as review request and mention -> two rows, distinct ids
    assert len({i.id for i in items}) == 4
    assert all(h["Authorization"] == "Bearer t0k" for _, h in http.calls)


def routes(**states):
    return {
        "https://api.github.com/search/issues": load("review_search.json"),
        "https://api.github.com/notifications": load("notifications.json"),
        "https://api.github.com/repos/acme/widgets/issues/comments/555": load("comment_555.json"),
        "https://api.github.com/repos/acme/widgets/issues/9": {"state": states.get("issue9", "open")},
        "https://api.github.com/repos/acme/widgets/pulls/42": {"state": states.get("pr42", "open")},
    }


def sync_with(watch):
    http = FakeHttp(routes())
    items = GitHubSource().sync(SyncContext(settings={"account": "me", "token": "t", "watch": watch}, http=http))
    return items, [url for url, _ in http.calls]


def test_watch_reviews_only_skips_notifications():
    items, calls = sync_with(["review_requests"])
    assert {i.action for i in items} == {"review requested"}
    assert calls == ["https://api.github.com/search/issues"]


def test_watch_mentions_only_skips_search_and_comments():
    items, calls = sync_with(["mentions"])
    assert [i.action for i in items] == ["mentioned"]
    assert "https://api.github.com/search/issues" not in calls


def test_missing_watch_means_everything():
    items, _ = sync_with(None)
    assert {i.action for i in items} == {"review requested", "commented", "mentioned"}


def test_parse_gh_hosts_keeps_working_github_com_logins():
    from watchkeep.plugins.github import parse_gh_hosts

    found = parse_gh_hosts(load("gh_auth_status.json"))
    assert [c.name for c in found] == ["octo-work", "octo-home"]
    assert found[0].settings == {"account": "octo-work", "token": "gh:octo-work"}
    assert "active" in found[0].description


def test_comments_and_mentions_on_closed_subjects_are_hidden():
    http = FakeHttp(routes(pr42="closed"))  # the mentioned PR was merged
    items = GitHubSource().sync(SyncContext(settings={"account": "me", "token": "t", "watch": ["comments", "mentions"]}, http=http))
    assert [i.action for i in items] == ["commented"]


def test_failed_state_lookup_keeps_the_row():
    r = routes()
    del r["https://api.github.com/repos/acme/widgets/pulls/42"]  # FakeHttp raises KeyError
    items = GitHubSource().sync(SyncContext(settings={"account": "me", "token": "t", "watch": ["mentions"]}, http=FakeHttp(r)))
    assert [i.action for i in items] == ["mentioned"]


def test_subject_without_state_is_kept():
    from watchkeep.plugins.github import _subject

    thread = {"subject": {"url": "https://api.github.com/repos/a/b/releases/1"}}
    http = FakeHttp({})
    assert _subject(SyncContext(settings={}, http=http), {}, thread) is None
    assert http.calls == []


# -- PRs with no reviewer -------------------------------------------------------

PULLS = "https://api.github.com/repos/acme/widgets/pulls"


def reviews(url):
    return f"{url.replace('https://github.com/', 'https://api.github.com/repos/').replace('/pull/', '/pulls/')}/reviews"


def unreviewed_routes(pulls):
    routes = {PULLS: pulls}
    for pr in pulls:
        routes[reviews(pr["html_url"])] = [{"id": 1}] if pr["number"] == 56 else []
    return routes


def unreviewed_sync(pulls, memory, repos="acme/widgets", http=None):
    http = http or FakeHttp(unreviewed_routes(pulls))
    ctx = SyncContext(settings={"account": "Me", "token": "t", "watch": ["unreviewed"], "repos": repos}, http=http, memory=memory)
    return GitHubSource().sync(ctx), ctx, http


def test_first_sync_of_a_repo_shows_waiting_prs_quietly():
    memory = {}
    items, ctx, _ = unreviewed_sync(load("pulls_widgets.json"), memory)
    assert [(i.id, i.action, i.title, i.repo, i.author) for i in items] == [
        ("unreviewed:https://github.com/acme/widgets/pull/50", "no reviewer", "Add sprocket cache", "acme/widgets", "dave")
    ]
    assert ctx.quiet == {"unreviewed:https://github.com/acme/widgets/pull/50"}
    assert memory["unreviewed"]["repos"] == ["acme/widgets"]


def test_later_prs_are_not_quiet_and_reviews_are_remembered():
    memory = {"unreviewed": {"repos": ["acme/widgets"]}}
    items, ctx, _ = unreviewed_sync(load("pulls_widgets.json"), memory)
    assert [i.id for i in items] == ["unreviewed:https://github.com/acme/widgets/pull/50"]
    assert ctx.quiet == set()
    # the reviewed PR is remembered, so the next sync doesn't ask again
    assert memory["unreviewed"]["reviewed"] == ["https://github.com/acme/widgets/pull/56"]
    _, _, http = unreviewed_sync(load("pulls_widgets.json"), memory)
    assert reviews("https://github.com/acme/widgets/pull/56") not in [url for url, _ in http.calls]


def test_old_hidden_baseline_is_dropped_so_those_prs_show_quietly():
    memory = {"unreviewed": {"baseline": {"acme/widgets": ["https://github.com/acme/widgets/pull/50"]}}}
    items, ctx, _ = unreviewed_sync(load("pulls_widgets.json"), memory)
    assert "baseline" not in memory["unreviewed"]
    assert [i.id for i in items] == ["unreviewed:https://github.com/acme/widgets/pull/50"]
    assert ctx.quiet == {i.id for i in items}


def test_unwatched_repos_are_forgotten():
    memory = {"unreviewed": {"repos": ["acme/widgets", "acme/old"]}}
    unreviewed_sync([], memory)
    assert memory["unreviewed"]["repos"] == ["acme/widgets"]


def test_failed_repo_listing_warns_and_keeps_it_seen():
    class Failing(FakeHttp):
        def get_json(self, url, *, headers=None, params=None):
            raise RuntimeError("404 Not Found")

    memory = {"unreviewed": {"repos": ["acme/widgets"]}}
    items, ctx, _ = unreviewed_sync([], memory, http=Failing({}))
    assert items == []
    assert ctx.warnings == ["acme/widgets: couldn't list pull requests (404 Not Found)"]
    assert memory["unreviewed"]["repos"] == ["acme/widgets"]


def test_patterns_expand_through_the_repo_list():
    class Paged(FakeHttp):
        def get_json(self, url, *, headers=None, params=None):
            if url.endswith("/user/repos"):
                self.calls.append((url, headers))
                return [{"full_name": "acme/widgets"}, {"full_name": "acme/gears", "archived": True}, {"full_name": "other/x"}]
            return super().get_json(url, headers=headers, params=params)

    memory = {}
    _, _, http = unreviewed_sync(load("pulls_widgets.json"), memory, repos="ACME/*", http=Paged(unreviewed_routes(load("pulls_widgets.json"))))
    assert memory["unreviewed"]["repos"] == ["acme/widgets"]


def test_repo_picker_pages_until_a_short_page():
    class Pages(FakeHttp):
        def get_json(self, url, *, headers=None, params=None):
            self.calls.append((url, params["page"]))
            return [{"full_name": f"o/r{params['page']}-{i}"} for i in range(100 if params["page"] == 1 else 3)]

    http = Pages({})
    names = GitHubSource().options(SyncContext(settings={"account": "me", "token": "t"}, http=http), "repos")
    assert len(names) == 103
    assert [page for _, page in http.calls] == [1, 2]


def test_unreviewed_is_opt_in():
    _, calls = sync_with(None)
    assert PULLS not in calls


# -- my PRs -----------------------------------------------------------------------


class SearchHttp:
    """Answers /search/issues by the qualifier in q (an exception raises), other URLs from `routes`."""

    def __init__(self, by_qualifier, routes=None):
        self.by_qualifier = by_qualifier
        self.routes = routes or {}
        self.queries = []

    def get_json(self, url, *, headers=None, params=None):
        if url != "https://api.github.com/search/issues":
            answer = self.routes.get(url, [])
            if isinstance(answer, Exception):
                raise answer
            return answer
        q = params["q"]
        self.queries.append(q)
        for qualifier, answer in self.by_qualifier.items():
            if q.endswith(qualifier):
                if isinstance(answer, Exception):
                    raise answer
                return {"items": answer}
        return {"items": []}


def test_my_prs_are_off_for_sources_from_before_watch_had_them():
    _, calls = sync_with(["review_requests", "comments", "mentions"])
    assert calls.count("https://api.github.com/search/issues") == 1
    assert next(s for s in GitHubSource.settings if s.key == "watch").choices[3].value == "mine"


def _node(n, *, decision=None, checks="SUCCESS", latest=(), reviews=(), comments=()):
    person = lambda login: {"__typename": "Bot" if login.endswith("[bot]") else "User", "login": login}
    return {
        "url": f"https://github.com/acme/widgets/pull/{n}",
        "title": f" PR {n} ",
        "updatedAt": "2026-01-01T00:00:00Z",
        "reviewDecision": decision,
        "repository": {"nameWithOwner": "acme/widgets"},
        "latestReviews": {"nodes": [{"state": st, "submittedAt": t, "author": person(who)} for who, st, t in latest]},
        "reviews": {"nodes": [{"state": st, "submittedAt": t, "author": person(who)} for who, st, t in reviews]},
        "comments": {"nodes": [{"createdAt": t, "author": person(who)} for who, t in comments]},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks} if checks else None}}]},
    }


class GraphQLHttp:
    def __init__(self, answer):
        self.answer = answer
        self.posts = []

    def get_json(self, url, *, headers=None, params=None):
        assert url == "https://api.github.com/notifications"
        return []

    def post_json(self, url, *, headers=None, json=None):
        self.posts.append((url, json))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def mine(*nodes, answer=None):
    http = GraphQLHttp(answer if answer is not None else {"data": {"search": {"nodes": list(nodes)}}})
    ctx = SyncContext(settings={"account": "Me", "token": "t", "watch": ["mine"]}, http=http)
    return GitHubSource().sync(ctx), ctx, http


def test_my_prs_each_show_once_under_their_most_pressing_state():
    items, ctx, http = mine(
        _node(1, checks="FAILURE", decision="APPROVED", latest=[("ana", "APPROVED", "t1")]),
        _node(2, decision="CHANGES_REQUESTED", latest=[("bob", "CHANGES_REQUESTED", "t1")]),
        _node(3, decision="APPROVED", latest=[("ana", "APPROVED", "t1"), ("dan", "APPROVED", "t2")]),
        _node(4, decision="REVIEW_REQUIRED", latest=[("ana", "APPROVED", "t1")], comments=[("eve", "t3")]),
        _node(5, decision="REVIEW_REQUIRED", reviews=[("carol", "COMMENTED", "t1")], comments=[("dave", "t2")]),
        _node(6, decision="REVIEW_REQUIRED", comments=[("github-actions[bot]", "t1"), ("me", "t2")]),
        _node(7, decision=None, latest=[("ana", "APPROVED", "t1")]),  # repo without required reviews
    )
    assert [(i.id.split(":")[0], i.action, i.author) for i in items] == [
        ("ci", "checks failing", ""),
        ("changes", "changes requested", "bob"),
        ("approved", "approved", "dan"),
        ("partial", "partly approved", "ana"),
        ("talked", "commented", "dave"),
        ("waiting", "waiting for review", ""),  # bots and yourself don't count
        ("approved", "approved", "ana"),
    ]
    assert items[0].title == "PR 1" and items[0].repo == "acme/widgets"
    assert ctx.quiet == {"waiting:https://github.com/acme/widgets/pull/6"}  # just your PR being open: no popup
    url, body = http.posts[0]
    assert url == "https://api.github.com/graphql" and "author:@me" in body["variables"]["q"] and "draft:false" in body["variables"]["q"]


def test_failed_query_warns_and_shows_none():
    items, ctx, _ = mine(answer=RuntimeError("502 Bad Gateway"))
    assert items == [] and ctx.warnings == ["my PRs: query failed (502 Bad Gateway)"]
    items, ctx, _ = mine(answer={"errors": [{"message": "Bad credentials"}]})
    assert items == [] and ctx.warnings == ["my PRs: query failed (Bad credentials)"]


def _thread(reason, n, comment=None):
    return {
        "reason": reason,
        "updated_at": "2026-10-16T10:00:00Z",
        "subject": {
            "title": f"PR {n}",
            "type": "PullRequest",
            "url": f"https://api.github.com/repos/acme/widgets/pulls/{n}",
            "latest_comment_url": comment,
        },
        "repository": {"full_name": "acme/widgets", "html_url": "https://github.com/acme/widgets"},
    }


class NotifHttp(SearchHttp):
    def __init__(self, threads, pulls, comments):
        routes = {"https://api.github.com/notifications": threads}
        for n, (author, state) in pulls.items():
            routes[f"https://api.github.com/repos/acme/widgets/pulls/{n}"] = {"state": state, "user": {"login": author}}
        routes.update(comments)
        super().__init__({}, routes)

    def post_json(self, url, *, headers=None, json=None):
        return {"data": {"search": {"nodes": []}}}


def test_comments_on_my_prs_become_activity_whatever_the_reason():
    c = "https://api.github.com/repos/acme/widgets/issues/comments/7"
    http = NotifHttp(
        [_thread("author", 1, c), _thread("comment", 2), _thread("comment", 3), _thread("author", 4), _thread("mention", 5)],
        {1: ("Me", "open"), 2: ("me", "open"), 3: ("someone", "open"), 4: ("me", "closed"), 5: ("me", "open")},
        {c: {"html_url": "https://github.com/acme/widgets/pull/1#issuecomment-7", "user": {"login": "carol"}}},
    )
    ctx = SyncContext(settings={"account": "me", "token": "t", "watch": ["comments", "mine"]}, http=http)
    items = GitHubSource().sync(ctx)
    assert [(i.id, i.action, i.author) for i in items] == [
        ("activity:https://github.com/acme/widgets/pull/1", "activity on my PR", "carol"),
        ("activity:https://github.com/acme/widgets/pull/2", "activity on my PR", ""),
        ("commented:https://github.com/acme/widgets/pull/3", "commented", ""),  # not mine: a plain comment row
        ("activity:https://github.com/acme/widgets/pull/5", "activity on my PR", ""),  # mentions not watched, but it's mine
    ]
    assert items[0].url.endswith("#issuecomment-7")


def test_without_my_prs_author_notifications_stay_hidden():
    http = NotifHttp([_thread("author", 1), _thread("comment", 2)], {1: ("me", "open"), 2: ("me", "open")}, {})
    items = GitHubSource().sync(SyncContext(settings={"account": "me", "token": "t", "watch": ["comments"]}, http=http))
    assert [(i.action, i.url) for i in items] == [("commented", "https://github.com/acme/widgets/pull/2")]
