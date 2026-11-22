#!/usr/bin/env bash
# Erzeugt ein BotC-Script-PDF ueber die API der Web-App (API v1) ohne jede Rueckfrage.
#
# Aufruf:
#   botc_pdf_api.sh <skriptname | script.json> [Optionen]
#
# Optionen:
#   --name-color HEX          Schriftfarbe des Scriptnamens (Standard: #000000)
#   --logo-description TEXT   Beschreibung fuer ein neues Logo (max. 500 Zeichen)
#   --logo-provider NAME      openai (Standard) oder pollinations
#   --blank-background        Druckerfreundlicher, leerer Hintergrund
#   --new-logo                Neues Logo erzeugen statt vorhandenes zu verwenden
#   --output-dir DIR          Zielordner fuer die Datei (Standard: aktuelles Verzeichnis)
#
# Umgebungsvariablen:
#   BOTC_API_BASE   Basis-URL    (Standard: http://localhost:8090/api/v1, z. B. https://botc.example.com/api/v1)
#   BOTC_NETRC      netrc-Datei  (Standard: ~/.config/botc-pdf/netrc)
#
# Anmeldung: HTTP Basic Auth. Benutzer und Passwort stehen NUR in der netrc-Datei:
#   machine localhost login BENUTZER password PASSWORT
# Der Wrapper gibt sie nie aus. Sie erscheinen nicht in der Prozessliste.
#
# Ausgabe: auf stdout genau eine Zeile mit dem absoluten Pfad der gespeicherten Datei.
# Alles andere (Status, Fehler) geht auf stderr.
#
# Exit-Codes:
#   0  PDF gespeichert
#   1  unerwarteter Fehler oder Serverfehler
#   2  falsche Aufrufparameter
#   3  Anmeldung fehlgeschlagen, Konto gesperrt oder netrc fehlt/unsicher
#   4  Auftrag fehlgeschlagen (z. B. Script nicht gefunden)
#   5  Ergebnis ist nur ein ZIP ohne Name/Logo (Datei wurde trotzdem gespeichert)
#   6  OpenAI-Logo-Limit erreicht (quota_exceeded)
#   7  Server nicht erreichbar oder Zeitueberschreitung

set -euo pipefail

BASE="${BOTC_API_BASE:-http://localhost:8090/api/v1}"
NETRC="${BOTC_NETRC:-$HOME/.config/botc-pdf/netrc}"
MAX_POLL_SECONDS=900

die() { echo "Fehler: $1" >&2; exit "${2:-1}"; }

command -v curl    >/dev/null 2>&1 || die "curl nicht gefunden" 2
command -v python3 >/dev/null 2>&1 || die "python3 nicht gefunden" 2

SCRIPT_ARG=""
NAME_COLOR="#000000"
LOGO_DESC=""
LOGO_PROVIDER="openai"
BLANK_BG="false"
NEW_LOGO="false"
OUT_DIR="."

while [ $# -gt 0 ]; do
    case "$1" in
        --name-color)       [ $# -ge 2 ] || die "--name-color braucht einen Wert" 2; NAME_COLOR="$2"; shift 2 ;;
        --logo-description) [ $# -ge 2 ] || die "--logo-description braucht einen Wert" 2; LOGO_DESC="$2"; shift 2 ;;
        --logo-provider)    [ $# -ge 2 ] || die "--logo-provider braucht einen Wert" 2; LOGO_PROVIDER="$2"; shift 2 ;;
        --output-dir)       [ $# -ge 2 ] || die "--output-dir braucht einen Wert" 2; OUT_DIR="$2"; shift 2 ;;
        --blank-background) BLANK_BG="true"; shift ;;
        --new-logo)         NEW_LOGO="true"; shift ;;
        -h|--help)          sed -n '2,36p' "${BASH_SOURCE[0]}" >&2; exit 0 ;;
        --*)                die "unbekannte Option: $1" 2 ;;
        *)                  [ -z "$SCRIPT_ARG" ] || die "nur ein Script erlaubt (zweites Argument: $1)" 2; SCRIPT_ARG="$1"; shift ;;
    esac
done

[ -n "$SCRIPT_ARG" ] || die "Skriptname oder JSON-Datei fehlt (siehe --help)" 2
[[ "$NAME_COLOR" =~ ^#[0-9A-Fa-f]{6}$ ]] || die "--name-color muss ein Hex-Wert wie #000000 sein" 2
case "$LOGO_PROVIDER" in openai|pollinations) ;; *) die "--logo-provider muss openai oder pollinations sein" 2 ;; esac
[ "${#LOGO_DESC}" -le 500 ] || die "--logo-description darf höchstens 500 Zeichen haben" 2

# Zugangsdaten nur pruefen, nie lesen oder ausgeben.
[ -f "$NETRC" ] || die "netrc-Datei fehlt: $NETRC (Anlegen siehe Skill-Beschreibung)" 3
PERM="$(stat -c '%a' "$NETRC" 2>/dev/null || stat -f '%Lp' "$NETRC")"
case "$PERM" in
    600|400) ;;
    *) die "netrc ist zu offen (Rechte $PERM). Bitte: chmod 600 $NETRC" 3 ;;
esac

case "$BASE" in
    https://*) ;;
    http://192.168.*|http://10.*|http://172.1[6-9].*|http://172.2[0-9].*|http://172.3[0-1].*|http://127.*|http://localhost*) ;;
    *) die "Unverschluesselte Verbindung nur im lokalen Netz erlaubt (BOTC_API_BASE=$BASE)" 2 ;;
esac

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Anfrage-Inhalt mit Python bauen, damit Anfuehrungszeichen und Umlaute sicher escaped werden.
BOTC_SCRIPT_ARG="$SCRIPT_ARG" BOTC_NAME_COLOR="$NAME_COLOR" BOTC_LOGO_DESC="$LOGO_DESC" \
BOTC_LOGO_PROVIDER="$LOGO_PROVIDER" BOTC_BLANK_BG="$BLANK_BG" BOTC_NEW_LOGO="$NEW_LOGO" \
python3 - >"$WORK/request.json" <<'PY' || die "Script-JSON nicht lesbar oder ungueltig" 2
import json, os, sys
arg = os.environ["BOTC_SCRIPT_ARG"]
opts = {
    "name_color": os.environ["BOTC_NAME_COLOR"],
    "logo_provider": os.environ["BOTC_LOGO_PROVIDER"],
    "blank_background": os.environ["BOTC_BLANK_BG"] == "true",
    "new_logo": os.environ["BOTC_NEW_LOGO"] == "true",
}
if os.environ["BOTC_LOGO_DESC"]:
    opts["logo_description"] = os.environ["BOTC_LOGO_DESC"]
if os.path.isfile(arg):
    with open(arg, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        sys.exit("Script-JSON muss eine Liste sein")
    body = {"script": data, "options": opts}
else:
    body = {"script_name": arg, "options": opts}
json.dump(body, sys.stdout, ensure_ascii=False)
PY

# Kleine Helfer zum Auslesen der JSON-Antworten.
jget() { python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    v=d.get(sys.argv[2],"")
    print("" if v is None else v)
except Exception:
    print("")' "$1" "$2"; }

curl_api() { # curl_api <Methode> <URL> <Ausgabedatei> <Headerdatei> [weitere curl-Argumente]
    local method="$1" url="$2" out="$3" hdr="$4"; shift 4
    curl -sS -m 330 --noproxy '*' --netrc-file "$NETRC" -X "$method" \
        -D "$hdr" -o "$out" -w '%{http_code}' "$@" "$url"
}

echo "Sende Auftrag an $BASE ..." >&2
set +e
CODE="$(curl_api POST "$BASE/pdfs?wait=1" "$WORK/resp.bin" "$WORK/resp.hdr" \
        -H 'Content-Type: application/json' --data-binary @"$WORK/request.json")"
RC=$?
set -e
[ "$RC" -eq 0 ] || die "Server nicht erreichbar (curl-Code $RC)" 7

save_result() { # save_result <Antwortdatei> <Headerdatei> <Fallback-ID>
    local src="$1" hdr="$2" id="$3" ctype name ext
    ctype="$(grep -i '^content-type:' "$hdr" | tail -n1 | tr -d '\r' | cut -d: -f2- | sed 's/^ *//;s/;.*//')"
    name="$(grep -i '^content-disposition:' "$hdr" | tail -n1 | tr -d '\r' \
        | sed -n 's/.*filename\*\?=\(UTF-8'"''"'\)\?"\?\([^";]*\)"\?.*/\2/Ip')"
    name="$(basename -- "${name:-}")"
    case "$ctype" in application/zip) ext="zip" ;; *) ext="pdf" ;; esac
    [ -n "$name" ] && [ "$name" != "." ] || name="${id:-script}.$ext"
    cp -- "$src" "$OUT_DIR/$name"
    echo "$OUT_DIR/$name"
    [ "$ext" = "zip" ] && return 5
    return 0
}

handle_error() { # handle_error <Status> <Antwortdatei>
    local status="$1" f="$2" msg code
    msg="$(jget "$f" error)"; [ -n "$msg" ] || msg="$(jget "$f" message)"
    code="$(jget "$f" code)"
    echo "API-Fehler $status${code:+ ($code)}: ${msg:-keine Meldung}" >&2
    case "$status:$code" in
        401:*|403:*|429:too_many_attempts) exit 3 ;;
        429:quota_exceeded|*:quota_exceeded)  exit 6 ;;
        400:*|413:*|415:*)                    exit 2 ;;
        404:*|409:*|422:*)                    exit 4 ;;
        *)                                    exit 1 ;;
    esac
}

JOB_ID=""
case "$CODE" in
    200)
        JOB_ID="$(grep -i '^x-job-id:' "$WORK/resp.hdr" | tail -n1 | tr -d '\r' | cut -d: -f2- | tr -d ' ')"
        set +e; save_result "$WORK/resp.bin" "$WORK/resp.hdr" "$JOB_ID"; R=$?; set -e
        [ "$R" -eq 5 ] && echo "Hinweis: Nur ZIP ohne Name/Logo erzeugt." >&2
        exit "$R" ;;
    202)
        JOB_ID="$(jget "$WORK/resp.bin" id)"
        [ -n "$JOB_ID" ] || die "Antwort 202 ohne Auftrags-ID" 1
        echo "Auftrag $JOB_ID laeuft noch, warte weiter ..." >&2 ;;
    *)
        handle_error "$CODE" "$WORK/resp.bin" ;;
esac

# Nach 5 Minuten noch nicht fertig: Status abfragen.
WAITED=0
while :; do
    sleep 5; WAITED=$((WAITED + 5))
    [ "$WAITED" -le "$MAX_POLL_SECONDS" ] || die "Zeitueberschreitung nach ${MAX_POLL_SECONDS}s (Auftrag $JOB_ID)" 7
    set +e
    S="$(curl_api GET "$BASE/pdfs/$JOB_ID" "$WORK/status.json" "$WORK/status.hdr")"; RC=$?
    set -e
    [ "$RC" -eq 0 ] || die "Server nicht erreichbar beim Abfragen (curl-Code $RC)" 7
    [ "$S" = "200" ] || handle_error "$S" "$WORK/status.json"
    STATUS="$(jget "$WORK/status.json" status)"
    case "$STATUS" in
        done) break ;;
        error) handle_error 422 "$WORK/status.json" ;;
        *) ;;
    esac
done

set +e
D="$(curl_api GET "$BASE/pdfs/$JOB_ID/download" "$WORK/resp.bin" "$WORK/resp.hdr")"; RC=$?
set -e
[ "$RC" -eq 0 ] || die "Server nicht erreichbar beim Download (curl-Code $RC)" 7
[ "$D" = "200" ] || handle_error "$D" "$WORK/resp.bin"
set +e; save_result "$WORK/resp.bin" "$WORK/resp.hdr" "$JOB_ID"; R=$?; set -e
[ "$R" -eq 5 ] && echo "Hinweis: Nur ZIP ohne Name/Logo erzeugt." >&2
exit "$R"
