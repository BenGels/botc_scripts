"""Wochenlimit fuer mit OpenAI erzeugte Logos.

Normale Benutzer duerfen in den letzten 7 Tagen (gleitend) hoechstens LIMIT Logos ueber OpenAI erzeugen.
Admins sind nicht begrenzt. Gezaehlt wird nur, was wirklich erzeugt wurde: Ein Logo aus dem Cache kostet
nichts, eine fehlgeschlagene Erzeugung auch nicht.

Damit parallele Auftraege das Limit nicht gemeinsam ueberschreiten, reserviert ein Auftrag vor dem Lauf
einen Platz und gibt ihn danach frei - als gezaehlte Erzeugung oder ungenutzt.
"""
# Standard Library
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import threading

# Application Specific
from . import users

LIMIT = 5
WINDOW = timedelta(days=7)


class QuotaExceeded(Exception):
    """Das Limit ist erreicht. Die Meldung ist fuer die Anzeige gedacht."""


class Reservation:
    """Ein reservierter Platz. `finish(generated)` muss genau einmal aufgerufen werden."""

    def __init__(self, quota, user_id, counted):
        self._quota = quota
        self.user_id = user_id
        self.counted = counted  # False fuer Admins: nichts zu zaehlen
        self._done = False

    def finish(self, generated, script_name=None):
        if self._done:
            return
        self._done = True
        self._quota._finish(self, generated, script_name)


class OpenAILogoQuota:

    def __init__(self, limit=LIMIT, window=WINDOW):
        self.limit = limit
        self.window = window
        self._reserved = defaultdict(int)  # user_id -> laufende Reservierungen
        self._lock = threading.Lock()

    def _limited(self, user_id):
        row = users.get(user_id)
        return row is not None and row["role"] != users.ADMIN

    def _used(self, user_id):
        return users.logo_generations_since(user_id, datetime.now(timezone.utc) - self.window)

    def remaining(self, user_id):
        """Noch moegliche OpenAI-Logos, oder None, wenn das Konto nicht begrenzt ist."""
        if not self._limited(user_id):
            return None
        with self._lock:
            return max(0, self.limit - len(self._used(user_id)) - self._reserved[user_id])

    def status(self, user_id, role=None):
        """Stand des Kontingents fuer die Anzeige (Benutzerverwaltung, Formular).

        Liefert ein dict: limited, limit, used, running, remaining, exhausted und next_free_at
        (formatierter Zeitpunkt, ab dem wieder ein Platz frei wird; None, wenn nichts verbraucht ist).
        `role` spart die Datenbankabfrage, wenn sie schon bekannt ist.
        """
        if role is None:
            row = users.get(user_id)
            role = row["role"] if row is not None else None
        if role == users.ADMIN:
            return {"limited": False, "limit": self.limit}
        with self._lock:
            used = self._used(user_id)
            running = self._reserved.get(user_id, 0)
        remaining = max(0, self.limit - len(used) - running)
        next_free_at = None
        if used:
            # Der naechste Platz wird frei, wenn die aelteste noch mitzaehlende Erzeugung aus dem Fenster faellt.
            oldest_counting = used[max(0, len(used) - self.limit)]
            next_free_at = users.format_time((oldest_counting + self.window).isoformat())
        return {"limited": True, "limit": self.limit, "used": len(used), "running": running,
                "remaining": remaining, "exhausted": remaining == 0, "next_free_at": next_free_at}

    def exhausted_message(self, user_id):
        used = self._used(user_id)
        text = f"Du hast in den letzten 7 Tagen schon {self.limit} Logos mit OpenAI erzeugt (Limit fuer Benutzer)."
        if len(used) >= self.limit:
            # Frei wird der Platz der aeltesten Erzeugung, die noch mitzaehlt.
            free_at = used[len(used) - self.limit] + self.window
            text += f" Das naechste ist ab {users.format_time(free_at.isoformat())} moeglich."
        else:
            text += " Ein Platz wird frei, sobald ein laufender Auftrag fertig ist."
        return text + " Bis dahin: Pollinations waehlen oder ein Script, dessen Logo schon vorhanden ist."

    def reserve(self, user_id):
        """Reserviert einen Platz oder wirft QuotaExceeded."""
        if not self._limited(user_id):
            return Reservation(self, user_id, counted=False)
        with self._lock:
            if len(self._used(user_id)) + self._reserved[user_id] >= self.limit:
                raise QuotaExceeded(self.exhausted_message(user_id))
            self._reserved[user_id] += 1
        return Reservation(self, user_id, counted=True)

    def _finish(self, reservation, generated, script_name):
        if not reservation.counted:
            return
        with self._lock:
            self._reserved[reservation.user_id] -= 1
            if self._reserved[reservation.user_id] <= 0:
                del self._reserved[reservation.user_id]
            if generated:
                users.record_logo_generation(reservation.user_id, script_name)
