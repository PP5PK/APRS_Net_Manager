import datetime as dt
import tempfile
import unittest
from pathlib import Path

from netsched.main import Daemon, load_config
from netsched.schedule import ConfigError, Net
from netsched.util import utcnow

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


PKTNET = """\
nets:
  - callsign: PKTNET
    mode: fixed
    message: CHECK
    certificate: email
    dialog_delay: 0
    dialog:
      - {when: '\\[2/2\\].*Ant:\\s*{name}\\s*/\\s*{email}', send: SIM}
      - {when: '\\[2/2\\]', send: NAO}
      - {when: 'Deseja o certificado', send: "{email}"}
      - {when: 'Usar "{name}"', send: SIM}
      - {when: 'Usar "', send: "{name}"}
"""


class Recorder:
    def __init__(self):
        self.sent = []

    def send_message(self, to, text, msgid=None):
        self.sent.append((to, text, msgid))


class Dialog(unittest.TestCase):
    NAME, EMAIL = "Daniel Kondlatsch", "svrutz@gmail.com"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "nets.yaml").write_text(PKTNET, encoding="utf-8")
        (root / "config.yaml").write_text(
            f"station: {{callsign: PP5PK-5, name: {self.NAME}, email: {self.EMAIL}}}\n", encoding="utf-8")
        self.daemon = Daemon(load_config(str(root / "config.yaml")), dry_run=True)
        self.addCleanup(self.daemon.db.close)
        self.client = self.daemon.client = Recorder()
        now = utcnow()
        self.send_id = self.daemon.db.create_send("PKTNET", now.date(), "CHECK", now)

    def bot(self, text: str) -> None:
        self.daemon.handle_line(f"PKTNET>APRS,TCPIP*::PP5PK-5  :{text}")

    def answers(self) -> list:
        return [text for to, text, msgid in self.client.sent if not text.startswith("ack")]

    def test_first_participation_sends_the_email_then_confirms_the_name(self):
        self.bot("Check-in OK 0515z. 73 de PP5PK")
        self.assertEqual(self.answers(), [])
        self.bot("Deseja o certificado? Responda com seu email ou apenas NAO")
        self.assertEqual(self.answers(), [self.EMAIL])
        self.bot('Usar "Daniel Kondlatsch"? SIM, ou envie outro nome')
        self.assertEqual(self.answers(), [self.EMAIL, "SIM"])

    def test_a_different_name_proposed_by_the_bot_is_corrected(self):
        self.bot('Usar "D. Kondlatsch"? SIM, ou envie outro nome')
        self.assertEqual(self.answers(), [self.NAME])

    def test_returning_participant_with_the_same_data_answers_yes(self):
        self.bot("Check-in OK 0515z. 73 de PP5PK")
        self.bot("[1/2]: Usar dados anteriores? SIM / NAO")
        self.assertEqual(self.answers(), [])  # waits for the saved data
        self.bot(f"[2/2]: Ant: {self.NAME} / {self.EMAIL}")
        self.assertEqual(self.answers(), ["SIM"])

    def test_returning_participant_with_other_data_starts_over(self):
        self.bot("[1/2]: Usar dados anteriores? SIM / NAO")
        self.bot("[2/2]: Ant: Old Name / old@example.com")
        self.assertEqual(self.answers(), ["NAO"])
        self.bot("Deseja o certificado? Responda com seu email ou apenas NAO")
        self.assertEqual(self.answers(), ["NAO", self.EMAIL])

    def test_each_rule_answers_once_and_the_step_is_acked(self):
        self.bot("Deseja o certificado? Responda com seu email ou apenas NAO")
        self.bot("Deseja o certificado? Responda com seu email ou apenas NAO!")
        self.assertEqual(self.answers(), [self.EMAIL])
        msgid = self.client.sent[-1][2]
        self.daemon.handle_line(f"PKTNET>APRS,TCPIP*::PP5PK-5  :ack{msgid[1:]}")  # not a dialog id: ignored
        self.daemon.handle_line(f"PKTNET>APRS,TCPIP*::PP5PK-5  :ack{msgid}")
        row = self.daemon.db.conn.execute("SELECT acked_at FROM dialog_steps").fetchone()
        self.assertIsNotNone(row[0])

    def test_nothing_is_sent_after_the_timeout_or_for_a_stranger(self):
        old = (utcnow() - dt.timedelta(minutes=30)).isoformat()
        self.daemon.db.conn.execute("UPDATE sends SET created_at = ?", (old,))
        self.bot("Deseja o certificado? Responda com seu email ou apenas NAO")
        self.assertEqual(self.answers(), [])
        self.daemon.handle_line("N0BODY>APRS,TCPIP*::PP5PK-5  :Deseja o certificado?")
        self.assertEqual(self.answers(), [])

    def test_a_rule_needing_a_missing_station_value_is_skipped(self):
        self.daemon.cfg["station"]["email"] = ""
        self.bot("Deseja o certificado? Responda com seu email ou apenas NAO")
        self.assertEqual(self.answers(), [])

    def test_invalid_rules_are_refused(self):
        for rule in ({"when": "("}, {"when": "x"}, {"send": "x"}):
            with self.assertRaises(ConfigError, msg=rule):
                Net.from_dict({"callsign": "PKTNET", "message": "CHECK", "dialog": [rule]})


if __name__ == "__main__":
    unittest.main()
