"""Web search (Tavily) and existing fact-checks (Google Fact Check Tools).

Only the query text leaves your machine, after passing the egress guard (personal details in a query are
refused, secrets are denied). API keys travel in headers and never appear in errors or the ledger.
Results are untrusted web text. Only snippets are used: EDITH never fetches arbitrary URLs (no SSRF surface).

NOTE: request/response shapes follow the providers' public docs as I know them; verify against the current
docs when you first use real keys.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional
from urllib.parse import urlencode, urlparse

from ..broker.egress import EgressGuard, EgressRequest, Verdict
from ..broker.gateway import ProviderError
from ..privacy.sensitivity import Destination
from ..providers.http import LOOPBACK_HOSTS, get_json, post_json

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


class QueryBlocked(ProviderError):
    """The egress guard refused to send this query (personal details, secrets, kill switch...)."""


class BudgetExhausted(ProviderError):
    """The self-imposed free-tier budget for this service is used up."""


def clean_text(value: Any, limit: int) -> str:
    return _CTRL.sub(" ", value).strip()[:limit] if isinstance(value, str) else ""


def clean_query(query: str, limit: int = 300) -> str:
    q = " ".join(_CTRL.sub(" ", str(query)).split())
    if not q:
        raise ValueError("bad_query")
    return q[:limit]


def guard_query(guard: EgressGuard, host: str, text: str) -> None:
    d = guard.check(EgressRequest(host, Destination.CLOUD_FREE, text, "search_query"))
    if d.verdict != Verdict.ALLOW:
        raise QueryBlocked(d.reason)


def spend(counter, key: str, limit: int, period: str) -> None:
    if counter is not None and not counter.try_use(key, limit, period):
        raise BudgetExhausted(key)


def check_base_url(base_url: str) -> "tuple[str, str]":
    parsed = urlparse(base_url)
    if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("base_url must be https")
    return parsed.netloc.lower(), f"{parsed.scheme}://{parsed.netloc}"


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    domain: str


@dataclass(frozen=True)
class ClaimReview:
    claim: str
    claimant: str
    publisher: str
    url: str
    rating: str
    title: str


class TavilySearch:
    def __init__(self, api_key: str, guard: EgressGuard, counter=None, monthly_limit: int = 300,
                 base_url: str = "https://api.tavily.com", timeout: float = 20.0):
        if not api_key:
            raise ValueError("TAVILY_API_KEY is required")
        self.host, self._base = check_base_url(base_url)
        self._key, self.guard, self.counter = api_key, guard, counter
        self.monthly_limit, self._timeout = monthly_limit, timeout

    def __repr__(self) -> str:
        return f"TavilySearch(host={self.host!r})"

    def search(self, query: str, limit: int = 5) -> List[SearchResult]:
        query = clean_query(query)
        limit = max(1, min(int(limit), 10))
        guard_query(self.guard, self.host, query)
        spend(self.counter, "tavily", self.monthly_limit, "month")
        data = post_json(f"{self._base}/search",
                         {"query": query, "max_results": limit, "search_depth": "basic", "include_answer": False},
                         headers={"Authorization": f"Bearer {self._key}"}, timeout=self._timeout)
        out: List[SearchResult] = []
        for r in (data.get("results") if isinstance(data, dict) else None) or []:
            if not isinstance(r, dict) or not isinstance(r.get("url"), str):
                continue
            if urlparse(r["url"]).scheme not in ("http", "https"):
                continue
            out.append(SearchResult(clean_text(r.get("title"), 150) or r["url"][:80], r["url"][:300],
                                    clean_text(r.get("content"), 500), domain_of(r["url"])))
        return out[:limit]


class FactCheckApi:
    def __init__(self, api_key: str, guard: EgressGuard, counter=None, daily_limit: int = 100,
                 base_url: str = "https://factchecktools.googleapis.com", timeout: float = 20.0):
        if not api_key:
            raise ValueError("GOOGLE_API_KEY is required")
        self.host, self._base = check_base_url(base_url)
        self._key, self.guard, self.counter = api_key, guard, counter
        self.daily_limit, self._timeout = daily_limit, timeout

    def __repr__(self) -> str:
        return f"FactCheckApi(host={self.host!r})"

    def claims(self, query: str, language: Optional[str] = None, limit: int = 5) -> List[ClaimReview]:
        query = clean_query(query)
        guard_query(self.guard, self.host, query)
        spend(self.counter, "factcheck", self.daily_limit, "day")
        params = {"query": query, "pageSize": max(1, min(int(limit), 10))}
        if language:
            params["languageCode"] = language
        data = get_json(f"{self._base}/v1alpha1/claims:search?{urlencode(params)}",
                        headers={"X-Goog-Api-Key": self._key}, timeout=self._timeout)
        out: List[ClaimReview] = []
        for c in (data.get("claims") if isinstance(data, dict) else None) or []:
            if not isinstance(c, dict):
                continue
            for rv in c.get("claimReview") or []:
                if not isinstance(rv, dict) or not isinstance(rv.get("url"), str):
                    continue
                if urlparse(rv["url"]).scheme not in ("http", "https"):
                    continue
                pub = rv.get("publisher") if isinstance(rv.get("publisher"), dict) else {}
                out.append(ClaimReview(clean_text(c.get("text"), 200), clean_text(c.get("claimant"), 80),
                                       clean_text(pub.get("name") or pub.get("site"), 60), rv["url"][:300],
                                       clean_text(rv.get("textualRating"), 60), clean_text(rv.get("title"), 150)))
        return out[:limit]


def format_results(results: List[SearchResult]) -> str:
    if not results:
        return "No results."
    return "\n".join(f"{i}. {r.title}\n   {r.domain} - {r.snippet[:160]}\n   {r.url}"
                     for i, r in enumerate(results, 1))
