"""Tests für das NRW-Route-Backend (offline, gegen den Fake-EFA-Server).

Die Fixtures in ``tests/fixtures/`` sind synthetisch (siehe ``_comment`` in den
Dateien) – sie prüfen die Normalisierung der dokumentierten EFA-Struktur, sie
sind **keine** echten Fahrplandaten.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app as nrw_app

ROOT = Path(__file__).resolve().parents[1]

#: Ziel-Datum für die Verbindungssuche: eine Woche in der Zukunft, damit die
#: Tests unabhängig vom aktuellen Datum gültig bleiben (das Backend lehnt
#: Fahrplandaten weit in der Vergangenheit bewusst ab).
TARGET_DATE = (datetime.now(timezone.utc) + timedelta(days=7)).date()
TARGET_ISO = TARGET_DATE.isoformat()


def berlin_hhmm(utc_time: str) -> str:
    """Erwartete Ortszeit (Europe/Berlin) für einen UTC-Zeitstempel am TARGET_DATE."""
    dt = datetime.fromisoformat(f"{TARGET_ISO}T{utc_time}:00+00:00")
    return nrw_app.to_berlin(dt).strftime("%H:%M")


# --------------------------------------------------------------------------- #
# Zeitzonen und Klassifizierung
# --------------------------------------------------------------------------- #


def test_parse_efa_datetime_converts_utc_to_berlin_summer():
    winter = nrw_app.parse_efa_datetime("2026-01-15T06:35:00Z")
    summer = nrw_app.parse_efa_datetime("2026-07-15T06:35:00Z")
    assert winter.strftime("%H:%M") == "07:35"  # MEZ = UTC+1
    assert summer.strftime("%H:%M") == "08:35"  # MESZ = UTC+2


def test_parse_efa_datetime_handles_naive_and_legacy_formats():
    naive = nrw_app.parse_efa_datetime("2026-07-15T08:35:00")
    assert naive.strftime("%H:%M") == "08:35"
    legacy = nrw_app.parse_efa_datetime("20260715 08:35")
    assert legacy.strftime("%H:%M") == "08:35"
    assert nrw_app.parse_efa_datetime(None) is None
    assert nrw_app.parse_efa_datetime("keine Zeit") is None


@pytest.mark.parametrize(
    ("product", "line", "expected"),
    [
        ({"name": "S-Bahn", "class": 1}, "S1", "sbahn"),
        ({"name": "U-Bahn", "class": 2}, "U35", "ubahn"),
        ({"name": "Straßenbahn", "class": 3}, "107", "tram"),
        ({"name": "Stadtbus", "class": 4}, "146", "bus"),
        ({"name": "Regionalzug", "class": 0}, "RE1", "regional"),
        ({}, "RB42", "regional"),
        ({}, "ICE 10", "fern"),
        ({"name": "Fähre"}, "F1", "ferry"),
        ({}, "X", "other"),
    ],
)
def test_classify_mot(product, line, expected):
    assert nrw_app.classify_mot(product, line) == expected


def test_extract_system_messages_translates_known_errors():
    payload = {
        "systemMessages": [
            {"code": -8020, "error": "origin: no matches", "type": "error", "module": "BROKER"},
            {"code": 1, "text": "Hinweis zur Linie", "type": "message", "module": "X"},
        ]
    }
    result = nrw_app.extract_system_messages(payload)
    assert result["errors"][0]["text"] == "Der Startort wurde nicht gefunden."
    assert result["errors"][0]["original"] == "origin: no matches"
    assert result["messages"][0]["text"] == "Hinweis zur Linie"


# --------------------------------------------------------------------------- #
# /api/health
# --------------------------------------------------------------------------- #


def test_health_reports_reachable_upstream(client, fake_efa):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["efa_reachable"] is True
    assert data["efa_base_url"] == fake_efa.base_url


# --------------------------------------------------------------------------- #
# /api/locations (Autovervollständigung)
# --------------------------------------------------------------------------- #


def test_locations_returns_stops_and_uses_stopfinder(client, fake_efa):
    response = client.get("/api/locations?q=Essen&limit=3")
    assert response.status_code == 200
    data = response.get_json()

    assert data["ok"] is True
    assert data["count"] == 3

    best = data["results"][0]
    assert best["id"] == "de:05113:9289"
    assert best["name"] == "Hbf"
    assert best["place"] == "Essen"
    assert best["stop_id"] == "20009289"
    assert best["is_best"] is True
    assert "sbahn" in best["products"]

    params = fake_efa.last_params("XML_STOPFINDER_REQUEST")
    assert params["name_sf"] == "Essen"
    assert params["outputFormat"] == "rapidJSON"
    assert params["anyObjFilter_sf"] == "2"


def test_locations_requires_minimum_length(client, fake_efa):
    response = client.get("/api/locations?q=E")
    assert response.status_code == 200
    assert response.get_json()["results"] == []
    assert fake_efa.params_for("XML_STOPFINDER_REQUEST") == []


# --------------------------------------------------------------------------- #
# /api/journeys
# ---------------------------------------------------------------------------


def _journey_payload(**overrides):
    payload = {
        "origin": {"id": "de:05113:9289", "name": "Hbf", "place": "Essen", "stop_id": "20009289"},
        "destination": {"id": "de:05913:1702", "name": "Hbf", "place": "Dortmund", "stop_id": "20001702"},
        "date": TARGET_ISO,
        "time": "08:30",
        "arrive_by": False,
        "realtime": True,
        "max_journeys": 4,
    }
    payload.update(overrides)
    return payload


def test_journeys_normalizes_times_delays_and_legs(client, fake_efa):
    fake_efa.set_trip_fixture(TARGET_DATE)
    response = client.post("/api/journeys", json=_journey_payload())
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()

    assert data["ok"] is True
    assert data["count"] == 2

    # Parameter, die wirklich an EFA gehen
    params = fake_efa.last_params("XML_TRIP_REQUEST2")
    assert params["outputFormat"] == "rapidJSON"
    assert params["itdDate"] == TARGET_DATE.strftime("%Y%m%d")
    assert params["itdTime"] == "0830"
    assert params["itdTripDateTimeDepArr"] == "dep"
    assert params["type_origin"] == "stop"
    assert params["name_origin"] == "de:05113:9289"
    assert params["type_destination"] == "stop"
    assert params["name_destination"] == "de:05913:1702"
    assert params["calcNumberOfTrips"] == "4"
    assert params["useRealtime"] == "1"

    first, second = data["journeys"]

    # --- erste Verbindung: S1 mit Echtzeitdaten --------------------------
    assert first["realtime"] is True
    assert first["interchanges"] == 0
    assert first["lines"] == ["S1"]
    assert first["departure"]["time"] == berlin_hhmm("06:35")
    assert first["departure"]["estimated_time"] == berlin_hhmm("06:37")
    assert first["departure"]["delay_min"] == 2
    assert first["departure"]["platform"] == "3"
    assert first["arrival"]["time"] == berlin_hhmm("07:12")
    assert first["arrival"]["estimated_time"] == berlin_hhmm("07:14")
    assert first["arrival"]["day_offset"] == 0
    assert first["duration_min"] == 37

    leg = first["legs"][0]
    assert leg["kind"] == "pt"
    assert leg["mot"] == "sbahn"
    assert leg["line"] == "S1"
    assert leg["operator"] == "DB Regio AG"
    assert leg["direction"] == "Dortmund Hbf"
    assert leg["trip_code"] == 4711
    assert leg["stops_available"] is True
    assert [stop["name"] for stop in leg["stops"]] == [
        "Essen Hbf",
        "Bochum Hbf",
        "Dortmund Hbf",
    ]
    assert leg["stops"][0]["platform"] == "3"
    assert leg["stops"][0]["departure_time"] == berlin_hhmm("06:35")
    assert leg["stops"][0]["departure_estimated_time"] == berlin_hhmm("06:37")
    assert leg["stops"][1]["arrival_time"] == berlin_hhmm("06:52")
    assert leg["stops"][1]["stop_id"] == "20001032"
    assert leg["stops"][1]["place"] == "Bochum"

    # --- zweite Verbindung: Umstieg, keine Echtzeitdaten -----------------
    assert second["realtime"] is False
    assert second["interchanges"] == 1
    assert second["lines"] == ["RE1", "U35"]
    assert second["departure"]["time"] == berlin_hhmm("06:52")
    assert second["departure"]["estimated_time"] is None
    assert second["departure"]["delay_min"] is None
    # Zielknoten liefert nur departureTime* → Fallback auf dieses Feld
    assert second["arrival"]["time"] == berlin_hhmm("07:38")
    assert second["duration_min"] == 46

    kinds = [leg["kind"] for leg in second["legs"]]
    assert kinds == ["pt", "footway", "pt"]

    transfer = second["legs"][1]
    assert transfer["mot"] == "walk"
    assert transfer["duration_min"] == 4

    regional = second["legs"][0]
    assert regional["mot"] == "regional"
    assert regional["infos"], "EFA-Meldung sollte übernommen werden"
    assert "Bauarbeiten" in regional["infos"][0]["title"]
    assert "<b>" not in regional["infos"][0]["text"]

    ubahn = second["legs"][2]
    assert ubahn["mot"] == "ubahn"
    assert ubahn["operator"] == "Bogestra"

    # Keine erfundenen Echtzeitwerte
    assert data["source"]["realtime_available"] is True
    assert data["source"]["timezone"] == "Europe/Berlin"


def test_journeys_with_arrive_by_sets_arrival_parameter(client, fake_efa):
    fake_efa.set_trip_fixture(TARGET_DATE)
    response = client.post("/api/journeys", json=_journey_payload(arrive_by=True, time="09:30"))
    assert response.status_code == 200
    params = fake_efa.last_params("XML_TRIP_REQUEST2")
    assert params["itdTripDateTimeDepArr"] == "arr"
    assert params["itdTime"] == "0930"


def test_journeys_resolves_free_text_via_stopfinder(client, fake_efa):
    fake_efa.set_trip_fixture(TARGET_DATE)
    response = client.post(
        "/api/journeys",
        json=_journey_payload(origin="Essen", destination={"id": "de:05913:1702", "name": "Hbf"}),
    )
    assert response.status_code == 200
    data = response.get_json()

    assert data["resolved"]["origin"]["id"] == "de:05113:9289"
    assert data["resolved"]["origin"]["resolved_from_text"] is True
    assert any("aufgelöst" in note for note in data["notes"])

    params = fake_efa.last_params("XML_TRIP_REQUEST2")
    assert params["name_origin"] == "de:05113:9289"
    # StopFinder wurde für die Auflösung genutzt
    assert fake_efa.params_for("XML_STOPFINDER_REQUEST")


def test_journeys_unknown_stop_returns_404(client, fake_efa):
    fake_efa.set_trip_fixture(TARGET_DATE)
    fake_efa.clear_stopfinder()
    response = client.post("/api/journeys", json=_journey_payload(origin="Gibtsnichtville"))
    assert response.status_code == 404
    error = response.get_json()["error"]
    assert error["code"] == "stop_not_found"
    assert "Startort" in error["message"]


def test_journeys_without_connections_returns_404(client, fake_efa):
    fake_efa.set_trip_empty()
    response = client.post("/api/journeys", json=_journey_payload())
    assert response.status_code == 404
    error = response.get_json()["error"]
    assert error["code"] == "no_journeys"
    assert "keine Verbindung" in error["message"]


def test_journeys_reports_upstream_error(client, fake_efa):
    fake_efa.set_trip_fixture(TARGET_DATE)
    fake_efa.forced_status["XML_TRIP_REQUEST2"] = 500
    response = client.post("/api/journeys", json=_journey_payload())
    assert response.status_code == 502
    error = response.get_json()["error"]
    assert error["code"] == "efa_http_error"


def test_journeys_invalid_date_falls_back_to_today(client, fake_efa):
    """Ein unlesbares Datum wird durch das heutige Datum ersetzt (kein Absturz)."""
    fake_efa.set_trip_fixture(TARGET_DATE)
    response = client.post("/api/journeys", json=_journey_payload(date="15.07.2026"))
    assert response.status_code == 200
    params = fake_efa.last_params("XML_TRIP_REQUEST2")
    today = datetime.now(timezone.utc).date().strftime("%Y%m%d")
    assert params["itdDate"] in {today, TARGET_DATE.strftime("%Y%m%d")}


def test_journeys_rejects_date_far_in_past(client, fake_efa):
    fake_efa.set_trip_fixture(TARGET_DATE)
    old = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    response = client.post("/api/journeys", json=_journey_payload(date=old))
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "date_out_of_range"


def test_journeys_requires_json_body(client, fake_efa):
    response = client.post("/api/journeys", data="kein json", content_type="text/plain")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# /api/leg-stops
# --------------------------------------------------------------------------- #


def test_leg_stops_returns_stop_sequence(client, fake_efa):
    response = client.post(
        "/api/leg-stops",
        json={
            "line_stateless": "vrr:25001: :H",
            "stop_id": "20009289",
            "trip_code": 4711,
            "date": "2026-07-15",
            "time": "06:35",
        },
    )
    assert response.status_code == 200
    data = response.get_json()

    assert data["ok"] is True
    assert data["available"] is True
    assert [stop["name"] for stop in data["stops"]] == ["Essen Hbf", "Bochum Hbf", "Dortmund Hbf"]

    params = fake_efa.last_params("XML_STOPSEQCOORD_REQUEST")
    assert params["line"] == "vrr:25001: :H"
    assert params["stop"] == "20009289"
    assert params["tripCode"] == "4711"
    assert params["date"] == "20260715"
    assert params["time"] == "0635"
    assert params["useRealtime"] == "1"


def test_leg_stops_requires_reference(client, fake_efa):
    response = client.post("/api/leg-stops", json={"line": "S1"})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "missing_trip_reference"


# --------------------------------------------------------------------------- #
# Frontend-Vertrag
# --------------------------------------------------------------------------- #


def test_index_and_static_files_exist():
    assert (ROOT / "index.html").is_file()
    assert (ROOT / "static" / "css" / "styles.css").is_file()
    assert (ROOT / "static" / "js" / "app.js").is_file()
    assert (ROOT / "requirements.txt").is_file()


def test_frontend_only_calls_known_api_routes(client):
    """Jeder im Frontend verwendete /api/-Pfad muss im Backend existieren."""
    sources = [
        (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8"),
        (ROOT / "index.html").read_text(encoding="utf-8"),
    ]
    referenced: set[str] = set()
    for source in sources:
        referenced.update(re.findall(r"/api/[a-z\-]+", source))

    assert referenced, "Das Frontend ruft offenbar keine API auf."

    known = {
        rule.rule
        for rule in nrw_app.app.url_map.iter_rules()
        if rule.rule.startswith("/api/")
    }
    unknown = {path for path in referenced if path not in known}
    assert not unknown, f"Frontend ruft unbekannte API-Pfade auf: {sorted(unknown)}"


def test_frontend_element_ids_exist():
    """Jede per getElementById referenzierte ID muss im HTML vorhanden sein."""
    js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "index.html").read_text(encoding="utf-8")

    referenced = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    dynamically_created = {"retryBtn"}  # wird im Fehlerzustand selbst erzeugt
    missing = {
        element_id
        for element_id in referenced - dynamically_created
        if f'id="{element_id}"' not in html
    }
    assert not missing, f"IDs fehlen im HTML: {sorted(missing)}"


def test_frontend_local_assets_exist():
    """Alle lokalen /static/-Verweise in HTML und JS müssen existieren."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

    paths = set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html))
    paths.update(re.findall(r'"(/static/[^"]+)"', js))

    assert paths, "Es wurden keine lokalen Assets referenziert."
    for path in sorted(paths):
        assert (ROOT / path.lstrip("/")).is_file(), f"Asset fehlt: {path}"


def test_index_contains_data_source_notice():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "kein offizielles Angebot des VRR" in html
    assert "EFA" in html


def test_api_returns_json_for_unknown_route(client):
    response = client.get("/api/gibtsnicht")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "NRW Route" in response.get_data(as_text=True)
