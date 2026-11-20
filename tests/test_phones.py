import unittest

import benchmarks
import market_analytics


class PhoneFocusTests(unittest.TestCase):
    def test_detects_phone_brands(self):
        name, tip, info = benchmarks.identifica_hardware("HONOR 600 5G 8/256GB Black")
        self.assertEqual(name, "HONOR")
        self.assertEqual(tip, "telefon")
        self.assertIn("puncte_atentie", info)

        name, tip, _ = benchmarks.identifica_hardware("iPhone 15 128GB")
        self.assertEqual(name, "IPHONE")
        self.assertEqual(tip, "telefon")

    def test_phone_search_helper(self):
        self.assertTrue(market_analytics.este_cautare_telefon("honor 600"))
        self.assertTrue(market_analytics.este_cautare_telefon("iPhone 12"))
        self.assertFalse(market_analytics.este_cautare_telefon("jbl 310"))

    def test_phone_liquidity_key(self):
        stats = market_analytics.obtine_statistici_piata("honor 600", 1500, 1500)
        self.assertEqual(stats["lichiditate"], "Ridicată")
        self.assertIn("zile", stats["timp_vanzare"])


if __name__ == "__main__":
    unittest.main()
