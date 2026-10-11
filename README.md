# NRW Route 🚌🚆

**Moderne Fahrplanauskunft für Nordrhein-Westfalen** – echte Bus- und
Bahnverbindungen über die öffentlich dokumentierte EFA-Schnittstelle der
VRR-Fahrplanauskunft.

> **Hinweis:** Dieses Projekt ist ein privates Angebot und **kein offizielles
> Angebot des VRR** oder eines anderen Verkehrsverbunds. Angaben ohne Gewähr.

![Stack](https://img.shields.io/badge/Backend-Flask-0b6d51) ![Stack](https://img.shields.io/badge/Frontend-Vanilla%20JS-14a478) ![Daten](https://img.shields.io/badge/Daten-EFA%20%2F%20VRR-0d9488)

---

## Inhalt

| Datei / Ordner | Zweck |
| --- | --- |
| `app.py` | Flask-Backend: Proxy zur EFA-Schnittstelle + Normalisierung der Antworten |
| `index.html` | Frontend (Startseite, Suchkarte, Ergebniskarten, Info-Bereich) |
| `static/css/styles.css` | Design-System (weiß, hellgrüne Verläufe, dunkel-/türkisgrüne Akzente) |
| `static/js/app.js` | Autovervollständigung, Suche, Ergebnis-Rendering |
| `static/favicon.svg` | Logo/Favicon |
| `requirements.txt` | Python-Abhängigkeiten |
| `tests/` | Offline-Tests (Fake-EFA-Server, keine echten Fahrplandaten nötig) |
| `scripts/efa-api-notes.md` | Dokumentation der verwendeten API-Parameter und Quellen |

---

## 1. Installation

Voraussetzung: **Python 3.10 oder neuer** (`python --version` prüfen).

```bash
# 1) In den Projektordner wechseln
cd nrw-route

# 2) Virtuelle Umgebung anlegen (empfohlen)
python -m venv .venv

# 3) Aktivieren
#    Windows (PowerShell):
.venv\Scripts\Activate.ps1
#    Windows (cmd):
.venv\Scripts\activate.bat
#    macOS / Linux:
source .venv/bin/activate

# 4) Abhängigkeiten installieren
python -m pip install -r requirements.txt
```

## 2. Starten

```bash
python app.py
```

Danach im Browser öffnen: **<http://127.0.0.1:5000>**

* Anderer Port: `PORT=8080 python app.py` (Windows cmd: `set PORT=8080 && python app.py`)
* Entwicklungsmodus mit Auto-Reload: `FLASK_DEBUG=1 python app.py`

Das Backend liefert Frontend **und** API aus – es ist kein zweiter Server und
keine CORS-Konfiguration nötig.

## 3. Benutzung

1. **Von** und **Nach** eingeben – während der Eingabe erscheinen echte
   Haltestellen und Bahnhöfe zur Auswahl (bitte einen Vorschlag anklicken,
   damit die Haltestellen-ID eindeutig ist).
2. Optional Start und Ziel mit dem Tausch-Button vertauschen.
3. **Datum** und **Uhrzeit** wählen (Schnellauswahl: *Jetzt*, *+15 Min*,
   *+1 Std*, *Morgen 8:00*).
4. Bei Bedarf **„Ankunftszeit statt Abfahrtszeit verwenden“** aktivieren.
5. **„Verbindungen suchen“** klicken.

Ergebnis: Verbindungskarten mit Abfahrts-/Ankunftszeit, Dauer, Umstiegen,
Linien (S-Bahn, U-Bahn, Tram, Bus, Regionalzug …), Gleis/Steig und – wenn die
Schnittstelle Echtzeitdaten liefert – Verspätungen. Ein Klick auf eine Karte
öffnet die Fahrtdetails mit Haltestellenfolge, Umstiegspunkten und Hinweisen.
Mit **Früher** / **Später** wird ab der ersten bzw. letzten gefundenen
Verbindung weitergesucht.

---

## 4. Verwendete Schnittstelle

Es wird **keine** erfundene API verwendet, sondern die dokumentierte
EFA-Instanz des VRR:

| Zweck | Endpunkt | Beispielparameter |
| --- | --- | --- |
| Haltestellensuche | `XML_STOPFINDER_REQUEST` | `outputFormat=rapidJSON`, `locationServerActive=1`, `type_sf=any`, `anyObjFilter_sf=2`, `name_sf=…` |
| Verbindungssuche | `XML_TRIP_REQUEST2` | `outputFormat=rapidJSON`, `useRealtime=1`, `useUT=1`, `calcNumberOfTrips=n`, `itdDate=YYYYMMDD`, `itdTime=HHMM`, `itdTripDateTimeDepArr=dep\|arr`, `type_origin=stop`, `name_origin=…` |
| Haltestellenfolge (Fallback) | `XML_STOPSEQCOORD_REQUEST` | `line`, `stop`, `tripCode`, `date`, `time` |

Standardbasis: `https://efa.vrr.de/standard` (kein API-Key, keine
Registrierung). Änderbar per Umgebungsvariable, z. B.:

```bash
EFA_BASE_URL=https://app.vrr.de/vrrstd python app.py
```

Die Herleitung der Parameter, die Antwortstruktur und die „Pitfalls“ der
Schnittstelle stehen in [`scripts/efa-api-notes.md`](scripts/efa-api-notes.md).

### Grundsätze der Datenanzeige

* Es werden **ausschließlich** Daten der Schnittstelle angezeigt – keine
  Beispiel-, Mock- oder Schätzverbindungen.
* Zeiten kommen als UTC-Zeitstempel (`…Z`) und werden im Backend nach
  **Europe/Berlin** umgerechnet. Die gewählte Uhrzeit wird als Ortszeit an die
  Schnittstelle gesendet (so wie es die Schnittstelle erwartet).
* Liegt für einen Abschnitt kein Echtzeitwert vor, zeigt die Oberfläche
  ausdrücklich **„Fahrplanzeit“** statt einer erfundenen Verspätung.
* Meldet die Schnittstelle keine Verbindung, wird genau diese Meldung
  ausgegeben (übersetzt und mit Hinweis) – nicht eine leere Liste.

---

## 5. API des Backends

### `GET /api/health`

```json
{ "ok": true, "efa_reachable": true, "efa_base_url": "https://efa.vrr.de/standard",
  "checked_at": "2026-10-11T12:00:00+00:00", "message": "Fahrplan-Schnittstelle erreichbar." }
```

### `GET /api/locations?q=<Text>&limit=8`

```json
{ "ok": true, "query": "Essen", "count": 3,
  "results": [ { "id": "de:05113:9289", "stop_id": "20009289", "name": "Hbf",
                 "full_name": "Essen, Hbf", "place": "Essen", "lat": 51.4516, "lon": 7.0137,
                 "is_best": true, "products": ["regional", "sbahn", "ubahn"] } ] }
```

### `POST /api/journeys`

Anfrage:

```json
{ "origin": { "id": "de:05113:9289", "name": "Hbf", "stop_id": "20009289" },
  "destination": { "id": "de:05913:1702", "name": "Hbf" },
  "date": "2026-10-18", "time": "08:30",
  "arrive_by": false, "realtime": true, "max_journeys": 5 }
```

`origin`/`destination` dürfen auch reiner Text sein
(`"origin": "Essen Hbf"`); der Text wird dann über die Haltestellensuche
aufgelöst und in `resolved` ausgewiesen.

Antwort (gekürzt):

```json
{ "ok": true,
  "request":  { "date": "2026-10-18", "time": "08:30", "arrive_by": false, "realtime": true },
  "resolved": { "origin": { "label": "Essen Hbf", "id": "de:05113:9289", "resolved_from_text": false },
                "destination": { "label": "Dortmund Hbf", "id": "de:05913:1702", "resolved_from_text": false } },
  "count": 1,
  "journeys": [ {
      "departure": { "time": "08:35", "estimated_time": "08:37", "delay_min": 2,
                     "name": "Essen Hbf", "platform": "3", "day_offset": 0 },
      "arrival":   { "time": "09:12", "estimated_time": "09:14", "delay_min": 2,
                     "name": "Dortmund Hbf", "platform": "21", "day_offset": 0 },
      "duration_min": 37, "interchanges": 0, "lines": ["S1"], "realtime": true,
      "legs": [ { "kind": "pt", "mot": "sbahn", "line": "S1", "operator": "DB Regio AG",
                  "direction": "Dortmund Hbf", "duration_min": 37,
                  "origin": { "...": "…" }, "destination": { "...": "…" },
                  "stops": [ { "name": "Bochum Hbf", "arrival_time": "08:52",
                               "departure_time": "08:53", "platform": "5" } ],
                  "infos": [] } ] } ],
  "notes": [], "messages": [],
  "source": { "provider": "EFA (Mentz GmbH) – VRR-Fahrplanauskunft",
              "endpoint": "https://efa.vrr.de/standard/XML_TRIP_REQUEST2",
              "realtime_requested": true, "realtime_available": true,
              "timezone": "Europe/Berlin" } }
```

`mot` ist eines von `walk`, `bus`, `tram`, `ubahn`, `sbahn`, `regional`,
`fern`, `ferry`, `other`.

### `POST /api/leg-stops`

Wird nur genutzt, wenn eine Fahrt ohne Haltestellenfolge geliefert wurde.

```json
{ "line_stateless": "vrr:25001: :H", "stop_id": "20009289",
  "trip_code": 4711, "date": "2026-10-18", "time": "08:35" }
```

### Fehlerformat

Alle API-Fehler haben dieselbe Struktur – passend zum HTTP-Status:

```json
{ "ok": false,
  "error": { "code": "efa_unreachable",
             "message": "Die Fahrplan-Schnittstelle (EFA/VRR) ist nicht erreichbar. …",
             "status": 503, "hint": "Ziel: https://efa.vrr.de/standard/XML_TRIP_REQUEST2" } }
```

Codes: `efa_unreachable` (503), `efa_http_error` / `efa_bad_response` (502),
`stop_not_found` (404), `no_journeys` (404), `missing_input` / `bad_date` /
`date_out_of_range` / `missing_trip_reference` / `bad_request` (400),
`not_found` (404), `internal_error` (500).

---

## 6. Tests

Die Tests laufen vollständig **offline** gegen einen Fake-EFA-Server mit
synthetischen Fixtures (`tests/fixtures/`). Sie prüfen unter anderem die
gesendeten API-Parameter, die Zeitzonen-Umrechnung, Verspätungen, Umstiege,
Haltestellenfolgen, Fehlerpfade und dass Frontend und Backend dieselben
Endpunkte verwenden.

```bash
python -m pip install pytest
python -m pytest tests/ -q
```

Es findet **kein** echter Netzwerkaufruf statt.

---

## 7. Konfiguration (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `PORT` | `5000` | Port des Flask-Servers |
| `EFA_BASE_URL` | `https://efa.vrr.de/standard` | EFA-Instanz |
| `EFA_TIMEOUT` | `15` | Timeout je Anfrage in Sekunden |
| `EFA_USER_AGENT` | `NRW-Route/1.0 …` | User-Agent gegenüber EFA |
| `CACHE_TTL_LOCATIONS` | `300` | Cache für Haltestellensuchen (Sekunden) |
| `CACHE_TTL_JOURNEYS` | `30` | Cache für identische Verbindungssuchen |
| `CACHE_TTL_HEALTH` | `120` | Cache für den Erreichbarkeitscheck |
| `NRW_ROUTE_CORS` | aus | `1` = CORS-Header senden (nur nötig, wenn das Frontend von einer anderen Herkunft geladen wird) |
| `FLASK_DEBUG` | aus | `1` = Debug-/Auto-Reload-Modus |

## 8. Fair Use und Grenzen

* Die Schnittstelle ist öffentlich und kostenlos, aber nicht für Lasttests
  gedacht. Deshalb: Cache, Timeout, genau ein Wiederholungsversuch, keine
  parallelen Anfragen.
* EFA deckt einen begrenzten Fahrplanzeitraum ab. Das Backend akzeptiert
  Datumsangaben von etwa 30 Tagen in der Vergangenheit bis rund ein Jahr in
  der Zukunft und weist sonst mit einer verständlichen Meldung darauf hin.
* Echtzeitdaten sind nur so gut wie die Daten der Verkehrsunternehmen; nicht
  jede Fahrt ist echtzeitgesteuert. Fehlende Echtzeit wird gekennzeichnet,
  nicht ersetzt.
* Fahrplanauskunft ist keine verbindliche Auskunft – maßgeblich sind die
  Aushänge und Apps der Verkehrsunternehmen.

## 9. Später veröffentlichen (optional)

Für einen lokalen Test reicht `python app.py`. Für einen richtigen Service
statt des Entwicklungs-Servers einen WSGI-Server verwenden, z. B. Waitress:

```bash
python -m pip install waitress
python -m waitress --listen=0.0.0.0:8080 app:app
```

* Dahinter gehört ein Reverse Proxy (nginx, Caddy) mit HTTPS.
* `EFA_BASE_URL`, `PORT` und `EFA_TIMEOUT` lassen sich per Umgebung setzen.
* Vor der Veröffentlichung: Nutzungsbedingungen der Datenquelle prüfen und
  eigene Rechtstexte (Impressum, Datenschutzerklärung) ergänzen. Das Backend
  speichert keine Nutzereingaben; es arbeitet nur mit einem kurzen
  In-Memory-Cache und gibt Suchanfragen an EFA weiter.

## 10. Rechtliches

* Fahrplandaten: EFA-Fahrplanauskunft (Mentz GmbH), abgefragt über die
  öffentliche VRR-Schnittstelle `efa.vrr.de`.
* Dieses Projekt steht in keiner Verbindung zum VRR, zu Mentz oder zu
  einzelnen Verkehrsunternehmen; es ist **kein offizielles Angebot**.
* Für die spätere Veröffentlichung als Service: eigene Rechtstexte
  (Impressum, Datenschutzerklärung) ergänzen und die Nutzungsbedingungen der
  Datenquelle prüfen.

## 11. Projektstruktur

```
nrw-route/
├── app.py                     # Flask-Backend (Proxy + Normalisierung)
├── index.html                 # Frontend
├── requirements.txt
├── static/
│   ├── css/styles.css
│   ├── js/app.js
│   └── favicon.svg
├── scripts/
│   └── efa-api-notes.md       # Herleitung der API-Parameter & Quellen
└── tests/
    ├── conftest.py
    ├── fake_efa.py            # Fake-EFA-Server für Tests
    ├── test_app.py
    └── fixtures/              # synthetische EFA-Antworten (keine echten Daten)
```

Viel Spaß beim Planen! 🟢
