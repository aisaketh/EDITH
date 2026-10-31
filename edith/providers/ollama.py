"""Local model via Ollama (https://ollama.com). Destination class LOCAL: loopback only."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from ..broker.gateway import ProviderError
from ..privacy.sensitivity import Destination
from .http import LOOPBACK_HOSTS, post_json

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")


class OllamaProvider:
    dest = Destination.LOCAL

    def __init__(self, model: str, base_url: str = "http://localhost:11434",
                 system: Optional[str] = None, timeout: float = 120.0, name: str = "local"):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
            raise ValueError("OllamaProvider only talks to a loopback http address")
        if not _MODEL_RE.match(model):
            raise ValueError("invalid model name")
        self.name = name
        self.model = model
        self.host = parsed.netloc.lower()
        self._base = f"{parsed.scheme}://{parsed.netloc}"
        self._system = system
        self._timeout = timeout

    def complete(self, prompt: str) -> str:
        messages = []
        if self._system:
            messages.append({"role": "system", "content": self._system})
        messages.append({"role": "user", "content": prompt})
        data = post_json(f"{self._base}/api/chat",
                         {"model": self.model, "messages": messages, "stream": False},
                         timeout=self._timeout)
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError):
            raise ProviderError("unexpected_response") from None
