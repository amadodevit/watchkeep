import asyncio

import pytest

from watchkeep.core.checks import Check, Condition
from watchkeep.core.config import SourceConfig
from watchkeep.core.engine import ERROR_RETRY, Core
from watchkeep.core.health import Rule
from watchkeep.core.models import Item, Sample
from watchkeep.core.panels import ALL_ID, Panel, PanelFilter, all_panel
from watchkeep.core.plugin import Candidate, Choice, Metric, Setting
from watchkeep.core.registry import discover
from watchkeep.core.store import Store


def item(id_):
    return Item(id=id_, action="review requested", title=id_, url=f"https://x/{id_}", repo="a/b", author="z", updated_at="t")


class FakePlugin:
    name = "fake"
    connection = "http"
    settings = (Setting("account", "Account"), Setting("token", "Token", secret=True, required=False, default="none"))

    def __init__(self):
        self.next = []
        self.fail = False
        self.seen_settings = None

    def sync(self, ctx):
        self.seen_settings = ctx.settings
        if self.fail:
            raise RuntimeError("boom")
        return list(self.next)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def setup():
    plugin, clock = FakePlugin(), Clock()
    core = Core({"fake": plugin}, Store(":memory:"), clock=clock)
    core.add_source("fake", "S1", {"account": "me"}, interval=600, source_id="s1")
    return core, plugin, clock


def items_of(core):
    return {v.item.id: v for v in core.get_state().sources[0].items}


def test_sync_lifecycle_keeps_first_seen_and_flags_new(setup):
    core, plugin, clock = setup
    plugin.next = [item("a")]
    asyncio.run(core.sync_now())
    assert items_of(core)["a"].is_new

    clock.now += 700
    plugin.next = [item("a"), item("b")]
    asyncio.run(core.sync_now())
    seen = items_of(core)
    assert seen["a"].first_seen == 1000.0 and not seen["a"].is_new
    assert seen["b"].is_new

    clock.now += 700
    plugin.next = [item("b")]  # "a" was reviewed
    asyncio.run(core.sync_now())
    assert set(items_of(core)) == {"b"}

    clock.now += 700
    plugin.next = [item("a"), item("b")]  # "a" re-requested
    asyncio.run(core.sync_now())
    assert items_of(core)["a"].is_new and items_of(core)["a"].first_seen == clock.now


def test_failed_sync_keeps_items_and_reports_error(setup):
    core, plugin, clock = setup
    plugin.next = [item("a")]
    asyncio.run(core.sync_now())
    plugin.fail = True
    asyncio.run(core.sync_now())
    source = core.get_state().sources[0]
    assert source.last_error == "boom"
    assert [v.item.id for v in source.items] == ["a"]


class RememberingPlugin(FakePlugin):
    """Counts its syncs in memory and warns about what it skipped."""

    def __init__(self):
        super().__init__()
        self.seen_memory = []

    def sync(self, ctx):
        self.seen_memory.append(dict(ctx.memory))
        ctx.memory["syncs"] = ctx.memory.get("syncs", 0) + 1
        if self.fail:
            raise RuntimeError("boom")
        ctx.warnings.extend(self.next)
        return []


def test_memory_persists_between_syncs_and_a_failed_sync_leaves_it_untouched(tmp_path):
    plugin, clock = RememberingPlugin(), Clock()
    core = Core({"fake": plugin}, Store(tmp_path / "state.db"), clock=clock)
    core.add_source("fake", "S1", {"account": "me"}, source_id="s1")
    asyncio.run(core.sync_now())
    plugin.fail = True
    asyncio.run(core.sync_now())
    plugin.fail = False
    asyncio.run(core.sync_now())
    assert plugin.seen_memory == [{}, {"syncs": 1}, {"syncs": 1}]

    restarted = Core({"fake": plugin}, Store(tmp_path / "state.db"), clock=clock)
    asyncio.run(restarted.sync_now())
    assert plugin.seen_memory[-1] == {"syncs": 2}


def test_warnings_show_on_the_source_until_a_clean_sync():
    plugin = RememberingPlugin()
    core = Core({"fake": plugin}, Store(":memory:"), clock=Clock())
    core.add_source("fake", "S1", {"account": "me"}, source_id="s1")
    plugin.next = ["feeds.example.com: timeout"]
    asyncio.run(core.sync_now())
    source = core.get_state().sources[0]
    assert source.warnings == ("feeds.example.com: timeout",) and source.last_error is None
    plugin.next = []
    asyncio.run(core.sync_now())
    assert core.get_state().sources[0].warnings == ()


def test_due_respects_interval_and_error_retry(setup):
    core, plugin, clock = setup
    assert core._is_due("s1")  # never synced
    asyncio.run(core.sync_now())
    assert not core._is_due("s1")
    clock.now += 600
    assert core._is_due("s1")

    core.add_source("fake", "S2", {"account": "me"}, interval=3600, source_id="s2")
    plugin.fail = True
    asyncio.run(core.sync_now("s2"))
    clock.now += ERROR_RETRY
    assert core._is_due("s2")  # retried before the 1h interval


def test_add_source_validates_and_fills_defaults(setup):
    core, plugin, _ = setup
    with pytest.raises(ValueError, match="no plugin"):
        core.add_source("nope", "x", {})
    with pytest.raises(ValueError, match="missing: Account"):
        core.add_source("fake", "x", {"account": ""})
    with pytest.raises(ValueError, match="name is required"):
        core.add_source("fake", "  ", {"account": "me"})
    asyncio.run(core.sync_now("s1"))
    assert plugin.seen_settings == {"account": "me", "token": "none"}  # default filled in


def test_ids_are_slugs_and_unique(setup):
    core, _, _ = setup
    assert core.add_source("fake", "Acme Work!", {"account": "me"}) == "acme-work"
    assert core.add_source("fake", "Acme Work", {"account": "me"}) == "acme-work-2"


def test_remove_source_drops_it_and_its_history(setup):
    core, plugin, _ = setup
    plugin.next = [item("a")]
    asyncio.run(core.sync_now())
    core.remove_source("s1")
    assert core.get_state().sources == ()
    core.add_source("fake", "S1", {"account": "me"}, source_id="s1")
    assert core.get_state().sources[0].items == ()  # no leftovers under a reused id


def test_source_with_uninstalled_plugin_is_shown_not_run():
    store = Store(":memory:")
    store.add_source(SourceConfig(id="old", plugin="gone", name="Old"), secret_keys=set())
    core = Core({}, store)
    [view] = core.get_state().sources
    assert view.last_error == "plugin 'gone' is not installed"
    asyncio.run(core.sync_now())  # must not crash
    with pytest.raises(RuntimeError, match="not installed"):
        core.test_connection("old")


def test_import_config_runs_once():
    core = Core({"fake": FakePlugin()}, Store(":memory:"))
    sources = [SourceConfig(id="gh-a", plugin="fake", name="A", interval=60, tags=("work",), settings={"account": "a"})]
    assert core.import_config(sources) == 1
    [view] = core.get_state().sources
    assert (view.id, view.interval, view.tags) == ("gh-a", 60, ("work",))
    core.remove_source("gh-a")
    assert core.import_config(sources) == 0  # marker set: deleting everything doesn't re-import


def test_github_plugin_is_discovered_via_entry_point():
    assert "github" in discover()


class DetectingPlugin(FakePlugin):
    name = "det"
    identity = ("account",)
    settings = (
        Setting("account", "Account"),
        Setting("watch", "Watch", choices=(Choice("a", "A"), Choice("b", "B", default=False))),
    )

    def detect(self):
        return [Candidate("one", {"account": "one"}), Candidate("two", {"account": "two"})]


def test_detect_marks_already_added_by_identity():
    core = Core({"det": DetectingPlugin()}, Store(":memory:"))
    sid = core.add_source("det", "Mine", {"account": "two"})
    found = core.detect_sources("det")
    assert [(f.name, f.existing_id) for f in found] == [("one", None), ("two", sid)]


def test_choice_defaults_and_empty_selection():
    plugin = DetectingPlugin()
    core = Core({"det": plugin}, Store(":memory:"))
    core.add_source("det", "x", {"account": "x"})  # watch omitted -> defaults
    asyncio.run(core.sync_now())
    assert plugin.seen_settings["watch"] == ["a"]
    with pytest.raises(ValueError, match="missing: Watch"):
        core.add_source("det", "y", {"account": "y", "watch": []})  # user unticked everything


class SamplePlugin:
    name = "probe"
    connection = "http"
    kind = "samples"
    default_rule = Rule(window=4, fail_after=2, recover_after=2)
    settings = (Setting("url", "URL"),)

    def __init__(self):
        self.ok = True

    def sync(self, ctx):
        return [Sample(key=ctx.settings["url"], ok=self.ok, summary="200" if self.ok else "500", values={}, url=ctx.settings["url"])]


def test_samples_drive_health_with_stored_rule():
    plugin, clock = SamplePlugin(), Clock()
    store = Store(":memory:")
    core = Core({"probe": plugin}, store, clock=clock)
    sid = core.add_source("probe", "site", {"url": "https://x"})
    assert store.list_sources()[0].rule == {"window": 4, "fail_after": 2, "recover_after": 2}

    plugin.ok = False
    for _ in range(2):
        clock.now += 1
        asyncio.run(core.sync_now(sid))
    [h] = core.get_state().sources[0].health
    assert (h.state, h.summary, h.failures, h.window, h.url) == ("failing", "500", 2, 4, "https://x")

    plugin.ok = True
    for _ in range(2):
        clock.now += 1
        asyncio.run(core.sync_now(sid))
    [h] = core.get_state().sources[0].health
    assert h.state == "ok" and h.failures == 0

    # survives a restart: health comes from disk
    [h2] = Core({"probe": plugin}, store, clock=clock).get_state().sources[0].health
    assert h2 == h


def test_custom_rule_is_validated_and_kept():
    core = Core({"probe": SamplePlugin()}, Store(":memory:"))
    with pytest.raises(ValueError, match="within only"):
        core.add_source("probe", "a", {"url": "u"}, rule={"window": 3, "fail_after": 5, "recover_after": 1})
    sid = core.add_source("probe", "b", {"url": "u"}, rule={"window": 20, "fail_after": 5, "recover_after": 1})
    assert core._all[sid].rule["window"] == 20


def test_item_sources_have_no_rule(setup):
    core, _, _ = setup
    assert core._all["s1"].rule is None


def test_store_migrates_databases_without_rule_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, plugin TEXT NOT NULL, name TEXT NOT NULL, interval INTEGER NOT NULL, tags TEXT NOT NULL, settings TEXT NOT NULL, position INTEGER NOT NULL)")
    db.execute("INSERT INTO sources VALUES ('gh', 'github', 'GH', 3600, '[]', '{}', 0)")
    db.commit()
    db.close()
    [source] = Store(path).list_sources()
    assert source.id == "gh" and source.rule is None


def test_secret_reaches_the_plugin_at_sync_and_a_locked_keyring_retries(fake_keyring):
    plugin, clock = FakePlugin(), Clock()
    core = Core({"fake": plugin}, Store(":memory:"), clock=clock)
    core.add_source("fake", "S1", {"account": "me", "token": "t0k"}, interval=3600, source_id="s1")
    asyncio.run(core.sync_now())
    assert plugin.seen_settings["token"] == "t0k"

    fake_keyring.locked = True
    clock.now += 3600
    asyncio.run(core.sync_now())
    assert "keyring locked" in core.get_state().sources[0].last_error
    clock.now += ERROR_RETRY
    assert core._is_due("s1")  # retried within 5 minutes, not an hour


class QueuePlugin:
    name = "queues"
    connection = "http"
    kind = "samples"
    default_rule = Rule(window=2, fail_after=2, recover_after=1)
    metrics = (Metric("size", "Waiting"), Metric("dequeued", "Dequeued", counter=True, short="out"))
    default_checks = (Check("stuck", "Stuck", (Condition("size", ">", 0), Condition("dequeued", "=", 0))),)
    settings = ()

    def __init__(self):
        self.values = {}

    def sync(self, ctx):
        return [Sample(key="orders", ok=True, summary=f"{self.values['size']} waiting", values=dict(self.values))]


def test_core_checks_turn_numbers_into_failing():
    plugin, clock = QueuePlugin(), Clock()
    core = Core({"queues": plugin}, Store(":memory:"), clock=clock)
    sid = core.add_source("queues", "Broker", {})
    assert core._all[sid].checks[0]["label"] == "Stuck"  # defaults stored with the source

    def sync(size, dequeued):
        plugin.values = {"size": size, "dequeued": dequeued}
        clock.now += 60
        asyncio.run(core.sync_now())
        return core.get_state().sources[0].health[0]

    first = sync(5, 10)
    assert first.state == "ok" and first.summary == "5 waiting"  # first sync: no growth known yet
    row = sync(6, 10)
    assert row.summary == "6 waiting · +0 out · Stuck" and row.failures == 1
    assert sync(7, 10).state == "failing"
    assert sync(0, 17).state == "ok"  # consumer caught up


def test_add_source_rejects_checks_on_unknown_metrics():
    core = Core({"queues": QueuePlugin()}, Store(":memory:"))
    bad = [{"id": "x", "label": "X", "conditions": [["latency", ">", 1]]}]
    with pytest.raises(ValueError, match="unknown metric"):
        core.add_source("queues", "Broker", {}, checks=bad)


class BrokerPlugin(QueuePlugin):
    identity = ("url",)
    settings = (Setting("url", "URL"), Setting("password", "Password", secret=True), Setting("queues", "Queues", required=False, default="*"))


def test_edit_keeps_history_and_applies_from_next_sync(fake_keyring):
    plugin, clock = BrokerPlugin(), Clock()
    core = Core({"queues": plugin}, Store(":memory:"), clock=clock)
    sid = core.add_source("queues", "Broker", {"url": "http://b", "password": "pw1"})

    def sync(size, dequeued):
        plugin.values = {"size": size, "dequeued": dequeued}
        clock.now += 60
        asyncio.run(core.sync_now())
        return core.get_state().sources[0]

    sync(5, 10)
    assert sync(6, 10).health[0].failures == 1  # stuck once
    checks = core._all[sid].checks
    checks[0]["conditions"][0][2] = 100  # stuck only above 100 waiting
    core.update_source(sid, name="Prod broker", settings={"password": "", "queues": "orders"}, interval=120, checks=checks)

    view = sync(7, 10)
    assert view.name == "Prod broker" and view.interval == 120
    assert view.health[0].failures == 1  # history kept; the new threshold no longer fires
    assert core._all[sid].settings == {"url": "http://b", "queues": "orders"}
    assert fake_keyring.entries == {("watchkeep", f"{sid}/password"): "pw1"}  # empty kept it


def test_edit_replaces_or_releases_secrets_and_refuses_identity(fake_keyring):
    core = Core({"queues": BrokerPlugin()}, Store(":memory:"))
    sid = core.add_source("queues", "Broker", {"url": "http://b", "password": "pw1"})
    core.update_source(sid, name="Broker", settings={"password": "pw2"}, interval=60)
    assert fake_keyring.entries == {("watchkeep", f"{sid}/password"): "pw2"}
    core.update_source(sid, name="Broker", settings={"password": "env:BROKER_PW"}, interval=60)
    assert fake_keyring.entries == {} and core._all[sid].settings["password"] == "env:BROKER_PW"
    with pytest.raises(ValueError, match="url can't change"):
        core.update_source(sid, name="Broker", settings={"url": "http://other"}, interval=60)


def test_edit_survives_a_restart(fake_keyring):
    store = Store(":memory:")
    core = Core({"queues": BrokerPlugin()}, store)
    sid = core.add_source("queues", "Broker", {"url": "http://b", "password": "pw"})
    core.update_source(sid, name="Renamed", settings={"password": ""}, interval=300, tags=("prod",), rule={"window": 5, "fail_after": 2, "recover_after": 2})
    [reloaded] = store.list_sources()
    assert (reloaded.name, reloaded.interval, reloaded.tags, reloaded.rule) == ("Renamed", 300, ("prod",), {"window": 5, "fail_after": 2, "recover_after": 2})


def panel_core():
    store = Store(":memory:")
    core = Core({"fake": FakePlugin()}, store)
    for sid in ("s1", "s2", "s3"):
        core.add_source("fake", sid, {"account": sid}, source_id=sid)
    return core, store


ACME = Panel("acme", "Acme", PanelFilter(clients=("acme",), picks=("s1", "s2")))
PERSONAL = Panel("personal", "Personal", PanelFilter(environments=("prod",), picks=("s1",)))


def test_fresh_core_has_only_the_all_panel():
    core, _ = panel_core()
    assert core.panels() == [all_panel()]


def test_saved_panels_round_trip_and_persist():
    core, store = panel_core()
    core.save_panels([all_panel(), ACME, PERSONAL])
    assert core.panels() == [all_panel(), ACME, PERSONAL]
    assert Core({"fake": FakePlugin()}, store).panels() == [all_panel(), ACME, PERSONAL]


def test_all_panel_is_always_present_and_first():
    core, _ = panel_core()
    core.save_panels([ACME])
    assert core.panels() == [all_panel(), ACME]
    core.save_panels([ACME, all_panel(), PERSONAL])
    assert core.panels() == [all_panel(), ACME, PERSONAL]
    core.save_panels([])
    assert core.panels() == [all_panel()]


@pytest.mark.parametrize("stored", ["not json", "", "{}", '{"panels": 3}'])
def test_unreadable_stored_panels_fall_back_to_all(stored):
    core, _ = panel_core()
    core.set_preference("panels", stored)
    assert core.panels() == [all_panel()]


def test_remove_source_drops_it_from_panel_picks_only():
    core, store = panel_core()
    core.save_panels([all_panel(), ACME, PERSONAL])
    core.remove_source("s1")
    assert core.panels() == [
        all_panel(),
        Panel("acme", "Acme", PanelFilter(clients=("acme",), picks=("s2",))),
        Panel("personal", "Personal", PanelFilter(environments=("prod",))),
    ]
    assert core.source_ids() == ["s2", "s3"]
    assert Core({"fake": FakePlugin()}, store).panels() == core.panels()


def test_remove_source_without_saved_panels_leaves_panels_unset():
    core, _ = panel_core()
    core.remove_source("s1")
    assert core.preference("panels") == ""
    assert core.panels() == [all_panel()]


def test_deleting_a_panel_keeps_every_source():
    core, _ = panel_core()
    core.save_panels([all_panel(), ACME, PERSONAL])
    core.save_panels([all_panel(), PERSONAL])
    assert core.panels() == [all_panel(), PERSONAL]
    assert core.source_ids() == ["s1", "s2", "s3"]
    assert {s.id for s in core.get_state().sources} == {"s1", "s2", "s3"}


def test_active_panel_persists_and_defaults_to_all():
    core, store = panel_core()
    assert core.active_panel() == ALL_ID
    core.save_panels([all_panel(), ACME])
    core.set_active_panel("acme")
    assert core.active_panel() == "acme"
    assert Core({"fake": FakePlugin()}, store).active_panel() == "acme"


def test_stale_active_panel_falls_back_to_all():
    core, _ = panel_core()
    core.save_panels([all_panel(), ACME])
    core.set_active_panel("acme")
    core.save_panels([all_panel()])
    assert core.active_panel() == ALL_ID
    core.set_preference("active_panel", "garbage")
    assert core.active_panel() == ALL_ID


def test_first_sync_and_later_syncs_go_to_the_archive(setup):
    core, plugin, clock = setup
    plugin.next = [item("a")]
    asyncio.run(core.sync_now())  # baseline: quiet, but archived
    clock.now += 700
    plugin.next = [item("a"), item("b")]
    asyncio.run(core.sync_now())
    entries = core.archive()
    assert [(e.item.id, e.first_seen) for e in entries] == [("b", 1700.0), ("a", 1000.0)]
    assert all(e.present and e.read_at is None and e.source_name == "S1" and e.plugin == "fake" for e in entries)


def test_read_hides_survives_leaving_and_unread_restores(setup):
    core, plugin, clock = setup
    plugin.next = [item("a"), item("b")]
    asyncio.run(core.sync_now())
    version = core.get_state().version
    core.mark_read("s1", "a")
    state = core.get_state()
    assert state.version > version
    assert set(items_of(core)) == {"b"} and state.sources[0].read == 1 and state.sources[0].ignored == 0

    clock.now += 700
    plugin.next = [item("b")]  # "a" leaves the snapshot
    asyncio.run(core.sync_now())
    [a] = [e for e in core.archive() if e.item.id == "a"]
    assert a.read_at == 1000.0 and not a.present
    assert core.get_state().sources[0].read == 0

    clock.now += 700
    plugin.next = [item("a"), item("b"), item("c")]  # "a" back, still read: hidden and no notification
    asyncio.run(core.sync_now())
    assert set(items_of(core)) == {"b", "c"}
    assert [e.body for e in core.events_since(0)] == ["c"]

    core.mark_unread("s1", "a")
    assert set(items_of(core)) == {"a", "b", "c"}
    [a] = [e for e in core.archive() if e.item.id == "a"]
    assert a.read_at is None and a.present and a.first_seen == 1000.0


def test_read_of_unknown_item_raises_and_remove_source_clears_archive(setup):
    core, plugin, _ = setup
    with pytest.raises(ValueError):
        core.mark_read("s1", "nope")
    with pytest.raises(ValueError):
        core.mark_unread("other", "a")
    plugin.next = [item("a")]
    asyncio.run(core.sync_now())
    core.mark_read("s1", "a")
    core.remove_source("s1")
    assert core.archive() == ()
    core.add_source("fake", "S1", {"account": "me"}, source_id="s1")
    plugin.next = [item("a")]
    asyncio.run(core.sync_now())
    assert set(items_of(core)) == {"a"}  # not read under a reused id


def test_quiet_items_show_without_a_notification(setup):
    core, plugin, clock = setup
    plugin.next = [item("a")]
    asyncio.run(core.sync_now())  # first sync: quiet anyway
    original = plugin.sync

    def sync(ctx):
        ctx.quiet.add("b")
        return original(ctx)

    plugin.sync = sync
    clock.now += 700
    plugin.next = [item("a"), item("b"), item("c")]
    asyncio.run(core.sync_now())
    assert set(items_of(core)) == {"a", "b", "c"}
    assert [e.body for e in core.events_since(0)] == ["c"]
