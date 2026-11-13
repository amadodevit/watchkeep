import asyncio

from textual.widgets import DataTable

from watchkeep.core.engine import Core
from watchkeep.core.models import Item
from watchkeep.core.plugin import Setting
from watchkeep.core.store import Store
from watchkeep.ui.app import WatchkeepApp
from watchkeep.ui.layout import depth_name


class ItemsPlugin:
    name = "items"
    connection = "http"
    settings = (Setting("account", "Account"),)

    def sync(self, ctx):
        return [
            Item(id=i, action="mentioned", title=f"PR {i}", url=f"https://x/{i}", repo="a/b", author="z", updated_at=i)
            for i in ("1", "2")
        ]


def test_pressing_i_hides_the_selected_item():
    core = Core({"items": ItemsPlugin()}, Store(":memory:"))
    sid = core.add_source("items", "Work", {"account": "me"})
    asyncio.run(core.sync_now(sid))

    async def run():
        app = WatchkeepApp(core)
        app.core.run = lambda: asyncio.sleep(3600)  # no background syncing in this test
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(1.2)
            table = app.query_one(DataTable)
            table.move_cursor(row=table.get_row_index(f"{sid}\x1f2"))  # newest item
            await pilot.press("i")
            await pilot.pause(1.2)
            titles = [str(table.get_row_at(r)[1]) for r in range(table.row_count)]
            header_status = str(table.get_row(f"hdr:{sid}")[1])
            return titles, header_status

    titles, header_status = asyncio.run(run())
    assert "PR 2" not in titles and "PR 1" in titles  # newest (updated_at "2") was first, now hidden
    assert "1 ignored" in header_status




def test_g_cycles_grouping_and_remembers_it():
    core = Core({"items": ItemsPlugin()}, Store(":memory:"))
    core.add_source("items", "Work", {"account": "me"}, client="Acme")

    async def run():
        app = WatchkeepApp(core)
        app.core.run = lambda: asyncio.sleep(3600)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause(1.2)
            first = str(app.query_one(DataTable).get_row_at(0)[0]).strip()
            await pilot.press("g")
            await pilot.pause(0.2)
            second = str(app.query_one(DataTable).get_row_at(0)[0]).strip()
            return first, second

    assert asyncio.run(run()) == ("▾ Acme", "▾ items")
    assert core.preference("grouping") == "Kind"


def test_plus_and_minus_step_through_levels():
    core = Core({"items": ItemsPlugin()}, Store(":memory:"))
    for client in ("Acme", "Globex"):
        sid = core.add_source("items", client, {"account": client}, client=client)
        asyncio.run(core.sync_now(sid))

    async def run():
        app = WatchkeepApp(core)
        app.core.run = lambda: asyncio.sleep(3600)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause(1.2)
            table = app.query_one(DataTable)
            counts = [table.row_count]
            for key in ("minus", "minus", "minus", "plus", "plus", "plus", "plus"):
                await pilot.press(key)
                await pilot.pause(0.1)
                counts.append(table.row_count)
            return counts

    # per client: group, kind, source, 2 items. Level 2 (start) shows all 10; items sources are open by default.
    # -: kinds folded -> client+kind = 4; -: clients only = 2; - at the top stays 2; then back up.
    assert asyncio.run(run()) == [10, 4, 2, 2, 4, 10, 10, 10]


def test_level_names():
    levels = ("client", "kind")
    assert [depth_name(levels, d) for d in range(4)] == [
        "Showing clients",
        "Showing clients and kinds",
        "Showing clients, kinds and sources",
        "Showing everything",
    ]


# -- panels and notifications --------------------------------------------------

from textual.widgets import Tabs  # noqa: E402

from watchkeep.core.notify import DesktopChannel  # noqa: E402
from watchkeep.core.panels import Panel, PanelFilter, all_panel  # noqa: E402
from watchkeep.ui import layout  # noqa: E402
from watchkeep.ui.forms import ConfirmScreen  # noqa: E402


class GrowingPlugin:
    """Reports one more item on every sync, so each sync after the first fires a new-item event."""

    name = "items"
    connection = "http"
    settings = (Setting("account", "Account"),)

    def __init__(self):
        self.count = 0

    def sync(self, ctx):
        self.count += 1
        return [
            Item(id=str(i), action="mentioned", title=f"PR {i}", url=f"https://x/{i}", repo="a/b", author="z", updated_at=str(i))
            for i in range(1, self.count + 1)
        ]


class BrokenPlugin:
    name = "broken"
    connection = "http"
    settings = (Setting("account", "Account"),)

    def sync(self, ctx):
        raise RuntimeError("unreachable")


def quiet_core(**plugins):
    return Core(plugins or {"items": ItemsPlugin()}, Store(":memory:"), desktop=DesktopChannel(which=lambda _: None))


def three_panels(core):
    core.save_panels([
        all_panel(),
        Panel("p-acme", "Acme", PanelFilter(clients=("acme",))),
        Panel("p-personal", "Personal", PanelFilter(tags=("personal",))),
    ])


def overview(app):
    return app.query_one("#overview", DataTable)


def row_keys(table):
    return [table.coordinate_to_cell_key((r, 0)).row_key.value for r in range(table.row_count)]


def tab_labels(app):
    return [str(tab.label) for tab in app.query_one(Tabs).query("Tab")]


def start(core, body, size=(160, 30)):
    async def run():
        app = WatchkeepApp(core)
        app.core.run = lambda: asyncio.sleep(3600)  # no background syncing in these tests
        async with app.run_test(size=size) as pilot:
            await pilot.pause(1.2)
            return await body(app, pilot)

    return asyncio.run(run())


def test_panel_first_run_shows_all_and_the_same_rows_as_before():
    core = quiet_core()
    sid = core.add_source("items", "Work", {"account": "me"}, client="acme")
    asyncio.run(core.sync_now(sid))

    async def body(app, pilot):
        return tab_labels(app), row_keys(overview(app))

    labels, keys = start(core, body)
    plugins = {p.name: p for p in core.list_plugins()}
    expected = [r.key for r in layout.build(core.get_state(), plugins, layout.View(levels=layout.PRESETS[0][1], depth=2))]
    assert [label.split()[-1] for label in labels] == ["All", "Notifications", "Archive"]
    assert keys == expected


def test_panel_number_keys_and_tab_switch_panels():
    core = quiet_core()
    acme = core.add_source("items", "Work", {"account": "a"}, client="acme")
    home = core.add_source("items", "Home", {"account": "h"}, tags=("personal",))
    three_panels(core)

    async def body(app, pilot):
        seen = []
        for key in ("2", "tab", "shift+tab", "1", "shift+tab"):
            await pilot.press(key)
            await pilot.pause(0.2)
            sources = {v[1] for v in app._rows.values()}
            seen.append((core.active_panel(), sources, overview(app).display))
        return seen

    seen = start(core, body)
    assert seen[0] == ("p-acme", {acme}, True)
    assert seen[1] == ("p-personal", {home}, True)
    assert seen[2] == ("p-acme", {acme}, True)
    assert seen[3] == ("all", {acme, home}, True)
    assert seen[4][2] is False  # shift+tab from the first tab wraps to Archive, which hides the overview


def test_panel_tab_labels_show_failing_counts():
    core = quiet_core(items=ItemsPlugin(), broken=BrokenPlugin())
    core.add_source("items", "Work", {"account": "a"}, client="acme")
    bad = core.add_source("broken", "Home", {"account": "h"}, tags=("personal",))
    asyncio.run(core.sync_now(bad))
    three_panels(core)

    async def body(app, pilot):
        return tab_labels(app)

    labels = start(core, body)
    assert labels[0] == "1 All ✗1"
    assert labels[1] == "2 Acme"
    assert labels[2] == "3 Personal ✗1"


def test_panel_new_source_with_a_matching_tag_shows_up_without_editing_the_panel():
    core = quiet_core()
    three_panels(core)
    core.set_active_panel("p-personal")

    async def body(app, pilot):
        before = row_keys(overview(app))
        sid = core.add_source("items", "Blog", {"account": "b"}, tags=("Personal ",))
        await pilot.pause(1.2)
        return before, sid, row_keys(overview(app))

    before, sid, after = start(core, body)
    assert before == ["none"]  # empty panel row
    assert f"hdr:{sid}" in after


def test_panel_empty_panel_says_what_its_filter_is():
    core = quiet_core()
    core.add_source("items", "Work", {"account": "a"}, client="globex")
    three_panels(core)
    core.set_active_panel("p-acme")

    async def body(app, pilot):
        return str(overview(app).get_row("none")[0]) + str(overview(app).get_row("none")[1])

    assert "No sources match client = acme" in start(core, body)


def test_panel_active_panel_survives_a_restart():
    core = quiet_core()
    three_panels(core)

    async def press_three(app, pilot):
        await pilot.press("3")
        await pilot.pause(0.2)

    async def read(app, pilot):
        return app.query_one(Tabs).active, core.active_panel()

    start(core, press_three)
    active_tab, active = start(core, read)
    assert active == "p-personal"
    assert active_tab == "tab-2"


def test_panel_capital_p_on_a_client_group_makes_a_panel():
    core = quiet_core()
    acme = core.add_source("items", "Work", {"account": "a"}, client="acme")
    core.add_source("items", "Other", {"account": "o"}, client="globex")

    async def body(app, pilot):
        table = overview(app)
        table.move_cursor(row=table.get_row_index("grp:acme"))
        await pilot.press("P")
        await pilot.pause(0.2)
        return tab_labels(app), {v[1] for v in app._rows.values()}

    labels, shown = start(core, body)
    made = core.panels()[-1]
    assert (made.name, made.filter) == ("acme", PanelFilter(clients=("acme",)))
    assert core.active_panel() == made.id
    assert labels[1] == "2 acme" and labels[-2:] == ["Notifications", "Archive"]
    assert shown == {acme}


def test_panel_capital_p_on_a_no_client_group_warns_and_makes_nothing():
    core = quiet_core()
    core.add_source("items", "Work", {"account": "a"})

    async def body(app, pilot):
        table = overview(app)
        table.move_cursor(row=table.get_row_index("grp:No client"))
        await pilot.press("P")
        await pilot.pause(0.2)

    start(core, body)
    assert [p.id for p in core.panels()] == ["all"]


def test_notifications_tab_lists_events_newest_first_and_enter_opens_the_url(monkeypatch):
    opened = []
    monkeypatch.setattr("watchkeep.ui.app.shutil.which", lambda _: None)
    monkeypatch.setattr("watchkeep.ui.app.webbrowser.open", opened.append)
    core = quiet_core(items=GrowingPlugin())
    sid = core.add_source("items", "Work", {"account": "a"})
    for _ in range(3):  # quiet first sync, then PR 2, then PR 3
        asyncio.run(core.sync_now(sid))

    async def body(app, pilot):
        await pilot.press("shift+tab", "shift+tab")  # from All, back past Archive to Notifications
        await pilot.pause(0.2)
        table = app.query_one("#notifications", DataTable)
        titles = [str(table.get_row_at(r)[2]) for r in range(table.row_count)]
        await pilot.press("enter")
        await pilot.pause(0.2)
        return table.display, app.focused is table, titles

    shown, focused, titles = start(core, body)
    assert shown and focused
    assert titles == ["PR 3", "PR 2"]
    assert opened == ["https://x/3"]


def test_notification_toasts_still_fire_on_any_panel():
    plugin = GrowingPlugin()
    core = quiet_core(items=plugin)
    sid = core.add_source("items", "Work", {"account": "a"}, client="globex")
    asyncio.run(core.sync_now(sid))
    three_panels(core)
    core.set_active_panel("p-acme")  # the source is not in this panel

    async def body(app, pilot):
        toasts = []
        original = app.notify
        app.notify = lambda message, **kw: (toasts.append(message), original(message, **kw))
        await core.sync_now(sid)
        await pilot.pause(1.2)
        return toasts, app.query_one("#notifications", DataTable).row_count

    toasts, rows = start(core, body)
    assert "PR 2" in toasts
    assert rows == 1


def test_panel_tab_still_moves_focus_inside_a_form():
    core = quiet_core()
    three_panels(core)

    async def body(app, pilot):
        app.push_screen(ConfirmScreen("Delete?"))
        await pilot.pause(0.2)
        before = app.focused
        await pilot.press("tab")
        await pilot.pause(0.2)
        return before, app.focused, core.active_panel()

    before, after, active = start(core, body)
    assert after is not before
    assert active == "all"


def test_m_marks_an_item_read_and_the_archive_tab_flips_it_back():
    core = Core({"items": ItemsPlugin()}, Store(":memory:"))
    sid = core.add_source("items", "Work", {"account": "me"})
    asyncio.run(core.sync_now(sid))

    async def run():
        app = WatchkeepApp(core)
        app.core.run = lambda: asyncio.sleep(3600)
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause(1.2)
            table = app.query_one("#overview", DataTable)
            table.move_cursor(row=table.get_row_index(f"{sid}\x1f2"))
            await pilot.press("m")
            await pilot.pause(1.2)
            titles = [str(table.get_row_at(r)[1]) for r in range(table.row_count)]
            await pilot.press("shift+tab")  # from All, wraps to Archive
            await pilot.pause(0.5)
            archive = app.query_one("#archive", DataTable)
            read = {str(archive.get_row_at(r)[3]): str(archive.get_row_at(r)[1]) for r in range(archive.row_count)}
            archive.move_cursor(row=archive.get_row_index(f"ar:{sid}\x1f2"))
            await pilot.press("m")  # flips it back to unread
            await pilot.pause(1.2)
            return titles, read

    titles, read = asyncio.run(run())
    assert "PR 2" not in titles and "PR 1" in titles
    assert read["PR 1"] == "unread" and read["PR 2"] != "unread"
    assert [e.read_at for e in core.archive() if e.item.id == "2"] == [None]


class AgendaPlugin:
    name = "agenda"
    connection = "http"
    kind = "events"
    settings = ()

    def sync(self, ctx):
        from datetime import datetime, timedelta

        from watchkeep.core.models import Appointment

        soon = datetime.now().astimezone() + timedelta(minutes=5)
        later = soon + timedelta(minutes=30)
        return [Appointment("m1", "Planning", soon.isoformat(), later.isoformat(), url="https://x/m1")]


def test_agenda_tab_appears_with_an_events_source_and_goes_with_it():
    core = quiet_core(items=ItemsPlugin(), agenda=AgendaPlugin())
    core.add_source("items", "Work", {"account": "a"})

    async def body(app, pilot):
        before = tab_labels(app)
        sid = core.add_source("agenda", "Calendar", {})
        await core.sync_now(sid)
        await pilot.pause(1.5)
        with_agenda = tab_labels(app)
        await pilot.press("tab")  # All -> Agenda
        await pilot.pause(0.3)
        table = app.query_one("#agenda", DataTable)
        shown = (table.display, [str(table.get_row_at(r)[2]) for r in range(table.row_count)])
        core.remove_source(sid)
        await pilot.pause(1.5)
        return before, with_agenda, shown, tab_labels(app), overview(app).display

    before, with_agenda, shown, after, overview_back = start(core, body)
    assert "Agenda" not in before
    assert with_agenda[-3:] == ["Agenda · today", "Notifications", "Archive"]
    assert shown == (True, ["", "Planning"])  # day header, then the meeting
    assert "Agenda" not in after and overview_back  # removing the last calendar returns to All


def test_capital_c_jumps_to_the_agenda_and_back():
    core = quiet_core(items=ItemsPlugin(), agenda=AgendaPlugin())
    core.add_source("items", "Work", {"account": "a"}, tags=("personal",))
    three_panels(core)

    async def body(app, pilot):
        await pilot.press("C")  # no calendar yet
        await pilot.pause(0.3)
        without = app._active
        sid = core.add_source("agenda", "Calendar", {})
        await core.sync_now(sid)
        await pilot.pause(1.5)
        await pilot.press("3")
        await pilot.pause(0.2)
        seen = []
        for _ in range(2):
            await pilot.press("C")
            await pilot.pause(0.3)
            seen.append((app._active, app.query_one("#agenda", DataTable).display))
        return without, seen

    without, seen = start(core, body)
    assert without == 0  # stayed on All, with a warning
    assert seen == [(-3, True), (2, False)]  # to the Agenda, then back to panel 3


def test_d_x_w_pick_the_agenda_days_and_d_still_deletes_elsewhere():
    core = quiet_core(items=ItemsPlugin(), agenda=AgendaPlugin())
    core.add_source("items", "Work", {"account": "a"})
    sid = core.add_source("agenda", "Calendar", {})

    async def body(app, pilot):
        await core.sync_now(sid)
        await pilot.pause(1.5)
        await pilot.press("C")
        await pilot.pause(0.3)
        table = app.query_one("#agenda", DataTable)
        seen = []
        for key in ("w", "x", "d"):
            await pilot.press(key)
            await pilot.pause(0.2)
            headers = sum(1 for r in range(table.row_count) if str(table.get_row_at(r)[2]) in ("", "nothing on") and str(table.get_row_at(r)[0]).strip())
            seen.append((tab_labels(app)[-3], headers, len(app.screen_stack)))
        await pilot.press("x")
        await pilot.press("C")  # back to All
        await pilot.pause(0.3)
        deleted = []
        app.action_delete = lambda: deleted.append(app._active)
        await pilot.press("d")
        await pilot.pause(0.3)
        return seen, deleted

    seen, asked = start(core, body)
    assert seen == [("Agenda · week", 7, 1), ("Agenda · 3 days", 3, 1), ("Agenda · today", 1, 1)]  # no delete prompt
    assert asked == [0]  # outside the Agenda, d is still delete (which asks first)
    assert core.preference("agenda_days") == "3"  # remembered for the next start
