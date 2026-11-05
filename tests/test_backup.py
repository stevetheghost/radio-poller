import gzip
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from app import backup, db, server
from app.providers import Play

NOW = 1_793_800_000


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "radio.db"
        self.backups = self.dir / "backups"
        db.init(self.path)
        conn = db.connect(self.path)
        db.add_station(conn, "a", "A", "iheart", "1", tz="America/Los_Angeles")
        db.insert_plays(conn, "a", [Play("Adele", "Hello", NOW + i * 300) for i in range(20)])
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def count(self, path):
        conn = sqlite3.connect(str(path))
        try:
            return conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        finally:
            conn.close()


class MakeBackupTests(Base):
    def test_a_backup_is_compressed_and_unpacks_to_the_database(self):
        path = backup.make_backup(self.path, self.backups, keep=7, now=NOW)
        self.assertRegex(path.name, r"^radio-\d{8}-\d{6}\.db\.gz$")
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), [path.name])  # no uncompressed copy left behind
        unpacked = self.dir / "restored.db"
        with gzip.open(path, "rb") as src:
            unpacked.write_bytes(src.read())
        self.assertEqual(self.count(unpacked), 20)
        conn = sqlite3.connect(str(unpacked))
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        conn.close()
        self.assertLess(path.stat().st_size, unpacked.stat().st_size)

    def test_a_restore_from_the_gzip_works_like_the_readme_says(self):
        path = backup.make_backup(self.path, self.backups, keep=7, now=NOW)
        conn = db.connect(self.path)
        db.insert_plays(conn, "a", [Play("New", "Song", NOW + 99999)])
        conn.close()
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        with gzip.open(path, "rb") as src:  # gunzip -c backups/radio-….db.gz > data/radio.db
            self.path.write_bytes(src.read())
        db.init(self.path)
        self.assertEqual(self.count(self.path), 20)

    def test_a_corrupt_compressed_file_is_not_kept(self):
        real = gzip.open

        def damaged_open(target, *args, **kwargs):  # the read-back check: someone flips bytes in the archive first
            with open(target, "r+b") as f:
                f.seek(os.path.getsize(target) // 2)
                f.write(b"\xff\xff\xff\xff")
            return real(target, *args, **kwargs)

        with mock.patch.object(backup.gzip, "open", damaged_open):
            with self.assertRaises(Exception):
                backup.make_backup(self.path, self.backups, keep=7, now=NOW)
        self.assertEqual(list(self.backups.iterdir()), [])  # neither the .gz nor the stepping-stone copy

    def test_a_failed_compression_leaves_nothing_behind(self):
        with mock.patch.object(backup, "_gzip_file", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                backup.make_backup(self.path, self.backups, keep=7, now=NOW)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_compression_can_be_turned_off(self):
        path = backup.make_backup(self.path, self.backups, keep=7, now=NOW, compress=False)
        self.assertRegex(path.name, r"^radio-\d{8}-\d{6}\.db$")

    def test_plain_and_compressed_backups_are_pruned_together(self):
        for n in range(3):
            backup.make_backup(self.path, self.backups, keep=3, now=NOW + n * 86400, compress=False)
        for n in range(3, 5):
            backup.make_backup(self.path, self.backups, keep=3, now=NOW + n * 86400)
        names = [p.name for p in backup.list_backups(self.backups)]
        self.assertEqual(len(names), 3)
        self.assertEqual([n.endswith(".gz") for n in names], [False, True, True])  # the oldest two plain ones went

    def test_a_backup_is_a_complete_standalone_database(self):
        path = backup.make_backup(self.path, self.backups, keep=7, now=NOW, compress=False)
        self.assertRegex(path.name, r"^radio-\d{8}-\d{6}\.db$")
        self.assertEqual(path.parent, self.backups)
        self.assertEqual(self.count(path), 20)
        conn = sqlite3.connect(str(path))
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")  # no -wal / -shm needed
        self.assertEqual(conn.execute("SELECT timezone FROM stations").fetchone()[0], "America/Los_Angeles")
        conn.close()
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), [path.name])  # nothing half-written left over

    def test_it_is_consistent_while_the_database_is_being_written(self):
        stop = threading.Event()
        written = []

        def writer():
            conn = db.connect(self.path)
            i = 0
            while not stop.is_set():
                db.insert_plays(conn, "a", [Play("Roxette", "The Look", NOW + 100000 + i * 300)])
                written.append(i)
                i += 1
            conn.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            sizes = []
            for n in range(8):
                path = backup.make_backup(self.path, self.backups, keep=20, now=NOW + n, compress=False)
                conn = sqlite3.connect(str(path))
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                sizes.append(conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0])
                conn.close()
        finally:
            stop.set()
            thread.join()
        self.assertGreater(len(written), 0)
        self.assertEqual(sizes, sorted(sizes))  # each snapshot is a real point in time, never a torn mix
        self.assertGreaterEqual(min(sizes), 20)

    def test_a_backup_can_be_restored(self):
        path = backup.make_backup(self.path, self.backups, keep=7, now=NOW, compress=False)
        conn = db.connect(self.path)
        db.insert_plays(conn, "a", [Play("New", "Song", NOW + 99999)])
        conn.close()
        self.assertEqual(self.count(self.path), 21)
        for suffix in ("-wal", "-shm"):  # what the README says to remove before restoring
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        self.path.write_bytes(path.read_bytes())
        db.init(self.path)  # the app opens it like any other database
        self.assertEqual(self.count(self.path), 20)
        conn = db.connect(self.path)
        self.assertEqual(conn.execute("SELECT name FROM stations").fetchone()[0], "A")
        conn.close()

    def test_a_failed_backup_leaves_nothing_behind(self):
        bad = self.dir / "garbage.db"
        bad.write_bytes(b"this is not a database" * 100)
        with self.assertRaises(sqlite3.DatabaseError):
            backup.make_backup(bad, self.backups, keep=7, now=NOW)
        self.assertEqual(list(self.backups.iterdir()), [])


class RetentionTests(Base):
    def test_only_the_newest_are_kept_and_unrelated_files_are_left_alone(self):
        self.backups.mkdir()
        (self.backups / "notes.txt").write_text("mine")
        (self.backups / "radio-latest.db").write_text("not one of ours")
        (self.backups / "radio-20260101-000000.db.partial").write_text("crashed")
        (self.backups / "radio-20260101-000000.db.partial-shm").write_text("crashed")
        (self.backups / "radio-20260101-000000.db.partial-wal").write_text("crashed")
        for n in range(5):
            backup.make_backup(self.path, self.backups, keep=3, now=NOW + n * 86400, compress=False)
        names = sorted(p.name for p in self.backups.iterdir())
        kept = [n for n in names if backup.NAME.fullmatch(n)]
        self.assertEqual(len(kept), 3)
        self.assertEqual(kept, [p.name for p in backup.list_backups(self.backups)])
        self.assertEqual(kept[-1], time.strftime("radio-%Y%m%d-%H%M%S.db", time.gmtime(NOW + 4 * 86400)))  # the newest survived
        self.assertIn("notes.txt", names)
        self.assertIn("radio-latest.db", names)
        self.assertNotIn("radio-20260101-000000.db.partial", names)
        self.assertFalse([n for n in names if "partial" in n])

    def test_keeping_one_keeps_the_latest(self):
        for n in range(3):
            backup.make_backup(self.path, self.backups, keep=1, now=NOW + n * 60)
        self.assertEqual(len(backup.list_backups(self.backups)), 1)


class ScheduleTests(Base):
    def test_due_when_none_exists_or_the_newest_is_old_enough(self):
        self.assertTrue(backup.due(self.backups, 3600))  # no folder yet
        path = backup.make_backup(self.path, self.backups, keep=3, now=NOW)
        self.assertFalse(backup.due(self.backups, 3600))
        old = time.time() - 7200
        os.utime(path, (old, old))
        self.assertTrue(backup.due(self.backups, 3600))
        self.assertFalse(backup.due(self.backups, 86400))

    def test_status_summarises_the_folder(self):
        off = backup.status(None, 0, 7)
        self.assertEqual((off["enabled"], off["count"], off["last"]), (False, 0, None))
        self.assertEqual(backup.status(self.backups, 0, 7)["enabled"], False)
        empty = backup.status(self.backups, 24, 7)
        self.assertEqual((empty["enabled"], empty["count"], empty["last"]), (True, 0, None))
        backup.make_backup(self.path, self.backups, keep=7, now=NOW)
        backup.make_backup(self.path, self.backups, keep=7, now=NOW + 60)
        info = backup.status(self.backups, 24, 7)
        self.assertEqual((info["count"], info["every_hours"], info["keep"]), (2, 24, 7))
        self.assertGreater(info["bytes"], 0)
        self.assertAlmostEqual(info["last"], time.time(), delta=60)

    def wait_for(self, count, seconds=6):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if len(backup.list_backups(self.backups)) >= count:
                return True
            time.sleep(0.05)
        return False

    def test_the_thread_makes_one_backup_then_waits_until_the_next_is_due(self):
        stop = threading.Event()
        with mock.patch.object(backup, "INITIAL_DELAY", 0), mock.patch.object(backup, "CHECK_EVERY", 0.2):
            thread = backup.BackupThread(self.path, self.backups, 3600, 7, stop)
            thread.start()
            try:
                self.assertTrue(self.wait_for(1))
                time.sleep(1)  # several checks later: still just the one
                self.assertEqual(len(backup.list_backups(self.backups)), 1)
            finally:
                stop.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_the_thread_survives_a_failing_backup_and_stops_promptly(self):
        stop = threading.Event()
        with mock.patch.object(backup, "INITIAL_DELAY", 0), mock.patch.object(backup, "RETRY_AFTER_FAILURE", 0.2), \
             mock.patch.object(backup, "make_backup", side_effect=OSError("disk full")) as made:
            thread = backup.BackupThread(self.path, self.backups, 3600, 7, stop)
            thread.start()
            deadline = time.time() + 5
            while made.call_count < 2 and time.time() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(made.call_count, 2)  # it kept trying
            self.assertTrue(thread.is_alive())
            stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_a_stop_during_the_start_up_delay_ends_the_thread_without_a_backup(self):
        stop = threading.Event()
        thread = backup.BackupThread(self.path, self.backups, 3600, 7, stop)  # real 20 s delay
        thread.start()
        stop.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(backup.list_backups(self.backups), [])


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.servers = []

    def tearDown(self):
        for httpd in self.servers:
            httpd.shutdown()
            httpd.server_close()
        super().tearDown()

    def info(self, **kwargs):
        httpd = server.make_server(0, self.path, "America/New_York", host="127.0.0.1", **kwargs)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.servers.append(httpd)
        url = "http://127.0.0.1:%d/api/transfer-info" % httpd.server_address[1]
        return json.loads(urlopen(url, timeout=5).read())["backup"]

    def test_transfer_info_reports_backups(self):
        self.assertEqual(self.info()["enabled"], False)  # a server not set up for backups says so
        backup.make_backup(self.path, self.backups, keep=7, now=NOW)
        info = self.info(backup_dir=self.backups, backup_every_hours=12, backup_keep=5)
        self.assertEqual((info["enabled"], info["every_hours"], info["keep"], info["count"]), (True, 12, 5, 1))
        self.assertIsNotNone(info["last"])
        self.assertEqual(self.info(backup_dir=self.backups, backup_every_hours=0)["enabled"], False)


if __name__ == "__main__":
    unittest.main()
