"""Plain data that crosses the core/UI line. Everything here must stay JSON-friendly."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Item:
    """One thing a source reports, e.g. a review request.

    `id` identifies the item inside its source across snapshots. `updated_at`
    tells the core whether an item it already knows has moved.
    """

    id: str
    action: str
    title: str
    url: str
    repo: str
    author: str
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Item:
        return cls(**data)


@dataclass(frozen=True)
class Sample:
    """One measurement of something that is always there, e.g. a website.

    `key` names the row (a URL, a queue). `ok` is whether this single check
    passed the user's criteria; the core judges what that means over time.
    """

    key: str
    ok: bool
    summary: str  # short, human: "200 · 84 ms", "timeout after 10s"
    values: dict  # raw numbers, e.g. {"status": 200, "response_ms": 84}
    url: str = ""  # what Enter opens, if anything


@dataclass(frozen=True)
class Appointment:
    """One calendar entry an events source reports, e.g. a meeting today.

    Named Appointment, not Event: `Event` is a notification (core/notify.py).
    `id` must be unique per occurrence, so a weekly meeting gets one id per week.
    `start` and `end` are ISO 8601: a datetime with its UTC offset, or a plain
    date ("2026-11-13") when `all_day`, with `end` exclusive as in iCalendar.
    """

    id: str
    title: str
    start: str
    end: str
    all_day: bool = False
    description: str = ""
    attendees: tuple[str, ...] = ()
    url: str = ""  # what Enter opens, if anything

    def to_dict(self) -> dict:
        data = asdict(self)
        data["attendees"] = list(self.attendees)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Appointment:
        return cls(**{**data, "attendees": tuple(data.get("attendees", ()))})


@dataclass(frozen=True)
class HealthView:
    key: str
    state: str  # "ok" | "failing"
    since: float
    summary: str  # latest check
    failures: int  # failed checks in the current window
    window: int
    checked_at: float
    url: str
    acknowledged: bool = False
    values: dict = field(default_factory=dict)  # latest raw numbers, keyed by Metric.key
    growth: dict = field(default_factory=dict)  # counters: growth since the previous check
    fired: tuple[str, ...] = ()  # labels of the checks firing now


@dataclass(frozen=True)
class ItemView:
    item: Item
    first_seen: float
    is_new: bool  # appeared in the most recent sync


@dataclass(frozen=True)
class SourceView:
    id: str
    plugin: str
    name: str
    tags: tuple[str, ...]
    interval: int
    last_sync: float | None
    last_error: str | None
    syncing: bool
    items: tuple[ItemView, ...]
    health: tuple[HealthView, ...] = ()  # for sample-based sources
    ignored: int = 0  # items hidden by the user
    read: int = 0  # items still reported but hidden because the user marked them read (not counted in `ignored`)
    client: str = ""
    environment: str = ""
    warnings: tuple[str, ...] = ()  # partial failures from the last successful sync, not an error
    appointments: tuple[Appointment, ...] = ()  # for events sources, as last reported


@dataclass(frozen=True)
class ArchiveEntry:
    """One item from the history of an items source, read or not."""

    source_id: str
    source_name: str
    plugin: str
    item: Item  # as last reported
    first_seen: float
    read_at: float | None  # None while unread
    present: bool  # still in the source's current snapshot


@dataclass(frozen=True)
class State:
    # Bumped on every change, so a UI can skip redraws when nothing moved.
    version: int
    sources: tuple[SourceView, ...]
