"""Gemini API free tier. Destination class CLOUD_FREE: free-tier inputs may be used by Google.

The API key is read from the environment (later: OS keychain) inside the broker process only.
"""
from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlparse

from ..broker.gateway import ProviderError
from ..privacy.sensitivity import Destination
from .http import LOOPBACK_HOSTS, post_json

_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class GeminiProvider:
    dest = Destination.CLOUD_FREE

    def __init__(self, model: str, api_key: Optional[str] = None,
                 base_url: str = "https://generativelanguage.googleapis.com",
                 system: Optional[str] = None, timeout: float = 60.0, name: str = "gemini"):
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY is not set")
        if not _MODEL_RE.match(model):
            raise ValueError("invalid model name")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
            raise ValueError("Gemini base_url must be https (http allowed for loopback test servers only)")
        self.name = name
        self.model = model
        self.host = parsed.netloc.lower()
        self._base = f"{parsed.scheme}://{parsed.netloc}"
        self._api_key = key
        self._system = system
        self._timeout = timeout

    def __repr__(self) -> str:  # never expose the key
        return f"GeminiProvider(model={self.model!r}, host={self.host!r})"

    def complete(self, prompt: str) -> str:
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if self._system:
            payload["systemInstruction"] = {"parts": [{"text": self._system}]}
        data = post_json(f"{self._base}/v1beta/models/{self.model}:generateContent", payload,
                         headers={"x-goog-api-key": self._api_key}, timeout=self._timeout)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError("unexpected_response") from None  # includes safety-blocked replies
        if not text:
            raise ProviderError("empty_response")
        return text
