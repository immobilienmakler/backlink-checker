"""Capture the running app with an isolated SQLite database and example data."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app

URL = "http://127.0.0.1:18080"
SOURCES = (
    "https://magazin.example/ratgeber",
    "https://partner.example/empfehlungen",
    "https://forum.example/thema",
)


def seed(directory):
    app.DB = Path(directory) / "backlinks.sqlite3"
    app.initialize()
    with app.connect() as db:
        db.execute("INSERT INTO jobs VALUES (1,'https://beispiel.de/',0,'[]',0,NULL,'2026-11-21')")
        for run, date in [(1, "2026-11-20"), (2, "2026-11-21")]:
            db.execute("INSERT INTO runs VALUES (?,1,?,?)",
                       (run, date + " 09:00:00 UTC", date + " 09:00:04 UTC"))
        link = {"url": "https://beispiel.de/ratgeber", "anchor": "Zum Ratgeber", "rel": "nofollow"}
        demo = [
            (1, SOURCES[0], 200, "fehlt", "", []),
            (1, SOURCES[1], 200, "gefunden", "", [link]),
            (2, SOURCES[0], 200, "gefunden", "", [link]),
            (2, SOURCES[1], 200, "fehlt", "", []),
            (2, SOURCES[2], 403, "nicht prüfbar", "HTTP-Fehler oder kein HTML", []),
        ]
        for run, source, code, status, detail, links in demo:
            db.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?)",
                       (run, source, source, code, status, detail, json.dumps(links)))


def wait_for_server():
    for _ in range(50):
        try:
            with urlopen(URL + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except URLError:
            time.sleep(0.1)
    raise RuntimeError("Die Demo-App antwortet nicht")


def capture():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1120, "height": 760})
        page.goto(URL)
        page.get_by_label("Zieldomain oder Ziel-URL").fill("https://beispiel.de/")
        page.get_by_label("Wiederholung").select_option("24")
        page.get_by_label("Quellseiten").fill("\n".join(SOURCES))
        page.screenshot(path=ROOT / "docs/check.png")
        page.goto(URL + "/runs/2/report")
        page.get_by_role("heading", name="Prüfergebnis").wait_for()
        page.get_by_text("Hinzugekommen", exact=True).wait_for()
        page.get_by_text("Verloren", exact=True).wait_for()
        page.screenshot(path=ROOT / "docs/report.png")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        seed(directory)
        env = dict(os.environ, DATA_DIR=directory, HOST="127.0.0.1", PORT="18080", APP_PASSWORD="")
        with subprocess.Popen([sys.executable, str(ROOT / "app.py")], env=env) as server:
            try:
                wait_for_server()
                capture()
            finally:
                server.terminate()
