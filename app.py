"""Single-user backlink monitor with a persistent SQLite history."""
import csv
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from checker import inspect, normalize

DB = Path(os.environ.get("DATA_DIR", "./data")) / "backlinks.sqlite3"
PASSWORD = os.environ.get("APP_PASSWORD", "")
TOKEN = secrets.token_urlsafe(32)
MAX_SOURCES = 250


def connect():
    return sqlite3.connect(DB, timeout=15)


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as db, db:
        db.executescript("""
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY, target TEXT NOT NULL, exact INTEGER NOT NULL,
          sources TEXT NOT NULL, interval_hours INTEGER NOT NULL, next_run INTEGER,
          created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, started TEXT NOT NULL,
          finished TEXT
        );
        CREATE TABLE IF NOT EXISTS results (
          run_id INTEGER NOT NULL, source TEXT NOT NULL, final TEXT NOT NULL,
          http INTEGER, status TEXT NOT NULL, detail TEXT NOT NULL, links TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS result_run ON results(run_id);
        CREATE INDEX IF NOT EXISTS run_job ON runs(job_id);
        """)
        db.execute("""UPDATE jobs SET next_run=unixepoch() WHERE id IN
                   (SELECT job_id FROM runs WHERE finished IS NULL)""")
        db.execute("DELETE FROM results WHERE run_id IN (SELECT id FROM runs WHERE finished IS NULL)")
        db.execute("DELETE FROM runs WHERE finished IS NULL")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def execute_next():
    with closing(connect()) as db, db:
        job = db.execute("""SELECT id,target,exact,sources,interval_hours FROM jobs
                            WHERE next_run <= unixepoch() ORDER BY next_run,id LIMIT 1""").fetchone()
        if job is None:
            return False
        job_id, target, exact, sources, interval = job
        run_id = db.execute("INSERT INTO runs(job_id,started) VALUES (?,?)", (job_id, now())).lastrowid
        db.execute("UPDATE jobs SET next_run=NULL WHERE id=?", (job_id,))
    for source in json.loads(sources):
        final, http, status, detail, links = inspect(source, target, exact)
        with closing(connect()) as db, db:
            db.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?)",
                       (run_id, source, final, http, status, detail, json.dumps(links, ensure_ascii=False)))
    with closing(connect()) as db, db:
        db.execute("UPDATE runs SET finished=? WHERE id=?", (now(), run_id))
        if interval:
            db.execute("""UPDATE jobs SET next_run=COALESCE(next_run,unixepoch()+?)
                          WHERE id=?""", (interval * 3600, job_id))
    return True


def worker():
    event = threading.Event()
    while True:
        if not execute_next():
            event.wait(2)


def clean_form(form):
    target = normalize(form.get("target", [""])[0])
    raw = form.get("sources", [""])[0]
    sources = list(dict.fromkeys(normalize(line) for line in raw.splitlines() if line.strip()))
    interval = int(form.get("interval", ["0"])[0])
    if not 1 <= len(sources) <= MAX_SOURCES or interval not in {0, 1, 24, 168}:
        raise ValueError("Bitte 1 bis 250 Quell-URLs und ein gültiges Prüfintervall angeben.")
    return target, int(form.get("match") == ["exact"]), json.dumps(sources), interval


def safe_csv(value):
    value = "" if value is None else str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def page(title, content):
    return f"""<!doctype html><html lang="de"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(title)} · Backlink-Checker</title><link rel="stylesheet" href="/style.css">
    <header><nav><a href="/">Backlink-Checker</a></nav></header>
    <main><h1>{escape(title)}</h1>{content}</main></html>"""


def create_form(target="", sources="", interval="0", exact=False):
    options = "".join(
        f'<option value="{hours}" {"selected" if str(hours) == interval else ""}>{label}</option>'
        for hours, label in [(0, "Einmalig"), (1, "Stündlich"), (24, "Täglich"), (168, "Wöchentlich")]
    )
    return f"""<form method="post" action="/jobs">
    <input type="hidden" name="csrf" value="{TOKEN}">
    <label>Zieldomain oder Ziel-URL<input name="target" value="{escape(target)}" placeholder="https://beispiel.de" required></label>
    <div class="pair"><label>Abgleich<select name="match">
    <option value="domain">Domain inklusive Subdomains</option>
    <option value="exact" {"selected" if exact else ""}>Exakte Ziel-URL</option></select></label>
    <label>Wiederholung<select name="interval">{options}</select></label></div>
    <label>Quellseiten<textarea name="sources" rows="6" placeholder="Eine URL pro Zeile" required>{escape(sources)}</textarea></label>
    <button>Prüfung starten</button></form>"""


class Handler(BaseHTTPRequestHandler):
    def send_body(self, body, status=200, mime="text/html; charset=utf-8", headers=()):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Cache-Control", "no-store")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if not PASSWORD:
            return True
        import base64
        from binascii import Error
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, supplied = base64.b64decode(auth[6:], validate=True).partition(b":")
                if hmac.compare_digest(supplied, PASSWORD.encode()):
                    return True
            except (ValueError, Error):
                pass
        self.send_body("Anmeldung erforderlich", 401, "text/plain; charset=utf-8",
                       [("WWW-Authenticate", 'Basic realm="Backlink-Checker", charset="UTF-8"')])
        return False

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_body("ok", mime="text/plain")
            return
        if not self.authorized():
            return
        if path == "/style.css":
            self.send_body(Path(__file__).with_name("style.css").read_text(), mime="text/css; charset=utf-8")
        elif path == "/":
            self.dashboard()
        else:
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "jobs" and parts[1].isascii() and parts[1].isdigit() and len(parts[1]) < 19:
                self.job(int(parts[1]))
            elif len(parts) == 3 and parts[0] == "runs" and parts[1].isascii() and parts[1].isdigit() and len(parts[1]) < 19:
                self.run_report(int(parts[1]), parts[2])
            else:
                self.send_body(page("Seite nicht gefunden", ""), 404)

    def dashboard(self):
        with closing(connect()) as db:
            jobs = db.execute("SELECT id,target,created FROM jobs ORDER BY id DESC").fetchall()
        rows = "".join(f'<li><a href="/jobs/{i}">{escape(target)}</a><small>{created}</small></li>'
                       for i, target, created in jobs)
        self.send_body(page("Backlinks prüfen", "<p>Überprüfe bekannte Quellseiten und verfolge ihre Links im Zeitverlauf.</p>"
                            + create_form() + "<h2>Prüfaufträge</h2>"
                            + (f'<ul class="entries">{rows}</ul>' if rows else "<p>Noch keine Prüfaufträge.</p>")))

    def job(self, job_id):
        with closing(connect()) as db:
            job = db.execute("SELECT target,interval_hours,next_run FROM jobs WHERE id=?", (job_id,)).fetchone()
            runs = db.execute("""SELECT r.id,r.started,r.finished,
                SUM(CASE WHEN s.status='gefunden' THEN 1 ELSE 0 END),COUNT(s.source)
                FROM runs r LEFT JOIN results s ON s.run_id=r.id WHERE r.job_id=?
                GROUP BY r.id ORDER BY r.id DESC""", (job_id,)).fetchall()
        if job is None:
            self.send_body(page("Prüfauftrag nicht gefunden", ""), 404)
            return
        def action(value, label):
            return f'<button name="action" value="{value}">{label}</button>'
        controls = action("run", "Erneut prüfen")
        if job[1]:
            controls += action("stop", "Automatik stoppen")
        rows = "".join(f'<li><a href="/runs/{i}/report">{started}</a>'
                       f'<small>{found}/{total} gefunden · {"abgeschlossen" if finished else "läuft"}</small></li>'
                       for i, started, finished, found, total in runs)
        schedule = datetime.fromtimestamp(job[2], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if job[2] else ""
        suffix = f" · Nächste Prüfung {schedule}" if schedule else ""
        self.send_body(page(job[0], f"""<p>{"Intervall " + str(job[1]) + " h" if job[1] else "Manuelle Prüfung"}{suffix}</p>
        <form class="actions" method="post" action="/jobs/{job_id}">
        <input type="hidden" name="csrf" value="{TOKEN}">{controls}</form>
        <h2>Verlauf</h2><ul class="entries">{rows or "<li>Prüfung wartet auf Ausführung.</li>"}</ul>"""))

    def run_report(self, run_id, mode):
        with closing(connect()) as db:
            run = db.execute("SELECT job_id, started, finished FROM runs WHERE id=?", (run_id,)).fetchone()
            rows = db.execute("SELECT source,final,http,status,detail,links FROM results WHERE run_id=? ORDER BY rowid",
                              (run_id,)).fetchall()
        if not run or mode not in {"report", "csv", "json"}:
            self.send_body(page("Prüflauf nicht gefunden", ""), 404)
            return
        fields = ("quelle", "effektive_quelle", "http_status", "status", "detail", "links")
        if mode == "json":
            payload = [dict(zip(fields, (*row[:-1], json.loads(row[-1])))) for row in rows]
            self.send_body(json.dumps(payload, ensure_ascii=False, indent=2), mime="application/json; charset=utf-8",
                           headers=[("Content-Disposition", f'attachment; filename="backlinks-{run_id}.json"')])
            return
        if mode == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter=";")
            writer.writerow((*fields[:-1], "ziel", "ankertext", "rel"))
            for *values, data in rows:
                for link in json.loads(data) or [{"url": "", "anchor": "", "rel": ""}]:
                    writer.writerow([safe_csv(value) for value in (*values, link["url"], link["anchor"], link["rel"])])
            self.send_body("\ufeff" + buffer.getvalue(), mime="text/csv; charset=utf-8",
                           headers=[("Content-Disposition", f'attachment; filename="backlinks-{run_id}.csv"')])
            return
        with closing(connect()) as db:
            previous = dict(db.execute("""SELECT source,status FROM results WHERE run_id=(
                SELECT MAX(id) FROM runs WHERE job_id=? AND id<? AND finished IS NOT NULL)""",
                (run[0], run_id)))
        changes = {("fehlt", "gefunden"): "Hinzugekommen", ("gefunden", "fehlt"): "Verloren"}
        counts = {key: sum(row[3] == key for row in rows) for key in ("gefunden", "fehlt", "nicht prüfbar")}
        table = []
        for source, final, http, status, detail, data in rows:
            links = "".join(f'<li>{escape(link["url"])}<br><small>{escape(link["anchor"] or "(ohne Text)")} · '
                            f'{escape(link["rel"] or "ohne rel")}</small></li>' for link in json.loads(data))
            redirect = f'<small>Weiter zu {escape(final)}</small>' if source != final else ""
            change = changes.get((previous.get(source), status), "")
            table.append(f'<tr><td>{escape(source)}{redirect}</td>'
                         f'<td>{escape(status)} {http or ""}<small>{escape(detail or change)}</small></td><td><ul>{links}</ul></td></tr>')
        summary = " · ".join(f"{value} {key}" for key, value in counts.items())
        self.send_body(page("Prüfergebnis", f"""<p><a href="/jobs/{run[0]}">Zum Prüfauftrag</a></p>
        <p>{run[1]} · {"abgeschlossen" if run[2] else "läuft, Seite zum Aktualisieren neu laden"}</p>
        <p>{summary}</p><p><a href="csv">CSV</a> · <a href="json">JSON</a></p>
        <div class="scroll"><table><thead><tr><th>Quelle</th><th>Status</th><th>Gefundene Links</th></tr></thead>
        <tbody>{"".join(table)}</tbody></table></div>"""))

    def do_POST(self):
        if not self.authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_600_000:
                raise ValueError("Ungültige Eingabegröße.")
            form = parse_qs(self.rfile.read(length).decode(), max_num_fields=12)
            if not hmac.compare_digest(form.get("csrf", [""])[0], TOKEN):
                self.send_body(page("Ungültige Anfrage", '<a href="/">Zurück zur Übersicht</a>'), 403)
                return
            path = urlsplit(self.path).path.strip("/").split("/")
            with closing(connect()) as db, db:
                if path == ["jobs"]:
                    job_id = db.execute("""INSERT INTO jobs(target,exact,sources,interval_hours,next_run,created)
                                        VALUES (?,?,?,?,unixepoch(),?)""", (*clean_form(form), now())).lastrowid
                elif len(path) == 2 and path[0] == "jobs" and path[1].isascii() and path[1].isdigit() and len(path[1]) < 19:
                    job_id = int(path[1])
                    action = form.get("action", [""])[0]
                    if action == "run":
                        db.execute("""UPDATE jobs SET next_run=unixepoch() WHERE id=? AND NOT EXISTS
                                    (SELECT 1 FROM runs WHERE job_id=? AND finished IS NULL)""", (job_id, job_id))
                    elif action == "stop":
                        db.execute("UPDATE jobs SET interval_hours=0,next_run=NULL WHERE id=?", (job_id,))
                    else:
                        raise ValueError("Unbekannte Aktion")
                else:
                    self.send_body(page("Seite nicht gefunden", ""), 404)
                    return
        except (ValueError, UnicodeError) as exc:
            form = locals().get("form", {})
            self.send_body(page("Eingabe prüfen", f'<p role="alert">{escape(str(exc))}</p>' + create_form(
                form.get("target", [""])[0], form.get("sources", [""])[0], form.get("interval", ["0"])[0],
                form.get("match") == ["exact"])), 400)
            return
        self.send_body("", 303, headers=[("Location", f"/jobs/{job_id}")])


if __name__ == "__main__":
    initialize()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))),
                        Handler).serve_forever()
