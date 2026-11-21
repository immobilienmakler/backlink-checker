import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import app
from checker import inspect, normalize, public_address


class CheckerTests(unittest.TestCase):
    def test_domain_and_exact_matching(self):
        html = """<base href="https://ziel.de/"><a href="artikel#abschnitt"
        rel="NOFOLLOW sponsored"><img alt="Titel"> mehr</a>
        <a href="https://blog.ziel.de/">Blog</a>
        <a href="https://ziel.de.evil.example/">Fremdseite</a>"""
        with patch("checker.fetch", return_value=("https://quelle.de/", 200, html)):
            self.assertEqual(len(inspect("https://quelle.de/", "https://ziel.de/", False)[4]), 2)
            exact = inspect("https://quelle.de/", "https://ziel.de/artikel", True)
            self.assertEqual(exact[4], [{"url": "https://ziel.de/artikel", "rel": "nofollow sponsored",
                                        "anchor": "Titel mehr"}])

    def test_missing_and_unavailable(self):
        with patch("checker.fetch", return_value=("https://quelle.de/", 200, "<p>Text</p>")):
            self.assertEqual(inspect("x", "https://ziel.de/", False)[2], "fehlt")
        with patch("checker.fetch", return_value=("https://quelle.de/", 403, None)):
            self.assertEqual(inspect("x", "https://ziel.de/", False)[2], "nicht prüfbar")

    def test_url_and_local_addresses(self):
        self.assertEqual(normalize("BÜCHER.de./text#abschnitt"), "https://xn--bcher-kva.de/text")
        for url in ["file:///etc/passwd", "https://user@host.de", "http://host.de:443"]:
            with self.assertRaises(ValueError):
                normalize(url)
        for host in ["127.0.0.1", "169.254.169.254", "::1", "::ffff:192.168.0.1"]:
            with self.assertRaises(ValueError):
                public_address(host, 80)

    def test_recurring_jobs_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DB", Path(directory) / "test.db"):
            app.initialize()
            with app.connect() as db:
                db.execute("INSERT INTO jobs VALUES (1,'https://ziel.de/',0,'[\"https://quelle.de/\"]',24,0,'now')")
            with patch.object(app, "inspect", return_value=("https://quelle.de/", 200, "fehlt", "", [])):
                self.assertTrue(app.execute_next())
            with app.connect() as db:
                self.assertEqual(db.execute("SELECT status FROM results").fetchone()[0], "fehlt")
                self.assertEqual(db.execute("SELECT next_run > unixepoch() FROM jobs").fetchone()[0], 1)

    def test_csv_escaping(self):
        self.assertEqual(app.safe_csv(" =1"), "' =1")
        self.assertEqual(app.safe_csv("normal"), "normal")

    def test_domain_overview_and_batch_are_scoped_to_existing_jobs(self):
        class Request:
            def __init__(self, wire):
                self.input = io.BytesIO(wire)
                self.output = bytearray()

            def makefile(self, *_):
                return self.input

            def sendall(self, data):
                self.output.extend(data)

        def request(method, path, form=None):
            body = urlencode(form or {}).encode()
            connection = Request(
                f"{method} {path} HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
            )
            app.Handler(connection, ("127.0.0.1", 1234), None)
            return bytes(connection.output).decode()

        with tempfile.TemporaryDirectory() as directory, patch.multiple(
            app, DB=Path(directory) / "test.db", PASSWORD=""
        ):
            app.initialize()
            with app.connect() as db:
                db.executemany("INSERT INTO jobs VALUES (?, ?, 0, '[]', 0, NULL, 'now')", [
                    (1, "https://www.ziel.de/seite"),
                    (2, "https://ziel.de/"),
                    (3, "https://andere.de/"),
                    (4, "https://blog.ziel.de/"),
                ])
                db.executemany("INSERT INTO runs VALUES (?, 1, 'now', ?)", [(1, "old"), (2, "done"), (3, None)])
                db.execute("INSERT INTO results VALUES (1,'source','source',200,'fehlt','','[]')")
                db.execute("INSERT INTO results VALUES (2,'source','source',200,'gefunden','','[]')")
                db.execute("INSERT INTO results VALUES (3,'source','source',200,'fehlt','','[]')")
                job = app.domain_jobs(db)["ziel.de"][1]
                self.assertEqual((job.total, job.found, job.missing, job.gained, job.active), (1, 1, 0, 1, 3))
            dashboard = request("GET", "/")
            self.assertIn('/domains/andere.de', dashboard)
            self.assertIn('/runs/2/report', dashboard)
            filtered = request("GET", "/?q=ANDERE&status=unverified")
            self.assertIn('/domains/andere.de', filtered)
            self.assertNotIn('/domains/ziel.de', filtered)
            self.assertIn("Keine Domain entspricht dem Filter.", request("GET", "/?status=lost"))
            detail = request("GET", "/domains/ziel.de")
            self.assertIn('/jobs/2', detail)
            self.assertNotIn('/jobs/3', detail)
            self.assertIn("403", request("POST", "/domains/ziel.de", {"csrf": "invalid"}))
            self.assertIn("Location: /domains/ziel.de", request("POST", "/domains/ziel.de", {"csrf": app.TOKEN}))
            with app.connect() as db:
                self.assertEqual(db.execute("SELECT id FROM jobs WHERE next_run IS NOT NULL").fetchall(), [(2,)])
