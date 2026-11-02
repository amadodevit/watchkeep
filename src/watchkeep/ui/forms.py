"""Modal screens.

Adding a source is a short wizard: one decision per step, and each step is
derived from the plugin's declaration, so new plugins get it for free.

    type ──► found (only if the plugin can detect) ──► watch ──► Add
                  └──► manual (nothing found, or "Enter manually…") ──┘
"""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Checkbox, Collapsible, Input, Label, OptionList, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from watchkeep.core.checks import Check
from watchkeep.core.config import parse_interval
from watchkeep.core.engine import Core, DetectedSource, PluginInfo
from watchkeep.core.health import Rule

DIALOG_CSS = """
#dialog {
    width: 72;
    height: auto;
    max-height: 90%;
    border: thick $accent;
    background: $surface;
    padding: 1 2;
}
#dialog #title { text-style: bold; margin-bottom: 1; }
#dialog #body { height: auto; }
#dialog .field { margin-top: 1; text-style: bold; }
#dialog .help { color: $text-muted; }
#dialog OptionList, #dialog SelectionList { height: auto; max-height: 12; }
#dialog Collapsible { margin-top: 1; }
#dialog .rule { height: auto; }
#dialog .rule Input { width: 8; }
#dialog .rule Static { width: auto; padding: 1 1 0 1; }
#dialog .check { height: auto; }
#dialog .check Checkbox { border: none; padding: 0; }
#dialog .check .rule { padding-left: 4; }
#dialog .check .rule Input { width: 10; }
#dialog .fetch { height: auto; }
#dialog .fetch Input { width: 1fr; }
#dialog #result { margin-top: 1; height: auto; }
#dialog #buttons { height: auto; margin-top: 1; }
#dialog Button { margin-right: 1; }
"""


class SourceFields:
    """Field behaviour shared by the add and edit screens."""

    core: Core
    plugin: PluginInfo | None

    def _fetch_settings(self) -> tuple[dict[str, str], str | None]:
        """Settings typed so far, and the source id when editing (for its stored secrets)."""
        raise NotImplementedError

    async def _fetch(self, key: str) -> None:
        """Ask the plugin for values to pick from; ticking one adds it to the field."""
        assert self.plugin is not None
        self.query_one("#result", Static).update(Text("Fetching…", style="italic"))
        settings, source_id = self._fetch_settings()
        try:
            values = await asyncio.to_thread(self.core.setting_options, self.plugin.name, settings, key, source_id)
        except Exception as exc:
            self._error(str(exc) or type(exc).__name__)
            return
        self.query_one("#result", Static).update(Text(f"{len(values)} found. Tick to add, or type a pattern.", style="dim"))
        for old in self.query(f"#options-{key}"):
            await old.remove()
        typed = set(_split(self.query_one(f"#setting-{key}", Input).value))
        picker = SelectionList(*((v, v, v in typed) for v in values), id=f"options-{key}")
        row = self.query_one(f"#fetch-{key}").parent
        await row.parent.mount(picker, after=row)
        picker.focus()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        list_id = event.selection_list.id or ""
        if not list_id.startswith("options-"):
            return
        field = self.query_one(f"#setting-{list_id.removeprefix('options-')}", Input)
        offered = {event.selection_list.get_option_at_index(i).value for i in range(event.selection_list.option_count)}
        patterns = [v for v in _split(field.value) if v not in offered]  # keep what was typed by hand
        field.value = ", ".join(patterns + list(event.selection_list.selected))

    def _error(self, message: str) -> None:
        self.query_one("#result", Static).update(Text(f"✗ {message}", style="bold red"))


class AddSourceScreen(SourceFields, ModalScreen[list[str]]):
    """Returns the ids of the sources added (empty if cancelled)."""

    DEFAULT_CSS = "AddSourceScreen { align: center middle; }\n" + DIALOG_CSS
    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, core: Core) -> None:
        super().__init__()
        self.core = core
        self.plugins = {p.name: p for p in core.list_plugins()}
        self.plugin: PluginInfo | None = None
        self.step = ""
        self._history: list[str] = []
        self._detected: list[DetectedSource] = []
        self._chosen: list[DetectedSource] = []  # from the "found" step
        self._manual: dict[str, str] | None = None  # from the "manual" step

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static("", id="title")
            yield Vertical(id="body")
            yield Static("", id="result")
            yield Horizontal(id="buttons")

    async def on_mount(self) -> None:
        await self._go("type", remember=False)

    # -- navigation -------------------------------------------------------------

    async def _go(self, step: str, *, remember: bool = True, note: str = "") -> None:
        if remember and self.step:
            self._history.append(self.step)
        self.step = step
        body, buttons = self.query_one("#body", Vertical), self.query_one("#buttons", Horizontal)
        await body.remove_children()
        await buttons.remove_children()
        self.query_one("#result", Static).update(Text(note, style="dim") if note else "")
        await getattr(self, f"_step_{step}")(body, buttons)

    async def action_back(self) -> None:
        if not self._history:
            self.dismiss([])
            return
        self.step = ""  # don't push the step we're leaving
        await self._go(self._history.pop(), remember=False)

    # -- steps ------------------------------------------------------------------

    async def _step_type(self, body: Vertical, buttons: Horizontal) -> None:
        self._title("What do you want to monitor?")
        options = OptionList(*(Option(name, id=name) for name in self.plugins), id="types")
        await body.mount(options)
        options.focus()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "types":
            return
        self.plugin = self.plugins[event.option.id]
        self._chosen, self._manual = [], None
        if self.plugin.can_detect:
            await self._go("found")
        elif self._identity_settings():
            await self._go("manual")
        else:
            await self._go("watch")

    async def _step_found(self, body: Vertical, buttons: Horizontal) -> None:
        self._title("Found on this machine")
        await body.mount(Static("Looking…", classes="help"))
        self.run_worker(self._detect(), group="detect", exclusive=True)

    async def _detect(self) -> None:
        assert self.plugin is not None
        try:
            found = await asyncio.to_thread(self.core.detect_sources, self.plugin.name)
        except Exception as exc:
            await self._go("manual", remember=False, note=f"Detection failed ({exc}). Enter the details instead.")
            return
        if self.step != "found":
            return  # user went back meanwhile
        if not found:
            await self._go("manual", remember=False, note="Nothing found on this machine. Enter the details instead.")
            return
        self._detected = list(found)
        body, buttons = self.query_one("#body", Vertical), self.query_one("#buttons", Horizontal)
        await body.remove_children()
        selections = []
        for n, candidate in enumerate(found):
            label = Text(candidate.name)
            if candidate.existing_id:
                label.append("  already added", style="dim")
            elif candidate.description:
                label.append(f"  {candidate.description}", style="dim")
            selections.append(Selection(label, n, False, disabled=bool(candidate.existing_id)))
        accounts = SelectionList(*selections, id="accounts")
        await body.mount(accounts)
        await buttons.mount_all([Button("Next", id="next-found", variant="primary"), Button("Enter manually…", id="manual")])
        accounts.focus()

    async def _step_manual(self, body: Vertical, buttons: Horizontal) -> None:
        self._title("Enter the details")
        widgets: list = []
        for setting in self._identity_settings():
            widgets.append(Label(f"{setting.label}{' *' if setting.required else ''}", classes="field"))
            field = Input(id=f"setting-{setting.key}", password=setting.secret, placeholder=setting.default or "")
            if setting.fetch:
                field = Horizontal(field, Button("Fetch", id=f"fetch-{setting.key}"), classes="fetch")
            widgets.append(field)
            if setting.help:
                widgets.append(Static(setting.help, classes="help"))
        await body.mount_all(widgets)
        assert self.plugin is not None
        actions = [Button("Next", id="next-manual", variant="primary")]
        if self.plugin.can_test:
            actions.append(Button("Test", id="test"))
        await buttons.mount_all(actions)
        if widgets:
            self.query(Input).first().focus()

    async def _step_watch(self, body: Vertical, buttons: Horizontal) -> None:
        assert self.plugin is not None
        choice_settings = [s for s in self.plugin.settings if s.choices]
        rule = self.plugin.default_rule
        self._title("What to watch?" if choice_settings else "When is it failing?" if rule else "Almost done")
        widgets: list = []
        if len(self._chosen) <= 1:  # several detected accounts keep their own names
            suggested = self._chosen[0].name if self._chosen else self._manual_name() if self._manual is not None else self.plugin.title
            widgets += [Label("Name *", classes="field"), Input(suggested, id="name", placeholder="e.g. Prod broker")]
        widgets += _placement_widgets(self.core)
        widgets += _check_widgets(self.plugin.default_checks, self.plugin.metrics)
        if rule:
            widgets += _rule_widgets(rule)
        for setting in choice_settings:
            if len(choice_settings) > 1:
                widgets.append(Label(setting.label, classes="field"))
            widgets.append(
                SelectionList(*((c.label, c.value, c.default) for c in setting.choices), id=f"setting-{setting.key}")
            )
        for setting in self._watch_fields():
            widgets.append(Label(setting.label, classes="field"))
            field = Input(id=f"setting-{setting.key}", placeholder=setting.default or "")
            widgets.append(Horizontal(field, Button("Fetch", id=f"fetch-{setting.key}"), classes="fetch"))
            if setting.help:
                widgets.append(Static(setting.help, classes="help"))
        widgets.append(
            Collapsible(
                Label("Sync interval", classes="field"),
                Input(id="interval", placeholder=f"{_duration(self.plugin.default_interval)}   (seconds, or e.g. 30m, 1h)"),
                Label("Tags", classes="field"),
                Input(id="tags", placeholder="comma separated, e.g. work, personal"),
                title="More options",
                collapsed=True,
            )
        )
        await body.mount_all(widgets)
        count = len(self._chosen) or 1
        await buttons.mount(Button(f"Add {count} source{'s' * (count != 1)}" if count > 1 else "Add", id="add", variant="primary"))
        if choice_settings:
            self.query(SelectionList).first().focus()
        elif rule:
            self.query_one("#add", Button).focus()

    # -- buttons ----------------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "manual":
                await self._go("manual")
            case "next-found":
                ticked = self.query_one("#accounts", SelectionList).selected
                if not ticked:
                    self._error("Tick at least one account.")
                    return
                self._chosen, self._manual = [self._detected[n] for n in ticked], None
                await self._go("watch")
            case "next-manual":
                values = self._manual_values()
                missing = [s.label for s in self._identity_settings() if s.required and not values.get(s.key)]
                if missing:
                    self._error(f"missing: {', '.join(missing)}")
                    return
                self._chosen, self._manual = [], values
                await self._go("watch")
            case "test":
                await self._test()
            case str(button) if button.startswith("fetch-"):
                self.run_worker(self._fetch(button.removeprefix("fetch-")), group="fetch", exclusive=True)
            case "add":
                self._add()

    async def _test(self) -> None:
        assert self.plugin is not None
        self.query_one("#result", Static).update(Text("Testing…", style="italic"))
        try:
            message = await asyncio.to_thread(self.core.test_settings, self.plugin.name, self._manual_values())
        except Exception as exc:
            self._error(str(exc) or type(exc).__name__)
        else:
            self.query_one("#result", Static).update(Text(f"✓ {message}", style="green"))

    def _add(self) -> None:
        assert self.plugin is not None
        try:
            raw = self.query_one("#interval", Input).value.strip()
            interval = parse_interval(raw) if raw else self.plugin.default_interval
            rule = _read_rule(self) if self.plugin.default_rule else None
            checks = _read_checks(self, self.plugin.default_checks) if self.plugin.default_checks else None
        except ValueError as exc:
            self._error(str(exc) if "interval" in str(exc) else "rule and check values must be numbers")
            return
        tags = tuple(t.strip() for t in self.query_one("#tags", Input).value.split(",") if t.strip())
        choices = {
            s.key: list(self.query_one(f"#setting-{s.key}", SelectionList).selected)
            for s in self.plugin.settings
            if s.choices
        }
        choices.update(self._watch_values())
        names = self.query("#name")
        typed = names.first(Input).value.strip() if names else ""
        if self._manual is not None:
            targets = [(typed or self._manual_name(), {**self._manual, **choices})]
        elif self._chosen:
            targets = [(typed if len(self._chosen) == 1 and typed else c.name, {**c.settings, **choices}) for c in self._chosen]
        else:
            targets = [(typed or self.plugin.title, choices)]  # plugin needs no identity at all
        client, environment = _placement(self)

        added: list[str] = []
        for name, settings in targets:
            try:
                added.append(
                    self.core.add_source(
                        self.plugin.name, name, settings, interval=interval, tags=tags, rule=rule, checks=checks,
                        client=client, environment=environment,
                    )
                )
            except ValueError as exc:
                if added:
                    self.notify(f"Added {', '.join(added)} before the error", severity="warning")
                self._error(f"{name}: {exc}")
                return
        self.dismiss(added)

    # -- helpers ----------------------------------------------------------------

    def _identity_settings(self):
        return [s for s in self.plugin.settings if not s.choices] if self.plugin else []

    def _watch_fields(self):
        """Optional picker fields (e.g. GitHub repos) the manual step would have shown.

        A detected source skips the manual step, so they go in the watch step instead.
        """
        if not self.plugin or self._manual is not None:
            return []
        return [s for s in self.plugin.settings if s.fetch and not s.choices and not s.required and not s.secret]

    def _watch_values(self) -> dict[str, str]:
        return {s.key: self.query_one(f"#setting-{s.key}", Input).value.strip() for s in self._watch_fields()}

    def _manual_values(self) -> dict[str, str]:
        return {s.key: self.query_one(f"#setting-{s.key}", Input).value.strip() for s in self._identity_settings()}

    def _manual_name(self) -> str:
        assert self.plugin is not None and self._manual is not None
        first = next((self._manual.get(s.key) for s in self._identity_settings() if not s.secret), None)
        return first or self.plugin.name

    def _title(self, text: str) -> None:
        self.query_one("#title", Static).update(Text(text))

    def _fetch_settings(self) -> tuple[dict[str, str], str | None]:
        if self.step == "watch":
            base = dict(self._chosen[0].settings) if self._chosen else dict(self._manual or {})
            return {**base, **self._watch_values()}, None
        return self._manual_values(), None


def _placement_widgets(core: Core, client: str = "", environment: str = "") -> list:
    """Client and environment: what the overview groups by. Suggests values already in use."""
    sources = core.get_state().sources
    clients = sorted({s.client for s in sources if s.client})
    environments = sorted({s.environment for s in sources if s.environment} | {"prod", "staging", "local"})
    return [
        Label("Client", classes="field"),
        Input(client, id="client", placeholder=", ".join(clients) or "e.g. acme, personal",
              suggester=SuggestFromList(clients, case_sensitive=False)),
        Label("Environment", classes="field"),
        Input(environment, id="environment", placeholder="e.g. prod, staging",
              suggester=SuggestFromList(environments, case_sensitive=False)),
    ]


def _placement(screen) -> tuple[str, str]:
    return screen.query_one("#client", Input).value.strip(), screen.query_one("#environment", Input).value.strip()


def _check_widgets(checks, metrics) -> list:
    if not checks:
        return []
    by_key = {m.key: m for m in metrics}
    widgets: list = [Label("Bad check when", classes="field")]
    for check in checks:
        row: list = []
        for n, condition in enumerate(check.conditions):
            metric = by_key[condition.metric]
            name = f"{metric.label.lower()}{' per sync' if metric.counter else ''}"
            row.append(Static(f"{'and ' if n else ''}{name} {condition.op}"))
            row.append(Input(f"{condition.value:g}", id=f"check-{check.id}-{n}", type="number"))
        widgets.append(
            Vertical(Checkbox(check.label, check.enabled, id=f"check-{check.id}"), Horizontal(*row, classes="rule"), classes="check")
        )
    return widgets


def _read_checks(screen, checks) -> list[dict]:
    result = []
    for check in checks:
        data = check.to_dict()
        data["enabled"] = screen.query_one(f"#check-{check.id}", Checkbox).value
        data["conditions"] = [
            [m, op, float(screen.query_one(f"#check-{check.id}-{n}", Input).value)]
            for n, (m, op, _) in enumerate(data["conditions"])
        ]
        result.append(data)
    return result


def _rule_widgets(rule) -> list:
    return [
        Label("Failing when", classes="field"),
        Horizontal(
            Input(str(rule.fail_after), id="rule-fail", type="integer"),
            Static("of the last"),
            Input(str(rule.window), id="rule-window", type="integer"),
            Static("checks fail"),
            classes="rule",
        ),
        Label("Recovered after", classes="field"),
        Horizontal(
            Input(str(rule.recover_after), id="rule-recover", type="integer"),
            Static("good checks in a row"),
            classes="rule",
        ),
    ]


def _read_rule(screen) -> dict[str, int]:
    return {
        "fail_after": int(screen.query_one("#rule-fail", Input).value),
        "window": int(screen.query_one("#rule-window", Input).value),
        "recover_after": int(screen.query_one("#rule-recover", Input).value),
    }


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _duration(seconds: int) -> str:
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class EditSourceScreen(SourceFields, ModalScreen[bool]):
    """Everything about a source on one screen. Returns True when saved.

    Identity settings are shown but can't change; secrets are never shown,
    and leaving one empty keeps the stored value.
    """

    DEFAULT_CSS = "EditSourceScreen { align: center middle; }\n" + DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "save", "Save")]

    def __init__(self, core: Core, source_id: str) -> None:
        super().__init__()
        self.core = core
        self.source_id = source_id
        self.editable = core.editable(source_id)
        self.plugin = self.editable.plugin

    def compose(self) -> ComposeResult:
        source, plugin = self.editable.source, self.plugin
        with VerticalScroll(id="dialog"):
            yield Static(Text(f"Edit {source.name}"), id="title")
            with Vertical(id="body"):
                yield Label("Name *", classes="field")
                yield Input(source.name, id="name")
                yield from _placement_widgets(self.core, source.client, source.environment)
                for setting in plugin.settings:
                    yield from self._setting_widgets(setting, source.settings.get(setting.key))
                if source.checks is not None:
                    yield from _check_widgets(self._checks(), plugin.metrics)
                if source.rule:
                    yield from _rule_widgets(Rule.from_dict(source.rule))
                yield Label("Sync interval", classes="field")
                yield Input(_duration(source.interval), id="interval", placeholder="seconds, or e.g. 30m, 1h")
                yield Label("Tags", classes="field")
                yield Input(", ".join(source.tags), id="tags", placeholder="comma separated, e.g. work, personal")
            yield Static("", id="result")
            with Horizontal(id="buttons"):
                yield Button("Save (ctrl+s)", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    def _setting_widgets(self, setting, value):
        if setting.key in self.editable.identity:
            yield Label(setting.label, classes="field")
            yield Static(Text(f"{value}  (can't change; add a new source instead)", style="dim"))
            return
        if setting.choices:
            yield Label(setting.label, classes="field")
            chosen = set(value or ())
            yield SelectionList(*((c.label, c.value, c.value in chosen) for c in setting.choices), id=f"setting-{setting.key}")
            return
        yield Label(setting.label + (" *" if setting.required else ""), classes="field")
        if setting.secret:
            stored = setting.key in self.editable.stored_secrets
            field = Input(
                "" if stored else str(value or ""),
                id=f"setting-{setting.key}",
                password=True,
                placeholder="unchanged; type to replace" if stored else setting.default or "",
            )
        else:
            field = Input(str(value or ""), id=f"setting-{setting.key}", placeholder=setting.default or "")
        yield Horizontal(field, Button("Fetch", id=f"fetch-{setting.key}"), classes="fetch") if setting.fetch else field
        if setting.help:
            yield Static(setting.help, classes="help")

    def _checks(self):
        return [Check.from_dict(c) for c in self.editable.source.checks or ()]

    def _values(self) -> dict:
        values: dict = {}
        for setting in self.plugin.settings:
            if setting.key in self.editable.identity:
                continue
            if setting.choices:
                values[setting.key] = list(self.query_one(f"#setting-{setting.key}", SelectionList).selected)
            else:
                values[setting.key] = self.query_one(f"#setting-{setting.key}", Input).value.strip()
        return values

    def _fetch_settings(self) -> tuple[dict[str, str], str | None]:
        return self._values(), self.source_id

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "save":
                self._save()
            case "cancel":
                self.dismiss(False)
            case str(button) if button.startswith("fetch-"):
                self.run_worker(self._fetch(button.removeprefix("fetch-")), group="fetch", exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_save(self) -> None:
        self._save()

    def _save(self) -> None:
        source = self.editable.source
        try:
            raw = self.query_one("#interval", Input).value.strip()
            interval = parse_interval(raw) if raw else source.interval
            rule = _read_rule(self) if source.rule else None
            checks = _read_checks(self, self._checks()) if source.checks is not None else None
        except ValueError as exc:
            self._error(str(exc) if "interval" in str(exc) else "rule and check values must be numbers")
            return
        tags = tuple(_split(self.query_one("#tags", Input).value))
        try:
            self.core.update_source(
                self.source_id,
                name=self.query_one("#name", Input).value,
                settings=self._values(),
                client=_placement(self)[0],
                environment=_placement(self)[1],
                interval=interval,
                tags=tags,
                rule=rule,
                checks=checks,
            )
        except ValueError as exc:
            self._error(str(exc))
            return
        self.dismiss(True)


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = "ConfirmScreen { align: center middle; }\n" + DIALOG_CSS.replace("width: 72", "width: 60")
    BINDINGS = [("y", "answer(True)", "Yes"), ("n", "answer(False)", "No"), ("escape", "answer(False)", "No")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.question))
            with Horizontal(id="buttons"):
                yield Button("Delete (y)", id="yes", variant="error")
                yield Button("Keep (n)", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)
