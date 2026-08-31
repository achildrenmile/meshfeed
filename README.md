# meshfeed — Kanalnachrichten aus dem Mesh im Web

Zeigt die Nachrichten eines oder mehrerer **MeshCore-Kanäle** als Webseite an.
Die Quelle ist ein bestehender **Observer**: dessen Bridge schiebt jedes gehörte
Paket nach MQTT, meshfeed liest dort mit, entschlüsselt die Kanalnachrichten und
stellt sie dar — live, ohne Neuladen.

Erste Instanz: **#kf** im CarinthiaMesh, erreichbar unter
`mesh.kaernten-funkt.at`.

---

## 1. Was es tut, und was nicht

**Es tut:**

- hängt lesend am MQTT-Broker eines Observer-Stacks
- entschlüsselt GRP_TXT-Pakete der konfigurierten Kanäle
- fasst Mehrfachempfänge derselben Nachricht zu einer Zeile zusammen
- zeigt zu jedem Empfang **den Weg, den das Paket genommen hat**, mit Namen
  statt Hex, soweit bekannt (siehe unten)
- hält eine Historie (Vorgabe 30 Tage) und liefert Live-Updates per SSE

In der Fußzeile steht, **von welchen Observern** mitgehört wird. Die Namen kommen
aus den Daten, nicht aus der Konfiguration — und sie sind zugleich die Grenze der
Anzeige: Was kein Observer empfängt, fehlt. Eine Lücke ist also nicht zwingend
ein Fehler, sondern meist schlicht Funk.

**Es tut nicht:**

- **nichts senden.** Kein Endpunkt schreibt ins Funknetz. Senden läuft über
  [meshinfra](https://github.com/achildrenmile/meshinfra) und bleibt dort
- **keine zweite Verbindung zum Node.** Ein MeshCore-Companion nimmt nur zwei
  gleichzeitige TCP-Clients an; die sind mit Observer-Bridge und meshinfra
  belegt. Ein dritter Client erzeugt `health check failed`. meshfeed hängt
  deshalb ausschließlich am Broker, nie am Node
- **keine Direktnachrichten.** DMs sind zwischen zwei Nodes ECDH-verschlüsselt
  und für einen Mithörer nicht lesbar. Nur Kanäle mit bekanntem Schlüssel

### Wege durchs Netz

Jedes geflutete Paket trägt den Weg mit, den es genommen hat: eine Liste von
Repeatern. Genannt wird darin aber nicht der ganze Public Key, sondern nur
dessen **erste ein bis zwei Bytes** — und beides kommt in denselben Daten vor,
je nach Firmware des weiterreichenden Repeaters.

Die Namen dazu stammen aus **Adverts**, die ohnehin über dieselbe MQTT-Verbindung
hereinkommen. Der Observer dekodiert sie bereits samt Signaturprüfung; meshfeed
übernimmt Public Key, Name und Modus in eine Tabelle `nodes` und löst die
Präfixe erst beim Ausliefern auf. Das ist Absicht: Ein Advert, das heute abend
eintrifft, benennt damit rückwirkend auch die Wege von gestern.

Bei einem Byte gibt es nur 256 mögliche Werte, Doppelbelegungen sind also normal.
Deshalb wird nichts geraten:

- **genau ein Treffer** — der Name steht da
- **mehrere Treffer, davon genau ein Repeater** — weitergereicht wird nur von
  Repeatern, damit ist es entschieden
- **mehrere Repeater** — es stehen alle Kandidaten da, mit „oder" verbunden
- **kein Treffer** — das Präfix bleibt als Hex stehen. Heißt nur: zu diesem
  Knoten haben wir noch kein Advert gehört

Weil sich die Wege der Mehrfachempfänge gerade unterscheiden, liegt jeder Empfang
einzeln in der Tabelle `receptions`. Die Zeile in `messages` bleibt daneben die
verdichtete Sicht (bester SNR, kürzester Weg). In `/api/messages` steht das unter
`heard`. Nachrichten von vor dieser Änderung haben keine Wege — dort wurde nur
die Anzahl der Hops gespeichert, nicht der Weg selbst.

## 2. Architektur

```
Funk  ->  Companion-Node  ->  Observer-Bridge  ->  Mosquitto (Observer-Host)
                                                        |
                                                        | meshcore/+/+/packets  (nur lesen)
                                                        v
                                                     meshfeed  ->  SQLite
                                                        |
                                                   cloudflared
                                                        |
                                              mesh.<deine-domain>
```

Der Feed steht bewusst **hinter** dem Observer und nicht daneben: Er braucht
keine eigene Hardware, keinen eigenen Node und keine Änderung am laufenden
Observer-Stack. Fällt er aus, merkt das Funknetz nichts.

## 3. Entschlüsselung

Hashtag-Kanäle haben keinen geheimen Schlüssel — er ergibt sich aus dem Namen:

```
key16 = SHA256("#name")[:16]        # #kf -> 18d080f05b6e908c08301da15f2e1d74
```

Das Paket selbst:

```
payload      = channel_hash(1) + cipher_mac(2) + ciphertext(n*16)
channel_hash = erstes Byte von SHA256(key16)
MAC          = HMAC_SHA256(key16 + 16 Nullbytes, ciphertext)[:2]
cipher       = AES-128-ECB ohne Padding
plaintext    = timestamp(4, LE) + flags(1) + text, meist "Name: Nachricht"
```

Deshalb ist ein Hashtag-Kanal **nicht privat** — das steht auch so in der
offiziellen MeshCore-Doku. meshfeed macht nichts sichtbar, was nicht ohnehin
jedes Gerät in Funkreichweite mitlesen kann.

Für private Kanäle mit zufälligem Schlüssel gibt es `CHANNEL_KEYS`.

**Warum nicht die Entschlüsselung im Observer einschalten?** Die Bridge kann das
(`DECODE_HASHTAG_CHANNELS`), schickt den Klartext dann aber an **alle**
konfigurierten Broker — auch an den Analyzer-Upstream. Der eigene Decoder hält
den Klartext im eigenen Haus, und der Observer-Stack bleibt unangetastet.

## 4. Voraussetzungen

| | |
|---|---|
| **Observer** | läuft, Bridge publiziert nach MQTT (`meshcore/<IATA>/<PUBKEY>/packets`) |
| **Broker-Zugang** | Benutzer mit Leserecht auf dem lokalen Mosquitto des Observers |
| **Host** | irgendetwas mit Docker, ~100 MB RAM |
| **Domain** | Cloudflare-Zone, wenn es über einen Tunnel nach außen soll |

Benutzer auf dem Observer-Broker anlegen (ohne `-c`, sonst sind die bestehenden
Benutzer weg):

```bash
cd ~/stacks/meshcore/config/mosquitto
docker run --rm -v "$PWD":/work eclipse-mosquitto:2 \
  mosquitto_passwd -b /work/passwd meshfeed '<PASSWORT>'
docker run --rm -v "$PWD":/work alpine:3 \
  sh -c 'chown 1883:1883 /work/passwd && chmod 600 /work/passwd'
docker kill -s HUP meshcore-mosquitto     # liest die Passwortdatei neu ein
```

`HUP` statt `restart`: Der Broker liest die Passwortdatei neu ein, ohne die
Verbindung der Observer-Bridge zu unterbrechen.

## 5. Installation

```bash
git clone <dieses-repo> meshfeed && cd meshfeed
cp .env.example .env && chmod 600 .env
$EDITOR .env
docker compose up -d --build
curl -s localhost:3430/api/status
```

Erwartet: `"connected": true`. Der Rest kommt, sobald auf dem Kanal gefunkt
wird — bei ruhigem Netz kann das dauern, das ist kein Fehler.

### Cloudflare-Tunnel

```bash
cloudflared tunnel login                    # Zone der Zieldomain auswählen!
cloudflared tunnel create kfmesh-hostnode02
cloudflared tunnel route dns kfmesh-hostnode02 mesh.kaernten-funkt.at
```

Danach Credential-JSON und `config.yml` nach `./cloudflared/` legen.

> **Falle:** `cert.pem` gilt für **eine Zone**. Zeigt es auf eine andere Domain,
> legt `route dns` klaglos einen Eintrag `mesh.deine-domain.at.andere-zone.at`
> an, statt einen Fehler zu melden. Vor dem Routen prüfen, für welche Zone das
> Zertifikat ausgestellt wurde — oder den CNAME direkt im Dashboard setzen:
> `mesh` → `<TUNNEL-ID>.cfargotunnel.com`, Proxy an.
{.is-warning}

## 6. Konfiguration

| Variable | Bedeutung |
|---|---|
| `MQTT_HOST` / `MQTT_PORT` | Broker des Observers |
| `MQTT_USER` / `MQTT_PASS` | Zugang, nur lesend nötig |
| `MQTT_TOPICS` | Vorgabe `meshcore/+/+/packets`, mehrere per Komma |
| `CHANNELS` | `#kf=Kärnten funkt,#at-ktn` — Schlüssel wird abgeleitet, `public` = fester Public-Key |
| `CHANNEL_KEYS` | `name=<32 Hex>=Beschriftung` für private Kanäle |
| `SITE_TITLE` / `SITE_TAGLINE` | Kopfzeile |
| `SITE_LINK_URL` / `SITE_LINK_LABEL` | Link zurück zur Hauptseite |
| `SITE_LOGO_URL` | Bild im Kopfbalken, z. B. `/static/logo.jpg`. Leer = kein Logo |
| `SITE_FAVICON_URL` | Icon in der Tableiste. Leer = das Logo wird genommen |
| `SITE_ALSO_URL` / `SITE_ALSO_LABEL` | Link in der Fußzeile auf einen zweiten Weg zum Mitlesen, etwa die Kanalansicht einer Mesh-Karte |
| `SITE_NOTE` | Zusatz in der Fußzeile, etwa der Standort des Observers |
| `THEME` | Dateiname ohne `.css` aus `app/static/themes/`. Mitgeliefert: `plain`, `kf` |
| `RETENTION_DAYS` | Vorgabe 30, `0` = nie löschen |
| `HOST_PORT` | Port auf `127.0.0.1`, Vorgabe 3430 |
| `INSTANCE_NAME` | Präfix für Container, Volume und MQTT-Client-ID |

Mehrere Kanäle in einer Instanz ergeben Reiter in der Oberfläche und eigene
Adressen unter `/c/<slug>`.

### Aussehen ändern

Zwei Dateien, mehr nicht:

- `app/static/base.css` — der **Aufbau**: was wo liegt. Bleibt, wie es ist
- `app/static/themes/<name>.css` — die **Farben**: setzt nur CSS-Variablen

Ein eigenes Theme ist eine Kopie von `themes/plain.css` mit anderen Werten,
dazu `THEME=<name>` in der `.env`. Die Variablen:

| Variable | wirkt auf |
|---|---|
| `--bar-bg` / `--bar-ink` | Kopfbalken |
| `--bg` / `--bg-alt` / `--card` | Seite, Kanalleiste und Fußzeile, Nachrichtenkarten |
| `--ink` / `--muted` / `--line` | Text, Nebentext, Linien |
| `--accent` / `--accent-ink` / `--accent-soft` | Kanalstreifen, aktiver Reiter, Aufblitzen bei neuer Nachricht |
| `--font`, `--radius`, `--shadow` | Schrift, Rundung, Schatten |

`kf.css` übernimmt die Farben des Auftritts von kaernten-funkt.at (Oliv
`#3d4f2f`, Sand `#f5f5ec`, Gold `#b8860b`) und bleibt bewusst nur hell — der
Hauptauftritt hat kein dunkles Schema. `plain.css` folgt der Systemeinstellung.

Ein unbekannter Themename fällt still auf `plain` zurück, damit eine falsche
`.env` die Seite nicht unbenutzbar macht.

### Zweite Instanz

Ein zweiter Feed ist eine **Kopie dieses Verzeichnisses** mit eigener `.env` —
dasselbe Image, andere Kanäle, andere Domain, eigener Tunnel, eigenes Volume:

```bash
cp -r meshfeed feed-oeradio && cd feed-oeradio
$EDITOR .env      # INSTANCE_NAME, CHANNELS, SITE_*, HOST_PORT, Tunnel-ID
docker compose up -d
```

Wichtig sind vier Werte: `INSTANCE_NAME` (sonst kollidieren Containernamen),
`HOST_PORT` (sonst der Port), `CHANNELS` und der Tunnel in `cloudflared/`.

## 7. API

| Endpunkt | Zweck |
|---|---|
| `GET /api/channels` | konfigurierte Kanäle mit Anzahl und letztem Empfang |
| `GET /api/messages?channel=&limit=&before=` | Nachrichten, neueste zuerst |
| `GET /api/stream` | Server-Sent Events, jede neue Nachricht sofort |
| `GET /api/status` | Broker-Verbindung, Zähler, letzter Fehler, Stille seit |
| `GET /healthz` | Healthcheck |
| `GET /healthz/quelle` | 200 solange etwas hereinkommt, sonst **503** |

`/healthz` prüft **nicht** den Funkverkehr. Ein ruhiges Netz ist kein Fehler,
und ein kurzer Broker-Ausfall soll den Container nicht neu starten.

### `/healthz/quelle` — läuft, aber fließt auch etwas?

Der teuerste Fehler ist nicht der, bei dem etwas umfällt, sondern der, bei dem
alles grün bleibt und trotzdem nichts ankommt. Am 28.08.2026 kam die
Observer-Bridge nach einem Stromausfall **vor** ihrem Broker hoch, strich ihn
aus der Zielliste und verband sich nie wieder. Der Feed blieb dabei `healthy`,
MQTT verbunden, Topic abonniert — nur kam nichts mehr. Gemerkt hat es 35
Stunden lang niemand.

Deshalb ein zweiter Endpunkt. Er misst, wann zuletzt **irgendein** Paket über
MQTT hereinkam, vor jedem Filter:

```json
{"ok": true, "mqtt": true, "still_seit_s": 12, "grenze_s": 1800,
 "letztes_paket": 1787998844, "pakete_gesamt": 41273}
```

Zwei Entscheidungen dahinter:

- **Nicht die letzte Kanalnachricht.** Auf `#kf` vergeht auch mal ein Tag, das
  ist normal. Der Observer dagegen hört dauernd etwas, Adverts allein schon
  alle paar Minuten. Stille *hier* heißt: Leitung tot, nicht Netz ruhig.
- **Getrennt von `/healthz`.** An `/healthz` hängt der Docker-Healthcheck. Ein
  Neustart des Feeds würde nichts heilen — die Ursache liegt eine Etage höher.
  Dieser Endpunkt ist für einen Monitor gedacht, der Bescheid sagt, nicht für
  etwas, das von selbst neu startet.

Grenze über `QUELLE_STILL_MINUTEN`, Vorgabe 30.

## 8. Betrieb

```bash
docker compose ps
docker compose logs -f app
docker compose up -d --build            # nach Änderungen
```

Die Datenbank liegt im Volume `<instanz>_data` unter `/data/meshfeed.db`.
Sichern heißt: Datei kopieren (SQLite, WAL-Modus).

## 9. Wenn etwas nicht geht

| Symptom | Ursache | Behebung |
|---|---|---|
| `"connected": false` | Zugangsdaten oder Broker-Port | `docker compose logs app`, Benutzer auf dem Observer-Broker prüfen |
| verbunden, `packets_seen` bleibt 0 | falsches Topic oder Observer hört nichts | Topic gegen `meshcore/<IATA>/<PUBKEY>/packets` halten, Bridge-Logs des Observers ansehen |
| `packets_seen` steigt, `messages_decoded` bleibt 0 | Kanal nicht konfiguriert | `CHANNELS` prüfen — nur Kanäle mit passendem Schlüssel werden entschlüsselt |
| Seite lädt, bleibt aber leer | schlicht kein Verkehr | Zähler in `/api/status` ansehen, Geduld |
| Seite lädt, alles grün, seit Stunden nichts Neues | Quelle liefert nicht mehr | `curl -s localhost:3430/healthz/quelle`. Bei 503 liegt es nicht am Feed — Bridge auf dem Observer-Host prüfen, `docker logs meshcore-bridge \| grep -oE 'MQTT: [0-9]+/[0-9]+' \| tail -1` |
| Live-Anzeige springt auf „verbinde neu" | Proxy schneidet die SSE-Verbindung | Tunnel-Logs prüfen; der Browser verbindet selbst neu |
| Umlaute zerschossen | Nachricht kam so über die Luft | nichts zu tun, MeshCore überträgt UTF-8 |

## 10. Sicherheit

- Der Container läuft als unprivilegierter Benutzer und lauscht nur auf
  `127.0.0.1`. Nach außen geht ausschließlich der Tunnel
- `.env`, `cloudflared/*.json` und die Broker-Zugangsdaten gehören nicht ins
  Git — siehe `.gitignore`
- Der Broker-Benutzer braucht nur Leserecht. Wenn der Observer-Stack ACLs führt,
  dort `topic read meshcore/#` eintragen
- Angezeigt wird nur, was ohnehin öffentlich über die Luft geht. Private Kanäle
  nur dann eintragen, wenn ihre Mitglieder das wissen

## 11. Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

`requirements-dev.txt` zieht `requirements.txt` mit und legt `pytest` und
`httpx` dazu. `httpx` braucht allein der TestClient und kommt deshalb nicht
ins Image.

Die Kanalpakete in den Tests sind **selbst gebaut**, nicht mitgeschnitten: Ein
echtes GRP_TXT-Paket eines Hashtag-Kanals kann jeder entschlüsseln — es hier
abzulegen hieße, die Nachricht einer realen Person dauerhaft zu veröffentlichen.
Der Paketaufbau ist gegen echten Verkehr geprüft und im Testhelfer nachgebaut.
