import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
