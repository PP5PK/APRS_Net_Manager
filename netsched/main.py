"""Command-line interface and daemon loop."""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import io
import logging
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .aprsis import AprsIsClient, AuthError, DryRunClient, aprs_passcode, parse_message
from .certificates import download, extract_urls, safe_name
from .db import Database
from .schedule import ConfigError, check_timezone, evaluate, load_nets, parse_time
from .util import same_station, utcnow

log = logging.getLogger("netsched")

DEFAULTS = {
    "timezone": "America/Sao_Paulo",   # your own: used to display times and to read `today --at`
    "net_timezone": "UTC",             # the default timezone that defines a net's day (a net can set `tz:`)
    "station": {"callsign": "", "passcode": "", "signature": ""},
    "aprs_is": {"host": "rotate.aprs2.net", "port": 14580},
    "paths": {
        "nets": "nets.yaml",
        "words_csv": "words.csv",
        "database": "data/netsched.db",
        "certificates": "certs",
    },
    "send": {
        "tick_seconds": 30,
        "gap_seconds": 5,
        "retries": 3,
        "retry_interval": 60,
        "default_spread_minutes": 60,  # spread send times after the window opens (0 = exactly on time)
    },
}


# -------------------------------------------------------------------- config

def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    try:
        user = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {cfg_path}: {exc}") from exc
    cfg = _merge(copy.deepcopy(DEFAULTS), user)  # never mutate the module-level defaults
    if not cfg["station"]["callsign"]:
        raise ConfigError("station.callsign is required in the config file")
    for key in ("timezone", "net_timezone"):
        check_timezone(str(cfg[key]), f"config '{key}'")
    if int(cfg["send"]["default_spread_minutes"]) < 0:
        raise ConfigError("send.default_spread_minutes cannot be negative")
    for key, value in cfg["paths"].items():
        cfg["paths"][key] = str((cfg_path.parent / value).resolve())
    return cfg


class NetStore:
    """Loads the nets file and reloads it whenever the file (or words CSV) changes."""

    def __init__(self, cfg: dict):
        self.path = Path(cfg["paths"]["nets"])
        self.words_csv = Path(cfg["paths"]["words_csv"])
        self._mtime = self._current_mtime()
        self._nets = load_nets(self.path, self.words_csv)  # fail fast on first load

    def _current_mtime(self) -> float:
        times = [self.path.stat().st_mtime]
        if self.words_csv.exists():
            times.append(self.words_csv.stat().st_mtime)
        return max(times)

    def get(self) -> list:
        mtime = self._current_mtime()
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._nets = load_nets(self.path, self.words_csv)
                log.info("reloaded %d nets", len(self._nets))
            except ConfigError as exc:
                log.error("nets file not reloaded, keeping previous version: %s", exc)
        return self._nets


# -------------------------------------------------------------------- daemon

def _info_link(net) -> str:
    """The net's website as a full URL, for certificates that need a manual step."""
    link = net.info_url.strip()
    if not link:
        return "-"
    return link if link.startswith("http") else "https://" + link


class Daemon:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.callsign = cfg["station"]["callsign"].upper()
        self.signature = cfg["station"]["signature"]
        self.tz = cfg["net_timezone"]
        self.spread = int(cfg["send"]["default_spread_minutes"])
        self.db = Database(":memory:" if dry_run else cfg["paths"]["database"])
        self.store = NetStore(cfg)
        self.cert_dir = Path(cfg["paths"]["certificates"])
        self.client = None
        self._warned: set = set()

    # ------------------------------------------------------------ connection

    def _connect(self) -> None:
        if self.dry_run:
            self.client = DryRunClient(self.callsign)
            return
        passcode = os.environ.get("APRS_PASSCODE") or self.cfg["station"]["passcode"] or aprs_passcode(self.callsign)
        aprs = self.cfg["aprs_is"]
        client = AprsIsClient(aprs["host"], int(aprs["port"]), self.callsign, str(passcode), f"g/{self.callsign}")
        client.connect()
        self.client = client

    def _close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def pump(self, seconds: float) -> None:
        """Receive and handle packets for `seconds`."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            for line in self.client.poll(min(1.0, remaining)):
                self.handle_line(line)

    # -------------------------------------------------------------- sending

    def tick(self) -> None:
        now = utcnow()
        send_cfg = self.cfg["send"]
        for net in self.store.get():
            decision = evaluate(net, now, self.signature, self.tz, self.db.is_done, self.spread)
            if decision.state in ("missing_word", "too_long"):
                key = (net.callsign, decision.day, decision.state)
                if key not in self._warned:
                    self._warned.add(key)
                    log.warning("%s skipped: %s", net.callsign, decision.detail)
                continue
            if decision.state != "due":
                continue
            send_id = self.db.create_send(net.callsign, decision.day, decision.text, now)
            if send_id is None:
                continue
            msgid = self.db.assign_msgid(send_id)
            self.client.send_message(net.callsign, decision.text, msgid)
            self.db.record_attempt(send_id, now, "sent" if net.ack else "unconfirmed")
            log.info("sent to %s: %s (msgid %s)", net.callsign, decision.text, msgid)
            self.pump(send_cfg["gap_seconds"])
        self.retry_unacked(now)

    def retry_unacked(self, now: dt.datetime) -> None:
        send_cfg = self.cfg["send"]
        for row in self.db.due_retries(send_cfg["retries"], send_cfg["retry_interval"], now):
            self.client.send_message(row["callsign"], row["text"], row["msgid"])
            self.db.record_attempt(row["id"], utcnow())
            log.info("retry %d to %s (no ACK yet)", row["attempts"] + 1, row["callsign"])
            self.pump(send_cfg["gap_seconds"])
        expired = self.db.expire_unacked(send_cfg["retries"], send_cfg["retry_interval"], utcnow())
        if expired:
            log.warning("%d message(s) never acknowledged", expired)

    # ------------------------------------------------------------ receiving

    def find_net(self, src: str):
        nets = self.store.get()
        for net in nets:
            if net.callsign == src:
                return net
        for net in nets:
            if same_station(net.callsign, src):
                return net
        return None

    def handle_line(self, line: str) -> None:
        msg = parse_message(line)
        if msg is None or msg.dst != self.callsign:
            return
        now = utcnow()
        if msg.ack:
            kind, msgid = msg.ack
            if self.db.mark_ack(msgid, msg.src, now, rejected=(kind == "rej")):
                log.info("%s %s from %s", kind.upper(), msgid, msg.src)
            return
        if msg.msgno:
            self.client.send_message(msg.src, "ack" + msg.msgno)
        net = self.find_net(msg.src)
        if net is None:
            log.debug("ignoring message from unknown station %s", msg.src)
            return
        if self.db.reply_exists(msg.src, msg.text, now):
            return
        send = self.db.latest_send_for(msg.src, now)
        reply_id = self.db.add_reply(send["id"] if send else None, msg.src, msg.text, msg.msgno, now)
        log.info("reply from %s: %s", msg.src, msg.text)
        self.collect_certificates(net, reply_id, msg.text, now)

    def collect_certificates(self, net, reply_id: int, text: str, now: dt.datetime) -> None:
        if net.certificate == "none":
            return
        urls = extract_urls(text)
        if net.certificate in ("code", "email"):
            # Nothing to download. Keep the reply (it may carry the code) for the manual step.
            if net.certificate == "code":
                status, note = "manual", f"code required on the website; reply: {text}"
            else:
                status, note = "email", f"certificate is sent by e-mail; reply: {text}"
            self.db.add_certificate(reply_id, urls[0] if urls else _info_link(net), status, None, note, now)
            log.info("%s: %s", net.callsign, note)
            return
        day = now.astimezone(ZoneInfo(net.tz or self.tz)).date()
        folder = self.cert_dir / safe_name(net.callsign)
        for url in urls:
            result = download(url, folder, day)
            self.db.add_certificate(reply_id, url, result.status, result.path, result.error, now)
            log.info("%s: %s %s %s", net.callsign, result.status, url, result.path or result.error or "")

    # ----------------------------------------------------------------- loop

    def run(self) -> None:
        backoff, next_tick = 5, 0.0
        tick_every = self.cfg["send"]["tick_seconds"]
        log.info("started as %s%s", self.callsign, " (dry-run)" if self.dry_run else "")
        while True:
            try:
                if self.client is None:
                    self._connect()
                    backoff = 5
                if time.monotonic() >= next_tick:
                    self.tick()
                    next_tick = time.monotonic() + tick_every
                self.pump(1.0)
            except AuthError as exc:
                log.error("%s", exc)
                return
            except KeyboardInterrupt:
                log.info("stopping")
                self._close()
                return
            except (ConnectionError, OSError) as exc:
                log.warning("connection problem: %s; retrying in %d s", exc, backoff)
                self._close()
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)


# ------------------------------------------------------------------ commands

def _resolve_now(at: str | None, tz: str) -> dt.datetime:
    if not at:
        return utcnow()
    moment = dt.datetime.fromisoformat(at)
    return moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo(tz))


def cmd_today(cfg: dict, args: argparse.Namespace) -> int:
    now = _resolve_now(args.at, cfg["timezone"])
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    db = Database(cfg["paths"]["database"])
    nets = NetStore(cfg).get()
    order = {"due": 0, "waiting": 1, "missing_word": 2, "too_long": 3, "closed": 4, "done": 5,
             "no_event": 6, "inactive": 7, "disabled": 8}
    spread = int(cfg["send"]["default_spread_minutes"])
    mine = ZoneInfo(cfg["timezone"])
    rows = [evaluate(n, now, cfg["station"]["signature"], cfg["net_timezone"], db.is_done, spread) for n in nets]
    print(f"{local:%Y-%m-%d (%a) %H:%M} {cfg['timezone']}  |  net days follow {cfg['net_timezone']} unless a net sets tz")
    print(f"{'STATE':<13} {'CALLSIGN':<12} {'SEND AT (UTC / yours)':<22} MESSAGE / DETAIL")
    for d in sorted(rows, key=lambda d: (order[d.state], d.net.callsign)):
        if d.state in ("inactive", "disabled", "no_event") and not args.all:
            continue
        info = d.text or ""
        if d.detail:
            info = f"{info}  [{d.detail}]".strip()
        when = ""
        if d.send_at is not None:
            when = f"{d.send_at.astimezone(dt.timezone.utc):%d %H:%M}Z / {d.send_at.astimezone(mine):%H:%M}"
        print(f"{d.state:<13} {d.net.callsign:<12} {when:<22} {info}")
    return 0


def cmd_report(cfg: dict, args: argparse.Namespace) -> int:
    db = Database(cfg["paths"]["database"])
    rows = db.open_certificates()
    if not rows:
        print("Nothing needs manual attention.")
        return 0
    for row in rows:
        print(f"{row['reply_at']}  {row['src']:<12} {row['status']:<7} {row['url']}  ({row['error']})")
    return 0


def cmd_addword(cfg: dict, args: argparse.Namespace) -> int:
    """Register a keyword for one or more days in words.csv (by_date nets only)."""
    call = args.callsign.strip().upper()
    net = next((n for n in NetStore(cfg).get() if n.callsign == call and n.mode == "by_date"), None)
    if net is None:
        raise ConfigError(f"{call} is not registered as a by_date net in the nets file")
    tz_name = net.tz or cfg["net_timezone"]  # the net's day (and the --at time) follow this zone
    today = utcnow().astimezone(ZoneInfo(tz_name)).date()
    try:
        start = dt.date.fromisoformat(args.start) if args.start else today
        end = dt.date.fromisoformat(args.end) if args.end else start
    except ValueError as exc:
        raise ConfigError(f"invalid date: {exc}") from exc
    if end < start:
        raise ConfigError("--to is before --from")
    at = f"{parse_time(args.at):%H:%M}" if args.at else ""
    word = " ".join(args.word).strip()

    path = Path(cfg["paths"]["words_csv"])
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    header = next((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), None)
    if header is None:
        lines.append("callsign,date,word,time")
    elif lines[header].strip() == "callsign,date,word":
        lines[header] = "callsign,date,word,time"  # upgrade the older layout
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for offset in range((end - start).days + 1):
        writer.writerow([call, (start + dt.timedelta(days=offset)).isoformat(), word, at])
    path.write_text("\n".join(lines) + "\n" + buffer.getvalue(), encoding="utf-8")

    period = start.isoformat() + (f" to {end.isoformat()}" if end > start else "")
    when = f"at {at} {tz_name}" if at else f"from the start of the day ({tz_name}, spread)"
    print(f"{call}: '{word}' registered for {period} {when}")
    return 0


def cmd_run(cfg: dict, args: argparse.Namespace) -> int:
    Daemon(cfg, dry_run=args.dry_run).run()
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netsched", description="APRS net check-in scheduler")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    today = sub.add_parser("today", help="show what is due (nothing is sent)")
    today.add_argument("--at", help="simulate a moment, e.g. 2026-10-11T14:00 (config timezone)")
    today.add_argument("--all", action="store_true", help="also list inactive and disabled nets")
    today.set_defaults(func=cmd_today)

    run = sub.add_parser("run", help="run the scheduler daemon")
    run.add_argument("--dry-run", action="store_true", help="do not connect; only log what would be sent")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="list certificates that need manual download")
    report.set_defaults(func=cmd_report)

    addword = sub.add_parser("addword", help="register a keyword for a by_date net (appends to words.csv)")
    addword.add_argument("callsign")
    addword.add_argument("word", nargs="+", help="the keyword; several words are joined with spaces")
    addword.add_argument("--from", dest="start", help="first day, YYYY-MM-DD (default: today)")
    addword.add_argument("--to", dest="end", help="last day, YYYY-MM-DD (default: same as --from)")
    addword.add_argument("--at", help="exact run time HH:MM in the net's timezone (default: spread after the day starts)")
    addword.set_defaults(func=cmd_addword)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        return args.func(load_config(args.config), args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
