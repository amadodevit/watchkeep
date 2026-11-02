import asyncio

from textual.app import App
from textual.widgets import Button, Checkbox, Input, OptionList, SelectionList, Static

from watchkeep.core.checks import Check, Condition
from watchkeep.core.engine import Core
from watchkeep.core.health import Rule
from watchkeep.core.plugin import Candidate, Choice, Metric, Setting
from watchkeep.core.store import Store
from watchkeep.ui.forms import AddSourceScreen, EditSourceScreen


class WebPlugin:
    """No detection, no choices: type -> manual -> watch(more options only)."""

    name = "web"
    connection = "http"
    settings = (
        Setting("url", "URL", help="what to check"),
        Setting("password", "Password", secret=True, required=False),
    )

    def sync(self, ctx):
        return []


class GhLikePlugin:
    """Detects two accounts; has a Watch choice."""

    name = "ghlike"
    connection = "http"
    identity = ("account",)
    settings = (
        Setting("account", "Account"),
        Setting("token", "Token", secret=True),
        Setting("watch", "Watch", choices=(Choice("reviews", "Reviews"), Choice("mentions", "Mentions"))),
    )

    def __init__(self, found=None):
        self.found = found if found is not None else [
            Candidate("work", {"account": "work", "token": "gh:work"}),
            Candidate("home", {"account": "home", "token": "gh:home"}),
        ]

    def detect(self):
        return self.found

    def sync(self, ctx):
        return []


def run_wizard(core, steps):
    """Open the wizard and run `steps(app, pilot)`; return what it dismissed with."""
    result = {}

    class Host(App):
        def on_mount(self):
            self.push_screen(AddSourceScreen(core), lambda ids: result.setdefault("ids", ids))

    async def run():
        app = Host()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await steps(app, pilot)
            await pilot.pause()

    asyncio.run(run())
    return result.get("ids")


async def pick_type(app, pilot, name):
    types = app.screen.query_one("#types", OptionList)
    types.highlighted = [o.id for o in types.options].index(name)
    types.focus()
    await pilot.press("enter")
    await pilot.pause()


async def wait_for(app, pilot, selector):
    for _ in range(30):
        await pilot.pause(0.05)
        if app.screen.query(selector):
            return
    raise AssertionError(f"{selector} never appeared")


def result_text(app):
    return str(app.screen.query_one("#result", Static).render())


def test_detected_accounts_skip_typing_and_respect_watch():
    core = Core({"ghlike": GhLikePlugin(), "web": WebPlugin()}, Store(":memory:"))
    core.add_source("ghlike", "work", {"account": "work", "token": "gh:work"})

    async def steps(app, pilot):
        await pick_type(app, pilot, "ghlike")
        await wait_for(app, pilot, "#accounts")
        accounts = app.screen.query_one("#accounts", SelectionList)
        assert accounts.get_option_at_index(0).disabled  # "work" already added
        assert not app.screen.query(Input)  # no username/token fields on the main path
        accounts.select(1)
        await pilot.click("#next-found")
        await wait_for(app, pilot, "#setting-watch")
        app.screen.query_one("#setting-watch", SelectionList).deselect("mentions")
        await pilot.click("#add")

    assert run_wizard(core, steps) == ["home"]
    home = core.get_state().sources[1]
    assert home.name == "home" and home.interval == 3600
    assert core._all["home"].settings == {"account": "home", "token": "gh:home", "watch": ["reviews"]}


def test_several_accounts_in_one_pass():
    core = Core({"ghlike": GhLikePlugin()}, Store(":memory:"))

    async def steps(app, pilot):
        await pick_type(app, pilot, "ghlike")
        await wait_for(app, pilot, "#accounts")
        app.screen.query_one("#accounts", SelectionList).select_all()
        await pilot.click("#next-found")
        await wait_for(app, pilot, "#add")
        assert "Add 2 sources" in str(app.screen.query_one("#add").label)
        await pilot.click("#add")

    assert run_wizard(core, steps) == ["work", "home"]


def test_next_without_ticking_shows_error():
    core = Core({"ghlike": GhLikePlugin()}, Store(":memory:"))

    async def steps(app, pilot):
        await pick_type(app, pilot, "ghlike")
        await wait_for(app, pilot, "#accounts")
        await pilot.click("#next-found")
        assert "Tick at least one" in result_text(app)

    assert run_wizard(core, steps) is None


def test_nothing_found_falls_back_to_manual():
    core = Core({"ghlike": GhLikePlugin(found=[])}, Store(":memory:"))

    async def steps(app, pilot):
        await pick_type(app, pilot, "ghlike")
        await wait_for(app, pilot, "#setting-account")
        assert "Nothing found" in result_text(app)
        assert app.screen.query_one("#setting-token", Input).password
        app.screen.query_one("#setting-account", Input).value = "someone"
        app.screen.query_one("#setting-token", Input).value = "not-a-real-token"
        await pilot.click("#next-manual")
        await wait_for(app, pilot, "#add")
        await pilot.click("#add")

    assert run_wizard(core, steps) == ["someone"]
    assert core._all["someone"].settings["watch"] == ["reviews", "mentions"]


def test_manual_validates_and_more_options_apply():
    core = Core({"web": WebPlugin()}, Store(":memory:"))

    async def steps(app, pilot):
        await pick_type(app, pilot, "web")
        await wait_for(app, pilot, "#setting-url")
        await pilot.click("#next-manual")
        assert "missing: URL" in result_text(app)
        await pilot.pause(0.6)  # let the click chain expire, or the next click counts as a double-click
        app.screen.query_one("#setting-url", Input).value = "https://example.com"
        await pilot.click("#next-manual")
        await wait_for(app, pilot, "#interval")
        app.screen.query_one("#interval", Input).value = "5m"
        app.screen.query_one("#tags", Input).value = "prod, web"
        await pilot.click("#add")

    [sid] = run_wizard(core, steps)
    view = core.get_state().sources[0]
    assert (view.name, view.interval, view.tags) == ("https://example.com", 300, ("prod", "web"))


def test_escape_goes_back_then_cancels():
    core = Core({"ghlike": GhLikePlugin(found=[]), "web": WebPlugin()}, Store(":memory:"))

    async def steps(app, pilot):
        await pick_type(app, pilot, "web")
        await wait_for(app, pilot, "#setting-url")
        await pilot.press("escape")
        await wait_for(app, pilot, "#types")
        await pilot.press("escape")

    assert run_wizard(core, steps) == []
    assert core.get_state().sources == ()


class ProbePlugin:
    name = "probe"
    connection = "http"
    kind = "samples"
    default_interval = 300
    default_rule = Rule(window=10, fail_after=5, recover_after=3)
    settings = (Setting("url", "URL"),)

    def sync(self, ctx):
        return []


def test_sample_plugin_wizard_shows_rule_and_uses_plugin_interval():
    core = Core({"probe": ProbePlugin()}, Store(":memory:"))

    async def steps(app, pilot):
        await pick_type(app, pilot, "probe")
        await wait_for(app, pilot, "#setting-url")
        app.screen.query_one("#setting-url", Input).value = "https://example.com"
        await pilot.click("#next-manual")
        await wait_for(app, pilot, "#rule-fail")
        assert str(app.screen.query_one("#title", Static).render()) == "When is it failing?"
        assert [app.screen.query_one(f"#rule-{k}", Input).value for k in ("fail", "window", "recover")] == ["5", "10", "3"]
        assert app.screen.query_one("#interval", Input).placeholder.startswith("5m")
        app.screen.query_one("#rule-recover", Input).value = "2"
        await pilot.click("#add")

    [sid] = run_wizard(core, steps)
    source = core._all[sid]
    assert source.interval == 300
    assert source.rule == {"window": 10, "fail_after": 5, "recover_after": 2}


class QueuePlugin:
    """Samples with metrics, editable checks and a fetchable setting."""

    name = "queues"
    connection = "http"
    kind = "samples"
    default_rule = Rule(window=3, fail_after=3, recover_after=1)
    metrics = (Metric("size", "Waiting"), Metric("dequeued", "Dequeued", counter=True))
    default_checks = (
        Check("backlog", "Backlog", (Condition("size", ">", 1000),)),
        Check("stuck", "Stuck", (Condition("size", ">", 0), Condition("dequeued", "=", 0))),
    )
    settings = (Setting("url", "URL"), Setting("queues", "Queues", required=False, default="*", fetch=True))

    def options(self, ctx, key):
        return ["orders", "billing"]

    def sync(self, ctx):
        return []


def test_checks_are_editable_and_queues_can_be_fetched():
    core = Core({"queues": QueuePlugin()}, Store(":memory:"))

    async def steps(app, pilot):
        await pick_type(app, pilot, "queues")
        await wait_for(app, pilot, "#setting-url")
        app.screen.query_one("#setting-url", Input).value = "http://broker"
        app.screen.query_one("#setting-queues", Input).value = "audit.*"
        await pilot.click("#fetch-queues")
        await wait_for(app, pilot, "#options-queues")
        app.screen.query_one("#options-queues", SelectionList).select("billing")
        await pilot.pause()
        assert app.screen.query_one("#setting-queues", Input).value == "audit.*, billing"  # typed pattern kept
        await pilot.click("#next-manual")
        await wait_for(app, pilot, "#check-backlog")
        app.screen.query_one("#check-backlog-0", Input).value = "50"
        app.screen.query_one("#check-stuck", Checkbox).value = False
        app.screen.query_one("#client", Input).value = "Acme"
        app.screen.query_one("#name", Input).value = "Prod broker"
        app.screen.query_one("#add", Button).press()  # below the fold with all the fields

    [sid] = run_wizard(core, steps)
    source = core._all[sid]
    assert (source.name, source.client) == ("Prod broker", "Acme")
    assert source.settings["queues"] == "audit.*, billing"
    assert [(c["label"], c["enabled"], c["conditions"]) for c in source.checks] == [
        ("Backlog", True, [["size", ">", 50.0]]),
        ("Stuck", False, [["size", ">", 0.0], ["dequeued", "=", 0.0]]),
    ]


def run_edit(core, source_id, steps):
    result = {}

    class Host(App):
        def on_mount(self):
            self.push_screen(EditSourceScreen(core, source_id), lambda saved: result.setdefault("saved", saved))

    async def run():
        app = Host()
        async with app.run_test(size=(100, 60)) as pilot:
            await pilot.pause()
            await steps(app, pilot)
            await pilot.pause()

    asyncio.run(run())
    return result.get("saved")


class BrokerPlugin(QueuePlugin):
    identity = ("url",)
    settings = (Setting("url", "URL"), Setting("password", "Password", secret=True), Setting("queues", "Queues", required=False, default="*", fetch=True))


def test_edit_screen_is_prefilled_and_saves(fake_keyring):
    core = Core({"queues": BrokerPlugin()}, Store(":memory:"))
    sid = core.add_source("queues", "Broker", {"url": "http://broker", "password": "pw", "queues": "orders"}, interval=60)

    async def steps(app, pilot):
        screen = app.screen
        assert screen.query_one("#name", Input).value == "Broker"
        assert not screen.query("#setting-url")  # identity is read-only
        password = screen.query_one("#setting-password", Input)
        assert password.value == "" and password.placeholder.startswith("unchanged")
        assert screen.query_one("#setting-queues", Input).value == "orders"
        assert screen.query_one("#check-backlog-0", Input).value == "1000"
        assert screen.query_one("#interval", Input).value == "1m"
        screen.query_one("#check-backlog-0", Input).value = "10"
        screen.query_one("#tags", Input).value = "prod"
        await pilot.click("#fetch-queues")  # uses the stored url and password
        await wait_for(app, pilot, "#options-queues")
        screen.query_one("#options-queues", SelectionList).select("billing")
        await pilot.pause()
        await pilot.press("ctrl+s")  # Save is below the fold once the picker opens

    assert run_edit(core, sid, steps) is True
    source = core._all[sid]
    assert source.checks[0]["conditions"] == [["size", ">", 10.0]]
    assert source.tags == ("prod",) and source.settings["queues"] == "orders, billing"
    assert fake_keyring.entries == {("watchkeep", f"{sid}/password"): "pw"}


def test_edit_screen_escape_changes_nothing():
    core = Core({"web": WebPlugin()}, Store(":memory:"))
    sid = core.add_source("web", "Site", {"url": "https://example.com"})

    async def steps(app, pilot):
        app.screen.query_one("#name", Input).value = "Changed"
        await pilot.press("escape")

    assert run_edit(core, sid, steps) is False
    assert core._all[sid].name == "Site"


# -- panel manager ---------------------------------------------------------------

from watchkeep.core.panels import Panel, PanelFilter, all_panel  # noqa: E402
from watchkeep.ui.panels import ListSuggester, PanelsScreen  # noqa: E402


def panel_core():
    core = Core({"web": WebPlugin(), "ghlike": GhLikePlugin()}, Store(":memory:"))
    ids = {
        "work": core.add_source("ghlike", "Work", {"account": "work", "token": "gh:work"}, client="acme", environment="prod"),
        "site": core.add_source("web", "Site", {"url": "https://example.com"}, client="acme", environment="staging"),
        "blog": core.add_source("web", "Blog", {"url": "https://example.org"}, tags=("personal",)),
    }
    return core, ids


def run_panels(core, steps, size=(100, 60)):
    """Open the panel manager and run `steps(app, pilot)`; return what it dismissed with."""
    result = {}

    class Host(App):
        def on_mount(self):
            self.push_screen(PanelsScreen(core), lambda saved: result.setdefault("saved", saved))

    async def run():
        app = Host()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await steps(app, pilot)
            await pilot.pause()

    asyncio.run(run())
    return result.get("saved")


def count_text(app):
    return str(app.screen.query_one("#count", Static).render())


def panel_names(app):
    return [str(o.prompt) for o in app.screen.query_one("#panels", OptionList).options]


async def type_into(app, pilot, selector, value):
    app.screen.query_one(selector, Input).value = value
    await pilot.pause()


async def press(app, pilot, button):
    app.screen.query_one(f"#{button}", Button).press()
    await pilot.pause()


async def highlight(app, pilot, index):
    app.screen.query_one("#panels", OptionList).highlighted = index
    await pilot.pause()


def test_panel_manager_fits_a_short_terminal():
    core, _ = panel_core()

    async def steps(app, pilot):
        screen = app.screen.region
        assert screen.contains_region(app.screen.query_one("#save", Button).region)
        body = app.screen.query_one("#body")
        assert body.max_scroll_y > 0
        body.scroll_end(animate=False)
        await pilot.pause()
        assert body.region.contains_region(app.screen.query_one("#picks").region)

    run_panels(core, steps, size=(100, 30))


def test_panel_manager_creates_a_tag_panel_with_a_live_count():
    core, _ = panel_core()

    async def steps(app, pilot):
        assert panel_names(app) == ["All"]
        await press(app, pilot, "new")
        assert panel_names(app) == ["All", "New panel"]
        await type_into(app, pilot, "#name", "Personal")
        assert count_text(app) == "0 sources match"  # no rules yet
        await type_into(app, pilot, "#tags", " Personal ")
        assert count_text(app) == "1 source matches"
        assert panel_names(app) == ["All", "Personal"]
        await pilot.press("ctrl+s")

    assert run_panels(core, steps) is True
    assert [(p.name, p.filter) for p in core.panels()] == [("All", PanelFilter()), ("Personal", PanelFilter(tags=("Personal",)))]


def test_panel_manager_count_follows_every_rule_field():
    core, _ = panel_core()

    async def steps(app, pilot):
        await press(app, pilot, "new")
        await type_into(app, pilot, "#clients", "acme")
        assert count_text(app) == "2 sources match"
        await type_into(app, pilot, "#environments", "prod")
        assert count_text(app) == "1 source matches"
        await type_into(app, pilot, "#environments", "prod, staging")
        assert count_text(app) == "2 sources match"
        await type_into(app, pilot, "#clients", "")
        await type_into(app, pilot, "#environments", "")
        await type_into(app, pilot, "#plugins", "web")
        assert count_text(app) == "2 sources match"
        await pilot.press("ctrl+s")

    assert run_panels(core, steps) is True
    assert core.panels()[1].filter == PanelFilter(plugins=("web",))


def test_panel_manager_hand_picks_sources():
    core, ids = panel_core()

    async def steps(app, pilot):
        await press(app, pilot, "new")
        await type_into(app, pilot, "#name", "Morning")
        picks = app.screen.query_one("#picks", SelectionList)
        assert sorted(str(o.prompt) for o in picks.options) == ["Blog", "Site", "Work"]
        picks.select(ids["work"])
        picks.select(ids["blog"])
        await pilot.pause()
        assert count_text(app) == "2 sources match"
        await pilot.press("ctrl+s")

    assert run_panels(core, steps) is True
    made = core.panels()[1]
    assert made.name == "Morning" and set(made.filter.picks) == {ids["work"], ids["blog"]}


def test_panel_manager_renames_reorders_and_deletes():
    core, ids = panel_core()
    core.save_panels([
        all_panel(),
        Panel("p1", "Acme", PanelFilter(clients=("acme",))),
        Panel("p2", "Personal", PanelFilter(tags=("personal",))),
        Panel("p3", "Prod", PanelFilter(environments=("prod",))),
    ])

    async def steps(app, pilot):
        await highlight(app, pilot, 2)  # Personal
        await press(app, pilot, "up")
        assert panel_names(app) == ["All", "Personal", "Acme", "Prod"]
        await press(app, pilot, "up")  # never above All
        assert panel_names(app) == ["All", "Personal", "Acme", "Prod"]
        await highlight(app, pilot, 2)  # Acme
        assert app.screen.query_one("#clients", Input).value == "acme"  # the editor follows the highlight
        await type_into(app, pilot, "#name", "Acme Co")
        await press(app, pilot, "down")
        assert panel_names(app) == ["All", "Personal", "Prod", "Acme Co"]
        await highlight(app, pilot, 2)  # Prod
        await press(app, pilot, "delete")
        assert panel_names(app) == ["All", "Personal", "Acme Co"]
        await pilot.press("ctrl+s")

    assert run_panels(core, steps) is True
    assert [(p.id, p.name) for p in core.panels()] == [("all", "All"), ("p2", "Personal"), ("p1", "Acme Co")]
    assert sorted(core.source_ids()) == sorted(ids.values())  # deleting a panel never deletes a source


def test_panel_manager_cannot_delete_or_filter_all():
    core, _ = panel_core()

    async def steps(app, pilot):
        await highlight(app, pilot, 0)
        assert count_text(app) == "3 sources match"
        assert app.screen.query_one("#tags", Input).disabled
        assert app.screen.query_one("#picks", SelectionList).disabled
        await press(app, pilot, "delete")
        assert panel_names(app) == ["All"]
        assert "can't be deleted" in str(app.screen.query_one("#result", Static).render())
        await pilot.press("ctrl+s")

    assert run_panels(core, steps) is True
    assert [p.id for p in core.panels()] == ["all"]


def test_panel_manager_suggests_values_already_in_use():
    core, _ = panel_core()

    async def steps(app, pilot):
        await press(app, pilot, "new")
        screen = app.screen
        assert screen.query_one("#clients", Input).placeholder == "acme"
        assert screen.query_one("#environments", Input).placeholder == "prod, staging"
        assert screen.query_one("#tags", Input).placeholder == "personal"
        assert screen.query_one("#plugins", Input).placeholder == "ghlike, web"
        await pilot.press("escape")

    run_panels(core, steps)


def test_panel_list_suggester_completes_the_last_value():
    suggester = ListSuggester(["acme", "globex"])
    assert asyncio.run(suggester.get_suggestion("globex, ac")) == "globex, acme"
    assert asyncio.run(suggester.get_suggestion("AC")) == "acme"
    assert asyncio.run(suggester.get_suggestion("globex, ")) is None


def test_panel_manager_saves_only_through_core_and_escape_changes_nothing():
    core, _ = panel_core()
    calls = []
    real = core.save_panels
    core.save_panels = lambda panels: (calls.append(panels), real(panels))

    async def cancel(app, pilot):
        await press(app, pilot, "new")
        await type_into(app, pilot, "#tags", "personal")
        await pilot.press("escape")

    async def save(app, pilot):
        await press(app, pilot, "new")
        await pilot.press("ctrl+s")

    assert run_panels(core, cancel) is False
    assert calls == [] and [p.id for p in core.panels()] == ["all"]
    assert run_panels(core, save) is True
    assert len(calls) == 1


def test_panel_manager_opens_from_the_app_with_p_and_updates_the_tabs():
    from textual.widgets import Tabs

    from watchkeep.ui.app import WatchkeepApp

    core, _ = panel_core()

    async def run():
        app = WatchkeepApp(core)
        app.core.run = lambda: asyncio.sleep(3600)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause(1.2)
            await pilot.press("p")
            await pilot.pause(0.2)
            assert isinstance(app.screen, PanelsScreen)
            await press(app, pilot, "new")
            await type_into(app, pilot, "#name", "Personal")
            await type_into(app, pilot, "#tags", "personal")
            await pilot.press("ctrl+s")
            await pilot.pause(0.5)
            return [str(t.label) for t in app.query_one(Tabs).query("Tab")]

    assert asyncio.run(run()) == ["1 All", "2 Personal", "Notifications", "Archive"]


class GhReposPlugin(GhLikePlugin):
    """Like GhLikePlugin, plus an optional repo picker that only the manual step used to show."""

    settings = GhLikePlugin.settings + (Setting("repos", "Repos", required=False, fetch=True),)

    def options(self, ctx, key):
        self.asked = (ctx.settings["account"], key)
        return ["acme/gears", "acme/widgets"]


def test_detected_account_gets_picker_fields_in_the_watch_step():
    plugin = GhReposPlugin(found=[Candidate("home", {"account": "home", "token": "gh:home"})])
    core = Core({"ghlike": plugin}, Store(":memory:"))
    core._resolve = lambda name, settings: settings  # no gh CLI in tests

    async def steps(app, pilot):
        await pick_type(app, pilot, "ghlike")
        await wait_for(app, pilot, "#accounts")
        app.screen.query_one("#accounts", SelectionList).select(0)
        await pilot.click("#next-found")
        await wait_for(app, pilot, "#setting-repos")
        app.screen.query_one("#fetch-repos", Button).press()
        await wait_for(app, pilot, "#options-repos")
        app.screen.query_one("#options-repos", SelectionList).select("acme/widgets")
        await pilot.pause()
        await pilot.click("#add")

    assert run_wizard(core, steps) == ["home"]
    assert plugin.asked == ("home", "repos")  # fetched with the detected account's settings
    assert core._all["home"].settings["repos"] == "acme/widgets"


def test_manual_path_keeps_picker_fields_on_the_details_step_only():
    plugin = GhReposPlugin(found=[])
    core = Core({"ghlike": plugin}, Store(":memory:"))

    async def steps(app, pilot):
        await pick_type(app, pilot, "ghlike")
        await wait_for(app, pilot, "#setting-repos")  # nothing detected -> manual step
        app.screen.query_one("#setting-account", Input).value = "me"
        app.screen.query_one("#setting-token", Input).value = "env:TOKEN"
        app.screen.query_one("#setting-repos", Input).value = "acme/*"
        await pilot.click("#next-manual")
        await wait_for(app, pilot, "#add")
        assert not app.screen.query("#setting-repos")
        await pilot.click("#add")

    assert run_wizard(core, steps) == ["me"]
    assert core._all["me"].settings["repos"] == "acme/*"
