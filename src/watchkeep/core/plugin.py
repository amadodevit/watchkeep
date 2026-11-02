"""The contract a source plugin implements.

A plugin declares what it needs (name, connection type, settings) and returns a
full snapshot of what is currently true on each sync. It knows nothing about
storage, history, lifecycle or notifications: the core owns those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .connections import HttpConnection
from .models import Item


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    default: bool = True


@dataclass(frozen=True)
class Setting:
    """One field a user fills in. The add-source form is generated from these.

    With `choices`, the setting is a multi-select and its value is a list of
    the ticked `Choice.value`s.
    """

    key: str
    label: str
    required: bool = True
    secret: bool = False  # masked in forms; "gh:<user>" / "env:VAR" references resolved before sync
    default: str | None = None
    help: str = ""
    choices: tuple[Choice, ...] = ()
    fetch: bool = False  # the form can ask the plugin's options(ctx, key) for values to pick from


@dataclass(frozen=True)
class Metric:
    """A number a samples plugin reports in `Sample.values`. Rules (checks) are written against these."""

    key: str
    label: str
    counter: bool = False  # only goes up (e.g. messages dequeued since start); checks see its growth per sync
    short: str = ""  # name in rows, e.g. "+120 in"; defaults to the label


@dataclass(frozen=True)
class Candidate:
    """A source a plugin found on this machine, ready to add."""

    name: str
    settings: dict[str, Any]
    description: str = ""


@dataclass
class SyncContext:
    """What a plugin gets on each call.

    `memory` is the plugin's own state for this source, kept by the core between
    syncs: change it in place. The core saves it only after a sync that returns;
    if sync raises, the next sync starts from the memory before. It must stay
    JSON-friendly (dicts, lists, strings, numbers) and never hold secrets.

    `warnings` are partial failures worth showing without failing the sync,
    e.g. one feed out of five unreachable. Short, plain English, no secrets.

    `quiet` holds ids of items to show without a notification when they are new,
    e.g. what was already there when a repo was first watched. Only this sync.
    """

    settings: dict[str, Any]
    http: HttpConnection
    extra: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    quiet: set[str] = field(default_factory=set)


@runtime_checkable
class SourcePlugin(Protocol):
    name: str
    connection: str  # connection type the core must provide, e.g. "http"
    settings: tuple[Setting, ...]

    def sync(self, ctx: SyncContext) -> list[Item]:
        """Return everything currently true. Items missing from the result are resolved."""
        ...

    # Optional members (checked with hasattr/getattr, so not part of the Protocol check):
    #
    #   identity: tuple[str, ...]
    #       Setting keys that identify a source, e.g. ("account",). Lets the core
    #       tell that a detected candidate is already added.
    #
    #   def test_connection(self, ctx: SyncContext) -> str
    #       Return a short description on success, raise on failure.
    #
    #   def detect(self) -> list[Candidate]
    #       Look for sources on this machine (e.g. logged-in CLIs). Never raise
    #       for "nothing found"; return [].
    #
    #   def options(self, ctx: SyncContext, key: str) -> list[str]
    #       Values to pick from for a Setting with fetch=True, e.g. queue names.
    #
    #   metrics: tuple[Metric, ...]          samples plugins: the numbers in Sample.values
    #   default_checks: tuple[Check, ...]    samples plugins: editable checks on those metrics
