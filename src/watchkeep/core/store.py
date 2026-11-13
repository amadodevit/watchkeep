"""On-disk state: sources, their secrets, and the last snapshot per source.

Also each source's plugin memory (SyncContext.memory): plain JSON a plugin keeps
between syncs, such as which posts it has already seen. Never secrets.

And the item history: every item an items source has ever reported, kept after
it leaves the snapshot, with when it was first seen and when the user marked it
read. Read state lives here, not in the plugin, so any items plugin gets it.

And the last appointments each events source reported, so the agenda shows on
startup before the first sync.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from .config import SourceConfig
from .connections import is_reference
from .health import Health
from .models import Appointment, Item, Sample

SAMPLES_KEPT = 50  # per row; enough for any reasonable rule window
KEYRING_SERVICE = "watchkeep"
# Backends that "work" but keep secrets in a plain file, or not at all.
INSECURE_BACKENDS = ("keyrings.alt", "keyring.backends.fail", "keyring.backends.null")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id       TEXT PRIMARY KEY,
    plugin   TEXT NOT NULL,
    name     TEXT NOT NULL,
    interval INTEGER NOT NULL,
    tags     TEXT NOT NULL,
    settings TEXT NOT NULL,
    position INTEGER NOT NULL
);
-- Legacy: plaintext secrets from before the keyring. Emptied by Store.migrate_secrets().
CREATE TABLE IF NOT EXISTS source_secrets (
    source_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    PRIMARY KEY (source_id, key)
);
-- Which secrets a source has in the keyring. Names only, never values.
CREATE TABLE IF NOT EXISTS keyring_entries (
    source_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    PRIMARY KEY (source_id, key)
);
CREATE TABLE IF NOT EXISTS source_state (
    source_id  TEXT PRIMARY KEY,
    last_sync  REAL,
    last_error TEXT,
    warnings   TEXT  -- JSON list from the last successful sync; NULL when none
);
-- SyncContext.memory per source, saved after each successful sync.
CREATE TABLE IF NOT EXISTS source_memory (
    source_id TEXT PRIMARY KEY,
    data      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    source_id  TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    data       TEXT NOT NULL,
    first_seen REAL NOT NULL,
    PRIMARY KEY (source_id, item_id)
);
CREATE TABLE IF NOT EXISTS samples (
    source_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    taken_at  REAL NOT NULL,
    ok        INTEGER NOT NULL,
    summary   TEXT NOT NULL,
    data      TEXT NOT NULL,
    url       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_by_row ON samples (source_id, key, taken_at);
CREATE TABLE IF NOT EXISTS health (
    source_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    data      TEXT NOT NULL,
    PRIMARY KEY (source_id, key)
);
-- Every item ever reported, kept after it leaves `items`. read_at NULL means unread.
CREATE TABLE IF NOT EXISTS item_history (
    source_id  TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    item       TEXT NOT NULL,  -- JSON of the latest Item
    first_seen REAL NOT NULL,
    read_at    REAL,
    PRIMARY KEY (source_id, item_id)
);
-- The last snapshot of each events source. No history: a past meeting has nothing left to do.
CREATE TABLE IF NOT EXISTS appointments (
    source_id      TEXT NOT NULL,
    appointment_id TEXT NOT NULL,
    data           TEXT NOT NULL,
    PRIMARY KEY (source_id, appointment_id)
);
CREATE TABLE IF NOT EXISTS ignored (
    source_id TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    PRIMARY KEY (source_id, item_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SecretStore:
    """Where secret settings live: the OS keyring, never the database.

    This is the only class that touches secret values, so swapping the backend
    means rewriting it and nothing else. The database only records which keys
    a source has, so a source can be removed without asking the keyring.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        # Key names mirrored in memory, so worker threads can ask without touching SQLite.
        self._names: dict[str, set[str]] = {}
        for source_id, key in db.execute("SELECT source_id, key FROM keyring_entries"):
            self._names.setdefault(source_id, set()).add(key)

    def available(self) -> bool:
        return _secure(keyring.get_keyring())

    def get_all(self, source_id: str) -> dict[str, str]:
        return self.read(source_id, self.keys(source_id))

    def keys(self, source_id: str) -> tuple[str, ...]:
        return tuple(sorted(self._names.get(source_id, ())))

    def read(self, source_id: str, keys: tuple[str, ...]) -> dict[str, str]:
        """Keyring only, no database, so it can run in a worker thread.

        Called at sync time, not at startup, so a locked keyring only stops syncing.
        """
        secrets = {}
        for key in keys:
            try:
                value = keyring.get_password(KEYRING_SERVICE, _entry(source_id, key))
            except KeyringError as exc:
                raise RuntimeError(f"keyring locked or unavailable ({type(exc).__name__}): unlock it to sync") from None
            if value is None:
                raise RuntimeError(f"secret {key!r} is missing from the keyring: remove this source and add it again")
            secrets[key] = value
        return secrets

    def put(self, source_id: str, key: str, value: str) -> None:
        if not self.available():
            raise ValueError("No keyring on this machine: use env:VAR or gh:<user>")
        keyring.set_password(KEYRING_SERVICE, _entry(source_id, key), value)
        self._db.execute("INSERT OR IGNORE INTO keyring_entries (source_id, key) VALUES (?, ?)", (source_id, key))
        self._names.setdefault(source_id, set()).add(key)

    def delete(self, source_id: str, key: str) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, _entry(source_id, key))
        except KeyringError:
            pass  # already gone, or locked: forgetting it here still stops it being used
        self._db.execute("DELETE FROM keyring_entries WHERE source_id = ? AND key = ?", (source_id, key))
        self._names.get(source_id, set()).discard(key)

    def delete_source(self, source_id: str) -> None:
        for key in self.keys(source_id):
            try:
                keyring.delete_password(KEYRING_SERVICE, _entry(source_id, key))
            except KeyringError:
                pass  # already gone, or locked: removing the source must still work
        self._db.execute("DELETE FROM keyring_entries WHERE source_id = ?", (source_id,))
        self._db.execute("DELETE FROM source_secrets WHERE source_id = ?", (source_id,))
        self._names.pop(source_id, None)


def _entry(source_id: str, key: str) -> str:
    return f"{source_id}/{key}"


def _secure(backend: object) -> bool:
    nested = getattr(backend, "backends", None)  # the chainer backend wraps several
    if nested is not None:
        return any(_secure(b) for b in nested)
    return getattr(backend, "priority", 0) >= 1 and not type(backend).__module__.startswith(INSECURE_BACKENDS)


class Store:
    def __init__(self, path: Path | str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        if path != ":memory:":
            os.chmod(path, 0o600)  # holds secrets: owner-only
        had_history = self._has_table("item_history")
        self._db.executescript(SCHEMA)
        self._migrate(had_history)
        self.secrets = SecretStore(self._db)

    def _has_table(self, name: str) -> bool:
        row = self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
        return row is not None

    def _migrate(self, had_history: bool = True) -> None:
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(sources)")}
        if "rule" not in columns:  # databases from before sample-based sources
            self._db.execute("ALTER TABLE sources ADD COLUMN rule TEXT")
        if "checks" not in columns:  # databases from before checks
            self._db.execute("ALTER TABLE sources ADD COLUMN checks TEXT")
        for column in ("client", "environment"):  # databases from before grouping
            if column not in columns:
                self._db.execute(f"ALTER TABLE sources ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        state_columns = {row[1] for row in self._db.execute("PRAGMA table_info(source_state)")}
        if "warnings" not in state_columns:  # databases from before sync warnings
            self._db.execute("ALTER TABLE source_state ADD COLUMN warnings TEXT")
        # source_memory and appointments are new tables, so SCHEMA's CREATE IF NOT EXISTS already added them.
        if not had_history:  # databases from before the archive: start it from what is shown now
            with self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO item_history (source_id, item_id, item, first_seen) "
                    "SELECT source_id, item_id, data, first_seen FROM items"
                )

    # -- sources ----------------------------------------------------------------

    def list_sources(self) -> list[SourceConfig]:
        rows = self._db.execute(
            "SELECT id, plugin, name, interval, tags, settings, rule, checks, client, environment "
            "FROM sources ORDER BY position"
        ).fetchall()
        return [
            SourceConfig(
                id=source_id,
                plugin=plugin,
                name=name,
                interval=interval,
                tags=tuple(json.loads(tags)),
                settings=json.loads(settings),  # secrets stay in the keyring until sync
                rule=json.loads(rule) if rule else None,
                checks=json.loads(checks) if checks else None,
                client=client,
                environment=environment,
            )
            for source_id, plugin, name, interval, tags, settings, rule, checks, client, environment in rows
        ]

    def add_source(self, source: SourceConfig, secret_keys: set[str]) -> None:
        """Secrets go to the keyring; references like gh:<user> are not secret and stay here."""
        secret = {k: str(v) for k, v in source.settings.items() if k in secret_keys and not is_reference(str(v))}
        public = {k: v for k, v in source.settings.items() if k not in secret}
        with self._db:
            (position,) = self._db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM sources").fetchone()
            self._db.execute(
                "INSERT INTO sources (id, plugin, name, interval, tags, settings, position, rule, checks, client, environment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source.id,
                    source.plugin,
                    source.name,
                    source.interval,
                    json.dumps(list(source.tags)),
                    json.dumps(public),
                    position,
                    json.dumps(source.rule) if source.rule else None,
                    json.dumps(source.checks) if source.checks is not None else None,
                    source.client,
                    source.environment,
                ),
            )
            for key, value in secret.items():  # a keyring failure rolls the whole insert back
                self.secrets.put(source.id, key, value)

    def update_source(self, source: SourceConfig, secret_keys: set[str], kept: set[str]) -> None:
        """Rewrite a source's definition. Secrets in `kept` stay in the keyring untouched;
        other secret keys get the new value, or leave the keyring when they became references."""
        secret = {
            k: str(v) for k, v in source.settings.items() if k in secret_keys and k not in kept and not is_reference(str(v))
        }
        public = {k: v for k, v in source.settings.items() if k not in secret}
        with self._db:
            self._db.execute(
                "UPDATE sources SET name = ?, interval = ?, tags = ?, settings = ?, rule = ?, checks = ?, "
                "client = ?, environment = ? WHERE id = ?",
                (
                    source.name,
                    source.interval,
                    json.dumps(list(source.tags)),
                    json.dumps(public),
                    json.dumps(source.rule) if source.rule else None,
                    json.dumps(source.checks) if source.checks is not None else None,
                    source.client,
                    source.environment,
                    source.id,
                ),
            )
            for key, value in secret.items():
                self.secrets.put(source.id, key, value)
            for key in set(self.secrets.keys(source.id)) - kept - secret.keys():
                self.secrets.delete(source.id, key)  # now a reference, or no longer set

    def remove_source(self, source_id: str) -> None:
        with self._db:
            for table, column in (
                ("sources", "id"),
                ("items", "source_id"),
                ("source_state", "source_id"),
                ("samples", "source_id"),
                ("health", "source_id"),
                ("ignored", "source_id"),
                ("item_history", "source_id"),
                ("source_memory", "source_id"),
                ("appointments", "source_id"),
            ):
                self._db.execute(f"DELETE FROM {table} WHERE {column} = ?", (source_id,))
            self.secrets.delete_source(source_id)

    def migrate_secrets(self) -> tuple[int, int]:
        """Move plaintext secrets from older databases into the keyring.

        Each secret is written, read back, and only then deleted here. Returns
        (moved, left behind); without a keyring everything stays put.
        """
        rows = self._db.execute("SELECT source_id, key, value FROM source_secrets").fetchall()
        if not rows:
            return 0, 0
        moved = 0
        for source_id, key, value in rows:
            with self._db:
                if is_reference(value):
                    self._move_to_settings(source_id, key, value)
                elif self.secrets.available():
                    self.secrets.put(source_id, key, value)
                    if keyring.get_password(KEYRING_SERVICE, _entry(source_id, key)) != value:
                        raise RuntimeError(f"keyring did not keep {source_id}/{key}; nothing deleted")
                else:
                    continue
                self._db.execute("DELETE FROM source_secrets WHERE source_id = ? AND key = ?", (source_id, key))
            moved += 1
        if moved:
            self._db.execute("VACUUM")  # deleted rows can linger in free pages until the file is rewritten
        return moved, len(rows) - moved

    def _move_to_settings(self, source_id: str, key: str, value: str) -> None:
        row = self._db.execute("SELECT settings FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row:
            settings = {**json.loads(row[0]), key: value}
            self._db.execute("UPDATE sources SET settings = ? WHERE id = ?", (json.dumps(settings), source_id))

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- snapshots --------------------------------------------------------------

    def snapshot(self, source_id: str) -> dict[str, tuple[Item, float]]:
        rows = self._db.execute(
            "SELECT item_id, data, first_seen FROM items WHERE source_id = ?", (source_id,)
        )
        return {item_id: (Item.from_dict(json.loads(data)), first) for item_id, data, first in rows}

    def source_state(self, source_id: str) -> tuple[float | None, str | None]:
        row = self._db.execute(
            "SELECT last_sync, last_error FROM source_state WHERE source_id = ?", (source_id,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def memory(self, source_id: str) -> dict:
        """The plugin memory saved after the last successful sync; {} before the first."""
        row = self._db.execute("SELECT data FROM source_memory WHERE source_id = ?", (source_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def warnings(self, source_id: str) -> tuple[str, ...]:
        row = self._db.execute("SELECT warnings FROM source_state WHERE source_id = ?", (source_id,)).fetchone()
        return tuple(json.loads(row[0])) if row and row[0] else ()

    def replace_snapshot(
        self,
        source_id: str,
        items: dict[str, tuple[Item, float]],
        synced_at: float,
        *,
        memory: dict | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        """`memory` None leaves the stored memory as it is."""
        with self._db:
            self._db.execute("DELETE FROM items WHERE source_id = ?", (source_id,))
            self._db.executemany(
                "INSERT INTO items (source_id, item_id, data, first_seen) VALUES (?, ?, ?, ?)",
                [(source_id, i, json.dumps(item.to_dict()), first) for i, (item, first) in items.items()],
            )
            # History keeps its own first_seen: an item that left and came back keeps the first one.
            self._db.executemany(
                "INSERT INTO item_history (source_id, item_id, item, first_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(source_id, item_id) DO UPDATE SET item = excluded.item",
                [(source_id, i, json.dumps(item.to_dict()), first) for i, (item, first) in items.items()],
            )
            self._set_state(source_id, synced_at, None)
            self._save_sync_extras(source_id, memory, warnings)

    # -- appointments -----------------------------------------------------------

    def appointments(self, source_id: str) -> list[Appointment]:
        rows = self._db.execute("SELECT data FROM appointments WHERE source_id = ?", (source_id,))
        return [Appointment.from_dict(json.loads(data)) for (data,) in rows]

    def replace_appointments(
        self,
        source_id: str,
        appointments: list[Appointment],
        synced_at: float,
        *,
        memory: dict | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        with self._db:
            self._db.execute("DELETE FROM appointments WHERE source_id = ?", (source_id,))
            self._db.executemany(
                "INSERT OR REPLACE INTO appointments (source_id, appointment_id, data) VALUES (?, ?, ?)",
                [(source_id, a.id, json.dumps(a.to_dict())) for a in appointments],
            )
            self._set_state(source_id, synced_at, None)
            self._save_sync_extras(source_id, memory, warnings)

    # -- samples and health -----------------------------------------------------

    def health(self, source_id: str) -> dict[str, Health]:
        rows = self._db.execute("SELECT key, data FROM health WHERE source_id = ?", (source_id,))
        return {key: Health.from_dict(json.loads(data)) for key, data in rows}

    def latest_samples(self, source_id: str) -> dict[str, tuple[Sample, float]]:
        rows = self._db.execute(
            "SELECT key, ok, summary, data, url, MAX(taken_at) FROM samples WHERE source_id = ? GROUP BY key",
            (source_id,),
        )
        return {
            key: (Sample(key=key, ok=bool(ok), summary=summary, values=json.loads(data), url=url), taken_at)
            for key, ok, summary, data, url, taken_at in rows
        }

    def previous_samples(self, source_id: str) -> dict[str, dict]:
        """Each row's values from the check before the latest, for growth since then."""
        rows = self._db.execute(
            "SELECT key, data FROM samples s WHERE source_id = ? AND taken_at = ("
            "  SELECT MAX(taken_at) FROM samples p WHERE p.source_id = s.source_id AND p.key = s.key AND p.taken_at < ("
            "    SELECT MAX(taken_at) FROM samples l WHERE l.source_id = s.source_id AND l.key = s.key))",
            (source_id,),
        )
        return {key: json.loads(data) for key, data in rows}

    def record_samples(
        self,
        source_id: str,
        samples: list[Sample],
        health: dict[str, Health],
        at: float,
        *,
        memory: dict | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        """Store this sync's samples and the resulting health. Rows no longer reported are dropped."""
        with self._db:
            self._db.executemany(
                "INSERT INTO samples (source_id, key, taken_at, ok, summary, data, url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(source_id, s.key, at, int(s.ok), s.summary, json.dumps(s.values), s.url) for s in samples],
            )
            for sample in samples:
                self._db.execute(
                    "DELETE FROM samples WHERE source_id = ? AND key = ? AND taken_at NOT IN "
                    "(SELECT taken_at FROM samples WHERE source_id = ? AND key = ? ORDER BY taken_at DESC LIMIT ?)",
                    (source_id, sample.key, source_id, sample.key, SAMPLES_KEPT),
                )
            keys = [s.key for s in samples]
            marks = ",".join("?" * len(keys)) or "''"
            self._db.execute(f"DELETE FROM samples WHERE source_id = ? AND key NOT IN ({marks})", (source_id, *keys))
            self._db.execute("DELETE FROM health WHERE source_id = ?", (source_id,))
            self._db.executemany(
                "INSERT INTO health (source_id, key, data) VALUES (?, ?, ?)",
                [(source_id, key, json.dumps(h.to_dict())) for key, h in health.items()],
            )
            self._set_state(source_id, at, None)
            self._save_sync_extras(source_id, memory, warnings)

    def set_health(self, source_id: str, key: str, health: Health) -> None:
        with self._db:
            self._db.execute(
                "UPDATE health SET data = ? WHERE source_id = ? AND key = ?",
                (json.dumps(health.to_dict()), source_id, key),
            )

    # -- ignored items ----------------------------------------------------------

    def ignored(self, source_id: str) -> set[str]:
        rows = self._db.execute("SELECT item_id FROM ignored WHERE source_id = ?", (source_id,))
        return {item_id for (item_id,) in rows}

    def ignore(self, source_id: str, item_id: str) -> None:
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO ignored (source_id, item_id) VALUES (?, ?)", (source_id, item_id))

    def forget_ignored_except(self, source_id: str, still_present: set[str]) -> None:
        """An ignore lasts while the item is in the snapshot. Once it leaves, forget it."""
        with self._db:
            for item_id in self.ignored(source_id) - still_present:
                self._db.execute("DELETE FROM ignored WHERE source_id = ? AND item_id = ?", (source_id, item_id))

    # -- item history and read state -------------------------------------------

    def read_items(self, source_id: str) -> set[str]:
        rows = self._db.execute(
            "SELECT item_id FROM item_history WHERE source_id = ? AND read_at IS NOT NULL", (source_id,)
        )
        return {item_id for (item_id,) in rows}

    def set_read(self, source_id: str, item_id: str, read_at: float | None) -> bool:
        """`read_at` None marks it unread. False when the item was never recorded."""
        with self._db:
            cursor = self._db.execute(
                "UPDATE item_history SET read_at = ? WHERE source_id = ? AND item_id = ?", (read_at, source_id, item_id)
            )
        return cursor.rowcount > 0

    def history(self) -> list[tuple[str, Item, float, float | None, bool]]:
        """(source id, item, first seen, read at, still in the snapshot), newest first."""
        rows = self._db.execute(
            "SELECT h.source_id, h.item, h.first_seen, h.read_at, i.item_id IS NOT NULL "
            "FROM item_history h LEFT JOIN items i ON i.source_id = h.source_id AND i.item_id = h.item_id "
            "ORDER BY h.first_seen DESC, h.rowid DESC"
        )
        return [
            (source_id, Item.from_dict(json.loads(data)), first, read_at, bool(present))
            for source_id, data, first, read_at, present in rows
        ]

    def record_error(self, source_id: str, error: str) -> None:
        last_sync, _ = self.source_state(source_id)
        with self._db:
            self._set_state(source_id, last_sync, error)

    def _set_state(self, source_id: str, last_sync: float | None, error: str | None) -> None:
        self._db.execute(
            "INSERT INTO source_state (source_id, last_sync, last_error) VALUES (?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET last_sync = excluded.last_sync, last_error = excluded.last_error",
            (source_id, last_sync, error),
        )

    def _save_sync_extras(self, source_id: str, memory: dict | None, warnings: tuple[str, ...]) -> None:
        """Part of a successful sync's transaction, so memory never runs ahead of the snapshot."""
        self._db.execute(
            "UPDATE source_state SET warnings = ? WHERE source_id = ?",
            (json.dumps(list(warnings)) if warnings else None, source_id),
        )
        if memory is None:
            return
        if memory:
            self._db.execute(
                "INSERT INTO source_memory (source_id, data) VALUES (?, ?) "
                "ON CONFLICT(source_id) DO UPDATE SET data = excluded.data",
                (source_id, json.dumps(memory)),
            )
        else:
            self._db.execute("DELETE FROM source_memory WHERE source_id = ?", (source_id,))

    def close(self) -> None:
        self._db.close()
