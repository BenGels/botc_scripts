---
name: botc-script-pdf
description: Erzeugt aus einem Blood-on-the-Clocktower-Script (JSON-Datei, Ordner oder Skriptname von botcscripts.com) ein druckfertiges, doppelseitiges Script-PDF (Vorderseite Rollen mit Beschreibung, Rückseite Nachtreihenfolge) mit dem Programm botc_tokens im Projekt botc_scripts. Nutzen bei Anfragen wie "Script-PDF erzeugen", "BotC Script drucken", "Clocktower Script als PDF", "botc_tokens script", "Token-Bögen drucken".
---

# BotC Script-PDF

Das Programm liegt in `/home/benjamin/PycharmProjects/botc_scripts` und wird über den Entrypoint `botc_tokens` bedient. Die Python-Umgebung des Projekts ist `.venv` im selben Ordner.

## Aufruf

Immer aus dem Projekt-Root starten, weil Standardpfade relativ zum aktuellen Verzeichnis gelten (`tokens`, `print`, `logos`):

```bash
cd /home/benjamin/PycharmProjects/botc_scripts
.venv/bin/botc_tokens script <script> --token-dir tokens_DEEN -o print \
  --logo-description "<Text>" --name-color "#RRGGBB"
```

`<script>` ist eine der drei Möglichkeiten:
- Pfad zu einer Script-JSON (Format des offiziellen Script-Tools)
- Ordner mit Script-Infos
- Kein existierender Pfad: wird als Skriptname behandelt und von botcscripts.com in der neuesten Version geladen

## Wichtig: keine interaktiven Rückfragen zulassen

Ohne `--logo-description` und `--name-color` fragt das Programm im Terminal nach. Das lässt sich hier nicht beantworten und blockiert den Lauf. Deshalb immer beide Optionen setzen (leerer Text bzw. ungültige Farbe fällt auf schwarz zurück) oder `--skip-branding` nutzen.

## Optionen

| Option | Wirkung |
|---|---|
| `--token-dir` | Ordner mit den Token-Bildern (Standard `tokens`, im Projekt gibt es `tokens_DEEN`) |
| `-o`, `--output-dir` | Ausgabeordner (Standard `print`) |
| `--nightorder` | JSON mit eigener Nachtreihenfolge |
| `--components` | Ordner oder ZIP mit eigenen Komponenten (Hintergründe etc.) |
| `--blankbg` | Druckerfreundlicher, leerer Hintergrund |
| `--blocklinebreak` | Kein Wechsel des Charaktertyps mit Trennlinie |
| `--skip-branding` | Nur rohe `script.pdf` und `backside.pdf`, ohne Namen, Logo und Zusammenführung |
| `--logo-description` | Zusatzbeschreibung für das generierte Logo |
| `--logo-provider` | `pollinations` (Standard, kostenlos) oder `openai` (kostenpflichtig, braucht `OPENAI_API_KEY`) |
| `--logo-reference-image` | Stilvorlage für das Logo, nur mit `openai` |
| `--name-color` | Hex-Farbe für den Skriptnamen, z. B. `#FFFFFF` |
| `--paper-width`, `--paper-height` | Papiergröße in Pixel (Standard 2550x3300) |
| `--margin-horizontal`, `--margin-vertical` | Ränder in Pixel (Standard 74) |

## Ergebnis

- Mit Branding: `print/<skriptname>.pdf`, Vorder- und Rückseite zusammengeführt und optimiert. Die Zwischendateien werden gelöscht.
- Mit `--skip-branding`: `print/script.pdf` und `print/backside.pdf`.
- Das Logo wird in `logos/<skriptname>.png` zwischengespeichert und beim nächsten Lauf wiederverwendet. Soll ein neues Logo entstehen, die Datei vorher löschen.

## Voraussetzungen

- Token-Bilder müssen schon existieren. Sie entstehen mit `botc_tokens create <inputs-ordner> -o <tokens-ordner>` und enthalten auch die Bilder mit den Endungen `-scriptblock` und `-nightorder`, die der Script-Befehl braucht.
- ImageMagick (für Wand) muss installiert sein. Ghostscript (`gs`) ist für die Komprimierung nötig; fehlt es, wird ein unkomprimiertes PDF erzeugt.
- Das Logo mit `pollinations` oder `openai` braucht Internetzugang.
- `.env` im Projekt kann den OpenAI-Schlüssel enthalten. Nicht auslesen oder ausgeben.

## Vorgehen

1. Klären, welches Script gemeint ist (Datei, Ordner oder Name) und welcher Token-Ordner gilt. Gibt es nur einen sinnvollen Kandidaten, diesen nehmen und nennen.
2. Sind Name-Farbe oder Logo-Wunsch nicht genannt, einmal kurz nachfragen oder `--skip-branding` vorschlagen.
3. Befehl wie oben ausführen und die Ausgabe auf Fehler prüfen (fehlende Token-Bilder, ungültiges JSON, fehlendes ImageMagick).
4. Den Pfad der fertigen PDF nennen.

## Verwandt: Token-Bögen

Für druckbare Token-Bögen statt Script-PDF gibt es `botc_tokens group <script.json> --token-dir <tokens> --output-dir <ordner>` mit den Optionen `--fixed-role-size`, `--fixed-reminder-size`, `--padding`, `--duplicates` und `--grid`.
