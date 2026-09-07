"""Who is allowed to talk to this gateway.

A.R.S holds Alex's contracts, his learned preferences, and standing grants to act on his
real accounts. For as long as it listened only on loopback, "anyone who can reach the
socket" meant "anyone already logged into this Mac", and that was the whole access control
story — correctly, because a check nobody needs is a check that rots.

The moment it listens on the network that stops being true, so this module exists. Two
rules, and they are deliberately blunt:

  * A request from loopback is the owner. The desktop shell's own gateway runs there.
  * A request from anywhere else must carry the device token, or it is refused before it
    reaches a route. Not "refused before it does something dangerous" — refused before it
    is answered at all, because /api/status naming your model and your languages is
    already more than a stranger on a café network should learn.

The token is a shared secret, not an identity: it says "this device was paired", not
"this device is Alex". That is the right strength for a phone joining a home network and
the wrong strength for peers that answer each other's questions — those need the Ed25519
identities in ADR 0002, and this module does not pretend otherwise.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import socket
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("ars.gateway.access")

COOKIE = "ars_device"
TOKEN_FILE = "device_token"
QUERY_PARAM = "t"

PUBLIC_PATHS = frozenset({"/health"})
"""Answerable without a token. Only liveness — it says nothing about the user, and a
device that cannot check whether the gateway is up cannot tell "wrong token" from
"machine asleep"."""


def load_or_create_token(data_dir: Path) -> str:
    """The device token, created once and kept at 0600.

    Persisted rather than regenerated per start, because a token that changes on every
    restart would re-pair every phone every morning, and a pairing flow people perform
    daily is a pairing flow people disable.
    """
    path = Path(data_dir) / TOKEN_FILE
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    log.info("device token created at %s", path)
    return token


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def lan_address() -> str | None:
    """This machine's address on the local network, for showing a pairing URL.

    Uses a UDP socket to a routable address to ask the OS which interface it would use.
    Nothing is sent — UDP connect only sets the peer — but it beats guessing at
    `gethostbyname`, which returns loopback as often as not.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routable, never routed
            return probe.getsockname()[0]
    except OSError:
        return None


def origin_is_own_page(origin: str | None, host_header: str | None) -> bool:
    """Is this WebSocket handshake coming from A.R.S's own page?

    WebSockets are not subject to the same-origin policy and trigger no preflight, so a
    browser will happily open `ws://127.0.0.1:<port>/ws` FOR ANY PAGE THE USER HAS OPEN.
    The handshake then arrives from 127.0.0.1 and, under a loopback-means-owner rule, is
    treated as Alex. Any web page — an advertisement in an iframe is enough — could ask
    A.R.S questions and read the answers: recalled memory, passages from his documents,
    everything he has taught it. With the standing web-search grant it is a two-way
    channel, because "search the web for <private answer>" sends that answer to a server
    the attacker chose.

    Browsers always send `Origin` on a WebSocket handshake and it cannot be forged from
    script. A hostile page's origin is its own, never ours, so requiring the origin to
    match the page A.R.S itself served closes it. Absent origin is allowed: that is a
    non-browser client (a script, a native app), which is not the threat here — anything
    running as a local process already has the filesystem.
    """
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    # `Host` is what the client asked for, which is exactly what its own page's origin
    # would be. Comparing against it rather than a configured address keeps this correct
    # for loopback, for the LAN address a paired phone uses, and for a future port.
    return bool(host_header) and parsed.netloc == host_header


def presented_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.query_params.get(QUERY_PARAM) or request.cookies.get(COOKIE)


def tokens_match(presented: str, expected: str) -> bool:
    """Constant-time comparison that cannot be crashed by the caller.

    `secrets.compare_digest` raises TypeError on non-ASCII strings, and the presented
    token comes straight off the wire — `?t=parolă` turned a failed authentication into an
    unhandled 500. Not a bypass, but an error path an unauthenticated caller controls, and
    a 500 where a 401 belongs.
    """
    try:
        return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    except (AttributeError, TypeError):
        return False


class DeviceTokenMiddleware(BaseHTTPMiddleware):
    """Refuses anything from off-device that does not carry the token."""

    def __init__(self, app, *, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS or is_loopback(
            request.client.host if request.client else None
        ):
            return await call_next(request)

        presented = presented_token(request)
        if presented is None or not tokens_match(presented, self._token):
            log.warning("refused %s %s from %s", request.method, request.url.path,
                        request.client.host if request.client else "unknown")
            return JSONResponse(
                {"error": "this device is not paired with A.R.S",
                 "hint": "open the pairing link shown on the machine running A.R.S"},
                status_code=401,
            )

        response = await call_next(request)
        if request.query_params.get(QUERY_PARAM):
            # Paired by following a link: hold it in a cookie so the token stops appearing
            # in every subsequent URL, and in the logs of everything that sees them.
            response.set_cookie(
                COOKIE, self._token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 365
            )
        return response
