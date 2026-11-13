"""Calendar source: your appointments for today (or the next few days) from an iCal address.

Reads the secret iCal address a calendar service gives out (Google Calendar:
Settings › your calendar › "Secret address in iCal format"). That address is a
password in a URL, so it is a secret setting, kept in the keyring, and never
shown in an error message.

Each sync returns every occurrence in the window, recurring meetings expanded
one per day, cancelled ones dropped. The window starts at local midnight today
and covers `days` days. Nothing here raises a notification: the core keeps the
snapshot for the Agenda tab.

`icalendar` and `recurring-ical-events` are imported inside the functions that
use them, so installing watchkeep without the `calendar` extra still works.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from watchkeep.core.models import Appointment
from watchkeep.core.plugin import Setting, SyncContext

MAX_DAYS = 31
DESCRIPTION_CHARS = 2000
MAX_ATTENDEES = 50
LINK = re.compile(r"https?://[^\s<>\"']+")
# Join links of the common video-call services: these win over any other link.
MEETING = re.compile(
    r"^https://(teams\.microsoft\.com/l/meetup-join/|teams\.live\.com/meet/|meet\.google\.com/[a-z]"
    r"|([\w-]+\.)?zoom\.us/j/|[\w-]+\.webex\.com/|whereby\.com/)"
)


class CalendarSource:
    name = "calendar"
    title = "Calendar"
    connection = "http"
    kind = "events"
    default_interval = 900  # 15m
    settings = (
        Setting(
            "url",
            "Secret iCal address",
            secret=True,
            help="Google Calendar: Settings › your calendar › Secret address in iCal format",
        ),
        Setting("days", "Days to show", required=False, default="1", help="1 = today only"),
    )

    def sync(self, ctx: SyncContext) -> list[Appointment]:
        return appointments(fetch(ctx), days=parse_days(ctx.settings.get("days", "1")))

    def test_connection(self, ctx: SyncContext) -> str:
        days = parse_days(ctx.settings.get("days", "1"))
        found = appointments(fetch(ctx), days=days)
        when = "today" if days == 1 else f"in the next {days} days"
        return f"{len(found)} event{'s' if len(found) != 1 else ''} {when}"


def parse_days(value: Any) -> int:
    try:
        days = int(str(value).strip())
    except ValueError:
        raise ValueError(f"days to show must be a whole number, got {value!r}") from None
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days to show must be between 1 and {MAX_DAYS}")
    return days


def fetch(ctx: SyncContext) -> str:
    """The calendar as text. Errors never include the address: it is the secret."""
    try:
        response = ctx.http.get(ctx.settings["url"])
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"calendar answered HTTP {exc.response.status_code}: check the secret address") from None
    except httpx.TimeoutException:
        raise RuntimeError("calendar timed out") from None
    except httpx.TransportError as exc:
        raise RuntimeError(f"calendar unreachable ({type(exc).__name__})") from None
    except httpx.InvalidURL:
        raise RuntimeError("the secret address is not a valid URL") from None
    text = response.text
    if "BEGIN:VCALENDAR" not in text[:1000]:
        raise RuntimeError("that address did not return a calendar (iCal)")
    return text


def appointments(text: str, *, days: int = 1, now: datetime | None = None) -> list[Appointment]:
    """Every occurrence from local midnight of `now`'s day, for `days` days."""
    try:
        import icalendar
        import recurring_ical_events
    except ImportError:
        raise RuntimeError("calendar support is not installed: pip install 'watchkeep[calendar]'") from None

    local_now = (now or datetime.now()).astimezone()
    start = datetime.combine(local_now.date(), time(), tzinfo=local_now.tzinfo)
    end = start + timedelta(days=days)
    try:
        calendar = icalendar.Calendar.from_ical(text)
        occurrences = recurring_ical_events.of(calendar).between(start, end)
    except Exception as exc:  # malformed files raise many different things
        raise RuntimeError(f"could not read the calendar ({type(exc).__name__})") from None

    found = []
    for event in occurrences:
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        found.append(_appointment(event, local_now))
    return found


def _appointment(event: Any, local_now: datetime) -> Appointment:
    begins = event.get("DTSTART").dt
    all_day = not isinstance(begins, datetime)
    ends = _end(event, begins, all_day)
    if not all_day:
        begins, ends = _aware(begins, local_now), _aware(ends, local_now)
    uid = str(event.get("UID", "")) or f"{event.get('SUMMARY', '')}"
    description = str(event.get("DESCRIPTION", ""))
    return Appointment(
        id=f"{uid}@{begins.isoformat()}",  # one id per occurrence of a recurring meeting
        title=str(event.get("SUMMARY", "")).strip() or "(no title)",
        start=begins.isoformat(),
        end=ends.isoformat(),
        all_day=all_day,
        description=description[:DESCRIPTION_CHARS],
        attendees=_attendees(event),
        url=_link(event, description),
    )


def _end(event: Any, begins: date, all_day: bool) -> date:
    if event.get("DTEND") is not None:
        return event.get("DTEND").dt
    if event.get("DURATION") is not None:
        return begins + event.get("DURATION").dt
    return begins + timedelta(days=1) if all_day else begins


def _aware(moment: datetime, local_now: datetime) -> datetime:
    """Floating times (no zone) mean "wall clock wherever you are": this machine's zone."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=local_now.tzinfo)


def _attendees(event: Any) -> tuple[str, ...]:
    raw = event.get("ATTENDEE")
    if raw is None:
        return ()
    names = []
    for attendee in raw if isinstance(raw, list) else [raw]:
        name = str(attendee.params.get("CN", "")).strip() or str(attendee).removeprefix("mailto:").removeprefix("MAILTO:")
        if name and name not in names:
            names.append(name)
    return tuple(names[:MAX_ATTENDEES])


def _link(event: Any, description: str) -> str:
    """What Enter opens: the video call if there is one, else the event's URL, else the first link.

    Invitations carry several links (a "need help?" page before the join link), and
    Outlook wraps them all in Safe Links, so each is unwrapped before choosing.
    """
    url = str(event.get("URL", ""))
    links = [_unwrap(m.group(0).rstrip(".,;)")) for text in (url, str(event.get("LOCATION", "")), description) for m in LINK.finditer(text)]
    call = next((link for link in links if MEETING.search(link)), "")
    if call:
        return call
    if url.startswith(("https://", "http://")):
        return _unwrap(url)
    return links[0] if links else ""


def _unwrap(link: str) -> str:
    """Outlook Safe Links hide the real address in the `url` query parameter."""
    parsed = urlsplit(link)
    if parsed.hostname and parsed.hostname.endswith(".safelinks.protection.outlook.com"):
        real = parse_qs(parsed.query).get("url")
        if real:
            return real[0]
    return link
