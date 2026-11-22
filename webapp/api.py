"""REST-API v1: Script-PDFs per HTTP Basic Auth erzeugen (Dokumentation: /api/v1/docs.html).

    POST /api/v1/pdfs                 Auftrag anlegen (JSON), mit ?wait=1 direkt das PDF
    GET  /api/v1/pdfs/<id>            Status eines Auftrags
    GET  /api/v1/pdfs/<id>/download   Ergebnis herunterladen

Angemeldet wird bei jeder Anfrage mit den normalen Login-Daten (E-Mail bzw. Benutzername und Passwort).
Es gibt keine Cookies; POST verlangt Content-Type application/json, damit fremde Seiten keine Auftraege
im Namen eines Browsers mit gespeicherten Basic-Auth-Daten abschicken koennen (das geht ohne CORS nicht).
"""
# Standard Library
import json
import time

# Third Party
from flask import Blueprint, g, jsonify, render_template, request, send_file, url_for

# Application Specific
from . import jobs, users

API_PREFIX = "/api/v1"
WAIT_MAX_SECONDS = 300  # ?wait=1 haelt die Verbindung hoechstens so lange offen
WAIT_POLL_SECONDS = 1.0

_SOURCES = ("script", "script_name", "script_id")
_OPTION_TYPES = {
    "name_color": str,
    "logo_description": str,
    "logo_provider": str,
    "blank_background": bool,
    "new_logo": bool,
}


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def parse_pdf_body(body):
    """Prueft eine Auftragsbeschreibung {script|script_name|script_id, options} (REST-API und MCP).

    Liefert (data, query, script_pk, options) fuer JobManager.submit oder wirft ApiError.
    """
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "Der Anfrage-Inhalt muss ein JSON-Objekt sein.")
    unknown = set(body) - set(_SOURCES) - {"options"}
    if unknown:
        raise ApiError(400, "invalid_field", f"Unbekannte Felder: {', '.join(sorted(unknown))}.")
    given = [k for k in _SOURCES if body.get(k) not in (None, "")]
    if len(given) != 1:
        raise ApiError(400, "invalid_source", "Bitte genau eines angeben: script, script_name oder script_id.")

    raw_options = body.get("options") or {}
    if not isinstance(raw_options, dict):
        raise ApiError(400, "invalid_field", "options muss ein JSON-Objekt sein.")
    for key, value in raw_options.items():
        expected = _OPTION_TYPES.get(key)
        if expected is None:
            raise ApiError(400, "invalid_field", f"Unbekannte Option: {key}.")
        if not isinstance(value, expected):
            kind = "true/false" if expected is bool else "Text"
            raise ApiError(400, "invalid_field", f"Option {key} muss {kind} sein.")
    try:
        options = jobs.Options(
            name_color=raw_options.get("name_color", ""),
            logo_description=raw_options.get("logo_description", ""),
            logo_provider=raw_options.get("logo_provider", "pollinations"),
            blankbg=raw_options.get("blank_background", False),
            new_logo=raw_options.get("new_logo", False),
        )
    except jobs.UploadError as e:
        raise ApiError(400, "invalid_field", str(e)) from None

    source = given[0]
    value = body[source]
    data = query = script_pk = None
    if source == "script":
        if not isinstance(value, list):
            raise ApiError(400, "invalid_script", "script muss die Script-JSON als Liste sein.")
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    elif source == "script_name":
        if not isinstance(value, str):
            raise ApiError(400, "invalid_field", "script_name muss Text sein.")
        query = value
    else:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ApiError(400, "invalid_field", "script_id muss eine positive Zahl sein (pk von botcscripts.com).")
        script_pk, query = str(value), f"botcscripts.com #{value}"
    return data, query, script_pk, options


def submit_pdf_job(manager, logo_quota, user_id, body):
    """Prueft die Auftragsbeschreibung, das OpenAI-Limit und legt den Auftrag an (oder wirft ApiError)."""
    data, query, script_pk, options = parse_pdf_body(body)
    # Bei mitgeschickter Script-JSON steht der Name fest: ein erschoepftes OpenAI-Limit gleich melden.
    if data is not None and options.logo_provider == "openai" and logo_quota.remaining(user_id) == 0:
        try:
            _, name = jobs.parse_script(data)
        except jobs.UploadError as e:
            raise ApiError(400, "invalid_script", str(e)) from None
        if manager.will_generate_openai_logo(name, options):
            raise ApiError(429, "quota_exceeded", logo_quota.exhausted_message(user_id))
    try:
        return manager.submit(data, options, query=query, script_pk=script_pk, owner=user_id)
    except jobs.UploadError as e:
        raise ApiError(400, "invalid_script", str(e)) from None


def create_blueprint(manager, logo_quota, login_limiter):
    bp = Blueprint("api_v1", __name__, url_prefix=API_PREFIX)

    # -- Hilfen ---------------------------------------------------------------------------------
    @bp.errorhandler(ApiError)
    def _api_error(e):
        response = jsonify(error=e.message, code=e.code)
        response.status_code = e.status
        response.headers.update(e.headers)
        return response

    def _authenticate():
        """Prueft Basic Auth bei jeder Anfrage; setzt g.api_user oder wirft ApiError."""
        auth = request.authorization
        challenge = {"WWW-Authenticate": 'Basic realm="BotC Script-PDF API", charset="UTF-8"'}
        if auth is None or auth.type != "basic" or not auth.username:
            raise ApiError(401, "unauthorized", "Anmeldung erforderlich (HTTP Basic Auth mit E-Mail und Passwort).",
                           challenge)
        keys = ("email:" + users.normalize_login(auth.username), "ip:" + (request.remote_addr or ""))
        if any(login_limiter.blocked(k) for k in keys):
            raise ApiError(429, "too_many_attempts",
                           "Zu viele fehlgeschlagene Anmeldungen. Bitte in 15 Minuten noch einmal versuchen.")
        row = users.authenticate(auth.username, auth.password or "")
        if row is None:
            for k in keys:
                login_limiter.hit(k)
            raise ApiError(401, "unauthorized", "E-Mail-Adresse oder Passwort ist falsch.", challenge)
        login_limiter.reset(keys[0])
        if row["status"] != users.APPROVED:
            raise ApiError(403, "account_pending", "Das Konto ist noch nicht freigegeben.")
        if row["must_change_password"]:
            raise ApiError(403, "password_change_required",
                           "Bitte zuerst in der Web-Oberflaeche ein eigenes Passwort setzen.")
        g.api_user = row

    def _own_job(job_id):
        job = manager.get(job_id) if jobs.JOB_ID_RE.match(job_id) else None
        if job is None or job.owner != g.api_user["id"]:
            raise ApiError(404, "not_found", "Auftrag nicht gefunden oder abgelaufen (Ergebnisse gibt es 1 Stunde).")
        return job

    def _job_json(job, status=200):
        data = job.public()
        data.pop("download_url", None)  # Pfad der Web-Oberflaeche, hier durch die API-Links ersetzt
        data["status_url"] = url_for("api_v1.pdf_status", job_id=job.id)
        if job.status == "done" and job.result is not None:
            data["download_url"] = url_for("api_v1.pdf_download", job_id=job.id)
        if job.status == "error":
            data["code"] = job.error_code or "job_failed"
        response = jsonify(data)
        response.status_code = status
        if status == 202:
            response.headers["Location"] = data["status_url"]
        return response

    def _send_result(job):
        mimetype = "application/zip" if job.result.suffix == ".zip" else "application/pdf"
        return send_file(job.result, mimetype=mimetype, as_attachment=True, download_name=job.result.name)

    def _parse_request():
        if not request.is_json:
            raise ApiError(415, "unsupported_media_type", "Bitte JSON senden (Content-Type: application/json).")
        return request.get_json(silent=True)

    # -- Endpunkte ------------------------------------------------------------------------------
    @bp.post("/pdfs")
    def pdf_create():
        _authenticate()
        job = submit_pdf_job(manager, logo_quota, g.api_user["id"], _parse_request())

        if request.args.get("wait") not in ("1", "true", "yes"):
            return _job_json(job, 202)

        deadline = time.monotonic() + WAIT_MAX_SECONDS
        while job.status not in ("done", "error") and time.monotonic() < deadline:
            time.sleep(WAIT_POLL_SECONDS)
        if job.status == "done" and job.result is not None and job.result.is_file():
            response = _send_result(job)
            response.headers["X-Job-Id"] = job.id
            return response
        if job.status == "error":
            return _job_json(job, 422)
        return _job_json(job, 202)  # noch nicht fertig: Status-URL weiter abfragen

    @bp.get("/pdfs/<job_id>")
    def pdf_status(job_id):
        _authenticate()
        return _job_json(_own_job(job_id))

    @bp.get("/pdfs/<job_id>/download")
    def pdf_download(job_id):
        _authenticate()
        job = _own_job(job_id)
        if job.status == "error":
            raise ApiError(409, "job_failed", f"Der Auftrag ist fehlgeschlagen: {job.message}")
        if job.status != "done" or job.result is None or not job.result.is_file():
            raise ApiError(409, "not_ready", "Das Ergebnis ist noch nicht fertig. Bitte den Status abfragen.")
        return _send_result(job)

    @bp.get("/docs.html")
    def docs():
        return render_template("api_docs.html", base_url=request.host_url.rstrip("/") + API_PREFIX,
                               wait_max=WAIT_MAX_SECONDS, max_upload_mb=jobs.MAX_UPLOAD_BYTES // 1024 // 1024)

    return bp
