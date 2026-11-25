"""Installation settings (Detailed Design Part 1 §5-2).

Read once at start from a TOML file beside the data directory. Secrets are never
settings: they are files under ``<data_dir>/secrets`` (Part 3 §11-2).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config" / "pm-dash" / "config.toml"
DEFAULT_DATA = Path.home() / ".local" / "share" / "pm-dash"
LOOPBACK = ("127.0.0.1", "::1", "localhost")


@dataclass(frozen=True)
class Config:
    data_dir: Path = DEFAULT_DATA
    listen_host: str = "127.0.0.1"
    port: int = 8787
    tailnet_name: str | None = None
    allowed_login: str | None = None
    extra_hosts: tuple[str, ...] = ()
    codex_fallback: bool = True
    codex_model: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pmd.sqlite3"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Host names a request may carry (Part 3 §11-6, "Host header")."""
        hosts = {"127.0.0.1", "localhost", *self.extra_hosts}
        if self.tailnet_name:
            hosts.add(self.tailnet_name)
        return frozenset(h.lower() for h in hosts)


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("PMD_CONFIG", DEFAULT_CONFIG))
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return Config(
        data_dir=Path(raw.get("data_dir", DEFAULT_DATA)).expanduser(),
        listen_host=str(raw.get("listen_host", "127.0.0.1")),
        port=int(raw.get("port", 8787)),
        tailnet_name=raw.get("tailnet_name") or None,
        allowed_login=raw.get("allowed_login") or None,
        extra_hosts=tuple(raw.get("extra_hosts", ())),
        codex_fallback=raw.get("codex_fallback", True),
        codex_model=raw.get("codex_model") or None,
    )
