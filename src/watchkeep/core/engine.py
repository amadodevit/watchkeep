"""The core. The UI talks to it only through the public methods below.

Runs in-process for now. The interface is kept small and takes/returns plain
data so it can later sit behind a local HTTP server without changing plugins
or storage.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from . import checks as checklib
from . import panels as panellib
from .checks import Check
from .config import DEFAULT_INTERVAL, SourceConfig
from .connections import CONNECTIONS, resolve_secret
from .diff import diff_snapshot
from .health import FAILING, Health, Rule, step
from .models import ArchiveEntry, HealthView, Item, ItemView, Sample, SourceView, State
from .notify import FAILING as FAILING_EVENT
from .notify import NEW_ITEM, RECOVERED, DesktopChannel, Event
from .panels import Panel
from .plugin import Metric, Setting, SourcePlugin, SyncContext
from .store import Store

ERROR_RETRY = 300  # after a failed sync, try again within 5 min even if the interval is longer
CONFIG_IMPORTED = "config_imported"


@dataclass(frozen=True)
class PluginInfo:
    name: str
    connection: str
    settings: tuple[Setting, ...]
    can_test: bool
    can_detect: bool
    kind: str  # "items" (things that come and go) or "samples" (rows that are always there)
    default_interval: int
    default_rule: Rule | None  # only for "samples"
    metrics: tuple[Metric, ...] = ()
    default_checks: tuple[Check, ...] = ()
    title: str = ""  # display name of the kind, e.g. "ActiveMQ queues"


@dataclass(frozen=True)
class EditableSource:
    """What the edit screen needs. Secret values are never included."""

    source: SourceConfig  # settings hold no keyring secrets
    plugin: PluginInfo
    identity: tuple[str, ...]  # setting keys that can't change
    stored_secrets: tuple[str, ...]  # secret keys kept in the keyring


@dataclass(frozen=True)
class DetectedSource:
    name: str
    settings: dict[str, Any]
    description: str
    existing_id: str | None  # set when a source with the same identity is already added


class Core:
    def __init__(
        self,
        plugins: dict[str, SourcePlugin],
        store: Store,
        *,
        clock: Callable[[], float] = time.time,
        tick: float = 5.0,
        desktop: DesktopChannel | None = None,
    ) -> None:
        self._plugins = plugins
        self._desktop = desktop or DesktopChannel()
        self._events: deque[Event] = deque(maxlen=200)
        self._seq = 0
        self._store = store
        self._clock = clock
        self._tick = tick
        self._all: dict[str, SourceConfig] = {}  # every stored source, in display order
        self._sources: dict[str, SourceConfig] = {}  # the runnable ones
        self._invalid: dict[str, str] = {}  # source id -> why it can't run (e.g. plugin uninstalled)
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_attempt: dict[str, float] = {}
        self._version = 0
        self._cached: State | None = None
        for source in store.list_sources():
            self._load(source)

    # -- interface used by the UI ---------------------------------------------

    def get_state(self) -> State:
        if self._cached is None or self._cached.version != self._version:
            self._cached = self._build_state()
        return self._cached

    async def sync_now(self, source_id: str | None = None) -> None:
        ids = [source_id] if source_id else self._runnable()
        await asyncio.gather(*(self._sync_source(i) for i in ids))

    def events_since(self, seq: int) -> tuple[Event, ...]:
        """Notification events newer than `seq` (0 for all kept). Events live in memory only."""
        return tuple(e for e in self._events if e.seq > seq)

    def ignore_item(self, source_id: str, item_id: str) -> None:
        """Hide an item. It stays hidden while the source keeps reporting it; if it leaves and
        comes back (e.g. a review re-requested after you reviewed), it shows up as new."""
        self._store.ignore(source_id, item_id)
        self._bump()

    def mark_read(self, source_id: str, item_id: str) -> None:
        """Hide an item for good. Unlike ignore, this is remembered after the item leaves the
        snapshot, so the archive shows it read and it stays hidden if the source reports it again."""
        if not self._store.set_read(source_id, item_id, self._clock()):
            raise ValueError("no such item")
        self._bump()

    def mark_unread(self, source_id: str, item_id: str) -> None:
        """Undo mark_read: back in the overview if the source still reports it."""
        if not self._store.set_read(source_id, item_id, None):
            raise ValueError("no such item")
        self._bump()

    def archive(self) -> tuple[ArchiveEntry, ...]:
        """Every item any items source has reported, read or not, newest first by first_seen."""
        return tuple(
            ArchiveEntry(
                source_id=source_id,
                source_name=source.name,
                plugin=source.plugin,
                item=item,
                first_seen=first,
                read_at=read_at,
                present=present,
            )
            for source_id, item, first, read_at, present in self._store.history()
            if (source := self._all.get(source_id)) is not None
        )

    def acknowledge(self, source_id: str, key: str) -> None:
        health = self._health_of(source_id, key)
        if health.state != FAILING:
            raise ValueError("only a failing row can be acknowledged")
        self._store.set_health(source_id, key, Health(health.state, health.since, health.recent, acknowledged=True))
        self._bump()

    def close(self, source_id: str, key: str) -> None:
        health = self._health_of(source_id, key)
        if not health.acknowledged:
            raise ValueError("nothing to close: this row isn't acknowledged")
        if health.state == FAILING:
            raise ValueError("still failing: close it once it has recovered")
        self._store.set_health(source_id, key, Health(health.state, health.since, health.recent, acknowledged=False))
        self._bump()

    def list_plugins(self) -> tuple[PluginInfo, ...]:
        return tuple(
            PluginInfo(
                name=p.name,
                connection=p.connection,
                settings=tuple(p.settings),
                can_test=hasattr(p, "test_connection"),
                can_detect=hasattr(p, "detect"),
                kind=_kind(p),
                default_interval=getattr(p, "default_interval", DEFAULT_INTERVAL),
                default_rule=getattr(p, "default_rule", Rule()) if _kind(p) == "samples" else None,
                metrics=tuple(getattr(p, "metrics", ())),
                default_checks=tuple(getattr(p, "default_checks", ())),
                title=getattr(p, "title", p.name),
            )
            for p in sorted(self._plugins.values(), key=lambda p: p.name)
        )

    def add_source(
        self,
        plugin: str,
        name: str,
        settings: dict[str, Any],
        *,
        interval: int = DEFAULT_INTERVAL,
        tags: tuple[str, ...] = (),
        source_id: str | None = None,
        rule: dict[str, int] | None = None,
        checks: list[dict] | None = None,
        client: str = "",
        environment: str = "",
    ) -> str:
        """Validate, persist and schedule a new source. Returns its id. Raises ValueError.

        `checks` (samples only) defaults to the plugin's default_checks.
        """
        if not name.strip():
            raise ValueError("name is required")
        settings = self._complete_settings(plugin, settings)
        stored_rule, stored_checks = self._judging(plugin, rule, checks)
        source = SourceConfig(
            id=source_id or self._new_id(name),
            plugin=plugin,
            name=name.strip(),
            interval=interval,
            tags=tags,
            settings=settings,
            rule=stored_rule,
            checks=stored_checks,
            client=client.strip(),
            environment=environment.strip(),
        )
        if source.id in self._all:
            raise ValueError(f"a source with id {source.id!r} already exists")
        self._store.add_source(source, self._secret_keys(plugin))
        self._load(source)
        self._bump()
        return source.id

    def editable(self, source_id: str) -> EditableSource:
        source = self._all[source_id]
        if source.plugin not in self._plugins:
            raise ValueError(f"plugin {source.plugin!r} is not installed")
        [info] = [p for p in self.list_plugins() if p.name == source.plugin]
        return EditableSource(
            source=source,
            plugin=info,
            identity=tuple(getattr(self._plugins[source.plugin], "identity", ())),
            stored_secrets=self._store.secrets.keys(source_id),
        )

    def update_source(
        self,
        source_id: str,
        *,
        name: str,
        settings: dict[str, Any],
        interval: int,
        tags: tuple[str, ...] = (),
        rule: dict[str, int] | None = None,
        checks: list[dict] | None = None,
        client: str | None = None,
        environment: str | None = None,
    ) -> None:
        """Change a source in place; its history stays. An empty secret keeps the stored one.

        Identity settings (e.g. a URL) can't change: that would be a different source.
        """
        old = self._all[source_id]
        if old.plugin not in self._plugins:
            raise ValueError(f"plugin {old.plugin!r} is not installed")
        if not name.strip():
            raise ValueError("name is required")
        for key in getattr(self._plugins[old.plugin], "identity", ()):
            if key in settings and settings[key] != old.settings.get(key):
                raise ValueError(f"{key} can't change: add a new source instead")
        secret_keys = self._secret_keys(old.plugin)
        stored = set(self._store.secrets.keys(source_id))
        kept = {k for k in secret_keys & stored if not settings.get(k)}
        merged = {**old.settings, **{k: v for k, v in settings.items() if k not in kept}}
        completed = self._complete_settings(old.plugin, {**merged, **{k: "kept" for k in kept}})
        stored_rule, stored_checks = self._judging(old.plugin, rule, checks if checks is not None else old.checks)
        source = replace(
            old,
            name=name.strip(),
            interval=interval,
            tags=tags,
            settings={k: v for k, v in completed.items() if k not in kept},
            rule=stored_rule,
            checks=stored_checks,
            client=old.client if client is None else client.strip(),
            environment=old.environment if environment is None else environment.strip(),
        )
        self._store.update_source(source, secret_keys, kept)
        self._all[source_id] = source
        if source_id in self._sources:
            self._sources[source_id] = source
        self._bump()

    def remove_source(self, source_id: str) -> None:
        self._store.remove_source(source_id)
        self._all.pop(source_id, None)
        self._sources.pop(source_id, None)
        self._invalid.pop(source_id, None)
        self._locks.pop(source_id, None)
        self._last_attempt.pop(source_id, None)
        self._drop_from_panels(source_id)
        self._bump()

    def detect_sources(self, plugin: str) -> tuple[DetectedSource, ...]:
        """Blocking. Ask a plugin what it can find on this machine, marking ones already added."""
        instance = self._plugins[plugin]
        if not hasattr(instance, "detect"):
            return ()
        identity = tuple(getattr(instance, "identity", ()))
        existing = {
            tuple(s.settings.get(k) for k in identity): s.id
            for s in self._all.values()
            if s.plugin == plugin
        }
        return tuple(
            DetectedSource(
                name=c.name,
                settings=dict(c.settings),
                description=c.description,
                existing_id=existing.get(tuple(c.settings.get(k) for k in identity)) if identity else None,
            )
            for c in instance.detect()
        )

    def setting_options(self, plugin: str, settings: dict[str, Any], key: str, source_id: str | None = None) -> list[str]:
        """Blocking. Values the user can pick for a Setting with fetch=True, e.g. queue names.

        With `source_id` (editing), settings left empty fall back to the stored ones, secrets included.
        """
        instance = self._plugins[plugin]
        if not hasattr(instance, "options"):
            return []
        if source_id is not None:
            settings = {**self._with_secrets(self._all[source_id]), **{k: v for k, v in settings.items() if v}}
        settings = self._complete_settings(plugin, settings)
        with CONNECTIONS[instance.connection]() as conn:
            return list(instance.options(SyncContext(settings=self._resolve(plugin, settings), http=conn), key))

    def test_settings(self, plugin: str, settings: dict[str, Any]) -> str:
        """Blocking. Ask the plugin to verify these settings work, before saving."""
        settings = self._complete_settings(plugin, settings)
        instance = self._plugins[plugin]
        if not hasattr(instance, "test_connection"):
            return "plugin has no connection test"
        with CONNECTIONS[instance.connection]() as conn:
            return instance.test_connection(SyncContext(settings=self._resolve(plugin, settings), http=conn))

    def test_connection(self, source_id: str) -> str:
        """Blocking. Test an existing source."""
        if source_id in self._invalid:
            raise RuntimeError(self._invalid[source_id])
        source = self._sources[source_id]
        return self.test_settings(source.plugin, self._with_secrets(source))

    def import_config(self, sources: list[SourceConfig]) -> int:
        """One-time import of config.toml. Does nothing after the first successful run."""
        if self._store.get_meta(CONFIG_IMPORTED) or self._all:
            return 0
        for s in sources:
            self.add_source(
                s.plugin, s.name, s.settings, interval=s.interval, tags=s.tags, source_id=s.id,
                client=s.client, environment=s.environment,
            )
        self._store.set_meta(CONFIG_IMPORTED, str(self._clock()))
        return len(sources)

    def preference(self, key: str, default: str = "") -> str:
        """UI preferences that survive restarts, e.g. the grouping."""
        return self._store.get_meta(f"pref:{key}") or default

    def set_preference(self, key: str, value: str) -> None:
        self._store.set_meta(f"pref:{key}", value)

    def panels(self) -> list[Panel]:
        """Saved panels, "All" first. Unreadable or missing data gives just "All"."""
        return panellib.from_json(self.preference("panels") or None)

    def save_panels(self, panels: list[Panel]) -> None:
        """Replace the saved panels. "All" is re-added and put first; sources are untouched."""
        self.set_preference("panels", panellib.to_json(panellib.from_json(panellib.to_json(panels))))

    def active_panel(self) -> str:
        """Id of the panel last shown, or "All" if it no longer exists."""
        pid = self.preference("active_panel", panellib.ALL_ID)
        return pid if any(p.id == pid for p in self.panels()) else panellib.ALL_ID

    def set_active_panel(self, panel_id: str) -> None:
        self.set_preference("active_panel", panel_id)

    def source_ids(self) -> list[str]:
        return list(self._all)

    # -- scheduling -------------------------------------------------------------

    async def run(self) -> None:
        """Sync each source when it is due. Runs until cancelled."""
        while True:
            due = [i for i in self._runnable() if self._is_due(i)]
            if due:
                await asyncio.gather(*(self._sync_source(i) for i in due))
            await asyncio.sleep(self._tick)

    def _runnable(self) -> list[str]:
        return list(self._sources)

    def _is_due(self, source_id: str) -> bool:
        if self._locks[source_id].locked():
            return False
        last_sync, last_error = self._store.source_state(source_id)
        last = self._last_attempt.get(source_id, last_sync)
        if last is None:
            return True
        wait = self._sources[source_id].interval
        if last_error:
            wait = min(wait, ERROR_RETRY)
        return self._clock() - last >= wait

    # -- syncing ----------------------------------------------------------------

    async def _sync_source(self, source_id: str) -> None:
        lock = self._locks.get(source_id)
        if lock is None or lock.locked():
            return None  # removed, or already syncing
        async with lock:
            self._bump()
            self._last_attempt[source_id] = self._clock()
            source = self._sources[source_id]
            memory = self._store.memory(source_id)  # fresh copy: a failed sync's changes are dropped
            try:
                items, warnings, quiet = await asyncio.to_thread(
                    self._fetch, source, self._store.secrets.keys(source_id), memory
                )
            except Exception as exc:  # a broken source must not take the core down
                if source_id in self._sources:
                    self._store.record_error(source_id, _describe(exc))
                return None
            else:
                if source_id not in self._sources:
                    return None  # removed while syncing
                if _kind(self._plugins[source.plugin]) == "samples":
                    events = self._apply_samples(source, items, memory, warnings)
                else:
                    events = self._apply(source, items, memory, warnings, quiet)
            finally:
                self._bump()
        if events:
            await asyncio.to_thread(self._desktop.send, events)

    def _fetch(
        self, source: SourceConfig, secret_keys: tuple[str, ...], memory: dict
    ) -> tuple[list[Item] | list[Sample], tuple[str, ...], frozenset[str]]:
        """Runs in a worker thread: no database access here. The plugin changes `memory` in place."""
        plugin = self._plugins[source.plugin]
        settings = {**source.settings, **self._store.secrets.read(source.id, secret_keys)}
        with CONNECTIONS[plugin.connection]() as conn:
            settings = self._resolve(source.plugin, settings)
            ctx = SyncContext(settings=settings, http=conn, memory=memory)
            result = plugin.sync(ctx)
        try:
            json.dumps(ctx.memory)
        except (TypeError, ValueError):
            raise RuntimeError("plugin memory is not plain JSON data") from None
        if ctx.memory is not memory:  # replaced rather than changed in place
            memory.clear()
            memory.update(ctx.memory)
        return result, tuple(str(w) for w in ctx.warnings), frozenset(str(q) for q in ctx.quiet)

    def _apply(
        self, source: SourceConfig, items: list[Item], memory: dict, warnings: tuple[str, ...], quiet: frozenset[str] = frozenset()
    ) -> list[Event]:
        now = self._clock()
        first_sync = self._store.source_state(source.id)[0] is None
        previous = self._store.snapshot(source.id)
        diff = diff_snapshot({k: item for k, (item, _) in previous.items()}, items)
        kept: dict[str, tuple[Item, float]] = {i.id: (i, now) for i in diff.new}
        for item in diff.updated + diff.unchanged:
            kept[item.id] = (item, previous[item.id][1])
        self._store.replace_snapshot(source.id, kept, now, memory=memory, warnings=warnings)
        self._store.forget_ignored_except(source.id, set(kept))
        if first_sync:
            return []  # adding a source shouldn't announce everything it already had
        read = self._store.read_items(source.id)  # back after leaving, but still read: it stays hidden, so no event
        return [
            self._event(NEW_ITEM, source, title=f"{source.name}: {item.action}", body=item.title, url=item.url)
            for item in diff.new
            if item.id not in read and item.id not in quiet  # quiet: the plugin says show it, don't announce it
        ]

    def _apply_samples(
        self, source: SourceConfig, samples: list[Sample], memory: dict, warnings: tuple[str, ...]
    ) -> list[Event]:
        now = self._clock()
        rule = Rule.from_dict(source.rule)
        previous = self._store.health(source.id)
        before = {key: s.values for key, (s, _) in self._store.latest_samples(source.id).items()}
        latest: dict[str, Sample] = {}
        for sample in samples:
            if sample.key not in latest:  # one result per row per sync
                latest[sample.key] = self._judge(source, sample, before.get(sample.key))
        health = {key: step(previous.get(key), sample.ok, now, rule) for key, sample in latest.items()}
        self._store.record_samples(source.id, list(latest.values()), health, now, memory=memory, warnings=warnings)
        events = []
        for key, h in health.items():
            was = previous.get(key)
            body = f"{key}: {latest[key].summary}"
            if h.state == FAILING and (was is None or was.state != FAILING):
                events.append(self._event(FAILING_EVENT, source, title=f"✗ {source.name} is failing", body=body, url=latest[key].url))
            elif h.state != FAILING and was is not None and was.state == FAILING:
                note = " · press c to close" if h.acknowledged else ""
                events.append(self._event(RECOVERED, source, title=f"✓ {source.name} recovered", body=body + note, url=latest[key].url))
        return events

    def _judge(self, source: SourceConfig, sample: Sample, previous: dict | None) -> Sample:
        """Add counter growth to the summary, then apply the source's checks: any that fires
        makes this a bad check, named in the summary."""
        metrics = getattr(self._plugins[source.plugin], "metrics", ())
        checks = [Check.from_dict(c) for c in source.checks or ()]
        labels = checklib.fired(checks, sample.values, previous, metrics)
        summary = " · ".join(
            part for part in (sample.summary, checklib.growth_summary(sample.values, previous, metrics), ", ".join(labels)) if part
        )
        return replace(sample, ok=sample.ok and not labels, summary=summary)

    def _event(self, kind: str, source: SourceConfig, *, title: str, body: str, url: str) -> Event:
        self._seq += 1
        event = Event(self._seq, self._clock(), kind, source.id, source.name, title, body, url)
        self._events.append(event)
        return event

    def _health_of(self, source_id: str, key: str) -> Health:
        health = self._store.health(source_id).get(key)
        if health is None:
            raise ValueError("no such row")
        return health

    # -- sources ----------------------------------------------------------------

    def _load(self, source: SourceConfig) -> None:
        self._all[source.id] = source
        plugin = self._plugins.get(source.plugin)
        if plugin is None:
            self._invalid[source.id] = f"plugin {source.plugin!r} is not installed"
        elif plugin.connection not in CONNECTIONS:
            self._invalid[source.id] = f"plugin needs unknown connection {plugin.connection!r}"
        else:
            self._sources[source.id] = source
            self._locks[source.id] = asyncio.Lock()

    def _complete_settings(self, plugin: str, settings: dict[str, Any]) -> dict[str, Any]:
        instance = self._plugins.get(plugin)
        if instance is None:
            raise ValueError(f"no plugin named {plugin!r} is installed")
        result = {k: v for k, v in settings.items() if v not in (None, "", [])}
        for setting in instance.settings:
            if setting.key in result:
                continue
            if setting.choices and setting.key not in settings:  # omitted entirely: use defaults
                result[setting.key] = [c.value for c in setting.choices if c.default]
            elif setting.default is not None:
                result[setting.key] = setting.default
        missing = [s.label for s in instance.settings if s.required and s.key not in result]
        if missing:
            raise ValueError(f"missing: {', '.join(missing)}")
        return result

    def _judging(self, plugin: str, rule: dict | None, checks: list[dict] | None) -> tuple[dict | None, list[dict] | None]:
        """Validated rule and checks to store; samples plugins only. None means the plugin's defaults."""
        instance = self._plugins[plugin]
        if _kind(instance) != "samples":
            return None, None
        default = getattr(instance, "default_rule", Rule())
        stored_rule = (Rule.from_dict(rule) if rule else default).to_dict()  # validates
        parsed = [Check.from_dict(c) for c in checks] if checks is not None else list(getattr(instance, "default_checks", ()))
        checklib.validate(parsed, getattr(instance, "metrics", ()))
        return stored_rule, [c.to_dict() for c in parsed]

    def _secret_keys(self, plugin: str) -> set[str]:
        return {s.key for s in self._plugins[plugin].settings if s.secret}

    def _with_secrets(self, source: SourceConfig) -> dict[str, Any]:
        return {**source.settings, **self._store.secrets.get_all(source.id)}

    def _resolve(self, plugin: str, settings: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(settings)
        for key in self._secret_keys(plugin) & resolved.keys():
            resolved[key] = resolve_secret(str(resolved[key]))
        return resolved

    def _new_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "source"
        taken = set(self.source_ids())
        candidate, n = base, 2
        while candidate in taken:
            candidate, n = f"{base}-{n}", n + 1
        return candidate

    def _drop_from_panels(self, source_id: str) -> None:
        # Panels stay; only the removed source leaves their hand-picked list.
        if not self.preference("panels"):
            return
        before = self.panels()
        after = [
            replace(p, filter=replace(p.filter, picks=tuple(x for x in p.filter.picks if x != source_id)))
            for p in before
        ]
        if after != before:
            self.save_panels(after)

    # -- state ------------------------------------------------------------------

    def _bump(self) -> None:
        self._version += 1

    def _build_state(self) -> State:
        views = []
        for source in self._all.values():
            last_sync, last_error = self._store.source_state(source.id)
            snapshot = self._store.snapshot(source.id)
            ignored = self._store.ignored(source.id)
            read = self._store.read_items(source.id) - ignored
            items = sorted(
                (
                    ItemView(item=i, first_seen=first, is_new=first == last_sync)
                    for i, first in snapshot.values()
                    if i.id not in ignored and i.id not in read
                ),
                key=lambda v: v.item.updated_at,
                reverse=True,
            )
            lock = self._locks.get(source.id)
            rule = Rule.from_dict(source.rule)
            health = self._store.health(source.id)
            plugin = self._plugins.get(source.plugin)
            metrics = getattr(plugin, "metrics", ())
            checks = [Check.from_dict(c) for c in source.checks or ()]
            before = self._store.previous_samples(source.id)
            health_views = tuple(
                HealthView(
                    key=key,
                    state=health[key].state,
                    since=health[key].since,
                    summary=sample.summary,
                    failures=health[key].failures(rule),
                    window=rule.window,
                    checked_at=taken_at,
                    url=sample.url,
                    acknowledged=health[key].acknowledged,
                    values=sample.values,
                    growth={
                        m.key: g
                        for m in metrics
                        if m.counter and (g := checklib.growth(m.key, sample.values, before.get(key))) is not None
                    },
                    fired=tuple(checklib.fired(checks, sample.values, before.get(key), metrics)),
                )
                for key, (sample, taken_at) in sorted(self._store.latest_samples(source.id).items())
                if key in health
            )
            views.append(
                SourceView(
                    id=source.id,
                    plugin=source.plugin,
                    name=source.name,
                    tags=source.tags,
                    interval=source.interval,
                    last_sync=last_sync,
                    last_error=self._invalid.get(source.id, last_error),
                    warnings=self._store.warnings(source.id),
                    syncing=bool(lock and lock.locked()),
                    items=tuple(items),
                    health=health_views,
                    ignored=len(ignored & snapshot.keys()),
                    read=len(read & snapshot.keys()),
                    client=source.client,
                    environment=source.environment,
                )
            )
        return State(version=self._version, sources=tuple(views))


def _kind(plugin: SourcePlugin) -> str:
    return getattr(plugin, "kind", "items")


def _describe(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    return text.splitlines()[0][:200]
