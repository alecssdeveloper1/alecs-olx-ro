import re
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from typing import Union

from bs4 import BeautifulSoup

OLX_BASE_URL = "https://www.olx.ro"
_LISTING_ID_PATTERN = re.compile(r"-ID([a-z0-9]+)\.html$", re.IGNORECASE)
_ALLOWED_HOSTS = {"olx.ro", "www.olx.ro"}

# Sufixe/variante de model: dacă apar în titlu dar nu în căutare, anunțul e alt produs.
_VARIANT_TOKENS = frozenset(
    {
        "smart",
        "pro",
        "lite",
        "plus",
        "max",
        "ultra",
        "mini",
        "se",
        "fe",
        "neo",
        "air",
        "prime",
        "premium",
        "active",
        "sport",
        "ti",
        "super",
        "xt",
        "note",
        "play",
        "go",
        "flip",
        "charge",
        "clip",
        "pulse",
        "link",
        "tune",
        "wave",
        "quantum",
        "xtreme",
        "partybox",
        "boombox",
        "boom",
    }
)

_DIACRITICS = str.maketrans(
    {
        "ă": "a",
        "â": "a",
        "î": "i",
        "ș": "s",
        "ş": "s",
        "ț": "t",
        "ţ": "t",
    }
)


def tokenize_model_text(text: str) -> list[str]:
    normalized = str(text).lower().translate(_DIACRITICS)
    return re.findall(r"[a-z0-9]+", normalized)


def title_matches_exact_model(query: str, title: str) -> bool:
    """
    Acceptă doar anunțul pentru modelul cerut.
    Ex: „honor 600” ≠ „Honor 600 Smart”; „jbl 310” ≠ orice alt JBL.
    """
    query_tokens = tokenize_model_text(query)
    title_tokens = tokenize_model_text(title)
    if not query_tokens or not title_tokens:
        return False

    title_token_set = set(title_tokens)
    if any(token not in title_token_set for token in query_tokens):
        return False

    query_variants = {token for token in query_tokens if token in _VARIANT_TOKENS}
    title_variants = {token for token in title_tokens if token in _VARIANT_TOKENS}
    return title_variants <= query_variants


def build_search_url(query: str, page: int = 1) -> str:
    if page < 1:
        raise ValueError("page must be a positive integer")

    slug = quote(query.strip().lower().replace(" ", "-"), safe="-")
    url = f"{OLX_BASE_URL}/oferte/q-{slug}/"
    return url if page == 1 else f"{url}?page={page}"


def parse_listing_links(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    seen_ids = set()

    for anchor in soup.find_all("a", href=True):
        url = urljoin(OLX_BASE_URL, anchor["href"])
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.hostname not in _ALLOWED_HOSTS:
            continue

        match = _LISTING_ID_PATTERN.search(parts.path)
        if not match:
            continue

        listing_id = match.group(1)
        if listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        canonical_url = urlunsplit(("https", "www.olx.ro", parts.path, "", ""))
        listings.append((listing_id, canonical_url))

    return listings


def format_price(price: float) -> str:
    formatted = f"{price:,.2f}"
    formatted = formatted.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return f"{formatted} lei"


def parse_price(value: Union[str, int, float]) -> float:
    normalized = str(value).strip().lower()
    normalized = re.sub(r"\s*(?:lei|ron)\s*$", "", normalized)
    normalized = normalized.replace("\u00a0", "").replace(" ", "")

    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif normalized.count(".") > 1 or (
        "." in normalized and len(normalized.rsplit(".", 1)[1]) == 3
    ):
        normalized = normalized.replace(".", "")

    return float(normalized)


def describe_market_position(discount_percent: float) -> str:
    percent = f"{abs(discount_percent):.1f}".replace(".", ",")
    if discount_percent > 0:
        return f"cu {percent}% sub mediana pieței"
    if discount_percent < 0:
        return f"cu {percent}% peste mediana pieței"
    return "la nivelul medianei pieței"


def align_score_with_market(
    score: Union[str, int, float], discount_percent: float, sample_count: int
) -> tuple[int, str]:
    normalized_score = max(0, min(int(score), 10))
    if normalized_score == 0:
        return 0, "Tier C"

    if discount_percent < 0:
        normalized_score = min(normalized_score, 4)
    elif sample_count < 3 or discount_percent < 20:
        normalized_score = min(normalized_score, 6)
    elif discount_percent < 35:
        normalized_score = min(normalized_score, 8)

    if normalized_score >= 9:
        tier = "Tier S"
    elif normalized_score >= 7:
        tier = "Tier A"
    elif normalized_score >= 5:
        tier = "Tier B"
    else:
        tier = "Tier C"

    return normalized_score, tier
