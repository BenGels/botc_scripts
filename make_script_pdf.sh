#!/usr/bin/env bash
# Erzeugt ein Script-PDF fuer Blood on the Clocktower ohne jede Rueckfrage.
# Geeignet fuer Cron, Make, CI und andere automatisierte Ablaeufe.
#
# Aufruf:
#   make_script_pdf.sh <skriptname|script.json|ordner> [Optionen]
#
# Optionen:
#   --logo-description TEXT   Zusatzbeschreibung fuers Logo (Standard: leer)
#   --name-color HEX          Farbe des Skriptnamens, z. B. #000000 (Standard: #000000)
#   --token-dir DIR           Ordner mit Token-Bildern (Standard: tokens_DEEN)
#   --output-dir DIR          Ausgabeordner (Standard: print)
#
# Ausgabe: auf stdout genau eine Zeile mit dem Pfad der fertigen PDF.
# Alles andere (Logs, Fehler) geht auf stderr.
#
# Exit-Codes:
#   0  PDF erzeugt, Pfad steht auf stdout
#   2  falsche Aufrufparameter
#   3  Voraussetzung fehlt (.venv, botc_tokens, OPENAI_API_KEY)
#   4  Programm lief, aber es entstand keine fertige PDF (z. B. Logo-Fehler,
#      Script nicht gefunden, Token-Bilder fehlen)
#   sonst: Exit-Code von botc_tokens
#
# Das Logo wird immer mit openai erzeugt und in logos/<skriptname>.png
# zwischengespeichert. Ein vorhandenes Logo wird wiederverwendet; fuer ein
# neues Logo die Datei vorher loeschen.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "Fehler: $*" >&2; exit "${2:-2}"; }

SCRIPT_ARG=""
LOGO_DESC=""
NAME_COLOR="#000000"
TOKEN_DIR="tokens_DEEN"
OUTPUT_DIR="print"

while [ $# -gt 0 ]; do
    case "$1" in
        --logo-description) [ $# -ge 2 ] || die "--logo-description braucht einen Wert"; LOGO_DESC="$2"; shift 2 ;;
        --name-color)       [ $# -ge 2 ] || die "--name-color braucht einen Wert"; NAME_COLOR="$2"; shift 2 ;;
        --token-dir)        [ $# -ge 2 ] || die "--token-dir braucht einen Wert"; TOKEN_DIR="$2"; shift 2 ;;
        --output-dir)       [ $# -ge 2 ] || die "--output-dir braucht einen Wert"; OUTPUT_DIR="$2"; shift 2 ;;
        -h|--help)          sed -n '2,26p' "${BASH_SOURCE[0]}" >&2; exit 0 ;;
        --*)                die "unbekannte Option: $1" ;;
        *)                  [ -z "$SCRIPT_ARG" ] || die "nur ein Script erlaubt (zweites Argument: $1)"; SCRIPT_ARG="$1"; shift ;;
    esac
done

[ -n "$SCRIPT_ARG" ] || die "Skriptname oder Pfad fehlt (siehe --help)"
[[ "$NAME_COLOR" =~ ^#[0-9A-Fa-f]{6}$ ]] || die "--name-color muss ein Hex-Wert wie #000000 sein"

# Existierende Pfade vor dem Verzeichniswechsel absolut machen.
# Alles andere gilt als Skriptname und wird von botcscripts.com geladen.
if [ -e "$SCRIPT_ARG" ]; then
    SCRIPT_ARG="$(realpath "$SCRIPT_ARG")"
fi

cd "$PROJECT_DIR"

BOTC="$PROJECT_DIR/.venv/bin/botc_tokens"
[ -x "$BOTC" ] || die ".venv/bin/botc_tokens nicht gefunden oder nicht ausfuehrbar" 3
[ -d "$TOKEN_DIR" ] || die "Token-Ordner nicht gefunden: $TOKEN_DIR" 3

# Pruefen, ob ein OpenAI-Schluessel vorhanden ist, ohne ihn auszugeben.
if [ -z "${OPENAI_API_KEY:-}" ]; then
    if ! { [ -f .env ] && grep -Eq '^[[:space:]]*(export[[:space:]]+)?OPENAI_API_KEY=.+' .env; }; then
        die "OPENAI_API_KEY fehlt (weder in der Umgebung noch in .env)" 3
    fi
fi

MARKER="$(mktemp)"
trap 'rm -f "$MARKER"' EXIT

echo "Starte botc_tokens script fuer: $SCRIPT_ARG" >&2

# stdin sperren: Sollte das Programm doch einmal nachfragen, bricht es
# mit einem Fehler ab, statt endlos zu warten.
set +e
"$BOTC" script "$SCRIPT_ARG" \
    --token-dir "$TOKEN_DIR" \
    -o "$OUTPUT_DIR" \
    --logo-provider openai \
    --logo-description "$LOGO_DESC" \
    --name-color "$NAME_COLOR" \
    </dev/null >&2
RC=$?
set -e

[ "$RC" -eq 0 ] || die "botc_tokens beendete sich mit Code $RC" "$RC"

# botc_tokens meldet manche Fehler nur im Text und beendet sich trotzdem mit 0.
# Deshalb die fertige PDF selbst suchen: neu entstanden, nicht die Rohdateien.
RESULT="$(find "$OUTPUT_DIR" -maxdepth 1 -type f -name '*.pdf' \
    ! -name 'script.pdf' ! -name 'backside.pdf' -newer "$MARKER" 2>/dev/null | head -n 1 || true)"

[ -n "$RESULT" ] || die "keine fertige PDF in $OUTPUT_DIR entstanden (Logo-Fehler, Script nicht gefunden oder Token-Bilder fehlen?)" 4

realpath "$RESULT"
