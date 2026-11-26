import json
import shlex
import subprocess
import sys

import pytest

from watchkeep.core.plugin import SourcePlugin, SyncContext
from watchkeep.plugins.command import CommandSource, last_line, parse, run


def ctx(code: str, timeout: str = "10") -> SyncContext:
    """A source whose program is this Python, running `code`."""
    return SyncContext(settings={"command": sys.executable, "args": f"-c {shlex.quote(code)}", "timeout": timeout}, http=None)


def test_satisfies_the_contract():
    assert isinstance(CommandSource(), SourcePlugin)


def test_items_from_a_real_program():
    rows = [{"id": "a1", "title": "Invoice due", "url": "https://example.com/a1", "action": "money",
             "author": "Utility Co", "updated_at": "2026-01-01T00:00:00+00:00"}]
    items = CommandSource().sync(ctx(f"print({json.dumps(json.dumps(rows))})"))
    assert [(i.id, i.title, i.action, i.author, i.repo) for i in items] == [("a1", "Invoice due", "money", "Utility Co", "")]


def test_missing_fields_default_to_empty_and_duplicates_collapse():
    items = parse('[{"id": 1, "title": "x", "author": null}, {"id": "1", "title": "again"}]')
    assert len(items) == 1 and items[0].id == "1" and items[0].author == "" and items[0].url == ""


def test_failure_shows_the_programs_last_stderr_line():
    code = "import sys; sys.stderr.write('  3/9 fetched\\r  9/9 fetched\\nsign-in expired: run check\\n'); sys.exit(1)"
    with pytest.raises(RuntimeError, match="^sign-in expired: run check$"):
        CommandSource().sync(ctx(code))


def test_silent_failure_names_the_exit_code():
    with pytest.raises(RuntimeError, match="exit code 3"):
        CommandSource().sync(ctx("import sys; sys.exit(3)"))


def test_output_must_be_a_json_list_of_items():
    with pytest.raises(ValueError, match="not JSON"):
        parse("hello")
    with pytest.raises(ValueError, match="JSON list"):
        parse('{"id": 1}')
    with pytest.raises(ValueError, match="id and a title"):
        parse('[{"id": 1}]')


def test_program_must_be_a_full_path():
    with pytest.raises(ValueError, match="full path"):
        run({"command": "python3"})


def test_missing_program():
    with pytest.raises(RuntimeError, match="not found"):
        run({"command": "/nonexistent/program"})


def test_timeout():
    with pytest.raises(RuntimeError, match="no answer after 0.5 s"):
        CommandSource().sync(ctx("import time; time.sleep(5)", timeout="0.5"))


def test_no_shell_runs():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    assert parse(run({"command": "/bin/echo", "args": "'a b' ; rm -rf x"}, runner=runner)) == []
    argv, kwargs = calls[0]
    assert argv == ["/bin/echo", "a b", ";", "rm", "-rf", "x"] and "shell" not in kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_last_line():
    assert last_line("one\r\ntwo\n\n") == "two" and last_line(None) == ""
