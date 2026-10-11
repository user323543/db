/**
 * Frontend-Integrationstest mit jsdom.
 *
 * Lädt die echte index.html + static/js/app.js und prüft das Rendering mit
 * echten Backend-Antworten (Bundle aus run.py). Es wird kein Netzwerk benötigt.
 *
 *   python tests/frontend/run.py
 *
 * Voraussetzung: Node.js und jsdom (npm install in diesem Ordner).
 */

"use strict";

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const bundlePath = process.argv[2] || path.join(__dirname, "bundle.json");
const bundle = JSON.parse(fs.readFileSync(bundlePath, "utf8"));

const REPO = path.resolve(__dirname, "..", "..");
const html = fs.readFileSync(path.join(REPO, "index.html"), "utf8");
const appJs = fs.readFileSync(path.join(REPO, "static", "js", "app.js"), "utf8");

const results = [];
function check(label, condition, extra = "") {
  results.push({ label, condition: Boolean(condition) });
  console.log((condition ? "  OK  " : " FAIL ") + label + (extra ? "  " + extra : ""));
}

const dom = new JSDOM(html, {
  url: "http://127.0.0.1:5000/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const { window } = dom;
const doc = window.document;

const calls = [];
window.fetch = (url, options = {}) => {
  const target = String(url);
  calls.push({ url: target, method: options.method || "GET", body: options.body });

  let payload = { ok: true };
  if (target.startsWith("/api/health")) payload = bundle.health;
  else if (target.startsWith("/api/locations")) {
    payload = /dortmund/i.test(decodeURIComponent(target)) ? bundle.locations_dortmund : bundle.locations;
  } else if (target.startsWith("/api/journeys")) payload = bundle.journeys;
  else if (target.startsWith("/api/leg-stops")) payload = bundle.leg_stops;

  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
};
window.HTMLElement.prototype.scrollIntoView = () => {};
window.scrollTo = () => {};

window.eval(appJs);

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const $ = (selector) => doc.querySelector(selector);

(async () => {
  await wait(50);

  // --- Grundzustand ------------------------------------------------------
  check("Datum vorbelegt", /^\d{4}-\d{2}-\d{2}$/.test($("#dateInput").value), $("#dateInput").value);
  check("Uhrzeit vorbelegt", /^\d{2}:\d{2}$/.test($("#timeInput").value), $("#timeInput").value);
  check("Statusabfrage ausgelöst", calls.some((call) => call.url.startsWith("/api/health")));
  check("Statuspille zeigt Verbindung", $("#statusPill").dataset.state === "ok", $("#statusText").textContent);
  check("Datenquelle wird angezeigt", $("#sourceEndpoint").textContent.trim().length > 0);

  // --- Autovervollständigung: Ziel per Maus ------------------------------
  const destination = $("#destinationInput");
  destination.value = "Dortmund";
  destination.dispatchEvent(new window.Event("input", { bubbles: true }));
  await wait(400);
  const destinationItems = doc.querySelectorAll("#destinationList li[role=option]");
  check("Zielvorschläge werden geladen", destinationItems.length > 0, `${destinationItems.length} Treffer`);
  destinationItems[0].dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true }));
  check("Auswahl per Mausklick", destination.value === "Dortmund Hbf", destination.value);

  // --- Autovervollständigung: Start per Tastatur -------------------------
  const origin = $("#originInput");
  origin.value = "Essen";
  origin.dispatchEvent(new window.Event("input", { bubbles: true }));
  await wait(400);
  const originItems = doc.querySelectorAll("#originList li[role=option]");
  check("Startvorschläge werden geladen", originItems.length >= 3, `${originItems.length} Treffer`);
  check("Treffer zeigt Haltestellennamen", /Hbf/.test(originItems[0].textContent));
  check("Bester Treffer steht oben", /Hbf\b/.test(originItems[0].textContent));
  origin.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  origin.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  check("Auswahl per Pfeiltaste/Enter", origin.value === "Essen Hbf", origin.value);

  // --- Suche -------------------------------------------------------------
  $("#searchForm").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await wait(250);

  const journeyCall = calls.find((call) => call.url.startsWith("/api/journeys"));
  check("Verbindungssuche geht als POST an /api/journeys", Boolean(journeyCall) && journeyCall.method === "POST");
  const sent = journeyCall ? JSON.parse(journeyCall.body) : {};
  check("Aufgelöste Start-Haltestelle wird gesendet", sent.origin && sent.origin.id === "de:05113:9289", JSON.stringify(sent.origin && sent.origin.id));
  check("Datum und Uhrzeit werden mitgesendet", Boolean(sent.date) && /^\d{2}:\d{2}$/.test(sent.time || ""));

  const cards = doc.querySelectorAll(".journey-card");
  check("Verbindungskarten werden gerendert", cards.length === 2, `${cards.length} Karten`);
  const first = cards[0];
  const second = cards[1];

  check("Abfahrtszeit sichtbar", /08:35/.test(first.textContent));
  check("Ankunftszeit sichtbar", /09:12/.test(first.textContent));
  check("Linien-Badge vorhanden", /S1/.test(first.textContent));
  check("Verspätung wird angezeigt", /\+2 Min/.test(first.textContent));
  check("Echtzeit wird gekennzeichnet", /Echtzeit/.test(first.textContent));
  check("Dauer und Umstiegsangabe", /37 Min/.test(first.textContent) && /direkt/.test(first.textContent));
  check("Ohne Echtzeit: Fahrplanzeit-Kennzeichnung", /Fahrplanzeit/.test(second.textContent));
  check("Umstieg wird ausgewiesen", /1 Umstieg/.test(second.textContent));
  check("Fußweg wird ausgewiesen", /Fußweg/.test(second.textContent));

  // --- Details aufklappen -------------------------------------------------
  first.querySelector(".journey-summary").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await wait(80);
  const details = first.querySelector(".journey-details");
  check("Details klappen auf", Boolean(details) && details.hidden === false);
  check("Haltestellen-Schaltfläche vorhanden", Boolean(details.querySelector(".stops-toggle[data-leg]")));

  const toggle = details.querySelector(".stops-toggle[data-leg]");
  if (toggle) {
    toggle.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await wait(80);
    const stops = first.querySelectorAll(".stop-list li");
    check("Haltestellenfolge wird angezeigt", stops.length === 3, `${stops.length} Halte`);
    check("Zwischenhalt enthalten", /Bochum Hbf/.test(first.textContent));
  }

  // --- Fehlerpfad: Schnittstelle nicht erreichbar -------------------------
  window.fetch = () =>
    Promise.resolve({
      ok: false,
      status: 503,
      json: () =>
        Promise.resolve({
          ok: false,
          error: {
            code: "efa_unreachable",
            status: 503,
            message: "Die Fahrplan-Schnittstelle (EFA/VRR) ist nicht erreichbar.",
            hint: "Ziel: https://efa.vrr.de/standard/XML_TRIP_REQUEST2",
          },
        }),
    });
  $("#searchForm").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await wait(250);
  const errorCard = doc.querySelector(".state-card.is-error");
  check("Fehlerzustand wird angezeigt", Boolean(errorCard));
  check("Meldung der Schnittstelle wird durchgereicht", Boolean(errorCard) && /nicht erreichbar/.test(errorCard.textContent));
  check("Hinweis (Ziel-URL) wird angezeigt", Boolean(errorCard) && /efa\.vrr\.de/.test(errorCard.textContent));

  const failed = results.filter((entry) => !entry.condition);
  console.log(
    "\nERGEBNIS: " +
      (failed.length === 0
        ? `ALLE ${results.length} FRONTEND-PRÜFUNGEN BESTANDEN`
        : `${failed.length} von ${results.length} FEHLGESCHLAGEN: ${failed.map((entry) => entry.label).join(", ")}`)
  );
  process.exit(failed.length === 0 ? 0 : 1);
})();
