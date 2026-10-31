"""Configuration from environment variables. Secrets (bot token, API keys) are never written to disk."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping, Optional


class ConfigError(Exception):
    pass


def _private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


@dataclass
class Config:
    data_dir: str
    tz: str = "Asia/Kolkata"
    telegram_token: Optional[str] = None
    local_model: Optional[str] = None        # Ollama model name, e.g. "qwen3:8b"
    gemini_model: Optional[str] = None       # needs GEMINI_API_KEY in the environment
    gemini_daily: int = 100                  # self-imposed daily request budget for the free tier
    allow_s1_cloud: bool = False             # allow low-sensitivity personal text to a free cloud model
    owner_chat_id: Optional[int] = None
    google_client_id: Optional[str] = None       # OAuth "Desktop app" client, for read-only Gmail
    google_client_secret: Optional[str] = None
    tavily_api_key: Optional[str] = None         # web search (free tier)
    google_api_key: Optional[str] = None         # Fact Check Tools + Places API (New)
    search_monthly: int = 300                    # self-imposed caps, remembered across restarts
    factcheck_daily: int = 100
    places_monthly: int = 100

    @property
    def token_path(self) -> str:
        return os.path.join(self.data_dir, "google_token.json")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "edith.db")

    @property
    def ledger_path(self) -> str:
        return os.path.join(self.data_dir, "ledger.db")

    @property
    def owner_path(self) -> str:
        return os.path.join(self.data_dir, "owner.json")

    @property
    def halt_path(self) -> str:
        return os.path.join(self.data_dir, "HALT")

    @property
    def log_path(self) -> str:
        return os.path.join(self.data_dir, "edith.log")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        env = os.environ if env is None else env
        data_dir = env.get("EDITH_HOME") or os.path.join(os.path.expanduser("~"), ".edith")
        _private_dir(data_dir)
        def number(name: str, default: str) -> int:
            try:
                value = int(env.get(name, default))
            except ValueError:
                raise ConfigError(f"{name} must be an integer") from None
            if value < 0:
                raise ConfigError(f"{name} must not be negative")
            return value

        daily = number("EDITH_GEMINI_DAILY", "100")
        cfg = cls(
            data_dir=data_dir,
            tz=env.get("EDITH_TZ", "Asia/Kolkata"),
            telegram_token=env.get("TELEGRAM_BOT_TOKEN") or None,
            local_model=env.get("EDITH_LOCAL_MODEL") or None,
            gemini_model=env.get("EDITH_GEMINI_MODEL") or None,
            gemini_daily=daily,
            allow_s1_cloud=env.get("EDITH_ALLOW_S1_CLOUD") == "1",
            google_client_id=env.get("GOOGLE_CLIENT_ID") or None,
            google_client_secret=env.get("GOOGLE_CLIENT_SECRET") or None,
            tavily_api_key=env.get("TAVILY_API_KEY") or None,
            google_api_key=env.get("GOOGLE_API_KEY") or None,
            search_monthly=number("EDITH_SEARCH_MONTHLY", "300"),
            factcheck_daily=number("EDITH_FACTCHECK_DAILY", "100"),
            places_monthly=number("EDITH_PLACES_MONTHLY", "100"),
        )
        cfg.owner_chat_id = cfg._load_owner()
        return cfg

    def _load_owner(self) -> Optional[int]:
        try:
            with open(self.owner_path, "r", encoding="utf-8") as fh:
                value = json.load(fh).get("chat_id")
        except (FileNotFoundError, ValueError, AttributeError):
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def save_owner(self, chat_id: int) -> None:
        fd = os.open(self.owner_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"chat_id": chat_id}, fh)
        self.owner_chat_id = chat_id
