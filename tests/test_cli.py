import asyncio
import json

from watchkeep import cli
from watchkeep.core.engine import Core
from watchkeep.core.store import Store

from test_engine import FakePlugin, item


def test_status_data_lists_open_items_without_settings():
    plugin = FakePlugin()
    plugin.next = [item("a"), item("b")]
    core = Core({"fake": plugin}, Store(":memory:"))
    core.add_source("fake", "Work", {"account": "me", "token": "s3cret"}, source_id="s1", client="acme")
    asyncio.run(core.sync_now())
    core.ignore_item("s1", "b")

    data = cli.status_data(core, now=5.0)

    assert data["generated_at"] == 5.0
    assert data["syncing"] is False
    (source,) = data["sources"]
    assert source["name"] == "Work" and source["client"] == "acme" and source["error"] is None
    assert [i["title"] for i in source["items"]] == ["a"]  # ignored items stay out
    assert source["items"][0]["action"] == "review requested"
    assert "s3cret" not in json.dumps(data)


def test_status_json_prints_one_document(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert cli.main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["sources"] == []
