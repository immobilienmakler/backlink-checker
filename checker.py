"""Fetch public HTML pages and extract links without executing page scripts."""
import http.client
import ipaddress
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

MAX_BODY = 2 * 1024 * 1024
REDIRECTS = {301, 302, 303, 307, 308}
TLS = ssl.create_default_context()


def normalize(value):
    value = value.strip()
    if len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("Ungültige URL")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or "\\" in value
            or parts.port not in {None, 443 if parts.scheme == "https" else 80}):
        raise ValueError("Nur öffentliche HTTP(S)-URLs mit Standardport sind erlaubt")
    host = parts.hostname.rstrip(".").encode("idna").decode("ascii")
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))


def public_address(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if isinstance(ip, ipaddress.IPv6Address):
            ip = ip.ipv4_mapped or ip
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError("Interne und reservierte Adressen sind gesperrt")
    return addresses[0][4][0]


def fetch(url):
    for _ in range(6):
        url = normalize(url)
        parts = urlsplit(url)
        secure = parts.scheme == "https"
        port = 443 if secure else 80
        address = public_address(parts.hostname, port)
        connection = (http.client.HTTPSConnection(parts.hostname, timeout=6, context=TLS)
                      if secure else http.client.HTTPConnection(parts.hostname, timeout=6))
        # Connect to the checked address while retaining the hostname for TLS and Host.
        connection._create_connection = lambda *_args: socket.create_connection((address, port), 6)
        try:
            path = urlunsplit(("", "", parts.path, parts.query, ""))
            from urllib.parse import quote
            connection.request("GET", quote(path, safe="/%?&=;:+,@!$'()*[]~-"), headers={
                "User-Agent": "BacklinkChecker/1.0", "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity", "Connection": "close",
            })
            response = connection.getresponse()
            location = response.getheader("Location")
            if response.status in REDIRECTS and location:
                url = urljoin(url, location)
                continue
            content_type = response.headers.get_content_type()
            if response.status >= 400 or content_type not in {"text/html", "application/xhtml+xml"}:
                return url, response.status, None
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Unbekannte Inhaltskodierung")
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise ValueError("HTML überschreitet 2 MiB")
            return url, response.status, body.decode(response.headers.get_content_charset() or "utf-8", "replace")
        finally:
            connection.close()
    raise ValueError("Zu viele Weiterleitungen")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.base = None
        self.active = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base" and self.base is None and attrs.get("href"):
            self.base = attrs["href"]
        if tag in {"a", "area"} and attrs.get("href"):
            self.handle_endtag("a")
            self.active = [attrs["href"], (attrs.get("rel") or "").lower().split(), ""]
            if tag == "area":
                self.active[2] = (attrs.get("alt") or "")[:500]
                self.handle_endtag("a")
        if tag == "img" and self.active:
            self.handle_data(attrs.get("alt") or "")

    def handle_data(self, data):
        if self.active:
            self.active[2] = (self.active[2] + data)[:500]

    def handle_endtag(self, tag):
        if tag == "a" and self.active:
            self.links.append(self.active)
            self.active = None


def inspect(source, target, exact):
    try:
        final, status, html = fetch(source)
        if html is None:
            return final, status, "nicht prüfbar", "HTTP-Fehler oder kein HTML", []
        parser = Links()
        parser.feed(html)
        parser.close()
        parser.handle_endtag("a")
        base = urljoin(final, parser.base or "")
        domain = urlsplit(target).hostname
        found = []
        for href, rel, anchor in parser.links:
            try:
                dest = normalize(urldefrag(urljoin(base, href))[0])
            except (ValueError, UnicodeError):
                continue
            host = urlsplit(dest).hostname
            matches = dest == target if exact else host == domain or host.endswith("." + domain)
            if matches:
                found.append({"url": dest, "rel": " ".join(sorted(set(rel))), "anchor": " ".join(anchor.split())})
        return final, status, "gefunden" if found else "fehlt", "", found
    except (OSError, ValueError, LookupError, http.client.HTTPException) as exc:
        return source, None, "nicht prüfbar", str(exc)[:250], []
