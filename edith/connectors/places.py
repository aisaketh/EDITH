"""Place suggestions via Google Places API (New) Text Search, ranked locally with your stored preferences.

Privacy: only the text the user typed ("biryani near Banjara Hills") is sent. Your remembered preferences
(for example "I am vegetarian") are NEVER sent; they are applied afterwards, on this machine, to re-rank results.
Cost: Places is billed per request after a free allowance, so a monthly self-imposed cap is enforced and
remembered across restarts (EDITH_PLACES_MONTHLY, default 100). Set a budget alert in Google Cloud as well.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from ..broker.egress import EgressGuard
from ..providers.http import post_json
from .search import check_base_url, clean_query, clean_text, guard_query, spend

FIELD_MASK = ",".join("places." + f for f in (
    "displayName", "formattedAddress", "rating", "userRatingCount", "priceLevel", "googleMapsUri",
    "primaryTypeDisplayName", "currentOpeningHours.openNow", "servesVegetarianFood", "types"))
PRICE = {"PRICE_LEVEL_INEXPENSIVE": "₹", "PRICE_LEVEL_MODERATE": "₹₹", "PRICE_LEVEL_EXPENSIVE": "₹₹₹",
         "PRICE_LEVEL_VERY_EXPENSIVE": "₹₹₹₹"}
PRIOR_MEAN, PRIOR_WEIGHT = 4.0, 30
_STOP = {"that", "this", "with", "from", "have", "like", "love", "prefer", "favourite", "favorite", "eat",
         "food", "about", "when", "what", "very", "also", "dont", "does", "into", "only", "much"}


@dataclass(frozen=True)
class Place:
    name: str
    address: str
    rating: Optional[float]
    rating_count: int
    price: str
    open_now: Optional[bool]
    maps_url: str
    kind: str
    serves_veg: Optional[bool]
    types: Sequence[str]


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_places(data: Any) -> List[Place]:
    out: List[Place] = []
    for p in (data.get("places") if isinstance(data, dict) else None) or []:
        if not isinstance(p, dict):
            continue
        name = clean_text((p.get("displayName") or {}).get("text") if isinstance(p.get("displayName"), dict) else "", 100)
        if not name:
            continue
        hours = p.get("currentOpeningHours") if isinstance(p.get("currentOpeningHours"), dict) else {}
        kind = p.get("primaryTypeDisplayName")
        veg = p.get("servesVegetarianFood")
        url = p.get("googleMapsUri") if isinstance(p.get("googleMapsUri"), str) else ""
        out.append(Place(
            name, clean_text(p.get("formattedAddress"), 150), _num(p.get("rating")),
            int(_num(p.get("userRatingCount")) or 0), PRICE.get(p.get("priceLevel"), ""),
            hours.get("openNow") if isinstance(hours.get("openNow"), bool) else None,
            url[:300] if url.startswith("https://") else "",
            clean_text(kind.get("text") if isinstance(kind, dict) else "", 40),
            veg if isinstance(veg, bool) else None,
            [t for t in (p.get("types") or []) if isinstance(t, str)][:10]))
    return out


class GooglePlaces:
    def __init__(self, api_key: str, guard: EgressGuard, counter=None, monthly_limit: int = 100,
                 base_url: str = "https://places.googleapis.com", timeout: float = 20.0):
        if not api_key:
            raise ValueError("GOOGLE_API_KEY is required")
        self.host, self._base = check_base_url(base_url)
        self._key, self.guard, self.counter = api_key, guard, counter
        self.monthly_limit, self._timeout = monthly_limit, timeout

    def __repr__(self) -> str:
        return f"GooglePlaces(host={self.host!r})"

    def search(self, text_query: str, limit: int = 10) -> List[Place]:
        text_query = clean_query(text_query, 200)
        guard_query(self.guard, self.host, text_query)
        spend(self.counter, "places", self.monthly_limit, "month")
        data = post_json(f"{self._base}/v1/places:searchText",
                         {"textQuery": text_query, "maxResultCount": max(1, min(int(limit), 20)),
                          "languageCode": "en", "regionCode": "IN"},
                         headers={"X-Goog-Api-Key": self._key, "X-Goog-FieldMask": FIELD_MASK},
                         timeout=self._timeout)
        return parse_places(data)


def is_vegetarian(fact_texts: Sequence[str]) -> bool:
    """Heuristic over the user's stored facts ('I am vegetarian' yes; 'not vegetarian' / 'non-veg' no)."""
    text = " ".join(fact_texts).lower()
    if re.search(r"\bnot\s+(a\s+)?(strict\s+)?veg(etarian)?\b|\bnon[- ]?veg", text):
        return False
    return bool(re.search(r"\bvegetarian\b|\bveg\b|\bvegan\b", text))


def preference_tokens(fact_texts: Sequence[str]) -> set:
    toks = set()
    for t in fact_texts:
        for w in re.split(r"[\s,.;:!?()\[\]\"'/-]+", t.lower()):
            if len(w) >= 4 and w not in _STOP and not w.isdigit():
                toks.add(w)
    return toks


def score(p: Place, prefs: set, vegetarian: bool) -> float:
    rating = p.rating if p.rating is not None else 3.0
    s = (rating * p.rating_count + PRIOR_MEAN * PRIOR_WEIGHT) / (p.rating_count + PRIOR_WEIGHT)
    if p.open_now is True:
        s += 0.4
    elif p.open_now is False:
        s -= 0.3
    haystack = f"{p.name} {p.kind} {' '.join(p.types)}".lower()
    s += min(0.9, 0.3 * sum(1 for t in prefs if t in haystack))
    if vegetarian:
        if p.serves_veg is True:
            s += 0.6
        elif p.serves_veg is False:
            s -= 1.0
    return s


def rank_places(places: Sequence[Place], fact_texts: Sequence[str]) -> List[Place]:
    prefs, veg = preference_tokens(fact_texts), is_vegetarian(fact_texts)
    return sorted(places, key=lambda p: -score(p, prefs, veg))


def format_places(places: Sequence[Place], vegetarian: bool, limit: int = 5) -> str:
    if not places:
        return "No places found."
    lines = []
    for i, p in enumerate(places[:limit], 1):
        bits = [f"{p.rating:.1f}* ({p.rating_count})" if p.rating is not None else "no rating"]
        if p.price:
            bits.append(p.price)
        if p.open_now is not None:
            bits.append("open now" if p.open_now else "closed now")
        if vegetarian and p.serves_veg is not None:
            bits.append("serves vegetarian" if p.serves_veg else "no vegetarian listed")
        lines.append(f"{i}. {p.name}" + (f" ({p.kind})" if p.kind else "") + " - " + ", ".join(bits))
        if p.address:
            lines.append(f"   {p.address}")
        if p.maps_url:
            lines.append(f"   {p.maps_url}")
    return "\n".join(lines)
