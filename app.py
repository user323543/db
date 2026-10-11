#!/usr/bin/env python3
"""
NRW Route – Backend (Flask)
===========================

Fahrplanauskunft für Nordrhein-Westfalen auf Basis der dokumentierten
EFA-Schnittstelle (Mentz) der VRR-Instanz ``https://efa.vrr.de/standard``.

Das Backend hat zwei Aufgaben:

1. **Proxy**: Der Browser kann EFA nicht direkt aufrufen (CORS). Das Backend
   stellt die HTTP-Anfragen an EFA und gibt die Antworten normalisiert weiter.
2. **Normalisierung**: EFA liefert verschachteltes JSON mit englischen
   Fehlercodes, UTC-Zeitstempeln und inkonsistenten Feldnamen. Hier wird daraus
   ein stabiles, dokumentiertes JSON-Format für das Frontend.

Es werden **ausschließlich** Daten ausgeliefert, die EFA tatsächlich
zurückgegeben hat. Es gibt keine Beispiel-, Mock- oder Fallback-Fahrzeiten.

Endpunkte
---------
GET  /                     → Frontend (index.html)
GET  /api/health           → Erreichbarkeit der EFA-Schnittstelle
GET  /api/locations?q=     → Haltestellen-Autovervollständigung
POST /api/journeys         → Verbindungssuche
POST /api/leg-stops        → Haltestellenfolge eines Teilwegs (Fallback)

Siehe scripts/efa-api-notes.md für die Herleitung der Parameter.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

#: EFA-Instanz. Überschreibbar per Umgebungsvariable, z. B.
#:   EFA_BASE_URL=https://app.vrr.de/vrrstd
EFA_BASE_URL = os.environ.get("EFA_BASE_URL", "https://efa.vrr.de/standard").rstrip("/")

#: Timeout je EFA-Anfrage in Sekunden.
EFA_TIMEOUT = float(os.environ.get("EFA_TIMEOUT", "15"))

#: CORS für das Frontend freischalten (standardmäßig aus, Flask liefert das
#: Frontend selbst aus → CORS ist normalerweise nicht nötig).
CORS_ENABLED = os.environ.get("NRW_ROUTE_CORS", "").strip() in {"1", "true", "yes"}

#: Cache-Zeiten in Sekunden (Fair Use gegenüber der öffentlichen Schnittstelle).
CACHE_TTL_LOCATIONS = float(os.environ.get("CACHE_TTL_LOCATIONS", "300"))
CACHE_TTL_JOURNEYS = float(os.environ.get("CACHE_TTL_JOURNEYS", "30"))
CACHE_TTL_HEALTH = float(os.environ.get("CACHE_TTL_HEALTH", "120"))

MAX_JOURNEYS_HARD_LIMIT = 8
MAX_STOPS_PER_LEG = 80
USER_AGENT = os.environ.get(
    "EFA_USER_AGENT",
    "NRW-Route/1.0 (Fahrplanauskunft NRW, privates Projekt)",
)

ENDPOINTS = {
    "stopfinder": "XML_STOPFINDER_REQUEST",
    "trip": "XML_TRIP_REQUEST2",
    "stopseq": "XML_STOPSEQCOORD_REQUEST",
    "departures": "XML_DM_REQUEST",
}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("nrw-route")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.json.ensure_ascii = False  # Umlaute lesbar ausliefern

# --------------------------------------------------------------------------- #
# Zeitzone Europe/Berlin
# --------------------------------------------------------------------------- #
# ``zoneinfo`` nutzt auf Linux/macOS die Systemdatenbank. Unter Windows fehlt
# sie; dort liefert das Paket ``tzdata`` (siehe requirements.txt) die Daten.
# Zusätzlich gibt es einen reinen Python-Fallback, damit die Anwendung auch
# ohne tzdata korrekt rechnet (EU-Sommerzeitregel).

try:  # pragma: no cover - Umgebungsabhängig
    from zoneinfo import ZoneInfo

    BERLIN_TZ: Any = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover - z. B. Windows ohne tzdata
    BERLIN_TZ = None
    log.warning(
        "zoneinfo/Europe/Berlin nicht verfügbar – nutze internen EU-Sommerzeit-Fallback."
    )


def _eu_dst_offset_hours(utc_dt: datetime) -> int:
    """Sommerzeit-Offset für Europe/Berlin (+1 Winter, +2 Sommer).

    EU-Regel: Sommerzeit von 01:00 UTC am letzten Sonntag im März bis
    01:00 UTC am letzten Sonntag im Oktober.
    """
    year = utc_dt.year

    def last_sunday(month: int) -> date:
        if month == 12:
            end = date(year, 12, 31)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)
        while end.weekday() != 6:  # 6 = Sonntag
            end -= timedelta(days=1)
        return end

    dst_start = datetime.combine(last_sunday(3), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1)
    dst_end = datetime.combine(last_sunday(10), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1)
    return 2 if dst_start <= utc_dt < dst_end else 1


def to_berlin(dt: datetime) -> datetime:
    """Rechnet einen Zeitstempel in die Zeitzone Europe/Berlin um."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if BERLIN_TZ is not None:
        return dt.astimezone(BERLIN_TZ)
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.astimezone(timezone(timedelta(hours=_eu_dst_offset_hours(utc_dt))))


# --------------------------------------------------------------------------- #
# Fehlerbehandlung
# --------------------------------------------------------------------------- #


@dataclass
class ApiError(Exception):
    """Fehler, der als strukturiertes JSON an das Frontend geht."""

    message: str
    code: str = "efa_error"
    status: int = 502
    hint: str | None = None
    details: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - nur für Logs
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "message": self.message, "status": self.status},
        }
        if self.hint:
            payload["error"]["hint"] = self.hint
        if self.details:
            payload["error"]["details"] = self.details
        return payload


@app.errorhandler(ApiError)
def _handle_api_error(exc: ApiError):
    if exc.status >= 500:
        log.warning("API-Fehler: %s", exc)
    return jsonify(exc.to_dict()), exc.status


@app.errorhandler(404)
def _handle_404(_exc):
    if request.path.startswith("/api/"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "not_found",
                        "message": f"Unbekannter API-Pfad: {request.path}",
                        "status": 404,
                    },
                }
            ),
            404,
        )
    return jsonify({"ok": False, "error": {"code": "not_found", "message": "Seite nicht gefunden"}}), 404


@app.errorhandler(Exception)
def _handle_unexpected(exc: Exception):  # pragma: no cover - Sicherheitsnetz
    # "Echte" HTTP-Fehler (z. B. 405) unverändert durchreichen.
    if isinstance(exc, HTTPException):
        return exc
    log.exception("Unerwarteter Fehler: %s", exc)
    message = (
        "Unerwarteter Serverfehler im NRW-Route-Backend."
        if request.path.startswith("/api/")
        else "Unerwarteter Serverfehler."
    )
    return jsonify({"ok": False, "error": {"code": "internal_error", "message": message, "status": 500}}), 500


@app.after_request
def _cors_headers(response):
    """Optionales CORS (nur wenn NRW_ROUTE_CORS aktiviert ist)."""
    if CORS_ENABLED:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# --------------------------------------------------------------------------- #
# Kleiner In-Memory-Cache (Fair Use)
# --------------------------------------------------------------------------- #

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def cache_get(key: str, ttl: float) -> Any | None:
    if ttl <= 0:
        return None
    with _cache_lock:
        entry = _cache.get(key)
    if not entry:
        return None
    stored_at, value = entry
    if time.time() - stored_at > ttl:
        with _cache_lock:
            _cache.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any) -> None:
    with _cache_lock:
        # Cache nicht unbegrenzt wachsen lassen.
        if len(_cache) > 512:
            _cache.clear()
        _cache[key] = (time.time(), value)


# --------------------------------------------------------------------------- #
# EFA-HTTP-Zugriff
# --------------------------------------------------------------------------- #

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


def efa_get(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """Fragt einen EFA-Endpunkt ab und liefert das geparste JSON.

    Es wird genau ein Wiederholungsversuch bei Verbindungsfehlern gemacht
    (kein Retry-Sturm gegenüber der öffentlichen Schnittstelle).
    """
    url = f"{EFA_BASE_URL}/{endpoint}"
    clean = {k: v for k, v in params.items() if v is not None}

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = _session.get(url, params=clean, timeout=EFA_TIMEOUT)
            break
        except requests.Timeout as exc:
            last_error = exc
            log.warning("Timeout bei %s (Versuch %d)", url, attempt + 1)
        except requests.RequestException as exc:
            last_error = exc
            log.warning("Verbindungsfehler bei %s: %s", url, exc)
    else:  # beide Versuche fehlgeschlagen
        raise ApiError(
            message=(
                "Die Fahrplan-Schnittstelle (EFA/VRR) ist nicht erreichbar. "
                "Bitte Internetverbindung prüfen und erneut versuchen."
            ),
            code="efa_unreachable",
            status=503,
            hint=f"Ziel: {url}",
            details=[str(last_error)] if last_error else [],
        )

    if response.status_code >= 400:
        raise ApiError(
            message=(
                f"Die Fahrplan-Schnittstelle hat mit HTTP {response.status_code} geantwortet."
            ),
            code="efa_http_error",
            status=502,
            hint=f"Ziel: {url}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiError(
            message="Die Antwort der Fahrplan-Schnittstelle war kein gültiges JSON.",
            code="efa_bad_response",
            status=502,
            hint="Möglicherweise blockiert ein Proxy oder eine Firewall die Anfrage.",
            details=[response.text[:300]],
        ) from exc

    if not isinstance(payload, dict):
        raise ApiError(
            message="Unerwartetes Antwortformat der Fahrplan-Schnittstelle.",
            code="efa_bad_response",
            status=502,
        )
    return payload


# --------------------------------------------------------------------------- #
# Helfer: Texte, Zeiten, Verkehrsmittel
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")

_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&szlig;": "ß",
    "&auml;": "ä",
    "&ouml;": "ö",
    "&uuml;": "ü",
    "&Auml;": "Ä",
    "&Ouml;": "Ö",
    "&Uuml;": "Ü",
}


def clean_text(value: Any) -> str:
    """Entfernt HTML und Normalisiert Leerraum aus EFA-Texten."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    for entity, replacement in _HTML_ENTITIES.items():
        text = text.replace(entity, replacement)
    text = _WS_RE.sub(" ", text).replace("\n", " ")
    return text.strip()


_DT_PATTERNS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%MZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y%m%d %H:%M:%S",
    "%Y%m%d %H:%M",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M",
    "%d.%m.%Y %H:%M",
)


def parse_efa_datetime(value: Any) -> datetime | None:
    """Parst EFA-Zeitstempel und liefert sie in Europe/Berlin.

    * Werte mit ``Z``/Offset sind absolut → Umrechnung nach Berlin.
    * Werte ohne Zeitzone sind Serverzeit (= Ortszeit) → als Berlin interpretiert.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    normalized = raw.replace(" ", "T", 1) if re.match(r"^\d{8} \d{4}", raw) else raw

    if normalized.endswith("Z"):
        try:
            dt = datetime.fromisoformat(normalized[:-1] + "+00:00")
        except ValueError:
            dt = None
        if dt is not None:
            return to_berlin(dt)

    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        dt = None
    if dt is not None:
        if dt.tzinfo is not None:
            return to_berlin(dt)
        return dt.replace(tzinfo=BERLIN_TZ) if BERLIN_TZ is not None else dt

    for pattern in _DT_PATTERNS:
        try:
            dt = datetime.strptime(raw, pattern)
        except ValueError:
            continue
        if pattern.endswith("Z") and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
            return to_berlin(dt)
        if dt.tzinfo is not None:
            return to_berlin(dt)
        return dt.replace(tzinfo=BERLIN_TZ) if BERLIN_TZ is not None else dt
    return None


def minutes_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return int(round((end - start).total_seconds() / 60))


def hhmm(dt: datetime | None) -> str | None:
    return dt.strftime("%H:%M") if dt else None


def iso_date(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d") if dt else None


# Verkehrsmittel-Normalisierung ------------------------------------------------

#: Reihenfolge ist wichtig: "stadtbahn" muss vor "bahn" geprüft werden usw.
_MOT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sbahn", ("s-bahn", "s bahn", "sbahn", "schnellbahn")),
    ("ubahn", ("u-bahn", "u bahn", "ubahn", "untergrundbahn", "metro")),
    ("tram", ("tram", "straßenbahn", "strassenbahn", "stadtbahn", "niederflurbahn")),
    ("bus", ("stadtbus", "regionalbus", "schnellbus", "nachtbus", "bürgerbus", "buergerbus", "bus", "ast", "taxi", "rufauto", "anrufsammeltaxi")),
    ("regional", ("regionalzug", "regionalbahn", "regionalexpress", "reisezug", "zug", "re ", "rb")),
    ("fern", ("fernverkehr", "fernzug", "ice", "ic ", "ec ", "intercity", "thalys", "rj", "nj")),
    ("ferry", ("fähre", "faehre", "schiff", "fähre", "fähranleger")),
    ("other", ("seilbahn", "zahnradbahn", "sonstige")),
)

_MOT_BY_PRODUCT_CLASS = {
    0: "regional",  # Zug
    1: "sbahn",  # S-Bahn
    2: "ubahn",  # U-Bahn
    3: "tram",  # Stadtbahn/Tram
    4: "bus",  # Stadtbus
    5: "bus",  # Regionalbus
    6: "bus",  # Schnellbus
    7: "other",  # Seil-/Zahnradbahn
    8: "ferry",  # Schiff
    9: "bus",  # AST/Rufbus
    10: "regional",  # sonstige Bahn
}

_LINE_PREFIX_MOT = (
    (re.compile(r"^S\s?\d", re.IGNORECASE), "sbahn"),
    (re.compile(r"^U\s?\d", re.IGNORECASE), "ubahn"),
    (re.compile(r"^(RE|RB|RRX|IRE|MRE|MRB)\s?\d", re.IGNORECASE), "regional"),
    (re.compile(r"^(ICE|IC|EC|EN|NJ|RJ|THA|FLX|TGV)\s?\d", re.IGNORECASE), "fern"),
)


def classify_mot(product: dict[str, Any] | None, line: str | None) -> str:
    """Bestimmt das Verkehrsmittel aus EFA-Produktdaten (Name/Klasse).

    Reihenfolge: Produktname → Produktklasse → Liniennummer. Es wird nichts
    geraten, was nicht aus den EFA-Daten ableitbar ist; im Zweifel "other".
    """
    product = product or {}
    name = clean_text(product.get("name") or product.get("productName") or "").lower()
    add_name = clean_text(product.get("addName") or "").lower()
    haystack = f"{name} {add_name}".strip()

    for mot, keywords in _MOT_KEYWORDS:
        for keyword in keywords:
            if keyword.strip() and keyword in haystack:
                return mot

    product_class = product.get("class")
    if isinstance(product_class, (int, float)):
        mapped = _MOT_BY_PRODUCT_CLASS.get(int(product_class))
        if mapped:
            return mapped

    if line:
        for pattern, mot in _LINE_PREFIX_MOT:
            if pattern.match(line.strip()):
                return mot
    return "other"


# --------------------------------------------------------------------------- #
# EFA-Systemmeldungen
# --------------------------------------------------------------------------- #

_ERROR_TEXT_MAP: tuple[tuple[str, str], ...] = (
    ("origin: no matches", "Der Startort wurde nicht gefunden."),
    ("destination: no matches", "Das Ziel wurde nicht gefunden."),
    ("no matches", "Die Haltestelle wurde nicht gefunden."),
    ("no input value", "Es fehlt eine Eingabe für Start oder Ziel."),
    ("invalid date", "Das gewählte Datum ist ungültig oder liegt außerhalb des Fahrplanzeitraums."),
    ("invalid time", "Die gewählte Uhrzeit ist ungültig."),
    ("no journeys", "Für diesen Zeitpunkt wurde keine Verbindung gefunden."),
    ("no connection", "Für diesen Zeitpunkt wurde keine Verbindung gefunden."),
)


def translate_system_message(text: str) -> str:
    lowered = (text or "").lower()
    for needle, german in _ERROR_TEXT_MAP:
        if needle in lowered:
            return german
    return clean_text(text)


def extract_system_messages(payload: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Trennt EFA-Systemmeldungen in Fehler und Hinweise."""
    errors: list[dict[str, str]] = []
    messages: list[dict[str, str]] = []
    for entry in payload.get("systemMessages") or []:
        if not isinstance(entry, dict):
            continue
        raw_text = entry.get("error") or entry.get("text") or entry.get("message") or ""
        text = translate_system_message(str(raw_text))
        if not text:
            continue
        item = {
            "text": text,
            "original": clean_text(raw_text),
            "module": clean_text(entry.get("module")),
            "code": str(entry.get("code", "")),
        }
        if str(entry.get("type", "")).lower() == "error" or entry.get("error"):
            errors.append(item)
        else:
            messages.append(item)
    return {"errors": errors, "messages": messages}


# --------------------------------------------------------------------------- #
# Normalisierung: Haltestellen (StopFinder)
# --------------------------------------------------------------------------- #


def _parent_chain_location(node: dict[str, Any]) -> tuple[str | None, str | None]:
    """Liest Ortsname und Haltestellen-Hierarchie aus ``parent``-Ketten."""
    place: str | None = None
    parent = node.get("parent")
    while isinstance(parent, dict):
        if parent.get("type") in {"locality", "place", "district"} and not place:
            place = clean_text(parent.get("name")) or None
        parent = parent.get("parent")
    return place, None


@dataclass
class ChainDetails:
    """Details, die EFA über ``parent``-Ketten verteilt (Plattform → Halt → Ort)."""

    platform: str | None = None
    name: str | None = None
    full_name: str | None = None
    id_code: str | None = None
    id_num: str | None = None
    place: str | None = None


def walk_chain(node: dict[str, Any]) -> ChainDetails:
    """Löst die ``parent``-Kette eines EFA-Haltepunkts auf.

    EFA liefert Haltepunkte teils als Plattform-Eintrag, dessen eigentliche
    Haltestelle im ``parent``-Baum steckt (``platform`` → ``stop`` → ``locality``).
    Der jeweils nächstgelegene Fund gewinnt.
    """
    details = ChainDetails()
    current: Any = node
    visited: set[int] = set()

    while isinstance(current, dict) and id(current) not in visited:
        visited.add(id(current))
        node_type = str(current.get("type") or "")
        properties = current.get("properties") or {}
        platform_here = clean_text(properties.get("platformName")) or clean_text(
            properties.get("platform")
        )

        if node_type in {"locality", "place", "district"}:
            if not details.place:
                details.place = clean_text(current.get("name")) or None
        elif node_type == "platform":
            if not details.platform:
                details.platform = platform_here or clean_text(current.get("name")) or None
        else:
            if not details.platform:
                details.platform = platform_here or None
            name = clean_text(current.get("disassembledName")) or clean_text(current.get("name"))
            if name and not details.name:
                details.name = name
                details.full_name = clean_text(current.get("name")) or name
            if not details.id_code and current.get("id"):
                details.id_code = str(current["id"])
            if not details.id_num and properties.get("stopId"):
                details.id_num = str(properties["stopId"])

        current = current.get("parent")

    return details


def normalize_location(node: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    loc_id = node.get("id")
    name = clean_text(node.get("name")) or clean_text(node.get("disassembledName"))
    if not loc_id or not name:
        return None

    properties = node.get("properties") or {}
    place, _ = _parent_chain_location(node)
    if not place:
        place = clean_text(properties.get("mainLocality")) or None

    short_name = clean_text(node.get("disassembledName"))
    if not short_name and place and name.lower().startswith(place.lower()):
        short_name = name[len(place) :].lstrip(", ").strip()

    coord = node.get("coord")
    lat = lon = None
    if isinstance(coord, (list, tuple)) and len(coord) >= 2:
        try:
            lat, lon = float(coord[0]), float(coord[1])
        except (TypeError, ValueError):
            lat = lon = None

    product_classes = node.get("productClasses") or []
    products: list[str] = []
    for cls in product_classes:
        mot = _MOT_BY_PRODUCT_CLASS.get(int(cls)) if isinstance(cls, (int, float)) else None
        if mot and mot not in products:
            products.append(mot)

    return {
        "id": str(loc_id),
        "stop_id": str(properties.get("stopId")) if properties.get("stopId") else None,
        "name": short_name or name,
        "full_name": name,
        "place": place,
        "lat": lat,
        "lon": lon,
        "match_quality": node.get("matchQuality"),
        "is_best": bool(node.get("isBest")),
        "products": products,
    }


def efa_stopfinder(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Haltestellensuche über ``XML_STOPFINDER_REQUEST``."""
    query = (query or "").strip()
    if len(query) < 2:
        return []

    cache_key = f"sf::{query.lower()}::{limit}"
    cached = cache_get(cache_key, CACHE_TTL_LOCATIONS)
    if cached is not None:
        return cached

    base_params = {
        "outputFormat": "rapidJSON",
        "locationServerActive": "1",
        "coordOutputFormat": "WGS84[DD.ddddd]",
        "name_sf": query,
        "anyMaxSizeHitList": str(max(limit * 2, 10)),
    }

    attempts: list[dict[str, Any]] = [
        {**base_params, "type_sf": "any", "anyObjFilter_sf": "2"},
        {**base_params, "type_sf": "stop"},
    ]

    results: list[dict[str, Any]] = []
    for params in attempts:
        payload = efa_get(ENDPOINTS["stopfinder"], params)
        locations = payload.get("locations") or []
        for node in locations:
            if not isinstance(node, dict):
                continue
            if node.get("type") not in (None, "stop", "platform", "single"):
                continue
            normalized = normalize_location(node)
            if normalized:
                results.append(normalized)
        if results:
            break

    # Duplikate (gleiche ID) entfernen, beste Treffer zuerst.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)

    unique.sort(key=lambda item: (not item["is_best"], -(item["match_quality"] or 0)))
    unique = unique[:limit]
    cache_set(cache_key, unique)
    return unique


# --------------------------------------------------------------------------- #
# Normalisierung: Teilwege und Verbindungen (Trip-Request)
# --------------------------------------------------------------------------- #


def _pick_time_pair(node: dict[str, Any], preferred: str) -> tuple[Any, Any]:
    """Liest ein Zeitpaar (planned, estimated).

    EFA nennt bei ``itdTripDateTimeDepArr=arr`` die Felder weiterhin
    ``departureTime*``; außerdem fehlen je nach Instanz einzelne Felder.
    Deshalb wird defensiv nach dem gewünschten Paar und dann nach dem anderen
    gesucht. Liegt kein Echtzeitwert vor, wird ``None`` zurückgegeben – das
    Frontend kennzeichnet solche Fahrten dann als „Fahrplanzeit“.
    """
    other = "arrival" if preferred == "departure" else "departure"
    planned = node.get(f"{preferred}TimePlanned")
    estimated = node.get(f"{preferred}TimeEstimated")
    if planned is None and estimated is None:
        planned = node.get(f"{other}TimePlanned")
        estimated = node.get(f"{other}TimeEstimated")
    return planned, estimated


def normalize_stop_point(node: dict[str, Any] | None, preferred: str) -> dict[str, Any] | None:
    """Normalisiert einen Haltepunkt (``leg.origin``/``leg.destination``).

    Die Zeiten stehen am übergebenen Knoten, Name/ID/Plattform können in der
    ``parent``-Kette liegen (siehe :func:`walk_chain`).
    """
    if not isinstance(node, dict):
        return None

    details = walk_chain(node)
    properties = node.get("properties") or {}
    place = details.place or clean_text(properties.get("mainLocality")) or None

    planned_raw, estimated_raw = _pick_time_pair(node, preferred)
    planned = parse_efa_datetime(planned_raw)
    estimated = parse_efa_datetime(estimated_raw)
    has_realtime_value = estimated is not None and estimated_raw is not None
    delay = minutes_between(planned, estimated) if (planned and estimated) else None

    name = details.name or clean_text(node.get("name"))
    if not name:
        return None

    return {
        "name": name,
        "full_name": details.full_name or clean_text(node.get("name")) or name,
        "place": place,
        "id": details.id_code,
        "stop_id": details.id_num,
        "platform": details.platform,
        "niveau": clean_text(node.get("niveau")) or None,
        "planned": planned.isoformat() if planned else None,
        "estimated": estimated.isoformat() if has_realtime_value else None,
        "time": hhmm(planned),
        "estimated_time": hhmm(estimated) if has_realtime_value else None,
        "delay_min": delay or None,
        "realtime": has_realtime_value,
        "cancelled": bool(node.get("isCancelled")),
        "_planned_dt": planned,
        "_estimated_dt": estimated if has_realtime_value else None,
    }


def _public_point(point: dict[str, Any] | None, reference: datetime | None = None) -> dict[str, Any] | None:
    """Entfernt interne Felder und ergänzt Tagesversatz für die Anzeige."""
    if not point:
        return None
    out = {k: v for k, v in point.items() if not k.startswith("_")}
    dt = point.get("_planned_dt")
    if dt is not None and reference is not None:
        out["day_offset"] = (dt.date() - reference.date()).days
    elif dt is not None:
        out["day_offset"] = 0
    return out


def normalize_infos(infos: Any) -> list[dict[str, str]]:
    """EFA-Meldungen (Störungen, Baustellen, Hinweise) zu einer Fahrt."""
    out: list[dict[str, str]] = []
    if not isinstance(infos, list):
        return out
    for entry in infos:
        if not isinstance(entry, dict):
            continue
        link = (entry.get("infoLinks") or [{}])[0] if isinstance(entry.get("infoLinks"), list) else {}
        title = clean_text(
            (link or {}).get("title") or entry.get("title") or entry.get("subject") or ""
        )
        text = clean_text(
            (link or {}).get("content")
            or (link or {}).get("additionalText")
            or entry.get("content")
            or entry.get("text")
            or ""
        )
        if not title and not text:
            continue
        out.append(
            {
                "title": title or text[:80],
                "text": text,
                "url": clean_text((link or {}).get("url") or entry.get("url") or "") or None,
            }
        )
    return out


def _is_cancelled(node: dict[str, Any]) -> bool:
    """Prüft ``isCancelled`` am Eintrag und in der ``parent``-Kette."""
    current: Any = node
    visited: set[int] = set()
    while isinstance(current, dict) and id(current) not in visited:
        visited.add(id(current))
        if current.get("isCancelled") is True:
            return True
        current = current.get("parent")
    return False


def normalize_stop_sequence(sequence: Any, limit: int = MAX_STOPS_PER_LEG) -> list[dict[str, Any]]:
    """Normalisiert ``leg.stopSequence`` bzw. ``locationSequence``.

    Aufbau je Eintrag (siehe scripts/efa-api-notes.md): ``type`` kann
    ``stop``/``platform``/``locality`` sein, Details stecken teils im
    ``parent``-Baum – deshalb wird die Kette aufgelöst.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(sequence, list):
        return out

    for entry in sequence:
        if not isinstance(entry, dict):
            continue

        details = walk_chain(entry)

        # Zeiten stehen am äußersten Eintrag; fehlen sie dort, wird der
        # Haltestellen-Knoten der Kette als Quelle genutzt.
        def _times_from(source: dict[str, Any]) -> tuple[Any, ...]:
            return (
                source.get("arrivalTimePlanned"),
                source.get("arrivalTimeEstimated"),
                source.get("departureTimePlanned"),
                source.get("departureTimeEstimated"),
            )

        raw_times = _times_from(entry)
        if not any(raw_times):
            chain_node: Any = entry.get("parent")
            while isinstance(chain_node, dict) and not any(raw_times):
                raw_times = _times_from(chain_node)
                chain_node = chain_node.get("parent")

        arr_planned = parse_efa_datetime(raw_times[0])
        arr_est = parse_efa_datetime(raw_times[1])
        dep_planned = parse_efa_datetime(raw_times[2])
        dep_est = parse_efa_datetime(raw_times[3])

        name = details.name or clean_text(entry.get("name"))
        if not name:
            continue

        arr_delay = minutes_between(arr_planned, arr_est) if arr_planned and arr_est else None
        dep_delay = minutes_between(dep_planned, dep_est) if dep_planned and dep_est else None

        out.append(
            {
                "name": name,
                "full_name": details.full_name or name,
                "place": details.place,
                "id": details.id_code,
                "stop_id": details.id_num,
                "platform": details.platform,
                "arrival_time": hhmm(arr_planned),
                "arrival_estimated_time": hhmm(arr_est) if arr_est else None,
                "arrival_delay_min": arr_delay,
                "departure_time": hhmm(dep_planned),
                "departure_estimated_time": hhmm(dep_est) if dep_est else None,
                "departure_delay_min": dep_delay,
                "realtime": bool(arr_est or dep_est),
                "cancelled": _is_cancelled(entry),
            }
        )
        if len(out) >= limit:
            break
    return out


def normalize_leg(leg: dict[str, Any], reference: datetime | None) -> dict[str, Any] | None:
    if not isinstance(leg, dict):
        return None

    transportation = leg.get("transportation") or {}
    properties = transportation.get("properties") or {}
    product = transportation.get("product") or {}

    origin = normalize_stop_point(leg.get("origin"), "departure")
    destination = normalize_stop_point(leg.get("destination"), "arrival")
    if origin is None and destination is None:
        return None

    line = clean_text(transportation.get("number")) or clean_text(
        transportation.get("disassembledName")
    ) or clean_text(transportation.get("name"))

    is_footway = not transportation
    mot = "walk" if is_footway else classify_mot(product, line)

    duration_raw = leg.get("duration")
    duration_min: int | None = None
    if isinstance(duration_raw, (int, float)) and duration_raw > 0:
        # EFA liefert die Dauer eines Teilwegs in Sekunden.
        duration_min = int(round(float(duration_raw) / 60))
    if duration_min is None and origin and destination:
        duration_min = minutes_between(
            origin.get("_planned_dt"), destination.get("_planned_dt")
        )

    stops = normalize_stop_sequence(leg.get("stopSequence"))
    stop_sequence_available = bool(stops) or isinstance(leg.get("stopSequence"), list)

    interchange = leg.get("interchange") or {}

    return {
        "kind": "footway" if is_footway else "pt",
        "mot": mot,
        "product": clean_text(product.get("name")) or ("Fußweg" if is_footway else None),
        "product_class": product.get("class") if isinstance(product.get("class"), (int, float)) else None,
        "line": line or None,
        "line_id": str(transportation.get("id")) if transportation.get("id") else None,
        "line_stateless": clean_text(transportation.get("stateless")) or None,
        "trip_code": properties.get("tripCode") if properties.get("tripCode") is not None else None,
        "operator": clean_text((transportation.get("operator") or {}).get("name")) or None,
        "direction": clean_text((transportation.get("destination") or {}).get("name")) or None,
        "duration_min": duration_min,
        "origin": _public_point(origin, reference),
        "destination": _public_point(destination, reference),
        "stops": stops,
        "stops_available": stop_sequence_available,
        "interchange": {
            "name": clean_text(interchange.get("desc")) or None,
            "type": clean_text(interchange.get("type")) or None,
        }
        if interchange
        else None,
        "infos": normalize_infos(leg.get("infos")) + normalize_infos(leg.get("hints")),
        "realtime": bool(leg.get("isRealtimeControlled")),
    }


def normalize_journey(journey: dict[str, Any], index: int) -> dict[str, Any] | None:
    if not isinstance(journey, dict):
        return None
    legs_raw = journey.get("legs") or []
    legs = [leg for leg in (normalize_leg(leg, None) for leg in legs_raw) if leg]
    if not legs:
        return None

    first, last = legs[0], legs[-1]
    departure = first.get("origin") or first.get("destination")
    arrival = last.get("destination") or last.get("origin")
    if not departure or not arrival:
        return None

    dep_dt = parse_efa_datetime(departure.get("estimated") or departure.get("planned"))
    arr_dt = parse_efa_datetime(arrival.get("estimated") or arrival.get("planned"))
    duration_min = minutes_between(dep_dt, arr_dt)
    if duration_min is None:
        duration_min = sum(leg.get("duration_min") or 0 for leg in legs) or None

    # Tagesversatz relativ zum Abfahrtstag ergänzen (Nachtverbindungen).
    reference = dep_dt
    for leg in legs:
        for key in ("origin", "destination"):
            point = leg.get(key)
            if point:
                point_dt = parse_efa_datetime(point.get("planned"))
                if point_dt is not None and reference is not None:
                    point["day_offset"] = (point_dt.date() - reference.date()).days
                else:
                    point.setdefault("day_offset", 0)

    lines = [leg["line"] for leg in legs if leg.get("kind") == "pt" and leg.get("line")]
    interchanges = journey.get("interchanges")
    if not isinstance(interchanges, int):
        interchanges = max(len([leg for leg in legs if leg.get("kind") == "pt"]) - 1, 0)

    infos: list[dict[str, str]] = []
    for leg in legs:
        infos.extend(leg.get("infos") or [])

    realtime = any(leg.get("realtime") for leg in legs)
    delay_min = departure.get("delay_min")

    return {
        "id": f"journey-{index}",
        "departure": departure,
        "arrival": arrival,
        "duration_min": duration_min,
        "interchanges": interchanges,
        "lines": lines,
        "legs": legs,
        "infos": infos,
        "realtime": realtime,
        "delay_min": delay_min if delay_min else 0,
        "is_additional": bool(journey.get("isAdditional")),
    }


# --------------------------------------------------------------------------- #
# Auflösung von Start/Ziel
# --------------------------------------------------------------------------- #


def _looks_like_efa_id(value: str) -> bool:
    """``de:05113:9289`` o. Ä. (DHID) – enthält Doppelpunkte und Buchstaben."""
    return bool(re.match(r"^[A-Za-z]{2,}[:_][\w:.\-]+$", value))


def _looks_like_numeric_id(value: str) -> bool:
    return bool(re.match(r"^\d{4,}$", value))


def _retry_with_numeric_stop_ids(
    params: dict[str, Any], origin: dict[str, Any], destination: dict[str, Any]
) -> dict[str, Any] | None:
    """Baut eine zweite Anfrage mit numerischen Stop-IDs (``type=stopID``).

    Manche EFA-Instanzen akzeptieren globale DHIDs (``de:…``) in der
    Verbindungssuche nur eingeschränkt. Liefert ``None``, wenn keine
    numerischen IDs bekannt sind.
    """
    retry = dict(params)
    changed = False
    for scope, endpoint in (("origin", origin), ("destination", destination)):
        location = endpoint.get("location") or {}
        candidate = location.get("stop_id") or endpoint.get("name")
        candidate = str(candidate or "").strip()
        if candidate and _looks_like_numeric_id(candidate):
            retry[f"type_{scope}"] = "stopID"
            retry[f"name_{scope}"] = candidate
            changed = True
    return retry if changed else None


def resolve_endpoint(raw: Any, label: str) -> dict[str, Any]:
    """Ermittelt aus der Frontend-Eingabe einen EFA-konformen Punkt.

    Akzeptiert:
      * eine bereits aufgelöste Haltestelle (``{"id": ..., "name": ...}``),
      * eine reine ID/Text-Zeichenkette.
    Freitext wird über den StopFinder aufgelöst – es wird nie eine ID geraten.
    """
    if isinstance(raw, dict):
        identifier = str(raw.get("id") or raw.get("stop_id") or "").strip()
        name = clean_text(raw.get("name") or raw.get("full_name"))
        place = clean_text(raw.get("place"))
        if identifier:
            if _looks_like_efa_id(identifier):
                efa_type, efa_name = "stop", identifier
            elif _looks_like_numeric_id(identifier):
                efa_type, efa_name = "stopID", identifier
            else:
                efa_type, efa_name = "stop", identifier
            return {
                "type": efa_type,
                "name": efa_name,
                "label": f"{place} {name}".strip() or identifier,
                "resolved": False,
                # Für den Eventualfall (DHID wird nicht akzeptiert) merken wir
                # uns die Haltestelle inkl. numerischer ID.
                "location": {
                    "id": identifier,
                    "stop_id": str(raw.get("stop_id")) if raw.get("stop_id") else None,
                    "name": name or identifier,
                    "place": place or None,
                },
            }
        raw = raw.get("name") or raw.get("text") or ""

    text = clean_text(raw)
    if not text:
        raise ApiError(
            message=f"Bitte einen {label} angeben.",
            code="missing_input",
            status=400,
        )

    if _looks_like_efa_id(text) or _looks_like_numeric_id(text):
        return {
            "type": "stop" if _looks_like_efa_id(text) else "stopID",
            "name": text,
            "label": text,
            "resolved": False,
        }

    matches = efa_stopfinder(text, limit=5)
    if not matches:
        raise ApiError(
            message=f"Zum {label} „{text}“ hat die Fahrplan-Schnittstelle keine Haltestelle gefunden.",
            code="stop_not_found",
            status=404,
            hint="Bitte Schreibweise prüfen oder einen Vorschlag aus der Autovervollständigung wählen.",
        )

    best = matches[0]
    identifier = best.get("id") or best.get("stop_id")
    efa_type = "stop" if _looks_like_efa_id(str(identifier)) else "stopID"
    return {
        "type": efa_type,
        "name": str(identifier),
        "label": f"{best.get('place') or ''} {best.get('name') or ''}".strip(),
        "resolved": True,
        "location": best,
        "matches": matches,
    }


# --------------------------------------------------------------------------- #
# Endpunkte
# --------------------------------------------------------------------------- #

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/health")
def api_health():
    """Prüft, ob die EFA-Schnittstelle erreichbar ist (für die Statusanzeige)."""
    cached = cache_get("health", CACHE_TTL_HEALTH)
    if cached is not None:
        return jsonify(cached)

    payload: dict[str, Any] = {
        "ok": True,
        "service": "NRW Route",
        "efa_base_url": EFA_BASE_URL,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        efa_get(
            ENDPOINTS["stopfinder"],
            {
                "outputFormat": "rapidJSON",
                "locationServerActive": "1",
                "type_sf": "stop",
                "name_sf": "Hauptbahnhof",
            },
        )
        payload["efa_reachable"] = True
        payload["message"] = "Fahrplan-Schnittstelle erreichbar."
    except ApiError as exc:
        payload["efa_reachable"] = False
        payload["message"] = exc.message
        payload["error_code"] = exc.code
    cache_set("health", payload)
    return jsonify(payload)


@app.get("/api/locations")
def api_locations():
    """Autovervollständigung: Haltestellen und Bahnhöfe suchen."""
    query = (request.args.get("q") or request.args.get("query") or "").strip()
    try:
        limit = int(request.args.get("limit") or 8)
    except ValueError:
        limit = 8
    limit = max(1, min(limit, 20))

    if len(query) < 2:
        return jsonify({"ok": True, "query": query, "results": [], "note": "Mindestens 2 Zeichen eingeben."})

    results = efa_stopfinder(query, limit=limit)
    return jsonify(
        {
            "ok": True,
            "query": query,
            "count": len(results),
            "results": results,
        }
    )


@app.post("/api/journeys")
def api_journeys():
    """Verbindungssuche über ``XML_TRIP_REQUEST2``."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(
            message="Ungültige Anfrage: JSON-Objekt erwartet.",
            code="bad_request",
            status=400,
        )

    origin = resolve_endpoint(data.get("origin"), "Startort")
    destination = resolve_endpoint(data.get("destination"), "Ziel")

    date_raw = clean_text(data.get("date"))
    time_raw = clean_text(data.get("time"))
    today = to_berlin(datetime.now(timezone.utc)).date()
    if not date_raw or not DATE_RE.match(date_raw):
        date_raw = today.isoformat()
    if not time_raw or not TIME_RE.match(time_raw):
        time_raw = to_berlin(datetime.now(timezone.utc)).strftime("%H:%M")

    try:
        wanted_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        raise ApiError(message="Ungültiges Datum.", code="bad_date", status=400)

    # EFA deckt einen begrenzten Fahrplanzeitraum ab (Vergangenheit weitgehend
    # leer). Wir blockieren nur offensichtlich unsinnige Werte.
    if (wanted_date - today).days > 400 or (today - wanted_date).days > 30:
        raise ApiError(
            message="Das gewählte Datum liegt außerhalb des üblichen Fahrplanzeitraums.",
            code="date_out_of_range",
            status=400,
            hint="Bitte ein Datum zwischen 30 Tagen zurück und etwa einem Jahr voraus wählen.",
        )

    arrive_by = bool(data.get("arrive_by"))
    realtime = data.get("realtime")
    realtime = True if realtime is None else bool(realtime)

    try:
        max_journeys = int(data.get("max_journeys") or 5)
    except (TypeError, ValueError):
        max_journeys = 5
    max_journeys = max(1, min(max_journeys, MAX_JOURNEYS_HARD_LIMIT))

    params: dict[str, Any] = {
        "outputFormat": "rapidJSON",
        "language": "de",
        "locationServerActive": "1",
        "coordOutputFormat": "WGS84[DD.ddddd]",
        "itdDate": wanted_date.strftime("%Y%m%d"),
        "itdTime": time_raw.replace(":", ""),
        "itdTripDateTimeDepArr": "arr" if arrive_by else "dep",
        "calcNumberOfTrips": str(max_journeys),
        "calcOneDirection": "1",
        "useRealtime": "1" if realtime else "0",
        "useUT": "1",
        "type_origin": origin["type"],
        "name_origin": origin["name"],
        "nameState_origin": "empty",
        "placeState_origin": "empty",
        "type_destination": destination["type"],
        "name_destination": destination["name"],
        "nameState_destination": "empty",
        "placeState_destination": "empty",
    }

    cache_key = "trip::" + "|".join(
        str(params[key])
        for key in (
            "itdDate",
            "itdTime",
            "itdTripDateTimeDepArr",
            "type_origin",
            "name_origin",
            "type_destination",
            "name_destination",
            "calcNumberOfTrips",
            "useRealtime",
        )
    )
    payload = cache_get(cache_key, CACHE_TTL_JOURNEYS)

    if payload is None:
        payload = efa_get(ENDPOINTS["trip"], params)
        if not payload.get("journeys"):
            # Einmaliger Wiederholungsversuch mit numerischen Stop-IDs, falls die
            # übergebene ID (z. B. eine globale DHID) nicht akzeptiert wurde.
            retry = _retry_with_numeric_stop_ids(params, origin, destination)
            if retry:
                log.info("Wiederhole Verbindungssuche mit numerischen Stop-IDs.")
                payload = efa_get(ENDPOINTS["trip"], retry)
        cache_set(cache_key, payload)

    system = extract_system_messages(payload)
    journeys_raw = payload.get("journeys") or []
    journeys = [
        journey
        for journey in (
            normalize_journey(raw, index) for index, raw in enumerate(journeys_raw)
        )
        if journey
    ]

    notes: list[str] = []
    if origin.get("resolved"):
        notes.append(f"Startort aufgelöst als „{origin['label']}“.")
    if destination.get("resolved"):
        notes.append(f"Ziel aufgelöst als „{destination['label']}“.")

    if system["errors"] and not journeys:
        raise ApiError(
            message=system["errors"][0]["text"]
            or "Es wurde keine Verbindung gefunden.",
            code="no_journeys",
            status=404,
            hint="Tipp: Haltestellen aus der Autovervollständigung wählen oder Uhrzeit/Datum ändern.",
            details=[item["text"] for item in system["errors"][1:]],
        )

    if not journeys:
        raise ApiError(
            message="Für diesen Zeitpunkt wurde keine Verbindung gefunden.",
            code="no_journeys",
            status=404,
            hint=(
                "Bitte Datum/Uhrzeit prüfen oder eine frühere/spätere Abfahrt versuchen. "
                "In der Nacht fahren viele Linien seltener."
            ),
            details=[item["text"] for item in system["errors"]],
        )

    realtime_available = any(journey["realtime"] for journey in journeys)
    if realtime and not realtime_available:
        notes.append(
            "Für diese Verbindungen liegen aktuell keine Echtzeitdaten vor – "
            "angezeigt werden die Fahrplanzeiten."
        )

    return jsonify(
        {
            "ok": True,
            "request": {
                "date": wanted_date.isoformat(),
                "time": time_raw,
                "arrive_by": arrive_by,
                "realtime": realtime,
                "max_journeys": max_journeys,
            },
            "resolved": {
                "origin": {
                    "label": origin["label"],
                    "id": origin["name"],
                    "resolved_from_text": origin.get("resolved", False),
                },
                "destination": {
                    "label": destination["label"],
                    "id": destination["name"],
                    "resolved_from_text": destination.get("resolved", False),
                },
            },
            "count": len(journeys),
            "journeys": journeys,
            "messages": system["messages"],
            "notes": notes,
            "source": {
                "provider": "EFA (Mentz GmbH) – VRR-Fahrplanauskunft",
                "endpoint": f"{EFA_BASE_URL}/{ENDPOINTS['trip']}",
                "realtime_requested": realtime,
                "realtime_available": realtime_available,
                "timezone": "Europe/Berlin",
            },
        }
    )


@app.post("/api/leg-stops")
def api_leg_stops():
    """Haltestellenfolge eines Teilwegs.

    Wird nur genutzt, wenn der Trip-Request die Haltestellenfolge nicht
    mitgeliefert hat. Parameter und Antwortstruktur siehe
    ``XML_STOPSEQCOORD_REQUEST`` in scripts/efa-api-notes.md.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(message="Ungültige Anfrage: JSON-Objekt erwartet.", code="bad_request", status=400)

    line = clean_text(data.get("line_stateless") or data.get("line_id") or data.get("line"))
    stop_id = clean_text(data.get("stop_id") or data.get("stop"))
    trip_code = data.get("trip_code")
    date_raw = clean_text(data.get("date"))
    time_raw = clean_text(data.get("time"))

    if not (line and stop_id and trip_code not in (None, "") and date_raw and time_raw):
        raise ApiError(
            message="Für diese Fahrt liegen keine vollständigen Fahrtdaten vor.",
            code="missing_trip_reference",
            status=400,
            hint="Die Haltestellenfolge kann nur mit Linie, Haltestelle, Fahrtcode, Datum und Uhrzeit abgefragt werden.",
        )

    if DATE_RE.match(date_raw):
        date_value = date_raw.replace("-", "")
    else:
        date_value = re.sub(r"[^0-9]", "", date_raw)[:8]

    time_value = time_raw.replace(":", "")
    if not re.match(r"^\d{3,4}$", time_value):
        raise ApiError(message="Ungültige Uhrzeit für die Fahrtabfrage.", code="bad_request", status=400)
    time_value = time_value.zfill(4)

    params = {
        "outputFormat": "rapidJSON",
        "coordOutputFormat": "WGS84[DD.ddddd]",
        "useRealtime": "1",
        "line": line,
        "stop": stop_id,
        "tripCode": str(trip_code),
        "date": date_value,
        "time": time_value,
    }

    cache_key = f"stopseq::{line}|{stop_id}|{trip_code}|{date_value}|{time_value}"
    payload = cache_get(cache_key, CACHE_TTL_LOCATIONS)
    if payload is None:
        payload = efa_get(ENDPOINTS["stopseq"], params)
        cache_set(cache_key, payload)

    sequence = None
    for key in ("locationSequence", "stopSequence", "stopSeq", "stops", "stopList"):
        candidate = payload.get(key)
        if isinstance(candidate, list) and candidate:
            sequence = candidate
            break

    stops = normalize_stop_sequence(sequence)
    system = extract_system_messages(payload)
    return jsonify(
        {
            "ok": True,
            "available": bool(stops),
            "stops": stops,
            "message": (
                None
                if stops
                else "Die Fahrplan-Schnittstelle liefert für diese Fahrt keine Haltestellenfolge."
            ),
            "messages": system["messages"],
            "source": {
                "endpoint": f"{EFA_BASE_URL}/{ENDPOINTS['stopseq']}",
                "realtime_requested": True,
            },
        }
    )


if __name__ == "__main__":  # pragma: no cover - manueller Start
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "").strip() in {"1", "true", "yes"}
    log.info("NRW Route startet auf http://127.0.0.1:%d (EFA: %s)", port, EFA_BASE_URL)
    app.run(host="0.0.0.0", port=port, debug=debug)
