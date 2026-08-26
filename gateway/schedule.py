"""When a slot is due, and by how much it moves.

Pure functions over a clock passed in, so every case here is testable without
patching `datetime.now` or waiting for a Tuesday.

Two decisions carry the whole file:

**Local wall clock, not a UTC offset.** A slot is "18:00 Europe/Oslo", which is
a different UTC instant in July than in January. Storing the offset instead
would silently move every post by an hour twice a year, in the direction nobody
notices until the analytics look odd.

**The jitter is derived, never rolled.** Posting at exactly 18:00:00 every day
is a tell, so each firing moves by up to `jitter_minutes` either way. The
obvious implementation, a random offset picked when the tick runs, is wrong in a
way that only shows up in production: the offset is re-rolled on every restart,
so a post judged not-yet-due at 17:55 can be judged due at 17:50 by the next
process, and a slot that already fired can come due again. Hashing the slot and
the local date instead gives an offset that is stable for that day, different
the next, and identical in every replica and every replay.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A slot that has been due for longer than this is not fired late. The laptop
# being off, or the cluster being down, should cost that day's post rather than
# firing it at 03:00 when the pod finally starts, which is both a strange time
# to post and a surprise to whoever queued it.
DEFAULT_GRACE_MINUTES = 90


@dataclass(frozen=True)
class Slot:
    """A schedule_slots row, with the parsing done once."""

    id: int
    account_id: str
    hour: int
    minute: int
    tz: str
    jitter_minutes: int
    days: frozenset[int]  # ISO weekdays, 1=Monday. Empty means every day.

    @classmethod
    def from_row(cls, row: Any) -> Slot:
        return cls(
            id=int(row["id"]),
            account_id=str(row["account_id"]),
            hour=int(row["hour"]),
            minute=int(row["minute"]),
            tz=str(row["tz"] or "UTC"),
            jitter_minutes=int(row["jitter_minutes"] or 0),
            days=parse_days(row["days"]),
        )

    @property
    def zone(self) -> ZoneInfo:
        return zone_or_utc(self.tz)

    @property
    def day_names(self) -> str:
        """"Sat, Sun", or "every day". Its own property because the template
        used to recover this by splitting `describe()` on a comma, which turned
        a Saturday and Sunday slot into a Sunday one."""
        if not self.days:
            return "every day"
        return ", ".join(_DAY_NAMES[d] for d in sorted(self.days))

    def describe(self) -> str:
        when = f"{self.hour:02d}:{self.minute:02d}"
        jitter = f" plus or minus {self.jitter_minutes}m" if self.jitter_minutes else ""
        return f"{when} {self.tz}{jitter}, {self.day_names}"


_DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


def zone_or_utc(name: str) -> ZoneInfo:
    """Never raise over a timezone.

    A bad zone name in the database would otherwise take the scheduler down for
    every account, not just the one with the typo. UTC is wrong but visible.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def parse_days(raw: str | None) -> frozenset[int]:
    """"1,3,5" to {1, 3, 5}. Empty, blank or unparseable means every day."""
    if not raw:
        return frozenset()
    out = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 7:
            out.add(int(part))
    return frozenset(out)


def format_days(days: frozenset[int] | set[int]) -> str:
    return ",".join(str(d) for d in sorted(days))


# Minutes past the hour that a human would never land on by accident. A post at
# exactly 18:00:00 every evening is the single cheapest thing to spot in a
# timestamp column, and :15, :30 and :45 are only slightly less obvious.
_ROUND_MINUTES = frozenset({0, 15, 30, 45})
# How many derived candidates to try before giving up on avoiding a round
# minute. Bounded so this stays a pure function with no worst case.
_MAX_JITTER_TRIES = 8


def _derive(slot_id: int, local_day: date, salt: int, span_s: int) -> int:
    digest = hashlib.sha256(f"{slot_id}:{local_day.isoformat()}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % span_s


def jitter_offset(
    slot_id: int, local_day: date, jitter_minutes: int, *, base_minute: int | None = None
) -> timedelta:
    """A stable pseudo-random offset in [-jitter, +jitter], to the second.

    Derived from the slot and the date, so it survives a restart, agrees across
    replicas, and can be asserted in a test. See the module docstring for why
    this is not `random.randint`.

    Resolution is seconds rather than minutes on purpose. An account that
    always posts exactly on the minute is still posting on a grid, just a
    coarser one.

    When `base_minute` is given, offsets that would land the post on a round
    minute are skipped. That is the difference between a schedule that looks
    like a person and one that looks like a cron entry.
    """
    if jitter_minutes <= 0:
        return timedelta()

    span_s = jitter_minutes * 120 + 1  # the full window, in seconds
    fallback = 0
    for salt in range(_MAX_JITTER_TRIES):
        seconds = _derive(slot_id, local_day, salt, span_s) - jitter_minutes * 60
        if salt == 0:
            fallback = seconds
        if base_minute is None:
            return timedelta(seconds=seconds)
        # Where this candidate actually lands, as minutes past the hour.
        landed = (base_minute * 60 + seconds) // 60 % 60
        if landed not in _ROUND_MINUTES:
            return timedelta(seconds=seconds)
    return timedelta(seconds=fallback)


def fire_time(slot: Slot, local_day: date) -> datetime:
    """The exact instant this slot fires on this local date, jitter included.

    The jitter is added in UTC, not in the local zone. Adding a timedelta to a
    zone-aware datetime is wall-clock arithmetic in Python, so on the two days a
    year that have a DST transition, a local-zone addition would skip or repeat
    an hour rather than moving the instant by the minutes asked for.
    """
    naive = datetime.combine(local_day, time(hour=slot.hour, minute=slot.minute))
    base = naive.replace(tzinfo=slot.zone).astimezone(UTC)
    return base + jitter_offset(
        slot.id, local_day, slot.jitter_minutes, base_minute=slot.minute
    )


def runs_on(slot: Slot, local_day: date) -> bool:
    return not slot.days or local_day.isoweekday() in slot.days


def due(
    slot: Slot, moment: datetime, *, grace_minutes: int = DEFAULT_GRACE_MINUTES
) -> date | None:
    """The local date this slot owes a post for, or None if it owes nothing.

    Yesterday is checked as well as today, because a slot at 23:50 in a zone
    ahead of UTC is still yesterday's business shortly after midnight, and
    because the tick that should have caught it may have been a minute late.
    The grace window is what stops a service that was down all night publishing
    at breakfast.
    """
    local_now = moment.astimezone(slot.zone)
    for day in (local_now.date(), local_now.date() - timedelta(days=1)):
        if not runs_on(slot, day):
            continue
        target = fire_time(slot, day)
        if target <= moment <= target + timedelta(minutes=grace_minutes):
            return day
    return None


def next_fire(slot: Slot, moment: datetime, *, horizon_days: int = 14) -> datetime | None:
    """When this slot fires next, for the admin UI's benefit."""
    local_today = moment.astimezone(slot.zone).date()
    for offset in range(horizon_days + 1):
        day = local_today + timedelta(days=offset)
        if not runs_on(slot, day):
            continue
        target = fire_time(slot, day)
        if target > moment:
            return target
    return None


@dataclass(frozen=True)
class SlotSpec:
    """A slot declared in configuration rather than clicked into the UI."""

    hour: int
    minute: int
    tz: str
    jitter_minutes: int
    days: str  # already in the stored "1,3,5" form
    # Which destination this slot belongs to. Empty means the one account, and
    # `GATEWAY_SLOTS_ACCOUNT` or a lone registered account resolves it. Naming
    # it per line is what lets one config hold several channels, which the UI
    # cannot do because slots clicked there do not survive a redeploy.
    account: str = ""


class SlotSpecError(ValueError):
    """A slot declaration that could not be read. Names the line."""


def parse_slots(raw: str, *, default_tz: str = "UTC", default_jitter: int = 15) -> list[SlotSpec]:
    """Read `GATEWAY_SLOTS` into slot specs.

    One slot per line or per semicolon, so the value drops into a ConfigMap as
    a block string and reads as a schedule rather than as JSON:

        18:00 Europe/Oslo jitter=15
        08:30 Europe/Oslo jitter=20 days=6,7
        12:00
        19:30 Europe/Oslo account=UCq0Ff3lJ7dK2sWnEv8mXtLp

    Only the time is required. `days` is ISO weekdays, 1 for Monday, and
    leaving it out means every day. A `#` starts a comment, because a schedule
    is exactly the kind of config someone wants to leave a note on.

    `account` names the destination and is how one config holds more than one.
    Without it a line belongs to `GATEWAY_SLOTS_ACCOUNT`, or to the single
    registered Instagram account when that is unambiguous. The admin UI is not
    an answer for a second permanent channel, because slots clicked there are a
    separate set that the next rollout does not rewrite.

    Raises rather than skipping a bad line. A schedule that silently drops the
    slot with the typo is a schedule that quietly stops posting.
    """
    specs: list[SlotSpec] = []
    for chunk in raw.replace(";", "\n").splitlines():
        line = chunk.split("#", 1)[0].strip()
        if not line:
            continue

        parts = line.split()
        clock = parts[0]
        if ":" not in clock:
            raise SlotSpecError(f"{line!r}: expected a time like 18:00")
        hh, _, mm = clock.partition(":")
        try:
            hour, minute = int(hh), int(mm)
        except ValueError:
            raise SlotSpecError(f"{line!r}: {clock!r} is not a time") from None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise SlotSpecError(f"{line!r}: {clock!r} is out of range")

        tz, jitter, days, account = default_tz, default_jitter, "", ""
        for token in parts[1:]:
            key, sep, value = token.partition("=")
            if not sep:
                tz = key  # a bare token is the zone
            elif key == "account":
                account = value.strip()
                if not account:
                    raise SlotSpecError(f"{line!r}: account= named nothing")
            elif key == "jitter":
                try:
                    jitter = max(0, int(value))
                except ValueError:
                    raise SlotSpecError(f"{line!r}: jitter={value!r} is not a number") from None
            elif key == "days":
                parsed = parse_days(value)
                if not parsed:
                    raise SlotSpecError(f"{line!r}: days={value!r} named no weekday")
                days = format_days(parsed)
            else:
                raise SlotSpecError(f"{line!r}: unknown setting {key!r}")

        # Checked here rather than at fire time, so a typo fails at startup with
        # the line in the message instead of silently posting in UTC.
        if str(zone_or_utc(tz)) != tz and tz != "UTC":
            raise SlotSpecError(f"{line!r}: {tz!r} is not a known timezone")

        specs.append(
            SlotSpec(
                hour=hour,
                minute=minute,
                tz=tz,
                jitter_minutes=jitter,
                days=days,
                account=account,
            )
        )
    return specs


def projected_times(
    slots: list[Slot], moment: datetime, count: int, *, horizon_days: int = 60
) -> list[datetime]:
    """The next `count` firings across every slot, in order.

    This is what lets the queue page show each post a real publish time rather
    than a position in a list, which is the difference between "it is queued"
    and "it goes out on Thursday evening".
    """
    if not slots or count <= 0:
        return []
    times: list[datetime] = []
    for slot in slots:
        # Each slot contributes at most `count` of its own firings before the
        # merge. Capping the shared list instead lets the first slot fill it
        # entirely, so a morning and an evening slot come back as four mornings.
        mine = 0
        local_today = moment.astimezone(slot.zone).date()
        for offset in range(horizon_days + 1):
            if mine >= count:
                break
            day = local_today + timedelta(days=offset)
            if not runs_on(slot, day):
                continue
            target = fire_time(slot, day)
            if target > moment:
                times.append(target)
                mine += 1
    return sorted(times)[:count]
