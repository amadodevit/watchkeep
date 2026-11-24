"""GitHub source: open review requests, unread comments and mentions, PRs nobody was asked to review,
and the account's own PRs that are waiting on it.

Review requests come from search (a true snapshot: once you review, the PR drops
out). Comments and mentions come from unread notifications (they drop out when
marked read on GitHub).

"No reviewer" rows catch PRs a teammate opened in a picked repo and forgot to ask
anyone to review. Search can't filter on "no reviewer requested", so each repo's
open PRs are listed and filtered here. A PR counts while it has no requested
reviewer, no assignee and no review ever submitted (GitHub empties the requested
list once a reviewer submits, so the review history is what tells "forgotten"
from "done"). Drafts and the account's own PRs are skipped. The PRs already
waiting when a repo is first watched show without a popup (ctx.quiet): those are
often the forgotten ones. Only PRs opened later notify.

"My PRs" lists every open, non-draft PR of the account, one row each, from a
single GraphQL query (search plus reviews, last comments and the checks rollup).
The row's action is its most pressing state: checks failing, changes requested,
approved, partly approved (approvals, but the repo wants more), commented (someone
else reviewed or commented), or waiting for review (nobody has yet). `author` is
who put it there: the reviewer, or the last person to comment or review. Bots
don't count as interaction. The state is part of the id, so a PR moving from
"changes requested" to "approved" shows (and notifies) as new; "waiting for
review" rows never pop up, since they are just your own PRs being open.

With "My PRs" on, unread notifications on the account's own open PRs (someone
commented or reviewed) become "activity on my PR" rows, whatever GitHub gives as
the reason ("author", or "comment"/"mention" once you've joined in), instead of
plain comment/mention rows. They sit next to the state row of the same PR, so a
UI can show "approved by ana, new comment from carla" as one line.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
from typing import Any

from watchkeep.core.models import Item
from watchkeep.core.plugin import Candidate, Choice, Setting, SyncContext

API = "https://api.github.com"
REVIEW_QUERY = "is:pr is:open review-requested:@me archived:false"
NOTIFICATION_ACTIONS = {"comment": "commented", "mention": "mentioned", "team_mention": "mentioned"}
# notification reason -> the "watch" choice that enables it
REASON_WATCH = {"comment": "comments", "mention": "mentions", "team_mention": "mentions"}
WATCH_ALL = ("review_requests", "comments", "mentions")  # sources from before "watch" existed; the rest are opt-in
MINE_QUERY = "is:pr is:open author:@me archived:false draft:false"
MINE_GRAPHQL = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 50) {
    nodes {
      ... on PullRequest {
        url title updatedAt reviewDecision
        repository { nameWithOwner }
        latestReviews(first: 30) { nodes { state submittedAt author { __typename login } } }
        reviews(last: 20) { nodes { state submittedAt author { __typename login } } }
        comments(last: 20) { nodes { createdAt author { __typename login } } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}
"""
# id prefix -> action, most pressing first
MINE_ACTIONS = {
    "ci": "checks failing",
    "changes": "changes requested",
    "approved": "approved",
    "partial": "partly approved",
    "talked": "commented",
    "waiting": "waiting for review",
}
MY_ACTIVITY = "activity on my PR"
# reasons a notification on your own PR can come with
MINE_REASONS = {"author", "comment", "mention", "team_mention"}
MAX_REPO_PAGES = 10  # the repo picker stops at 1 000 repos


class GitHubSource:
    name = "github"
    title = "GitHub"
    connection = "http"
    settings = (
        Setting("account", "GitHub username", help="The login this source watches, e.g. octocat"),
        Setting(
            "token",
            "Token",
            secret=True,
            help="gh:<user> reads it from the GitHub CLI (nothing stored); or paste a token; or env:VAR",
        ),
        Setting(
            "watch",
            "Watch",
            choices=(
                Choice("review_requests", "Review requests"),
                Choice("comments", "Comments"),
                Choice("mentions", "Mentions"),
                Choice("mine", "My PRs: checks failing, changes requested, approved"),
                Choice("unreviewed", "PRs with no reviewer (in the repos below)", default=False),
            ),
        ),
        Setting(
            "repos",
            "Repos to watch for PRs with no reviewer",
            required=False,
            fetch=True,
            help="owner/name or patterns, comma separated, e.g. acme/*, acme/widgets",
        ),
    )
    identity = ("account",)

    def detect(self) -> list[Candidate]:
        if not shutil.which("gh"):
            return []
        result = subprocess.run(["gh", "auth", "status", "--json", "hosts"], capture_output=True, text=True, timeout=10)
        try:
            return parse_gh_hosts(json.loads(result.stdout))
        except (json.JSONDecodeError, AttributeError):
            return []  # older gh without --json, or not logged in

    def sync(self, ctx: SyncContext) -> list[Item]:
        watch = set(ctx.settings.get("watch") or WATCH_ALL)  # sources from before "watch" existed get everything
        headers = _headers(ctx.settings["token"])
        items: list[Item] = []
        if "review_requests" in watch:
            search = ctx.http.get_json(f"{API}/search/issues", headers=headers, params={"q": REVIEW_QUERY, "per_page": 100})
            items += parse_review_search(search)

        if "mine" in watch:
            items += self._mine(ctx, headers)

        if "unreviewed" in watch:
            items += self._unreviewed(ctx, headers)

        mine_on = "mine" in watch
        if not watch & {"comments", "mentions"} and not mine_on:
            return items
        me = str(ctx.settings.get("account", "")).lower()
        notifications = ctx.http.get_json(f"{API}/notifications", headers=headers, params={"per_page": 50})
        for thread in notifications:
            reason = thread.get("reason", "")
            wanted = REASON_WATCH.get(reason) in watch
            if not wanted and not (mine_on and reason in MINE_REASONS):
                continue
            subject = _subject(ctx, headers, thread)
            if subject and subject.get("state") == "closed":
                continue  # merged/closed: nothing left to act on, even if the notification is unread
            pr_author = ((subject or {}).get("user") or {}).get("login", "").lower()
            on_mine = mine_on and bool(me) and thread["subject"].get("type") == "PullRequest" and pr_author == me
            if not (on_mine or wanted):
                continue
            comment = None
            if comment_url := thread["subject"].get("latest_comment_url"):
                try:
                    comment = ctx.http.get_json(comment_url, headers=headers)
                except Exception:
                    comment = None  # still show the row, just without author/deep link
            if item := (parse_my_activity(thread, comment) if on_mine else parse_notification(thread, comment)):
                items.append(item)
        return items

    def options(self, ctx: SyncContext, key: str) -> list[str]:
        if key != "repos":
            return []
        headers = _headers(ctx.settings["token"])
        names: list[str] = []
        for page in range(1, MAX_REPO_PAGES + 1):
            batch = ctx.http.get_json(f"{API}/user/repos", headers=headers, params={"per_page": 100, "page": page, "sort": "full_name"})
            names += [r["full_name"] for r in batch if not r.get("archived")]
            if len(batch) < 100:
                break
        return sorted(names, key=str.lower)

    def _mine(self, ctx: SyncContext, headers: dict[str, str]) -> list[Item]:
        try:
            answer = ctx.http.post_json(f"{API}/graphql", headers=headers, json={"query": MINE_GRAPHQL, "variables": {"q": MINE_QUERY}})
            if answer.get("errors") and not answer.get("data"):
                raise RuntimeError(answer["errors"][0].get("message", "GraphQL error"))
            nodes = answer["data"]["search"]["nodes"]
        except Exception as exc:
            ctx.warnings.append(f"my PRs: query failed ({_short(exc)})")
            return []
        me = str(ctx.settings.get("account", "")).lower()
        items = [parse_mine(pr, me) for pr in nodes if pr and pr.get("url")]
        ctx.quiet.update(i.id for i in items if i.id.startswith("waiting:"))
        return items

    def _unreviewed(self, ctx: SyncContext, headers: dict[str, str]) -> list[Item]:
        patterns = _split(ctx.settings.get("repos"))
        if not patterns:
            return []
        literal = [p for p in patterns if not any(ch in p for ch in "*?[")]
        repos, listed_all = literal, True
        if len(literal) < len(patterns):
            try:
                repos = sorted(set(literal) | set(_matching(self.options(ctx, "repos"), patterns)))
            except Exception as exc:
                listed_all = False  # can't tell which repos the patterns cover: forget none
                ctx.warnings.append(f"couldn't list repos for the patterns ({_short(exc)})")

        me = str(ctx.settings.get("account", "")).lower()
        memory = ctx.memory.setdefault("unreviewed", {})
        memory.pop("baseline", None)  # older versions hid PRs that were already open; now they show, quietly
        seen: list[str] = memory.setdefault("repos", [])  # repos synced at least once
        reviewed: list[str] = memory.setdefault("reviewed", [])  # PR urls known to have a review
        items: list[Item] = []
        open_urls: set[str] = set()
        listed: set[str] = set()
        for repo in repos:
            try:
                pulls = ctx.http.get_json(f"{API}/repos/{repo}/pulls", headers=headers, params={"state": "open", "per_page": 100})
            except Exception as exc:
                ctx.warnings.append(f"{repo}: couldn't list pull requests ({_short(exc)})")
                continue
            listed.add(repo)
            open_urls |= {pr["html_url"] for pr in pulls}
            first_sight = repo not in seen
            for pr in pulls:
                url = pr["html_url"]
                if not _waiting_for_reviewer(pr, me) or url in reviewed:
                    continue
                try:
                    has_review = bool(ctx.http.get_json(f"{API}/repos/{repo}/pulls/{pr['number']}/reviews", headers=headers, params={"per_page": 1}))
                except Exception:
                    has_review = False  # better a stale row than a hidden one
                if has_review:
                    reviewed.append(url)
                    continue
                item = Item(
                    id=f"unreviewed:{url}",
                    action="no reviewer",
                    title=pr["title"].strip(),
                    url=url,
                    repo=repo,
                    author=pr["user"]["login"],
                    updated_at=pr["updated_at"],
                )
                items.append(item)
                if first_sight:
                    ctx.quiet.add(item.id)  # already waiting when the repo was added: show, don't pop up
        # forget repos no longer watched and PRs that closed, so memory doesn't grow forever
        # (only for repos listed this time: a repo that failed stays "seen")
        seen[:] = [r for r in seen if r in repos or not listed_all] + sorted(listed - set(seen))
        reviewed[:] = [u for u in reviewed if u in open_urls or _repo_of(u) not in listed]
        return items

    def test_connection(self, ctx: SyncContext) -> str:
        user = ctx.http.get_json(f"{API}/user", headers=_headers(ctx.settings["token"]))
        login = user["login"]
        expected = ctx.settings["account"]
        if login.lower() != expected.lower():
            return f"authenticated as {login}, but config says {expected}"
        return f"authenticated as {login}"


def parse_gh_hosts(data: dict[str, Any]) -> list[Candidate]:
    candidates = []
    for account in data.get("hosts", {}).get("github.com", []):
        if account.get("state") != "success" or not account.get("login"):
            continue
        login = account["login"]
        candidates.append(
            Candidate(
                name=login,
                settings={"account": login, "token": f"gh:{login}"},
                description="logged in with gh" + (" (active)" if account.get("active") else ""),
            )
        )
    return candidates


def _subject(ctx: SyncContext, headers: dict[str, str], thread: dict[str, Any]) -> dict[str, Any] | None:
    """The thread's PR or issue as the API gives it (state, author), or None.

    Subjects without a state (discussions, releases, commits) and failed lookups
    give None, which callers treat as open: better a stale row than a silently
    hidden one.
    """
    url = thread["subject"].get("url") or ""
    if not re.search(r"/(pulls|issues)/\d+$", url):
        return None
    try:
        data = ctx.http.get_json(url, headers=headers)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _waiting_for_reviewer(pr: dict[str, Any], me: str) -> bool:
    """Open, not a draft, not mine, nobody asked to review and nobody assigned. Reviews are checked separately."""
    return (
        not pr.get("draft")
        and (pr.get("user") or {}).get("login", "").lower() != me
        and not pr.get("requested_reviewers")
        and not pr.get("requested_teams")
        and not pr.get("assignees")
        and not pr.get("assignee")
    )


def _repo_of(html_url: str) -> str:
    return "/".join(html_url.removeprefix("https://github.com/").split("/")[:2])


def _matching(names: list[str], patterns: list[str]) -> list[str]:
    return [n for n in names if any(fnmatch.fnmatch(n.lower(), p.lower()) for p in patterns)]


def _split(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _short(exc: Exception) -> str:
    return (str(exc) or type(exc).__name__).splitlines()[0][:80]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def parse_review_search(data: dict[str, Any]) -> list[Item]:
    items = []
    for pr in data.get("items", []):
        url = pr["html_url"]
        items.append(
            Item(
                id=f"review:{url}",
                action="review requested",
                title=pr["title"].strip(),
                url=url,
                repo=_repo_from_api(pr["repository_url"]),
                author=pr["user"]["login"],
                updated_at=pr["updated_at"],
            )
        )
    return items


def _people(nodes: Any, me: str, when: str) -> list[tuple[str, str, str]]:
    """(time, login, state) of what others did, oldest first. Bots and the account itself are left out."""
    out = []
    for n in nodes or []:
        author = (n or {}).get("author") or {}
        login = author.get("login") or ""
        if not login or login.lower() == me or author.get("__typename") == "Bot":
            continue
        out.append((n.get(when) or "", login, n.get("state") or ""))
    return sorted(out)


def parse_mine(pr: dict[str, Any], me: str) -> Item:
    """One of your open PRs (a GraphQL node), under its most pressing state."""
    latest = _people((pr.get("latestReviews") or {}).get("nodes"), me, "submittedAt")  # one per reviewer
    talk = _people((pr.get("reviews") or {}).get("nodes"), me, "submittedAt")
    talk += _people((pr.get("comments") or {}).get("nodes"), me, "createdAt")
    talk = [t for t in sorted(talk) if t[2] != "PENDING"]
    approvals = [t for t in latest if t[2] == "APPROVED"]
    changes = [t for t in latest if t[2] == "CHANGES_REQUESTED"]
    commits = (pr.get("commits") or {}).get("nodes") or [{}]
    checks = (((commits[-1] or {}).get("commit") or {}).get("statusCheckRollup") or {}).get("state")
    decision = pr.get("reviewDecision")

    who = ""
    if checks in ("FAILURE", "ERROR"):
        key = "ci"
    elif decision == "CHANGES_REQUESTED" or changes:
        key, who = "changes", (changes[-1][1] if changes else "")
    elif approvals and decision in ("APPROVED", None):  # None: the repo doesn't require reviews
        key, who = "approved", approvals[-1][1]
    elif approvals:
        key, who = "partial", approvals[-1][1]
    elif talk:
        key = "talked"
    else:
        key = "waiting"
    if not who and talk:
        who = talk[-1][1]
    return Item(
        id=f"{key}:{pr['url']}",
        action=MINE_ACTIONS[key],
        title=(pr.get("title") or "").strip(),
        url=pr["url"],
        repo=(pr.get("repository") or {}).get("nameWithOwner", ""),
        author=who,
        updated_at=pr.get("updatedAt") or "",
    )


def parse_my_activity(thread: dict[str, Any], comment: dict[str, Any] | None) -> Item | None:
    """Someone commented on or reviewed one of your PRs. The id is the PR, so one row per PR."""
    subject = thread["subject"]
    pr_url = api_to_html(subject.get("url"))
    if not pr_url:
        return None
    return Item(
        id=f"activity:{pr_url}",
        action=MY_ACTIVITY,
        title=subject["title"].strip(),
        url=(comment or {}).get("html_url") or pr_url,
        repo=thread["repository"]["full_name"],
        author=((comment or {}).get("user") or {}).get("login", ""),
        updated_at=thread["updated_at"],
    )


def parse_notification(thread: dict[str, Any], comment: dict[str, Any] | None) -> Item | None:
    action = NOTIFICATION_ACTIONS.get(thread.get("reason", ""))
    if action is None:
        return None
    subject = thread["subject"]
    subject_url = api_to_html(subject.get("url")) or thread["repository"]["html_url"]
    link = (comment or {}).get("html_url") or subject_url
    return Item(
        id=f"{action}:{subject_url}",
        action=action,
        title=subject["title"].strip(),
        url=link,
        repo=thread["repository"]["full_name"],
        author=((comment or {}).get("user") or {}).get("login", ""),
        updated_at=thread["updated_at"],
    )


_API_PATH = re.compile(r"^https://api\.github\.com/repos/([^/]+/[^/]+)/(pulls|issues|commits)/(.+)$")


def api_to_html(url: str | None) -> str | None:
    if not url:
        return None
    match = _API_PATH.match(url)
    if not match:
        return None
    repo, kind, rest = match.groups()
    kind = {"pulls": "pull", "commits": "commit"}.get(kind, kind)
    return f"https://github.com/{repo}/{kind}/{rest}"


def _repo_from_api(url: str) -> str:
    return url.removeprefix(f"{API}/repos/")
