"""python -m edith [--check]"""
from __future__ import annotations

import logging
import signal
import sys
import threading

from .app import build_app
from .config import Config, ConfigError


def gmail_login(cfg: Config) -> int:
    from .broker.egress import EgressGuard
    from .broker.gateway import ProviderError
    from .broker.ledger import Ledger
    from .connectors.google_oauth import FileTokenStore, GoogleOAuth
    if not (cfg.google_client_id and cfg.google_client_secret):
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first (see edith/connectors/google_oauth.py).",
              file=sys.stderr)
        return 2
    oauth = GoogleOAuth(cfg.google_client_id, cfg.google_client_secret, FileTokenStore(cfg.token_path),
                        EgressGuard(Ledger(cfg.ledger_path)))
    try:
        oauth.login()
    except ProviderError as e:
        print(f"Gmail login failed: {e}", file=sys.stderr)
        return 1
    print("Gmail connected (read-only). Restart EDITH and try /mail.")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cfg = Config.from_env()
        logging.basicConfig(filename=cfg.log_path, level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        if argv and argv[0] == "gmail-login":
            return gmail_login(cfg)
        app = build_app(cfg)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    providers = [p.name for p in getattr(app.assistant.llm, "providers", [])]
    print(f"Data dir : {cfg.data_dir}")
    print(f"Providers: {', '.join(providers) or 'none (only /commands will work)'}")
    print(f"Paired   : {'yes' if cfg.owner_chat_id else 'no'}")
    if "--check" in argv:
        return 0
    if cfg.owner_chat_id is None:
        print(f"\nOpen your bot in Telegram and send:  /pair {app.pair_code}\n")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print("EDITH is running. Ctrl-C to stop.")
    app.run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
