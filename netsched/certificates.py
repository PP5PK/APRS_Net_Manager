"""Extract certificate links from net replies and download them safely.

Replies arrive over radio from stations anyone can impersonate, so downloads are
restricted: public hosts only, http(s) only, size-capped, and only images or PDFs
are saved. Anything else is reported for manual handling.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import mimetypes
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

MAX_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 30
USER_AGENT = "netsched/0.1 (+https://github.com/PP5PK)"

URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+"
    r"|\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,}/[^\s<>\"']+",
    re.IGNORECASE,
)
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


@dataclass
class DownloadResult:
    status: str  # downloaded | manual | failed
    path: str | None = None
    error: str | None = None


def extract_urls(text: str) -> list:
    """Links found in a message; links without a scheme are assumed to be https."""
    urls = []
    for raw in URL_RE.findall(text):
        url = raw.rstrip(".,;:!?)]}")
        if not url.lower().startswith("http"):
            url = "https://" + url
        if url not in urls:
            urls.append(url)
    return urls


def safe_name(value: str) -> str:
    return re.sub(r"[^\w.\-]", "_", value).strip("._") or "file"


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
                return False
        except ValueError:
            return False
    return True


def _filename(url: str, headers, content_type: str, day: dt.date) -> str:
    name = ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', headers.get("Content-Disposition", ""), re.I)
    if match:
        name = unquote(match.group(1))
    if not name:
        name = unquote(Path(urlparse(url).path).name)
    name = safe_name(name) if name else "certificate"
    ext = EXTENSIONS.get(content_type) or mimetypes.guess_extension(content_type) or ""
    if ext and not name.lower().endswith(ext):
        stem = Path(name).stem if Path(name).suffix else name
        name = stem + ext
    return f"{day.isoformat()}_{name}"


def _unique(path: Path) -> Path:
    candidate, counter = path, 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        counter += 1
    return candidate


def download(url: str, dest_dir: Path, day: dt.date) -> DownloadResult:
    """Download a certificate to `dest_dir`; never raises."""
    current = url
    with requests.Session() as session:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlparse(current)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                return DownloadResult("failed", error="unsupported URL")
            if not _is_public_host(parts.hostname):
                return DownloadResult("failed", error="host is not public (blocked)")
            try:
                resp = session.get(
                    current,
                    stream=True,
                    timeout=TIMEOUT,
                    allow_redirects=False,
                    headers={"User-Agent": USER_AGENT},
                )
            except requests.RequestException as exc:
                return DownloadResult("failed", error=str(exc)[:200])
            with resp:
                if resp.is_redirect and resp.headers.get("Location"):
                    current = urljoin(current, resp.headers["Location"])
                    continue
                if resp.status_code != 200:
                    return DownloadResult("failed", error=f"HTTP {resp.status_code}")
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if not (ctype.startswith("image/") or ctype == "application/pdf"):
                    return DownloadResult("manual", error=f"not a file download ({ctype or 'unknown type'})")
                data = bytearray()
                for chunk in resp.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        return DownloadResult("failed", error="file too large")
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = _unique(dest_dir / _filename(current, resp.headers, ctype, day))
                target.write_bytes(bytes(data))
                return DownloadResult("downloaded", path=str(target))
    return DownloadResult("failed", error="too many redirects")
