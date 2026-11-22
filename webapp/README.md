# BotC Script-PDF als WebApp

Nach Anmeldung: Script-JSON hochladen oder einen Scriptnamen eingeben, das doppelseitige Script-PDF erzeugen lassen und
herunterladen. Die WebApp startet pro Auftrag dasselbe Programm wie auf der Kommandozeile (`botc_tokens script ...`) und liefert dessen Ergebnis aus.

## Start

Im Projektordner:

```bash
.venv/bin/pip install -r webapp/requirements.txt
.venv/bin/python -m webapp.app
```

Danach <http://127.0.0.1:8090> im Browser öffnen.

## Docker

Das Image enthält alles: botc_tokens, die WebApp, die Token-Bilder (`tokens_DEEN`) und die bisherigen Logos.
Von außen braucht es nur den OpenAI-Schlüssel (optional - ohne ihn gibt es Logos nur über Pollinations):

```bash
OPENAI_API_KEY=sk-... docker compose up -d --build     # liest den Schlüssel auch aus .env im Projektordner
```

Danach unter <http://127.0.0.1:8090> mit `admin` / `deleteme!` anmelden, Passwort ändern und unter
„Benutzerverwaltung“ die Anmeldungen freischalten.

Ohne Compose:

```bash
docker build -t botc-webapp .
docker run -d --name botc-webapp -p 127.0.0.1:8090:8090 -e OPENAI_API_KEY=sk-... \
  -v botc_data:/app/webapp_data -v botc_logos:/app/logos -v botc_scripts:/app/downloaded_scripts botc-webapp
```

- Die drei Volumes halten Konten und Sitzungsschlüssel, den Logo-Cache und heruntergeladene Scripts über
  Neustarts und neue Images hinweg. Die `.env` des Projekts kommt nie ins Image (`.dockerignore`).
- Der Port ist nur lokal freigegeben. Für Zugriff von außen einen HTTPS-Reverse-Proxy davorsetzen und
  `BOTC_WEB_SECURE_COOKIES=1` sowie `BOTC_WEB_TRUST_PROXY=1` setzen (dann zählt für die Login-Sperre die echte
  Client-IP aus `X-Forwarded-For`).
- Im Container läuft gunicorn mit genau einem Prozess (Aufträge und Login-Sperren liegen im Speicher).

## Verhalten

- Statt einer Datei kann ein Scriptname eingegeben werden. Ab drei Zeichen schlägt die Seite passende Scripts von
  botcscripts.com vor (Autor, Version, Typ) und zeigt zum ausgewählten Script den Link auf botcscripts.com, damit
  man es vor dem Erzeugen prüfen kann. Erzeugt wird dann genau diese Version (abgelegt in `downloaded_scripts/`).
  Die Vorschläge werden zehn Minuten zwischengespeichert, um botcscripts.com nicht mit Anfragen zu überhäufen.
- Wird kein Vorschlag ausgewählt, wird der Name wie bei `botc_tokens script <name>` gesucht: zuerst unter
  `downloaded_scripts/` im Projekt, sonst auf botcscripts.com (neueste Version, wird dort abgelegt).

- Jeder Upload bekommt einen eigenen Arbeitsordner (Standard: `<tmp>/botc_webapp/<id>`), mehrere Nutzer stören
  sich nicht. Die Erzeugung läuft standardmäßig ein Job nach dem anderen.
- Der Logo-Cache `logos/` im Projekt wird wie bei der CLI genutzt. „Neues Logo erzeugen“ benennt ein vorhandenes
  Logo in `<name>.old.png` um, bevor ein neues entsteht.
- Fertige Ergebnisse werden nach einer Stunde aus dem Arbeitsordner gelöscht.
- Die PDFs entstehen wie bei den von Hand erzeugten Scripts in A4 ohne Rand (`--paper-width 2480 --paper-height 3508
  --margin-horizontal 0 --margin-vertical 0`), nicht im Letter-Format, das die CLI ohne diese Angaben nimmt.
- Schlägt das Branding fehl (z. B. Logo nicht erzeugbar oder kein Name im `_meta`-Eintrag), gibt es Vorder- und
  Rückseite ohne Name/Logo als ZIP, genau wie die CLI die beiden Roh-PDFs stehen lässt.
- **OpenAI-Limit:** Normale Benutzer können in den letzten 7 Tagen (gleitend) höchstens 5 neue Logos über OpenAI
  erzeugen, Admins unbegrenzt. Gezählt wird nur eine tatsächlich erfolgreiche Erzeugung. Ein Logo aus dem Cache,
  Pollinations und fehlgeschlagene Versuche zählen nicht. Ist das Limit erreicht, bricht der Auftrag vor der
  Erzeugung ab (ein vorhandenes Logo wird dann auch bei „Neues Logo“ nicht angetastet), und die Meldung nennt den
  Zeitpunkt, ab dem wieder eins möglich ist. Das Formular zeigt, wie viele noch übrig sind.
- Das Logo mit OpenAI (kostenpflichtig) wird nur angeboten, wenn `OPENAI_API_KEY` gesetzt ist (Umgebung oder `.env`).

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `BOTC_WEB_HOST` | `127.0.0.1` | Adresse, auf der gelauscht wird |
| `BOTC_WEB_PORT` | `8090` | Port |
| `BOTC_TOKEN_DIR` | `tokens_DEEN` | Ordner mit den Token-Bildern (relativ zum Projekt oder absolut) |
| `BOTC_WEB_WORKDIR` | `<tmp>/botc_webapp` | Arbeitsordner für Uploads und Ergebnisse |
| `BOTC_JOB_TIMEOUT` | `900` | Zeitlimit pro Job in Sekunden |
| `BOTC_JOB_TTL` | `3600` | Aufbewahrung fertiger Jobs in Sekunden |
| `BOTC_WEB_WORKERS` | `1` | Anzahl gleichzeitiger Jobs |
| `BOTC_WEB_DATA` | `webapp_data` | Ordner für Benutzerdatenbank und Sitzungsschlüssel |
| `BOTC_WEB_SECRET_KEY` | (erzeugt) | Schlüssel für die Sitzungs-Cookies, statt der Datei in `BOTC_WEB_DATA` |
| `BOTC_WEB_SECURE_COOKIES` | aus | `1`: Cookies nur über HTTPS senden (hinter HTTPS-Proxy setzen) |
| `BOTC_WEB_TRUST_PROXY` | aus | `1`: Client-IP und Protokoll aus den Headern eines (einzigen) Reverse-Proxys übernehmen |
| `BOTC_WEB_PUBLIC_URL` | (aus der Anfrage) | Öffentliche Adresse, z. B. `https://botc.example.com` (Pflicht für OAuth/MCP hinter einem Proxy) |
| `BOTC_WEB_TRUSTED_PROXIES` | leer | IPs von Proxys, deren `CF-Connecting-IP`/`X-Forwarded-For` als echte Besucher-IP gilt |

## REST-API

Script-PDFs lassen sich auch per HTTP erzeugen, angemeldet per HTTP Basic Auth mit den normalen Login-Daten.
Ausführliche Beschreibung mit allen Feldern, Fehlercodes und Beispielen: `/api/v1/docs.html` (in der App oben
unter „API“).

| Methode | Pfad | Zweck |
|---|---|---|
| `POST` | `/api/v1/pdfs` | Auftrag anlegen (JSON: `script`, `script_name` oder `script_id`, dazu `options`); mit `?wait=1` direkt das PDF |
| `GET` | `/api/v1/pdfs/<id>` | Status des Auftrags |
| `GET` | `/api/v1/pdfs/<id>/download` | Fertiges PDF |

```bash
curl -u 'anna@example.org:passwort' -H 'Content-Type: application/json' \
     -d '{"script_name": "Trust"}' -o Trust.pdf 'http://127.0.0.1:8090/api/v1/pdfs?wait=1'
```

Auftrags-IDs sind 32-stellige Zufalls-Hashes; jedes Konto sieht nur seine eigenen Aufträge. Login-Sperre und
OpenAI-Limit gelten für API und Web-Oberfläche gemeinsam. Da das Passwort bei jeder Anfrage mitgeht, die API nur
über HTTPS nutzen.

## MCP-Server für Claude

Unter `/mcp` läuft ein MCP-Server (Streamable HTTP), mit dem Claude Script-PDFs erzeugen kann - in claude.ai,
den Claude-Apps und Claude Code. Angemeldet wird per OAuth mit dem normalen Konto:

- **claude.ai:** *Einstellungen → Connectors → Eigenen Connector hinzufügen*, URL
  `https://botc.example.com/mcp`, dann „Verbinden“: Login-Seite der App, Zugriff erlauben.
- **Claude Code:** `claude mcp add --transport http botc-pdf https://botc.example.com/mcp`, danach `/mcp`.

Werkzeuge: `create_script_pdf` (Scriptname, botcscripts.com-ID oder Script-JSON plus dieselben Optionen wie die
REST-API; wartet bis 60 s) und `get_script_pdf_status`. Fertige PDFs gibt es als signierten Download-Link, der
1 Stunde gilt. Unter „Verbindungen“ sieht jeder Benutzer seine verbundenen Apps und kann sie trennen.

Technik: OAuth 2.1 mit PKCE und Dynamic Client Registration (`/.well-known/oauth-protected-resource`,
`/.well-known/oauth-authorization-server`, `/oauth/register`, `/oauth/authorize`, `/oauth/token`).
Zugangstoken gelten 1 Stunde, Erneuerungstoken 30 Tage (bei jeder Erneuerung neu); in der Datenbank liegen nur
Hashes. `BOTC_WEB_PUBLIC_URL` muss die öffentliche Adresse sein.

## Konten und Freigabe

Ohne Anmeldung ist nichts von der App erreichbar. Neue Nutzer registrieren sich unter `/register` mit Name,
E-Mail-Adresse und Passwort (mindestens 10 Zeichen). Sie werden dabei immer normale Benutzer, und ihr Konto
bleibt gesperrt, bis ein Admin es freischaltet.

**Benutzerverwaltung** (`/admin`, nur für Admins sichtbar): alle Konten mit Status und Rolle einsehen, offene
Anmeldungen freischalten, Konten löschen (mit Rückfrage auf der Seite) und die Rolle zwischen Benutzer und Admin
wechseln. Das eigene Konto kann man dort weder löschen noch herabstufen, damit man sich nicht aussperrt.
Gelöschte Konten verlieren sofort ihre Sitzung, ihre Aufträge und PDFs werden mit gelöscht.

**Erster Admin:** Gibt es beim Start keinen freigegebenen Admin, legt die App das Konto `admin` mit dem Passwort
`deleteme!` an (bzw. setzt ein vorhandenes Konto `admin` darauf zurück) und schreibt eine Warnung ins Log. Man
meldet sich mit dem Benutzernamen `admin` statt einer E-Mail an und muss das Passwort sofort ändern - vorher ist
nichts anderes erreichbar. Jeder kann sein Passwort unter „Passwort ändern“ neu setzen.

Notfalls geht die Verwaltung auch auf der Kommandozeile (im Projektordner bzw. mit `docker compose exec
botc-webapp ...`):

```bash
.venv/bin/python -m webapp.users list                    # alle Konten mit Status und Rolle
.venv/bin/python -m webapp.users pending                 # offene Anmeldungen
.venv/bin/python -m webapp.users approve <id|email>
.venv/bin/python -m webapp.users reject <id|email>       # offene Anmeldung ablehnen und löschen
```

- Konten liegen in `webapp_data/users.sqlite3`, Passwörter nur als Hash (scrypt). Daneben liegt der automatisch
  erzeugte Schlüssel für die Sitzungs-Cookies (`webapp_data/secret_key`). Der Ordner ist nur für den Besitzer
  lesbar und steht in `.gitignore`.
- Eine Anmeldung gilt 14 Tage. Wird ein Konto gelöscht, ist auch eine laufende Sitzung sofort ungültig.
- Jeder sieht nur seine eigenen Aufträge und Ergebnisse.
- Nach 5 Fehlversuchen (pro E-Mail-Adresse bzw. IP) ist die Anmeldung 15 Minuten gesperrt, pro IP gibt es
  höchstens 5 Registrierungen pro Stunde.

## Hinweis zur Sicherheit

Standardmäßig ist die App nur lokal erreichbar. Wer sie im Netz anbietet, sollte sie hinter einen Reverse-Proxy
mit HTTPS stellen und `BOTC_WEB_SECURE_COOKIES=1` setzen, damit Passwörter und Sitzungs-Cookies nicht
unverschlüsselt übertragen werden. Der eingebaute Flask-Server ist nur für kleinen Betrieb gedacht.
