#!/usr/bin/env python3
"""Erzeugt das Daten-Bundle für den jsdom-Frontendtest und führt ihn aus.

Ablauf (ohne Netzwerk):
1. Fake-EFA-Server starten (tests/fake_efa.py) mit Fixture-Daten.
2. Flask-Testclient (app.py) gegen diesen Fake-EFA laufen lassen und die
   Antworten von /api/health, /api/locations und /api/journeys einsammeln.
3. ``node render.test.js bundle.json`` aufrufen.

Aufruf:
    python tests/frontend/run.py

Voraussetzung: Node.js + jsdom (``cd tests/frontend && npm install``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_efa import FakeEfa  # noqa: E402

import app as nrw_app  # noqa: E402


def build_bundle() -> dict:
    target = date.today() + timedelta(days=7)

    fake = FakeEfa()
    fake.start()
    try:
        fake.set_trip_fixture(target)
        nrw_app.EFA_BASE_URL = fake.base_url
        nrw_app._cache.clear()
        client = nrw_app.app.test_client()

        health = client.get("/api/health").get_json()
        locations = client.get("/api/locations?q=Essen&limit=8").get_json()

        dortmund = json.loads(json.dumps(locations))
        dortmund["query"] = "Dortmund"
        dortmund["results"] = [
            {
                "id": "de:05913:1702",
                "stop_id": "20001702",
                "name": "Hbf",
                "full_name": "Dortmund, Hbf",
                "place": "Dortmund",
                "lat": 51.5178,
                "lon": 7.4592,
                "match_quality": 1000,
                "is_best": True,
                "products": ["regional", "sbahn", "ubahn", "tram", "bus"],
            }
        ]

        journeys = client.post(
            "/api/journeys",
            json={
                "origin": {"id": "de:05113:9289", "name": "Hbf", "place": "Essen", "stop_id": "20009289"},
                "destination": {"id": "de:05913:1702", "name": "Hbf", "place": "Dortmund"},
                "date": target.isoformat(),
                "time": "08:30",
                "realtime": True,
            },
        ).get_json()

        leg_stops = client.post(
            "/api/leg-stops",
            json={
                "line_stateless": "vrr:25001: :H",
                "stop_id": "20009289",
                "trip_code": 4711,
                "date": target.isoformat(),
                "time": "08:35",
            },
        ).get_json()
    finally:
        fake.stop()
        nrw_app._cache.clear()

    return {
        "health": health,
        "locations": locations,
        "locations_dortmund": dortmund,
        "journeys": journeys,
        "leg_stops": leg_stops,
    }


def main() -> int:
    if shutil.which("node") is None:
        print("Node.js wurde nicht gefunden. Bitte Node installieren (https://nodejs.org).")
        return 2

    if not (HERE / "node_modules" / "jsdom").exists():
        print("jsdom fehlt. Bitte zuerst ausführen:  cd tests/frontend && npm install")
        return 2

    bundle = build_bundle()
    with tempfile.TemporaryDirectory() as tmp:
        bundle_path = Path(tmp) / "bundle.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        if not bundle["journeys"].get("ok"):
            print("Warnung: /api/journeys lieferte keinen Erfolg – Testdaten prüfen.")

        result = subprocess.run(  # noqa: S603 - feste Argumente
            ["node", str(HERE / "render.test.js"), str(bundle_path)],
            cwd=str(HERE),
            check=False,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
