"""URL validation and SSRF protection.

Threat model: the API fetches an arbitrary user-supplied URL from our server, so an attacker could
point it at internal services (127.0.0.1, 10.x, cloud metadata at 169.254.169.254, ...).
Defences implemented here:

1. Only http/https, no credentials in the URL, sane length, IDNA-normalised host.
2. Obvious internal host names / suffixes (localhost, *.internal, *.local ...) are refused.
3. The host is resolved *ourselves* and EVERY returned address must be a public (globally
   routable) IP. IPv4-mapped / 6to4 / Teredo / NAT64 IPv6 forms are unwrapped and checked too,
   which also neutralises decimal / octal / hex IP tricks such as http://2130706433/.
4. The fetcher re-runs these checks for every redirect hop (see website_extractor.SafeFetcher).

Known limitation: a DNS-rebinding attacker could change the DNS answer between our check and the
actual TCP connect. Mitigate in production with an egress firewall / separate network namespace.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from app.errors import AppError

MAX_URL_LENGTH = 2048
ALLOWED_PORTS = {80, 443, 8080, 8443}
BLOCKED_HOSTNAMES = {"localhost", "ip6-localhost", "ip6-loopback", "broadcasthost"}
BLOCKED_SUFFIXES = (
    ".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".corp",
    ".localdomain", ".home.arpa", ".private",
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _bad_url(message: str) -> AppError:
    return AppError(422, "invalid_url", message)


def _blocked(message: str = "This URL points to a private or reserved network address and cannot be analysed.") -> AppError:
    return AppError(400, "blocked_url", message)


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address hidden inside an IPv6 transition address, if any."""
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]  # (server, client) -> check the client side too below
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None and not is_public_ip(embedded):
            return False
        if ip.teredo and not is_public_ip(ip.teredo[0]):
            return False
    return bool(ip.is_global) and not ip.is_multicast


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def normalize_url(raw: str, *, allow_private: bool = False) -> str:
    """Validate + canonicalise a user-supplied URL. Raises AppError (422/400) when unusable.

    A missing scheme is tolerated (``example.com`` -> ``https://example.com``) because that is what
    people type; any *other* scheme (file:, ftp:, gopher:, javascript: ...) is rejected.
    """
    if raw is None or not str(raw).strip():
        raise _bad_url("URL must not be empty.")
    candidate = str(raw).strip()
    if len(candidate) > MAX_URL_LENGTH:
        raise _bad_url(f"URL is too long (max {MAX_URL_LENGTH} characters).")
    if any(ch.isspace() or ord(ch) < 32 for ch in candidate):
        raise _bad_url("URL must not contain spaces or control characters.")
    if "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")

    try:
        parts = urlsplit(candidate)
        port = parts.port  # raises ValueError for non-numeric / out-of-range ports
        host = parts.hostname
    except ValueError:
        raise _bad_url("URL is malformed.") from None

    if parts.scheme.lower() not in {"http", "https"}:
        raise _bad_url("Only http:// and https:// URLs are supported.")
    if not host:
        raise _bad_url("URL has no host name.")
    if "@" in parts.netloc:
        raise _bad_url("URLs containing credentials (user:password@host) are not allowed.")

    host = host.rstrip(".").lower()
    ip = _parse_ip(host)
    if ip is None:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise _bad_url("Host name is not valid.") from None
        if not allow_private:
            if "." not in host or host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
                raise _blocked("Internal or single-label host names cannot be analysed.")
        if len(host) > 253 or any(len(label) > 63 or not label for label in host.split(".")):
            raise _bad_url("Host name is not valid.")
    elif not allow_private and not is_public_ip(ip):
        raise _blocked()

    if not allow_private and port is not None and port not in ALLOWED_PORTS:
        raise _blocked(f"Port {port} is not allowed. Use a standard web port (80/443/8080/8443).")

    netloc = f"[{host}]" if ip is not None and ip.version == 6 else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


async def resolve_public_ips(host: str, port: int, *, allow_private: bool = False) -> list[str]:
    """Resolve ``host`` and make sure every address is public. Returns the address list."""
    literal = _parse_ip(host.strip("[]"))
    if literal is not None:
        if not allow_private and not is_public_ip(literal):
            raise _blocked()
        return [str(literal)]

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        if exc.errno in {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)}:
            raise AppError(422, "domain_not_found", f"The domain '{host}' could not be found.") from None
        raise AppError(502, "dns_error", f"DNS lookup failed for '{host}'.") from None

    addresses: list[str] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
        if not allow_private and not is_public_ip(ip):
            raise _blocked()
        addresses.append(str(ip))
    if not addresses:
        raise AppError(422, "domain_not_found", f"The domain '{host}' has no address records.")
    return addresses


def default_port(url: str) -> int:
    parts = urlsplit(url)
    return parts.port or (443 if parts.scheme == "https" else 80)
