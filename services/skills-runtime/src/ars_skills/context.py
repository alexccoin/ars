"""What a skill is given when it runs — and, more importantly, what it is not given.

A skill never receives: the user's raw credentials, an unrestricted HTTP client, the
conversation history, or the memory store. It receives a mediated context object. If a
skill can reach something not on this object, that is a sandbox escape.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from ars_protocol import Language, Provenance, SourceKind, TrustLevel


class NetworkDenied(RuntimeError):
    """A skill tried to reach a host outside its manifest allowlist."""


class MediatedHttp:
    """An HTTP client locked to a skill's declared allowlist.

    Two separate protections, because they defend against different attackers:

    * **Allowlist** — a skill may only reach hosts its manifest declared. `"*"` means
      "any public host", which is what a general web reader genuinely needs; it is not
      a way to opt out of the second protection.
    * **SSRF denylist** — no skill may reach loopback, private ranges, or link-local
      addresses, *whatever* the allowlist says. This is not paranoia: A.R.S runs on the
      user's own machine, next to their router admin page and their other services, and
      `169.254.169.254` is the cloud metadata endpoint. A page that says "fetch
      http://192.168.1.1/admin" must fail closed. Checked on the resolved IP, after DNS,
      on every redirect hop — a hostname that resolves to 127.0.0.1 is the standard bypass.
    """

    BROWSER_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
    HONEST_UA = "ARS/0.1 (+local personal assistant; respects robots.txt)"

    def __init__(self, allowlist: tuple[str, ...], *, timeout_s: float = 20.0,
                 max_bytes: int = 4 * 1024 * 1024, user_agent: str | None = None) -> None:
        self._allow = tuple(h.lower().lstrip(".") for h in allowlist)
        self._any_host = "*" in self._allow
        self._max_bytes = max_bytes
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=False,  # followed manually so every hop is re-checked
            headers={"User-Agent": user_agent or self.HONEST_UA},
        )

    def _check_allowlist(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise NetworkDenied(f"scheme {parsed.scheme!r} is not permitted")
        host = (parsed.hostname or "").lower()
        if not host:
            raise NetworkDenied(f"unparseable host in {url!r}")
        if not self._any_host and not any(
            host == a or host.endswith("." + a) for a in self._allow
        ):
            raise NetworkDenied(
                f"host {host!r} is not in this skill's allowlist {self._allow}"
            )
        return host

    @staticmethod
    def _reject_internal(ip: str) -> None:
        addr = ipaddress.ip_address(ip)
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            raise NetworkDenied(
                f"{ip} is an internal address; skills may not reach the local network "
                "or cloud metadata endpoints"
            )

    async def _check_ssrf(self, host: str) -> None:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, None, proto=socket.IPPROTO_TCP
            )
        except socket.gaierror as e:
            raise NetworkDenied(f"could not resolve {host!r}") from e
        for info in infos:
            self._reject_internal(info[4][0])

    async def _guard(self, url: str) -> None:
        await self._check_ssrf(self._check_allowlist(url))

    async def get(self, url: str, *, max_redirects: int = 4, **kw: Any) -> httpx.Response:
        for _ in range(max_redirects + 1):
            await self._guard(url)
            resp = await self._client.get(url, **kw)
            if resp.is_redirect and (loc := resp.headers.get("location")):
                url = str(httpx.URL(url).join(loc))
                continue
            if len(resp.content) > self._max_bytes:
                raise NetworkDenied(f"response exceeded {self._max_bytes} bytes")
            return resp
        raise NetworkDenied("too many redirects")

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        await self._guard(url)
        return await self._client.post(url, **kw)

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(slots=True)
class SkillContext:
    """Handed to a skill for the duration of one call."""

    http: MediatedHttp
    language: Language
    cancelled: asyncio.Event
    secret: Callable[[str], Awaitable[str | None]]
    """Fetches a credential BY NAME from the token vault. The skill receives the value
    only inside its own call and must never return it, log it, or place it in output.
    The reasoning layer can never call this — it is not on the tool interface."""

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise asyncio.CancelledError("skill cancelled by barge-in or user interrupt")


def external(source: SourceKind, uri: str | None, label: str | None = None) -> Provenance:
    """Provenance for anything written by someone other than the user.

    Every skill that fetches outside content stamps its output with this. It is what
    makes the guard treat the resulting turn as tainted, and it is not optional —
    a skill returning EXTERNAL text as trusted is a security defect.
    """
    return Provenance(source=source, trust=TrustLevel.EXTERNAL, uri=uri, label=label)
