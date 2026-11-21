import io
import json
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
            self.assertIn("Prüfung pausieren", request("GET", "/runs/3/report"))
            self.assertIn("Location: /runs/3/report", request("POST", "/runs/3/control",
                          {"action": "pause", "csrf": app.TOKEN}))
            self.assertIn("Prüfung fortsetzen", request("GET", "/runs/3/report"))
            self.assertIn("1 pausiert", request("GET", "/"))
            request("POST", "/runs/3/control", {"action": "resume", "csrf": app.TOKEN})
            filtered = request("GET", "/?q=ANDERE&status=unverified")
            self.assertIn('/domains/andere.de', filtered)
            self.assertNotIn('/domains/ziel.de', filtered)
            self.assertIn("Keine Domain entspricht dem Filter.", request("GET", "/?status=lost"))
            self.assertIn("Quelle", request("GET", "/runs/2/report"))
            self.assertIn("\"status\": \"gefunden\"", request("GET", "/runs/2/json"))
            self.assertIn("quelle;effektive_quelle", request("GET", "/runs/2/csv"))
            self.assertIn("Hinzugekommen", request("GET", "/runs/2/changes?change=new"))
            self.assertIn("Keine Ergebnisse für diese Auswahl", request("GET", "/runs/2/report?change=lost"))
            self.assertIn("Quellen verwalten", request("GET", "/jobs/2/sources"))
            response = request("POST", "/jobs/2", {"action": "append", "sources": "quelle.example", "csrf": app.TOKEN})
            self.assertIn("Location: /jobs/2/sources?updated=", response)
            detail = request("GET", "/domains/ziel.de")
            self.assertIn('/jobs/2', detail)
            self.assertNotIn('/jobs/3', detail)
            self.assertIn("403", request("POST", "/domains/ziel.de", {"csrf": "invalid"}))
            self.assertIn("Location: /domains/ziel.de", request("POST", "/domains/ziel.de", {"csrf": app.TOKEN}))
            with app.connect() as db:
                self.assertEqual(db.execute("SELECT id FROM jobs WHERE next_run IS NOT NULL").fetchall(), [(2,)])

    def test_bulk_upload_and_resume(self):
        from types import SimpleNamespace

        count = 25_000
        entries = [f"https://quelle-{i % 30}.example/{i}" for i in range(count)]
        csv_data = "Referring Page URL;Titel\r\n" + "\r\n".join(
            f'{source};"Eintrag; {i}"' for i, source in enumerate(entries)
        ) + f"\r\n{entries[0]};Duplikat"
        boundary = "backlinks-test"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="target"\r\n\r\nziel.de\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="source_file"; filename="links.csv"\r\n'
            f'Content-Type: text/csv\r\n\r\n{csv_data}\r\n--{boundary}--\r\n'
        ).encode("utf-8")
        handler = SimpleNamespace(headers={"Content-Length": str(len(body)),
                                           "Content-Type": f"multipart/form-data; boundary={boundary}"},
                                  rfile=io.BytesIO(body))
        job = app.clean_form(app.read_form(handler))

        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DB", Path(directory) / "test.db"):
            app.initialize()
            with app.connect() as db:
                db.execute("INSERT INTO jobs VALUES (1,?,?,?,?,NULL,'now')", job)
                db.execute("INSERT INTO runs VALUES (1,1,'now',NULL)")
                db.execute("INSERT INTO results VALUES (1,?,?,200,'fehlt','','[]')", (entries[0], entries[0]))
            app.initialize()  # Simulate a container restart during the run.
            def simulated(source, _target, _exact):
                return source, 200, "fehlt", "", []

            with patch.object(app, "inspect", new=simulated):
                self.assertTrue(app.execute_next())
            with app.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*), COUNT(DISTINCT source) FROM results").fetchone(),
                                 (count, count))
                self.assertEqual(db.execute("SELECT id FROM runs WHERE finished IS NOT NULL").fetchall(), [(1,)])
                self.assertEqual(app.domain_jobs(db)["ziel.de"][0].missing, count)

    def test_file_and_text_deduplicate_and_enforce_limit(self):
        import json
        form = {"target": ["ziel.de"], "sources": ["https://quelle.example/"],
                "source_file": ["URL,Title\nhttps://quelle.example/#fragment,Duplicate\nhttps://other.example/,Other"]}
        self.assertEqual(json.loads(app.clean_form(form)[2]), ["https://quelle.example/", "https://other.example/"])
        with patch.object(app, "MAX_SOURCES", 1), self.assertRaisesRegex(ValueError, "Maximal"):
            app.clean_form(form)

    def test_targeted_retry_keeps_healthy_results_and_detects_link_edits(self):
        sources = [f"https://quelle.example/{i}" for i in range(5)]
        stable = {"url": "https://ziel.de/", "anchor": "Alt", "rel": ""}
        edited = dict(stable, anchor="Neu")
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DB", Path(directory) / "test.db"):
            app.initialize()
            with app.connect() as db:
                db.execute("INSERT INTO jobs VALUES (1,'https://ziel.de/',0,?,24,unixepoch()+3600,'now')",
                           (json.dumps(sources[:4]),))
                db.executemany("INSERT INTO runs VALUES (?,1,'now','done')", [(1,), (2,)])
                for run, source, status, links in [
                    (1, sources[0], "gefunden", [stable]), (2, sources[0], "fehlt", []),
                    (2, sources[1], "gefunden", [stable]), (2, sources[2], "nicht prüfbar", []),
                    (2, sources[3], "gefunden", [stable]),
                ]:
                    db.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?)",
                               (run, source, source, 200, status, "", json.dumps(links)))
                removed = app.update_sources(db, 1, sources[:4], {"source": [sources[3]]}, "remove")
                self.assertIn("1 Quelle", removed)
                current = json.loads(db.execute("SELECT sources FROM jobs").fetchone()[0])
                app.update_sources(db, 1, current, {"sources": ["\n".join([sources[1], sources[4]])]}, "append")
                db.execute("INSERT INTO retries VALUES (1,'attention')")
                schedule = db.execute("SELECT next_run FROM jobs").fetchone()[0]

            checked = set()

            def inspect(source, *_):
                checked.add(source)
                return source, 200, "gefunden", "", [edited]

            with patch.object(app, "inspect", new=inspect):
                self.assertTrue(app.execute_next())
            self.assertEqual(checked, {sources[0], sources[2], sources[4]})
            with app.connect() as db:
                db.create_function("link_signature", 1, app.link_signature)
                parameters = {"job": 1, "run": 3}
                self.assertEqual(db.execute(app.CHANGE_ROWS + "SELECT change_kind FROM comparison WHERE change_kind<>''",
                                            parameters).fetchall(), [("recovered",)])
                self.assertEqual(db.execute("SELECT next_run FROM jobs").fetchone()[0], schedule)
                self.assertEqual(db.execute("SELECT inherited FROM run_plans WHERE run_id=3").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM results WHERE run_id=2").fetchone()[0], 4)
                db.execute("INSERT INTO runs VALUES (4,1,'now','done')")
                db.execute("INSERT INTO results VALUES (4,?,?,200,'gefunden','',?)",
                           (sources[1], sources[1], json.dumps([edited])))
                parameters["run"] = 4
                self.assertEqual(db.execute(app.CHANGE_ROWS + "SELECT change_kind FROM comparison",
                                            parameters).fetchone()[0], "edited")

    def test_dedup_keeps_equivalent_checks_and_preserves_history(self):
        source = "https://quelle.example/"
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DB", Path(directory) / "test.db"):
            app.initialize()
            with app.connect() as db:
                db.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,1,'now')", [
                    (1, "https://ziel.de/", 0, json.dumps([source]), 24),
                    (2, "http://ziel.de/pfad", 0, json.dumps([source]), 1),
                    (3, "https://www.ziel.de/", 0, json.dumps([source]), 1),
                    (4, "https://ziel.de/", 1, json.dumps([source]), 1),
                    (5, "https://andere.de/", 0, json.dumps([source]), 1),
                ])
                db.execute("INSERT INTO runs VALUES (1,1,'now','done')")
                db.execute("INSERT INTO results VALUES (1,?,?,200,'gefunden','','[]')", (source, source))
                changes = app.duplicate_preview(db, "ziel.de")
                self.assertEqual([(job, duplicates) for job, _, duplicates, _ in changes], [(1, 1)])
                app.clean_duplicates(db, changes)
                self.assertEqual(app.duplicate_preview(db, "ziel.de"), [])
                self.assertEqual(db.execute("SELECT sources,next_run FROM jobs WHERE id=1").fetchone(), ("[]", None))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE json_array_length(sources)=1").fetchone(), (4,))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM results").fetchone(), (1,))
                app.queue_jobs(db, [1])
                self.assertIsNone(db.execute("SELECT next_run FROM jobs WHERE id=1").fetchone()[0])

    def test_worker_switches_between_jobs_and_leaves_paused_run_intact(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DB", Path(directory) / "test.db"):
            app.initialize()
            sources = [f"https://large.example/{i}" for i in range(5)]
            with app.connect() as db:
                db.executemany("INSERT INTO jobs VALUES (?,?,0,?,0,0,'now')", [
                    (1, "https://gross.de/", json.dumps(sources)),
                    (2, "https://klein.de/", json.dumps(["https://small.example/"])),
                ])

            def inspect(source, *_):
                return source, 200, "fehlt", "", []

            with patch.object(app, "inspect", new=inspect):
                self.assertTrue(app.execute_next(2))
                self.assertTrue(app.execute_next(2))
                with app.connect() as db:
                    self.assertEqual(db.execute("SELECT job_id FROM runs WHERE finished IS NOT NULL").fetchall(), [(2,)])
                    db.execute("UPDATE run_controls SET paused=1 WHERE run_id=1")
                app.initialize()
                self.assertFalse(app.execute_next(2))
                with app.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM results WHERE run_id=1").fetchone()[0], 2)
                    db.execute("UPDATE run_controls SET paused=0 WHERE run_id=1")
                self.assertTrue(app.execute_next(2))
                self.assertTrue(app.execute_next(2))
            with app.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(DISTINCT source) FROM results WHERE run_id=1").fetchone()[0], 5)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM runs WHERE finished IS NOT NULL").fetchone()[0], 2)
