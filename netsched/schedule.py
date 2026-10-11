"""Schedule engine: decides which nets are due and what to send."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_MESSAGE_LEN = 67  # APRS message text limit
SCHEDULE_TYPES = {"daily", "weekly", "nth_weekday", "date_range"}
MODES = {"fixed", "by_date", "by_weekday"}
CERT_MODES = {"link", "code", "email", "none"}


class ConfigError(ValueError):
    """Raised when the nets file contains invalid data."""


# ---------------------------------------------------------------- converters

def _to_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"invalid date: {value!r}") from exc


def _to_time(value) -> dt.time:
    if isinstance(value, dt.time):
        return value
    if isinstance(value, int):
        # YAML 1.1 reads an unquoted 13:00 as a base-60 integer (780 minutes).
        try:
            return dt.time(value // 60, value % 60)
        except ValueError as exc:
            raise ConfigError(f"invalid time: {value!r}") from exc
    try:
        hour, minute = str(value).strip().split(":")[:2]
        return dt.time(int(hour), int(minute))
    except ValueError as exc:
        raise ConfigError(f"invalid time: {value!r}") from exc


parse_time = _to_time  # public name for other modules


def _to_weekday(value) -> int:
    key = str(value).strip().lower()[:3]
    if key not in WEEKDAYS:
        raise ConfigError(f"invalid weekday: {value!r}")
    return WEEKDAYS.index(key)


def _fill(template: str, **values: str) -> str:
    for key, val in values.items():
        template = template.replace("{" + key + "}", val)
    return template


def check_timezone(name: str, where: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{where}: unknown timezone {name!r}") from exc
    return name


def _parse_words(callsign: str, raw) -> tuple:
    """`words:` mapping -> (date -> word, date -> run time).

    A value is either the word itself or a mapping `{word: X, at: "14:30"}`.
    """
    words, times = {}, {}
    for key, value in (raw or {}).items():
        day = _to_date(key)
        if isinstance(value, dict):
            if not value.get("word"):
                raise ConfigError(f"{callsign}: words entry for {day} needs 'word'")
            words[day] = str(value["word"])
            if value.get("at"):
                times[day] = _to_time(value["at"])
        else:
            words[day] = str(value)
    return words, times


# ------------------------------------------------------------------ schedule

@dataclass(frozen=True)
class Schedule:
    type: str = "daily"
    weekdays: tuple = ()
    nth: int | None = None
    start: dt.date | None = None
    end: dt.date | None = None
    after: dt.time = dt.time(0, 0)   # the window opens (net-local time); `at:` is the exact-time form
    before: dt.time | None = None    # the window closes; nothing is sent after it
    spread: int | None = None        # minutes over which the send time is spread (None = config default)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Schedule":
        data = data or {}
        stype = data.get("type", "daily")
        if stype not in SCHEDULE_TYPES:
            raise ConfigError(f"unknown schedule type: {stype!r}")
        raw_days = data.get("weekdays", data.get("weekday", []))
        if not isinstance(raw_days, (list, tuple)):
            raw_days = [raw_days]
        if "at" in data and "after" in data:
            raise ConfigError("use either 'at' or 'after' in a schedule, not both")
        spread = data.get("spread")
        if "at" in data and spread is None:
            spread = 0  # an exact time is never spread
        try:
            spread = None if spread is None else int(spread)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid spread: {spread!r}") from exc
        schedule = cls(
            type=stype,
            weekdays=tuple(sorted({_to_weekday(d) for d in raw_days})),
            nth=data.get("nth"),
            start=_to_date(data["start"]) if data.get("start") else None,
            end=_to_date(data["end"]) if data.get("end") else None,
            after=_to_time(data.get("at", data.get("after", "00:00"))),
            before=_to_time(data["before"]) if data.get("before") else None,
            spread=spread,
        )
        schedule._validate()
        return schedule

    def _validate(self) -> None:
        if self.type in ("weekly", "nth_weekday") and not self.weekdays:
            raise ConfigError(f"schedule type {self.type!r} needs 'weekdays'")
        if self.type == "nth_weekday" and self.nth not in (1, 2, 3, 4, 5, -1):
            raise ConfigError("nth_weekday needs 'nth' in 1-5 or -1")
        if self.type == "date_range" and self.start is None:
            raise ConfigError("date_range needs 'start'")
        if self.start and self.end and self.end < self.start:
            raise ConfigError("schedule 'end' is before 'start'")
        if self.before is not None and self.before < self.after:
            raise ConfigError("schedule 'before' is earlier than 'after'")
        if self.spread is not None and self.spread < 0:
            raise ConfigError("schedule 'spread' cannot be negative")

    def is_active_on(self, day: dt.date) -> bool:
        if self.start and day < self.start:
            return False
        if self.end and day > self.end:
            return False
        if self.weekdays and day.weekday() not in self.weekdays:
            return False
        if self.type == "nth_weekday":
            if self.nth == -1:  # last occurrence of the weekday in the month
                return (day + dt.timedelta(days=7)).month != day.month
            return (day.day - 1) // 7 + 1 == self.nth
        return True


# ----------------------------------------------------------------------- net

@dataclass
class Net:
    callsign: str
    schedule: Schedule
    mode: str = "fixed"
    message: str = ""
    template: str = "{word}"
    words: dict = field(default_factory=dict)            # date -> word
    weekday_words: dict = field(default_factory=dict)    # weekday index -> word
    word_times: dict = field(default_factory=dict)       # date -> exact run time for that day's word
    certificate: str = "link"
    ack: bool = True
    enabled: bool = True
    tz: str | None = None
    notes: str = ""
    sporadic: bool = False      # no word for the day = nothing to do (not a warning)
    info_url: str = ""          # net website, shown for manual steps
    word_source: str = ""       # where the daily word is published, shown in warnings

    @classmethod
    def from_dict(cls, data: dict) -> "Net":
        if not isinstance(data, dict) or "callsign" not in data:
            raise ConfigError(f"net entry without callsign: {data!r}")
        callsign = str(data["callsign"]).strip().upper()
        mode = data.get("mode", "fixed")
        if mode not in MODES:
            raise ConfigError(f"{callsign}: unknown mode {mode!r}")
        certificate = data.get("certificate", "link")
        if certificate not in CERT_MODES:
            raise ConfigError(f"{callsign}: unknown certificate mode {certificate!r}")
        message = str(data.get("message", "") or "")
        if mode == "fixed" and not message.strip():
            raise ConfigError(f"{callsign}: mode 'fixed' needs 'message'")
        tz = data.get("tz")
        if tz:
            check_timezone(str(tz), callsign)
        words, word_times = _parse_words(callsign, data.get("words"))
        return cls(
            callsign=callsign,
            schedule=Schedule.from_dict(data.get("schedule")),
            mode=mode,
            message=message,
            template=str(data.get("template", "{word}")),
            words=words,
            word_times=word_times,
            weekday_words={_to_weekday(k): str(v) for k, v in (data.get("words_by_weekday") or {}).items()},
            certificate=certificate,
            ack=bool(data.get("ack", True)),
            enabled=bool(data.get("enabled", True)),
            tz=str(tz) if tz else None,
            notes=str(data.get("notes", "") or ""),
            sporadic=bool(data.get("sporadic", False)),
            info_url=str(data.get("info_url", "") or ""),
            word_source=str(data.get("word_source", "") or ""),
        )

    def word_for(self, day: dt.date) -> str | None:
        if self.mode == "by_date":
            return self.words.get(day)
        if self.mode == "by_weekday":
            return self.weekday_words.get(day.weekday())
        return None

    def render(self, day: dt.date, signature: str = "") -> str | None:
        """Message text for `day`, or None when the net has no word for that day."""
        if self.mode == "fixed":
            template, word = self.message, ""
        else:
            word = self.word_for(day)
            if word is None:
                return None
            template = self.template
        week = str(day.isocalendar()[1])  # ISO week of the year (APRSThursday "#41")
        return _fill(template, word=word, sig=signature, week=week).strip()


# ------------------------------------------------------------------ decision

@dataclass
class Decision:
    net: Net
    state: str  # due | waiting | done | closed | inactive | disabled | no_event | missing_word | too_long
    day: dt.date
    text: str | None = None
    detail: str = ""
    send_at: dt.datetime | None = None   # when the message is (or was) due


def _offset_seconds(callsign: str, day: dt.date, spread_minutes: int) -> int:
    """Stable pseudo-random delay (0..spread) for this net on this day.

    Derived from a hash, so it is the same after a restart, yet differs between
    nets and days: the nets are not all contacted in the first second of the day.
    """
    if spread_minutes <= 0:
        return 0
    digest = hashlib.sha256(f"{callsign}|{day.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (spread_minutes * 60 + 1)


def send_time(net: Net, day: dt.date, tz: ZoneInfo, default_spread: int = 0) -> dt.datetime:
    """The instant `net` is to be contacted on `day` (a date in the net's own timezone)."""
    explicit = net.word_times.get(day) if net.mode == "by_date" else None
    if explicit is not None:
        opens, spread = explicit, 0                       # a time given for the event is exact
    else:
        opens = net.schedule.after
        spread = net.schedule.spread if net.schedule.spread is not None else default_spread
    when = dt.datetime.combine(day, opens, tzinfo=tz) + dt.timedelta(
        seconds=_offset_seconds(net.callsign, day, spread)
    )
    latest = dt.datetime.combine(day, net.schedule.before or dt.time(23, 59, 59), tzinfo=tz)
    return min(when, latest)


def evaluate(
    net: Net,
    now: dt.datetime,
    signature: str = "",
    default_tz: str = "UTC",
    is_done: Callable[[str, dt.date, str], bool] = lambda callsign, day, text: False,
    default_spread: int = 0,
) -> Decision:
    """Decide what should happen for `net` at the (timezone-aware) instant `now`.

    The net's "day" is the calendar date in its own timezone (`tz`, else `default_tz`).
    """
    tz = ZoneInfo(net.tz or default_tz)
    local = now.astimezone(tz)
    day = local.date()
    if not net.enabled:
        return Decision(net, "disabled", day)
    if not net.schedule.is_active_on(day):
        return Decision(net, "inactive", day)
    text = net.render(day, signature)
    if text is None:
        if net.sporadic:
            return Decision(net, "no_event", day)
        hint = f" (source: {net.word_source})" if net.word_source else ""
        return Decision(net, "missing_word", day, detail=f"no word for {day.isoformat()}{hint}")
    if len(text) > MAX_MESSAGE_LEN:
        return Decision(net, "too_long", day, text, f"{len(text)} chars (max {MAX_MESSAGE_LEN})")
    send_at = send_time(net, day, tz, default_spread)
    if is_done(net.callsign, day, text):
        return Decision(net, "done", day, text, send_at=send_at)
    if net.schedule.before is not None and local > dt.datetime.combine(day, net.schedule.before, tzinfo=tz):
        return Decision(net, "closed", day, text, f"window closed at {net.schedule.before:%H:%M}", send_at)
    if now < send_at:
        return Decision(net, "waiting", day, text, send_at=send_at)
    return Decision(net, "due", day, text, send_at=send_at)


# ------------------------------------------------------------------- loading

def merge_words_csv(nets: list, path: str | Path) -> None:
    """Merge `callsign,date,word[,time]` rows into the by_date nets (in place).

    `time` (HH:MM, net-local) is optional: it fixes the exact run time for that day.
    """
    by_call: dict = {}
    for net in nets:
        by_call.setdefault(net.callsign, []).append(net)
    with open(path, newline="", encoding="utf-8") as fh:
        lines = (line for line in fh if line.strip() and not line.lstrip().startswith("#"))
        for row_no, row in enumerate(csv.DictReader(lines), start=1):
            call = (row.get("callsign") or "").strip().upper()
            targets = [n for n in by_call.get(call, []) if n.mode == "by_date"]
            if not targets:
                raise ConfigError(f"{path} row {row_no}: no by_date net registered for {call!r}")
            word = (row.get("word") or "").strip()
            if not word:
                raise ConfigError(f"{path} row {row_no}: empty word")
            day = _to_date(row.get("date"))
            run_time = (row.get("time") or "").strip()
            at = _to_time(run_time) if run_time else None
            for net in targets:
                net.words[day] = word
                if at is not None:
                    net.word_times[day] = at
                else:
                    net.word_times.pop(day, None)  # the latest registration wins


def load_nets(path: str | Path, words_csv: str | Path | None = None) -> list:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    entries = raw.get("nets") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: expected a top-level 'nets:' list")
    nets = [Net.from_dict(entry) for entry in entries]
    if words_csv and Path(words_csv).exists():
        merge_words_csv(nets, words_csv)
    return nets
