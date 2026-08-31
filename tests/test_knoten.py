"""Tests fuer die Uebergaenge in ``Store.record_node``.

Adverts kommen rund 650 mal am Tag herein. Anzeigen will man davon nur die paar
Stellen, an denen sich wirklich etwas geaendert hat — und genau die muss
``record_node`` melden, ohne bei jedem zweiten Advert falschen Alarm zu geben.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import Store  # noqa: E402

PUB = "a1b2c3d4e5f60718"


def store() -> Store:
    return Store(":memory:")


def test_erster_advert_ist_ein_neuer_knoten():
    s = store()
    assert s.record_node(PUB, "AT-VL-Noetsch") == ("neu", None)


def test_zweiter_advert_desselben_knotens_meldet_nichts():
    s = store()
    s.record_node(PUB, "AT-VL-Noetsch")
    assert s.record_node(PUB, "AT-VL-Noetsch") is None


def test_namenswechsel_wird_gemeldet():
    s = store()
    s.record_node(PUB, "AT-VL-KF-TEST-01")
    assert s.record_node(PUB, "AT-VL-Noetsch") == ("umbenannt", "AT-VL-KF-TEST-01")


def test_advert_ohne_namen_ist_keine_umbenennung():
    # Ein Advert ohne Namensfeld loescht den bekannten Namen nicht (siehe
    # record_node) — dann darf es auch keine Umbenennung melden.
    s = store()
    s.record_node(PUB, "AT-VL-Noetsch")
    assert s.record_node(PUB, None) is None
    assert s.record_node(PUB, "") is None


def test_grossschreibung_des_pubkeys_ist_derselbe_knoten():
    s = store()
    s.record_node(PUB.upper(), "AT-VL-Noetsch")
    assert s.record_node(PUB.lower(), "AT-VL-Noetsch") is None
