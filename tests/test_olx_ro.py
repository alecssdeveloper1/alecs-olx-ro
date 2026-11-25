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
                "Honor 600 Smartt",
            )
        )
        self.assertFalse(
            olx_ro.title_matches_exact_model(
                "honor 600",
                "Vand tel honor 600 smart5g",
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
        self.assertFalse(
            olx_ro.title_matches_exact_model("iphone 12", "iPhone 12promax 128GB")
        )
        self.assertFalse(
            olx_ro.title_matches_exact_model("samsung s23", "Samsung S23 Ultra")
        )
        self.assertTrue(
            olx_ro.title_matches_exact_model("samsung s23", "Samsung Galaxy S23 256GB")
        )
        self.assertFalse(
            olx_ro.listing_matches_exact_model(
                "honor 600",
                "Honor 600 256GB",
                "Honor 600 Smart sigilat, garantie Orange",
            )
        )
        self.assertTrue(
            olx_ro.listing_matches_exact_model(
                "honor 600",
                "Honor 600 256GB Black",
                "Telefon sigilat, factura si garantie",
            )
        )
        self.assertTrue(
            olx_ro.listing_matches_exact_model(
                "iphone 13 128",
                "iPhone 13 128GB Midnight",
                "Ca nou, baterie 91%",
            )
        )
        self.assertFalse(
            olx_ro.listing_matches_exact_model(
                "iphone 13 128",
                "iPhone 13 256GB Midnight",
                "Ca nou",
            )
        )
        self.assertTrue(
            olx_ro.listing_matches_exact_model(
                "iphone 13 128gb",
                "iPhone 13 128 GB",
                "",
            )
        )


class FlipFilterTests(unittest.TestCase):
    def test_rejects_parts_and_locks(self):
        import market_analytics

        self.assertIsNotNone(
            market_analytics.text_are_risc_flip(
                "iPhone 12 display spart", "pentru piese"
            )
        )
        self.assertIsNotNone(
            market_analytics.text_are_risc_flip(
                "iPhone 11", "icloud activ, cont blocat"
            )
        )
        self.assertIsNone(
            market_analytics.text_are_risc_flip(
                "iPhone 13 128GB", "sigilat, factura"
            )
        )

    def test_listing_profile_and_storage_cohort(self):
        profil = olx_ro.inspect_listing(
            "Samsung S26 Ultra 256GB",
            "nou, stare perfecta, 256 gb",
            description_fetched=True,
        )
        self.assertTrue(profil["ok"])
        self.assertEqual(profil["storage"], "256")
        self.assertEqual(profil["condition"], "nou")
        self.assertEqual(profil["variant"], "ultra")

        conflict = olx_ro.inspect_listing(
            "iPhone 13 256 GB",
            "de fapt este 128 gb",
            description_fetched=True,
        )
        self.assertFalse(conflict["ok"])

        sealed = olx_ro.inspect_listing(
            "Honor 600 Lite",
            "",
            platform_condition="New with tags",
            description_fetched=True,
        )
        self.assertEqual(sealed["condition"], "sigilat")
        self.assertGreaterEqual(
            olx_ro.with_confidence(sealed, sample_count=5, cohort_specific=False)["incredere"],
            50,
        )

        import market_analytics

        prices, specific = market_analytics.cohort_prices(
            [
                ("S26 Ultra 256", 4000),
                ("S26 Ultra 256GB", 4200),
                ("S26 Ultra 256 gb", 4500),
                ("S26 Ultra 512GB", 5200),
                ("S26", 3000),
            ],
            "s26 ultra",
            storage="256",
        )
        self.assertTrue(specific)
        self.assertEqual(prices, [4000, 4200, 4500])
        mixed, mixed_specific = market_analytics.cohort_prices(
            [
                ("S26 Ultra 256", 4000),
                ("S26 Ultra 512", 5200),
            ],
            "s26 ultra",
            storage="256",
        )
        self.assertFalse(mixed_specific)
        self.assertEqual(mixed, [4000, 5200])

    def test_thin_olx_sample_has_no_invented_margin(self):
        import sqlite3
        import tempfile

        import market_analytics

        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/ads.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE ads (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    price REAL,
                    source TEXT DEFAULT 'olx'
                )
                """
            )
            conn.execute(
                "INSERT INTO ads (id, title, price, source) VALUES ('1', 'S26 Ultra 256', 4000, 'olx')"
            )
            conn.execute(
                "INSERT INTO ads (id, title, price, source) VALUES ('vinted:9', 'S26 Ultra 256', 500, 'vinted')"
            )
            conn.commit()
            conn.close()
            stats = market_analytics.obtine_statistici_piata(
                "s26 ultra", 4000, cale_db=path
            )
        self.assertFalse(stats["esantion_suficient"])
        self.assertEqual(stats["marja_profit"], 0)
        self.assertEqual(stats["total_esantioane"], 1)

    def test_net_margin(self):
        import market_analytics

        self.assertEqual(
            market_analytics.marja_neta_lei({"marja_profit": 300}, 25),
            275.0,
        )


if __name__ == "__main__":
    unittest.main()
