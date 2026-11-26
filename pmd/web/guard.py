"""The request guard (Design §9-1, §9-2; Part 3 §11-6).

There is no login. Instead:

* the ``Host`` header must be a known name (the tailnet name or loopback);
* when an owner is configured, proxied requests must carry that owner's login;
* a changing request must come from PMD's own pages: ``Origin`` equal to the host
  and PMD's own request header, which only ``pmd.js`` adds;
* every response carries a strict Content Security Policy.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from ..config import Config

OWN_HEADER = "x-pmd-request"
IDENTITY_HEADER = "tailscale-user-login"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _host_name(value: str) -> str:
    """Host header without the port. Handles ``[::1]:8787``."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def check(config: Config, method: str, headers: dict[str, str]) -> tuple[int, str] | None:
    """Return ``(status, reason)`` when the request must be refused, else ``None``."""
    host = _host_name(headers.get("host", ""))
    if host not in config.allowed_hosts:
        return 400, "Unknown host."
    login = headers.get(IDENTITY_HEADER)
    proxied = host == (config.tailnet_name or "").lower() or "x-forwarded-for" in headers
    if config.allowed_login and (proxied or login is not None) and (login or "").strip().lower() != config.allowed_login.lower():
        return 403, "This dashboard belongs to another user."
    if method.upper() not in SAFE_METHODS:
        origin = headers.get("origin", "")
        origin_host = (urlsplit(origin).hostname or "").lower() if origin else ""
        if not origin_host or origin_host != host:
            return 403, "Changes are accepted only from PM Dash's own pages."
        if headers.get(OWN_HEADER) != "1":
            return 403, "Changes are accepted only from PM Dash's own pages."
    return None


class GuardMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, config: Config) -> None:
        super().__init__(app)
        self.config = config

    async def dispatch(self, request: Request, call_next) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        refused = check(self.config, request.method, headers)
        if refused:
            response: Response = PlainTextResponse(refused[1], status_code=refused[0])
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
