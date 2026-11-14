import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from firstcontact.store import ContactStore, StoreError


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="first-contact-test-")
        self.path = os.path.join(self.directory, "contacts.db")
        self.store = ContactStore(self.path)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_first_contact_then_known(self):
        self.assertTrue(
            self.store.first_contact(
                "mario@example.com",
                "anna@example.it",
                first_seen=1700000000,
                first_client="203.0.113.9",
                first_helo="mail.example.com",
                first_msgid="<abc@x>",
            )
        )
        self.assertFalse(self.store.first_contact("mario@example.com", "anna@example.it"))

    def test_record_message_states(self):
        from firstcontact.store import STATUS_KNOWN, STATUS_NEW, STATUS_RESUMED

        now = 1700000000
        day = 86400
        status, _last, _first = self.store.record_message(
            "mario@example.com", "anna@example.it", first_seen=now - 100 * day
        )
        self.assertEqual(status, STATUS_NEW)

        status, previous_last, original_first = self.store.record_message(
            "mario@example.com",
            "anna@example.it",
            first_seen=now,
            dormant_seconds=90 * day,
        )
        self.assertEqual(status, STATUS_RESUMED)
        self.assertEqual(previous_last, now - 100 * day)
        self.assertEqual(original_first, now - 100 * day)
        self.assertEqual(
            self.store.lookup("mario@example.com", "anna@example.it")["last_seen"], now
        )

        status, _last, _first = self.store.record_message(
            "mario@example.com",
            "anna@example.it",
            first_seen=now + 10,
            dormant_seconds=90 * day,
        )
        self.assertEqual(status, STATUS_KNOWN)

    def test_resumed_boundary_is_inclusive(self):
        from firstcontact.store import STATUS_RESUMED

        now = 1700000000
        day = 86400
        self.store.record_message("a@x.it", "b@y.it", first_seen=now - 90 * day)
        status, _last, _first = self.store.record_message(
            "a@x.it", "b@y.it", first_seen=now, dormant_seconds=90 * day
        )
        self.assertEqual(status, STATUS_RESUMED)

    def test_dormant_disabled_reports_known(self):
        from firstcontact.store import STATUS_KNOWN

        now = 1700000000
        day = 86400
        self.store.record_message("a@x.it", "b@y.it", first_seen=now - 900 * day)
        status, _last, _first = self.store.record_message(
            "a@x.it", "b@y.it", first_seen=now, dormant_seconds=0
        )
        self.assertEqual(status, STATUS_KNOWN)

    def test_resumed_updates_last_seen_even_without_stats(self):
        store = ContactStore(self.path + "3", track_stats=False)
        now = 1700000000
        day = 86400
        try:
            store.record_message("a@x.it", "b@y.it", first_seen=now - 400 * day)
            status, _last, _first = store.record_message(
                "a@x.it", "b@y.it", first_seen=now, dormant_seconds=90 * day
            )
            from firstcontact.store import STATUS_RESUMED

            self.assertEqual(status, STATUS_RESUMED)
            self.assertEqual(store.lookup("a@x.it", "b@y.it")["last_seen"], now)
        finally:
            store.close()

    def test_concurrent_resumed_reports_single_alert(self):
        from firstcontact.store import STATUS_RESUMED

        now = 1700000000
        day = 86400
        self.store.record_message("race@example.com", "anna@example.it", first_seen=now - 900 * day)

        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            store = ContactStore(self.path)
            try:
                status, _last, _first = store.record_message(
                    "race@example.com",
                    "anna@example.it",
                    first_seen=now,
                    dormant_seconds=90 * day,
                )
            except Exception as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)
                return
            finally:
                store.close()
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results.count(STATUS_RESUMED), 1, results)

    def test_lookup_fields(self):
        self.store.first_contact(
            "mario@example.com",
            "anna@example.it",
            first_seen=1700000000,
            first_client="203.0.113.9",
            first_helo="mail.example.com",
            first_msgid="<abc@x>",
        )
        row = self.store.lookup("mario@example.com", "anna@example.it")
        self.assertEqual(row["sender"], "mario@example.com")
        self.assertEqual(row["recipient"], "anna@example.it")
        self.assertEqual(row["first_seen"], 1700000000)
        self.assertEqual(row["messages"], 1)
        self.assertEqual(row["first_client"], "203.0.113.9")
        self.assertEqual(row["first_helo"], "mail.example.com")
        self.assertEqual(row["first_msgid"], "<abc@x>")
        self.assertIsNone(self.store.lookup("mario@example.com", "altro@example.it"))

    def test_stats_track_messages(self):
        self.store.first_contact("a@x.it", "b@y.it")
        self.store.first_contact("a@x.it", "b@y.it")
        self.store.first_contact("a@x.it", "b@y.it")
        self.store.first_contact("c@x.it", "b@y.it")
        stats = self.store.stats()
        self.assertEqual(stats["pairs"], 2)
        self.assertEqual(stats["senders"], 2)
        self.assertEqual(stats["recipients"], 1)
        self.assertEqual(stats["messages"], 4)

    def test_without_stats_messages_stay_at_one(self):
        store = ContactStore(self.path + "2", track_stats=False)
        try:
            store.first_contact("a@x.it", "b@y.it")
            store.first_contact("a@x.it", "b@y.it")
            self.assertEqual(store.lookup("a@x.it", "b@y.it")["messages"], 1)
        finally:
            store.close()

    def test_concurrent_insert_reports_single_first_contact(self):
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            store = ContactStore(self.path)
            try:
                inserted = store.first_contact("race@example.com", "anna@example.it")
            except Exception as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)
                return
            finally:
                store.close()
            with lock:
                results.append(inserted)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 12)
        self.assertEqual(results.count(True), 1, results)
        self.assertEqual(self.store.pairs(), 1)

    def test_multi_thread_reuse_of_same_store(self):
        errors = []

        def worker(index):
            try:
                self.store.first_contact("m%d@example.com" % index, "anna@example.it")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.store.pairs(), 8)

    def test_thread_churn_reuses_one_connection_and_main_can_close(self):
        connections = []
        errors = []
        baseline = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None

        def worker(index):
            try:
                self.store.first_contact("m%d@example.com" % index, "anna@example.it")
                connections.append(self.store._conn)
            except Exception as exc:
                errors.append(exc)

        for index in range(30):
            thread = threading.Thread(target=worker, args=(index,))
            thread.start()
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({id(conn) for conn in connections}), 1)
        self.assertEqual(self.store.pairs(), 30)
        if baseline is not None:
            self.assertLessEqual(len(os.listdir("/proc/self/fd")) - baseline, 3)
        connection = connections[0]
        self.store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_close_waits_for_active_operation(self):
        self.store.first_contact("a@x.it", "b@y.it")
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        errors = []

        def trace(sql):
            if sql.startswith("SELECT COUNT(*)"):
                entered.set()
                release.wait(5)

        self.store._conn.set_trace_callback(trace)

        def read():
            try:
                self.assertEqual(self.store.pairs(), 1)
            except Exception as exc:
                errors.append(exc)

        def close():
            self.store.close()
            closed.set()

        reader = threading.Thread(target=read)
        closer = threading.Thread(target=close)
        reader.start()
        try:
            self.assertTrue(entered.wait(5))
            closer.start()
            self.assertFalse(closed.wait(0.1))
        finally:
            release.set()
            reader.join(5)
            if closer.ident is not None:
                closer.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(closed.is_set())

    def test_prune(self):
        now = int(time.time())
        self.store.first_contact("vecchio@example.com", "anna@example.it", first_seen=now - 900 * 86400)
        self.store.first_contact("nuovo@example.com", "anna@example.it", first_seen=now)
        self.assertEqual(self.store.prune(365, now=now, dry_run=True), 1)
        self.assertEqual(self.store.pairs(), 2)
        self.assertEqual(self.store.prune(365, now=now), 1)
        self.assertEqual(self.store.pairs(), 1)

    def test_prune_disabled(self):
        self.store.first_contact("a@x.it", "b@y.it")
        self.assertEqual(self.store.prune(0), 0)
        self.assertEqual(self.store.pairs(), 1)

    def test_integrity_check(self):
        self.assertEqual(self.store.integrity_check(), "ok")

    def test_iter_contacts_filters(self):
        self.store.first_contact("a@x.it", "b@y.it")
        self.store.first_contact("c@x.it", "b@y.it")
        self.store.first_contact("a@x.it", "d@y.it")
        self.assertEqual(len(list(self.store.iter_contacts(sender="a@x.it"))), 2)
        self.assertEqual(len(list(self.store.iter_contacts(recipient="b@y.it"))), 2)
        self.assertEqual(len(list(self.store.iter_contacts(limit=1))), 1)

    def test_directory_is_created(self):
        nested = os.path.join(self.directory, "sub", "dir", "contacts.db")
        store = ContactStore(nested)
        try:
            self.assertTrue(store.first_contact("a@x.it", "b@y.it"))
        finally:
            store.close()
        self.assertTrue(os.path.exists(nested))


if __name__ == "__main__":
    unittest.main()
