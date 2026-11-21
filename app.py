"""Single-user backlink monitor with a persistent SQLite history."""
import csv
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
from time import monotonic
from collections import defaultdict, deque, namedtuple
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from checker import inspect, normalize

DB = Path(os.environ.get("DATA_DIR", "./data")) / "backlinks.sqlite3"
PASSWORD = os.environ.get("APP_PASSWORD", "")
TOKEN = secrets.token_urlsafe(32)
MAX_SOURCES = 25_000
MAX_IMPORT_BYTES = 16 * 1024 * 1024
WORKERS = 16
HOST_WORKERS = 2
WRITE_BATCH = 25
PAGE_SIZE = 100
STATUSES = ("gefunden", "fehlt", "nicht prüfbar")


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
        CREATE INDEX IF NOT EXISTS result_run_source ON results(run_id,source);
        CREATE INDEX IF NOT EXISTS run_job ON runs(job_id);
        """)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def check_sources(sources, target, exact):
    by_host = defaultdict(deque)
    for source in sources:
        by_host[urlsplit(source).hostname].append(source)
    available = deque(by_host)
    active = defaultdict(int)
    pending = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while available or pending:
            while available and len(pending) < WORKERS:
                host = available.popleft()
                source = by_host[host].popleft()
                pending[pool.submit(inspect, source, target, exact)] = (host, source)
                active[host] += 1
                if by_host[host] and active[host] < HOST_WORKERS:
                    available.append(host)
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                host, source = pending.pop(future)
                if by_host[host] and active[host] == HOST_WORKERS:
                    available.append(host)
                active[host] -= 1
                yield source, future.result()


def save_results(db, rows):
    with db:
        db.executemany("INSERT INTO results VALUES (?,?,?,?,?,?,?)", rows)
    rows.clear()


def execute_next():
    with closing(connect()) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("""SELECT j.id,j.target,j.exact,j.sources,r.id
                                FROM runs r JOIN jobs j ON j.id=r.job_id
                                WHERE r.finished IS NULL ORDER BY r.id LIMIT 1""").fetchone()
            if job is None:
                job = db.execute("""SELECT id,target,exact,sources,NULL FROM jobs
                                    WHERE next_run <= unixepoch() ORDER BY next_run,id LIMIT 1""").fetchone()
            if job is None:
                return False
            job_id, target, exact, sources, run_id = job
            if run_id is None:
                run_id = db.execute("INSERT INTO runs(job_id,started) VALUES (?,?)", (job_id, now())).lastrowid
                db.execute("UPDATE jobs SET next_run=NULL WHERE id=?", (job_id,))
        done = {row[0] for row in db.execute("SELECT source FROM results WHERE run_id=?", (run_id,))}
        batch = []
        next_write = monotonic() + 2
        for source, (final, http, status, detail, links) in check_sources(
            (source for source in json.loads(sources) if source not in done), target, exact
        ):
            batch.append((run_id, source, final, http, status, detail, json.dumps(links, ensure_ascii=False)))
            if len(batch) == WRITE_BATCH or monotonic() >= next_write:
                save_results(db, batch)
                next_write = monotonic() + 2
        save_results(db, batch)
        with db:
            db.execute("UPDATE runs SET finished=? WHERE id=?", (now(), run_id))
            db.execute("""UPDATE jobs SET next_run=COALESCE(next_run,unixepoch()+interval_hours*3600)
                          WHERE id=? AND interval_hours>0""", (job_id,))
    return True


def worker():
    event = threading.Event()
    while True:
        if not execute_next():
            event.wait(2)


SOURCE_HEADERS = {
    "quelle", "quell-url", "quellseite", "source", "source url", "source_url",
    "referring page", "referring page url", "referring url", "referring_url", "url",
}


def import_urls(raw):
    raw = raw.lstrip("\ufeff")
    if not raw.strip():
        return
    for delimiter in (";", ",", "\t"):
        records = csv.reader(io.StringIO(raw, newline=""), delimiter=delimiter)
        first = next((row for row in records if any(cell.strip() for cell in row)), [])
        header = [cell.strip().casefold() for cell in first]
        named = [index for index, name in enumerate(header) if name in SOURCE_HEADERS - {"url"}]
        if named or "url" in header:
            column = named[0] if named else header.index("url")
            for row in records:
                if any(cell.strip() for cell in row):
                    yield records.line_num, row[column] if len(row) > column else ""
            return
    for number, line in enumerate(raw.splitlines(), 1):
        if line.strip():
            yield number, line


def clean_form(form):
    target = normalize(form.get("target", [""])[0])
    sources = {}
    for name in ("sources", "source_file"):
        raw = form.get(name, [""])[0]
        for number, value in import_urls(raw):
            try:
                if not value.strip():
                    raise ValueError("Quell-URL fehlt")
                source = normalize(value)
            except ValueError as exc:
                raise ValueError(f'{("Datei" if name == "source_file" else "Liste")}, Zeile {number} · {exc}') from exc
            sources[source] = None
            if len(sources) > MAX_SOURCES:
                raise ValueError("Maximal 25.000 eindeutige Quell-URLs pro Auftrag.")
    interval = int(form.get("interval", ["0"])[0])
    if not sources or interval not in {0, 1, 24, 168}:
        raise ValueError("Bitte mindestens eine Quell-URL und ein gültiges Prüfintervall angeben.")
    return target, int(form.get("match") == ["exact"]), json.dumps(list(sources)), interval


def read_form(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if not 0 < length <= MAX_IMPORT_BYTES:
        raise ValueError("Die Anfrage darf höchstens 16 MiB groß sein.")
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ValueError("Der Upload wurde unterbrochen.")
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        return parse_qs(body.decode(), max_num_fields=12)
    headers = ("Content-Type: " + content_type + "\r\n\r\n").encode("ascii")
    message = BytesParser(policy=policy.default).parsebytes(headers + body)
    if not message.is_multipart():
        raise ValueError("Die Datei konnte nicht gelesen werden.")
    form = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name in {"csrf", "target", "sources", "source_file", "interval", "match"} and not part.is_multipart():
            form[name] = [part.get_payload(decode=True).decode("utf-8-sig")]
    return form


def domain_key(target):
    return urlsplit(target).hostname.removeprefix("www.")


def domain_path(domain):
    return "/domains/" + quote(domain, safe="")


DomainJob = namedtuple("DomainJob", "id target exact interval scheduled run finished total found missing unavailable gained lost active")


def domain_jobs(db):
    domains = {}
    rows = db.execute("""
        WITH latest AS (
          SELECT job_id, MAX(id) AS run_id FROM runs WHERE finished IS NOT NULL GROUP BY job_id
        ), comparison AS (
          SELECT l.job_id, MAX(p.id) AS previous FROM latest l
          JOIN runs p ON p.job_id=l.job_id AND p.id<l.run_id AND p.finished IS NOT NULL
          GROUP BY l.job_id
        )
        SELECT j.id,j.target,j.exact,j.interval_hours,j.next_run,r.id,r.finished,
          COUNT(s.source),
          COUNT(CASE WHEN s.status='gefunden' THEN 1 END),
          COUNT(CASE WHEN s.status='fehlt' THEN 1 END),
          COUNT(CASE WHEN s.status='nicht prüfbar' THEN 1 END),
          COUNT(CASE WHEN s.status='gefunden' AND old.status='fehlt' THEN 1 END),
          COUNT(CASE WHEN s.status='fehlt' AND old.status='gefunden' THEN 1 END),
          (SELECT MAX(id) FROM runs WHERE job_id=j.id AND finished IS NULL)
        FROM jobs j LEFT JOIN latest l ON j.id=l.job_id
        LEFT JOIN comparison c ON c.job_id=j.id
        LEFT JOIN runs r ON r.id=l.run_id LEFT JOIN results s ON s.run_id=r.id
        LEFT JOIN results old ON old.run_id=c.previous AND old.source=s.source
        GROUP BY j.id ORDER BY j.id DESC
    """)
    for row in rows:
        job = DomainJob(*row)
        domains.setdefault(domain_key(job.target), []).append(job)
    return domains


def schedule_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def domain_summary(jobs, timestamp):
    latest = max((job for job in jobs if job.finished), key=lambda job: (job.finished, job.run), default=None)
    scheduled = [job.scheduled for job in jobs if job.scheduled is not None]
    return {
        "latest": latest,
        "next": min(scheduled, default=None),
        "running": sum(job.active is not None for job in jobs),
        "queued": sum(value <= timestamp for value in scheduled),
        "jobs": len(jobs),
        **{field: sum(getattr(job, field) for job in jobs)
           for field in ("total", "found", "missing", "unavailable", "gained", "lost")},
    }


def dashboard_domain_row(domain, data):
    latest = data["latest"]
    last = f'<a href="/runs/{latest.run}/report">{escape(latest.finished)}</a>' if latest else "Noch offen"
    activity = []
    if data["running"]:
        activity.append(f'{data["running"]} aktiv')
    if data["queued"]:
        activity.append(f'{data["queued"]} in Warteschlange')
    if data["next"] is not None and not data["queued"]:
        activity.append(schedule_time(data["next"]))
    if not activity:
        activity.append("Keine geplant")
    return f"""<tr><th scope="row"><a href="{domain_path(domain)}">{escape(domain)}</a>
    <small>{data["jobs"]} Aufträge</small></th>
    <td><strong>{data["found"]}</strong><small>{data["total"]} geprüft</small></td>
    <td>{data["missing"]}</td><td>{data["unavailable"]}</td>
    <td><span class="gain">+{data["gained"]}</span> <span class="loss">−{data["lost"]}</span></td>
    <td>{last}</td><td>{'<br>'.join(activity)}</td></tr>"""


def queue_jobs(db, job_ids):
    db.executemany("""UPDATE jobs SET next_run=unixepoch() WHERE id=? AND NOT EXISTS
                     (SELECT 1 FROM runs WHERE job_id=jobs.id AND finished IS NULL)""",
                   [(job_id,) for job_id in job_ids])


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
    return f"""<form method="post" action="/jobs" enctype="multipart/form-data">
    <input type="hidden" name="csrf" value="{TOKEN}">
    <label>Zieldomain oder Ziel-URL<input name="target" value="{escape(target)}" placeholder="https://beispiel.de" required></label>
    <div class="pair"><label>Abgleich<select name="match">
    <option value="domain">Domain inklusive Subdomains</option>
    <option value="exact" {"selected" if exact else ""}>Exakte Ziel-URL</option></select></label>
    <label>Wiederholung<select name="interval">{options}</select></label></div>
    <label>Quellseiten<textarea name="sources" rows="6" placeholder="Eine URL pro Zeile">{escape(sources)}</textarea></label>
    <label>CSV- oder Textdatei<input type="file" name="source_file" accept=".csv,.tsv,.txt,text/plain,text/csv"></label>
    <small>Bis zu 25.000 eindeutige Quell-URLs pro Auftrag. CSV mit Kopfzeile, beispielsweise „Quell-URL“ oder „Source URL“.</small>
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
        url = urlsplit(self.path)
        path = url.path
        if path == "/health":
            self.send_body("ok", mime="text/plain")
            return
        if not self.authorized():
            return
        if path == "/style.css":
            self.send_body(Path(__file__).with_name("style.css").read_text(), mime="text/css; charset=utf-8")
        elif path == "/":
            self.dashboard(url.query)
        else:
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "domains":
                self.domain(unquote(parts[1]))
            elif len(parts) == 2 and parts[0] == "jobs" and parts[1].isascii() and parts[1].isdigit() and len(parts[1]) < 19:
                self.job(int(parts[1]))
            elif len(parts) == 3 and parts[0] == "runs" and parts[1].isascii() and parts[1].isdigit() and len(parts[1]) < 19:
                self.run_report(int(parts[1]), parts[2])
            else:
                self.send_body(page("Seite nicht gefunden", ""), 404)

    def dashboard(self, query):
        try:
            params = parse_qs(query, max_num_fields=10)
        except ValueError:
            params = {}
        search = params.get("q", [""])[0][:254].strip()
        status = params.get("status", ["all"])[0]
        with closing(connect()) as db, db:
            db.execute("BEGIN")
            timestamp = db.execute("SELECT unixepoch()").fetchone()[0]
            domains = {name: domain_summary(jobs, timestamp) for name, jobs in domain_jobs(db).items()}
            recent = db.execute("""SELECT r.id,j.target,r.finished FROM runs r
                               JOIN jobs j ON j.id=r.job_id WHERE r.finished IS NOT NULL
                               ORDER BY r.finished DESC,r.id DESC LIMIT 5""").fetchall()
        totals = {key: sum(data[key] for data in domains.values())
                  for key in ("jobs", "total", "found", "missing", "unavailable", "gained", "lost", "running", "queued")}
        metrics = (
            (str(len(domains)), "Domains", f'{totals["jobs"]} Prüfaufträge'),
            (str(totals["found"]), "Quellen mit Backlink", f'{totals["total"]} geprüfte Quellen'),
            (str(totals["missing"]), "Backlink fehlt", f'{totals["unavailable"]} nicht prüfbar'),
            (f'+{totals["gained"]} / −{totals["lost"]}', "Hinzugekommen / verloren", "Gegenüber dem vorherigen Lauf"),
        )
        cards = "".join(f'<li><span>{label}</span><strong>{value}</strong><small>{note}</small></li>'
                        for value, label, note in metrics)
        filters = (
            ("all", "Alle Domains"), ("missing", "Backlink fehlt"), ("unavailable", "Nicht prüfbar"),
            ("lost", "Backlinks verloren"), ("unverified", "Noch ohne Ergebnis"),
        )
        if status not in dict(filters):
            status = "all"
        options = "".join(f'<option value="{value}" {"selected" if value == status else ""}>{label}</option>'
                          for value, label in filters)
        rows = []
        for name, data in sorted(domains.items()):
            match = {
                "all": True, "missing": data["missing"] > 0, "unavailable": data["unavailable"] > 0,
                "lost": data["lost"] > 0, "unverified": data["latest"] is None,
            }
            if search.casefold() in name.casefold() and match[status]:
                rows.append(dashboard_domain_row(name, data))
        table = f"""<div class="scroll overview"><table><thead><tr>
        <th scope="col">Domain</th><th scope="col">Gefunden</th><th scope="col">Fehlt</th>
        <th scope="col">Nicht prüfbar</th><th scope="col">Neu / verloren</th>
        <th scope="col">Letzter Abschluss</th><th scope="col">Nächste Prüfung</th>
        </tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""
        empty = "Keine Domain entspricht dem Filter." if domains else "Lege den ersten Prüfauftrag an, um eine Domain hinzuzufügen."
        activity = "".join(f'<li><a href="/runs/{run}/report">{escape(target)}</a>'
                           f'<small>{escape(finished)}</small></li>' for run, target, finished in recent)
        self.send_body(page("Backlink-Dashboard", f"""
        <div class="dashboard-intro"><p>Prüfergebnisse und Zeitpläne für deine Domains.</p>
        <a class="primary-link" href="#neuer-auftrag">Neuer Prüfauftrag</a></div>
        <ul class="metrics">{cards}</ul>
        <section aria-labelledby="domains-title"><h2 id="domains-title">Domains</h2>
        <p>{totals["running"]} aktive Prüfungen · {totals["queued"]} in Warteschlange</p>
        <form method="get" action="/" class="filters">
        <label>Domain suchen<input type="search" name="q" value="{escape(search)}"></label>
        <label>Status<select name="status">{options}</select></label><button>Filtern</button></form>
        {table if rows else f'<p class="empty">{empty}</p>'}
        <p class="caption">Die Zahlen stammen aus dem letzten abgeschlossenen Lauf jedes Auftrags.
        Dieselbe Quelle kann in mehreren Aufträgen gezählt werden.</p></section>
        <section aria-labelledby="recent-title"><h2 id="recent-title">Letzte Prüfläufe</h2>
        {f'<ul class="entries">{activity}</ul>' if activity else '<p>Noch keine Prüfläufe abgeschlossen.</p>'}</section>
        <section id="neuer-auftrag"><h2>Neuer Prüfauftrag</h2>{create_form()}</section>"""))

    def domain(self, domain):
        with closing(connect()) as db:
            jobs = domain_jobs(db).get(domain)
        if not jobs:
            self.send_body(page("Domain nicht gefunden", '<a href="/">Zur Domain-Übersicht</a>'), 404)
            return
        rows = []
        for job in jobs:
            job_id, target, exact, interval, scheduled, run_id, finished, total, found, missing, unavailable, _, _, active = job
            schedule = f"Alle {interval} h" if interval else "Manuell"
            if active is not None:
                schedule += f' · <a href="/runs/{active}/report">Läuft</a>'
            elif scheduled is not None:
                schedule += " · nächste Prüfung " + schedule_time(scheduled)
            outcome = f'{found}/{total} gefunden · {missing} fehlen · {unavailable} nicht prüfbar'
            report = f'<a href="/runs/{run_id}/report">{finished}</a><small>{outcome}</small>' if run_id else "Noch kein abgeschlossener Lauf"
            rows.append(f'<tr><td><a href="/jobs/{job_id}">{escape(target)}</a>'
                        f'<small>{"Exakte URL" if exact else "Domain-Abgleich"}</small></td><td>{schedule}</td>'
                        f'<td>{report}</td></tr>')
        self.send_body(page(domain, f"""<p><a href="/">Zur Domain-Übersicht</a></p>
        <form class="actions" method="post" action="{domain_path(domain)}">
        <input type="hidden" name="csrf" value="{TOKEN}"><button>Alle Aufträge dieser Domain prüfen</button></form>
        <h2>Prüfaufträge</h2><div class="scroll"><table><thead><tr><th>Ziel</th><th>Zeitplan</th>
        <th>Letzter Abschluss</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
        <h2>Neuer Prüfauftrag</h2>{create_form(jobs[0][1] if ":" in domain else "https://" + domain + "/")}"""))

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
        self.send_body(page(job[0], f"""<p><a href="{domain_path(domain_key(job[0]))}">Zur Domain</a></p>
        <p>{"Intervall " + str(job[1]) + " h" if job[1] else "Manuelle Prüfung"}{suffix}</p>
        <form class="actions" method="post" action="/jobs/{job_id}">
        <input type="hidden" name="csrf" value="{TOKEN}">{controls}</form>
        <h2>Verlauf</h2><ul class="entries">{rows or "<li>Prüfung wartet auf Ausführung.</li>"}</ul>"""))

    def export_run(self, db, run_id, mode):
        fields = ("quelle", "effektive_quelle", "http_status", "status", "detail", "links")
        mime = "application/json" if mode == "json" else "text/csv"
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="backlinks-{run_id}.{mode}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        rows = db.execute("SELECT source,final,http,status,detail,links FROM results WHERE run_id=? ORDER BY rowid",
                          (run_id,))
        write = self.wfile.write
        try:
            if mode == "json":
                write(b"[")
                for count, (*values, data) in enumerate(rows):
                    payload = dict(zip(fields, (*values, json.loads(data))))
                    write((("," if count else "") + json.dumps(payload, ensure_ascii=False)).encode())
                write(b"]")
            else:
                buffer = io.StringIO()
                writer = csv.writer(buffer, delimiter=";")
                writer.writerow((*fields[:-1], "ziel", "ankertext", "rel"))
                write(("\ufeff" + buffer.getvalue()).encode())
                for *values, data in rows:
                    buffer.seek(0)
                    buffer.truncate()
                    for link in json.loads(data) or [{"url": "", "anchor": "", "rel": ""}]:
                        writer.writerow([safe_csv(value) for value in (*values, link["url"], link["anchor"], link["rel"])])
                    write(buffer.getvalue().encode())
        except (BrokenPipeError, ConnectionResetError):
            return

    def run_report(self, run_id, mode):
        with closing(connect()) as db, db:
            db.execute("BEGIN")
            run = db.execute("""SELECT r.job_id,r.started,r.finished,json_array_length(j.sources)
                              FROM runs r JOIN jobs j ON j.id=r.job_id WHERE r.id=?""", (run_id,)).fetchone()
            if not run or mode not in {"report", "csv", "json"}:
                self.send_body(page("Prüflauf nicht gefunden", ""), 404)
                return
            if mode in {"csv", "json"}:
                self.export_run(db, run_id, mode)
                return
            try:
                query = parse_qs(urlsplit(self.path).query, max_num_fields=6)
            except ValueError:
                query = {}
            selection = query.get("status", [""])[0]
            raw_page = query.get("page", ["1"])[0]
            page_number = max(1, int(raw_page)) if raw_page.isascii() and raw_page.isdigit() and len(raw_page) <= 6 else 1
            counts = dict(db.execute("SELECT status,COUNT(*) FROM results WHERE run_id=? GROUP BY status", (run_id,)))
            processed = sum(counts.values())
            if selection not in STATUSES:
                selection = ""
            matching = counts.get(selection, 0) if selection else processed
            pages = max(1, (matching + PAGE_SIZE - 1) // PAGE_SIZE)
            page_number = min(page_number, pages)
            rows = db.execute("""SELECT s.source,s.final,s.http,s.status,s.detail,s.links,old.status
                FROM results s LEFT JOIN results old ON old.source=s.source AND old.run_id=(
                  SELECT MAX(id) FROM runs WHERE job_id=? AND id<? AND finished IS NOT NULL)
                WHERE s.run_id=? AND (?='' OR s.status=?)
                ORDER BY s.rowid LIMIT ? OFFSET ?""",
                (run[0], run_id, run_id, selection, selection, PAGE_SIZE, (page_number - 1) * PAGE_SIZE)).fetchall()
        changes = {("fehlt", "gefunden"): "Hinzugekommen", ("gefunden", "fehlt"): "Verloren"}
        table = []
        for source, final, http, status, detail, data, previous in rows:
            found = json.loads(data)
            links = "".join(f'<li>{escape(link["url"])}<br><small>{escape(link["anchor"] or "(ohne Text)")} · '
                            f'{escape(link["rel"] or "ohne rel")}</small></li>' for link in found[:10])
            if len(found) > 10:
                links += f"<li><small>{len(found) - 10} weitere Links im Export</small></li>"
            redirect = f'<small>Weiter zu {escape(final)}</small>' if source != final else ""
            change = changes.get((previous, status), "")
            table.append(f'<tr><td>{escape(source)}{redirect}</td>'
                         f'<td>{escape(status)} {http or ""}<small>{escape(detail or change)}</small></td><td><ul>{links}</ul></td></tr>')
        summary = " · ".join(f"{counts.get(key, 0)} {key}" for key in STATUSES)
        def report_link(number, label):
            query_string = escape(urlencode({"page": number, "status": selection}))
            return f'<a href="?{query_string}">{label}</a>'
        pagination = [f"Seite {page_number} von {pages}"]
        if page_number > 1:
            pagination.insert(0, report_link(page_number - 1, "Zurück"))
        if page_number < pages:
            pagination.append(report_link(page_number + 1, "Weiter"))
        options = "".join(f'<option value="{escape(key)}" {"selected" if key == selection else ""}>{label}</option>'
                          for key, label in [("", "Alle Ergebnisse"), *((key, key.capitalize()) for key in STATUSES)])
        progress = f"{processed:,} von {max(run[3], processed):,} Quellen geprüft".replace(",", ".")
        self.send_body(page("Prüfergebnis", f"""<p><a href="/jobs/{run[0]}">Zum Prüfauftrag</a></p>
        <p>{run[1]} · {"abgeschlossen" if run[2] else "läuft"} · {progress}</p>
        <p>{summary}</p><p><a href="csv">Alle als CSV</a> · <a href="json">Alle als JSON</a>
        {' · ' + report_link(page_number, 'Aktualisieren') if not run[2] else ''}</p>
        <form method="get" class="filters"><label>Status<select name="status">{options}</select></label><button>Filtern</button></form>
        <div class="scroll"><table><thead><tr><th>Quelle</th><th>Status</th><th>Gefundene Links</th></tr></thead>
        <tbody>{"".join(table) or '<tr><td colspan="3">Keine Ergebnisse für diese Auswahl.</td></tr>'}</tbody></table></div>
        <p aria-label="Seitennavigation">{" · ".join(pagination)}</p>"""))

    def do_POST(self):
        if not self.authorized():
            return
        form = {}
        location = ""
        try:
            form = read_form(self)
            if not hmac.compare_digest(form.get("csrf", [""])[0].encode(), TOKEN.encode()):
                self.send_body(page("Ungültige Anfrage", '<a href="/">Zurück zur Übersicht</a>'), 403)
                return
            path = urlsplit(self.path).path.strip("/").split("/")
            with closing(connect()) as db, db:
                if path == ["jobs"]:
                    job_id = db.execute("""INSERT INTO jobs(target,exact,sources,interval_hours,next_run,created)
                                        VALUES (?,?,?,?,unixepoch(),?)""", (*clean_form(form), now())).lastrowid
                elif len(path) == 2 and path[0] == "domains":
                    domain = unquote(path[1])
                    jobs = domain_jobs(db).get(domain)
                    if not jobs:
                        self.send_body(page("Domain nicht gefunden", '<a href="/">Zur Domain-Übersicht</a>'), 404)
                        return
                    queue_jobs(db, [job[0] for job in jobs])
                    location = domain_path(domain)
                elif len(path) == 2 and path[0] == "jobs" and path[1].isascii() and path[1].isdigit() and len(path[1]) < 19:
                    job_id = int(path[1])
                    action = form.get("action", [""])[0]
                    if action == "run":
                        queue_jobs(db, [job_id])
                    elif action == "stop":
                        db.execute("UPDATE jobs SET interval_hours=0,next_run=NULL WHERE id=?", (job_id,))
                    else:
                        raise ValueError("Unbekannte Aktion")
                else:
                    self.send_body(page("Seite nicht gefunden", ""), 404)
                    return
        except (ValueError, csv.Error) as exc:
            self.send_body(page("Eingabe prüfen", f'<p role="alert">{escape(str(exc))}</p>' + create_form(
                form.get("target", [""])[0], form.get("sources", [""])[0][:50_000], form.get("interval", ["0"])[0],
                form.get("match") == ["exact"])), 400)
            return
        self.send_body("", 303, headers=[("Location", location or f"/jobs/{job_id}")])


if __name__ == "__main__":
    initialize()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))),
                        Handler).serve_forever()
