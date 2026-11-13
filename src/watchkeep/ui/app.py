"""Terminal UI. Reads state from the core; never touches plugins or storage.

Panels are tabs over one overview table: switching a panel only changes which
sources the table is built from, so grouping, folding and the cursor work the
same in every panel. After the panels come the fixed tabs, each with its own
table: Agenda (only while an events source exists), Notifications (this
session's events) and Archive.
"""

from __future__ import annotations

import shutil
import subprocess
import webbrowser
from datetime import datetime

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Tab, Tabs

from watchkeep.core.engine import Core
from watchkeep.core.models import State
from watchkeep.core.notify import FAILING as FAILING_EVENT
from watchkeep.core.notify import RECOVERED, Event
from watchkeep.core.panels import Panel
from watchkeep.ui import layout
from watchkeep.ui.forms import AddSourceScreen, ConfirmScreen, EditSourceScreen
from watchkeep.ui.panels import PanelsScreen, new_panel_id

NOTIFICATIONS = "tab-notifications"
ARCHIVE = "tab-archive"
AGENDA = "tab-agenda"
FIXED = {-1: NOTIFICATIONS, -2: ARCHIVE, -3: AGENDA}  # fixed tab index -> tab id
EVENT_LABELS = {FAILING_EVENT: Text("failing", style="bold red"), RECOVERED: Text("recovered", style="green")}


class WatchkeepApp(App):
    TITLE = "watchkeep"
    CSS = "DataTable { height: 1fr; }"
    BINDINGS = [
        # Priority, or the screen's focus_next takes Tab first; check_action hands Tab back to forms.
        Binding("tab", "next_panel", "Next panel", show=False, priority=True),
        Binding("shift+tab", "previous_panel", "Previous panel", show=False, priority=True),
        *(Binding(str(n), f"panel({n - 1})", f"Panel {n}", show=False) for n in range(1, 10)),
        ("p", "panels", "Panels"),
        ("P", "panel_from_group", "Panel from group"),
        ("C", "calendar", "Calendar"),
        ("enter", "open", "Open"),
        ("r", "sync", "Sync now"),
        ("a", "add", "Add source"),
        ("e", "edit", "Edit source"),
        ("g", "grouping", "Group"),
        ("minus", "fold_level", "Fold level"),
        ("plus", "unfold_level", "Unfold level"),
        Binding("equals_sign", "unfold_level", "Unfold level", show=False),  # + without shift
        ("d", "delete", "Delete source"),
        ("i", "ignore", "Ignore"),
        ("m", "mark_read", "Read"),
        ("k", "acknowledge", "Acknowledge"),
        ("c", "close", "Close"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, core: Core, notice: str = "") -> None:
        super().__init__()
        self.core = core
        self._notice = notice
        self._drawn_version = -1
        self._urls: dict[str, str] = {}
        self._rows: dict[str, tuple[str, str, str]] = {}  # row key -> (kind, source id, item id / health key)
        self._last_event = 0
        self._events: list[Event] = []  # this session's, oldest first; the core keeps the last 200
        self._event_urls: dict[str, str] = {}
        self._archived: dict[str, tuple[str, str, str, bool]] = {}  # row key -> (source id, item id, url, read)
        self._agenda_urls: dict[str, str] = {}
        self._before_agenda = 0  # where C goes back to
        self._has_agenda = layout.has_events(core.get_state(), self._plugin_infos())
        self._panels: list[Panel] = core.panels()
        active = core.active_panel()
        self._active = next((i for i, p in enumerate(self._panels) if p.id == active), 0)  # index, or a FIXED key
        levels = dict(layout.PRESETS).get(core.preference("grouping"), layout.PRESETS[0][1])
        self._view = layout.View(levels=levels, depth=len(levels))

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tabs(*self._tabs(), active=self._tab_id(self._active))
        yield DataTable(id="overview", cursor_type="row", show_header=False)
        yield DataTable(id="notifications", cursor_type="row", show_header=False)
        yield DataTable(id="archive", cursor_type="row")
        yield DataTable(id="agenda", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Tabs).can_focus = False  # keys switch panels; the table keeps the cursor
        self._overview.add_columns(*["" for _ in range(layout.VALUE_COLUMNS + 3)])
        self._event_table.add_columns("", "", "", "")
        self._archive_table.add_columns("arrived", "read", "source", "item", "")
        self._agenda_table.add_columns(*layout.AGENDA_COLUMNS)
        self._show_active()
        self.run_worker(self.core.run(), group="core")
        self.set_interval(1, self._poll)
        self.set_interval(30, self._redraw)  # keep "3m ago" fresh
        self._poll()
        if self._notice:
            self.notify(self._notice, timeout=10)

    def _poll(self) -> None:
        events = self.core.events_since(self._last_event)
        for event in events:
            self._last_event = event.seq
            failing = event.kind == "failing"
            self.notify(event.body, title=event.title, severity="error" if failing else "information", timeout=8)
        if events:
            self._events.extend(events)
            self._draw_notifications()
        state = self.core.get_state()
        if state.version != self._drawn_version:
            self._draw(state)
            if self._active == -2:
                self._draw_archive()
            if self._active == -3:
                self._draw_agenda(state)
            if layout.has_events(state, self._plugin_infos()) != self._has_agenda:
                self.call_later(self._toggle_agenda)

    def _redraw(self) -> None:
        state = self.core.get_state()
        self._draw(state)
        if self._active == -3:
            self._draw_agenda(state)  # what is over or happening now moves with the clock

    def _plugin_infos(self) -> dict:
        return {p.name: p for p in self.core.list_plugins()}

    @property
    def _overview(self) -> DataTable:
        return self.query_one("#overview", DataTable)

    @property
    def _event_table(self) -> DataTable:
        return self.query_one("#notifications", DataTable)

    @property
    def _archive_table(self) -> DataTable:
        return self.query_one("#archive", DataTable)

    @property
    def _agenda_table(self) -> DataTable:
        return self.query_one("#agenda", DataTable)

    def _panel(self) -> Panel:
        return self._panels[self._active] if self._active >= 0 else self._panels[0]

    def _draw(self, state: State) -> None:
        self._draw_tabs(state)
        table = self._overview
        selected = None
        if table.row_count:
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

        fixed = layout.WIDTHS["name"] + sum(layout.WIDTHS["value"]) + layout.WIDTHS["note"] + 2 * (layout.VALUE_COLUMNS + 3)
        self._view.title_width = max(20, self.size.width - fixed)
        plugins = {p.name: p for p in self.core.list_plugins()}
        table.clear()
        self._urls, self._rows = {}, {}
        shown = layout.sources_in(self._panel(), state)
        if not state.sources:
            table.add_row(Text(" no sources yet ", style="reverse"), Text("press a to add one", style="dim"), *[""] * (layout.VALUE_COLUMNS + 1), key="none")
        elif not shown.sources:
            table.add_row(Text(f" {self._panel().name} ", style="reverse"), Text(layout.empty_message(self._panel()), style="dim"), *[""] * (layout.VALUE_COLUMNS + 1), key="none")
        for row in layout.build(shown, plugins, self._view):
            table.add_row(*row.cells, key=row.key)
            if row.url:
                self._urls[row.key] = row.url
            if row.ref:
                self._rows[row.key] = ("health" if row.kind in ("health", "source") else row.kind, row.source_id, row.ref)
            elif row.source_id:
                self._rows[row.key] = (row.kind, row.source_id, "")

        # clear() keeps column widths, so they only ever grew. Fit them to these rows now, not on idle:
        # rows drawn before Textual's idle update would be cached at the old widths.
        for column in table.columns.values():
            column.content_width = max((Text(c).cell_len if isinstance(c, str) else c.cell_len for c in table.get_column(column.key)), default=0)

        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected))
            except Exception:
                pass  # row is gone
        self._drawn_version = state.version

    # -- panels ----------------------------------------------------------------

    def _tab_id(self, index: int) -> str:
        return FIXED.get(index, f"tab-{index}")

    def _order(self) -> list[int]:
        """Tab indexes left to right: panels, then Agenda (if any), Notifications, Archive."""
        return [*range(len(self._panels)), *([-3] if self._has_agenda else []), -1, -2]

    def _tabs(self, state: State | None = None) -> list[Tab]:
        tabs = [Tab(self._tab_label(i, p, state), id=self._tab_id(i)) for i, p in enumerate(self._panels)]
        names = {-3: "Agenda", -1: "Notifications", -2: "Archive"}
        return [*tabs, *(Tab(names[i], id=FIXED[i]) for i in self._order() if i < 0)]

    def _tab_label(self, index: int, panel: Panel, state: State | None) -> Text:
        label = Text(f"{index + 1} {panel.name}" if index < 9 else panel.name)
        failing = layout.tab_failing_count(panel, state) if state else 0
        if failing:
            label.append(f" ✗{failing}", style="bold red")  # trouble in a hidden panel stays visible
        return label

    def _draw_tabs(self, state: State) -> None:
        tabs = self.query_one(Tabs)
        for i, panel in enumerate(self._panels):
            try:
                tabs.query_one(f"#{self._tab_id(i)}", Tab).label = self._tab_label(i, panel, state)
            except Exception:
                pass  # tab is being rebuilt

    def _show_active(self) -> None:
        tables = {-1: self._event_table, -2: self._archive_table, -3: self._agenda_table}
        shown = tables.get(self._active, self._overview)
        for table in (self._overview, *tables.values()):
            table.display = table is shown
        shown.focus()
        if self._active == -2:
            self._draw_archive()
        if self._active == -3:
            self._draw_agenda(self.core.get_state())

    def _switch(self, index: int) -> None:
        """Show panel `index` (or a FIXED tab) and remember panels across restarts."""
        self._active = index
        if index >= 0:
            self.core.set_active_panel(self._panels[index].id)
            self._view.folded_groups.clear()  # group keys belong to the panel they were folded in
            self._view.expanded = {}
        self.query_one(Tabs).active = self._tab_id(index)
        self._show_active()
        self._redraw()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id or ""
        index = {v: k for k, v in FIXED.items()}.get(tab_id)
        if index is None:
            index = int(tab_id.removeprefix("tab-"))
        if index != self._active:
            self._switch(index)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("next_panel", "previous_panel", "panel", "panel_from_group", "panels", "calendar"):
            return len(self.screen_stack) == 1  # in a form, Tab moves between fields and keys type
        return True

    def action_panel(self, index: int) -> None:
        if index < len(self._panels):
            self._switch(index)

    def _step(self, by: int) -> None:
        order = self._order()
        self._switch(order[(order.index(self._active) + by) % len(order)])

    def action_calendar(self) -> None:
        """C: jump to the Agenda, or back to where you were if already there."""
        if self._active == -3:
            self._switch(self._before_agenda if self._before_agenda in self._order() else 0)
            return
        if not self._has_agenda:
            self.notify("No calendar yet: press a to add one", severity="warning")
            return
        self._before_agenda = self._active
        self._switch(-3)

    def action_next_panel(self) -> None:
        self._step(1)

    def action_previous_panel(self) -> None:
        self._step(-1)

    async def action_panel_from_group(self) -> None:
        table = self._overview
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value if table.row_count and self._active >= 0 else ""
        plugins = {p.name: p for p in self.core.list_plugins()}
        made = layout.filter_from_group(key or "", self._view.levels, plugins)
        if made is None:
            self.notify("Move to a client, environment or kind header first (not a 'No …' one)", severity="warning")
            return
        name, panel_filter = made
        panel_id = new_panel_id(self._panels)
        self.core.save_panels([*self._panels, Panel(panel_id, name, panel_filter)])
        await self._reload_panels(panel_id)
        self.notify(f"Panel '{name}' added")

    def action_panels(self) -> None:
        current = self._panel().id if self._active >= 0 else None

        async def saved(yes: bool | None) -> None:
            if yes:
                await self._reload_panels(current)  # stays on the open panel, or "All" if it was deleted

        self.push_screen(PanelsScreen(self.core), lambda yes: self.call_later(saved, yes))

    async def _reload_panels(self, show: str | None) -> None:
        """Re-read panels after a save and show panel `show` (None: Notifications)."""
        self._panels = self.core.panels()
        index = -1 if show is None else next((i for i, p in enumerate(self._panels) if p.id == show), 0)
        await self._rebuild_tabs(index)

    async def _toggle_agenda(self) -> None:
        """The first events source was added, or the last one removed: show or drop the Agenda tab."""
        has = layout.has_events(self.core.get_state(), self._plugin_infos())
        if has == self._has_agenda:
            return
        self._has_agenda = has
        await self._rebuild_tabs(0 if self._active == -3 and not has else self._active)

    async def _rebuild_tabs(self, index: int) -> None:
        # A fresh strip, born on the right tab: rebuilding in place re-activates the first tab.
        await self.query_one(Tabs).remove()
        tabs = Tabs(*self._tabs(self.core.get_state()), active=self._tab_id(index))
        tabs.can_focus = False
        await self.mount(tabs, before=self._overview)
        self._switch(index)

    # -- notifications -----------------------------------------------------------

    def _draw_notifications(self) -> None:
        table = self._event_table
        table.clear()
        self._event_urls = {}
        for event in reversed(self._events):
            key = f"ev:{event.seq}"
            when = datetime.fromtimestamp(event.at).strftime("%H:%M")
            kind = EVENT_LABELS.get(event.kind, Text("new", style="yellow"))
            table.add_row(Text(when, style="dim"), kind, Text(event.body), Text(event.source_name, style="dim"), key=key)
            if event.url:
                self._event_urls[key] = event.url

    # -- agenda ----------------------------------------------------------------

    def _draw_agenda(self, state: State) -> None:
        table = self._agenda_table
        selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value if table.row_count else None
        table.clear()
        fixed = sum(layout.AGENDA_WIDTHS.values()) + 2 * len(layout.AGENDA_COLUMNS)
        rows = layout.agenda(state, self._plugin_infos(), title_width=max(20, self.size.width - fixed))
        self._agenda_urls = {r.key: r.url for r in rows if r.url}
        if not rows:
            table.add_row(Text(" nothing on ", style="reverse"), "", Text("no appointments left today", style="dim"), "", "", key="none")
        for row in rows:
            table.add_row(*row.cells, key=row.key)
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected))
            except Exception:
                pass  # over and gone

    # -- archive ---------------------------------------------------------------

    def _draw_archive(self) -> None:
        """Every item that has come through an items source, newest first, read or not."""
        table = self._archive_table
        selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value if table.row_count else None
        table.clear()
        self._archived = {}
        for entry in self.core.archive():
            key = f"ar:{entry.source_id}\x1f{entry.item.id}"
            read = Text(_when(entry.read_at), style="green") if entry.read_at else Text("unread", style="bold yellow")
            title = Text(entry.item.title, style="" if entry.present else "dim")
            gone = Text("" if entry.present else "gone from source", style="dim")
            table.add_row(Text(_when(entry.first_seen), style="dim"), read, Text(entry.source_name, style="dim"), title, gone, key=key)
            self._archived[key] = (entry.source_id, entry.item.id, entry.item.url, entry.read_at is not None)
        if selected in self._archived:
            table.move_cursor(row=table.get_row_index(selected))

    def _selected_archived(self) -> tuple[str, str, str, bool] | None:
        table = self._archive_table
        if not table.row_count:
            return None
        return self._archived.get(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or "")

    # -- rows ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if event.data_table.id == "notifications":
            self._browse(self._event_urls.get(key))
        elif event.data_table.id == "archive":
            self._browse(self._archived[key][2] if key in self._archived else None)
        elif event.data_table.id == "agenda":
            self._browse(self._agenda_urls.get(key))
        else:
            self._open(key)

    def action_open(self) -> None:
        if self._active == -2:
            row = self._selected_archived()
            self._browse(row[2] if row else None)
            return
        if self._active == -3:
            table = self._agenda_table
            if table.row_count:
                self._browse(self._agenda_urls.get(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or ""))
            return
        table = self._event_table if self._active < 0 else self._overview
        if not table.row_count:
            return
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if self._active < 0:
            self._browse(self._event_urls.get(key or ""))
        else:
            self._open(key)

    def _open(self, key: str | None) -> None:
        key = key or ""
        if key.startswith("grp:"):
            self._view.folded_groups ^= {key}
            self._redraw()
            return
        if key.startswith(("hdr:", "more:")) and key not in self._urls:
            source_id = key.split(":", 1)[1]
            source = next((s for s in self.core.get_state().sources if s.id == source_id), None)
            if source is not None:
                default = not source.health
                self._view.expanded[source_id] = not self._view.expanded.get(source_id, default)
                self._redraw()
            return
        self._browse(self._urls.get(key))

    def _browse(self, url: str | None) -> None:
        if not url:
            return
        if shutil.which("xdg-open"):
            # Keep the browser's output from scribbling over the TUI.
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)
        self.notify(url, title="Opened")

    def _selected_source(self) -> str | None:
        row = self._selected_row()
        return row[1] if row else None

    def action_add(self) -> None:
        def added(source_ids: list[str] | None) -> None:
            for source_id in source_ids or []:
                self.run_worker(self.core.sync_now(source_id), group="sync")
            if source_ids:
                self.notify(f"Added {', '.join(source_ids)}, syncing")

        self.push_screen(AddSourceScreen(self.core), added)

    def action_edit(self) -> None:
        source_id = self._selected_source()
        if source_id is None:
            return
        try:
            screen = EditSourceScreen(self.core, source_id)
        except ValueError as exc:
            self.notify(str(exc), severity="warning")
            return

        def saved(yes: bool | None) -> None:
            if yes:
                self.notify("Saved. Applies from the next sync.")

        self.push_screen(screen, saved)

    def action_delete(self) -> None:
        source_id = self._selected_source()
        if source_id is None:
            return
        name = next((s.name for s in self.core.get_state().sources if s.id == source_id), source_id)

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.core.remove_source(source_id)
                self.notify(f"Deleted {name}")

        self.push_screen(ConfirmScreen(f"Delete source '{name}' and its history?"), confirmed)

    def _selected_row(self) -> tuple[str, str, str] | None:
        table = self._overview
        if self._active < 0 or not table.row_count:
            return None
        return self._rows.get(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or "")

    def _row_action(self, kind: str, action, done: str) -> None:
        row = self._selected_row()
        if row is None or row[0] != kind:
            self.notify(f"Select {'an item (a post or GitHub item)' if kind == 'item' else 'a check (queue or website)'} row first", severity="warning")
            return
        try:
            action(row[1], row[2])
        except ValueError as exc:
            self.notify(str(exc), severity="warning")
            return
        self.notify(done)

    def action_ignore(self) -> None:
        self._row_action("item", self.core.ignore_item, "Ignored. Hidden until it's gone from the source")

    def action_mark_read(self) -> None:
        if self._active != -2:
            self._row_action("item", self.core.mark_read, "Read. It's in the Archive tab")
            return
        row = self._selected_archived()  # in the archive, m flips read and unread
        if row is None:
            return
        source_id, item_id, _, read = row
        (self.core.mark_unread if read else self.core.mark_read)(source_id, item_id)
        self.notify("Marked unread" if read else "Marked read", timeout=2)

    def action_acknowledge(self) -> None:
        self._row_action("health", self.core.acknowledge, "Acknowledged. Press c once it has recovered")

    def action_close(self) -> None:
        self._row_action("health", self.core.close, "Closed")

    def action_grouping(self) -> None:
        names = [name for name, _ in layout.PRESETS]
        current = next((name for name, levels in layout.PRESETS if levels == self._view.levels), names[0])
        name = names[(names.index(current) + 1) % len(names)]
        self._view.levels = dict(layout.PRESETS)[name]
        self._view.folded_groups.clear()
        self._view.expanded = {}
        self._view.depth = len(self._view.levels)
        self.core.set_preference("grouping", name)
        self._redraw()
        self.notify(f"Grouped by {name}")

    def action_fold_level(self) -> None:
        self._set_depth(self._view.depth - 1)

    def action_unfold_level(self) -> None:
        self._set_depth(self._view.depth + 1)

    def _set_depth(self, depth: int) -> None:
        """Show the overview down to `depth`: one grouping level per step, then sources, then everything."""
        levels = self._view.levels
        depth = max(0, min(depth, len(levels) + 1))
        state = layout.sources_in(self._panel(), self.core.get_state())
        plugins = {p.name: p for p in self.core.list_plugins()}
        self._view.depth = depth
        self._view.folded_groups = {
            row.key
            for row in layout.build(state, plugins, layout.View(levels=levels))
            if row.kind == "group" and row.key.count("\x1f") >= depth  # a group's level is its depth in the path
        }
        if depth > len(levels):
            self._view.expanded = {s.id: True for s in state.sources}
        else:
            self._view.expanded = {}  # sources as usual: healthy on one line, problems shown
        self._redraw()
        self.notify(layout.depth_name(levels, depth), timeout=2)

    def action_sync(self) -> None:
        self.run_worker(self.core.sync_now(), group="sync")
        self.notify("Syncing all sources")


def _when(at: float) -> str:
    """14:05 today, else 'Oct 27 14:05'."""
    moment = datetime.fromtimestamp(at)
    return moment.strftime("%H:%M" if moment.date() == datetime.now().date() else "%b %d %H:%M")
