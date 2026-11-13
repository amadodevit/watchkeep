"""What the overview shows, as plain rows. Pure: no Textual, so it's easy to test.

    Client › Kind (default)        ▾ Acme                1 failing · 14 ok
                                     ▾ ActiveMQ queues       waiting  consumers  in    out
                                       ▸ Prod broker   1 failing · 5 ok   101  22  +199  +329
                                         ✗ orders      ...
                                         +5 ok

Healthy sources take one line; trouble shows without unfolding anything.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from rich.text import Text

from watchkeep.core.checks import compact
from watchkeep.core.engine import PluginInfo
from watchkeep.core.health import FAILING
from watchkeep.core.models import Appointment, HealthView, SourceView, State
from watchkeep.core.panels import Panel, PanelFilter, is_all, panel_matches

# (name shown on g, grouping levels outer to inner)
PRESETS = (
    ("Client › Kind", ("client", "kind")),
    ("Kind", ("kind",)),
    ("Environment › Kind", ("environment", "kind")),
    ("Flat", ()),
)
VALUE_COLUMNS = 4
ITEM_COLUMNS = ("repo", "by", "updated")
WIDTHS = {"name": 36, "value": (20, 10, 8, 8), "note": 32}
UNSET = {"client": "No client", "environment": "No environment"}


@dataclass
class Row:
    key: str
    cells: list  # name, title, VALUE_COLUMNS values, note
    kind: str  # group | source | health | item | more
    source_id: str = ""
    ref: str = ""  # health key or item id
    url: str = ""


@dataclass
class View:
    """What the user folded or unfolded. UI-only, not persisted."""

    levels: tuple[str, ...] = PRESETS[0][1]
    expanded: dict[str, bool] = field(default_factory=dict)  # source id -> override of the default fold
    folded_groups: set[str] = field(default_factory=set)
    title_width: int = 50
    depth: int = 2  # what +/- last set: grouping levels shown, then sources, then everything


LEVEL_NAMES = {"client": "clients", "kind": "kinds", "environment": "environments"}


def depth_name(levels: tuple[str, ...], depth: int) -> str:
    """ "Showing clients and kinds" for the toast after +/-."""
    shown = [LEVEL_NAMES[level] for level in levels[: depth + 1]] if depth < len(levels) else [LEVEL_NAMES[level] for level in levels]
    if depth == len(levels):
        shown.append("sources")
    if depth > len(levels):
        return "Showing everything"
    return "Showing " + (" and ".join([", ".join(shown[:-1]), shown[-1]]) if len(shown) > 1 else shown[0])


def build(state: State, plugins: dict[str, PluginInfo], view: View, now: Callable[[], float] = time.time) -> list[Row]:
    rows: list[Row] = []
    ctx = _Ctx(plugins, view, now)
    _group(rows, list(state.sources), view.levels, (), ctx)
    return rows


@dataclass
class _Ctx:
    plugins: dict[str, PluginInfo]
    view: View
    now: Callable[[], float]

    def title(self, source: SourceView) -> str:
        info = self.plugins.get(source.plugin)
        return info.title if info else source.plugin

    def columns(self, source: SourceView) -> tuple[str, ...]:
        info = self.plugins.get(source.plugin)
        if info is not None and info.kind == "events":
            return ()  # the Agenda tab has the columns; the overview only counts
        if info is None or info.kind != "samples":
            return ITEM_COLUMNS
        return tuple((m.short or m.label.lower()) for m in info.metrics[:VALUE_COLUMNS])


def _group(rows: list[Row], sources: list[SourceView], levels: tuple[str, ...], path: tuple[str, ...], ctx: _Ctx) -> None:
    if not levels:
        for source in sources:
            _source(rows, source, len(path), ctx)
        return
    level, rest = levels[0], levels[1:]
    groups: dict[str, list[SourceView]] = {}
    for source in sources:
        groups.setdefault(_value(source, level, ctx), []).append(source)
    for name in sorted(groups, key=lambda n: (n in UNSET.values(), n.lower())):
        members = groups[name]
        key = "grp:" + "\x1f".join((*path, name))
        folded = key in ctx.view.folded_groups
        indent = "  " * len(path)
        arrow = "▸" if folded else "▾"
        colour = _colour(members)
        if level == "kind":
            labels = ctx.columns(members[0])
            cells = [Text(f"{indent}{arrow} {name}", style=f"bold {colour}".strip()), Text("")]
            cells += _pad(list(labels))  # column names for this kind
            cells.append(Text(""))
        else:
            cells = [Text(f"{indent}{arrow} {name} ", style=f"bold reverse {colour}".strip()), _counts(members, ctx)]
            cells += [Text("")] * (VALUE_COLUMNS + 1)
        rows.append(Row(key, cells, "group"))
        if not folded:
            _group(rows, members, rest, (*path, name), ctx)


def _value(source: SourceView, level: str, ctx: _Ctx) -> str:
    if level == "kind":
        return ctx.title(source)
    return getattr(source, level) or UNSET[level]


def _source(rows: list[Row], source: SourceView, depth: int, ctx: _Ctx) -> None:
    indent = "  " * depth
    note = _source_note(source, ctx)
    key = f"hdr:{source.id}"

    if len(source.health) == 1 and not source.items:  # e.g. a website: everything on one line
        health = source.health[0]
        cells = [Text(f"{indent}  {source.name}", style="bold"), Text(_cut(health.key, ctx.view.title_width), style="dim")]
        cells += _health_values(health, source, ctx)
        cells.append(_health_note(health, ctx) or note)
        rows.append(Row(key, cells, "source", source.id, health.key, health.url))
        return

    info = ctx.plugins.get(source.plugin)
    if info is not None and info.kind == "events":  # one line; the Agenda tab lists them
        cells = [Text(f"{indent}  {source.name}", style="bold"), _appointment_counts(source, ctx), *[Text("")] * VALUE_COLUMNS, note]
        rows.append(Row(key, cells, "source", source.id))
        return

    expandable = bool(source.items or len(source.health) > 1)
    expanded = ctx.view.expanded.get(source.id, not source.health)  # items open, health folded
    arrow = ("▾ " if expanded else "▸ ") if expandable else "  "
    name = Text(f"{indent}{arrow}{source.name}", style="bold")
    if source.health:
        cells = [name, _health_counts(source), *_aggregate(source, ctx), note]
    else:
        cells = [name, _item_counts(source), *[Text("")] * VALUE_COLUMNS, note]
    rows.append(Row(key, cells, "source", source.id))

    inner = "  " * (depth + 2)
    if source.health:
        ordered = sorted(source.health, key=attention_order)
        shown = ordered if expanded else [h for h in ordered if _needs_you(h)]
        for health in shown:
            cells = [_marker(health, inner), Text(_cut(health.key, ctx.view.title_width))]
            cells += _health_values(health, source, ctx)
            cells.append(_health_note(health, ctx))
            rows.append(Row(f"{source.id}\x1f{health.key}", cells, "health", source.id, health.key, health.url))
        hidden = len(ordered) - len(shown)
        if hidden and shown:
            cells = [Text(f"{inner}+{hidden} ok", style="dim"), Text("enter to show", style="dim")]
            rows.append(Row(f"more:{source.id}", cells + [Text("")] * (VALUE_COLUMNS + 1), "more", source.id))
    elif expanded:
        for view in source.items:
            item = view.item
            label = Text(inner)
            label.append("● " if view.is_new else "  ", style="bold yellow")
            label.append(_cut(item.action, max(8, WIDTHS["name"] - len(inner) - 2)), style="dim")
            values = [Text(_cut(item.repo, WIDTHS["value"][0])), Text(_cut(item.author, WIDTHS["value"][1])), Text(_ago(_iso(item.updated_at), ctx))]
            cells = [label, Text(_cut(item.title, ctx.view.title_width)), *_pad(values), Text("")]
            rows.append(Row(f"{source.id}\x1f{item.id}", cells, "item", source.id, item.id, item.url))


# -- agenda --------------------------------------------------------------------

AGENDA_COLUMNS = ("when", "length", "event", "with", "calendar")
AGENDA_VIEWS = {1: "today", 3: "3 days", 7: "week"}  # days shown -> tab label; keys d, x, w
AGENDA_WIDTHS = {"when": 30, "length": 7, "with": 32, "calendar": 20}  # "event" takes the rest


@dataclass(frozen=True)
class AgendaRow:
    """One appointment placed on the day it is listed under, with times as timestamps."""

    appointment: Appointment
    day: date
    begins: float
    ends: float
    source_id: str = ""
    source_name: str = ""

    @property
    def all_day(self) -> bool:
        return self.appointment.all_day


def agenda_rows(appointments: Iterable[Appointment], now: float, source_id: str = "", source_name: str = "") -> list[AgendaRow]:
    """Appointments from today on, all-day ones first in each day, then by start time.

    Something that started before today (a trip, a late meeting) is listed under today.
    Anything already over before today is left out.
    """
    today = datetime.fromtimestamp(now).astimezone().date()
    midnight = _midnight(today)
    rows = []
    for a in appointments:
        try:
            if a.all_day:
                first, last = date.fromisoformat(a.start[:10]), date.fromisoformat(a.end[:10])
                begins, ends = _midnight(first), _midnight(max(last, first + timedelta(days=1)))
                day = first
            else:
                begins, ends = datetime.fromisoformat(a.start).timestamp(), datetime.fromisoformat(a.end).timestamp()
                day = datetime.fromtimestamp(begins).astimezone().date()
        except ValueError:
            continue  # a plugin sent a date we can't read: skip it rather than break the tab
        if ends <= midnight and begins < midnight:
            continue
        rows.append(AgendaRow(a, max(day, today), begins, ends, source_id, source_name))
    return sorted(rows, key=lambda r: (r.day, not r.all_day, r.begins, r.appointment.title.lower()))


def agenda(
    state: State, plugins: dict[str, PluginInfo], now: Callable[[], float] = time.time, title_width: int = 50, days: int = 1
) -> list[Row]:
    """The Agenda tab: every events source merged, one header per day, `days` days from today.

    Every day in the view gets a header, so a free day reads "nothing on" instead of
    just being missing. Over: dim and struck through. Happening now: bold, "now" in front.
    """
    moment = now()
    today = datetime.fromtimestamp(moment).astimezone().date()
    last = today + timedelta(days=max(1, days))
    sources = [s for s in state.sources if (info := plugins.get(s.plugin)) is not None and info.kind == "events"]
    by_day: dict[date, list[AgendaRow]] = {today + timedelta(days=n): [] for n in range((last - today).days)}
    for s in sources:
        for r in agenda_rows(s.appointments, moment, s.id, s.name):
            if r.day < last:
                by_day[r.day].append(r)
    out: list[Row] = []
    for day, rows in by_day.items():
        out.append(Row(f"day:{day.isoformat()}", [Text(_day_name(day, today), style="bold reverse"), *[Text("")] * 4], "group"))
        if not rows:
            out.append(Row(f"empty:{day.isoformat()}", [Text(""), Text(""), Text("nothing on", style="dim"), Text(""), Text("")], "empty"))
        for r in sorted(rows, key=lambda r: (not r.all_day, r.begins, r.appointment.title.lower())):
            out.append(_appointment_row(r, moment, title_width))
    return out


def _appointment_row(r: AgendaRow, moment: float, title_width: int) -> Row:
    a = r.appointment
    over, running = r.ends <= moment, not r.all_day and r.begins <= moment < r.ends
    style = "dim strike" if over else "bold" if running else ""
    cells = [
        Text(_when_cell(r, running), style="dim strike" if over else "bold green" if running else ""),
        Text(_length(r), style="dim"),
        Text(_cut(a.title, title_width), style=style),
        Text(_cut(_people(a.attendees), AGENDA_WIDTHS["with"]), style="dim"),
        Text(_cut(r.source_name, AGENDA_WIDTHS["calendar"]), style="dim"),
    ]
    return Row(f"ap:{r.source_id}\x1f{a.id}", cells, "appointment", r.source_id, a.id, a.url)


def has_events(state: State, plugins: dict[str, PluginInfo]) -> bool:
    """Whether the Agenda tab should show: at least one events source exists."""
    return any((info := plugins.get(s.plugin)) is not None and info.kind == "events" for s in state.sources)


def _midnight(day: date) -> float:
    return datetime.combine(day, datetime.min.time()).astimezone().timestamp()


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _day_name(day: date, today: date) -> str:
    label = day.strftime("%a %d %b")
    if day == today:
        return f" Today · {label} "
    if day == today + timedelta(days=1):
        return f" Tomorrow · {label} "
    return f" {label} "


def _when_cell(r: AgendaRow, running: bool) -> str:
    if r.all_day:
        last = date.fromisoformat(r.appointment.end[:10]) - timedelta(days=1)
        return "all day" if last <= r.day else f"all day · until {last.strftime('%d %b')}"
    span = f"{_clock(r.begins)}–{_clock(r.ends)}" if r.ends > r.begins else _clock(r.begins)
    return f"now · {span}" if running else span


def _length(r: AgendaRow) -> str:
    seconds = int(r.ends - r.begins)
    if r.all_day:
        days = round(seconds / 86400)
        return f"{days}d" if days > 1 else ""
    hours, minutes = divmod(seconds // 60, 60)
    return " ".join(part for part in (f"{hours}h" if hours else "", f"{minutes}m" if minutes else "") if part)


def _people(attendees: tuple[str, ...]) -> str:
    if len(attendees) <= 2:
        return ", ".join(attendees)
    return f"{', '.join(attendees[:2])} +{len(attendees) - 2}"


# -- panels --------------------------------------------------------------------

# Group level -> PanelFilter field, and the word shown to the user for it.
PANEL_FIELDS = {"client": "clients", "environment": "environments", "kind": "plugins"}
RULE_LABELS = (("clients", "client"), ("environments", "environment"), ("tags", "tag"), ("plugins", "type"))


def sources_in(panel: Panel, state: State) -> State:
    """The state as one panel sees it: its sources, in order, same version. Feed it to `build`."""
    return replace(state, sources=tuple(s for s in state.sources if panel_matches(panel, s)))


def tab_failing_count(panel: Panel, state: State) -> int:
    """Trouble in a panel, for its tab: failing rows nobody acknowledged, plus sources with errors."""
    sources = sources_in(panel, state).sources
    failing = sum(h.state == FAILING and not h.acknowledged for s in sources for h in s.health)
    return failing + sum(bool(s.last_error) for s in sources)


def empty_message(panel: Panel) -> str:
    """What an empty panel says instead of a blank table, e.g. "No sources match client = acme"."""
    if is_all(panel):
        return "No sources yet, press a to add one"
    f = panel.filter
    parts = [f"{label} = {' or '.join(v.strip() for v in values)}" for name, label in RULE_LABELS if (values := getattr(f, name))]
    if f.picks:
        parts.append(f"{len(f.picks)} hand-picked source{'s' if len(f.picks) > 1 else ''}")
    return "No sources match " + ", ".join(parts) if parts else "No sources match: this panel has no rules yet"


def filter_from_group(key: str, levels: tuple[str, ...], plugins: dict[str, PluginInfo]) -> tuple[str, PanelFilter] | None:
    """Name and filter for a panel made from a group header (P), or None if a rule can't say it.

    "No client" and similar can't: an empty value sets no constraint. Kind groups show the
    plugin's title, so it is turned back into the plugin name.
    """
    if not key.startswith("grp:"):
        return None
    path = key.removeprefix("grp:").split("\x1f")
    rules: dict[str, tuple[str, ...]] = {}
    for level, value in zip(levels, path, strict=False):
        if value == UNSET.get(level):
            return None
        if level == "kind":
            value = next((name for name, info in plugins.items() if (info.title or name) == value), value)
        rules[PANEL_FIELDS[level]] = (value,)
    return " › ".join(path), PanelFilter(**rules)


# -- cells ---------------------------------------------------------------------


def _health_values(health: HealthView, source: SourceView, ctx: _Ctx) -> list[Text]:
    info = ctx.plugins.get(source.plugin)
    metrics = info.metrics[:VALUE_COLUMNS] if info else ()
    if not metrics:
        return _pad([Text(_cut(health.summary, WIDTHS["value"][0]))])
    cells = []
    for metric in metrics:
        if metric.counter:
            grown = health.growth.get(metric.key)
            cells.append(Text("" if grown is None else f"+{compact(grown)}"))
        else:
            value = health.values.get(metric.key)
            cells.append(Text("—" if value is None else compact(value)))
    return _pad(cells)


def _aggregate(source: SourceView, ctx: _Ctx) -> list[Text]:
    """Totals across a source's rows: summed numbers and growth."""
    info = ctx.plugins.get(source.plugin)
    metrics = info.metrics[:VALUE_COLUMNS] if info else ()
    cells = []
    for metric in metrics:
        if metric.counter:
            grown = [h.growth[metric.key] for h in source.health if metric.key in h.growth]
            cells.append(Text(f"+{compact(sum(grown))}" if grown else "", style="dim"))
        else:
            values = [h.values.get(metric.key) for h in source.health]
            numbers = [v for v in values if isinstance(v, int | float)]
            cells.append(Text(compact(sum(numbers)) if numbers and len(source.health) > 1 else "", style="dim"))
    return _pad(cells)


def _marker(health: HealthView, indent: str) -> Text:
    text = Text(indent)
    if health.acknowledged:
        text.append("◐", style="bold dark_orange")
    elif health.state == FAILING:
        text.append("✗", style="bold red")
    elif health.fired:
        text.append("•", style="yellow")  # a bad check, not failing (yet)
    return text


def _health_note(health: HealthView, ctx: _Ctx) -> Text:
    fired = ", ".join(health.fired)
    if health.acknowledged:
        status = "acknowledged" if health.state == FAILING else "recovered · c to close"
        return Text(_cut(" · ".join(p for p in (status, fired) if p), WIDTHS["note"]), style="dark_orange")
    if health.state == FAILING:
        age = _ago(health.since, ctx).removesuffix(" ago").replace("just now", "<1m")
        return Text(_cut(" · ".join(p for p in (f"failing {age}", fired) if p), WIDTHS["note"]), style="red")
    if fired:
        return Text(_cut(fired, WIDTHS["note"]), style="yellow")
    return Text("")


def _source_note(source: SourceView, ctx: _Ctx) -> Text:
    if source.syncing:
        return Text("syncing…", style="italic dim")
    if source.last_error:
        return Text(_cut(f"error: {source.last_error}", WIDTHS["note"]), style="bold red")
    if source.warnings:
        return Text(_cut(f"warning: {'; '.join(source.warnings)}", WIDTHS["note"]), style="yellow")
    if source.last_sync is None:
        return Text("never synced", style="dim")
    verb = "checked" if source.health else "synced"
    return Text(f"{verb} {_ago(source.last_sync, ctx)}", style="dim")


def _health_counts(source: SourceView) -> Text:
    failing = sum(h.state == FAILING and not h.acknowledged for h in source.health)
    acknowledged = sum(h.acknowledged for h in source.health)
    ok = len(source.health) - failing - acknowledged
    return _join([(failing, "failing", "bold red"), (acknowledged, "acknowledged", "bold dark_orange"), (ok, "ok", "dim")])


def _item_counts(source: SourceView) -> Text:
    count = len(source.items)
    text = Text(f"{count} to look at" if count else "nothing needs you", style="" if count else "dim")
    if source.ignored:
        text.append(f" · {source.ignored} ignored", style="dim")
    if source.read:
        text.append(f" · {source.read} read", style="dim")
    return text


def _appointment_counts(source: SourceView, ctx: _Ctx) -> Text:
    """ "3 events today · next 14:00 Standup", so you can see the calendar is being read."""
    today = datetime.fromtimestamp(ctx.now()).astimezone().date()
    rows = [r for r in agenda_rows(source.appointments, ctx.now()) if r.day == today]
    text = Text(f"{len(rows)} event{'s' if len(rows) != 1 else ''} today", style="" if rows else "dim")
    upcoming = next((r for r in rows if not r.all_day and r.begins > ctx.now()), None)
    if upcoming is not None:
        text.append(f" · next {_clock(upcoming.begins)} {upcoming.appointment.title}", style="dim")
    return text


def _counts(sources: Iterable[SourceView], ctx: _Ctx) -> Text:
    sources = list(sources)
    health = [h for s in sources for h in s.health]
    failing = sum(h.state == FAILING and not h.acknowledged for h in health)
    acknowledged = sum(h.acknowledged for h in health)
    errors = sum(bool(s.last_error) for s in sources)
    items = sum(len(s.items) for s in sources)
    return _join([
        (errors, "with errors", "bold red"),
        (failing, "failing", "bold red"),
        (acknowledged, "acknowledged", "bold dark_orange"),
        (items, "to look at", ""),
        (len(health) - failing - acknowledged, "ok", "dim"),
    ])


def _join(parts: list[tuple[int, str, str]]) -> Text:
    text = Text()
    for count, label, style in parts:
        if count:
            if text:
                text.append(" · ", style="dim")
            text.append(f"{count} {label}", style=style)
    return text


def _colour(sources: Iterable[SourceView]) -> str:
    """Red needs you, orange is in hand, nothing is fine."""
    sources = list(sources)
    if any(s.last_error for s in sources) or any(h.state == FAILING and not h.acknowledged for s in sources for h in s.health):
        return "red"
    if any(h.acknowledged for s in sources for h in s.health):
        return "dark_orange"
    return ""


def attention_order(health: HealthView) -> tuple[int, str]:
    """Failing first, then acknowledged or recovered-but-open, then bad checks, then OK."""
    if health.state == FAILING and not health.acknowledged:
        return 0, health.key
    if health.acknowledged:
        return 1, health.key
    return (2 if health.fired else 3), health.key


def _needs_you(health: HealthView) -> bool:
    return health.state == FAILING or health.acknowledged


def _pad(cells: list) -> list:
    cells = [c if isinstance(c, Text) else Text(c, style="dim italic") for c in cells]
    return (cells + [Text("")] * VALUE_COLUMNS)[:VALUE_COLUMNS]


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _iso(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _ago(ts: float | None, ctx: _Ctx) -> str:
    if ts is None:
        return ""
    seconds = max(0, int(ctx.now() - ts))
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"
