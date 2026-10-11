# EFA-Schnittstelle (VRR) – Recherchenotizen

Diese Datei dokumentiert **warum** das Backend welche HTTP-Parameter sendet.
Alle Angaben stammen aus öffentlich dokumentierten Quellen und produktiven
Open-Source-Implementierungen – es wurden **keine** Endpunkte oder Parameter erfunden.

## Datenquelle

* EFA-Intermodal-Journey-Planner (Mentz GmbH), JSON-Schnittstelle („rapidJSON“).
* VRR-Instanz: `https://efa.vrr.de/standard` (Endpunkte werden angehängt).
* Kein API-Key, keine Registrierung. Öffentliche, dokumentierte Schnittstelle.
* EFA ist **kein** offizielles Angebot des VRR – Hinweis ist in der UI eingebaut.

Belege:
* Offizielle Parameter-Dokumentation „EFA JSON-Schnittstelle“ (Mentz GmbH):
  <https://mobidata-bw.de/data/Dokumentation-EFA_JSON_Schnittstelle_V2.pdf>
* EFA JSON API Training (Mentz GmbH):
  <https://mobidata-bw.de/daten/portal/efa-json/EFA_JSON_API_Training_EN_2.9.pdf>
* Produktive Referenzimplementierungen:
  * <https://github.com/derf/Travel-Status-DE-VRR> (Perl, XML-Variante)
  * <https://github.com/derf/Travel-Routing-DE-EFA> (Perl)
  * `rhein-ruhr-fahrplan-mcp` (PyPI, VRR-EFA rapidJSON, live getestet)
  * <https://github.com/Klingenstadt-Solingen/Open-SmartCity-App-Server> (`trip.js`)

## Endpunkte

| Endpunkt | Zweck | Verwendete Parameter |
| --- | --- | --- |
| `XML_STOPFINDER_REQUEST` | Haltestellen suchen (Autovervollständigung) | `outputFormat=rapidJSON`, `locationServerActive=1`, `type_sf=any`, `anyObjFilter_sf=2`, `name_sf=<Text>` |
| `XML_TRIP_REQUEST2` | Verbindungssuche | `outputFormat=rapidJSON`, `useRealtime=1`, `useUT=1`, `calcOneDirection=1`, `calcNumberOfTrips`, `itdTripDateTimeDepArr=dep\|arr`, `itdDate=YYYYMMDD`, `itdTime=HHMM`, `type_origin/name_origin`, `type_destination/name_destination`, `nameState_*=empty` |
| `XML_STOPSEQCOORD_REQUEST` | Haltestellenfolge einer Fahrt (Fallback) | `outputFormat=rapidJSON`, `coordOutputFormat=WGS84[DD.ddddd]`, `useRealtime=1`, `line=<stateless Line-ID>`, `stop=<numerische Stop-ID>`, `tripCode=<Schlüssel>`, `date=YYYYMMDD`, `time=HHMM` |

## Antwortstrukturen (rapidJSON)

* Fehler/Meldungen: `systemMessages[]` mit `{code, error|text, type: message|error, module}`.
  Codes sind **nicht** eindeutig → Fehlertexte werden ausgewertet, nicht nur Codes.
* Haltestellensuche: `locations[]` mit `{id, isGlobalId, name, disassembledName, type,
  matchQuality, isBest, coord: [lat, lon], productClasses[], parent{id,name,type},
  properties{stopId, mainLocality}}`.
* Verbindungssuche: `journeys[]` mit `{interchanges, legs[], ...}`.
  * `leg.origin` / `leg.destination`: `{id, name, disassembledName, type, coord, niveau,
    departureTimePlanned, departureTimeEstimated, arrivalTimePlanned, arrivalTimeEstimated,
    properties{stopId, platformName, platform}, parent{...}}`
  * `leg.transportation`: `{id, name, number, disassembledName, product{class,name},
    operator{code,name}, destination{id,name}, properties{tripCode, ...}}`
  * `leg.duration` (Sekunden), `leg.isRealtimeControlled`, `leg.infos[]`, `leg.hints[]`
  * `leg.interchange {desc, type, coords}` (Umstiegspunkt)
  * `leg.stopSequence[]`: Haltestellenliste des Teilwegs mit
    `{name, disassembledName, coord, niveau, isCancelled,
      arrivalTimePlanned, arrivalTimeEstimated, departureTimePlanned, departureTimeEstimated,
      properties{stopId, platformName}}`
  * Fußwege haben **kein** `transportation`-Objekt, aber `duration`.

## Verifizierte Eigenheiten (Pitfalls)

1. `outputFormat=rapidJSON` liefert die Schlüssel `journeys`, `stopEvents`, `locations`
   (nicht `trips`/`departureList`).
2. Alle Zeitstempel kommen mit `Z`-Suffix (UTC) → müssen für die Anzeige nach
   `Europe/Berlin` umgerechnet werden. Die Eingabe `itdDate`/`itdTime` ist dagegen
   Serverzeit (= Ortszeit Europe/Berlin), also **nicht** umrechnen.
3. Bei `itdTripDateTimeDepArr=arr` bleiben die JSON-Feldnamen `departureTime*`.
   Semantik steckt im Parameter, nicht im Feldnamen → Backend liest defensiv beide Paare.
4. Freitext in `name_origin` kann mehrdeutig sein → erst per StopFinder auflösen und
   die Haltestellen-ID senden (`type=stop` für `de:…`-IDs, `type=stopID` für numerische).
5. `calcNumberRequests` greift in rapidJSON nicht zuverlässig → es wird
   `calcNumberOfTrips` verwendet.
6. Für `XML_STOPFINDER_REQUEST` ist `anyObjFilter_sf=2` (nur Haltestellen) nützlich.

## Fair Use

Die Schnittstelle ist öffentlich und schlüssellos. Das Backend betreibt deswegen
* eine 30-Sekunden-Cache für identische Verbindungsanfragen,
* eine 5-Minuten-Cache für Haltestellensuchen,
* genau einen Wiederholungsversuch nur bei Verbindungsfehlern (kein Retry-Sturm),
* einen Timeout von 15 s,
* keine Parallelisierung von Anfragen (Legen-Schleife läuft sequenziell).
