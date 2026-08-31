"""Spiegel nach Discord. Nur schreiben, nie lesen.

Ein Webhook kann ausschliesslich in genau einen Kanal posten — er kann nicht
lesen, hat keine Gateway-Verbindung und kein Konto. Dass aus Discord nichts ins
Funknetz zurueckkommt, haengt damit nicht an Disziplin, sondern daran, was das
Token ueberhaupt kann.

Ohne ``DISCORD_WEBHOOKS`` ist alles hier tot: ``DiscordSink.aktiv`` ist dann
``False``, und der Feed verhaelt sich wie vorher.

Die Grenzen, gegen die hier gearbeitet wird (Discord-Entwicklerdokumentation):

* je Webhook 5 Anfragen / 2 s und rund 30 / 60 s. Darunter bleiben wir mit
  Abstand, statt sie auszureizen.
* **10.000 ungueltige Anfragen (401/403/429) in 10 Minuten sperren die IP** —
  nicht den Dienst, den ganzen Host gegenueber Discord. Deshalb wird ein
  Webhook bei 401/403/404 *dauerhaft* stillgelegt und nicht wiederholt: ein
  falsches Token heilt nicht durch Wiederholen, es sperrt uns aus.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("meshfeed.discord")

# Discord weist einen Anzeigenamen mit diesen Woertern mit HTTP 400 zurueck.
# Ein 400 in Schleife faellt unter dieselbe 10.000er-Grenze wie ein 401.
VERBOTENE_NAMEN = ("discord", "clyde")
NAME_MAX = 80
INHALT_MAX = 2000
# Nach so vielen Fehlversuchen wird eine Nachricht verworfen. Sie ist dann
# ohnehin Minuten alt, und eine Funkzeile von vorhin will niemand mehr lesen.
VERSUCHE_MAX = 3


def saeubere_namen(name: Optional[str]) -> str:
    """Absendernamen in etwas verwandeln, das Discord annimmt.

    Der Name kommt ungefiltert aus dem Funk. Faellt er leer aus oder traegt er
    ein verbotenes Wort, ist die Antwort ein HTTP 400 — und genau das darf nicht
    im Sekundentakt passieren.
    """
    name = " ".join((name or "").split())
    if not name:
        return "unbekannt"
    if any(wort in name.lower() for wort in VERBOTENE_NAMEN):
        name = re.sub("|".join(VERBOTENE_NAMEN), "***", name, flags=re.IGNORECASE)
    return name[:NAME_MAX] or "unbekannt"


def kuerze(text: str, grenze: int = INHALT_MAX) -> str:
    text = text or ""
    return text if len(text) <= grenze else text[: grenze - 1] + "…"


def maskiere(url: str) -> str:
    """Webhook-URLs sind Zugangsdaten — wer sie hat, postet unter jedem Namen."""
    return f"…{url[-6:]}" if len(url) > 6 else "…"


@dataclass
class Post:
    username: str
    inhalt: str


@dataclass
class Weg:
    """Ein Webhook, also genau ein Discord-Kanal, mit eigener Warteschlange.

    Eigene Warteschlange je Weg, damit ein haengender Kanal die anderen nicht
    aufhaelt — und eigene Bremse, weil Discord je Webhook zaehlt.
    """

    slug: str
    url: str
    warteschlange: "queue.Queue[Post]" = field(default_factory=lambda: queue.Queue(maxsize=200))
    stillgelegt: bool = False
    gepostet: int = 0
    verworfen_voll: int = 0
    verworfen_fehler: int = 0
    gebremst: int = 0
    letzter_fehler: Optional[str] = None


class DiscordSink:
    def __init__(self, ziele: dict[str, str], *, min_abstand_s: float = 2.5,
                 warteschlange_max: int = 200, trockenlauf: bool = False,
                 start_still_s: float = 5.0, zeitlimit_s: float = 10.0) -> None:
        self.min_abstand_s = min_abstand_s
        self.trockenlauf = trockenlauf
        # Erst nach dieser Frist wird gepostet. Absicherung gegen retained
        # Messages auf dem Paket-Topic: die duerfte es nicht geben, aber ein
        # Schwall alter Nachrichten nach jedem Neustart waere genau das, was
        # der Spiegel nicht tun soll.
        self.start_still_bis = time.monotonic() + start_still_s
        self.zeitlimit_s = zeitlimit_s
        self.wege: dict[str, Weg] = {
            slug: Weg(slug=slug, url=url,
                      warteschlange=queue.Queue(maxsize=warteschlange_max))
            for slug, url in ziele.items()
        }
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    @property
    def aktiv(self) -> bool:
        return bool(self.wege)

    def start(self) -> None:
        if not self.aktiv:
            logger.info("Discord-Spiegel aus: keine Webhooks konfiguriert")
            return
        for weg in self.wege.values():
            thread = threading.Thread(target=self._arbeite, args=(weg,),
                                      name=f"discord-{weg.slug}", daemon=True)
            thread.start()
            self._threads.append(thread)
        logger.info("Discord-Spiegel an: %s%s",
                    ", ".join(f"{w.slug}→{maskiere(w.url)}" for w in self.wege.values()),
                    " (Trockenlauf)" if self.trockenlauf else "")

    def stop(self) -> None:
        self._stop.set()

    # --- Einliefern ------------------------------------------------------

    def post(self, slug: str, username: Optional[str], inhalt: str) -> bool:
        """Eine Zeile einreihen. Kehrt sofort zurueck, sendet nie selbst.

        Ein Kanal ohne Webhook wird **verworfen** und nicht in einen Sammelkanal
        umgeleitet — sonst landet irgendwann etwas dort, wo es nicht hingehoert.
        """
        weg = self.wege.get(slug)
        if weg is None or weg.stillgelegt:
            return False
        if time.monotonic() < self.start_still_bis:
            return False
        post = Post(username=saeubere_namen(username), inhalt=kuerze(inhalt))
        try:
            weg.warteschlange.put_nowait(post)
            return True
        except queue.Full:
            # Verwerfen statt puffern, dieselbe Regel wie beim Sendegate im
            # Funknetz. Die aelteste fliegt, die neue kommt rein: bei einem
            # Rueckstau ist das Neueste das Interessantere.
            weg.verworfen_voll += 1
            try:
                weg.warteschlange.get_nowait()
                weg.warteschlange.put_nowait(post)
            except (queue.Empty, queue.Full):  # pragma: no cover - Wettlauf
                pass
            return False

    # --- Senden ----------------------------------------------------------

    def _arbeite(self, weg: Weg) -> None:
        naechster_start = 0.0
        while not self._stop.is_set():
            try:
                post = weg.warteschlange.get(timeout=0.5)
            except queue.Empty:
                continue
            wartezeit = naechster_start - time.monotonic()
            if wartezeit > 0:
                weg.gebremst += 1
                if self._stop.wait(wartezeit):
                    return
            self._sende(weg, post)
            naechster_start = time.monotonic() + self.min_abstand_s

    def _sende(self, weg: Weg, post: Post) -> None:
        if self.trockenlauf:
            logger.info("discord_trocken slug=%s name=%s text=%s",
                        weg.slug, post.username, post.inhalt[:120])
            weg.gepostet += 1
            return

        koerper = json.dumps({
            "username": post.username,
            "content": post.inhalt,
            # Ohne das pingt ein im Funk getipptes @everyone den ganzen Server.
            # Das ist die eine Stelle, an der ein reiner Lesespiegel doch etwas
            # anrichten koennte.
            "allowed_mentions": {"parse": []},
        }).encode("utf-8")

        for versuch in range(1, VERSUCHE_MAX + 1):
            try:
                anfrage = urllib.request.Request(
                    weg.url, data=koerper, method="POST",
                    headers={"Content-Type": "application/json",
                             "User-Agent": "meshfeed (+CarinthiaMesh)"},
                )
                with urllib.request.urlopen(anfrage, timeout=self.zeitlimit_s):
                    weg.gepostet += 1
                    weg.letzter_fehler = None
                    return
            except urllib.error.HTTPError as fehler:
                if fehler.code in (401, 403, 404):
                    # Endgueltig. Weiterprobieren wuerde uns in die
                    # 10.000-ungueltige-Anfragen-Sperre laufen und den Host
                    # fuer Discord aussperren, nicht nur diesen Webhook.
                    #
                    # 403 heisst dabei nicht zwingend "Token falsch": Discord
                    # antwortet auch auf eine Anfrage ohne User-Agent mit 403,
                    # gemessen am 31.08.2026. Deshalb setzt der Request oben
                    # immer einen — fehlt er, sieht ein gueltiger Webhook wie
                    # ein toter aus.
                    weg.stillgelegt = True
                    weg.letzter_fehler = f"HTTP {fehler.code}, Weg stillgelegt"
                    logger.error("Discord-Weg %s stillgelegt: HTTP %d auf %s — "
                                 "Token falsch oder Webhook geloescht. Kein Wiederholen.",
                                 weg.slug, fehler.code, maskiere(weg.url))
                    return
                if fehler.code == 429:
                    weg.letzter_fehler = "429"
                    self._warte_nach_429(fehler)
                    continue
                weg.letzter_fehler = f"HTTP {fehler.code}"
                logger.warning("Discord %s: HTTP %d (Versuch %d)", weg.slug, fehler.code, versuch)
            except Exception as fehler:  # Netz weg, DNS, Zeitlimit
                weg.letzter_fehler = str(fehler)
                logger.warning("Discord %s: %s (Versuch %d)", weg.slug, fehler, versuch)
            if self._stop.wait(min(2.0 * versuch, 10.0)):
                return

        weg.verworfen_fehler += 1
        logger.warning("Discord %s: nach %d Versuchen verworfen", weg.slug, VERSUCHE_MAX)

    def _warte_nach_429(self, fehler: urllib.error.HTTPError) -> None:
        """``retry_after`` aus dem Koerper lesen, nicht aus dem Kopf.

        Der Kopfwert ist auf ganze Sekunden gerundet; der Koerper nennt
        Millisekunden genau. Ein halbe Sekunde Puffer obendrauf, damit wir nicht
        exakt an der Kante wieder anklopfen und das naechste 429 einsammeln.
        """
        wartezeit = 1.0
        try:
            koerper = json.loads(fehler.read().decode("utf-8"))
            wartezeit = float(koerper.get("retry_after", 1.0))
        except Exception:
            kopf = fehler.headers.get("Retry-After") if fehler.headers else None
            if kopf:
                try:
                    wartezeit = float(kopf)
                except ValueError:
                    pass
        logger.info("Discord bremst, warte %.1fs", wartezeit + 0.5)
        self._stop.wait(min(wartezeit + 0.5, 60.0))

    # --- Zustand ---------------------------------------------------------

    def stats(self) -> dict:
        return {
            "aktiv": self.aktiv,
            "trockenlauf": self.trockenlauf,
            "wege": {
                weg.slug: {
                    "gepostet": weg.gepostet,
                    "warteschlange": weg.warteschlange.qsize(),
                    "verworfen_voll": weg.verworfen_voll,
                    "verworfen_fehler": weg.verworfen_fehler,
                    "gebremst": weg.gebremst,
                    "stillgelegt": weg.stillgelegt,
                    "letzter_fehler": weg.letzter_fehler,
                }
                for weg in self.wege.values()
            },
        }

    @property
    def alle_stillgelegt(self) -> bool:
        """Alle Wege tot heisst: der Dienst tut nichts mehr, was er soll."""
        return self.aktiv and all(weg.stillgelegt for weg in self.wege.values())
