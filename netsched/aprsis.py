"""Minimal APRS-IS client: login, send messages, receive messages."""
from __future__ import annotations

import logging
import re
import socket
import time
import unicodedata
from dataclasses import dataclass

from .util import same_station  # noqa: F401  (re-exported for convenience)

log = logging.getLogger(__name__)

SOFTWARE = "netsched"
VERSION = "0.1"
TOCALL = "APZNSC"      # experimental tocall
SILENCE_LIMIT = 120    # seconds without any data (servers send a comment every ~20 s)

MESSAGE_RE = re.compile(r"^(?P<src>[^>]+)>(?P<path>[^:]*)::(?P<dst>.{9}):(?P<body>.*)$")
ACK_RE = re.compile(r"^(?P<kind>ack|rej)(?P<id>[A-Za-z0-9]{1,5})$")
MSGNO_RE = re.compile(r"^[A-Za-z0-9]{1,5}(\}[A-Za-z0-9]{0,5})?$")


class AuthError(Exception):
    """The server rejected the callsign/passcode pair."""


def aprs_passcode(callsign: str) -> int:
    """Standard APRS-IS passcode for a callsign (SSID ignored)."""
    call = callsign.split("-")[0].upper()
    value = 0x73E2
    for i in range(0, len(call), 2):
        value ^= ord(call[i]) << 8
        if i + 1 < len(call):
            value ^= ord(call[i + 1])
    return value & 0x7FFF


def clean_text(text: str) -> str:
    """ASCII-only text without the characters APRS forbids inside messages."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[|~{}\r\n]", " ", text).strip()


@dataclass
class IncomingMessage:
    src: str
    dst: str
    text: str
    msgno: str | None = None

    @property
    def ack(self) -> tuple | None:
        """('ack' | 'rej', message id) when this packet acknowledges one of our messages."""
        match = ACK_RE.match(self.text.strip())
        return (match["kind"], match["id"]) if match else None


def parse_message(line: str) -> IncomingMessage | None:
    """Parse an APRS-IS line; returns None unless it is a message packet."""
    match = MESSAGE_RE.match(line)
    if not match:
        return None
    body = match["body"]
    text, sep, tail = body.rpartition("{")
    if sep and MSGNO_RE.match(tail):
        msgno = tail.split("}")[0]
    else:
        text, msgno = body, None
    return IncomingMessage(match["src"].strip().upper(), match["dst"].strip().upper(), text, msgno)


class AprsIsClient:
    def __init__(self, host: str, port: int, callsign: str, passcode: str, filter_: str):
        self.host, self.port = host, port
        self.callsign = callsign.upper()
        self.passcode = passcode
        self.filter = filter_
        self.sock: socket.socket | None = None
        self._buf = b""
        self._last_rx = time.monotonic()

    # ------------------------------------------------------------ connection

    def connect(self) -> None:
        self.close()
        log.info("connecting to %s:%s as %s", self.host, self.port, self.callsign)
        self.sock = socket.create_connection((self.host, self.port), timeout=15)
        self._buf, self._last_rx = b"", time.monotonic()
        self._send_raw(
            f"user {self.callsign} pass {self.passcode} vers {SOFTWARE} {VERSION} filter {self.filter}"
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for line in self._read_lines(1.0, keep_comments=True):
                if line.startswith("# logresp"):
                    lowered = line.lower()
                    if "unverified" in lowered:
                        self.close()
                        raise AuthError(f"login not verified: {line}")
                    if "verified" in lowered:
                        log.info("logged in: %s", line.lstrip("# "))
                        return
        self.close()
        raise ConnectionError("no login response from server")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    # ----------------------------------------------------------------- I/O

    def _send_raw(self, line: str) -> None:
        if self.sock is None:
            raise ConnectionError("not connected")
        try:
            self.sock.sendall((line + "\r\n").encode("ascii", "replace"))
        except OSError as exc:
            raise ConnectionError(str(exc)) from exc

    def _read_lines(self, timeout: float, keep_comments: bool = False) -> list:
        if self.sock is None:
            raise ConnectionError("not connected")
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            data = None
        except OSError as exc:
            raise ConnectionError(str(exc)) from exc
        if data == b"":
            raise ConnectionError("server closed the connection")
        if data:
            self._buf += data
            self._last_rx = time.monotonic()
        lines = []
        while b"\n" in self._buf:
            raw, _, self._buf = self._buf.partition(b"\n")
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if line and (keep_comments or not line.startswith("#")):
                lines.append(line)
        if time.monotonic() - self._last_rx > SILENCE_LIMIT:
            raise ConnectionError(f"no data for {SILENCE_LIMIT} s")
        return lines

    def poll(self, timeout: float = 1.0) -> list:
        """Wait up to `timeout` seconds and return complete packet lines received."""
        return self._read_lines(timeout)

    def send_message(self, to: str, text: str, msgid: str | None = None) -> None:
        packet = f"{self.callsign}>{TOCALL},TCPIP*::{to.upper():<9}:{clean_text(text)}"
        if msgid:
            packet += "{" + msgid
        log.debug("TX %s", packet)
        self._send_raw(packet)


class DryRunClient:
    """Stand-in client that only logs what would be sent."""

    def __init__(self, callsign: str):
        self.callsign = callsign.upper()

    def connect(self) -> None:
        log.info("[dry-run] not connecting to APRS-IS")

    def close(self) -> None:
        pass

    def poll(self, timeout: float = 1.0) -> list:
        time.sleep(timeout)
        return []

    def send_message(self, to: str, text: str, msgid: str | None = None) -> None:
        log.info("[dry-run] would send to %s: %s (msgid %s)", to, clean_text(text), msgid)
