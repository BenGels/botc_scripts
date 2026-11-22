"""MCP-Server (Model Context Protocol) fuer Claude: Script-PDFs erzeugen als Werkzeug.

Transport: "Streamable HTTP" ohne Sitzung - jede Anfrage ist ein JSON-RPC-POST auf /mcp, die Antwort ist
JSON. Anmeldung per OAuth-Zugangstoken (siehe oauth.py); ohne gueltiges Token antwortet /mcp mit 401 und
verweist auf die OAuth-Metadaten, damit Claude den Login selbst startet.

Werkzeuge:
  create_script_pdf      Auftrag anlegen, bis WAIT_SECONDS warten, Download-Link oder Auftrags-Hash liefern
  get_script_pdf_status  Stand eines Auftrags, fertig -> Download-Link

Das PDF selbst gibt es ueber einen signierten, zeitlich begrenzten Link (/dl/<token>), den der Benutzer im
Chat anklickt - ohne erneute Anmeldung, aber nur fuer seinen eigenen Auftrag und nur fuer kurze Zeit.
"""
# Standard Library
from urllib.parse import urlsplit
import time

# Third Party
from flask import Blueprint, abort, current_app, jsonify, request, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Application Specific
from . import api, jobs, oauth

SERVER_NAME = "botc-script-pdf"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
# Cloudflare bricht Anfragen nach 100 s ab - darunter bleiben, laengere Laeufe per Status nachfragen.
WAIT_SECONDS = 60
DOWNLOAD_LINK_SECONDS = 60 * 60

INSTRUCTIONS = (
    "Erzeugt druckfertige, doppelseitige Script-PDFs fuer Blood on the Clocktower (Vorderseite: Rollen, "
    "Rueckseite: Nachtreihenfolge, mit Scriptname und Logo). Ein Script per Name von botcscripts.com, "
    "per botcscripts.com-ID oder als Script-JSON uebergeben. Meldet create_script_pdf, dass der Auftrag noch "
    "laeuft, nach etwa 30 Sekunden mit get_script_pdf_status nachfragen. Den Download-Link dem Benutzer als "
    "anklickbaren Link zeigen; er ist 1 Stunde gueltig."
)

_OPTION_PROPS = {
    "name_color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$",
                   "description": "Schriftfarbe des Scriptnamens als Hex-Wert, Standard #000000."},
    "logo_description": {"type": "string", "maxLength": 500,
                         "description": "Beschreibung fuer ein neu zu erzeugendes Logo (Motiv, Stimmung)."},
    "logo_provider": {"type": "string", "enum": ["pollinations", "openai"],
                      "description": "Wer ein neues Logo erzeugt: pollinations (kostenlos, Standard) oder openai "
                                     "(bessere Qualitaet; fuer normale Benutzer 5 neue Logos pro 7 Tage)."},
    "blank_background": {"type": "boolean", "description": "Druckerfreundlicher, leerer Hintergrund."},
    "new_logo": {"type": "boolean",
                 "description": "Neues Logo erzeugen, auch wenn fuer das Script schon eins vorhanden ist."},
}

TOOLS = [
    {
        "name": "create_script_pdf",
        "title": "Script-PDF erzeugen",
        "description": (
            "Erzeugt ein Blood-on-the-Clocktower-Script-PDF. Genau eine Quelle angeben: script_name (Suche auf "
            "botcscripts.com, neueste Version), script_id (genau diese Version auf botcscripts.com) oder "
            "script_json (Script im Format des offiziellen Script-Tools). Wartet bis zu einer Minute; danach "
            "kommt entweder der Download-Link oder ein job_id zum Nachfragen mit get_script_pdf_status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "script_name": {"type": "string", "description": "Name des Scripts, z. B. \"Trouble Brewing\"."},
                "script_id": {"type": "integer", "minimum": 1,
                              "description": "Interne Nummer (pk) einer Script-Version auf botcscripts.com."},
                "script_json": {"type": "array", "items": {"type": ["string", "object"]},
                                "description": "Script-JSON: erster Eintrag {\"id\": \"_meta\", \"name\": ...}, "
                                               "danach Rollen-IDs wie \"imp\", \"washerwoman\"."},
                **_OPTION_PROPS,
            },
            "additionalProperties": False,
        },
        "annotations": {"title": "Script-PDF erzeugen", "readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "get_script_pdf_status",
        "title": "Status eines Script-PDFs",
        "description": "Fragt den Stand eines Auftrags von create_script_pdf ab; ist er fertig, mit Download-Link.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "pattern": "^[0-9a-f]{32}$",
                                      "description": "Der job_id aus create_script_pdf."}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Status eines Script-PDFs", "readOnlyHint": True, "openWorldHint": False},
    },
]


class ToolError(Exception):
    """Fehler, den Claude als Werkzeug-Ergebnis (isError) sehen soll."""


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="botc-pdf-download")


def download_url(job):
    token = _serializer().dumps({"j": job.id, "u": job.owner})
    return f"{oauth.public_base_url()}/dl/{token}"


def _job_result(job):
    data = {"job_id": job.id, "status": job.status, "message": job.message}
    if job.status == "done" and job.result is not None:
        data.update(filename=job.result.name, download_url=download_url(job),
                    download_expires_in_minutes=DOWNLOAD_LINK_SECONDS // 60, partial=job.partial)
        if job.partial:
            text = (f"Teilweise fertig: {job.message}\nDownload (ZIP mit Vorder- und Rueckseite, 1 Stunde gueltig): "
                    f"{data['download_url']}")
        else:
            text = f"Fertig: {job.result.name}\nDownload (1 Stunde gueltig): {data['download_url']}"
        return text, data, False
    if job.status == "error":
        data["code"] = job.error_code or "job_failed"
        return f"Fehlgeschlagen: {job.message}", data, True
    return (f"Laeuft noch ({job.message}). job_id: {job.id} - in etwa 30 Sekunden mit get_script_pdf_status "
            f"nachfragen."), data, False


def create_blueprint(manager, logo_quota):
    bp = Blueprint("mcp", __name__)

    def _own_job(user_id, job_id):
        job = manager.get(job_id) if isinstance(job_id, str) and jobs.JOB_ID_RE.match(job_id) else None
        if job is None or job.owner != user_id:
            raise ToolError("Auftrag nicht gefunden oder abgelaufen (Ergebnisse gibt es 1 Stunde).")
        return job

    # -- Werkzeuge ------------------------------------------------------------------------------
    def tool_create(user, args):
        sources = [k for k in ("script_name", "script_id", "script_json") if args.get(k) not in (None, "")]
        if len(sources) != 1:
            raise ToolError("Bitte genau eines angeben: script_name, script_id oder script_json.")
        body = {"options": {k: args[k] for k in _OPTION_PROPS if k in args}}
        key = {"script_json": "script"}.get(sources[0], sources[0])
        body[key] = args[sources[0]]
        unknown = set(args) - set(_OPTION_PROPS) - {"script_name", "script_id", "script_json"}
        if unknown:
            raise ToolError(f"Unbekannte Angaben: {', '.join(sorted(unknown))}.")
        try:
            job = api.submit_pdf_job(manager, logo_quota, user["id"], body)
        except api.ApiError as e:
            raise ToolError(e.message) from None
        deadline = time.monotonic() + WAIT_SECONDS
        while job.status not in ("done", "error") and time.monotonic() < deadline:
            time.sleep(1)
        return _job_result(job)

    def tool_status(user, args):
        return _job_result(_own_job(user["id"], args.get("job_id")))

    handlers = {"create_script_pdf": tool_create, "get_script_pdf_status": tool_status}

    # -- JSON-RPC ---------------------------------------------------------------------------------
    def _rpc_result(msg_id, result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _rpc_error(msg_id, code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def _handle(user, msg):
        """Eine JSON-RPC-Nachricht; None fuer Benachrichtigungen (ohne Antwort)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        method, msg_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if "id" not in msg:
            return None  # Benachrichtigung, z. B. notifications/initialized
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return _rpc_result(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "BotC Script-PDF", "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _rpc_result(msg_id, {})
        if method == "tools/list":
            return _rpc_result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            handler = handlers.get(name)
            if handler is None:
                return _rpc_error(msg_id, -32602, f"Unbekanntes Werkzeug: {name}")
            if not isinstance(args, dict):
                return _rpc_error(msg_id, -32602, "arguments muss ein Objekt sein.")
            try:
                text, data, is_error = handler(user, args)
            except ToolError as e:
                text, data, is_error = str(e), {"error": str(e)}, True
            return _rpc_result(msg_id, {"content": [{"type": "text", "text": text}],
                                        "structuredContent": data, "isError": is_error})
        return _rpc_error(msg_id, -32601, f"Methode nicht unterstuetzt: {method}")

    def _unauthorized(description):
        metadata = oauth.public_base_url() + "/.well-known/oauth-protected-resource/mcp"
        response = jsonify(error="invalid_token", error_description=description)
        response.status_code = 401
        response.headers["WWW-Authenticate"] = (f'Bearer resource_metadata="{metadata}", '
                                                f'error="invalid_token", error_description="{description}"')
        return response

    @bp.route("/mcp", methods=["POST"])
    def endpoint():
        # Schutz vor DNS-Rebinding: Browser-Anfragen nur von der eigenen Adresse.
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc not in (urlsplit(oauth.public_base_url()).netloc, request.host):
            return jsonify(error="Origin nicht erlaubt."), 403

        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        verified = oauth.verify_access_token(token)
        if verified is None:
            return _unauthorized("Anmeldung erforderlich" if not token else "Token ungueltig oder abgelaufen")
        user, _grant = verified

        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify(_rpc_error(None, -32700, "Parse error")), 400
        if isinstance(payload, list):  # Stapel (aeltere Protokollversion)
            replies = [r for r in (_handle(user, m) for m in payload) if r is not None]
            return (jsonify(replies), 200) if replies else ("", 202)
        reply = _handle(user, payload)
        return (jsonify(reply), 200) if reply is not None else ("", 202)

    @bp.route("/mcp", methods=["GET", "DELETE"])
    def endpoint_other():
        # Kein Server-Push-Stream und keine Sitzungen: nur POST.
        response = jsonify(error="Nur POST (Streamable HTTP ohne Sitzung).")
        response.status_code = 405
        response.headers["Allow"] = "POST"
        return response

    # -- Download per signiertem Link -------------------------------------------------------------
    @bp.get("/dl/<token>")
    def download(token):
        try:
            data = _serializer().loads(token, max_age=DOWNLOAD_LINK_SECONDS)
        except SignatureExpired:
            abort(410)
        except BadSignature:
            abort(404)
        job = manager.get(data.get("j", "")) if isinstance(data, dict) else None
        if job is None or job.owner != data.get("u") or job.status != "done" or job.result is None \
                or not job.result.is_file():
            abort(404)
        mimetype = "application/zip" if job.result.suffix == ".zip" else "application/pdf"
        return send_file(job.result, mimetype=mimetype, as_attachment=True, download_name=job.result.name)

    return bp
