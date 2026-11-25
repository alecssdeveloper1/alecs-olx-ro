import re
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from typing import Union

from bs4 import BeautifulSoup

OLX_BASE_URL = "https://www.olx.ro"
_LISTING_ID_PATTERN = re.compile(r"-ID([a-z0-9]+)\.html$", re.IGNORECASE)
_ALLOWED_HOSTS = {"olx.ro", "www.olx.ro"}

# Sufixe/variante de model: dacă apar în anunț dar nu în căutare → alt produs.
# Inclusiv telefoane: Smart/Pro/Max/Ultra/Lite/FE/SE/Note/Fold/Edge…
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
        "fold",
        "edge",
        "gt",
        "ace",
        "nord",
        "magic",
        "rival",
        "digital",
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
_VARIANT_TOKENS_SORTED = tuple(sorted(_VARIANT_TOKENS, key=len, reverse=True))

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
    """Desparte și pe granița litere/cifre: smart5g → smart, 5, g; 600smart → 600, smart."""
    normalized = str(text).lower().translate(_DIACRITICS)
    tokens: list[str] = []
    for raw in re.findall(r"[a-z0-9]+", normalized):
        parts = re.findall(r"[a-z]+|\d+", raw)
        tokens.extend(parts if parts else [raw])
    return tokens


def _is_typo_junk(rest: str, variant: str) -> bool:
    """Rest scurt după variantă: smartt → t (litera finală repetată), nu digital+a."""
    return (
        0 < len(rest) <= 2
        and rest.isalpha()
        and set(rest) == {variant[-1]}
    )


def _extract_variants_from_token(token: str) -> set[str]:
    """
    Extrage variante din token: smart, smart5g, smartt, promax, 600pro.
    „pro” din „product” NU trece — restul trebuie gol / cifră / altă variantă / typo scurt.
    """
    if token in _VARIANT_TOKENS:
        return {token}

    found: set[str] = set()
    remaining = token
    while remaining:
        matched = False
        for variant in _VARIANT_TOKENS_SORTED:
            if not remaining.startswith(variant):
                continue
            rest = remaining[len(variant) :]
            if (
                rest == ""
                or rest.isdigit()
                or _is_typo_junk(rest, variant)
                or any(rest.startswith(v) for v in _VARIANT_TOKENS_SORTED)
            ):
                found.add(variant)
                remaining = rest.lstrip("0123456789")
                matched = True
                break
        if not matched:
            break
    return found


def _variant_tokens_in(tokens: list[str]) -> set[str]:
    found: set[str] = set()
    for token in tokens:
        found |= _extract_variants_from_token(token)
    return found


# Stocare: dacă o ceri în căutare, anunțul cu altă capacitate e respins.
_STORAGE_SIZES = frozenset({"32", "64", "128", "256", "512", "1024"})
_OPTIONAL_QUERY_UNITS = frozenset({"gb", "tb", "g", "ram", "lei", "ron"})


def extract_storage_sizes(text: str) -> set[str]:
    tokens = tokenize_model_text(text)
    sizes: set[str] = set()
    for i, token in enumerate(tokens):
        if token in _STORAGE_SIZES:
            sizes.add(token)
        elif token == "1" and i + 1 < len(tokens) and tokens[i + 1] == "tb":
            sizes.add("1024")
    return sizes


def listing_matches_exact_model(
    query: str, title: str, description: str = ""
) -> bool:
    """
    Model exact pe telefoane (și rest): ce cauți = ce primești.
    Tokenii căutării trebuie în titlu; variantele (Smart/Pro/…) din titlu+descriere
    trebuie să fie subset al căutării. Stocare cerută ≠ altă stocare în anunț.
    """
    query_tokens = tokenize_model_text(query)
    title_tokens = tokenize_model_text(title)
    if not query_tokens or not title_tokens:
        return False

    required_tokens = [
        token for token in query_tokens if token not in _OPTIONAL_QUERY_UNITS
    ]
    title_token_set = set(title_tokens)
    if any(token not in title_token_set for token in required_tokens):
        return False

    query_variants = _variant_tokens_in(query_tokens)
    blob = f"{title} {(description or '')[:800]}"
    blob_tokens = tokenize_model_text(blob)
    listing_variants = _variant_tokens_in(blob_tokens)
    if not listing_variants <= query_variants:
        return False

    query_storage = extract_storage_sizes(query)
    if query_storage:
        listing_storage = extract_storage_sizes(blob)
        if listing_storage and not query_storage.issubset(listing_storage):
            return False

    return True


def title_matches_exact_model(query: str, title: str) -> bool:
    """Compat: același filtru, doar pe titlu."""
    return listing_matches_exact_model(query, title, "")


def build_search_url(query: str, page: int = 1) -> str:
    """Căutare OLX ordonată de la cele mai noi anunțuri (sortarea „Noi”)."""
    if page < 1:
        raise ValueError("page must be a positive integer")

    slug = quote(query.strip().lower().replace(" ", "-"), safe="-")
    params = {"search[order]": "created_at:desc"}
    if page > 1:
        params = {"page": str(page), **params}

    def quote_query(value, safe="", encoding=None, errors=None):
        return quote(str(value), safe="[]:")

    query_string = urlencode(params, quote_via=quote_query)
    return f"{OLX_BASE_URL}/oferte/q-{slug}/?{query_string}"


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


_CONDITION_FROM_PLATFORM = {
    "new with tags": "sigilat",
    "new without tags": "nou",
    "very good": "utilizat",
    "good": "utilizat",
    "satisfactory": "utilizat",
}
_VARIANT_ORDER = (
    "mini",
    "se",
    "fe",
    "lite",
    "air",
    "neo",
    "plus",
    "pro",
    "max",
    "ultra",
    "fold",
    "flip",
    "note",
    "edge",
)


def format_storage(size: str) -> str:
    if str(size) == "1024":
        return "1 TB"
    return f"{size} GB"


def primary_storage(text: str) -> Union[str, None]:
    sizes = extract_storage_sizes(text or "")
    if len(sizes) == 1:
        return next(iter(sizes))
    return None


def variant_label(text: str) -> str:
    found = _variant_tokens_in(tokenize_model_text(text or ""))
    ordered = [token for token in _VARIANT_ORDER if token in found]
    ordered.extend(sorted(found - set(ordered)))
    return " ".join(ordered) if ordered else "standard"


def identify_condition(text: str, platform_condition: str = "") -> str:
    platform = (platform_condition or "").strip().lower()
    if platform in _CONDITION_FROM_PLATFORM:
        return _CONDITION_FROM_PLATFORM[platform]

    blob = str(text or "").lower().translate(_DIACRITICS)
    if any(marker in blob for marker in ("sigilat", "sealed", "new with tags")):
        return "sigilat"
    if any(
        marker in blob
        for marker in ("ca nou", "like new", "utilizat", "second hand", "second-hand", "uzat", "folosit")
    ):
        return "utilizat"
    tokens = set(tokenize_model_text(blob))
    if tokens & {"nou", "nefolosit", "new", "unused"}:
        return "nou"
    if "used" in tokens:
        return "utilizat"
    return "necunoscut"


def inspect_listing(
    title: str,
    description: str = "",
    platform_condition: str = "",
    description_fetched: bool = False,
) -> dict:
    """Stocare, stare, variantă și dacă descrierea contrazice titlul."""
    title_storage = primary_storage(title)
    description_storage = primary_storage(description)
    ok = True
    motiv = ""
    if title_storage and description_storage and title_storage != description_storage:
        ok = False
        motiv = (
            f"Stocare diferită: titlu {format_storage(title_storage)}, "
            f"descriere {format_storage(description_storage)}"
        )
    storage = title_storage or description_storage
    return {
        "ok": ok,
        "motiv": motiv,
        "storage": storage,
        "storage_label": format_storage(storage) if storage else "necunoscută",
        "condition": identify_condition(f"{title} {description}", platform_condition),
        "variant": variant_label(title),
        "description_fetched": description_fetched,
        "description_len": len((description or "").strip()),
    }


def with_confidence(profil: dict, sample_count: int = 0, cohort_specific: bool = False) -> dict:
    enriched = dict(profil)
    score = 20
    if enriched.get("description_fetched") and enriched.get("description_len", 0) >= 15:
        score += 25
    elif enriched.get("description_fetched"):
        score += 10
    if enriched.get("storage"):
        score += 20
    if enriched.get("condition") not in {None, "", "necunoscut"}:
        score += 15
    if enriched.get("variant") and enriched["variant"] != "standard":
        score += 10
    elif enriched.get("variant") == "standard":
        score += 5
    if sample_count >= 3:
        score += 10
    if cohort_specific:
        score += 10
    if not enriched.get("ok", True):
        score = min(score, 25)
    enriched["incredere"] = max(0, min(100, score))
    return enriched


def bind_profile(stats: dict, profil: dict) -> dict:
    merged = dict(stats or {})
    merged["profil"] = with_confidence(
        profil,
        sample_count=int(merged.get("total_esantioane") or 0),
        cohort_specific=bool(merged.get("cohort_specific")),
    )
    return merged


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
