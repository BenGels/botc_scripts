"""WebApp: Script-JSON hochladen oder Scriptnamen suchen, Script-PDF erzeugen lassen, PDF herunterladen.

Start (im Projektordner, mit der Umgebung des Projekts):

    .venv/bin/pip install -r webapp/requirements.txt
    .venv/bin/python -m webapp.app

Danach im Browser http://127.0.0.1:8090 oeffnen. Nutzen kann die App nur, wer sich registriert hat und
von einem Admin freigegeben wurde (Benutzerverwaltung unter /admin). Gibt es beim Start keinen Admin, wird
das Konto "admin" mit dem Passwort "deleteme!" angelegt; es muss bei der ersten Anmeldung geaendert werden.
"""
# Standard Library
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps
import hmac
import os
import secrets
import threading
import time

# Third Party
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session,
                   url_for)

# Application Specific
from . import api, jobs, mcp, oauth, quota, users

app = Flask(__name__)
# Etwas Luft ueber dem Upload-Limit, damit die eigene, verstaendliche Meldung greift.
app.config["MAX_CONTENT_LENGTH"] = jobs.MAX_UPLOAD_BYTES + 64 * 1024
app.config.update(
    SECRET_KEY=users.secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Hinter einem HTTPS-Proxy auf 1 setzen, damit das Cookie nur ueber HTTPS geschickt wird.
    SESSION_COOKIE_SECURE=os.environ.get("BOTC_WEB_SECURE_COOKIES", "") in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)

if os.environ.get("BOTC_WEB_TRUST_PROXY", "") in ("1", "true", "yes"):
    # Hinter genau einem Reverse-Proxy: echte Client-IP (fuer die Login-Sperre) und HTTPS aus dessen Headern.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


class TrustedProxyRealIP:
    """Uebernimmt die echte Client-IP nur von eingetragenen Proxys (BOTC_WEB_TRUSTED_PROXIES).

    Kommt eine Anfrage von dort, gilt CF-Connecting-IP (Cloudflare) bzw. der letzte Eintrag in X-Forwarded-For.
    Von allen anderen Absendern werden diese Header ignoriert, sie lassen sich also nicht faelschen.
    """

    def __init__(self, wsgi_app, proxies):
        self.wsgi_app = wsgi_app
        self.proxies = proxies

    def __call__(self, environ, start_response):
        if environ.get("REMOTE_ADDR") in self.proxies:
            real = environ.get("HTTP_CF_CONNECTING_IP", "").strip()
            if not real:
                forwarded = [p.strip() for p in environ.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
                real = forwarded[-1] if forwarded else ""
            if real:
                environ["botc.proxy_addr"] = environ["REMOTE_ADDR"]
                environ["REMOTE_ADDR"] = real
        return self.wsgi_app(environ, start_response)


_trusted_proxies = {p.strip() for p in os.environ.get("BOTC_WEB_TRUSTED_PROXIES", "").split(",") if p.strip()}
if _trusted_proxies:
    app.wsgi_app = TrustedProxyRealIP(app.wsgi_app, _trusted_proxies)

if users.ensure_admin():
    app.logger.warning("Kein Admin vorhanden: Konto '%s' mit dem Standardpasswort angelegt. Bitte sofort anmelden "
                       "und das Passwort aendern.", users.DEFAULT_ADMIN_LOGIN)

logo_quota = quota.OpenAILogoQuota()
manager = jobs.JobManager(logo_quota=logo_quota)
script_search = jobs.ScriptSearch()

_JOB_ID_RE = jobs.JOB_ID_RE


class RateLimiter:
    """Zaehlt Versuche pro Schluessel in einem Zeitfenster (im Speicher, reicht fuer einen Prozess)."""

    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window = window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key, now):
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if not hits:
            del self._hits[key]
        return hits

    def blocked(self, key):
        with self._lock:
            return len(self._prune(key, time.time())) >= self.limit

    def hit(self, key):
        with self._lock:
            self._hits[key].append(time.time())

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)


login_limiter = RateLimiter(limit=5, window_seconds=15 * 60)        # Fehlversuche pro E-Mail und pro IP
register_limiter = RateLimiter(limit=5, window_seconds=60 * 60)     # Registrierungen pro IP
# App-Registrierungen fuer OAuth/MCP: claude.ai registriert sich von wenigen gemeinsamen Adressen aus.
oauth_register_limiter = RateLimiter(limit=60, window_seconds=60 * 60)

# Endpunkte ohne Browser-Sitzung (eigene Anmeldung per Token, oder oeffentlich): kein CSRF-Token.
_STATELESS_ENDPOINTS = {
    "mcp.endpoint", "mcp.endpoint_other", "mcp.download",
    "oauth.token", "oauth.register_client",
    "oauth.protected_resource_metadata", "oauth.authorization_server_metadata",
}


def _checked(value):
    """Haekchen aus einem HTML-Formular: fehlt oder 'on'/'1'/'true'."""
    return (value or "").strip().lower() in ("on", "1", "true", "yes")


# -- Anmeldung und CSRF-Schutz ------------------------------------------------------------------
def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = session["csrf_token"] = secrets.token_urlsafe(32)
    return token


app.jinja_env.globals["csrf_token"] = _csrf_token
app.jinja_env.filters["localtime"] = users.format_time


@app.before_request
def _load_user_and_check_csrf():
    g.user = None
    # Die REST-API meldet sich per Basic Auth bei jeder Anfrage an (siehe api.py): keine Sitzung, kein CSRF-Token.
    # Nur ihre Dokumentation ist eine normale Seite.
    if request.path.startswith(api.API_PREFIX + "/") and request.endpoint != "api_v1.docs":
        return None
    if request.endpoint in _STATELESS_ENDPOINTS:
        return None
    user_id = session.get("user_id")
    if user_id is not None:
        row = users.get(user_id)
        # Konto geloescht oder (noch) nicht freigegeben: Sitzung gilt nicht mehr.
        if row is None or row["status"] != users.APPROVED:
            session.pop("user_id", None)
        else:
            g.user = row

    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf_token") or ""
        if not expected or not hmac.compare_digest(sent, expected):
            if request.path.startswith("/api/"):
                return jsonify(error="Sitzung abgelaufen. Bitte die Seite neu laden."), 400
            flash("Sitzung abgelaufen, bitte noch einmal versuchen.", "error")
            return redirect(request.path if request.endpoint in ("login", "register", "password", "admin")
                            else url_for("index"))

    # Mit dem Standardpasswort (oder nach Zuruecksetzen) geht nichts, bis das Passwort geaendert ist.
    if g.user is not None and g.user["must_change_password"] and request.endpoint not in ("password", "logout",
                                                                                          "static"):
        if request.path.startswith("/api/"):
            return jsonify(error="Bitte zuerst das Passwort aendern."), 403
        return redirect(url_for("password"))
    return None


@app.after_request
def _no_store_pages(response):
    # Seiten nie aus dem Browser-Cache zeigen: sonst bleiben nach einem Update alte Formulare stehen, und
    # angemeldete Seiten (z. B. Benutzerverwaltung) waeren nach dem Abmelden ueber "Zurueck" noch sichtbar.
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    # Nie in fremde Seiten einbetten (Clickjacking, v. a. Zustimmung fuer Claude und Benutzerverwaltung).
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Bitte zuerst anmelden."), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if g.user["role"] != users.ADMIN:
            abort(404)  # fuer normale Benutzer gibt es die Seite nicht
        return view(*args, **kwargs)
    return wrapper


def _safe_next(value):
    """Ruecksprung nach dem Login - nur zur OAuth-Zustimmung (Claude verbinden), nie zu fremden Seiten."""
    value = value or ""
    return value if value.startswith("/oauth/authorize?") and "\\" not in value else None


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_next(request.values.get("next"))
    if g.user is not None:
        return redirect(next_url or url_for("index"))
    email = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        keys = ("email:" + users.normalize_login(email), "ip:" + (request.remote_addr or ""))
        if any(login_limiter.blocked(k) for k in keys):
            flash("Zu viele Fehlversuche. Bitte in 15 Minuten noch einmal versuchen.", "error")
        else:
            row = users.authenticate(email, request.form.get("password", ""))
            if row is None:
                for k in keys:
                    login_limiter.hit(k)
                flash("E-Mail-Adresse oder Passwort ist falsch.", "error")
            elif row["status"] != users.APPROVED:
                flash("Dein Konto ist noch nicht freigegeben. Bitte warte auf die Freischaltung.", "warn")
            else:
                login_limiter.reset(keys[0])
                session.clear()  # neue Sitzung, damit keine alte (fremde) Sitzungs-ID weiterlebt
                session.permanent = True
                session["user_id"] = row["id"]
                return redirect(next_url or url_for("index"))
    return render_template("login.html", email=email, next_url=next_url)


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user is not None:
        return redirect(url_for("index"))
    form = {"email": "", "name": ""}
    if request.method == "POST":
        form = {"email": (request.form.get("email") or "").strip(), "name": (request.form.get("name") or "").strip()}
        ip_key = "ip:" + (request.remote_addr or "")
        if register_limiter.blocked(ip_key):
            flash("Zu viele Registrierungen von dieser Adresse. Bitte spaeter noch einmal versuchen.", "error")
        else:
            try:
                users.register(form["email"], form["name"], request.form.get("password", ""),
                               request.form.get("password_confirm", ""))
            except users.AccountError as e:
                flash(str(e), "error")
            else:
                register_limiter.hit(ip_key)
                flash("Danke! Deine Registrierung ist eingegangen. Sobald dein Konto freigegeben ist, "
                      "kannst du dich anmelden.", "ok")
                return redirect(url_for("login"))
    return render_template("register.html", form=form, min_password=users.MIN_PASSWORD_LENGTH)


@app.route("/password", methods=["GET", "POST"])
@login_required
def password():
    forced = bool(g.user["must_change_password"])
    if request.method == "POST":
        try:
            users.change_password(g.user["id"], request.form.get("current_password", ""),
                                  request.form.get("password", ""), request.form.get("password_confirm", ""))
        except users.AccountError as e:
            flash(str(e), "error")
        else:
            flash("Dein Passwort wurde geaendert.", "ok")
            return redirect(url_for("index"))
    return render_template("password.html", forced=forced, min_password=users.MIN_PASSWORD_LENGTH)


@app.post("/logout")
def logout():
    session.clear()
    flash("Du bist abgemeldet.", "ok")
    return redirect(url_for("login"))


# -- Benutzerverwaltung (nur Admins) ------------------------------------------------------------
@app.get("/admin")
@admin_required
def admin():
    rows = users.list_users()
    # Offene Anmeldungen zuerst, danach nach Anmeldedatum.
    rows = sorted(rows, key=lambda r: (r["status"] != users.PENDING, r["created_at"], r["id"]))
    pending = sum(1 for r in rows if r["status"] == users.PENDING)
    quotas = {r["id"]: logo_quota.status(r["id"], role=r["role"]) for r in rows}
    return render_template("admin.html", rows=rows, pending=pending, users=users, quotas=quotas)


@app.post("/admin/users/<int:user_id>")
@admin_required
def admin_action(user_id):
    action = request.form.get("action", "")
    row = users.get(user_id)
    if row is None:
        flash("Dieses Konto gibt es nicht mehr.", "error")
        return redirect(url_for("admin"))
    who = f"{row['name']} ({row['email']})"
    is_self = row["id"] == g.user["id"]
    try:
        if action == "approve":
            users.approve(user_id)
            flash(f"{who} ist freigeschaltet und kann sich jetzt anmelden.", "ok")
        elif action in ("make_admin", "make_user"):
            if is_self:
                raise users.AccountError("Die eigene Rolle kannst du nicht aendern.")
            users.set_role(user_id, users.ADMIN if action == "make_admin" else users.USER)
            flash(f"{who} ist jetzt {'Admin' if action == 'make_admin' else 'normaler Benutzer'}.", "ok")
        elif action == "delete":
            if is_self:
                raise users.AccountError("Das eigene Konto kannst du hier nicht loeschen.")
            users.delete(user_id)
            manager.drop_owner(user_id)
            flash(f"{who} wurde mit allen Daten geloescht.", "ok")
        else:
            raise users.AccountError("Unbekannte Aktion.")
    except users.AccountError as e:
        flash(str(e), "error")
    return redirect(url_for("admin"))


# -- Funktionen der App (nur angemeldet) --------------------------------------------------------
@app.get("/")
@login_required
def index():
    return render_template(
        "index.html",
        openai_available=jobs.openai_available(),
        default_color=jobs.DEFAULT_NAME_COLOR,
        openai_quota=logo_quota.status(g.user["id"], role=g.user["role"]),
    )


@app.post("/api/jobs")
@login_required
def create_job():
    upload = request.files.get("script")
    has_file = upload is not None and bool(upload.filename)
    query = (request.form.get("script_name") or "").strip() or None
    if has_file and query:
        return jsonify(error="Bitte entweder eine Datei hochladen oder einen Scriptnamen eingeben, nicht beides."), 400
    if not has_file and not query:
        return jsonify(error="Bitte eine Script-JSON-Datei auswaehlen oder einen Scriptnamen eingeben."), 400

    data = upload.read(jobs.MAX_UPLOAD_BYTES + 1) if has_file else None
    try:
        options = jobs.Options(
            name_color=request.form.get("name_color", ""),
            logo_description=request.form.get("logo_description", ""),
            logo_provider=request.form.get("logo_provider", "pollinations"),
            blankbg=_checked(request.form.get("blankbg")),
            new_logo=_checked(request.form.get("new_logo")),
        )
        # Bei hochgeladenen Scripts steht der Name schon fest: ein erschoepftes OpenAI-Limit gleich melden.
        # (Bei der Suche per Name prueft der Auftrag selbst, sobald das Script geladen ist.)
        if has_file and options.logo_provider == "openai" and logo_quota.remaining(g.user["id"]) == 0:
            _, name = jobs.parse_script(data)
            if manager.will_generate_openai_logo(name, options):
                return jsonify(error=logo_quota.exhausted_message(g.user["id"])), 429
        job = manager.submit(data, options, query=query, script_pk=request.form.get("script_pk"),
                             owner=g.user["id"])
    except jobs.UploadError as e:
        return jsonify(error=str(e)), 400
    return jsonify(job.public(include_log=g.user["role"] == users.ADMIN)), 202


@app.get("/api/search")
@login_required
def search():
    """Vorschlaege fuer das Scriptnamen-Feld (botcscripts.com)."""
    try:
        results = script_search.search(request.args.get("q", ""))
    except (RuntimeError, ImportError) as e:
        return jsonify(error=str(e)), 502
    return jsonify(results=results)


def _own_job(job_id):
    """Auftrag des angemeldeten Nutzers, sonst None (fremde Auftraege gibt es fuer ihn nicht)."""
    job = manager.get(job_id) if _JOB_ID_RE.match(job_id) else None
    if job is None or job.owner != g.user["id"]:
        return None
    return job


@app.get("/api/jobs/<job_id>")
@login_required
def job_status(job_id):
    job = _own_job(job_id)
    if job is None:
        return jsonify(error="Auftrag nicht gefunden oder abgelaufen."), 404
    return jsonify(job.public(include_log=g.user["role"] == users.ADMIN))


@app.get("/api/jobs/<job_id>/download")
@login_required
def job_download(job_id):
    job = _own_job(job_id)
    if job is None or job.status != "done" or job.result is None or not job.result.is_file():
        abort(404)
    return send_file(job.result, as_attachment=True, download_name=job.result.name)


app.register_blueprint(api.create_blueprint(manager, logo_quota, login_limiter))
app.register_blueprint(oauth.create_blueprint(login_required, oauth_register_limiter))
app.register_blueprint(mcp.create_blueprint(manager, logo_quota))


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="Die Datei bzw. Anfrage ist zu gross.", code="too_large"), 413


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith(api.API_PREFIX + "/"):
        return jsonify(error="Diesen Endpunkt gibt es nicht. Siehe /api/v1/docs.html.", code="not_found"), 404
    return error


@app.errorhandler(405)
def method_not_allowed(error):
    if request.path.startswith(api.API_PREFIX + "/"):
        return jsonify(error=f"Methode {request.method} ist hier nicht erlaubt.", code="method_not_allowed"), 405
    return error


def main():
    # Standard: nur lokal erreichbar. Fuer den Betrieb im Netz einen HTTPS-Reverse-Proxy davorsetzen.
    host = os.environ.get("BOTC_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("BOTC_WEB_PORT", "8090"))
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
