# APRS_Net_Manager

Nets APRS participation manager, with scheduling of future events and automated collection and storage of participation certificates.

Automates check-ins on APRS nets: sends the right message to each net's callsign
at the right time through APRS-IS, waits for the reply, and downloads the
participation certificate when the reply contains a direct link.

## How it works

1. `nets.yaml` registers each net: callsign, schedule, message (or per-day words).
2. The daemon checks every 30 s what is due *today* (each net's day, UTC unless the
   net says otherwise) and sends each message once.
3. A message the net does not ACK is repeated (3 attempts, 60 s apart).
4. Replies from registered nets are stored. Links to images/PDFs are downloaded to
   `certs/<CALLSIGN>/`; replies that need a manual step (a code typed on a website)
   are listed by `report`.

State lives in plain files next to `config.yaml`: `data/netsched.db` (what was sent
and received; it is also what stops the same message going out twice, so do not
delete it) and `certs/`.

## Requirements

| What | Details |
| --- | --- |
| System | Linux with Python 3.9 or newer: Debian 11+, Ubuntu 22.04+, Raspberry Pi OS Bullseye+ |
| Packages | `python3`, `python3-yaml`, `python3-requests`, `tzdata`, `git` (all from `apt`) |
| Network | outbound TCP to the APRS-IS server (`rotate.aprs2.net:14580`) and HTTPS to the sites that host the certificates. No inbound ports |
| Clock | correct system time: schedules are based on UTC. `timedatectl` must show `System clock synchronized: yes` |
| Callsign | your callsign with an **SSID nothing else uses**. If APRSdroid or another client logs in with the same callsign-SSID, messages and ACKs get mixed up. The APRS-IS passcode is calculated for you |
| Permissions | the user that runs the program must be able to write in the project folder (`data/` and `certs/` are created there) |

## Installation

No virtual environment is needed: the two libraries come from `apt`.

```bash
sudo apt update
sudo apt install -y python3 python3-yaml python3-requests tzdata git
python3 --version                      # must say 3.9 or newer

git clone https://github.com/PP5PK/APRS_Net_Manager.git
cd APRS_Net_Manager
cp config.example.yaml config.yaml
nano config.yaml                       # set station.callsign (with SSID) and station.signature
```

Then check it step by step, from the project folder:

```bash
python3 -m netsched today              # what is due now and when it will be sent; sends nothing
python3 -m netsched run --dry-run      # runs the whole loop but only logs what it would send (Ctrl+C stops)
python3 -m netsched run                # live: connects to APRS-IS and sends what is due
```

The first live run sends every message whose time has already passed today (5 s
apart), exactly as it would after any downtime. Messages already sent are never
repeated.

On other systems install the libraries with `pip install -r requirements.txt`
(inside a virtual environment if your Python is "externally managed").

## Configuration

`config.yaml` (copy of `config.example.yaml`; it is git-ignored). Relative paths
are resolved from the folder that contains the file, and `--config` goes before the
command: `python3 -m netsched --config /path/config.yaml today`.

| Key | Meaning |
| --- | --- |
| `station.callsign` | **required.** Callsign with SSID used to send and receive |
| `station.signature` | text that replaces `{sig}` in messages |
| `station.passcode` | normally empty (calculated). `APRS_PASSCODE` in the environment overrides it |
| `timezone` | your own zone, used only to display times and to read `today --at` |
| `net_timezone` | zone that defines a net's day (default `UTC`) |
| `aprs_is.host` / `port` | APRS-IS server (default `rotate.aprs2.net:14580`; `soam.aprs2.net` is closer to South America) |
| `paths.*` | `nets`, `words_csv`, `database`, `certificates` |
| `send.tick_seconds` | how often the schedule is checked (30) |
| `send.gap_seconds` | pause between two outgoing messages (5) |
| `send.retries` / `retry_interval` | attempts per message while waiting for an ACK (3) and seconds between them (60) |
| `send.default_start` | time of day (net time, UTC) at which the send window opens, for nets that do not set their own `after` or `at` (`"00:00"`; use `"03:00"` to start at 03:00Z). Keep the quotes |
| `send.default_spread_minutes` | spread of the send time after the window opens (60) |

Messages are limited to 67 characters; a longer one is reported as `too_long` and
not sent.

## Everyday commands

```bash
python3 -m netsched today [--at 2026-10-11T14:00] [--all]   # --all also lists inactive nets
python3 -m netsched report                                   # certificates that need a manual step
python3 -m netsched addword LU8HEG-5 MESSI --from 2026-11-01 --to 2026-11-03 --at 14:30
```

## Registering nets

See the comment header in `nets.yaml` for every option. Examples:

```yaml
- callsign: 9W4GTS                      # every Friday
  schedule: {type: weekly, weekdays: [fri]}
  mode: fixed
  message: CQ KV50S {sig}               # {sig} = station.signature

- callsign: TI0RHU-15                   # weekends inside a date range
  schedule: {type: date_range, start: 2026-10-03, end: 2026-12-27, weekdays: [sat, sun]}
  mode: fixed
  message: QSL

- callsign: CX2JO-10                    # a different word each day, only after 13:00
  schedule: {type: daily, after: "13:00"}
  mode: by_date
  words: {2026-10-05: DIVISION, 2026-10-06: VIRGIN}

- callsign: SOMENET                     # second Sunday of every month
  schedule: {type: nth_weekday, nth: 2, weekdays: [sun]}
  mode: fixed
  message: CHECK
```

Placeholders in messages: `{sig}` (your signature), `{word}` (the day's word)
and `{week}` (ISO week of the year, used by APRSThursday: `(APRSTh#{week})`).

Words for `by_date` nets live in `words.csv` (`callsign,date,word`, see
`words.example.csv`). The `addword` command appends to that file for you. Both
`nets.yaml` and `words.csv` are reloaded automatically when they change, so the
daemon never needs a restart to pick up new nets or words.

**Occasional nets** are registered with `sporadic: true`: nothing is sent until you
register a word for the day. **Daily nets whose word is published elsewhere**
(CE4JWI-10, CX2JO-10, XE1JMB-10) log a warning when today's word is missing, and
name `word_source` so you know where to look.

**Certificate modes:** `link` downloads the file automatically; `code` keeps the
reply (it may carry the code) and lists it in `report` together with the net's
website; `email` just records the reply; `none` ignores it.

Always quote times (`"13:00"`): YAML reads an unquoted `13:00` as a number.

## Days, times and congestion

**Which day is it?** Most nets run on UTC, so a net's day is the calendar date in
`net_timezone` (default `UTC`). A net that follows its own local time sets
`tz: Region/City` in `nets.yaml`. Your own `timezone` is only used to *display*
times and to read `today --at`.

**When is the message sent?** Not at the first second of the day. For every net
and day the scheduler picks a stable pseudo-random moment inside a window
(the same after a restart, different between nets), so a hundred stations do not
hit the same net at 00:00:

| In `schedule:` | Effect |
| --- | --- |
| *(nothing)* | window opens at `send.default_start` (00:00) and the send is spread over `send.default_spread_minutes` (60) |
| `after: "13:00"` | window opens at 13:00 (net time) instead of the default start, then the same spread |
| `at: "14:30"` | exactly 14:30, no spread |
| `before: "18:00"` | the send is never later than this; after it the net shows as `closed` |
| `spread: 15` | this net only: spread over 15 minutes (0 = on the dot) |

**Events:** an event is a word plus the days it applies to, and optionally the
exact time. Registering it also fixes the run time for those days:

```bash
python3 -m netsched addword LU8HEG-5 MESSI --from 2026-11-01 --to 2026-11-03 --at 14:30
```

In `nets.yaml` the same thing is `words: {2026-11-01: {word: MESSI, at: "14:30"}}`,
and in `words.csv` it is the optional `time` column. The time is in the net's
timezone. `python3 -m netsched today` shows, for every net, when it will be sent
(UTC and your own time).

## Running as a service (systemd)

After the `apt install` line above:

```bash
sudo useradd --system --home-dir /opt/APRS_Net_Manager netsched
sudo git clone https://github.com/PP5PK/APRS_Net_Manager.git /opt/APRS_Net_Manager
cd /opt/APRS_Net_Manager
sudo cp config.example.yaml config.yaml
sudo nano config.yaml                   # station.callsign, signature
sudo chown -R netsched: /opt/APRS_Net_Manager

sudo -u netsched python3 -m netsched today          # check before enabling

sudo cp deploy/netsched.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now netsched
systemctl status netsched
journalctl -u netsched -f               # follow the log
```

Run every command that touches the project as the `netsched` user (`sudo -u netsched
...`). If you ever ran one as root, fix the owner with
`sudo chown -R netsched: /opt/APRS_Net_Manager`.

A healthy log looks like this:

```
started as PP5PK-7
logged in: logresp PP5PK-7 verified, server T2...
sent to APRSMY: CHECK (msgid 2)
ACK 2 from APRSMY
reply from TI0RHU-15: ... https://.../certificate.png
TI0RHU-15: downloaded https://.../certificate.png /opt/APRS_Net_Manager/certs/TI0RHU-15/2026-10-14_certificate.png
```

## Operation

**Update:** `sudo -u netsched git -C /opt/APRS_Net_Manager pull`, then
`sudo systemctl restart netsched`. Edits to `nets.yaml` and `words.csv` are picked up
without a restart; an invalid edit is logged and the previous version keeps running.

**Back up:** `config.yaml`, `words.csv`, `data/` and `certs/` are not in git.

**What was sent?** (needs `sudo apt install sqlite3`)

```bash
sqlite3 -header -column data/netsched.db \
  "SELECT callsign, local_date, text, status, attempts FROM sends ORDER BY id DESC LIMIT 20;"
```

Status: `sent` waiting for the ACK, `acked` confirmed, `rejected` the net refused it,
`no_ack` never confirmed after all attempts, `unconfirmed` the net is set to `ack: false`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'yaml'` (or `requests`) | `sudo apt install python3-yaml python3-requests` |
| `unknown timezone 'UTC'` | `sudo apt install tzdata` |
| `error: cannot read config .../config.yaml` | run from the project folder or pass `--config /path/config.yaml`; create it with `cp config.example.yaml config.yaml` |
| `error: station.callsign is required` | fill in `station.callsign` in `config.yaml` |
| `error: cannot read the nets file` | `paths.nets` points to a file that does not exist; paths are relative to `config.yaml` |
| `login not verified` | wrong callsign or passcode. Leave `station.passcode` empty and unset `APRS_PASSCODE`. The service stops on purpose; fix and restart it |
| `connection problem ... retrying` | no route to APRS-IS: test `nc -zv rotate.aprs2.net 14580`, check the firewall and DNS. It reconnects by itself |
| `Permission denied` on `data/` or `certs/` | the service user cannot write: `sudo chown -R netsched: /opt/APRS_Net_Manager` |
| nothing is sent, `today` shows `waiting` | the send time has not come yet (it is spread after the day starts, see *Days, times and congestion*) |
| `skipped: no word for ...` | a daily net has no word for today: add it with `addword` or in `words.csv` |
| `never acknowledged` | the net did not ACK. Some nets never do: set `ack: false` on that net |
| certificate `failed` / `manual` in `report` | the link needs a code, a login or a browser: open the URL listed by `report` |
| messages arrive twice or ACKs are missing | another client uses the same callsign-SSID: give the program its own SSID |
| `words.csv: must be saved as UTF-8` | re-save the file as UTF-8 |

## Safety notes

Replies come over radio from stations anyone can impersonate, so the downloader
only talks to public hosts (private/loopback addresses are blocked, including
across redirects), accepts http(s) only, caps files at 15 MB, and saves only
images and PDFs. Replies from stations that are not registered nets are ignored.

## Not done yet

- Nets that require a code typed into a website (`certificate: code`, e.g.
  TICANET) are only reported, not automated.
- No notification channel yet (Telegram/e-mail) for items listed by `report`.
