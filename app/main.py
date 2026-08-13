"""Webteil: Seite, JSON-API, Live-Strom.

Nur lesend. Es gibt keinen Endpunkt, der etwas ins Funknetz schiebt — Senden
laeuft ueber meshinfra und bleibt dort.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .collector import Collector
from .config import Settings
from .store import StoredMessage, Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("meshfeed")

STATIC_DIR = Path(__file__).parent / "static"
PURGE_INTERVAL_SECONDS = 3600


class Broadcaster:
    """Verteilt neue Nachrichten an alle offenen SSE-Verbindungen.

    Der MQTT-Ingest laeuft in einem eigenen Thread, die Warteschlangen gehoeren
    dem Eventloop — deshalb geht alles ueber ``call_soon_threadsafe``.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[str]] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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
        payload = json.dumps({"new": is_new, "message": message.as_dict()}, ensure_ascii=False)
        self._loop.call_soon_threadsafe(self._fanout, payload)

    def _fanout(self, payload: str) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Langsamer Client: lieber eine Nachricht verlieren als den
                # Ingest ausbremsen. Beim Neuladen holt er sich alles per API.
                self._queues.discard(queue)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    broadcaster = Broadcaster()
    collector = Collector(settings, store, on_message=broadcaster.publish)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broadcaster.bind_loop(asyncio.get_running_loop())
        removed = store.purge(settings.retention_days)
        logger.info("Start: %d Kanaele, %d alte Nachrichten entfernt",
                    len(settings.channels), removed)
        collector.start()
        purge_task = asyncio.create_task(_purge_loop())
        try:
            yield
        finally:
            purge_task.cancel()
            collector.stop()
            store.close()

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
            "note": settings.site_note,
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
        return JSONResponse({"messages": [row.as_dict() for row in rows]})

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse({
            "connected": collector.connected.is_set(),
            "packets_seen": collector.packets_seen,
            "messages_decoded": collector.messages_decoded,
            "last_error": collector.last_error,
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
        store.stats()
        return JSONResponse({"ok": True, "mqtt": collector.connected.is_set()})

    return app


app = create_app()
