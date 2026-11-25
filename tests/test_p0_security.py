"""P0: allowlist Telegram + serializare SQLite."""
import os
import tempfile
import threading
import unittest

# Importul monitor cere tokeni; .env din proiect e suficient.
import monitor


class AllowlistTests(unittest.TestCase):
    def test_parse_csv_and_semicolon(self):
        self.assertEqual(
            monitor.parse_allowed_chat_ids("111, 222;333"),
            frozenset({"111", "222", "333"}),
        )

    def test_empty_is_fail_closed(self):
        self.assertEqual(monitor.parse_allowed_chat_ids(""), frozenset())
        self.assertEqual(monitor.parse_allowed_chat_ids("  "), frozenset())

    def test_chat_is_allowed_uses_frozenset(self):
        original = monitor.TELEGRAM_ALLOWED_CHAT_IDS
        try:
            monitor.TELEGRAM_ALLOWED_CHAT_IDS = frozenset({"7076547920"})
            self.assertTrue(monitor.chat_is_allowed("7076547920"))
            self.assertTrue(monitor.chat_is_allowed(7076547920))
            self.assertFalse(monitor.chat_is_allowed("999"))
            monitor.TELEGRAM_ALLOWED_CHAT_IDS = frozenset()
            self.assertFalse(monitor.chat_is_allowed("7076547920"))
        finally:
            monitor.TELEGRAM_ALLOWED_CHAT_IDS = original


class SqliteLockTests(unittest.TestCase):
    def test_concurrent_writes_under_db_session(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        original = monitor.DB_PATH
        errors = []
        try:
            monitor.DB_PATH = path
            monitor.init_db()

            def writer(n):
                try:
                    for i in range(40):
                        with monitor.db_session() as conn:
                            conn.execute(
                                "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                (f"t{n}_{i}", i),
                            )
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            with monitor.db_session() as conn:
                count = conn.execute("SELECT COUNT(*) AS c FROM bot_state").fetchone()["c"]
            # 1 rând ads_since_last_digest din init + 4*40 scrieri
            self.assertEqual(count, 1 + 160)
        finally:
            monitor.DB_PATH = original
            try:
                os.unlink(path)
            except OSError:
                pass
            for suffix in ("-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
