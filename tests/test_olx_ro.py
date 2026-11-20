import unittest

import olx_ro


class OlxRomaniaTests(unittest.TestCase):
    def test_builds_romanian_search_url(self):
        self.assertEqual(
            olx_ro.build_search_url("iPhone 16"),
            "https://www.olx.ro/oferte/q-iphone-16/?search[order]=created_at:desc",
        )
        self.assertEqual(
            olx_ro.build_search_url("iPhone 16", page=2),
            "https://www.olx.ro/oferte/q-iphone-16/?page=2&search[order]=created_at:desc",
        )
        self.assertEqual(
            olx_ro.build_search_url("telefon încărcător"),
            "https://www.olx.ro/oferte/q-telefon-%C3%AEnc%C4%83rc%C4%83tor/?search[order]=created_at:desc",
        )
        with self.assertRaises(ValueError):
            olx_ro.build_search_url("iPhone 16", page=0)

    def test_parses_romanian_listing_ids_and_ignores_external_links(self):
        html = """
        <a href="/d/oferta/iphone-12-IDl7I3m.html?search_reason=search">
            First listing
        </a>
        <a href="https://www.olx.ro/d/oferta/iphone-12-IDl7I3m.html">
            Duplicate listing
        </a>
        <a href="https://www.autovit.ro/anunt/ford-ID7HQoBu.html">
            External listing
        </a>
        <a href="/oferte/user/m0S0a/">Seller page</a>
        """

        self.assertEqual(
            olx_ro.parse_listing_links(html),
            [("l7I3m", "https://www.olx.ro/d/oferta/iphone-12-IDl7I3m.html")],
        )

    def test_formats_and_parses_lei_prices(self):
        self.assertEqual(olx_ro.format_price(1234.5), "1.234,50 lei")
        self.assertEqual(olx_ro.parse_price("1.234 lei"), 1234.0)
        self.assertEqual(olx_ro.parse_price("1.234,50 RON"), 1234.5)

    def test_describes_price_above_or_below_market_median(self):
        self.assertEqual(
            olx_ro.describe_market_position(-43.8),
            "cu 43,8% peste mediana pieței",
        )
        self.assertEqual(
            olx_ro.describe_market_position(25),
            "cu 25,0% sub mediana pieței",
        )

    def test_caps_ai_tier_when_market_data_does_not_support_it(self):
        self.assertEqual(
            olx_ro.align_score_with_market(8, -43.8, 6),
            (4, "Tier C"),
        )
        self.assertEqual(
            olx_ro.align_score_with_market(10, 25, 8),
            (8, "Tier A"),
        )
        self.assertEqual(
            olx_ro.align_score_with_market(9, 25, 2),
            (6, "Tier B"),
        )

    def test_matches_exact_model_and_rejects_variants(self):
        self.assertTrue(
            olx_ro.title_matches_exact_model(
                "honor 600",
                "HONOR 600 5G 8/256GB Black – Ca Nou",
            )
        )
        self.assertFalse(
            olx_ro.title_matches_exact_model(
                "honor 600",
                "Honor 600 Smart 256GB",
            )
        )
        self.assertFalse(
            olx_ro.title_matches_exact_model(
                "honor 600",
                "Honor 600 Pro 512GB",
            )
        )
        self.assertTrue(
            olx_ro.title_matches_exact_model(
                "honor 600 pro",
                "Honor 600 Pro 512GB, Garantie 2 ani",
            )
        )
        self.assertTrue(
            olx_ro.title_matches_exact_model("jbl 310", "Boxă JBL 310 Bluetooth")
        )
        self.assertFalse(
            olx_ro.title_matches_exact_model("jbl 310", "JBL Flip 6")
        )
        self.assertFalse(
            olx_ro.title_matches_exact_model("jbl 310", "JBL Charge 5")
        )
        self.assertFalse(
            olx_ro.title_matches_exact_model("iphone 12", "iPhone 12 Pro Max")
        )


if __name__ == "__main__":
    unittest.main()
