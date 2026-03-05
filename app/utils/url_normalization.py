from __future__ import annotations
import re
from typing import Union
from urllib.parse import (
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
    quote,
    unquote,
)
from pydantic import HttpUrl
# Characters that are safe to leave un-percent-encoded in each component
_PATH_SAFE = "/:@!$&'()*+,;=-._~"
_QUERY_SAFE = "/:@!$&'()*+,;=?-._~"

# Default ports for common schemes
_DEFAULT_PORTS: dict[str, str] = {
    "http": "80",
    "https": "443",
    "ftp": "21",
    "ftps": "990",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _normalize_scheme(scheme: str) -> str:
    return scheme.lower()


def _strip_www(host: str) -> str:
    """
    Strip a bare 'www.' prefix for canonical deduplication.

    Rules:
    - Only strip if the prefix is exactly 'www.' (case-insensitive)
    - Never strip if the remainder would be empty (e.g. bare 'www' domain)
    - Never strip numbered variants like 'www2.', 'www3.' etc.
    - Never strip if host is an IP address (v4 or v6 literal)

    Examples:
        www.youtube.com   → youtube.com
        www.example.co.uk → example.co.uk
        www2.example.com  → www2.example.com  (unchanged)
        www               → www               (unchanged – no remainder)
        192.168.1.1       → 192.168.1.1       (unchanged – IP)
        [::1]             → [::1]             (unchanged – IPv6)
    """
    # IPv6 literal – leave alone
    if host.startswith("["):
        return host

    # IPv4 literal – leave alone
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return host

    # Only strip bare 'www.' not 'www2.', 'wwww.', etc.
    lower = host.lower()
    if lower.startswith("www."):
        remainder = host[4:]   # preserve original casing of the rest
        if remainder:          # don't reduce 'www.' → ''
            return remainder

    return host


def _normalize_netloc(scheme: str, netloc: str, strip_www: bool = True) -> str:
    """
    Normalise the authority component.

    * Lowercase host
    * Strip trailing dot from host (RFC 3986 §3.2.2)
    * Optionally strip 'www.' prefix
    * Remove default ports for the given scheme
    * Preserve userinfo unchanged
    """
    if not netloc:
        return netloc

    # ── Split off userinfo ──────────────────────────────────────────────────
    userinfo = ""
    hostport = netloc
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)

    # ── Split host / port (IPv6-safe) ────────────────────────────────────────
    ipv6_match = re.fullmatch(r"(\[.*?\])(?::(\d+))?", hostport)
    if ipv6_match:
        host = ipv6_match.group(1).lower()
        port = ipv6_match.group(2) or ""
    else:
        host = hostport
        port = ""
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)

    # Lowercase, strip trailing dot
    host = host.lower().rstrip(".")

    # Optionally strip www. prefix
    if strip_www:
        host = _strip_www(host)

    # Drop default port
    if port and port == _DEFAULT_PORTS.get(scheme, ""):
        port = ""

    hostport = f"{host}:{port}" if port else host
    return f"{userinfo}@{hostport}" if userinfo else hostport


def _normalize_path(path: str) -> str:
    """
    Normalise the path component.

    * Ensure a leading slash is always present
    * Collapse consecutive slashes
    * Resolve '.' and '..' segments (RFC 3986 §5.2.4)
    * Preserve a trailing slash only for non-root paths that originally had one
    * Normalise percent-encoding
    """
    if not path:
        return "/"

    path = _normalise_pct_encoding(path, safe=_PATH_SAFE)

    had_trailing_slash = path.endswith("/") and path != "/"

    segments = path.split("/")
    stack: list[str] = []
    for seg in segments:
        if seg in ("", "."):
            continue
        if seg == "..":
            if stack:
                stack.pop()
        else:
            stack.append(seg)

    normalized = "/" + "/".join(stack) if stack else "/"

    if had_trailing_slash and not normalized.endswith("/"):
        normalized += "/"

    return normalized


def _normalize_query(query: str) -> str:
    """
    Normalise the query string.

    * Parse into (key, value) pairs, preserving blank values
    * Sort pairs by key then value
    * Normalise percent-encoding in each key/value
    * Re-encode to a stable form
    """
    if not query:
        return ""

    params = parse_qsl(query, keep_blank_values=True)
    params = [
        (
            _normalise_pct_encoding(k, safe=""),
            _normalise_pct_encoding(v, safe=""),
        )
        for k, v in params
    ]
    params.sort()
    return urlencode(params, doseq=True)


def _normalise_pct_encoding(value: str, safe: str = "") -> str:
    """
    Decode percent-encoded sequences for unreserved characters
    (RFC 3986 §2.3: ALPHA / DIGIT / '-' / '.' / '_' / '~') and
    upper-case the hex digits for all remaining encoded sequences.
    """
    decoded = unquote(value)
    return quote(decoded, safe=safe)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_url(url: Union[str, "HttpUrl"], *, strip_www: bool = True) -> str:
    """
    Return the canonical form of *url*.

    Parameters
    ----------
    url:
        The URL to normalise. Accepts plain strings or Pydantic HttpUrl objects.
    strip_www:
        If True (default), strip a bare 'www.' prefix from the host so that
        ``https://www.youtube.com/`` and ``https://youtube.com/`` map to the
        same canonical key.  Set to False to preserve 'www.' as-is.

    Transformations applied (all semantics-preserving by default):
    - Strip surrounding whitespace
    - Lowercase scheme and host
    - Remove trailing dot from host
    - Strip 'www.' prefix (configurable)
    - Remove default ports (80 ↔ http, 443 ↔ https, 21 ↔ ftp, …)
    - Normalise path (leading slash, collapse '//', resolve '.'/'..',
      preserve non-root trailing slash)
    - Sort and normalise query parameters
    - Normalise percent-encoding (upper-case hex; decode unreserved chars)
    - Drop the fragment
    """
    if HttpUrl is not None and isinstance(url, HttpUrl):
        url_str = str(url)
    else:
        url_str = str(url)

    url_str = url_str.strip()

    split = urlsplit(url_str)

    scheme = _normalize_scheme(split.scheme)
    netloc = _normalize_netloc(scheme, split.netloc, strip_www=strip_www)
    path = _normalize_path(split.path)
    query = _normalize_query(split.query)
    fragment = ""

    return urlunsplit((scheme, netloc, path, query, fragment))