import unittest

import vinted_ro


class _ShippingZeroMixin:
    def setUp(self):
        self._prev_shipping = vinted_ro.SHIPPING_LEI
        vinted_ro.SHIPPING_LEI = 0

    def tearDown(self):
        vinted_ro.SHIPPING_LEI = self._prev_shipping


SAMPLE = """
<div data-testid="grid-item">
  <a href="/items/101-iphone-13?referrer=catalog">x</a>
  <div data-testid="product-item-id-101--description-title">Apple iPhone 13</div>
  <div data-testid="product-item-id-101--description-subtitle">Good</div>
  <div data-testid="product-item-id-101--price-text">800,00 RON</div>
  <div data-testid="total-combined-price">860,00 RON</div>
</div>
<div data-testid="grid-item">
  <a href="/items/102-husa-iphone-13">x</a>
  <div data-testid="product-item-id-102--description-title">Husa iPhone 13</div>
  <div data-testid="product-item-id-102--price-text">40,00 RON</div>
</div>
<div data-testid="grid-item">
  <a href="/items/103-iphone-13-pro">x</a>
  <div data-testid="product-item-id-103--description-title">iPhone 13 Pro</div>
  <div data-testid="product-item-id-103--price-text">1400,00 RON</div>
</div>
<div data-testid="grid-item">
  <a href="/items/104-iphone-13">x</a>
  <div data-testid="product-item-id-104--description-title">iPhone 13</div>
  <div data-testid="product-item-id-104--price-text">1500,00 RON</div>
</div>
<div data-testid="grid-item">
  <a href="/items/105-iphone-13">x</a>
  <div data-testid="product-item-id-105--description-title">iPhone 13</div>
  <div data-testid="product-item-id-105--price-text">1600,00 RON</div>
</div>
<div data-testid="grid-item">
  <a href="/items/106-iphone-13">x</a>
  <div data-testid="product-item-id-106--description-title">iPhone 13</div>
  <div data-testid="product-item-id-106--price-text">1550,00 RON</div>
</div>
"""


class VintedTests(_ShippingZeroMixin, unittest.TestCase):
    def test_parse_catalog(self):
        items = vinted_ro.parse_catalog(SAMPLE)
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0]["id"], "vinted:101")
        self.assertEqual(items[0]["price"], 800)
        self.assertEqual(items[0]["total_price"], 860)
        self.assertEqual(items[0]["url"], "https://www.vinted.ro/items/101-iphone-13")

    def test_selects_only_bargains_of_exact_model(self):
        items = vinted_ro.parse_catalog(SAMPLE)
        deals, median = vinted_ro.select_deals(items, "iphone 13", 2000)
        # Cohortă mixtă → mediană pe preț listat: 800,1500,1600,1550
        self.assertEqual(median, 1525)
        self.assertEqual([d["id"] for d in deals], ["vinted:101"])

    def test_uses_seller_title_not_catalog_model(self):
        html = """
        <div data-testid="grid-item">
          <a href="/items/301-iphone-17-pro-max" title="iPhone 17 pro max, Brand: Apple, Model: iPhone 18 Pro Max, Condition: New, 5135 lei"></a>
          <div data-testid="product-item-id-301--description-title">Apple iPhone 18 Pro Max</div>
          <div data-testid="product-item-id-301--price-text">5135,56 RON</div>
        </div>
        """
        items = vinted_ro.parse_catalog(html)
        self.assertEqual(items[0]["title"], "iPhone 17 pro max")
        deals, _median = vinted_ro.select_deals(items, "iphone 18 pro max", 9000)
        self.assertEqual(deals, [])

    def test_ignores_case_priced_as_phone(self):
        html = SAMPLE + """
        <div data-testid="grid-item">
          <a href="/items/201-maska">x</a>
          <div data-testid="product-item-id-201--description-title">Maska za Honor 600 Lite</div>
          <div data-testid="product-item-id-201--price-text">38,00 RON</div>
        </div>
        <div data-testid="grid-item">
          <a href="/items/202-honor">x</a>
          <div data-testid="product-item-id-202--description-title">Honor 600 Lite</div>
          <div data-testid="product-item-id-202--price-text">850,00 RON</div>
        </div>
        <div data-testid="grid-item">
          <a href="/items/203-honor">x</a>
          <div data-testid="product-item-id-203--description-title">Honor 600 Lite</div>
          <div data-testid="product-item-id-203--price-text">900,00 RON</div>
        </div>
        <div data-testid="grid-item">
          <a href="/items/204-honor">x</a>
          <div data-testid="product-item-id-204--description-title">Honor 600 Lite</div>
          <div data-testid="product-item-id-204--price-text">950,00 RON</div>
        </div>
        <div data-testid="grid-item">
          <a href="/items/205-honor">x</a>
          <div data-testid="product-item-id-205--description-title">Honor 600 Lite</div>
          <div data-testid="product-item-id-205--price-text">1000,00 RON</div>
        </div>
        """
        deals, _median = vinted_ro.select_deals(
            vinted_ro.parse_catalog(html), "honor 600 lite", 1500
        )
        self.assertEqual(deals, [])

    def test_max_price_does_not_shrink_median(self):
        items = vinted_ro.parse_catalog(SAMPLE)
        deals, median = vinted_ro.select_deals(items, "iphone 13", 900)
        self.assertEqual(median, 1525)
        self.assertEqual([deal["id"] for deal in deals], ["vinted:101"])
        self.assertFalse(deals[0]["suspect"])
        self.assertEqual(deals[0]["acquisition"], 860)

    def test_cheap_phone_stays_and_is_marked_suspect(self):
        items = [
            {"id": "a", "title": "iPhone 13", "price": 400},
            {"id": "b", "title": "iPhone 13", "price": 1000},
            {"id": "c", "title": "iPhone 13", "price": 1000},
            {"id": "d", "title": "iPhone 13", "price": 1100},
            {"id": "e", "title": "iPhone 13", "price": 1200},
        ]
        deals, median = vinted_ro.select_deals(items, "iphone 13", 2000)
        self.assertEqual(median, 1000)
        cheap = next(deal for deal in deals if deal["id"] == "a")
        self.assertTrue(cheap["suspect"])

    def test_search_url(self):
        self.assertIn("search_text=iphone%2013", vinted_ro.build_search_url("iphone 13"))
        self.assertIn("page=2", vinted_ro.build_search_url("iphone 13", page=2))
        with self.assertRaises(ValueError):
            vinted_ro.build_search_url("iphone 13", page=0)

    def test_shipping_increases_acquisition_not_median(self):
        vinted_ro.SHIPPING_LEI = 20
        items = vinted_ro.parse_catalog(SAMPLE)
        deals, median = vinted_ro.select_deals(items, "iphone 13", 2000)
        self.assertEqual(median, 1525)
        self.assertEqual(deals[0]["acquisition"], 880)


if __name__ == "__main__":
    unittest.main()
