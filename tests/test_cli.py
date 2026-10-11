import contextlib
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path

from netsched.main import load_config, main
from netsched.schedule import ConfigError, load_nets

NETS = """\
nets:
  - {callsign: LU8HEG-5, mode: by_date, template: "CQ {word}", sporadic: true}
  - {callsign: ANSRVR, mode: fixed, message: CQ}
"""


class AddWord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "nets.yaml").write_text(NETS, encoding="utf-8")
        (self.root / "config.yaml").write_text("station: {callsign: PP5PK-7}\n", encoding="utf-8")
        self.csv = self.root / "words.csv"

    def run_cli(self, *argv) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.root / "config.yaml"), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_range_with_an_exact_time(self):
        code, out, _ = self.run_cli("addword", "LU8HEG-5", "MESSI", "--from", "2026-11-01", "--to", "2026-11-02",
                                    "--at", "14:30")
        self.assertEqual(code, 0)
        self.assertIn("14:30 UTC", out)
        self.assertEqual(self.csv.read_text(encoding="utf-8").splitlines(), [
            "callsign,date,word,time",
            "LU8HEG-5,2026-11-01,MESSI,14:30",
            "LU8HEG-5,2026-11-02,MESSI,14:30",
        ])
        net = load_nets(self.root / "nets.yaml", self.csv)[0]
        self.assertEqual(net.word_times[dt.date(2026, 11, 2)], dt.time(14, 30))
        self.assertEqual(net.render(dt.date(2026, 11, 1)), "CQ MESSI")

    def test_without_a_time_the_run_is_spread(self):
        self.assertEqual(self.run_cli("addword", "LU8HEG-5", "TWO", "WORDS", "--from", "2026-11-01")[0], 0)
        net = load_nets(self.root / "nets.yaml", self.csv)[0]
        self.assertEqual(net.words[dt.date(2026, 11, 1)], "TWO WORDS")
        self.assertEqual(net.word_times, {})

    def test_an_older_three_column_file_is_upgraded_and_kept(self):
        self.csv.write_text("# my words\ncallsign,date,word\nLU8HEG-5,2026-10-01,OLD\n", encoding="utf-8")
        self.assertEqual(self.run_cli("addword", "LU8HEG-5", "NEW", "--from", "2026-10-02", "--at", "08:00")[0], 0)
        lines = self.csv.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:3], ["# my words", "callsign,date,word,time", "LU8HEG-5,2026-10-01,OLD"])
        net = load_nets(self.root / "nets.yaml", self.csv)[0]
        self.assertEqual((net.words[dt.date(2026, 10, 1)], net.words[dt.date(2026, 10, 2)]), ("OLD", "NEW"))
        self.assertEqual(net.word_times, {dt.date(2026, 10, 2): dt.time(8, 0)})

    def test_bad_input_is_refused_and_nothing_is_written(self):
        for argv in (("addword", "ANSRVR", "X"),                               # not a by_date net
                     ("addword", "NOBODY", "X"),                               # unknown net
                     ("addword", "LU8HEG-5", "X", "--at", "25:99"),            # bad time
                     ("addword", "LU8HEG-5", "X", "--from", "2026-11-05", "--to", "2026-11-01")):
            self.assertEqual(self.run_cli(*argv)[0], 2, argv)
        self.assertFalse(self.csv.exists())

    def test_an_unknown_timezone_in_the_config_is_rejected(self):
        (self.root / "bad.yaml").write_text("station: {callsign: PP5PK-7}\nnet_timezone: Nowhere/Land\n",
                                            encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(str(self.root / "bad.yaml"))


if __name__ == "__main__":
    unittest.main()
