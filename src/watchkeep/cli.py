from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from watchkeep.core.config import config_path, data_path, load_config
from watchkeep.core.engine import Core
from watchkeep.core.health import FAILING
from watchkeep.core.leader import SyncLeader
from watchkeep.core.registry import discover
from watchkeep.core.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watchkeep")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="test each source's connection and exit")
    sub.add_parser("rejected", help="list feed posts the model turned down, with its reason")
    sub.add_parser("run", help="sync on schedule without the UI (e.g. as a login service)")
    status = sub.add_parser("status", help="print what needs you, from the last syncs, and exit")
    status.add_argument("--json", action="store_true", help="machine-readable output for other tools")
    args = parser.parse_args(argv)

    path = data_path()
    store = Store(path)
    moved = _migrate_secrets(store)  # before Core loads the sources
    core = Core(discover(), store, leader=SyncLeader(path.with_name("sync.lock")))
    notice = _import_config_once(core)
    if notice is None:
        return 1
    notice = " ".join(n for n in (notice, moved) if n)

    if args.command == "check":
        return _check(core)
    if args.command == "rejected":
        return _rejected(core, store)
    if args.command == "status":
        return _status(core, as_json=args.json)
    if args.command == "run":
        return _run_headless(core, notice)

    from watchkeep.ui.app import WatchkeepApp  # keep `check` fast and UI-free

    WatchkeepApp(core, notice=notice).run()
    return 0


def _import_config_once(core: Core) -> str | None:
    """Returns a notice for the user ("" if nothing happened), or None on a fatal error."""
    path = config_path()
    if not path.exists():
        return ""
    try:
        count = core.import_config(load_config(path))
    except ValueError as exc:
        print(f"Could not import {path}: {exc}", file=sys.stderr)
        return None
    if not count:
        return ""
    return f"Imported {count} source{'s' * (count != 1)} from {path}. That file is no longer read."


def _migrate_secrets(store: Store) -> str:
    """Older versions kept secrets in the database. Move them to the keyring once."""
    try:
        moved, left = store.migrate_secrets()
    except Exception as exc:
        return f"Could not move secrets to the keyring: {exc}"
    if left:
        return f"{left} secret{'s' * (left != 1)} still stored in plaintext: no keyring on this machine."
    if moved:
        return f"Moved {moved} secret{'s' * (moved != 1)} out of the database."
    return ""


def _check(core: Core) -> int:
    ids = core.source_ids()
    if not ids:
        print("No sources yet. Run watchkeep and press a to add one.")
        return 0
    failed = 0
    for source_id in ids:
        try:
            print(f"✓ {source_id}: {core.test_connection(source_id)}")
        except Exception as exc:
            failed += 1
            print(f"✗ {source_id}: {exc}")
    return 1 if failed else 0


def _run_headless(core: Core, notice: str) -> int:
    """The UI's scheduler without the UI. Errors are kept per source, as in the UI."""
    if notice:
        print(notice, flush=True)
    print(f"watchkeep running headless with {len(core.source_ids())} sources. Ctrl+C stops it.", flush=True)
    try:
        asyncio.run(core.run())
    except KeyboardInterrupt:
        pass
    return 0


def status_data(core: Core, now: float | None = None) -> dict:
    """What needs attention, as plain data: open items and failing rows per source.

    Reads only what the last syncs stored; it never syncs. Secrets and settings are
    left out on purpose: this is meant to be handed to other tools.
    """
    state = core.get_state()
    sources = []
    for view in state.sources:
        if view.plugin == "calendar" or view.appointments:
            continue  # appointments have their own home; this is about what needs you
        sources.append(
            {
                "id": view.id,
                "plugin": view.plugin,
                "name": view.name,
                "client": view.client,
                "last_sync": view.last_sync,
                "error": view.last_error,
                "warnings": list(view.warnings),
                "items": [
                    {
                        "action": v.item.action,
                        "title": v.item.title,
                        "url": v.item.url,
                        "repo": v.item.repo,
                        "author": v.item.author,
                        "updated_at": v.item.updated_at,
                        "first_seen": v.first_seen,
                    }
                    for v in view.items
                ],
                "failing": [
                    {"key": h.key, "summary": h.summary, "since": h.since, "acknowledged": h.acknowledged, "url": h.url}
                    for h in view.health
                    if h.state == FAILING
                ],
            }
        )
    return {
        "generated_at": time.time() if now is None else now,
        "syncing": core.syncs_elsewhere(),
        "sources": sources,
    }


def _status(core: Core, as_json: bool) -> int:
    data = status_data(core)
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    if not data["sources"]:
        print("No sources yet. Run watchkeep and press a to add one.")
        return 0
    for s in data["sources"]:
        parts = []
        if s["items"]:
            parts.append(f"{len(s['items'])} open")
        if s["failing"]:
            parts.append(f"{len(s['failing'])} failing")
        if s["error"]:
            parts.append(f"error: {s['error']}")
        print(f"{s['name']}: {', '.join(parts) or 'all clear'}")
    return 0


def _rejected(core: Core, store: Store) -> int:
    """Feed posts judged and turned down in the last KEEP_DAYS, newest first."""
    rows = []
    for source_id in core.source_ids():
        for post in store.memory(source_id).get("rejected", {}).values():
            rows.append(post)
    if not rows:
        print("No rejected posts yet. They are recorded from the next feeds sync on.")
        return 0
    for post in sorted(rows, key=lambda p: p["judged"], reverse=True):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(post["judged"]))
        print(f"{when}  {post['site']}: {post['title']}\n    {post['reason']}\n    {post['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
