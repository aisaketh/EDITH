"""File-based kill switch. Survives restarts: if HALT exists, EDITH stays stopped until /resume."""
from __future__ import annotations

import os
import time


class KillSwitch:
    def __init__(self, path: str):
        self.path = path

    @property
    def engaged(self) -> bool:
        return os.path.exists(self.path)

    def engage(self) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S"))

    def release(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
