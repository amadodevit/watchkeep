import asyncio

from watchkeep.core.engine import Core
from watchkeep.core.leader import SyncLeader
from watchkeep.core.store import Store

from test_engine import FakePlugin, item


def test_only_one_holder(tmp_path):
    a, b = SyncLeader(tmp_path / "sync.lock"), SyncLeader(tmp_path / "sync.lock")
    assert a.acquire()
    assert a.acquire()  # re-entrant for the holder
    assert not b.acquire()
    assert b.other_is_syncing()
    a.release()
    assert not b.other_is_syncing()
    assert not b.held  # probing leaves no lock behind
    assert b.acquire()
    b.release()


def run_ticks(core, ticks=2):
    async def go():
        task = asyncio.create_task(core.run())
        await asyncio.sleep(0.01 * ticks)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())


def test_follower_does_not_sync_and_takes_over(tmp_path):
    lock = tmp_path / "sync.lock"
    other = SyncLeader(lock)
    assert other.acquire()  # e.g. `watchkeep run` already syncing
    plugin = FakePlugin()
    plugin.next = [item("a")]
    core = Core({"fake": plugin}, Store(":memory:"), tick=0.001, leader=SyncLeader(lock))
    core.add_source("fake", "S1", {"account": "me"}, interval=600, source_id="s1")
    assert core.syncs_elsewhere()
    version = core.get_state().version
    run_ticks(core)
    assert plugin.seen_settings is None  # never synced
    assert core.get_state().version > version  # but keeps re-reading the database

    other.release()
    run_ticks(core)
    assert plugin.seen_settings is not None
    assert not core.syncs_elsewhere()
