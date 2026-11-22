"""Benutzerkonten der WebApp: Registrierung, Freigabe, Rollen und Login.

Neue Konten sind zunaechst gesperrt ("pending") und haben die Rolle "user". Freigeben, loeschen und die
Rolle wechseln koennen Admins in der WebApp unter /admin. Gibt es beim Start keinen Admin, wird das Konto
"admin" mit dem Passwort aus DEFAULT_ADMIN_PASSWORD angelegt (Passwortwechsel bei der ersten Anmeldung).

Notfalls geht die Freigabe auch auf der Kommandozeile (im Projektordner, mit der Umgebung des Projekts):

    .venv/bin/python -m webapp.users pending          # offene Anmeldungen anzeigen
    .venv/bin/python -m webapp.users approve <id>     # Anmeldung genehmigen
    .venv/bin/python -m webapp.users reject <id>      # ablehnen und alle Daten des Kontos loeschen

Statt der ID geht auch die E-Mail-Adresse.
"""
# Standard Library
from datetime import datetime, timezone
from pathlib import Path
import argparse
import os
import re
import secrets
import sqlite3
import sys

# Third Party
from werkzeug.security import check_password_hash, generate_password_hash

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Ordner fuer Datenbank und Sitzungsschluessel. Gehoert nicht ins Git.
DATA_DIR = Path(os.environ.get("BOTC_WEB_DATA") or PROJECT_ROOT / "webapp_data")
DB_PATH = DATA_DIR / "users.sqlite3"
SECRET_KEY_PATH = DATA_DIR / "secret_key"

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200
MAX_NAME_LENGTH = 100
MAX_EMAIL_LENGTH = 254

PENDING = "pending"
APPROVED = "approved"
USER = "user"
ADMIN = "admin"
ROLES = (USER, ADMIN)

# Standard-Admin, falls beim Start keiner existiert. Meldet sich mit diesem Namen statt einer E-Mail an.
DEFAULT_ADMIN_LOGIN = "admin"
DEFAULT_ADMIN_PASSWORD = "deleteme!"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Wird geprueft, wenn es die E-Mail nicht gibt - damit dauert ein Fehlversuch immer gleich lang.
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'approved')),
    created_at    TEXT NOT NULL,
    approved_at   TEXT,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    must_change_password INTEGER NOT NULL DEFAULT 0
)
"""
# Mit OpenAI erzeugte Logos je Konto (fuer das Wochenlimit normaler Benutzer).
_SCHEMA_LOGOS = """
CREATE TABLE IF NOT EXISTS logo_generations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    script_name TEXT
)
"""
# OAuth fuer den MCP-Server (siehe oauth.py): registrierte Apps, einmalige Codes, Zugaenge mit Token.
# Codes und Token liegen nur als SHA-256-Hash vor.
_SCHEMA_OAUTH = (
    """CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id          TEXT PRIMARY KEY,
        client_secret_hash TEXT,
        client_name        TEXT NOT NULL,
        redirect_uris      TEXT NOT NULL,
        created_at         INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS oauth_codes (
        code_hash      TEXT PRIMARY KEY,
        client_id      TEXT NOT NULL,
        user_id        INTEGER NOT NULL,
        redirect_uri   TEXT NOT NULL,
        code_challenge TEXT NOT NULL,
        scope          TEXT NOT NULL,
        expires_at     INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS oauth_grants (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        client_id       TEXT NOT NULL,
        scope           TEXT NOT NULL,
        access_hash     TEXT NOT NULL UNIQUE,
        access_expires  INTEGER NOT NULL,
        refresh_hash    TEXT NOT NULL UNIQUE,
        refresh_expires INTEGER NOT NULL,
        created_at      INTEGER NOT NULL,
        last_used_at    INTEGER
    )""",
)
# Spalten, die nach der ersten Version dazugekommen sind (fuer bestehende Datenbanken).
_MIGRATIONS = {
    "role": "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
    "must_change_password": "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0",
}


class AccountError(ValueError):
    """Eingabe oder Zustand passt nicht. Die Meldung ist fuer die Anzeige gedacht."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    """Datenbankverbindung fuer andere Module der App (z. B. oauth.py)."""
    return _connect()


def _connect():
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not DB_PATH.exists():
        os.close(os.open(DB_PATH, os.O_WRONLY | os.O_CREAT, 0o600))  # nur fuer den Besitzer lesbar
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # Geloeschte Datensaetze werden in der Datei ueberschrieben, nicht nur als frei markiert.
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute(_SCHEMA)
    conn.execute(_SCHEMA_LOGOS)
    for statement in _SCHEMA_OAUTH:
        conn.execute(statement)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for column, statement in _MIGRATIONS.items():
        if column not in columns:
            conn.execute(statement)
    return conn


def secret_key():
    """Schluessel fuer die Sitzungs-Cookies: aus BOTC_WEB_SECRET_KEY oder einmal erzeugt und gespeichert."""
    from_env = os.environ.get("BOTC_WEB_SECRET_KEY")
    if from_env:
        return from_env
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(SECRET_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return SECRET_KEY_PATH.read_text(encoding="ascii").strip()
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key)
    return key


# -- Pruefen der Eingaben ----------------------------------------------------------------------
def normalize_email(value):
    email = (value or "").strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(email):
        raise AccountError("Bitte eine gueltige E-Mail-Adresse eingeben.")
    return email


def normalize_name(value):
    name = " ".join(_CONTROL_RE.sub(" ", value or "").split())
    if not name:
        raise AccountError("Bitte einen Namen eingeben.")
    if len(name) > MAX_NAME_LENGTH:
        raise AccountError(f"Der Name ist zu lang (maximal {MAX_NAME_LENGTH} Zeichen).")
    return name


def normalize_login(value):
    """Anmeldename: E-Mail-Adresse oder der Name des Standard-Admins."""
    return (value or "").strip().lower()[:MAX_EMAIL_LENGTH]


def check_password(password, confirm):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AccountError(f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AccountError(f"Das Passwort ist zu lang (maximal {MAX_PASSWORD_LENGTH} Zeichen).")
    if password != confirm:
        raise AccountError("Die beiden Passwoerter stimmen nicht ueberein.")


# -- Konten -------------------------------------------------------------------------------------
def register(email, name, password, confirm):
    """Legt ein gesperrtes Konto an. Liefert die neue ID."""
    email = normalize_email(email)
    name = normalize_name(name)
    check_password(password, confirm)
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (email, name, password_hash, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (email, name, generate_password_hash(password), PENDING, _now()),
            )
        except sqlite3.IntegrityError:
            raise AccountError("Fuer diese E-Mail-Adresse gibt es schon ein Konto oder eine offene Anmeldung.") from None
        return cur.lastrowid


def authenticate(login, password):
    """Liefert das Konto bei richtigem Passwort, sonst None. Der Status wird nicht geprueft."""
    login = normalize_login(login)
    row = get_by_email(login) if login else None
    if row is None:
        check_password_hash(_DUMMY_HASH, password or "")
        return None
    return row if check_password_hash(row["password_hash"], password or "") else None


def get(user_id):
    with _connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_by_email(email):
    with _connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def list_users(status=None):
    with _connect() as conn:
        if status is None:
            return conn.execute("SELECT * FROM users ORDER BY created_at, id").fetchall()
        return conn.execute("SELECT * FROM users WHERE status = ? ORDER BY created_at, id", (status,)).fetchall()


def find(ref):
    """Konto per ID oder E-Mail-Adresse."""
    ref = (ref or "").strip()
    if ref.isdigit():
        return get(int(ref))
    return get_by_email(ref.lower())


def ensure_admin():
    """Sorgt dafuer, dass es mindestens einen freigegebenen Admin gibt.

    Fehlt einer, wird das Konto DEFAULT_ADMIN_LOGIN angelegt (oder, falls es schon existiert, wieder zum
    Admin gemacht) - mit dem Standardpasswort, das bei der ersten Anmeldung geaendert werden muss.
    Liefert True, wenn das Standardkonto angelegt oder zurueckgesetzt wurde.
    """
    with _connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role = ? AND status = ?", (ADMIN, APPROVED)).fetchone():
            return False
        values = (generate_password_hash(DEFAULT_ADMIN_PASSWORD), ADMIN, APPROVED, _now())
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, role = ?, status = ?, approved_at = ?, must_change_password = 1 "
            "WHERE email = ?", (*values, DEFAULT_ADMIN_LOGIN))
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO users (email, name, password_hash, role, status, created_at, approved_at, "
                "must_change_password) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (DEFAULT_ADMIN_LOGIN, "Administrator", values[0], ADMIN, APPROVED, values[3], values[3]))
        return True


def change_password(user_id, current, new, confirm):
    row = get(user_id)
    if row is None or not check_password_hash(row["password_hash"], current or ""):
        raise AccountError("Das bisherige Passwort ist falsch.")
    check_password(new, confirm)
    if new == current:
        raise AccountError("Das neue Passwort muss sich vom bisherigen unterscheiden.")
    with _connect() as conn:
        conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                     (generate_password_hash(new), user_id))


def set_role(user_id, role):
    if role not in ROLES:
        raise AccountError("Unbekannte Rolle.")
    with _connect() as conn:
        cur = conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        if cur.rowcount != 1:
            raise AccountError("Konto nicht gefunden.")


def delete(user_id):
    """Loescht ein Konto (offen oder freigegeben) mit allen Daten."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        if cur.rowcount != 1:
            raise AccountError("Konto nicht gefunden.")
        conn.execute("DELETE FROM logo_generations WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM oauth_codes WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM oauth_grants WHERE user_id = ?", (user_id,))
    with _connect() as conn:
        conn.execute("VACUUM")  # auch freie Seiten der Datei leeren


def logo_generations_since(user_id, since):
    """Zeitpunkte (UTC, ISO) der OpenAI-Logos eines Kontos seit `since` (datetime), aelteste zuerst."""
    with _connect() as conn:
        rows = conn.execute("SELECT created_at FROM logo_generations WHERE user_id = ? AND created_at >= ? "
                            "ORDER BY created_at", (user_id, since.isoformat(timespec="seconds"))).fetchall()
    return [datetime.fromisoformat(r["created_at"]) for r in rows]


def record_logo_generation(user_id, script_name):
    with _connect() as conn:
        conn.execute("INSERT INTO logo_generations (user_id, created_at, script_name) VALUES (?, ?, ?)",
                     (user_id, _now(), script_name))


def approve(user_id):
    with _connect() as conn:
        cur = conn.execute("UPDATE users SET status = ?, approved_at = ? WHERE id = ? AND status = ?",
                           (APPROVED, _now(), user_id, PENDING))
        if cur.rowcount != 1:
            raise AccountError("Keine offene Anmeldung mit dieser ID.")


def reject(user_id):
    """Lehnt eine offene Anmeldung ab und loescht das Konto mit allen Daten."""
    row = get(user_id)
    if row is None or row["status"] != PENDING:
        raise AccountError("Keine offene Anmeldung mit dieser ID.")
    delete(user_id)


# -- Kommandozeile ------------------------------------------------------------------------------
def format_time(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return iso or "-"


def _print_table(rows):
    if not rows:
        return
    header = ("ID", "Angemeldet", "Status", "Rolle", "Name", "E-Mail")
    lines = [(str(r["id"]), format_time(r["created_at"]),
              "offen" if r["status"] == PENDING else "freigegeben",
              "Admin" if r["role"] == ADMIN else "Benutzer", r["name"], r["email"]) for r in rows]
    widths = [max(len(h), *(len(line[i]) for line in lines)) for i, h in enumerate(header)]
    for line in (header, *lines):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip())


def _resolve(ref):
    row = find(ref)
    if row is None:
        raise AccountError(f"Kein Konto gefunden fuer '{ref}'.")
    if row["status"] != PENDING:
        raise AccountError(f"Konto {row['id']} ({row['email']}) ist bereits freigegeben, keine offene Anmeldung.")
    return row


def _cmd_pending(_args):
    rows = list_users(PENDING)
    if not rows:
        print("Keine offenen Anmeldungen.")
        return
    _print_table(rows)


def _cmd_list(_args):
    rows = list_users()
    if not rows:
        print("Keine Konten.")
        return
    _print_table(rows)


def _cmd_approve(args):
    row = _resolve(args.user)
    approve(row["id"])
    print(f"Genehmigt: {row['name']} <{row['email']}> (ID {row['id']}) kann sich jetzt anmelden.")


def _cmd_reject(args):
    row = _resolve(args.user)
    if not args.yes:
        if not sys.stdin.isatty():
            raise AccountError("Ablehnen loescht das Konto endgueltig. Ohne Terminal bitte mit --yes bestaetigen.")
        answer = input(f"Anmeldung von {row['name']} <{row['email']}> ablehnen und alle Daten loeschen? [j/N] ")
        if answer.strip().lower() not in ("j", "ja", "y", "yes"):
            print("Abgebrochen, nichts geaendert.")
            return
    reject(row["id"])
    print(f"Abgelehnt und geloescht: {row['name']} <{row['email']}> (ID {row['id']}).")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m webapp.users",
                                     description="Anmeldungen fuer die BotC-WebApp verwalten.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pending", help="offene Anmeldungen anzeigen").set_defaults(func=_cmd_pending)
    sub.add_parser("list", help="alle Konten anzeigen").set_defaults(func=_cmd_list)
    p = sub.add_parser("approve", help="offene Anmeldung genehmigen")
    p.add_argument("user", help="ID oder E-Mail-Adresse")
    p.set_defaults(func=_cmd_approve)
    p = sub.add_parser("reject", help="offene Anmeldung ablehnen und alle Daten dazu loeschen")
    p.add_argument("user", help="ID oder E-Mail-Adresse")
    p.add_argument("-y", "--yes", action="store_true", help="ohne Rueckfrage")
    p.set_defaults(func=_cmd_reject)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except AccountError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
