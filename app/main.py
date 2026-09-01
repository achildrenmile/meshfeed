"""Webteil: Seite, JSON-API, Live-Strom.

Nur lesend. Es gibt keinen Endpunkt, der etwas ins Funknetz schiebt — Senden
laeuft ueber meshinfra und bleibt dort.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .collector import Collector
from .config import Settings
from .discord import DiscordSink
from .karte import KartenQuelle
from .store import StoredMessage, Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("meshfeed")

STATIC_DIR = Path(__file__).parent / "static"
PURGE_INTERVAL_SECONDS = 3600


def attach_paths(store: Store, messages: list[StoredMessage]) -> list[dict]:
    """Nachrichten als dicts, jede um ihre einzelnen Empfaenge ergaenzt.

    Ein Pfadeintrag im Paket nennt nur ein Praefix des Public Key. Aufgeloest
    wird erst hier und nicht beim Speichern: so bekommt auch ein Weg von gestern
    seinen Namen, sobald das zugehoerige Advert hereinkommt.
    """
    payload = [message.as_dict() for message in messages]
    by_id = {item["id"]: item for item in payload}
    for item in payload:
        item["heard"] = []
    if not by_id:
        return payload

    receptions = store.receptions_for(list(by_id))
    prefixes = {
        hop.lower()
        for entries in receptions.values()
        for entry in entries
        for hop in (entry["path"] or [])
    }
    known = store.resolve_prefixes(prefixes)

    for message_id, entries in receptions.items():
        target = by_id.get(message_id)
        if target is None:
            continue
        for entry in entries:
            hops = [
                {
                    "prefix": hop.lower(),
                    "names": [c["name"] for c in known.get(hop.lower(), []) if c["name"]],
                }
                for hop in (entry["path"] or [])
            ]
            target["heard"].append({**entry, "path": hops})
    return payload


class Broadcaster:
    """Verteilt neue Nachrichten an alle offenen SSE-Verbindungen.

    Der MQTT-Ingest laeuft in einem eigenen Thread, die Warteschlangen gehoeren
    dem Eventloop — deshalb geht alles ueber ``call_soon_threadsafe``.
    """

    def __init__(self, store: Optional[Store] = None) -> None:
        self._queues: set[asyncio.Queue[str]] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._store = store

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._queues.discard(queue)

    def publish(self, message: StoredMessage, is_new: bool) -> None:
        if self._loop is None:
            return
        if self._store is not None:
            body = attach_paths(self._store, [message])[0]
        else:
            body = message.as_dict()
        payload = json.dumps({"new": is_new, "message": body}, ensure_ascii=False)
        self._loop.call_soon_threadsafe(self._fanout, payload)

    def _fanout(self, payload: str) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Langsamer Client: lieber eine Nachricht verlieren als den
                # Ingest ausbremsen. Beim Neuladen holt er sich alles per API.
                self._queues.discard(queue)



def _asset_version(theme: str) -> str:
    """Kurzes Kennzeichen ueber den Inhalt von Stilvorlage und Seitengeruest.

    Haengt als ?v= an den Verweisen. Ohne das liefert ein vorgelagerter Cache
    nach einer Aenderung stundenlang die alte Datei weiter -- die Seite sieht
    dann kaputt aus, obwohl der Ursprung laengst das Neue hat. Aendert sich
    nichts, bleibt das Kennzeichen gleich und der Cache greift wie gewuenscht.
    """
    roh = b""
    for name in ("base.css", f"themes/{theme}.css", "index.html"):
        pfad = STATIC_DIR / name
        if pfad.is_file():
            roh += pfad.read_bytes()
    return hashlib.sha256(roh).hexdigest()[:10]

def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    broadcaster = Broadcaster(store)
    discord = DiscordSink(
        settings.discord_webhooks,
        min_abstand_s=settings.discord_min_abstand_s,
        warteschlange_max=settings.discord_warteschlange_max,
        trockenlauf=settings.discord_trockenlauf,
        start_still_s=settings.discord_start_still_s,
    )
    # Aufwaermfrist fuer Knotenmeldungen: beim ersten Start ist jeder Knoten
    # neu, das waeren drei Dutzend Meldungen am Stueck.
    knoten_ab = time.monotonic() + settings.discord_warmup_min * 60

    def _an_discord(message, is_new: bool) -> None:
        """Nur Neues. Jede Wiederholung eines Pakets kaeme sonst noch einmal."""
        if not is_new or not discord.aktiv:
            return
        text = message.text or ""
        if settings.discord_funkdaten:
            teile = []
            if message.hops is not None:
                teile.append(f"{message.hops} Hops")
            if message.snr is not None:
                teile.append(f"SNR {message.snr:.1f} dB")
            if teile:
                text = f"{text}\n-# {' · '.join(teile)}"
        discord.post(message.channel, message.sender, text)

    # Ohne Filter meldet die Karten-Quelle Neuzugaenge aus ganz Oesterreich —
    # gemessen am 01.09.2026 in gut zwei Minuten sieben Knoten, davon keiner
    # aus Kaernten. Der eigene Observer hoerte nur die Nachbarschaft, deshalb
    # fiel das vorher nicht an.
    knoten_muster = (re.compile(settings.discord_knoten_muster, re.IGNORECASE)
                     if settings.discord_knoten_muster else None)

    def _knoten_an_discord(art: str, pubkey: str, name, alter_name) -> None:
        if not settings.discord_knoten or not discord.aktiv:
            return
        if time.monotonic() < knoten_ab:
            return
        if knoten_muster and not knoten_muster.search(name or alter_name or ""):
            return
        angezeigt = name or pubkey[:8]
        if art == "neu":
            discord.post("_knoten", "Netzwache", f"neuer Knoten **{angezeigt}**")
        else:
            discord.post("_knoten", "Netzwache",
                         f"**{alter_name}** heisst jetzt **{angezeigt}**")

    def _weiterreichen(message, is_new: bool) -> None:
        broadcaster.publish(message, is_new)
        _an_discord(message, is_new)

    # Welche Quelle laeuft, entscheidet allein die .env. Beide bedienen
    # dieselbe Flaeche — Zaehler, connected, quiet_seconds —, deshalb aendert
    # sich unterhalb dieser Zeile nichts.
    bauen = KartenQuelle if settings.quelle == "http" else Collector
    collector = bauen(settings, store, on_message=_weiterreichen,
                      on_node=_knoten_an_discord)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broadcaster.bind_loop(asyncio.get_running_loop())
        removed = store.purge(settings.retention_days)
        logger.info("Start: %d Kanaele, %d alte Nachrichten entfernt",
                    len(settings.channels), removed)
        discord.start()
        collector.start()
        purge_task = asyncio.create_task(_purge_loop())
        wacht_task = asyncio.create_task(_kanalwacht_loop())
        try:
            yield
        finally:
            purge_task.cancel()
            wacht_task.cancel()
            collector.stop()
            discord.stop()
            store.close()

    async def _kanalwacht_loop() -> None:
        """Einmal am Tag melden, welche Kanaele gelaufen sind.

        Geprueft wird stuendlich, gemeldet zur eingestellten Stunde — ein
        Wecker auf die Minute genau waere hier Aufwand ohne Gewinn.
        """
        gemeldet_am = None
        while True:
            await asyncio.sleep(600)
            wacht = getattr(collector, "kanalwacht", None)
            if wacht is None or "_knoten" not in settings.discord_webhooks:
                continue
            jetzt = time.localtime()
            if jetzt.tm_hour != settings.kanalwacht_stunde or gemeldet_am == jetzt.tm_yday:
                continue
            bericht = wacht.bericht()
            gemeldet_am = jetzt.tm_yday
            if bericht:
                discord.post("_knoten", "Kanalwacht", bericht)
                logger.info("Kanalwacht gemeldet: %d benannt, %d unbekannt",
                            len(wacht.benannt), len(wacht.unbekannt))
            wacht.tag_abschliessen()

    async def _purge_loop() -> None:
        while True:
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)
            try:
                removed = store.purge(settings.retention_days)
                if removed:
                    logger.info("Aufbewahrung: %d Nachrichten entfernt", removed)
            except Exception:
                logger.exception("Aufraeumen fehlgeschlagen")

    app = FastAPI(title=settings.site_title, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.collector = collector
    app.state.discord = discord
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _channel_payload() -> list[dict]:
        stats = store.stats()
        return [
            {
                "slug": channel.slug,
                "name": channel.name,
                "label": channel.label,
                "count": stats.get(channel.slug, {}).get("count", 0),
                "last_seen": stats.get(channel.slug, {}).get("last_seen"),
            }
            for channel in settings.channels.all()
        ]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        config = json.dumps({
            "title": settings.site_title,
            "tagline": settings.site_tagline,
            "linkUrl": settings.site_link_url,
            "linkLabel": settings.site_link_label,
            "logoUrl": settings.site_logo_url,
            "retentionDays": settings.retention_days,
            "pageSize": settings.page_size,
            "channels": _channel_payload(),
            "observers": store.observers(),
            "alsoUrl": settings.site_also_url,
            "alsoLabel": settings.site_also_label,
            "note": settings.site_note,
            "imprintUrl": settings.site_imprint_url,
            "imprintLabel": settings.site_imprint_label,
        }, ensure_ascii=False)
        if settings.site_favicon_url:
            html = html.replace("__MESHFEED_FAVICON__", settings.site_favicon_url)
        else:
            # Ohne Bild die Zeilen ganz weglassen — ein leeres href laedt sonst
            # die Seite selbst als Icon.
            html = "\n".join(line for line in html.splitlines()
                             if "__MESHFEED_FAVICON__" not in line)
        return HTMLResponse(
            html.replace("__MESHFEED_THEME__", settings.theme)
                .replace("__MESHFEED_VERSION__", _asset_version(settings.theme))
                .replace("__MESHFEED_CONFIG__", config)
        )

    @app.get("/c/{slug}", response_class=HTMLResponse)
    async def channel_page(slug: str) -> HTMLResponse:
        if settings.channels.by_slug(slug) is None:
            raise HTTPException(status_code=404, detail="Kanal nicht konfiguriert")
        return await index()

    @app.get("/api/channels")
    async def api_channels() -> JSONResponse:
        return JSONResponse({"channels": _channel_payload()})

    @app.get("/api/messages")
    async def api_messages(
        channel: Optional[str] = None,
        limit: int = Query(default=0, ge=0, le=500),
        before: Optional[int] = Query(default=None, ge=1),
    ) -> JSONResponse:
        if channel and settings.channels.by_slug(channel) is None:
            raise HTTPException(status_code=404, detail="Kanal nicht konfiguriert")
        rows = store.recent(channel=channel, limit=limit or settings.page_size, before_id=before)
        return JSONResponse({"messages": attach_paths(store, rows)})

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse({
            "connected": collector.connected.is_set(),
            "packets_seen": collector.packets_seen,
            "messages_received": collector.messages_received,
            "messages_decoded": collector.messages_decoded,
            "adverts_seen": collector.adverts_seen,
            "known_nodes": store.known_nodes(),
            "last_error": collector.last_error,
            "last_packet_at": (
                int(collector.last_packet_at) if collector.last_packet_at else None
            ),
            "quiet_seconds": int(collector.quiet_seconds()),
            "channels": _channel_payload(),
            "observers": store.observers(),
            "server_time": int(time.time()),
        })

    @app.get("/api/stream")
    async def api_stream(request: Request) -> StreamingResponse:
        queue = broadcaster.subscribe()

        async def events():
            try:
                yield ": verbunden\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=25)
                        yield f"data: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"  # haelt Proxys und Tunnel offen
            finally:
                broadcaster.unsubscribe(queue)

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Absichtlich nur der Prozess plus Datenbank. Ein ruhiges Funknetz ist
        # kein Fehler, und ein kurzer Broker-Ausfall soll nicht neu starten.
        # Der Discord-Spiegel steht bewusst nicht hier — siehe /healthz/discord.
        store.stats()
        return JSONResponse({"ok": True, "mqtt": collector.connected.is_set()})

    @app.get("/healthz/discord")
    async def healthz_discord() -> JSONResponse:
        """Kommt am anderen Ende noch etwas an?

        Getrennt von ``/healthz`` aus demselben Grund wie ``/healthz/quelle``:
        An ``/healthz`` haengt der Docker-Healthcheck, und der Fall, der hier
        rot wird — Token falsch, Webhook geloescht — heilt durch einen Neustart
        gerade **nicht**. Er heilt durch eine Hand an der ``.env``.

        Schlimmer noch: Ein Neustart wuerde es wieder versuchen, und genau
        dieses Wiederholen ist das, was Discord mit einer IP-Sperre beantwortet
        (10.000 ungueltige Anfragen in 10 Minuten). Deshalb legt der Spiegel
        einen Weg bei 401/403/404 endgueltig still und sagt es hier — statt
        einen Neustartkreis anzuwerfen.

        Ohne konfigurierte Webhooks ist der Endpunkt gruen und meldet ``aus``:
        Nicht eingeschaltet ist kein Fehler.
        """
        stats = discord.stats()
        ok = not discord.alle_stillgelegt
        return JSONResponse({"ok": ok, **stats}, status_code=200 if ok else 503)

    @app.get("/healthz/quelle")
    async def healthz_quelle() -> JSONResponse:
        """Kommt ueberhaupt noch etwas herein?

        Getrennt von ``/healthz``, und das mit Absicht: An ``/healthz`` haengt
        der Docker-Healthcheck, und ein Neustart des Feeds wuerde hier gar
        nichts heilen. Die Ursache liegt regelmaessig eine Etage hoeher, bei
        der Bridge auf dem anderen Host. Dieser Endpunkt ist reine Sicht —
        gedacht fuer einen Monitor, der Bescheid sagt, nicht fuer etwas, das
        von selbst neu startet.

        Vorgeschichte: Am 28.8.2026 kam die Bridge nach einem Stromausfall vor
        ihrem Broker hoch, strich ihn aus der Zielliste und verband sich nie
        wieder. Der Feed blieb dabei ``healthy``, MQTT verbunden, Topic
        abonniert — nur kam nichts mehr. 35 Stunden lang hat das niemand
        bemerkt, weil keine einzige Stelle "laeuft, aber es fliesst nichts"
        abgedeckt hat.
        """
        still = collector.quiet_seconds()
        grenze = settings.quelle_still_minuten * 60
        ok = still < grenze
        antwort = {
            "ok": ok,
            # Welche Quelle laeuft, gehoert hierher: Wer vor einem stillen
            # Dienst steht, will nicht erst in der .env nachsehen muessen,
            # wo er ueberhaupt suchen soll.
            "quelle": settings.quelle,
            "verbunden": collector.connected.is_set(),
            # Alter Name, bleibt fuer alles stehen, was ihn schon abfragt.
            "mqtt": collector.connected.is_set(),
            "still_seit_s": int(still),
            "grenze_s": grenze,
            "letztes_paket": (
                int(collector.last_packet_at) if collector.last_packet_at else None
            ),
            "pakete_gesamt": collector.messages_received,
        }
        if hasattr(collector, "stats"):
            antwort["karte"] = collector.stats()
        return JSONResponse(antwort, status_code=200 if ok else 503)

    return app


app = create_app()
