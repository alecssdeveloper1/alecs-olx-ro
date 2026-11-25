"""P1/P2: stări watch OLX/Vinted, cache created_at, filtre source."""
import os
import tempfile
import unittest

import monitor
import vinted_ro


class WatchLogicTests(unittest.TestCase):
    def test_reopen_on_price_drop(self):
        self.assertTrue(monitor.should_reopen_watch(1000, 900))
        self.assertFalse(monitor.should_reopen_watch(1000, 999.6))
        self.assertFalse(monitor.should_reopen_watch(1000, 1000))

    def test_reopen_forced_for_olx_refresh(self):
        self.assertTrue(monitor.should_reopen_watch(1000, 1000, force_reeval=True))

    def test_deep_round(self):
        self.assertTrue(monitor.is_deep_round(10))
        self.assertTrue(monitor.is_deep_round(20))
        self.assertFalse(monitor.is_deep_round(9))
        self.assertFalse(monitor.is_deep_round(0))


class CacheCreatedAtTests(unittest.TestCase):
    def test_update_preserves_created_at(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        original = monitor.DB_PATH
        try:
            monitor.DB_PATH = path
            monitor.init_db()
            monitor.salvar_ad_cache("ad1", "A", 100, "https://olx.ro/a", score=6, source="olx")
            with monitor.db_session() as conn:
                created = conn.execute(
                    "SELECT created_at FROM ads WHERE id = ?", ("ad1",)
                ).fetchone()["created_at"]
                conn.execute(
                    "UPDATE ads SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
                    ("ad1",),
                )
            monitor.salvar_ad_cache("ad1", "B", 90, "https://olx.ro/a", score=7, source="olx")
            with monitor.db_session() as conn:
                row = conn.execute("SELECT title, score, created_at FROM ads WHERE id = ?", ("ad1",)).fetchone()
            self.assertEqual(row["title"], "B")
            self.assertEqual(row["score"], 7)
            self.assertEqual(row["created_at"], "2020-01-01 00:00:00")
            self.assertIsNotNone(created)
        finally:
            monitor.DB_PATH = original
            for p in (path, path + "-wal", path + "-shm"):
                try:
                    os.unlink(p)
                except OSError:
                    pass


class TopSourceFilterTests(unittest.TestCase):
    def test_top_query_excludes_vinted(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        original = monitor.DB_PATH
        sent = []
        real_send = monitor.send_msg

        def fake_send(chat_id, text, reply_markup=None):
            sent.append(text)

        try:
            monitor.DB_PATH = path
            monitor.init_db()
            monitor.salvar_ad_cache(
                "olx1", "Honor 600", 1000, "https://olx.ro/1", score=9, review="ok", source="olx"
            )
            monitor.salvar_ad_cache(
                "vinted:9",
                "Vinted Phone",
                500,
                "https://www.vinted.ro/items/9",
                score=10,
                review="nu",
                source="vinted",
            )
            monitor.send_msg = fake_send
            monitor.show_top("1")
            self.assertEqual(len(sent), 1)
            self.assertIn("Honor 600", sent[0])
            self.assertNotIn("Vinted Phone", sent[0])
        finally:
            monitor.send_msg = real_send
            monitor.DB_PATH = original
            for p in (path, path + "-wal", path + "-shm"):
                try:
                    os.unlink(p)
                except OSError:
                    pass


class VintedMedianProtectionTests(unittest.TestCase):
    def setUp(self):
        self._ship = vinted_ro.SHIPPING_LEI
        vinted_ro.SHIPPING_LEI = 0

    def tearDown(self):
        vinted_ro.SHIPPING_LEI = self._ship

    def test_median_prefers_protection_cohort_when_enough(self):
        items = [
            {"id": "1", "title": "iPhone 13", "price": 800, "total_price": 860},
            {"id": "2", "title": "iPhone 13", "price": 900, "total_price": 960},
            {"id": "3", "title": "iPhone 13", "price": 1000, "total_price": 1060},
            {"id": "4", "title": "iPhone 13", "price": 1100, "total_price": 1160},
            {"id": "5", "title": "iPhone 13", "price": 500},  # fără protecție
        ]
        classified = vinted_ro.classify_listings(items, "iphone 13", 2000)
        self.assertEqual(classified["median_basis"], "protection")
        # mediană doar pe 860,960,1060,1160
        self.assertEqual(classified["median"], 1010)

    def test_shipping_added_to_acquisition(self):
        vinted_ro.SHIPPING_LEI = 20
        item = {"price": 800, "total_price": 860}
        self.assertEqual(vinted_ro.acquisition_cost(item), 880)
        self.assertEqual(vinted_ro.catalog_unit_price(item), 860)


class OlxWatchStateTests(unittest.TestCase):
    def test_over_budget_is_watch_not_closed(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        original = monitor.DB_PATH
        try:
            monitor.DB_PATH = path
            monitor.init_db()
            monitor.salveaza_stare("olx:1", "chat", "watch", 2000)
            self.assertFalse(monitor.ad_ja_notificado("olx:1", "chat"))
            monitor.salveaza_stare("olx:1", "chat", "sent", 1500)
            self.assertTrue(monitor.ad_ja_notificado("olx:1", "chat"))
            monitor.salveaza_stare("olx:2", "chat", "rejected", 10)
            self.assertTrue(monitor.ad_ja_notificado("olx:2", "chat"))
        finally:
            monitor.DB_PATH = original
            for p in (path, path + "-wal", path + "-shm"):
                try:
                    os.unlink(p)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
