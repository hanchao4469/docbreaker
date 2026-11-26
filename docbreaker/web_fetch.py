"""Bounded public HTTP fetcher. DNS answers are checked and pinned to the socket."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlsplit, urlunsplit

import httpx

BOT = "DocBreakerBot"
# Identify both the HTTP client and crawler; keep BOT for robots.txt matching.
USER_AGENT = f"python-httpx/{httpx.__version__} {BOT}/1.0"
TRACKING = {"gclid", "fbclid", "msclkid", "_pos", "_sid", "_ss", "_psq", "_v", "srsltid"}
PRIVATE_PATH = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:cart|checkout[s]?|account|customer_authentication|orders|search|apps|challenge|password|admin)(?:/|$)", re.I)


class WebError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def normalize(value, base=None):
    try:
        if len(value) > 2048 or re.search(r"[\\\s\x00-\x1f\x7f]", value):
            return None
        p = urlsplit(urljoin(base, value) if base else value)
        if p.scheme not in ("http", "https") or not p.hostname or p.username is not None:
            return None
        if p.port not in (None, 443 if p.scheme == "https" else 80):
            return None
        host = p.hostname.rstrip(".").encode("idna").decode().lower()
        if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
            return None
        if not re.search(r"[a-z]", host.rsplit(".", 1)[-1]):
            return None
        query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True, max_num_fields=32)
                           if not k.lower().startswith("utm_") and k.lower() not in TRACKING])
        return urlunsplit((p.scheme, host, p.path or "/", query, ""))
    except (ValueError, UnicodeError):
        return None


def same_site(a, b):
    return (urlsplit(a).hostname or "").removeprefix("www.") == (urlsplit(b).hostname or "").removeprefix("www.")



def private_path(url):
    path = unquote(urlsplit(url).path)
    return bool(PRIVATE_PATH.search(path) or re.search(r"(?:^|/)\.\.?(?:/|$)", path))


def public_address(address):
    ip = ipaddress.ip_address(address)
    # IPv4-mapped IPv6 and transition prefixes are unnecessary for public crawling.
    return ip.is_global and not ip.is_multicast and not (ip.version == 6 and
        (ip.ipv4_mapped or ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96")))


class Fetcher:
    def __init__(self):
        self.client = httpx.AsyncClient(trust_env=False, timeout=8, limits=httpx.Limits(max_connections=8,
                                        max_keepalive_connections=0))
        self.locks = {}
        self.blocked_until = {}

    async def close(self):
        await self.client.aclose()

    async def get(self, url, seed, limit=6_000_000, permitted=None, *, image=False):
        for _ in range(5):
            if not same_site(url, seed) or (urlsplit(seed).scheme == "https" and urlsplit(url).scheme != "https"):
                raise WebError("redirect_out_of_scope")
            if private_path(url):
                raise WebError("private_path")
            if permitted and not await permitted(url):
                raise WebError("robots_denied")
            p = urlsplit(url)
            host = p.hostname
            port = 443 if p.scheme == "https" else 80
            try:
                records = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM), timeout=5)
                addresses = list(dict.fromkeys(r[4][0] for r in records))
                if not addresses or not all(map(public_address, addresses)):
                    raise WebError("non_public_address")
                addresses.sort(key=lambda ip: ":" in ip)
                ip = addresses[0]
                pinned = httpx.URL(url).copy_with(host=ip)
                key = host.removeprefix("www.")
                now = asyncio.get_running_loop().time()
                if len(self.locks) >= 512:
                    for old, lock in list(self.locks.items()):
                        if not lock.locked() and self.blocked_until.get(old, 0) <= now:
                            self.locks.pop(old)
                            self.blocked_until.pop(old, None)
                if key not in self.locks and len(self.locks) >= 512:
                    raise WebError("fetch_capacity")
                async with self.locks.setdefault(key, asyncio.Lock()):
                    if self.blocked_until.get(key, 0) > asyncio.get_running_loop().time():
                        raise WebError("http_429")
                    try:
                        async with asyncio.timeout(18):
                            async with self.client.stream("GET", pinned,
                                headers={"Host": host, "Cookie": "", "User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                                         "Accept": "image/jpeg,image/png,image/webp,image/gif" if image else "text/html,application/xhtml+xml,application/xml,text/xml,text/plain,*/*;q=0.1"},
                                extensions={"sni_hostname": host}) as response:
                                status = response.status_code
                                if status in (301, 302, 303, 307, 308):
                                    url = normalize(response.headers.get("location", ""), url)
                                    if not url:
                                        raise WebError("invalid_redirect")
                                    continue
                                if status == 429:
                                    retry_after = response.headers.get("retry-after", "")
                                    try:
                                        delay = (int(retry_after) if retry_after.isdigit() and len(retry_after) <= 9 else
                                                 (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                                    except (ValueError, OverflowError, TypeError):
                                        delay = 60
                                    self.blocked_until[key] = asyncio.get_running_loop().time() + max(60, delay)
                                if status != 200:
                                    raise WebError(f"http_{status}")
                                mime = response.headers.get("content-type", "").split(";")[0].lower()
                                allowed = ({"image/jpeg", "image/png", "image/webp", "image/gif"} if image else
                                           {"", "text/html", "application/xhtml+xml", "text/xml",
                                            "application/xml", "text/plain", "application/octet-stream"})
                                if mime not in allowed:
                                    raise WebError("unsupported_content")
                                if response.headers.get("content-encoding", "identity").lower() != "identity":
                                    raise WebError("unsupported_encoding")
                                body = bytearray()
                                async for chunk in response.aiter_raw():
                                    body.extend(chunk)
                                    if len(body) > limit:
                                        raise WebError("response_too_large")
                                return url, bytes(body), mime
                    finally:
                        await asyncio.sleep(0.25)
                        # Request cookies must never be shared between tenants or forwarded to a new site.
                        self.client.cookies.clear()
            except (httpx.HTTPError, OSError, TimeoutError) as exc:
                raise WebError("network_error") from exc
        raise WebError("too_many_redirects")
