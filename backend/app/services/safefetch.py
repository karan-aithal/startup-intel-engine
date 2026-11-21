"""Outbound-fetch guard (J2, decision 102): a client that refuses private, loopback, link-local and metadata addresses on every request,
including each redirect hop, plus a scheme check.

Why: company websites, careers pages and discovered feeds come from news text and third-party pages, so a hostile or mistyped URL could point
the poller at this machine (127.0.0.1:8000 has no login) or at a cloud metadata address. career-ops checks the resolved address at connect time
(providers/_ip-guard.mjs, MIT, reference only); httpx has no connect hook, so this checks in a request event hook instead. The hook runs for every
redirect hop. DNS rebinding (a name that answers public at the check and private at the connect) is closed by pinning: the transport's network backend
resolves the name itself, refuses any non-public answer, and connects to that exact address, so no second lookup can differ. TLS still verifies the
original host name (httpcore passes the origin host as server_hostname). Not covered: a proxy from the environment (HTTP_PROXY) replaces the transport,
leaving only the hook check; none is configured here.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpcore
import httpx

BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


class UnsafeUrl(httpx.RequestError):
    """Raised before any connection is made. A RequestError so existing `except httpx.HTTPError` handlers treat it as a failed fetch."""


def _bad_address(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return not a.is_global


async def resolve_public(host: str, port: int) -> list[str]:
    """The addresses a host name resolves to, all of which must be public. A literal address is checked as written."""
    host = host.lower().rstrip(".")
    if not host:
        raise UnsafeUrl("no host")
    if host in BLOCKED_HOSTS or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrl(f"local host name: {host}")
    try:
        literal = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        literal = None
    if literal is not None:
        if _bad_address(literal):
            raise UnsafeUrl(f"non-public address: {host}")
        return [literal]
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeUrl(f"{host} does not resolve: {e}") from e
    addrs = list(dict.fromkeys(i[4][0] for i in infos))
    if not addrs or any(_bad_address(a) for a in addrs):
        raise UnsafeUrl(f"{host} resolves to a non-public address")
    return addrs


async def check_url(url: str, *, schemes: tuple[str, ...] = ("https", "http")) -> None:
    u = urlsplit(url)
    if u.scheme not in schemes:
        raise UnsafeUrl(f"scheme {u.scheme!r} is not allowed: {url}")
    if not u.hostname or u.username or u.password:
        raise UnsafeUrl(f"no host, or credentials in the URL: {url}")
    await resolve_public(u.hostname, u.port or (80 if u.scheme == "http" else 443))


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Connects only to an address this class has just checked, so the check and the connect cannot see different DNS answers."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend):
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        last: Exception | None = None
        for ip in await resolve_public(host, port):
            try:
                return await self._inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except httpcore.ConnectError as e:  # try the next address, as a normal client would
                last = e
        raise last or UnsafeUrl(f"{host}: no address to connect to")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise UnsafeUrl("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


async def _hook(request: httpx.Request) -> None:
    await check_url(str(request.url))


def guarded_client(**kw) -> httpx.AsyncClient:
    """An AsyncClient whose every request, redirect hops included, passes check_url. Use it for any URL that did not come from this repo's config."""
    hooks = kw.pop("event_hooks", None) or {}
    hooks["request"] = [*hooks.get("request", []), _hook]
    if "transport" not in kw:  # a caller-supplied transport (a test's MockTransport) is left alone
        kw["transport"] = transport = httpx.AsyncHTTPTransport()
        transport._pool._network_backend = PinnedBackend(transport._pool._network_backend)  # private attributes: test_the_guarded_client_pins_the_connection fails loudly if they move
    return httpx.AsyncClient(event_hooks=hooks, **kw)
