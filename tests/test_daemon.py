import tempfile
import unittest
from pathlib import Path

from netsched.main import Daemon, load_config

NETS = """\
nets:
  - {callsign: TICANET, mode: fixed, message: CQ TICANET, certificate: code, info_url: tinyurl.com/TICANET}
  - {callsign: PKTNET, mode: fixed, message: CHECK, certificate: email}
  - {callsign: ANSRVR, mode: fixed, message: CQ HOTG, certificate: none}
"""


class ReplyHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # keep the reference: GC would delete the folder
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "nets.yaml").write_text(NETS, encoding="utf-8")
        (root / "config.yaml").write_text("station: {callsign: PP5PK-7}\n", encoding="utf-8")
        self.daemon = Daemon(load_config(str(root / "config.yaml")), dry_run=True)
        self.addCleanup(self.daemon.db.close)
        self.daemon._connect()

    def reply(self, src: str, text: str) -> None:
        self.daemon.handle_line(f"{src}>APRS,TCPIP*::PP5PK-7  :{text}")

    def certificates(self) -> list:
        return [tuple(r) for r in self.daemon.db.conn.execute("SELECT url, status, error FROM certificates")]

    def test_code_net_keeps_the_reply_for_the_manual_step(self):
        self.reply("TICANET", "Your code is 4821{9")
        rows = self.certificates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], ("https://tinyurl.com/TICANET", "manual"))
        self.assertIn("4821", rows[0][2])
        self.assertEqual(len(self.daemon.db.open_certificates()), 1)

    def test_email_net_is_recorded_but_not_listed_as_pending(self):
        self.reply("PKTNET", "Send your e-mail to receive the certificate")
        self.assertEqual(self.certificates()[0][1], "email")
        self.assertEqual(self.daemon.db.open_certificates(), [])

    def test_net_without_certificate_stores_only_the_reply(self):
        self.reply("ANSRVR", "Welcome to APRSThursday")
        self.assertEqual(self.certificates(), [])
        self.assertEqual(self.daemon.db.conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 1)

    def test_unknown_station_and_duplicates_are_ignored(self):
        self.reply("N0BODY", "http://evil.example/cert.pdf")
        self.assertEqual(self.daemon.db.conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0)
        self.reply("TICANET", "Your code is 4821")
        self.reply("TICANET", "Your code is 4821")
        self.assertEqual(len(self.certificates()), 1)


if __name__ == "__main__":
    unittest.main()
