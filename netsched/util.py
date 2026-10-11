"""Small shared helpers."""
from __future__ import annotations

import datetime as dt


def base_call(callsign: str) -> str:
    """Return the callsign without its SSID, upper-cased."""
    return callsign.strip().upper().split("-")[0]


def same_station(net_callsign: str, src: str) -> bool:
    """True when `src` is the net's callsign (exact match, or same base call)."""
    a, b = net_callsign.strip().upper(), src.strip().upper()
    return a == b or base_call(a) == base_call(b)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    """UTC ISO-8601 string; lexicographic order equals chronological order."""
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
