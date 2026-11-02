from __future__ import annotations

import argparse
import sys
import time

from watchkeep.core.config import config_path, data_path, load_config
from watchkeep.core.engine import Core
from watchkeep.core.registry import discover
from watchkeep.core.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watchkeep")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="test each source's connection and exit")
    sub.add_parser("rejected", help="list feed posts the model turned down, with its reason")
    args = parser.parse_args(argv)

    store = Store(data_path())
    moved = _migrate_secrets(store)  # before Core loads the sources
    core = Core(discover(), store)
    notice = _import_config_once(core)
    if notice is None:
        return 1
    notice = " ".join(n for n in (notice, moved) if n)

    if args.command == "check":
        return _check(core)
    if args.command == "rejected":
        return _rejected(core, store)

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
