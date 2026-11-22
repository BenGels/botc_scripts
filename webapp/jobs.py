"""Hintergrund-Jobs, die `botc_tokens script` fuer hochgeladene oder per Namen gesuchte Scripts ausfuehren.

Die WebApp ruft bewusst das bestehende Kommandozeilenprogramm auf (statt dessen Funktionen
nachzubauen). Dadurch kommt exakt dasselbe PDF heraus wie bei

    botc_tokens script <script.json> --token-dir tokens_DEEN -o <ausgabe> ...

Dieses Modul nutzt nur die Standardbibliothek, damit es sich ohne Flask testen laesst. Die Suche auf
botcscripts.com (Scriptname statt Datei) laeuft ueber den Helfer aus botc_tokens und wird erst im Job geladen.
"""
# Standard Library
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Ordner mit den Token-Bildern, relativ zum Projektordner (oder absolut).
TOKEN_DIR = os.environ.get("BOTC_TOKEN_DIR", "tokens_DEEN")
# Arbeitsordner fuer Uploads und Ergebnisse. Liegt bewusst ausserhalb des Projekts.
WORK_ROOT = Path(os.environ.get("BOTC_WEB_WORKDIR") or Path(tempfile.gettempdir()) / "botc_webapp")
# Seitenformat wie bei den von Hand erzeugten PDFs: A4 bei 300 dpi, ohne Rand (die Hintergruende sind
# fuer A4 gemacht). Die Voreinstellung der CLI waere US Letter mit 74 px Rand.
PAPER_ARGS = ["--paper-width", "2480", "--paper-height", "3508",
              "--margin-horizontal", "0", "--margin-vertical", "0"]
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_SCRIPT_NAME_LENGTH = 200
# Autocomplete: erst ab so vielen Zeichen suchen, Antworten eine Weile merken (schont botcscripts.com).
SEARCH_MIN_CHARS = 3
SEARCH_CACHE_SECONDS = 600
SEARCH_CACHE_MAX = 500
# Derselbe Cache wie bei der CLI (botc_tokens script <name>, gestartet im Projektordner).
DOWNLOAD_DIR = PROJECT_ROOT / "downloaded_scripts"
JOB_TIMEOUT_SECONDS = int(os.environ.get("BOTC_JOB_TIMEOUT", "900"))
JOB_TTL_SECONDS = int(os.environ.get("BOTC_JOB_TTL", "3600"))
# Die Erzeugung ist schwer (ImageMagick, grosse Bilder): standardmaessig ein Job nach dem anderen.
WORKERS = max(1, int(os.environ.get("BOTC_WEB_WORKERS", "1")))
MAX_LOG_LINES = 300

RAW_PDFS = ("script.pdf", "backside.pdf")
LOGO_PROVIDERS = ("pollinations", "openai")
DEFAULT_NAME_COLOR = "#000000"

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
# Auftrags-IDs: 32-stelliger Zufalls-Hash (uuid4), nicht fortlaufend und nicht erratbar.
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PROBLEM_RE = re.compile(r"error|fehler|exception|traceback|warning|warnung", re.IGNORECASE)


class UploadError(ValueError):
    """Eingabe des Nutzers ist unbrauchbar. Die Meldung ist fuer die Anzeige gedacht."""


def openai_available():
    """True, wenn ein OpenAI-Schluessel konfiguriert ist (Umgebung oder .env). Gibt ihn nie aus."""
    if os.environ.get("OPENAI_API_KEY"):
        return True
    env_file = PROJECT_ROOT / ".env"
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"^\s*(export\s+)?OPENAI_API_KEY=.+", text, re.MULTILINE) is not None


class Options:
    """Pruefte Einstellungen aus dem Formular."""

    def __init__(self, name_color="", logo_description="", logo_provider="pollinations",
                 blankbg=False, new_logo=False):
        name_color = (name_color or "").strip() or DEFAULT_NAME_COLOR
        if not _HEX_COLOR_RE.match(name_color):
            raise UploadError("Die Schriftfarbe muss ein Hex-Wert wie #000000 sein.")
        self.name_color = name_color

        description = _CONTROL_RE.sub(" ", logo_description or "").strip()
        if len(description) > 500:
            raise UploadError("Die Logo-Beschreibung ist zu lang (maximal 500 Zeichen).")
        self.logo_description = description

        provider = (logo_provider or "pollinations").strip()
        if provider not in LOGO_PROVIDERS:
            raise UploadError("Unbekannter Logo-Anbieter.")
        if provider == "openai" and not openai_available():
            raise UploadError("OpenAI ist auf diesem Server nicht eingerichtet (OPENAI_API_KEY fehlt).")
        self.logo_provider = provider

        self.blankbg = bool(blankbg)
        self.new_logo = bool(new_logo)


def _import_from_src(module, attr):
    """Holt ein Objekt aus botc_tokens (src/), ohne das Paket installieren zu muessen."""
    src = str(PROJECT_ROOT / "src")
    sys.path.insert(0, src)
    try:
        return getattr(__import__(module, fromlist=[attr]), attr)
    finally:
        sys.path.remove(src)


def check_script_name(value):
    """Prueft einen eingegebenen Scriptnamen fuer die Suche auf botcscripts.com."""
    name = " ".join(_CONTROL_RE.sub(" ", value or "").split())
    if not name:
        raise UploadError("Bitte einen Scriptnamen eingeben.")
    if len(name) > MAX_SCRIPT_NAME_LENGTH:
        raise UploadError(f"Der Scriptname ist zu lang (maximal {MAX_SCRIPT_NAME_LENGTH} Zeichen).")
    return name


def check_script_pk(value):
    """Prueft die ID (pk) eines auf botcscripts.com ausgewaehlten Scripts. None, wenn leer."""
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdigit() or not 0 < int(value) < 10**9:
        raise UploadError("Ungueltige Script-Auswahl.")
    return int(value)


class ScriptSearch:
    """Suche fuer das Autocomplete, mit kleinem Zwischenspeicher pro Suchbegriff."""

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def search(self, query):
        query = " ".join(_CONTROL_RE.sub(" ", query or "").split())[:MAX_SCRIPT_NAME_LENGTH]
        if len(query) < SEARCH_MIN_CHARS:
            return []
        key = query.lower()
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < SEARCH_CACHE_SECONDS:
                return hit[1]
        lookup = _import_from_src("botc_tokens.helpers", "botc_scripts_lookup")
        results = lookup.search_scripts(query, limit=8)  # RuntimeError bei Netzwerkproblemen
        with self._lock:
            if len(self._cache) >= SEARCH_CACHE_MAX:
                self._cache.clear()
            self._cache[key] = (now, results)
        return results


def parse_script(data):
    """Prueft den Upload und liefert (script_liste, scriptname_oder_None)."""
    if not data:
        raise UploadError("Die Datei ist leer.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"Die Datei ist zu gross (maximal {MAX_UPLOAD_BYTES // 1024 // 1024} MB).")
    try:
        script = json.loads(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise UploadError("Die Datei ist nicht UTF-8-kodiert.") from None
    except json.JSONDecodeError as e:
        raise UploadError(f"Das ist kein gueltiges JSON (Zeile {e.lineno}, Spalte {e.colno}): {e.msg}") from None
    if not isinstance(script, list) or not script:
        raise UploadError("Ein Script muss eine nicht leere JSON-Liste sein (Format des offiziellen Script-Tools).")
    for entry in script:
        if not isinstance(entry, (str, dict)):
            raise UploadError("Die Script-Liste darf nur Rollen-IDs (Text) oder Objekte enthalten.")
    name = None
    if isinstance(script[0], dict):
        raw_name = script[0].get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
    return script, name


def _write_script(directory, script):
    # ensure_ascii: unabhaengig von der Locale des Servers lesbar (botc_tokens oeffnet ohne Encoding).
    (directory / "script.json").write_text(json.dumps(script, ensure_ascii=True), encoding="ascii")


class Job:
    """Ein Auftrag: ein hochgeladenes Script, ein Lauf von botc_tokens."""

    def __init__(self, job_id, directory, script, script_name, options, query=None, script_pk=None, owner=None):
        self.id = job_id
        self.dir = directory
        self.script = script
        self.script_name = script_name
        self.options = options
        self.query = query  # Scriptname fuer botcscripts.com, solange das Script noch fehlt
        self.script_pk = script_pk  # genau diese Version laden (im Autocomplete ausgewaehlt)
        self.owner = owner  # ID des Benutzerkontos; nur dieses darf Status und Ergebnis abrufen
        self.error_code = None  # maschinenlesbarer Grund fuer "error" (z. B. "quota_exceeded"), sonst None
        self.orphaned = False  # Konto wurde waehrend des Laufs geloescht -> Ergebnis danach wegwerfen
        self.created = time.time()
        self.finished = None
        self.status = "queued"  # queued -> running -> done | error
        self.message = "Wartet in der Warteschlange ..."
        self.partial = False
        self.result = None  # Pfad der Datei zum Herunterladen
        self.timed_out = False
        self.log = deque(maxlen=MAX_LOG_LINES)
        self.lock = threading.Lock()

    def add_log(self, line):
        with self.lock:
            self.log.append(line)

    def set_state(self, status, message, partial=False, result=None):
        with self.lock:
            self.status = status
            self.message = message
            self.partial = partial
            self.result = result
            if status in ("done", "error"):
                self.finished = time.time()

    def last_problem(self):
        with self.lock:
            lines = list(self.log)
        for line in reversed(lines):
            if _PROBLEM_RE.search(line):
                return line.strip()
        return None

    def public(self, include_log=True):
        with self.lock:
            data = {
                "id": self.id,
                "status": self.status,
                "message": self.message,
                "partial": self.partial,
                "log": list(self.log)[-40:] if include_log else [],
            }
            if self.status == "done" and self.result is not None:
                data["filename"] = self.result.name
                data["download_url"] = f"api/jobs/{self.id}/download"
            return data


class JobManager:
    """Verwaltet Jobs: Annehmen, nacheinander ausfuehren, spaeter aufraeumen."""

    def __init__(self, logo_quota=None):
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        # Optional: Limit fuer OpenAI-Logos (siehe quota.py). Ohne gibt es keine Begrenzung.
        self.logo_quota = logo_quota
        self._jobs = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="botc-job")

    # -- Annehmen -----------------------------------------------------------------------------
    def submit(self, data, options, query=None, script_pk=None, owner=None):
        """Prueft den Upload (oder den Scriptnamen), legt den Job an und stellt ihn in die Warteschlange.

        Mit `query` statt `data` wird das Script erst im Job auf botcscripts.com gesucht; mit `script_pk`
        wird genau dieses Script geladen statt nach dem Namen gesucht.
        """
        if query is not None:
            script, name = None, None
            query = check_script_name(query)
            script_pk = check_script_pk(script_pk)
        else:
            script, name = parse_script(data)
        self.cleanup()

        job_id = uuid.uuid4().hex
        directory = WORK_ROOT / job_id
        directory.mkdir(parents=True)
        if script is not None:
            _write_script(directory, script)

        job = Job(job_id, directory, script, name, options, query=query, script_pk=script_pk, owner=owner)
        with self._lock:
            self._jobs[job_id] = job
        self._executor.submit(self._run, job)
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    # -- Ausfuehren ---------------------------------------------------------------------------
    def _run(self, job):
        try:
            self._run_steps(job)
        finally:
            if job.orphaned:
                shutil.rmtree(job.dir, ignore_errors=True)

    def _run_steps(self, job):
        reservation = None
        try:
            if job.query is not None and not self._fetch_script(job):
                return
            # Vor dem Zur-Seite-Legen des alten Logos: Ist das OpenAI-Limit erreicht, bleibt alles, wie es ist.
            if self.logo_quota is not None and self.will_generate_openai_logo(job.script_name, job.options):
                try:
                    reservation = self.logo_quota.reserve(job.owner)
                except Exception as e:  # QuotaExceeded - Meldung ist fuer den Nutzer formuliert
                    job.error_code = "quota_exceeded"
                    job.add_log(str(e))
                    job.set_state("error", str(e))
                    return
            job.set_state("running", "PDF wird erzeugt - das kann ein bis zwei Minuten dauern ...")
            self._retire_cached_logo(job)
            returncode = self._run_botc_tokens(job)
        except Exception as e:  # noqa: BLE001 - jeder Fehler soll beim Nutzer ankommen, nicht den Worker toeten
            job.add_log(f"Interner Fehler: {e}")
            job.set_state("error", f"Interner Fehler: {e}")
            return
        finally:
            if reservation is not None:
                # Gezaehlt wird nur, wenn danach wirklich ein (neues) Logo im Cache liegt.
                logo = self.logo_path(job.script_name)
                generated = logo is not None and logo.is_file()
                reservation.finish(generated, job.script_name)
                if generated and reservation.counted:
                    job.add_log("Neues OpenAI-Logo erzeugt (zaehlt fuer dein Wochenlimit).")
        self._finish(job, returncode)

    @staticmethod
    def logo_path(script_name):
        """Pfad des Logos im Cache, wie botc_tokens ihn benutzt; None ohne Namen."""
        if not script_name:
            return None
        format_filename = _import_from_src("botc_tokens.helpers.text_tools", "format_filename")
        return PROJECT_ROOT / "logos" / f"{format_filename(script_name)}.png"

    @classmethod
    def will_generate_openai_logo(cls, script_name, options):
        """True, wenn der Auftrag ein Logo bei OpenAI erzeugen wird (statt eins aus dem Cache zu nehmen)."""
        if options.logo_provider != "openai":
            return False
        logo = cls.logo_path(script_name)
        if logo is None:
            return False  # ohne Scriptnamen gibt es kein Logo
        return options.new_logo or not logo.is_file()

    def _fetch_script(self, job):
        """Laedt das ausgewaehlte Script bzw. sucht es per Namen (lokaler Cache, sonst botcscripts.com).

        False bei Fehler.
        """
        try:
            lookup = _import_from_src("botc_tokens.helpers", "botc_scripts_lookup")
        except ImportError as e:
            job.set_state("error", f"Die Suche ist hier nicht verfuegbar, botc_tokens ist nicht importierbar ({e}).")
            return False
        try:
            if job.script_pk is not None:
                job.set_state("running", f"Lade Script '{job.query}' von botcscripts.com ...")
                job.add_log(f"Lade das ausgewaehlte Script '{job.query}' (botcscripts.com, ID {job.script_pk})")
                path, _ = lookup.download_script(job.script_pk, DOWNLOAD_DIR)
            else:
                job.set_state("running", f"Suche Script '{job.query}' auf botcscripts.com ...")
                job.add_log(f"Suche Script '{job.query}' (zuerst in {DOWNLOAD_DIR.name}/, sonst auf botcscripts.com)")
                path = lookup.find_and_download_script(job.query, DOWNLOAD_DIR)
            script, name = parse_script(Path(path).read_bytes())
        except (RuntimeError, OSError, UploadError) as e:
            job.add_log(str(e))
            job.set_state("error", str(e))
            return False

        _write_script(job.dir, script)
        job.script, job.script_name = script, name
        job.add_log(f"Gefunden: '{name or job.query}' ({Path(path).name})")
        return True

    def _run_botc_tokens(self, job):
        out_dir = job.dir / "out"
        opts = job.options
        cmd = [
            sys.executable, "-m", "botc_tokens", "script", str(job.dir / "script.json"),
            "--token-dir", TOKEN_DIR,
            *PAPER_ARGS,
            "-o", str(out_dir),
            "--logo-provider", opts.logo_provider,
            "--logo-description", opts.logo_description,
            "--name-color", opts.name_color,
        ]
        if opts.blankbg:
            cmd.append("--blankbg")

        env = dict(os.environ)
        src = str(PROJECT_ROOT / "src")
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                    "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "140"})

        job.add_log("$ botc_tokens script " + " ".join(cmd[5:]))
        # cwd = Projektordner: dort liegen .env (OpenAI-Schluessel) und der Logo-Cache logos/.
        # stdin ist gesperrt, damit das Programm nie auf eine Eingabe wartet.
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

        def _kill():
            job.timed_out = True
            proc.kill()

        timer = threading.Timer(JOB_TIMEOUT_SECONDS, _kill)
        timer.start()
        try:
            for raw in proc.stdout:
                line = _ANSI_RE.sub("", raw).rstrip()
                if line.strip():
                    job.add_log(line)
            return proc.wait()
        finally:
            timer.cancel()

    def _finish(self, job, returncode):
        out_dir = job.dir / "out"
        if job.timed_out:
            minutes = max(1, round(JOB_TIMEOUT_SECONDS / 60))
            job.set_state("error", f"Abbruch: Die Erzeugung hat das Zeitlimit von {minutes} Min. ueberschritten.")
            return
        if returncode != 0:
            job.set_state("error", job.last_problem() or f"botc_tokens wurde mit Fehlercode {returncode} beendet.")
            return

        # botc_tokens meldet manche Fehler nur im Text und beendet sich trotzdem mit 0 (z. B. wenn das
        # Logo nicht erzeugt werden konnte). Deshalb wird das Ergebnis selbst gesucht.
        finals = sorted(
            p for p in out_dir.glob("*.pdf")
            if p.name not in RAW_PDFS and ".tmp." not in p.name
        ) if out_dir.is_dir() else []
        if finals:
            job.set_state("done", "Fertig.", result=finals[0])
            return

        raw = [out_dir / name for name in RAW_PDFS if (out_dir / name).is_file()]
        if len(raw) == len(RAW_PDFS):
            archive = job.dir / "script_pdfs.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
                for path in raw:
                    zf.write(path, path.name)
            reason = job.last_problem()
            message = ("Name und Logo konnten nicht eingefuegt werden, deshalb gibt es Vorder- und Rueckseite "
                       "ohne Branding als ZIP.")
            if reason:
                message += f" Hinweis aus dem Log: {reason}"
            job.set_state("done", message, partial=True, result=archive)
            return

        job.set_state("error", job.last_problem() or "Es ist kein PDF entstanden (siehe Log).")

    def _retire_cached_logo(self, job):
        """Legt ein vorhandenes Logo aus dem Cache zur Seite, damit botc_tokens ein neues erzeugt."""
        if not job.options.new_logo:
            return
        if not job.script_name:
            job.add_log("Hinweis: Ohne Scriptnamen im _meta-Eintrag gibt es kein Logo - 'Neues Logo' wird ignoriert.")
            return
        try:
            logo = self.logo_path(job.script_name)
        except ImportError as e:
            job.add_log(f"Hinweis: 'Neues Logo' nicht moeglich, botc_tokens ist hier nicht importierbar ({e}).")
            return
        if logo.is_file():
            backup = logo.with_suffix(".old.png")
            logo.replace(backup)
            job.add_log(f"Altes Logo gesichert als {backup.name}, es wird ein neues erzeugt.")

    # -- Aufraeumen ---------------------------------------------------------------------------
    def drop_owner(self, owner):
        """Vergisst alle Auftraege eines (geloeschten) Kontos und loescht deren Arbeitsordner.

        Laufende Auftraege arbeiten zu Ende, ihr Ergebnis ist aber nicht mehr abrufbar.
        """
        with self._lock:
            dropped = [j for j in self._jobs.values() if j.owner == owner]
            for job in dropped:
                del self._jobs[job.id]
        for job in dropped:
            # Erst markieren, dann loeschen: ein noch laufender Auftrag raeumt nach dem Ende erneut auf.
            job.orphaned = True
            shutil.rmtree(job.dir, ignore_errors=True)


    def cleanup(self):
        """Loescht Jobs, deren Ergebnis laenger als JOB_TTL_SECONDS zurueckliegt (nur Arbeitsordner)."""
        now = time.time()
        with self._lock:
            expired = [j for j in self._jobs.values()
                       if j.finished is not None and now - j.finished > JOB_TTL_SECONDS]
            for job in expired:
                del self._jobs[job.id]
        for job in expired:
            shutil.rmtree(job.dir, ignore_errors=True)
