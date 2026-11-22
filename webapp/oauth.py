"""OAuth 2.1 fuer den MCP-Server: Claude meldet sich damit im Namen eines Benutzerkontos an.

Ablauf (so wie claude.ai, die Claude-Apps und Claude Code es machen):
  1. Claude liest /.well-known/oauth-protected-resource und /.well-known/oauth-authorization-server.
  2. Claude registriert sich selbst als App (POST /oauth/register, "Dynamic Client Registration").
  3. Der Benutzer meldet sich auf /oauth/authorize mit seinem normalen Konto an und stimmt zu.
  4. Claude tauscht den einmaligen Code gegen Zugangs- und Erneuerungstoken (POST /oauth/token, mit PKCE).

Nur freigeschaltete Konten bekommen Token. Codes und Token liegen nur als SHA-256-Hash in der Datenbank;
jeder Benutzer sieht und widerruft seine Verbindungen unter /connections.
"""
# Standard Library
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

# Third Party
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

# Application Specific
from . import users

SCOPE = "pdf"
CODE_LIFETIME = 10 * 60
ACCESS_LIFETIME = 60 * 60
REFRESH_LIFETIME = 30 * 24 * 60 * 60
MAX_REDIRECT_URIS = 5
MAX_CLIENT_NAME = 100

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


def public_base_url():
    """Oeffentliche Basis-URL (ohne / am Ende): aus BOTC_WEB_PUBLIC_URL, sonst aus der Anfrage.

    Hinter einem Proxy unbedingt setzen - Claude prueft, dass die gemeldeten Adressen zusammenpassen.
    """
    configured = os.environ.get("BOTC_WEB_PUBLIC_URL", "").strip().rstrip("/")
    return configured or request.url_root.rstrip("/")


def mcp_resource_url():
    return public_base_url() + "/mcp"


def _format_unix(ts):
    return users.format_time(datetime.fromtimestamp(ts, timezone.utc).isoformat()) if ts else "noch nie"


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now():
    return int(time.time())


def _oauth_error(error, description, status=400, headers=None):
    response = jsonify(error=error, error_description=description)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    response.headers.update(headers or {})
    return response


# -- Datenbank ----------------------------------------------------------------------------------
def get_client(client_id):
    with users.connect() as conn:
        row = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id or "",)).fetchone()
    if row is None:
        return None
    client = dict(row)
    client["redirect_uris"] = json.loads(client["redirect_uris"])
    return client


def verify_access_token(token):
    """Liefert (Konto, Zugang) fuer ein gueltiges Zugangstoken, sonst None."""
    if not token:
        return None
    with users.connect() as conn:
        grant = conn.execute("SELECT * FROM oauth_grants WHERE access_hash = ?", (_hash(token),)).fetchone()
        if grant is None or grant["access_expires"] < _now():
            return None
        conn.execute("UPDATE oauth_grants SET last_used_at = ? WHERE id = ?", (_now(), grant["id"]))
    user = users.get(grant["user_id"])
    if user is None or user["status"] != users.APPROVED or user["must_change_password"]:
        return None
    return user, grant


def list_grants(user_id):
    with users.connect() as conn:
        return conn.execute(
            "SELECT g.id, g.created_at, g.last_used_at, g.refresh_expires, c.client_name, c.redirect_uris "
            "FROM oauth_grants g LEFT JOIN oauth_clients c ON c.client_id = g.client_id "
            "WHERE g.user_id = ? ORDER BY g.created_at DESC", (user_id,)).fetchall()


def revoke_grant(user_id, grant_id):
    with users.connect() as conn:
        return conn.execute("DELETE FROM oauth_grants WHERE id = ? AND user_id = ?",
                            (grant_id, user_id)).rowcount == 1


def _issue_tokens(conn, user_id, client_id, scope, grant_id=None):
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    now = _now()
    values = (_hash(access), now + ACCESS_LIFETIME, _hash(refresh), now + REFRESH_LIFETIME)
    if grant_id is None:
        conn.execute("INSERT INTO oauth_grants (user_id, client_id, scope, access_hash, access_expires, refresh_hash, "
                     "refresh_expires, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (user_id, client_id, scope, *values, now))
    else:  # Erneuerung: alte Token werden ungueltig (Rotation)
        conn.execute("UPDATE oauth_grants SET access_hash = ?, access_expires = ?, refresh_hash = ?, refresh_expires = ? "
                     "WHERE id = ?", (*values, grant_id))
    response = jsonify(access_token=access, token_type="Bearer", expires_in=ACCESS_LIFETIME,
                       refresh_token=refresh, scope=scope)
    response.headers["Cache-Control"] = "no-store"
    return response


def _cleanup(conn):
    now = _now()
    conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
    conn.execute("DELETE FROM oauth_grants WHERE refresh_expires < ?", (now,))


# -- Pruefungen -----------------------------------------------------------------------------------
def _valid_redirect_uri(uri):
    """https://..., oder http://localhost/127.0.0.1 fuer lokale Programme wie Claude Code."""
    if not isinstance(uri, str) or len(uri) > 2000:
        return False
    parts = urlsplit(uri)
    if parts.fragment or not parts.netloc:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


def _client_secret_ok(client, presented):
    if not client["client_secret_hash"]:
        return True  # oeffentliche App (z. B. Claude Code): Schutz kommt von PKCE
    return bool(presented) and hmac.compare_digest(_hash(presented), client["client_secret_hash"])


def _pkce_ok(verifier, challenge):
    if not verifier or not 43 <= len(verifier) <= 128:
        return False
    digest = hashlib.sha256(verifier.encode("ascii", "ignore")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, challenge)


def _redirect_with(redirect_uri, **params):
    params = {k: v for k, v in params.items() if v is not None}
    separator = "&" if urlsplit(redirect_uri).query else "?"
    return redirect(redirect_uri + separator + urlencode(params), code=302)


def create_blueprint(login_required, register_limiter):
    bp = Blueprint("oauth", __name__)

    # -- Metadaten -----------------------------------------------------------------------------
    @bp.get("/.well-known/oauth-protected-resource")
    @bp.get("/.well-known/oauth-protected-resource/mcp")
    def protected_resource_metadata():
        return jsonify(resource=mcp_resource_url(), authorization_servers=[public_base_url()],
                       scopes_supported=[SCOPE], bearer_methods_supported=["header"],
                       resource_name="BotC Script-PDF")

    @bp.get("/.well-known/oauth-authorization-server")
    def authorization_server_metadata():
        base = public_base_url()
        return jsonify(
            issuer=base,
            authorization_endpoint=base + "/oauth/authorize",
            token_endpoint=base + "/oauth/token",
            registration_endpoint=base + "/oauth/register",
            scopes_supported=[SCOPE],
            response_types_supported=["code"],
            grant_types_supported=["authorization_code", "refresh_token"],
            code_challenge_methods_supported=["S256"],
            token_endpoint_auth_methods_supported=["none", "client_secret_post", "client_secret_basic"],
            service_documentation=base + "/api/v1/docs.html",
        )

    # -- Registrierung (Dynamic Client Registration, RFC 7591) ---------------------------------
    @bp.post("/oauth/register")
    def register_client():
        ip_key = "oauth-register:" + (request.remote_addr or "")
        if register_limiter.blocked(ip_key):
            return _oauth_error("invalid_client_metadata", "Zu viele Registrierungen. Bitte spaeter erneut.", 429)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _oauth_error("invalid_client_metadata", "JSON-Objekt erwartet.")
        uris = body.get("redirect_uris")
        if (not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS
                or not all(_valid_redirect_uri(u) for u in uris)):
            return _oauth_error("invalid_redirect_uri",
                                "redirect_uris: 1-5 Adressen, https oder http://localhost bzw. 127.0.0.1.")
        grant_types = body.get("grant_types") or ["authorization_code"]
        if not isinstance(grant_types, list) or not set(grant_types) <= {"authorization_code", "refresh_token"}:
            return _oauth_error("invalid_client_metadata", "Nur authorization_code und refresh_token.")
        response_types = body.get("response_types") or ["code"]
        if response_types != ["code"]:
            return _oauth_error("invalid_client_metadata", "Nur response_type 'code'.")
        auth_method = body.get("token_endpoint_auth_method") or "none"
        if auth_method not in ("none", "client_secret_post", "client_secret_basic"):
            return _oauth_error("invalid_client_metadata", "Nicht unterstuetzte token_endpoint_auth_method.")
        name = _CONTROL_RE.sub(" ", str(body.get("client_name") or "")).strip()[:MAX_CLIENT_NAME] or "Unbenannte App"

        client_id = secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32) if auth_method != "none" else None
        with users.connect() as conn:
            conn.execute("INSERT INTO oauth_clients (client_id, client_secret_hash, client_name, redirect_uris, "
                         "created_at) VALUES (?, ?, ?, ?, ?)",
                         (client_id, _hash(secret) if secret else None, name, json.dumps(uris), _now()))
        register_limiter.hit(ip_key)
        result = {"client_id": client_id, "client_id_issued_at": _now(), "client_name": name,
                  "redirect_uris": uris, "grant_types": grant_types, "response_types": ["code"],
                  "token_endpoint_auth_method": auth_method, "scope": SCOPE}
        if secret:
            result.update(client_secret=secret, client_secret_expires_at=0)
        response = jsonify(result)
        response.status_code = 201
        response.headers["Cache-Control"] = "no-store"
        return response

    # -- Zustimmung (Authorization Endpoint) ----------------------------------------------------
    def _authorize_request():
        """Prueft die Parameter. Liefert (params, client) oder eine fertige Fehlerantwort."""
        src = request.form if request.method == "POST" else request.args
        params = {k: src.get(k) for k in ("response_type", "client_id", "redirect_uri", "state", "scope",
                                          "code_challenge", "code_challenge_method", "resource")}
        client = get_client(params["client_id"])
        if client is None:
            return None, render_template("oauth_error.html", message="Diese App ist hier nicht registriert."), 400
        if not params["redirect_uri"] and len(client["redirect_uris"]) == 1:
            params["redirect_uri"] = client["redirect_uris"][0]
        # Ohne passende Ruecksprungadresse wird nie weitergeleitet (sonst offene Weiterleitung).
        if params["redirect_uri"] not in client["redirect_uris"]:
            return None, render_template("oauth_error.html",
                                         message="Die Ruecksprungadresse passt nicht zu dieser App."), 400
        error = None
        if params["response_type"] != "code":
            error = ("unsupported_response_type", "Nur response_type=code.")
        elif not params["code_challenge"] or params["code_challenge_method"] != "S256":
            error = ("invalid_request", "PKCE mit S256 ist Pflicht.")
        elif params["resource"] and params["resource"].rstrip("/") not in (mcp_resource_url(), public_base_url()):
            error = ("invalid_target", "Unbekannte Ressource.")
        elif params["scope"] and SCOPE not in params["scope"].split():
            error = ("invalid_scope", f"Verfuegbar ist nur der Scope '{SCOPE}'.")
        if error:
            return None, _redirect_with(params["redirect_uri"], error=error[0], error_description=error[1],
                                        state=params["state"]), None
        return (params, client), None, None

    @bp.route("/oauth/authorize", methods=["GET", "POST"])
    def authorize():
        ok, response, status = _authorize_request()
        if ok is None:
            return (response, status) if status else response
        params, client = ok
        if g.user is None:
            # Erst anmelden, danach geht es mit denselben Parametern hier weiter.
            return redirect(url_for("login", next=request.full_path if request.method == "GET" else None))

        if request.method == "GET":
            return render_template("oauth_consent.html", params=params, client=client,
                                   redirect_host=urlsplit(params["redirect_uri"]).netloc)

        if request.form.get("decision") != "allow":
            return _redirect_with(params["redirect_uri"], error="access_denied",
                                  error_description="Der Benutzer hat den Zugriff abgelehnt.", state=params["state"])
        code = secrets.token_urlsafe(32)
        with users.connect() as conn:
            _cleanup(conn)
            conn.execute("INSERT INTO oauth_codes (code_hash, client_id, user_id, redirect_uri, code_challenge, scope, "
                         "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (_hash(code), client["client_id"], g.user["id"], params["redirect_uri"],
                          params["code_challenge"], SCOPE, _now() + CODE_LIFETIME))
        return _redirect_with(params["redirect_uri"], code=code, state=params["state"], iss=public_base_url())

    # -- Token ----------------------------------------------------------------------------------
    @bp.post("/oauth/token")
    def token():
        form = request.form
        client_id, client_secret = form.get("client_id"), form.get("client_secret")
        auth = request.authorization
        if auth is not None and auth.type == "basic":  # client_secret_basic
            client_id, client_secret = auth.username, auth.password
        client = get_client(client_id)
        if client is None or not _client_secret_ok(client, client_secret):
            return _oauth_error("invalid_client", "Unbekannte App oder falsches Geheimnis.", 401)

        grant_type = form.get("grant_type")
        with users.connect() as conn:
            if grant_type == "authorization_code":
                row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?",
                                   (_hash(form.get("code") or ""),)).fetchone()
                if row is not None:
                    conn.execute("DELETE FROM oauth_codes WHERE code_hash = ?", (row["code_hash"],))  # nur einmal
                if (row is None or row["expires_at"] < _now() or row["client_id"] != client["client_id"]
                        or row["redirect_uri"] != (form.get("redirect_uri") or row["redirect_uri"])):
                    return _oauth_error("invalid_grant", "Code ungueltig, abgelaufen oder schon benutzt.")
                if not _pkce_ok(form.get("code_verifier"), row["code_challenge"]):
                    return _oauth_error("invalid_grant", "PKCE-Pruefung fehlgeschlagen.")
                user = users.get(row["user_id"])
                if user is None or user["status"] != users.APPROVED:
                    return _oauth_error("invalid_grant", "Das Konto ist nicht (mehr) freigegeben.")
                return _issue_tokens(conn, user["id"], client["client_id"], row["scope"])

            if grant_type == "refresh_token":
                grant = conn.execute("SELECT * FROM oauth_grants WHERE refresh_hash = ?",
                                     (_hash(form.get("refresh_token") or ""),)).fetchone()
                if grant is None or grant["refresh_expires"] < _now() or grant["client_id"] != client["client_id"]:
                    return _oauth_error("invalid_grant", "Erneuerungstoken ungueltig oder abgelaufen.")
                user = users.get(grant["user_id"])
                if user is None or user["status"] != users.APPROVED:
                    return _oauth_error("invalid_grant", "Das Konto ist nicht (mehr) freigegeben.")
                return _issue_tokens(conn, user["id"], client["client_id"], grant["scope"], grant_id=grant["id"])

        return _oauth_error("unsupported_grant_type", "Nur authorization_code und refresh_token.")

    # -- Verbindungen verwalten -------------------------------------------------------------------
    @bp.get("/connections")
    @login_required
    def connections():
        rows = []
        for r in list_grants(g.user["id"]):
            uris = json.loads(r["redirect_uris"] or "[]")
            rows.append({"id": r["id"], "name": r["client_name"] or "Unbekannte App",
                         "host": urlsplit(uris[0]).netloc if uris else "",
                         "created_at": _format_unix(r["created_at"]),
                         "last_used_at": _format_unix(r["last_used_at"])})
        return render_template("connections.html", rows=rows, mcp_url=mcp_resource_url())

    @bp.post("/connections/<int:grant_id>/revoke")
    @login_required
    def revoke(grant_id):
        if revoke_grant(g.user["id"], grant_id):
            flash("Verbindung getrennt. Die App muss sich neu anmelden, um wieder zuzugreifen.", "ok")
        else:
            abort(404)
        return redirect(url_for("oauth.connections"))

    return bp
