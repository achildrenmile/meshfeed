"""``app.main`` legt beim Import bereits eine App an und liest dafuer die
Umgebung. Fuer die Tests genuegen Minimalwerte; eine Verbindung wird dabei nicht
aufgebaut, das passiert erst im Lifespan."""

import os

os.environ.setdefault("MQTT_HOST", "127.0.0.1")
os.environ.setdefault("CHANNELS", "#test=Test")
os.environ.setdefault("DB_PATH", ":memory:")
