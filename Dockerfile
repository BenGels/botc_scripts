# BotC Script-PDF WebApp (webapp/) samt botc_tokens und Token-Bildern (tokens_DEEN).
#
#   docker build -t botc-webapp .
#   docker run -d -p 8090:8090 -e OPENAI_API_KEY=sk-... \
#     -v botc_data:/app/webapp_data -v botc_logos:/app/logos -v botc_scripts:/app/downloaded_scripts botc-webapp
#
# Oder einfacher: docker compose up -d (siehe docker-compose.yml).
FROM python:3.12-slim-trixie

# ImageMagick 7 (fuer Wand), Ghostscript (PDF-Komprimierung), Schriften-Grundausstattung, Zeitzonen.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libmagickwand-7.q16-10 ghostscript fontconfig fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*

# Debians ImageMagick-Policy verbietet PDF-Ausgabe und begrenzt Speicher/Bildgroesse zu knapp fuer
# A4 bei 300 dpi. Eigene Policy: PDF erlaubt, grosszuegigere Ressourcen.
COPY docker/imagemagick-policy.xml /etc/ImageMagick-7/policy.xml

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Abhaengigkeiten zuerst (aendern sich selten -> Docker-Cache).
COPY pyproject.toml README.md ./
COPY webapp/requirements.txt webapp/requirements.txt
# setuptools-scm braucht sonst ein Git-Repository fuer die Versionsnummer.
ARG BOTC_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${BOTC_VERSION}
COPY src src
# Lokal sind manche Dateien nur fuer den Besitzer lesbar; im Image muss der App-Benutzer sie lesen koennen.
RUN chmod -R a+rX src \
 && pip install . -r webapp/requirements.txt gunicorn==23.0.0 \
 && rm -rf build

COPY webapp webapp
COPY tokens_DEEN tokens_DEEN
# Bereits erzeugte Logos als Startbestand; neue landen im Volume (siehe unten).
COPY logos logos

RUN useradd --system --create-home --uid 10001 botc \
 && chmod -R a+rX webapp \
 && mkdir -p webapp_data downloaded_scripts \
 && chown -R botc:botc webapp_data logos downloaded_scripts
USER botc

# Persistente Daten: Benutzerkonten + Sitzungsschluessel, Logo-Cache, heruntergeladene Scripts.
VOLUME ["/app/webapp_data", "/app/logos", "/app/downloaded_scripts"]

ENV TZ=Europe/Berlin \
    BOTC_WEB_HOST=0.0.0.0 \
    BOTC_WEB_PORT=8090 \
    BOTC_WEB_WORKDIR=/tmp/botc_webapp
EXPOSE 8090

# Genau ein Prozess: Auftraege, Warteschlange und Login-Sperren liegen im Speicher dieses Prozesses.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 8 --timeout 120 --bind \"$BOTC_WEB_HOST:$BOTC_WEB_PORT\" --access-logfile - webapp.app:app"]
