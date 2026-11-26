"""Command source: items from any local program that prints them as JSON.

For things watchkeep has no plugin for, when a script of yours already knows the answer:
a mail triage tool, a backup check, a to-do exporter. Each sync runs the program with its
arguments, without a shell and with no input, and reads a JSON list from its standard output:

    [{"id": "a1", "title": "Invoice due", "url": "https://...", "action": "money",
      "author": "Utility Co", "repo": "", "updated_at": "2026-01-01T00:00:00+00:00"}]

`id` and `title` are required; the other fields default to "". As with every items source,
`id` must stay the same for the same thing, and a changed `updated_at` means it moved.

A non-zero exit, a timeout or output that isn't such a list fails the sync, showing the last
line the program wrote to stderr. That way a broken script shows as an error on its row
instead of quietly looking like "nothing to do".
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from watchkeep.core.models import Item
from watchkeep.core.plugin import Setting, SyncContext

DEFAULT_TIMEOUT = 60  # seconds
MAX_ITEMS = 500
MAX_OUTPUT = 2_000_000  # characters of stdout
FIELDS = ("action", "url", "repo", "author", "updated_at")


class CommandSource:
    name = "command"
    title = "Command"
    connection = "http"  # every plugin names one; this one doesn't use it
    kind = "items"
    default_interval = 300  # 5m
    settings = (
        Setting("command", "Program", help="full path, e.g. /usr/local/bin/my-check"),
        Setting(
            "args",
            "Arguments",
            required=False,
            default="",
            help="split like a shell would (quotes work), but no shell runs: --account work report",
        ),
        Setting("timeout", "Timeout (seconds)", required=False, default=str(DEFAULT_TIMEOUT)),
    )

    def sync(self, ctx: SyncContext) -> list[Item]:
        return parse(run(ctx.settings))

    def test_connection(self, ctx: SyncContext) -> str:
        n = len(self.sync(ctx))
        return f"{n} item" + ("" if n == 1 else "s")


def run(settings: dict[str, Any], runner=subprocess.run) -> str:
    """Run the program and return its stdout. Raises with a short reason on failure."""
    program = str(settings.get("command") or "").strip()
    if not Path(program).is_absolute():
        raise ValueError("Program must be a full path, e.g. /usr/local/bin/my-check")
    try:
        args = shlex.split(str(settings.get("args") or ""))
    except ValueError as exc:
        raise ValueError(f"Arguments: {exc}") from None
    try:
        timeout = float(settings.get("timeout") or DEFAULT_TIMEOUT)
    except ValueError:
        raise ValueError("Timeout must be a number of seconds") from None
    try:
        done = runner(
            [program, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        raise RuntimeError(f"{program}: not found") from None
    except PermissionError:
        raise RuntimeError(f"{program}: not executable") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"no answer after {timeout:g} s") from None
    if done.returncode != 0:
        raise RuntimeError(last_line(done.stderr) or f"exit code {done.returncode}")
    return done.stdout


def last_line(text: str | None) -> str:
    """The program's own reason is usually last; progress lines come before it."""
    lines = [line.strip() for line in (text or "").replace("\r", "\n").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def parse(text: str) -> list[Item]:
    if len(text) > MAX_OUTPUT:
        raise ValueError("Output too large")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("Output is not JSON") from None
    if not isinstance(data, list):
        raise ValueError("Output must be a JSON list of items")
    items, seen = [], set()
    for entry in data[:MAX_ITEMS]:
        if not isinstance(entry, dict) or not _text(entry.get("id")) or not _text(entry.get("title")):
            raise ValueError("Each item needs an id and a title")
        item_id = _text(entry["id"])
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(Item(id=item_id, title=_text(entry["title"]), **{k: _text(entry.get(k)) for k in FIELDS}))
    return items


def _text(value: Any) -> str:
    return "" if value is None else str(value)
