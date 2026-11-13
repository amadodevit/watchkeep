import asyncio
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from watchkeep.core.engine import Core
from watchkeep.core.models import Appointment, State
from watchkeep.core.store import Store
from watchkeep.plugins.calendar import FETCH_DAYS, CalendarSource, appointments, fetch
from watchkeep.ui import layout

ICS = (Path(__file__).parent / "fixtures" / "calendar.ics").read_text()
MONDAY_8AM = datetime(2026, 11, 16, 8, 0)  # local (Europe/Lisbon, UTC+0 in November)


@pytest.fixture(autouse=True)
def lisbon(monkeypatch):
    # Day boundaries depend on the machine's zone: pin it so tests pass anywhere.
    monkeypatch.setenv("TZ", "Europe/Lisbon")
    time.tzset()
    yield
    monkeypatch.delenv("TZ")
    time.tzset()


def titles(found):
    return [a.title for a in found]


# -- plugin --------------------------------------------------------------------


def test_today_expands_recurring_and_drops_cancelled_and_later():
    found = appointments(ICS, days=1, now=MONDAY_8AM)
    assert sorted(titles(found)) == ["Design review", "Gym", "Standup", "Team offsite"]


def test_excluded_occurrence_of_a_recurring_meeting_is_gone():
    assert appointments(ICS, days=1, now=datetime(2026, 11, 9, 8, 0)) == []


def test_window_of_several_days():
    assert "Planning" in titles(appointments(ICS, days=5, now=MONDAY_8AM))
    assert "Planning" not in titles(appointments(ICS, days=4, now=MONDAY_8AM))


def test_fields_zones_and_links():
    by_title = {a.title: a for a in appointments(ICS, days=7, now=MONDAY_8AM)}
    review = by_title["Design review"]
    assert datetime.fromisoformat(review.start).astimezone().strftime("%H:%M") == "15:00"  # 10:00 New York
    assert review.attendees == ("Ana Example", "bob@example.com", "Carla Example")
    assert review.url == "https://meet.example.com/abc-defg"  # found in the description
    offsite = by_title["Team offsite"]
    assert offsite.all_day and (offsite.start, offsite.end) == ("2026-11-16", "2026-11-18")
    assert by_title["Planning"].url == "https://calendar.example.com/event/planning"
    gym = by_title["Gym"]  # floating time: X-WR-TIMEZONE, here Lisbon
    assert datetime.fromisoformat(gym.start).astimezone().strftime("%H:%M") == "18:00"


def test_enter_opens_the_video_call_not_the_help_link_and_unwraps_safe_links():
    sync = next(a for a in appointments(ICS, days=2, now=MONDAY_8AM) if a.title == "Daily sync")
    assert sync.url == "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0"


def test_occurrences_of_a_recurring_meeting_get_distinct_ids():
    found = [a for a in appointments(ICS, days=30, now=MONDAY_8AM) if a.title == "Standup"]
    assert len(found) == 5 and len({a.id for a in found}) == 5  # 16, 23, 30 Nov, 7, 14 Dec


class FakeHttp:
    def __init__(self, text="", status=200):
        self.text, self.status = text, status

    def get(self, url, **_):
        request = httpx.Request("GET", url)
        response = httpx.Response(self.status, text=self.text, request=request)
        if self.status >= 400:
            raise httpx.HTTPStatusError(f"error for {url}", request=request, response=response)
        return response


SECRET = "https://calendar.example.com/ical/someone%40example.com/private-0123456789abcdef/basic.ics"


def ctx(http):
    from watchkeep.core.plugin import SyncContext

    return SyncContext(settings={"url": SECRET, "days": "1"}, http=http)  # "days": stored by older versions


def test_fetch_errors_never_show_the_secret_address():
    for http in (FakeHttp(status=404), FakeHttp(text="<html>login</html>")):
        with pytest.raises(RuntimeError) as exc:
            fetch(ctx(http))
        assert "private" not in str(exc.value) and "example.com" not in str(exc.value)


def test_sync_always_covers_31_days_and_ignores_an_old_days_setting():
    assert FETCH_DAYS == 31 and not any(s.key == "days" for s in CalendarSource.settings)
    found = CalendarSource().sync(ctx(FakeHttp(text=ICS)))
    later = [a for a in found if a.start[:10] > datetime.now().date().isoformat()]
    assert later or not found  # fixture dates may all be past on the machine's clock: then nothing at all
    assert CalendarSource().test_connection(ctx(FakeHttp(text=ICS))).endswith("in the next 31 days")


# -- core ----------------------------------------------------------------------


class FakeEvents:
    name = "agenda"
    connection = "http"
    kind = "events"
    settings = ()

    def __init__(self):
        self.next = []

    def sync(self, ctx):
        return list(self.next)


def meeting(id_, start="2026-11-16T10:00:00+00:00", end="2026-11-16T10:30:00+00:00"):
    return Appointment(id=id_, title=id_, start=start, end=end)


def test_core_keeps_appointments_without_notifications(tmp_path):
    plugin = FakeEvents()
    store_path = tmp_path / "state.db"
    core = Core({"agenda": plugin}, Store(store_path))
    core.add_source("agenda", "Work", {}, source_id="cal")
    plugin.next = [meeting("a")]
    asyncio.run(core.sync_now())
    plugin.next = [meeting("a"), meeting("b"), meeting("b")]  # duplicate ids: one row
    asyncio.run(core.sync_now())
    [source] = core.get_state().sources
    assert sorted(a.id for a in source.appointments) == ["a", "b"]
    assert source.items == () and core.events_since(0) == ()
    assert [p.kind for p in core.list_plugins()] == ["events"]

    reopened = Core({"agenda": plugin}, Store(store_path))  # shown on startup, before any sync
    assert len(reopened.get_state().sources[0].appointments) == 2
    reopened.remove_source("cal")
    assert Store(store_path).appointments("cal") == []


def test_core_rejects_events_plugin_returning_items():
    from watchkeep.core.models import Item

    plugin = FakeEvents()
    core = Core({"agenda": plugin}, Store(":memory:"))
    core.add_source("agenda", "Work", {}, source_id="cal")
    plugin.next = [Item("x", "a", "t", "", "", "", "")]
    asyncio.run(core.sync_now())
    assert "Appointment" in core.get_state().sources[0].last_error


# -- agenda layout -------------------------------------------------------------


def state_with(appointments_):
    from watchkeep.core.models import SourceView

    source = SourceView(
        id="cal", plugin="agenda", name="Work", tags=(), interval=900, last_sync=1.0, last_error=None,
        syncing=False, items=(), appointments=tuple(appointments_),
    )
    return State(version=1, sources=(source,))


def plugins():
    core = Core({"agenda": FakeEvents()}, Store(":memory:"))
    return {p.name: p for p in core.list_plugins()}


def at(hour, minute=0, day=16):
    return datetime(2026, 11, day, hour, minute).timestamp()


def test_agenda_orders_all_day_first_and_strikes_what_is_over():
    found = appointments(ICS, days=1, now=MONDAY_8AM)
    rows = layout.agenda(state_with(found), plugins(), now=lambda: at(9, 50))
    assert rows[0].kind == "group" and "Today" in rows[0].cells[0].plain
    order = [r.cells[2].plain for r in rows[1:]]
    assert order == ["Team offsite", "Standup", "Design review", "Gym"]
    standup, review = rows[2], rows[3]
    assert "strike" in str(standup.cells[2].style)  # 9:30-9:45, over at 9:50
    assert standup.cells[0].plain == "09:30–09:45" and standup.cells[1].plain == "15m"
    assert review.cells[3].plain == "Ana Example, bob@example.com +1"
    assert rows[1].cells[0].plain == "all day · until 17 Nov" and rows[1].cells[1].plain == "2d"


def test_agenda_marks_what_is_happening_now():
    rows = layout.agenda(state_with([meeting("call")]), plugins(), now=lambda: at(10, 10))
    assert rows[1].cells[0].plain.startswith("now · ")


def test_agenda_lists_earlier_days_under_today_and_drops_yesterday():
    trip = Appointment("trip", "Trip", "2026-11-14", "2026-11-17", all_day=True)
    late = meeting("late", start="2026-11-15T23:30:00+00:00", end="2026-11-16T00:30:00+00:00")
    gone = meeting("gone", start="2026-11-15T10:00:00+00:00", end="2026-11-15T11:00:00+00:00")
    rows = layout.agenda(state_with([trip, late, gone]), plugins(), now=lambda: at(8))
    assert [r.cells[2].plain for r in rows if r.kind == "appointment"] == ["Trip", "late"]
    assert all(r.cells[0].plain.startswith(" Today") for r in rows if r.kind == "group")


def test_agenda_shows_the_chosen_days_with_a_header_for_each():
    found = appointments(ICS, days=31, now=MONDAY_8AM)
    week = layout.agenda(state_with(found), plugins(), now=lambda: at(8), days=7)
    headers = [r.cells[0].plain.strip() for r in week if r.kind == "group"]
    assert len(headers) == 7 and headers[0].startswith("Today") and headers[1].startswith("Tomorrow")
    assert headers[-1] == "Sun 22 Nov"  # rolling from today, not Mon-Sun
    assert "Planning" in [r.cells[2].plain for r in week]  # Friday
    three = layout.agenda(state_with(found), plugins(), now=lambda: at(8), days=3)
    assert "Planning" not in [r.cells[2].plain for r in three]
    assert sum(r.kind == "group" for r in three) == 3


def test_agenda_marks_a_free_day():
    rows = layout.agenda(state_with([]), plugins(), now=lambda: at(8), days=3)
    assert [r.kind for r in rows] == ["group", "empty"] * 3
    assert rows[1].cells[2].plain == "nothing on"


def test_overview_row_counts_today_and_names_the_next_one():
    found = appointments(ICS, days=1, now=MONDAY_8AM)
    rows = layout.build(state_with(found), plugins(), layout.View(levels=()), now=lambda: at(9, 50))
    [row] = rows
    assert row.cells[1].plain == "4 events today · next 15:00 Design review"
    assert layout.has_events(state_with([]), plugins())
    assert not layout.has_events(State(version=1, sources=()), plugins())


def test_bad_appointments_fail_only_their_source_and_generators_work():
    class Odd(FakeEvents):
        def sync(self, ctx):
            if self.next == "generator":
                return (meeting(i) for i in ("a", "b"))
            return [Appointment("x", "x", datetime(2026, 11, 16, 10), "2026-11-16T11:00:00+00:00")]

    plugin = Odd()
    core = Core({"agenda": plugin}, Store(":memory:"))
    core.add_source("agenda", "Work", {}, source_id="cal")
    asyncio.run(core.sync_now())  # must not raise out of the core
    assert "ISO 8601" in core.get_state().sources[0].last_error
    plugin.next = "generator"
    asyncio.run(core.sync_now())
    source = core.get_state().sources[0]
    assert source.last_error is None and len(source.appointments) == 2
