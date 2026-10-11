"""Fake-EFA-Server für Tests.

Simuliert die drei verwendeten Endpunkte der EFA-Schnittstelle mit den
Fixtures aus ``tests/fixtures/`` und protokolliert alle eingehenden
Parameter, damit die Tests die tatsächlich gesendeten Requests prüfen können.

Es wird **kein** Netzwerkzugriff benötigt.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from datetime import date
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?=T\d{2}:\d{2})")


def shift_dates(obj: Any, target: date) -> Any:
    """Verschiebt alle ISO-Zeitstempel im Fixture auf ``target`` (Uhrzeiten bleiben).

    So bleiben die Tests unabhängig vom aktuellen Datum gültig – das Backend
    lehnt Fahrplandaten weit in der Vergangenheit bewusst ab.
    """
    if isinstance(obj, dict):
        return {key: shift_dates(value, target) for key, value in obj.items()}
    if isinstance(obj, list):
        return [shift_dates(item, target) for item in obj]
    if isinstance(obj, str):
        return _ISO_RE.sub(target.isoformat(), obj)
    return obj


class FakeEfa:
    """Lokaler EFA-Ersatz auf einem freien Port."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.forced_status: dict[str, int] = {}
        self.forced_body: dict[str, str] = {}
        self.trip_response: dict[str, Any] = load_fixture("trip_sample.json")
        self.stopfinder_response: dict[str, Any] = load_fixture("stopfinder_essen.json")
        self.stopseq_response: dict[str, Any] = load_fixture("stopseq.json")

        app = Flask("fake-efa")

        @app.get("/<endpoint>")
        def handle(endpoint: str):  # noqa: ANN202 - Flask-View
            self.requests.append((endpoint, dict(request.args)))

            if endpoint in self.forced_body:
                return Response(
                    self.forced_body[endpoint],
                    status=self.forced_status.get(endpoint, 200),
                    mimetype="application/json",
                )
            if endpoint in self.forced_status:
                return Response(
                    json.dumps({"systemMessages": []}),
                    status=self.forced_status[endpoint],
                    mimetype="application/json",
                )

            if endpoint == "XML_STOPFINDER_REQUEST":
                query = (request.args.get("name_sf") or "").strip().lower()
                if query in {"", "xyz"} or "gibtsnicht" in query:
                    return jsonify({"version": "test", "systemMessages": [], "locations": []})
                return jsonify(self.stopfinder_response)
            if endpoint == "XML_TRIP_REQUEST2":
                return jsonify(self.trip_response)
            if endpoint == "XML_STOPSEQCOORD_REQUEST":
                return jsonify(self.stopseq_response)
            return jsonify({"systemMessages": [], "unknownEndpoint": endpoint})

        self.app = app
        self._server = None
        self._thread = None
        self.base_url = ""

    # -- Lebenszyklus ------------------------------------------------------- #

    def start(self) -> str:
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- Hilfsfunktionen für Tests ------------------------------------------ #

    def reset(self) -> None:
        self.requests.clear()

    def set_trip_fixture(self, target_date: date) -> dict[str, Any]:
        """Lädt das Verbindungs-Fixture und verschiebt es auf ``target_date``."""
        fixture = shift_dates(load_fixture("trip_sample.json"), target_date)
        self.trip_response = fixture
        return fixture

    def set_trip_empty(self) -> None:
        self.trip_response = copy.deepcopy(load_fixture("trip_empty.json"))

    def clear_stopfinder(self) -> None:
        self.stopfinder_response = {
            "version": "test",
            "systemMessages": [],
            "locations": [],
        }

    def params_for(self, endpoint: str) -> list[dict[str, str]]:
        return [params for name, params in self.requests if name == endpoint]

    def last_params(self, endpoint: str) -> dict[str, str]:
        found = self.params_for(endpoint)
        assert found, f"Kein Request an {endpoint} protokolliert."
        return found[-1]
