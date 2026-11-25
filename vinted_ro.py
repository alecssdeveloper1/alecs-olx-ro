"""Căutare oferte pe Vinted.ro (catalog HTML, cele mai noi)."""

import os
import re
import statistics
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

import olx_ro

VINTED_BASE_URL = "https://www.vinted.ro"
VINTED_IMPERSONATE = "chrome120"
MIN_DISCOUNT = 0.20
MIN_SAMPLE = 4
# Transportul nu e pe cardul de catalog; îl adăugăm la costul de achiziție.
SHIPPING_LEI = float(os.getenv("VINTED_SHIPPING_LEI", "20"))
_ACCESSORY_TOKENS = frozenset(
    {
        "husa",
        "carcasa",
        "case",
        "cover",
        "folie",
        "cablu",
        "incarcator",
        "sticla",
        "bumper",
        "pouch",
        "etui",
        "maska",
        "maski",
        "kryt",
        "obudowa",
        "hoes",
        "hoesje",
        "funda",
        "coque",
        "pouzdro",
        "obal",
        "kapak",
        "skal",
        "kuori",
        "ovitek",
        "silikon",
        "silicone",
        "protector",
    }
)
# Sub acest raport față de mediană, prețul nu e același produs (husă, piesă).
_OUTLIER_RATIO = 0.45
_ITEM_ID = re.compile(r"/items/(\d+)")
_session = None


def build_search_url(query: str, page: int = 1) -> str:
    if page < 1:
        raise ValueError("page must be a positive integer")
    text = quote(query.strip())
    url = f"{VINTED_BASE_URL}/catalog?search_text={text}&order=newest_first"
    if page > 1:
        url += f"&page={page}"
    return url


def _client():
    global _session
    if _session is None:
        _session = cffi_requests.Session(impersonate=VINTED_IMPERSONATE)
        _session.get(f"{VINTED_BASE_URL}/", timeout=20)
    return _session


def reset_session():
    global _session
    _session = None


def fetch_catalog(query: str, page: int = 1, timeout: int = 25):
    url = build_search_url(query, page)
    response = _client().get(
        url,
        timeout=timeout,
        headers={"Referer": f"{VINTED_BASE_URL}/"},
    )
    if response.status_code in {401, 403, 429}:
        reset_session()
    return response


def _listing_title(card, link) -> str:
    """Titlul pus de vânzător, nu câmpul Model din catalog (poate fi greșit)."""
    raw = (link.get("title") or "").strip()
    if not raw:
        img = card.find("img")
        raw = (img.get("alt") or "").strip() if img else ""
    if ", Brand:" in raw:
        raw = raw.split(", Brand:", 1)[0].strip()
    if raw:
        return raw
    return _text_by_suffix(card, "--description-title")


def _text_by_suffix(card, suffix: str) -> str:
    node = card.find(attrs={"data-testid": re.compile(re.escape(suffix) + r"$")})
    if not node:
        return ""
    return node.get_text(" ", strip=True)


def parse_catalog(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for card in soup.select('[data-testid="grid-item"]'):
        link = card.find("a", href=True)
        if not link:
            continue
        match = _ITEM_ID.search(link["href"])
        if not match:
            continue
        item_id = match.group(1)
        if item_id in seen:
            continue
        seen.add(item_id)
        price_text = _text_by_suffix(card, "--price-text")
        if not price_text:
            continue
        try:
            price = olx_ro.parse_price(price_text)
        except ValueError:
            continue
        total_text = _text_by_suffix(card, "total-combined-price")
        total_price = None
        if total_text:
            try:
                total_price = olx_ro.parse_price(total_text)
            except ValueError:
                total_price = None
        title = _listing_title(card, link)
        image = ""
        img = card.find("img")
        if img and img.get("src"):
            image = img["src"]
        items.append(
            {
                "id": f"vinted:{item_id}",
                "title": title,
                "price": price,
                "total_price": total_price,
                "url": urljoin(VINTED_BASE_URL, link["href"].split("?")[0]),
                "image_url": image,
                "condition": _text_by_suffix(card, "--description-subtitle"),
            }
        )
    return items


def fetch_item(url: str) -> dict | None:
    response = _client().get(
        url,
        timeout=25,
        headers={"Referer": f"{VINTED_BASE_URL}/"},
    )
    if response.status_code != 200:
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    desc_node = soup.find(attrs={"itemprop": "description"})
    description = desc_node.get_text(" ", strip=True) if desc_node else ""
    if not title and not description:
        return None
    return {"title": title, "description": description}


def _is_accessory(query: str, title: str) -> bool:
    query_tokens = set(olx_ro.tokenize_model_text(query))
    title_tokens = set(olx_ro.tokenize_model_text(title))
    if query_tokens & _ACCESSORY_TOKENS:
        return False
    return bool(title_tokens & _ACCESSORY_TOKENS)


def _has_buyer_protection(item: dict) -> bool:
    price = float(item.get("price") or 0)
    total = item.get("total_price")
    return total is not None and float(total) > price


def catalog_unit_price(item: dict) -> float:
    """Preț omogen pe card (cu protecție dacă există), fără transport estimat."""
    price = float(item.get("price") or 0)
    if _has_buyer_protection(item):
        return float(item["total_price"])
    return price


def acquisition_cost(item: dict, shipping_lei: float | None = None) -> float:
    """Cost de cumpărare: unitate (+ protecție) + transport estimat."""
    ship = SHIPPING_LEI if shipping_lei is None else float(shipping_lei)
    return catalog_unit_price(item) + max(ship, 0.0)


def _median_reference_prices(comparable: list[dict]) -> tuple[list[float], bool]:
    """
    Mediana pe o cohortă omogenă: dacă majoritatea au protecție și sunt
    destule eșantioane, doar acele totaluri; altfel doar prețul listat.
    Nu include transportul estimat.
    """
    protected = [item for item in comparable if _has_buyer_protection(item)]
    use_protection = (
        len(protected) >= MIN_SAMPLE and len(protected) * 2 >= len(comparable)
    )
    if use_protection:
        return [float(item["total_price"]) for item in protected], True
    return [float(item.get("price") or 0) for item in comparable], False


def _comparable(items: list[dict], query: str) -> list[dict]:
    matched = []
    for item in items:
        title = item.get("title") or ""
        if catalog_unit_price(item) <= 5:
            continue
        if _is_accessory(query, title):
            continue
        if not olx_ro.listing_matches_exact_model(query, title, ""):
            continue
        matched.append(item)
    return matched


def classify_listings(
    items: list[dict],
    query: str,
    max_price: float,
    min_discount: float = MIN_DISCOUNT,
) -> dict:
    """Mediana pe cohortă omogenă; plafonul de cumpărare se aplică după."""
    comparable = _comparable(items, query)
    if not comparable:
        return {"deals": [], "median": None, "comparable": [], "median_basis": "listed"}

    ref_prices, used_protection = _median_reference_prices(comparable)
    median = float(statistics.median(ref_prices))
    floor = median * _OUTLIER_RATIO
    ceiling = median * (1 - min_discount)
    deals = []
    annotated = []
    for item in comparable:
        # Discount față de mediană: aceeași bază (protecție sau listat).
        if used_protection and _has_buyer_protection(item):
            ref = float(item["total_price"])
        elif used_protection:
            # Fără protecție într-o mediană pe protecție: folosim listatul (conservator).
            ref = float(item.get("price") or 0)
        else:
            ref = float(item.get("price") or 0)
        cost = acquisition_cost(item)
        enriched = {
            **item,
            "acquisition": cost,
            "median": median,
            "discount": ((median - ref) / median) if median else 0,
            "suspect": bool(median and ref < floor),
            "ref_price": ref,
        }
        annotated.append(enriched)
        if len(comparable) < MIN_SAMPLE or cost > max_price or ref > ceiling:
            continue
        deals.append(enriched)
    return {
        "deals": deals,
        "median": median,
        "comparable": annotated,
        "median_basis": "protection" if used_protection else "listed",
    }


def select_deals(
    items: list[dict],
    query: str,
    max_price: float,
    min_discount: float = MIN_DISCOUNT,
) -> tuple[list[dict], float | None]:
    classified = classify_listings(items, query, max_price, min_discount)
    return classified["deals"], classified["median"]
