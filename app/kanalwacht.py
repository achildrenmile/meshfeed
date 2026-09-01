"""Kanalwacht: welche Kanaele im Netz ueberhaupt laufen.

Der Spiegel sieht ohnehin **jedes** Kanalpaket, auch die, deren Schluessel wir
nicht haben. Daraus laesst sich zaehlen, wie viele Kanaele es gibt und wie viel
auf ihnen los ist — ohne einen davon zu spiegeln.

Gemessen am 01.09.2026: von 309 Kanalpaketen in 6,5 Stunden waren **229 nicht
entschluesselbar**, verteilt auf 33 Hashes. Der groesste unbenannte Kanal trug
allein 28 % des Kanalverkehrs.

**Wo die Grenze liegt.** Bei einem Hashtag-Kanal ergibt sich der Schluessel aus
dem Namen — deshalb laesst sich raten, wie einer heisst, und die Karte macht
genau das. Wir tun es auch, aber ausschliesslich zum **Benennen**: gezaehlt wird
je Kanal, der Inhalt fremder Kanaele wird weder gespeichert noch angezeigt noch
gespiegelt. Was sich nicht benennen laesst, ist entweder ein privater Kanal oder
ein Name, der nicht auf der Liste steht — unterscheiden kann man das nicht, und
die Meldung behauptet es deshalb auch nicht.

**Ein Byte sind 256 Faecher.** Die Zahl der unbekannten Hashes ist eine
Untergrenze fuer die Zahl der Kanaele, keine genaue Angabe: Am 01.09. lagen
unter ``0x11`` sowohl entschluesselte Public-Pakete als auch fehlgeschlagene.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Optional

from .decode import ChannelSet, decode_group_text
from .kandidaten import kandidaten_kanaele

logger = logging.getLogger("meshfeed.kanalwacht")


class Kanalwacht:
    def __init__(self, eigene: ChannelSet, raten: bool = True) -> None:
        self.eigene = eigene
        # Die Kandidaten kommen in einen eigenen Satz: Ein Treffer hier darf
        # nie dazu fuehren, dass eine fremde Nachricht in einem unserer
        # Kanaele landet.
        self.kandidaten = kandidaten_kanaele() if raten else ChannelSet()
        self.benannt: Counter[str] = Counter()
        self.unbekannt: Counter[str] = Counter()
        self.bekannte_hashes: set[str] = set()
        self.neue_hashes: set[str] = set()
        self.seit = time.time()
        # Beim ersten Bericht ist jeder Hash neu — das waeren drei Dutzend
        # "neu aufgetaucht" und kein Erkenntnisgewinn. Erst ab dem zweiten.
        self.erster_bericht = True

    def zaehle(self, raw: bytes, name: Optional[str], hash_hinweis: Optional[str]) -> None:
        """Ein Kanalpaket verbuchen.

        ``name`` ist gesetzt, wenn einer **unserer** Kanaele gepasst hat — dann
        ist nichts mehr zu raten. Sonst wird einmal die Kandidatenliste
        versucht, und wenn auch die nichts hergibt, zaehlt der Hash.
        """
        if name:
            self.benannt[name] += 1
            return

        treffer = decode_group_text(raw, self.kandidaten)
        if treffer is not None:
            # Benannt, aber nicht unserer: nur zaehlen. Der Text bleibt liegen.
            self.benannt[treffer.channel.name] += 1
            return

        schluessel = (hash_hinweis or "??").upper()
        self.unbekannt[schluessel] += 1
        if schluessel not in self.bekannte_hashes:
            self.bekannte_hashes.add(schluessel)
            self.neue_hashes.add(schluessel)

    # --- Meldung ---------------------------------------------------------

    def bericht(self) -> Optional[str]:
        """Eine Discord-Nachricht, oder None wenn es nichts zu sagen gibt."""
        if not self.benannt and not self.unbekannt:
            return None

        stunden = max((time.time() - self.seit) / 3600, 0.1)
        zeilen = [f"**Kanalwacht** — was in {stunden:.0f} h im Netz gelaufen ist"]

        if self.benannt:
            teile = [f"{n} {c}" for n, c in self.benannt.most_common(12)]
            zeilen.append(f"Benannt ({sum(self.benannt.values())} Pakete): " + ", ".join(teile))

        if self.unbekannt:
            teile = [f"`{h}` {c}" for h, c in self.unbekannt.most_common(8)]
            rest = len(self.unbekannt) - len(teile)
            zusatz = f" und {rest} weitere" if rest > 0 else ""
            zeilen.append(
                f"Ohne Schluessel ({sum(self.unbekannt.values())} Pakete auf "
                f"{len(self.unbekannt)} Hashes): " + ", ".join(teile) + zusatz)

        if self.neue_hashes and not self.erster_bericht:
            sortiert = sorted(self.neue_hashes)
            gezeigt = ", ".join(f"`{h}`" for h in sortiert[:10])
            rest = len(sortiert) - 10
            zeilen.append(f"Neu aufgetaucht: {gezeigt}"
                          + (f" und {rest} weitere" if rest > 0 else ""))

        # Ein Byte, 256 Faecher: die Hashzahl ist eine Untergrenze. Das gehoert
        # in die Meldung, sonst liest sie sich genauer als sie ist.
        zeilen.append("-# Ein Kanal-Hash ist ein Byte — mehrere Kanaele koennen sich eines teilen. "
                      "Unbenannt heisst privat *oder* ungeratener Name.")
        return "\n".join(zeilen)

    def tag_abschliessen(self) -> None:
        """Zaehler zuruecksetzen, bekannte Hashes behalten."""
        self.benannt.clear()
        self.unbekannt.clear()
        self.neue_hashes.clear()
        self.seit = time.time()
        self.erster_bericht = False

    def stats(self) -> dict:
        return {
            "benannt": dict(self.benannt.most_common(20)),
            "unbekannt": dict(self.unbekannt.most_common(20)),
            "hashes_gesamt": len(self.bekannte_hashes),
        }
