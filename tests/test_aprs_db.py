import datetime as dt
import unittest

from netsched.aprsis import aprs_passcode, clean_text, parse_message
from netsched.certificates import extract_urls, safe_name
from netsched.db import Database

NOW = dt.datetime(2026, 10, 10, 18, 0, tzinfo=dt.timezone.utc)


class AprsParsing(unittest.TestCase):
    def test_known_passcode(self):
        self.assertEqual(aprs_passcode("N0CALL"), 13023)
        self.assertEqual(aprs_passcode("n0call-7"), 13023)

    def test_parse_reply_with_msgno(self):
        msg = parse_message("CE4JWI-10>APRS,TCPIP*,qAC,T2X::PP5PK-7  :Gracias! http://x.org/c.pdf{42")
        self.assertEqual((msg.src, msg.dst, msg.msgno), ("CE4JWI-10", "PP5PK-7", "42"))
        self.assertEqual(msg.text, "Gracias! http://x.org/c.pdf")
        self.assertIsNone(msg.ack)

    def test_parse_ack_and_rej(self):
        self.assertEqual(parse_message("ANSRVR>APRS::PP5PK-7  :ack17").ack, ("ack", "17"))
        self.assertEqual(parse_message("ANSRVR>APRS::PP5PK-7  :rej17").ack, ("rej", "17"))

    def test_non_message_packets_are_ignored(self):
        self.assertIsNone(parse_message("PP5PK-7>APRS,TCPIP*:!2625.00S/04916.00W-test"))

    def test_clean_text(self):
        self.assertEqual(clean_text("ÁNGAMOS {x}|~"), "ANGAMOS  x")


class Certificates(unittest.TestCase):
    def test_extract_urls(self):
        text = "QSL at qsl.net/ce4jwi, or https://a.org/x.png. www.b.com/y)"
        self.assertEqual(extract_urls(text),
                         ["https://qsl.net/ce4jwi", "https://a.org/x.png", "https://www.b.com/y"])
        self.assertEqual(extract_urls("73 de PP5PK e.g. 1.5/2"), [])

    def test_safe_name(self):
        self.assertEqual(safe_name("CX2JO-10"), "CX2JO-10")
        self.assertEqual(safe_name("../etc/passwd"), "etc_passwd")


class DatabaseFlow(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.day = dt.date(2026, 10, 10)

    def test_send_is_unique_per_day_and_text(self):
        first = self.db.create_send("APRSMY", self.day, "CHECK", NOW)
        self.assertIsNotNone(first)
        self.assertIsNone(self.db.create_send("APRSMY", self.day, "CHECK", NOW))
        self.assertTrue(self.db.is_done("APRSMY", self.day, "CHECK"))
        self.assertFalse(self.db.is_done("APRSMY", dt.date(2026, 10, 11), "CHECK"))

    def test_ack_flow_and_retries(self):
        send_id = self.db.create_send("ANSRVR", self.day, "CQ HOTG", NOW)
        msgid = self.db.assign_msgid(send_id)
        self.assertEqual(len(self.db.due_retries(3, 60, NOW)), 1)   # never attempted yet
        self.db.record_attempt(send_id, NOW)
        self.assertEqual(self.db.due_retries(3, 60, NOW), [])        # too soon
        later = NOW + dt.timedelta(seconds=61)
        self.assertEqual(len(self.db.due_retries(3, 60, later)), 1)
        self.assertFalse(self.db.mark_ack(msgid, "OTHER", later))    # wrong station
        self.assertTrue(self.db.mark_ack(msgid, "ANSRVR", later))
        self.assertEqual(self.db.due_retries(3, 60, later), [])

    def test_expire_unacked(self):
        send_id = self.db.create_send("TICANET", self.day, "CQ TICANET", NOW)
        self.db.assign_msgid(send_id)
        for i in range(3):
            self.db.record_attempt(send_id, NOW + dt.timedelta(seconds=60 * i))
        self.assertEqual(self.db.expire_unacked(3, 60, NOW + dt.timedelta(seconds=300)), 1)

    def test_reply_dedupe_and_certificates(self):
        send_id = self.db.create_send("CE4JWI-10", self.day, "CORREO", NOW)
        self.assertEqual(self.db.latest_send_for("CE4JWI-10", NOW)["id"], send_id)
        reply_id = self.db.add_reply(send_id, "CE4JWI-10", "see https://x.org/c.pdf", None, NOW)
        self.assertTrue(self.db.reply_exists("ce4jwi-10", "see https://x.org/c.pdf", NOW))
        self.db.add_certificate(reply_id, "https://x.org/c.pdf", "manual", None, "needs code", NOW)
        self.db.add_certificate(reply_id, "https://x.org/d.pdf", "downloaded", "/tmp/d.pdf", None, NOW)
        self.assertEqual([r["url"] for r in self.db.open_certificates()], ["https://x.org/c.pdf"])


if __name__ == "__main__":
    unittest.main()
