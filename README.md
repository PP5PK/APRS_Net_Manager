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

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml     # set station.callsign (with SSID)
python -m netsched today               # what is due right now (sends nothing)
python -m netsched today --at 2026-10-11T14:00 --all
python -m netsched run --dry-run       # full loop, but never connects
python -m netsched run                 # live
python -m netsched report              # certificates that need manual download

# register the word of an occasional net (a single day, or a range)
python -m netsched addword LU8HEG-5 MESSI --from 2026-11-01 --to 2026-11-03
```

Run the tests with `python -m unittest discover -s tests -t .`.

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

## Running as a service

`deploy/netsched.service` is a systemd unit. The APRS-IS passcode is computed
from the callsign; set `APRS_PASSCODE` in the environment to override it.

## Safety notes

Replies come over radio from stations anyone can impersonate, so the downloader
only talks to public hosts (private/loopback addresses are blocked, including
across redirects), accepts http(s) only, caps files at 15 MB, and saves only
images and PDFs. Replies from stations that are not registered nets are ignored.

## Not done yet

- Nets that require a code typed into a website (`certificate: code`, e.g.
  TICANET) are only reported, not automated.
- No notification channel yet (Telegram/e-mail) for items listed by `report`.
- Each net's "day" follows the `timezone` in config.yaml (or a per-net `tz:`),
  which may differ from the net's own time zone.
