"""Submit recently changed URLs to the fixed IndexNow endpoint."""
import http.client
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

from checker import TLS, normalize, public_address

BATCH_SIZE = 10_000
ACCEPTED = {200, 202}


def load_keys(value):
    raw = json.loads(value or "{}")
    if not isinstance(raw, dict):
        raise ValueError("INDEXNOW_KEYS erwartet ein JSON-Objekt.")
    keys = {}
    for host, key in raw.items():
        target = urlsplit(normalize("https://" + host))
        if (target.netloc != target.hostname or target.path != "/" or target.query
                or "." not in target.hostname or not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{8,128}", key)):
            raise ValueError("Ungültiger IndexNow-Hostname oder Schlüssel.")
        try:
            ipaddress.ip_address(target.hostname)
        except ValueError:
            keys[target.hostname] = key
        else:
            raise ValueError("IndexNow benötigt einen Hostnamen.")
    return keys


def verify_key(host, key):
    # Pin the checked public IP and retain the hostname for TLS validation.
    address = public_address(host, 443)
    with_connection = http.client.HTTPSConnection(host, timeout=8, context=TLS)
    with_connection._create_connection = lambda *_: socket.create_connection((address, 443), timeout=8)
    try:
        with_connection.request("GET", f"/{key}.txt", headers={"Accept-Encoding": "identity"})
        reply = with_connection.getresponse()
        return reply.status == 200 and reply.read(1024).decode("utf-8", errors="replace").strip() == key
    finally:
        with_connection.close()


def submit_batch(host, key, urls):
    if not 1 <= len(urls) <= BATCH_SIZE or any(urlsplit(url).hostname != host for url in urls):
        raise ValueError("IndexNow benötigt URLs desselben Hosts.")
    payload = json.dumps({
        "host": host, "key": key, "keyLocation": f"https://{host}/{key}.txt", "urlList": urls,
    }, ensure_ascii=True).encode("utf-8")
    connection = http.client.HTTPSConnection("api.indexnow.org", timeout=15, context=TLS)
    try:
        connection.request("POST", "/indexnow", body=payload, headers={"Content-Type": "application/json; charset=utf-8"})
        return connection.getresponse().status
    finally:
        connection.close()
