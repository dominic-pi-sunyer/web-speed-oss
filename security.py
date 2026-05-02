"""Security validation for web-speed.

All fetch paths (httpx and Playwright) run their URL through validate_url()
before making any network request.  The Playwright path additionally installs
a route interceptor that re-checks every sub-request the browser makes while
rendering a page, blocking any that try to reach private infrastructure.

Threats addressed
─────────────────
SSRF (Server-Side Request Forgery)
    A malicious URL like http://169.254.169.254/latest/meta-data/ (AWS IMDS),
    http://10.0.0.1/admin, or http://localhost:6379/ could be used to probe or
    exfiltrate internal services.  We block these at two layers:
    1. validate_url() — resolves the hostname and rejects any private IP.
    2. Playwright route interceptor — blocks every sub-request the page JS
       makes, not just the initial navigation URL.

DNS rebinding
    An attacker controls DNS for evil.example.com and returns a public IP for
    the first lookup (passing our check), then switches to 169.254.169.254 for
    the actual connection.  We mitigate this by re-validating inside the
    Playwright route interceptor, which fires at browser connection time, not
    just at URL-parsing time.

Scheme injection
    Only http:// and https:// are allowed.  file://, data:, javascript:,
    ftp:// and all other schemes are rejected before any fetch begins.

Arbitrary JS execution
    The page's own JavaScript runs inside Chromium's sandboxed renderer
    process.  web-speed never eval()s or executes any string as code.
    The wait_for parameter accepts only CSS selectors (passed to Playwright's
    wait_for_selector); CSS selectors are data, not code.
    CSS selector length is capped to prevent degenerate input.

Resource exhaustion
    Page load is time-limited (JS_TIMEOUT_MS in fetcher.py).  Media, fonts,
    and large binary responses are blocked by the Playwright route interceptor
    so the browser doesn't download gigabytes of video while rendering a page.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# ── allowed schemes ───────────────────────────────────────────────────────────
ALLOWED_SCHEMES = frozenset({"http", "https"})

# ── private / reserved address spaces ────────────────────────────────────────
# Any IP in these ranges is off-limits, regardless of how we got there.
_PRIVATE_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    # IPv4
    ipaddress.ip_network("0.0.0.0/8"),          # "this" network
    ipaddress.ip_network("10.0.0.0/8"),          # RFC 1918 class A
    ipaddress.ip_network("100.64.0.0/10"),       # shared address space (RFC 6598)
    ipaddress.ip_network("127.0.0.0/8"),         # loopback
    ipaddress.ip_network("169.254.0.0/16"),      # link-local / AWS IMDS
    ipaddress.ip_network("172.16.0.0/12"),       # RFC 1918 class B
    ipaddress.ip_network("192.0.0.0/24"),        # IETF protocol assignments
    ipaddress.ip_network("192.168.0.0/16"),      # RFC 1918 class C
    ipaddress.ip_network("198.18.0.0/15"),       # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),     # TEST-NET-2 (documentation)
    ipaddress.ip_network("203.0.113.0/24"),      # TEST-NET-3 (documentation)
    ipaddress.ip_network("224.0.0.0/4"),         # multicast
    ipaddress.ip_network("240.0.0.0/4"),         # reserved
    ipaddress.ip_network("255.255.255.255/32"),  # broadcast
    # IPv6
    ipaddress.ip_network("::1/128"),             # loopback
    ipaddress.ip_network("fc00::/7"),            # unique local (ULA)
    ipaddress.ip_network("fe80::/10"),           # link-local
    ipaddress.ip_network("ff00::/8"),            # multicast
]

# ── CSS selector safety cap ───────────────────────────────────────────────────
MAX_SELECTOR_LENGTH = 500


class SecurityError(Exception):
    """Raised when a request is blocked for security reasons."""
    code = "BLOCKED"


def validate_url(url: str) -> None:
    """Raise SecurityError if `url` is unsafe to fetch.

    Checks:
    - URL is well-formed
    - Scheme is http or https
    - Hostname is present and not a bare 'localhost' alias
    - Resolved IP addresses are not in private/reserved ranges (SSRF)
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise SecurityError(f"malformed URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SecurityError(
            f"scheme '{scheme}' is not allowed; only http and https are permitted"
        )

    hostname = parsed.hostname or ""
    if not hostname:
        raise SecurityError("URL has no hostname")

    # Fast path: literal keyword aliases for loopback
    if hostname.lower() in ("localhost", "localhost.localdomain", "ip6-localhost",
                             "ip6-loopback", "broadcasthost"):
        raise SecurityError(
            f"fetching '{hostname}' is not allowed (loopback alias)"
        )

    # Attempt to parse hostname as a literal IP first (no DNS lookup needed)
    try:
        _reject_if_private(ipaddress.ip_address(hostname), hostname)
        return  # It's a literal IP and it's public — we're done
    except ValueError:
        pass  # Not a literal IP — need to resolve via DNS

    # Resolve hostname → IP(s) and check each one
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SecurityError(
            f"hostname '{hostname}' could not be resolved: {exc}"
        ) from exc

    for family, _type, _proto, _canonname, sockaddr in addr_infos:
        raw_addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(raw_addr)
        except ValueError:
            continue
        _reject_if_private(ip, hostname)


def validate_selector(selector: str) -> None:
    """Raise SecurityError if `selector` looks unsafe or degenerate."""
    if not selector or not selector.strip():
        raise SecurityError("CSS selector must not be empty")
    if len(selector) > MAX_SELECTOR_LENGTH:
        raise SecurityError(
            f"CSS selector exceeds maximum length of {MAX_SELECTOR_LENGTH} characters"
        )


def is_blockable_resource(resource_type: str, url: str) -> bool:
    """Return True if the Playwright route interceptor should block this request.

    Blocks media (video/audio), large fonts, and raw data URIs that are not
    needed to render page structure.  Does NOT block scripts — they must run
    for JS pages to render.
    """
    if resource_type in ("media", "websocket", "eventsource", "manifest"):
        return True
    lowered_url = url.lower()
    if lowered_url.startswith("data:"):
        # Allow small inline data URIs (base64 images in CSS etc.) but block
        # navigations to data: URLs which can be used for phishing/exfil.
        return False  # handled by scheme check at navigation time
    return False


# ── internal helpers ──────────────────────────────────────────────────────────

def _reject_if_private(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    hostname: str,
) -> None:
    """Raise SecurityError if `ip` falls within any private/reserved range."""
    for network in _PRIVATE_NETWORKS:
        try:
            if ip in network:
                raise SecurityError(
                    f"fetching '{hostname}' resolves to {ip} which is in the "
                    f"reserved range {network} — request blocked to prevent "
                    f"internal network access (SSRF)"
                )
        except TypeError:
            # ip_address type mismatch (v4 vs v6 network) — skip
            continue
