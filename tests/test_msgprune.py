"""Tests: parsers, plus end-to-end runs of the real CLI against a fake phone.

Run from the project folder:   python3 -m unittest discover -s tests -v
"""
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
SRC = os.path.join(PROJECT, "src")
FAKE_ADB = os.path.join(HERE, "fakephone", "adb")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(HERE, "fakephone"))

import fixture  # noqa: E402
from msgprune import adb as adbmod  # noqa: E402
from msgprune import util  # noqa: E402
from msgprune.inventory import Part, category  # noqa: E402
from msgprune.selection import placeholder_text  # noqa: E402

TEXT_TYPES = ("text/plain", "application/smil")


class ParserTests(unittest.TestCase):
    def test_basic_and_null(self):
        out = "Row: 0 _id=1, mid=2, ct=image/jpeg, name=NULL\nRow: 1 _id=3, mid=2, ct=text/plain, name=a.txt\n"
        rows = adbmod.parse_query_output(out, ["_id", "mid", "ct", "name"])
        self.assertEqual(rows, [
            {"_id": "1", "mid": "2", "ct": "image/jpeg", "name": None},
            {"_id": "3", "mid": "2", "ct": "text/plain", "name": "a.txt"},
        ])

    def test_commas_and_newlines_in_last_column(self):
        out = "Row: 0 _id=7, name=photo, beach, mid=1.jpg\nsecond line\nRow: 1 _id=8, name=x\n"
        rows = adbmod.parse_query_output(out, ["_id", "name"])
        self.assertEqual(rows[0]["name"], "photo, beach, mid=1.jpg\nsecond line")
        self.assertEqual(rows[1], {"_id": "8", "name": "x"})

    def test_commas_in_middle_column(self):
        out = "Row: 0 _id=7, cl=a, b.jpg, name=z\n"
        self.assertEqual(adbmod.parse_query_output(out, ["_id", "cl", "name"])[0]["cl"], "a, b.jpg")

    def test_empty(self):
        self.assertEqual(adbmod.parse_query_output("No result found.\n", ["_id"]), [])
        self.assertEqual(adbmod.parse_query_output("", ["_id"]), [])

    def test_unexpected_output_raises(self):
        with self.assertRaises(adbmod.QueryParseError):
            adbmod.parse_query_output("Row: 0 foo=1\n", ["_id"])


class UtilTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(util.parse_size("100KB"), 100_000)
        self.assertEqual(util.parse_size("1.5 gb"), 1_500_000_000)
        self.assertEqual(util.parse_size("2MiB"), 2 * 1024 * 1024)
        self.assertEqual(util.parse_size("512"), 512)
        with self.assertRaises(ValueError):
            util.parse_size("lots")
        self.assertEqual(util.fmt_size(2_340_000), "2.3 MB")
        self.assertEqual(util.fmt_size(999), "999 B")

    def test_cutoff(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 25)
        self.assertEqual(util.parse_cutoff("90d", now), now - dt.timedelta(days=90))
        self.assertEqual(util.parse_cutoff("2y", now), now - dt.timedelta(days=730))
        self.assertEqual(util.parse_cutoff("2023-01-31", now), dt.datetime(2023, 1, 31))
        with self.assertRaises(ValueError):
            util.parse_cutoff("6m", now)  # minutes or months? refuse to guess

    def test_categories_and_placeholder(self):
        self.assertEqual(category("text/plain"), "text")
        self.assertEqual(category("application/smil"), "text")
        self.assertEqual(category("text/x-vCard"), "other")
        self.assertEqual(category("image/jpeg; name=x"), "image")
        one = [Part(1, 1, "image/jpeg", None, None, 2_300_000)]
        self.assertEqual(placeholder_text(one), "[Photo removed to save space - 2.3 MB]")
        many = one + [Part(2, 1, "image/png", None, None, 1_000_000), Part(3, 1, "video/mp4", None, None, 7_000_000)]
        self.assertEqual(placeholder_text(many), "[2 photos and 1 video removed to save space - 10.3 MB]")
        self.assertNotIn(":", placeholder_text(many))  # `content --bind` splits on colons


class FakePhoneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="msgprune-test-")
        self.root = os.path.join(self.tmp, "phone")
        fixture.build(self.root)
        self.data = os.path.join(self.tmp, "data")
        self.backups = os.path.join(self.tmp, "backups")
        self.env = dict(os.environ, FAKEPHONE_ROOT=self.root, MSGPRUNE_ADB=FAKE_ADB, PYTHONPATH=SRC)
        self.before = self.snapshot()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, **env):
        cp = subprocess.run([sys.executable, "-m", "msgprune", "--data-dir", self.data, *args],
                            env=dict(self.env, **env), capture_output=True, text=True, timeout=300,
                            stdin=subprocess.DEVNULL)
        return cp.returncode, cp.stdout, cp.stderr

    def db(self):
        return sqlite3.connect(os.path.join(self.root, "telephony.db"))

    def snapshot(self):
        """part rows + sha256 of each attachment file."""
        con = self.db()
        rows = {}
        for r in con.execute("SELECT _id, mid, ct, cl, text, _data FROM part"):
            digest = None
            if r[5] and os.path.exists(r[5]):
                with open(r[5], "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
            rows[r[0]] = {"mid": r[1], "ct": r[2], "cl": r[3], "text": r[4], "data": r[5], "sha": digest,
                          "size": os.path.getsize(r[5]) if r[5] and os.path.exists(r[5]) else None}
        con.close()
        return rows

    def expected_targets(self, min_size=100_000, before_day=None, threads=None, types=None):
        con = self.db()
        boxes = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT _id, msg_box, thread_id, date FROM pdu")}
        con.close()
        out = set()
        for pid, p in self.before.items():
            box, thread, date = boxes[p["mid"]]
            if box not in (1, 2) or p["ct"] in TEXT_TYPES or not p["size"] or p["size"] < min_size:
                continue
            if before_day and date >= fixture.ts(before_day) - 12 * 3600:
                continue
            if threads and thread not in threads:
                continue
            if types and category(p["ct"]) not in types:
                continue
            out.add(pid)
        return out

    def appops_state(self):
        try:
            with open(os.path.join(self.root, "state.json")) as fh:
                return json.load(fh)["appops"]
        except OSError:
            return {}

    def assert_text_untouched(self, after):
        for pid, p in self.before.items():
            if p["ct"] in TEXT_TYPES:
                self.assertIn(pid, after, f"text part {pid} disappeared")
                self.assertEqual(after[pid]["text"], p["text"])


class EndToEndTests(FakePhoneCase):
    def test_devices_and_doctor(self):
        rc, out, err = self.cli("devices")
        self.assertEqual(rc, 0, err)
        self.assertIn("Pixel 11 Pro", out)
        rc, out, err = self.cli("doctor")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Can read the MMS attachments store", out)
        self.assertIn("Default SMS app: Google Messages", out)

    def test_no_phone(self):
        rc, out, err = self.cli("scan", FAKEPHONE_STATE="none")
        self.assertEqual(rc, 1)
        self.assertIn("USB debugging", err)
        rc, out, err = self.cli("scan", FAKEPHONE_STATE="unauthorized")
        self.assertIn("Allow USB debugging", err)

    def test_scan(self):
        rc, out, err = self.cli("scan")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Google Messages app storage", out)
        self.assertIn("64.4 GB", out)
        self.assertIn("Alex, Jr.", out)
        self.assertIn("Mom, Dad, Sis", out)
        self.assertIn("couldn't be measured", out)
        scans = [f for f in os.listdir(self.data) if f.endswith(".json") and f.startswith("scan-")]
        with open(os.path.join(self.data, scans[0])) as fh:
            data = json.load(fh)
        est = {e["min_size"]: e for e in data["prune_estimates"]}
        expected = self.expected_targets(100_000)
        self.assertEqual(est[100_000]["count"], len(expected))
        self.assertEqual(est[100_000]["bytes"], sum(self.before[p]["size"] for p in expected))
        self.assertEqual(self.snapshot(), self.before, "scan must not change anything")
        self.assertFalse(os.path.exists(os.path.join(self.root, "data/local/tmp/msgprune")))
        # second scan uses the size cache
        rc, out2, err = self.cli("scan")
        self.assertEqual(rc, 0)
        self.assertNotIn("Measuring attachment sizes", err)

    def test_preview_changes_nothing(self):
        rc, out, err = self.cli("prune")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("PRUNE PREVIEW", out)
        self.assertIn("Nothing has been changed", out)
        self.assertEqual(self.snapshot(), self.before)
        self.assertFalse(os.path.exists(self.backups))

    def test_apply_full_run(self):
        expected = self.expected_targets()
        rc, out, err = self.cli("prune", "--apply", "--yes", "--backup-dir", self.backups)
        self.assertEqual(rc, 0, out + err)
        after = self.snapshot()
        # 1. exactly the expected attachments are gone, files included
        gone = set(self.before) - set(after)
        self.assertEqual(gone, expected)
        for pid in expected:
            self.assertFalse(os.path.exists(self.before[pid]["data"]))
        # 2. all text survives; everything not targeted is byte-identical (draft included)
        self.assert_text_untouched(after)
        for pid, p in self.before.items():
            if pid not in expected:
                self.assertEqual(after[pid]["sha"], p["sha"])
        # 3. one placeholder per pruned message, describing what was removed
        new = {pid: p for pid, p in after.items() if pid not in self.before}
        pruned_mids = {self.before[pid]["mid"] for pid in expected}
        self.assertEqual(sorted(p["mid"] for p in new.values()), sorted(pruned_mids))
        texts = {p["mid"]: p["text"] for p in new.values()}
        self.assertTrue(all(p["ct"] == "text/plain" for p in new.values()))
        self.assertEqual(texts[7], f"[2 photos removed to save space - {util.fmt_size(2_150_000)}]")
        self.assertEqual(texts[3], "[Video removed to save space - 3.5 MB]")
        # 4. backups are byte-identical and indexed
        with open(os.path.join(self.backups, "index.csv"), newline="") as fh:
            index = {int(r["part_id"]): r for r in csv.DictReader(fh)}
        self.assertEqual(set(index), expected)
        for pid, row in index.items():
            with open(os.path.join(self.backups, row["file"]), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), self.before[pid]["sha"])
        self.assertTrue(any("Mom [1]" in r["file"] for r in index.values()))
        # 5. permission put back, phone temp files cleaned, journal written
        self.assertEqual(self.appops_state(), {})
        self.assertFalse(os.path.exists(os.path.join(self.root, "data/local/tmp/msgprune")))
        journals = os.listdir(os.path.join(self.data, "journals"))
        with open(os.path.join(self.data, "journals", journals[0])) as fh:
            events = [json.loads(line) for line in fh]
        self.assertEqual(events[-1]["event"], "run_end")
        self.assertEqual(events[-1]["deleted"], len(expected))
        self.assertIn(f"Removed {len(expected)} attachments", out)
        # 6. running again finds nothing left and adds no duplicate notes
        rc, out, err = self.cli("prune", "--apply", "--yes", "--backup-dir", self.backups)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Nothing to do", out)
        self.assertEqual(self.snapshot(), after)

    def test_phone_ignores_writes(self):
        rc, out, err = self.cli("prune", "--apply", "--yes", "--backup-dir", self.backups,
                                FAKEPHONE_IGNORE_APPOPS="1")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("didn't accept the change on the first message", err)
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(self.appops_state(), {})

    def test_uid_mode_fallback(self):
        rc, out, err = self.cli("prune", "--apply", "--yes", "--no-backup", FAKEPHONE_NEEDS_UID_MODE="1")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(set(self.before) - set(self.snapshot()), self.expected_targets())
        self.assertEqual(self.appops_state(), {})

    def test_limit_takes_oldest_whole_message(self):
        rc, out, err = self.cli("prune", "--apply", "--yes", "--limit", "1", "--no-backup")
        self.assertEqual(rc, 0, out + err)
        gone = set(self.before) - set(self.snapshot())
        # message 1 (2016-03-01 photo) is the oldest eligible one; the 2015 draft never is
        self.assertEqual(gone, {pid for pid in self.expected_targets() if self.before[pid]["mid"] == 1})

    def test_filters(self):
        rc, out, err = self.cli("prune", "--apply", "--yes", "--no-backup", "--skip", "Mom",
                                "--older-than", "2024-01-15", "--types", "image,video")
        self.assertEqual(rc, 0, out + err)
        gone = set(self.before) - set(self.snapshot())
        # thread 1 is "Mom"; thread 2 "Mom, Dad, Sis" also matches "Mom", so only threads 3 and 4 qualify
        self.assertEqual(gone, self.expected_targets(before_day="2024-01-15", threads={3, 4},
                                                     types={"image", "video"}))

    def test_only_by_number_without_placeholder(self):
        rc, out, err = self.cli("prune", "--apply", "--yes", "--no-backup", "--no-placeholder",
                                "--only", "555.123.0005")
        self.assertEqual(rc, 0, out + err)
        after = self.snapshot()
        self.assertEqual(set(self.before) - set(after), self.expected_targets(threads={4}))
        self.assertEqual(set(after) - set(self.before), set(), "no placeholder notes expected")


class BackupSpaceTests(FakePhoneCase):
    def test_refuses_when_mac_is_nearly_full(self):
        os.environ["FAKEPHONE_ROOT"] = self.root
        from msgprune import inventory, prune
        dev = adbmod.Device(FAKE_ADB, "FAKE11PRO", "Pixel 11 Pro")
        inv = inventory.gather(dev, self.data, quiet=True)
        parts = [p for p in inv.parts() if p.category != "text" and p.size]
        store = prune.BackupStore(self.backups)
        journal = prune.Journal(os.path.join(self.data, "j.jsonl"))
        fake_usage = mock.Mock(free=prune.MIN_FREE_AFTER_BACKUP + 1000)
        with mock.patch("msgprune.prune.shutil.disk_usage", return_value=fake_usage):
            with self.assertRaises(prune.PruneAborted) as ctx:
                prune.backup_all(dev, inv, parts, store, 2, journal)
        self.assertIn("Not enough space", str(ctx.exception))
        journal.close()


if __name__ == "__main__":
    unittest.main()
