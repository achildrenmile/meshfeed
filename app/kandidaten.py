"""Namensliste, um fremde Hashtag-Kanaele zu **benennen** — mehr nicht.

Bei einem Hashtag-Kanal ist der Schluessel die SHA256-Ableitung aus dem Namen.
Wer den Namen errät, kann den Kanal benennen; genau so macht es auch die
Karte mit ihrer Probierliste.

Benutzt wird das ausschliesslich fuer die Zaehlung in ``kanalwacht.py``: Ein
Treffer sagt „auf diesem Hash lief ein Kanal namens X" und sonst nichts. Der
Text solcher Pakete wird nirgends gespeichert, angezeigt oder gespiegelt.

Wer die Liste erweitert, sollte sich das vorher noch einmal durchlesen.
"""

from __future__ import annotations

from .decode import PUBLIC_CHANNEL_KEY, Channel, ChannelSet, derive_hashtag_key

# Nach Herkunft sortiert, damit man sieht, wonach ueberhaupt gesucht wird.
NAMEN = (
    # Oesterreich, Land und Bundeslaender
    "at at-ktn at-ktn-bot at-ooe at-noe at-stmk at-sbg at-tirol at-vbg at-bgld "
    "at-w at-sued austria oesterreich"
    # Staedte und Regionen
    " wien vienna graz linz salzburg innsbruck klagenfurt villach bregenz "
    "eisenstadt stpoelten spittal lienz wolfsberg villach-land gailtal drautal "
    "lavanttal moelltal karawanken koralpe pack"
    # Nachbarn
    " de bayern muenchen it italia italy si slo slovenia ljubljana hu hungary "
    "sk cz ch schweiz"
    # Themen, die im MeshCore-Netz vorkommen
    " test testing bot bots chat talk tech technik weather wetter alerts alarm "
    "notfunk emcom sota dx ham funk lora meshcore meshcore-at mesh meshtastic "
    "wardriving coffee beer bier gaming public general offtopic news info "
    "hilfe help support handel market kleinanzeigen sensors sensoren solar "
    "repeater nodes karte map"
    # Eigene und benachbarte Gruppen
    " kf oeradio oevsv kaernten-funkt carinthiamesh"
).split()


def kandidaten_kanaele() -> ChannelSet:
    """Alle Kandidaten als ChannelSet — getrennt von den eigenen Kanaelen."""
    satz = ChannelSet()
    # Public zuerst: sein Schluessel ist **nicht** die Hashtag-Ableitung von
    # "#public", sondern ein fester, allgemein bekannter Wert. Ohne diese
    # Zeile zaehlt der meistbenutzte Kanal ueberhaupt als unbekannter Hash.
    satz.add(Channel(name="Public", label="Public", key=PUBLIC_CHANNEL_KEY, slug="public"))
    for name in NAMEN:
        voll = "#" + name
        satz.add(Channel(name=voll, label=voll, key=derive_hashtag_key(voll), slug=name))
    return satz
