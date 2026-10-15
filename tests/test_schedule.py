import datetime as dt
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from netsched.schedule import ConfigError, Net, Schedule, evaluate, load_nets, merge_words_csv, send_time

SP = ZoneInfo("America/Sao_Paulo")
UTC_ZONE = ZoneInfo("UTC")
UTC = dt.timezone.utc
SIG = "Greetings from Daniel K. / Mafra, SC - Brazil"


def at(year, month, day, hour=12, minute=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=SP)


def net(**data):
    data.setdefault("callsign", "TEST")
    data.setdefault("mode", "fixed")
    data.setdefault("message", "CQ")
    return Net.from_dict(data)


class ScheduleRules(unittest.TestCase):
    def test_weekly_tuesday(self):
        s = Schedule.from_dict({"type": "weekly", "weekdays": ["tue"]})
        self.assertTrue(s.is_active_on(dt.date(2026, 10, 13)))   # Tuesday
        self.assertFalse(s.is_active_on(dt.date(2026, 10, 14)))

    def test_second_sunday_of_month(self):
        s = Schedule.from_dict({"type": "nth_weekday", "nth": 2, "weekdays": ["sun"]})
        self.assertFalse(s.is_active_on(dt.date(2026, 10, 4)))
        self.assertTrue(s.is_active_on(dt.date(2026, 10, 11)))
        self.assertFalse(s.is_active_on(dt.date(2026, 10, 18)))

    def test_last_friday_of_month(self):
        s = Schedule.from_dict({"type": "nth_weekday", "nth": -1, "weekdays": ["fri"]})
        self.assertTrue(s.is_active_on(dt.date(2026, 10, 30)))
        self.assertFalse(s.is_active_on(dt.date(2026, 10, 23)))

    def test_date_range_weekends_only(self):
        s = Schedule.from_dict({"type": "date_range", "start": "2026-10-03", "end": "2026-12-27",
                                "weekdays": ["sat", "sun"]})
        self.assertTrue(s.is_active_on(dt.date(2026, 10, 3)))    # Saturday
        self.assertFalse(s.is_active_on(dt.date(2026, 10, 5)))   # Monday
        self.assertTrue(s.is_active_on(dt.date(2026, 12, 27)))   # Sunday, last day
        self.assertFalse(s.is_active_on(dt.date(2026, 12, 28)))
        self.assertFalse(s.is_active_on(dt.date(2026, 10, 2)))

    def test_unquoted_yaml_time_is_understood(self):
        # PyYAML parses an unquoted 13:00 as the integer 780
        self.assertEqual(Schedule.from_dict({"after": 780}).after, dt.time(13, 0))

    def test_validation(self):
        with self.assertRaises(ConfigError):
            Schedule.from_dict({"type": "weekly"})
        with self.assertRaises(ConfigError):
            Schedule.from_dict({"type": "nth_weekday", "weekdays": ["sun"]})
        with self.assertRaises(ConfigError):
            Schedule.from_dict({"type": "date_range"})
        with self.assertRaises(ConfigError):
            Schedule.from_dict({"type": "nope"})


class Evaluation(unittest.TestCase):
    def test_after_time_waits_then_due(self):
        n = net(schedule={"type": "daily", "after": "13:00"})
        self.assertEqual(evaluate(n, at(2026, 10, 10, 12, 59), default_tz="America/Sao_Paulo").state, "waiting")
        self.assertEqual(evaluate(n, at(2026, 10, 10, 13, 0), default_tz="America/Sao_Paulo").state, "due")

    def test_local_date_uses_timezone(self):
        n = net(schedule={"type": "weekly", "weekdays": ["sat"]})
        # 01:30 UTC on Sunday is still Saturday night in Sao Paulo
        now = dt.datetime(2026, 10, 11, 1, 30, tzinfo=dt.timezone.utc)
        self.assertEqual(evaluate(n, now, default_tz="America/Sao_Paulo").state, "due")

    def test_by_date_missing_word(self):
        n = net(mode="by_date", message="", words={"2026-10-10": "CORREO"})
        self.assertEqual(evaluate(n, at(2026, 10, 10)).text, "CORREO")
        self.assertEqual(evaluate(n, at(2026, 10, 11)).state, "missing_word")

    def test_by_weekday_with_template(self):
        n = net(mode="by_weekday", message="", template="CQ {word}", words_by_weekday={"sat": "MOON"})
        self.assertEqual(evaluate(n, at(2026, 10, 10)).text, "CQ MOON")

    def test_signature_placeholder_and_length(self):
        n = net(message="CQ HOTG {sig}")
        self.assertEqual(evaluate(n, at(2026, 10, 10), SIG).text, "CQ HOTG " + SIG)
        long_net = net(message="X" * 40 + " {sig}")
        self.assertEqual(evaluate(long_net, at(2026, 10, 10), SIG).state, "too_long")

    def test_done_and_disabled(self):
        n = net()
        self.assertEqual(evaluate(n, at(2026, 10, 10), is_done=lambda c, d, t: True).state, "done")
        self.assertEqual(evaluate(net(enabled=False), at(2026, 10, 10)).state, "disabled")

    def test_sporadic_net_is_silent_without_a_word(self):
        quiet = net(mode="by_date", message="", sporadic=True, words={"2026-10-10": "TIKAL"})
        self.assertEqual(evaluate(quiet, at(2026, 10, 10)).text, "TIKAL")
        self.assertEqual(evaluate(quiet, at(2026, 10, 11)).state, "no_event")
        loud = net(mode="by_date", message="", word_source="WhatsApp")
        decision = evaluate(loud, at(2026, 10, 11))
        self.assertEqual(decision.state, "missing_word")
        self.assertIn("WhatsApp", decision.detail)

    def test_week_placeholder_is_the_iso_week(self):
        self.assertEqual(evaluate(net(message="CQ {week}"), at(2026, 10, 8)).text, "CQ 41")


class SendTimes(unittest.TestCase):
    DAY = dt.date(2026, 10, 11)

    def test_at_means_an_exact_time_without_spread(self):
        s = Schedule.from_dict({"at": "14:30"})
        self.assertEqual((s.after, s.spread), (dt.time(14, 30), 0))
        with self.assertRaises(ConfigError):
            Schedule.from_dict({"at": "14:30", "after": "10:00"})

    def test_window_validation(self):
        with self.assertRaises(ConfigError):
            Schedule.from_dict({"after": "12:00", "before": "11:00"})
        with self.assertRaises(ConfigError):
            Schedule.from_dict({"spread": -5})

    def test_spread_is_stable_and_inside_the_window(self):
        n = net(callsign="APRSMY")
        first = send_time(n, self.DAY, UTC_ZONE, 60)
        self.assertEqual(first, send_time(n, self.DAY, UTC_ZONE, 60))
        opens = dt.datetime(2026, 10, 11, tzinfo=UTC)
        self.assertTrue(opens <= first <= opens + dt.timedelta(minutes=60))
        moments = {send_time(net(callsign=c), self.DAY, UTC_ZONE, 60) for c in ("A1", "B2", "C3", "D4", "E5", "F6")}
        self.assertGreater(len(moments), 1)  # the nets are not all contacted at the same second

    def test_due_exactly_at_the_send_time(self):
        n = net(schedule={"type": "daily", "after": "10:00", "spread": 30})
        when = send_time(n, self.DAY, UTC_ZONE, 0)
        self.assertEqual(evaluate(n, when - dt.timedelta(seconds=1), default_tz="UTC").state, "waiting")
        self.assertEqual(evaluate(n, when, default_tz="UTC").state, "due")

    def test_before_caps_the_spread_and_closes_the_window(self):
        n = net(schedule={"type": "daily", "after": "10:00", "before": "10:05", "spread": 600})
        self.assertLessEqual(send_time(n, self.DAY, UTC_ZONE, 0), dt.datetime(2026, 10, 11, 10, 5, tzinfo=UTC))
        late = dt.datetime(2026, 10, 11, 10, 6, tzinfo=UTC)
        self.assertEqual(evaluate(n, late, default_tz="UTC").state, "closed")

    def test_event_time_is_exact_and_overrides_the_window(self):
        n = net(mode="by_date", message="", schedule={"type": "daily", "spread": 600},
                words={"2026-10-11": {"word": "TIKAL", "at": "14:30"}})
        self.assertEqual(send_time(n, self.DAY, UTC_ZONE, 60), dt.datetime(2026, 10, 11, 14, 30, tzinfo=UTC))
        self.assertEqual(evaluate(n, dt.datetime(2026, 10, 11, 14, 29, tzinfo=UTC), default_tz="UTC").state, "waiting")
        self.assertEqual(evaluate(n, dt.datetime(2026, 10, 11, 14, 30, tzinfo=UTC), default_tz="UTC").state, "due")

    def test_the_net_day_follows_its_own_timezone(self):
        # Saturday 23:30 in Sao Paulo is already Sunday 02:30 UTC
        now = dt.datetime(2026, 10, 10, 23, 30, tzinfo=SP)
        rule = {"type": "weekly", "weekdays": ["sun"], "at": "00:00"}
        self.assertEqual(evaluate(net(schedule=rule), now, default_tz="UTC").state, "due")
        self.assertEqual(evaluate(net(schedule=rule, tz="America/Sao_Paulo"), now, default_tz="UTC").state, "inactive")

    def test_unknown_timezone_is_rejected(self):
        with self.assertRaises(ConfigError):
            net(tz="Mars/Olympus")


class Loading(unittest.TestCase):
    def test_repository_nets_file_loads(self):
        nets = load_nets(Path(__file__).resolve().parent.parent / "nets.yaml")
        by_call = {n.callsign: n for n in nets}
        self.assertEqual(by_call["9W4GTS"].schedule.weekdays, (4,))
        self.assertEqual(by_call["CX2JO-10"].schedule.after, dt.time(14, 30))  # UTC
        self.assertEqual(by_call["CX2JO-10"].mode, "by_date")
        self.assertTrue(all(net.tz is None for net in nets))  # every net follows UTC
        self.assertEqual(by_call["TICANET"].certificate, "code")
        self.assertEqual(by_call["PKTNET"].certificate, "email")
        self.assertEqual(by_call["ANSRVR"].certificate, "none")
        self.assertTrue(by_call["HR5HAC-10"].sporadic)

    def test_entries_sharing_a_callsign_never_fall_on_the_same_day(self):
        # ANSRVR hosts several nets (Thursday, weekends, 2nd Monday): one message per day at most
        groups = {}
        for n in load_nets(Path(__file__).resolve().parent.parent / "nets.yaml"):
            groups.setdefault(n.callsign, []).append(n)
        shared = {call: entries for call, entries in groups.items() if len(entries) > 1}
        self.assertIn("ANSRVR", shared)
        day = dt.date(2026, 1, 1)
        while day <= dt.date(2027, 12, 31):
            for call, entries in shared.items():
                active = [n for n in entries if n.schedule.is_active_on(day) and n.render(day, SIG) is not None]
                self.assertLessEqual(len(active), 1, f"{call} has two messages on {day}")
            day += dt.timedelta(days=1)

    def test_aprs_thursday_message_matches_the_real_one(self):
        nets = load_nets(Path(__file__).resolve().parent.parent / "nets.yaml")
        ansrvr = next(n for n in nets if n.callsign == "ANSRVR" and "HOTG" in n.message)
        decision = evaluate(ansrvr, at(2026, 10, 8), SIG, "America/Sao_Paulo")  # Thursday of week 41
        self.assertEqual(decision.state, "due")
        self.assertEqual(decision.text, "CQ HOTG Greetings from Daniel K. / Mafra, SC - Brazil (APRSTh#41)")
        self.assertEqual(evaluate(ansrvr, at(2026, 10, 9), SIG, "America/Sao_Paulo").state, "inactive")

    def test_malformed_entries_are_config_errors_not_crashes(self):
        for bad in ("schedule: 5", "words: [1, 2]", "schedule: {type: weekly, weekdays: 7}", "tz: 42"):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "nets.yaml"
                path.write_text(f"nets:\n  - {{callsign: X, message: CQ, {bad}}}\n", encoding="utf-8")
                with self.assertRaises(ConfigError, msg=bad):
                    load_nets(path)

    def test_words_csv_time_column(self):
        nets = [net(callsign="X", mode="by_date", message="")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.csv"
            path.write_text("callsign,date,word,time\nX,2026-10-02,A,14:30\nX,2026-10-03,B,\n", encoding="utf-8")
            merge_words_csv(nets, path)
        self.assertEqual(nets[0].word_times, {dt.date(2026, 10, 2): dt.time(14, 30)})
        self.assertEqual(nets[0].words[dt.date(2026, 10, 3)], "B")

    def test_words_csv_merge(self):
        nets = [net(callsign="CE4JWI-10", mode="by_date", message="")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.csv"
            path.write_text("callsign,date,word\nce4jwi-10,2026-10-02,CORREO\n", encoding="utf-8")
            merge_words_csv(nets, path)
            path.write_text("callsign,date,word\nNOPE,2026-10-02,X\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                merge_words_csv(nets, path)
        self.assertEqual(nets[0].words[dt.date(2026, 10, 2)], "CORREO")


if __name__ == "__main__":
    unittest.main()
