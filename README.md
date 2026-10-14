# APRS_Net_Manager

Nets APRS participation manager, with scheduling of future events and automated collection and storage of participation certificates.

Automates check-ins on APRS nets: sends the right message to each net's callsign
at the right time through APRS-IS, waits for the reply, and downloads the
participation certificate when the reply contains a direct link.

## How it works

1. `nets.yaml` registers each net: callsign, schedule, message (or per-day words).
2. The daemon checks every 30 s what is due *today* (in your timezone), and sends it once.
3. Messages are retried until the net ACKs them (`retries` / `retry_interval`).
4. Replies from registered nets are stored; links to images/PDFs are downloaded to
   `certs/<CALLSIGN>/`. Links that need a code on a website are listed by `report`.

## Quick start

Needs Python 3.9+ and two libraries. On Debian, Ubuntu and Raspberry Pi OS install
them with `apt`, so **no virtual environment is needed**:

```bash
sudo apt install -y python3 python3-yaml python3-requests tzdata git
git clone https://github.com/PP5PK/APRS_Net_Manager.git
cd APRS_Net_Manager

cp config.example.yaml config.yaml     # set station.callsign (with SSID)
python3 -m netsched today              # what is due right now (sends nothing)
python3 -m netsched today --at 2026-10-11T14:00 --all
python3 -m netsched run --dry-run      # full loop, but never connects
python3 -m netsched run                # live
python3 -m netsched report             # certificates that need manual download

# register the word of an occasional net (a day or a range, optionally at an exact time)
python3 -m netsched addword LU8HEG-5 MESSI --from 2026-11-01 --to 2026-11-03 --at 14:30
```

On other systems install the libraries with `pip install -r requirements.txt`
(inside a virtual environment if your Python is "externally managed").

Run the tests with `python3 -m unittest discover -s tests -t .`.

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
| *(nothing)* | window opens at 00:00 and the send is spread over `send.default_spread_minutes` (60) |
| `after: "13:00"` | window opens at 13:00 (net time), then the same spread |
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
journalctl -u netsched -f               # follow the log
```

To update later: `sudo -u netsched git -C /opt/APRS_Net_Manager pull` and
`sudo systemctl restart netsched`. Edits to `nets.yaml` and `words.csv` are picked
up without a restart.

The APRS-IS passcode is computed from the callsign; set `APRS_PASSCODE` in the
environment to override it. If APRS-IS rejects the login the service stops
instead of retrying forever; check the log.

## Safety notes

Replies come over radio from stations anyone can impersonate, so the downloader
only talks to public hosts (private/loopback addresses are blocked, including
across redirects), accepts http(s) only, caps files at 15 MB, and saves only
images and PDFs. Replies from stations that are not registered nets are ignored.

## Not done yet

- Nets that require a code typed into a website (`certificate: code`, e.g.
  TICANET) are only reported, not automated.
- No notification channel yet (Telegram/e-mail) for items listed by `report`.
