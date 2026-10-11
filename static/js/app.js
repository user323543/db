/* ==========================================================================
   NRW Route – Frontend-Logik
   Ruft ausschließlich die Endpunkte des Flask-Backends auf:
     GET  /api/health
     GET  /api/locations?q=…
     POST /api/journeys
     POST /api/leg-stops
   Alle Zeiten kommen bereits als Europe/Berlin-Zeiten aus dem Backend.
   ========================================================================== */

(() => {
  "use strict";

  /** Optionaler API-Host (leer = gleiche Herkunft, Standard beim Flask-Start). */
  const API_BASE = (window.NRW_ROUTE_API_BASE || "").replace(/\/+$/, "");

  const el = {
    form: document.getElementById("searchForm"),
    origin: document.getElementById("originInput"),
    destination: document.getElementById("destinationInput"),
    originList: document.getElementById("originList"),
    destinationList: document.getElementById("destinationList"),
    swap: document.getElementById("swapBtn"),
    date: document.getElementById("dateInput"),
    time: document.getElementById("timeInput"),
    arriveBy: document.getElementById("arriveByInput"),
    submit: document.getElementById("submitBtn"),
    resultsHead: document.getElementById("resultsHead"),
    resultsTitle: document.getElementById("resultsTitle"),
    resultsSub: document.getElementById("resultsSub"),
    resultsBody: document.getElementById("resultsBody"),
    earlier: document.getElementById("earlierBtn"),
    later: document.getElementById("laterBtn"),
    statusPill: document.getElementById("statusPill"),
    statusText: document.getElementById("statusText"),
    sourceEndpoint: document.getElementById("sourceEndpoint"),
    year: document.getElementById("year"),
  };

  const state = {
    selected: { origin: null, destination: null },
    activeSuggestion: { origin: -1, destination: -1 },
    suggestions: { origin: [], destination: [] },
    lastSearch: null,
    lastData: null,
    lastJourneys: [],
    loading: false,
    searchToken: 0,
  };

  const STORAGE_KEY = "nrwroute:last-search";
  const RECENT_KEY = "nrwroute:recent-stops";

  /* ---------------------------------------------------------------- Helfer */

  const esc = (value) =>
    String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      (char) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])
    );

  const pad = (n) => String(n).padStart(2, "0");

  const localNow = () => new Date();

  const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

  const addMinutes = (date, minutes) => new Date(date.getTime() + minutes * 60000);

  const formatGermanDate = (isoString) => {
    if (!isoString) return "";
    const [y, m, d] = isoString.split("-").map(Number);
    const date = new Date(y, (m || 1) - 1, d || 1);
    return date.toLocaleDateString("de-DE", {
      weekday: "short",
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    });
  };

  const motLabel = (mot) =>
    ({
      sbahn: "S",
      ubahn: "U",
      tram: "T",
      bus: "B",
      regional: "R",
      fern: "F",
      ferry: "⛴",
      walk: "🚶",
      other: "•",
    }[mot] || "•");

  const motName = (mot) =>
    ({
      sbahn: "S-Bahn",
      ubahn: "U-Bahn",
      tram: "Tram",
      bus: "Bus",
      regional: "Regionalzug",
      fern: "Fernverkehr",
      ferry: "Fähre",
      walk: "Fußweg",
      other: "Sonstige",
    }[mot] || "Verkehrsmittel");

  const debounce = (fn, delay) => {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  };

  const iconFor = (mot) => {
    if (mot === "walk") {
      return `<svg viewBox="0 0 24 24"><path d="M13.5 5.5a1.9 1.9 0 1 0 0-3.8 1.9 1.9 0 0 0 0 3.8zM9.8 21l1.3-5.4-2.3-2.2 -1.1 3.9 -2 .6 .6-2 1.3-4.6 3-2.4 2.6 .1 2.1 3.3 2.3 .9 -.7 1.9 -3.2-1.3 -1.6-2.4 -.9 2.7 2.9 2.8 -1.3 6.1z" fill="currentColor"/></svg>`;
    }
    if (mot === "ferry") {
      return `<svg viewBox="0 0 24 24"><path d="M4 15h16l-1.6 4.2a2 2 0 0 1-1.9 1.3H7.5a2 2 0 0 1-1.9-1.3zM11 4h2v3h4v2H7V7h4z" fill="currentColor"/></svg>`;
    }
    return `<svg viewBox="0 0 24 24"><path d="M7 3h10a3 3 0 0 1 3 3v9a3 3 0 0 1-3 3v2.2h-2.2V20H9.2v.2H7V18a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3zm0 2a1 1 0 0 0-1 1v5h12V6a1 1 0 0 0-1-1zm1.2 9.6a1.4 1.4 0 1 0 0 2.8 1.4 1.4 0 0 0 0-2.8zm7.6 0a1.4 1.4 0 1 0 0 2.8 1.4 1.4 0 0 0 0-2.8z" fill="currentColor"/></svg>`;
  };

  /* ------------------------------------------------------------- API-Zugriff */

  class ApiFailure extends Error {
    constructor(error, status) {
      super((error && error.message) || "Unbekannter Fehler");
      this.code = (error && error.code) || "unknown";
      this.hint = (error && error.hint) || "";
      this.details = (error && error.details) || [];
      this.status = status;
    }
  }

  async function apiFetch(path, options = {}) {
    const response = await fetch(API_BASE + path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });

    let payload = null;
    try {
      payload = await response.json();
    } catch (parseError) {
      throw new ApiFailure(
        {
          code: "bad_response",
          message:
            "Das Backend hat keine gültige JSON-Antwort geliefert. Läuft der Flask-Server?",
        },
        response.status
      );
    }

    if (!response.ok || payload.ok === false) {
      throw new ApiFailure(payload.error, response.status);
    }
    return payload;
  }

  /* ----------------------------------------------------- Datum / Zeit-Setup */

  function setDefaults({ keepValues = false } = {}) {
    if (!keepValues || !el.date.value) el.date.value = isoDate(localNow());
    if (!keepValues || !el.time.value) {
      el.time.value = hhmm(addMinutes(localNow(), 5));
    }
  }

  function restoreLastSearch() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return false;
      const saved = JSON.parse(raw);
      if (!saved || !saved.origin || !saved.destination) return false;

      state.selected.origin = saved.origin;
      state.selected.destination = saved.destination;
      el.origin.value = saved.origin.full_name || saved.origin.name || "";
      el.destination.value = saved.destination.full_name || saved.destination.name || "";
      if (saved.date) el.date.value = saved.date;
      if (saved.time) el.time.value = saved.time;
      el.arriveBy.checked = Boolean(saved.arriveBy);
      toggleClearButtons();
      return true;
    } catch (error) {
      return false;
    }
  }

  function persistLastSearch() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          origin: state.selected.origin,
          destination: state.selected.destination,
          date: el.date.value,
          time: el.time.value,
          arriveBy: el.arriveBy.checked,
        })
      );
    } catch (error) {
      /* localStorage kann blockiert sein – kein Grund abzubrechen. */
    }
  }

  function recentStops() {
    try {
      const raw = localStorage.getItem(RECENT_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
      return [];
    }
  }

  function rememberStop(location) {
    if (!location || !location.name) return;
    try {
      const list = recentStops().filter((item) => item.id !== location.id);
      list.unshift(location);
      localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 6)));
    } catch (error) {
      /* ignorieren */
    }
  }

  /* -------------------------------------------------------- Autovervollständigung */

  function suggestionLabel(location) {
    const place = location.place ? `${location.place}, ` : "";
    return `${place}${location.name}`;
  }

  function setFieldValue(kind, location) {
    const input = kind === "origin" ? el.origin : el.destination;
    state.selected[kind] = location;
    input.value = location ? suggestionLabel(location) : "";
    state.suggestions[kind] = [];
    state.activeSuggestion[kind] = -1;
    closeSuggestions(kind);
    toggleClearButtons();
  }

  function toggleClearButtons() {
    document.querySelectorAll("[data-clear]").forEach((button) => {
      const kind = button.dataset.clear;
      const input = kind === "origin" ? el.origin : el.destination;
      button.hidden = !input.value;
    });
  }

  function closeSuggestions(kind) {
    const list = kind === "origin" ? el.originList : el.destinationList;
    const input = kind === "origin" ? el.origin : el.destination;
    list.hidden = true;
    list.innerHTML = "";
    input.setAttribute("aria-expanded", "false");
  }

  function renderSuggestions(kind, items, term, message) {
    const list = kind === "origin" ? el.originList : el.destinationList;
    const input = kind === "origin" ? el.origin : el.destination;

    if (!items.length) {
      list.innerHTML = `<li class="sug-empty" role="presentation">${esc(
        message || "Keine Haltestelle gefunden."
      )}</li>`;
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      state.suggestions[kind] = [];
      return;
    }

    const needle = (term || "").toLowerCase();
    list.innerHTML = items
      .map((item, index) => {
        const label = suggestionLabel(item);
        const haystack = label.toLowerCase();
        const at = needle ? haystack.indexOf(needle) : -1;
        const highlighted =
          at >= 0
            ? `${esc(label.slice(0, at))}<mark>${esc(label.slice(at, at + needle.length))}</mark>${esc(
                label.slice(at + needle.length)
              )}`
            : esc(label);
        const products = (item.products || []).map(motLabel).join(" ");
        return `<li role="option" id="${kind}-sug-${index}" data-index="${index}" aria-selected="false">
            <span class="sug-icon" aria-hidden="true">${esc(products ? products.slice(0, 3) : "H")}</span>
            <span class="sug-text">
              <strong>${highlighted}</strong>
              <span>${esc(
                item.full_name && item.full_name !== label ? item.full_name : item.place || ""
              )}${
                item.products && item.products.length
                  ? ` · ${esc(item.products.map(motName).slice(0, 3).join(", "))}`
                  : ""
              }</span>
            </span>
          </li>`;
      })
      .join("");

    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    state.suggestions[kind] = items;
    state.activeSuggestion[kind] = -1;

    list.querySelectorAll("li[role=option]").forEach((node) => {
      node.addEventListener("mousedown", (event) => {
        event.preventDefault();
        pickSuggestion(kind, Number(node.dataset.index));
      });
    });
  }

  function pickSuggestion(kind, index) {
    const item = state.suggestions[kind][index];
    if (!item) return;
    setFieldValue(kind, item);
    rememberStop(item);
  }

  function moveSuggestion(kind, delta) {
    const items = state.suggestions[kind];
    if (!items.length) return;
    const list = kind === "origin" ? el.originList : el.destinationList;
    const current = state.activeSuggestion[kind];
    const next = (current + delta + items.length) % items.length;
    state.activeSuggestion[kind] = next;
    list.querySelectorAll("li[role=option]").forEach((node, index) => {
      node.setAttribute("aria-selected", index === next ? "true" : "false");
      if (index === next) node.scrollIntoView({ block: "nearest" });
    });
  }

  const requestSuggestions = debounce(async (kind, term) => {
    const query = term.trim();
    if (query.length < 2) {
      closeSuggestions(kind);
      return;
    }
    try {
      const data = await apiFetch(`/api/locations?q=${encodeURIComponent(query)}&limit=8`);
      renderSuggestions(kind, data.results || [], query);
    } catch (error) {
      renderSuggestions(kind, [], query, `Suche nicht möglich: ${error.message}`);
    }
  }, 250);

  function bindAutocomplete() {
    [
      ["origin", el.origin],
      ["destination", el.destination],
    ].forEach(([kind, input]) => {
      input.addEventListener("input", () => {
        state.selected[kind] = null;
        toggleClearButtons();
        const value = input.value.trim();
        if (value.length < 2) {
          // Bei leerem Feld: zuletzt genutzte Haltestellen anbieten.
          const recents = recentStops();
          if (!value && recents.length) {
            renderSuggestions(kind, recents.slice(0, 5), "", "Zuletzt verwendet");
          } else {
            closeSuggestions(kind);
          }
          return;
        }
        requestSuggestions(kind, value);
      });

      input.addEventListener("focus", () => {
        const value = input.value.trim();
        const recents = recentStops();
        if (!value && recents.length) renderSuggestions(kind, recents.slice(0, 5), "", "Zuletzt verwendet");
      });

      input.addEventListener("keydown", (event) => {
        if (event.key === "ArrowDown") {
          event.preventDefault();
          moveSuggestion(kind, 1);
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          moveSuggestion(kind, -1);
        } else if (event.key === "Enter") {
          if (state.activeSuggestion[kind] >= 0) {
            event.preventDefault();
            pickSuggestion(kind, state.activeSuggestion[kind]);
          } else if (!el.originList.hidden || !el.destinationList.hidden) {
            closeSuggestions(kind);
          }
        } else if (event.key === "Escape") {
          closeSuggestions(kind);
        }
      });

      input.addEventListener("blur", () => {
        window.setTimeout(() => closeSuggestions(kind), 120);
      });
    });

    document.addEventListener("click", (event) => {
      if (!event.target.closest(".field")) {
        closeSuggestions("origin");
        closeSuggestions("destination");
      }
    });

    document.querySelectorAll("[data-clear]").forEach((button) => {
      button.addEventListener("click", () => {
        const kind = button.dataset.clear;
        setFieldValue(kind, null);
        (kind === "origin" ? el.origin : el.destination).focus();
      });
    });
  }

  /* --------------------------------------------------------------- Suche */

  function buildPayload(overrides = {}) {
    const payload = {
      date: el.date.value || isoDate(localNow()),
      time: el.time.value || hhmm(localNow()),
      arrive_by: el.arriveBy.checked,
      realtime: true,
      max_journeys: 5,
      origin: state.selected.origin || el.origin.value.trim(),
      destination: state.selected.destination || el.destination.value.trim(),
    };
    return { ...payload, ...overrides };
  }

  function markInvalid(kind, invalid) {
    const field = document.querySelector(`.field[data-field="${kind}"]`);
    if (!field) return;
    field.classList.toggle("is-invalid", invalid);
    const input = kind === "origin" ? el.origin : el.destination;
    input.setAttribute("aria-invalid", invalid ? "true" : "false");
  }

  function setLoading(isLoading) {
    state.loading = isLoading;
    el.submit.classList.toggle("loading", isLoading);
    el.submit.disabled = isLoading;
    el.earlier.disabled = isLoading;
    el.later.disabled = isLoading;
  }

  function renderSkeletons() {
    el.resultsHead.hidden = false;
    el.resultsTitle.textContent = "Verbindungen werden gesucht …";
    el.resultsSub.textContent = "Die Fahrplanauskunft wird abgefragt.";
    el.resultsBody.innerHTML = `
      <div class="journey-list">
        ${Array.from({ length: 3 })
          .map(
            () => `<div class="skeleton-card">
              <div class="sk-line w-30"></div>
              <div class="sk-line w-70"></div>
              <div class="sk-line w-50"></div>
            </div>`
          )
          .join("")}
      </div>`;
  }

  function renderError(error) {
    const details = (error.details || []).filter(Boolean);
    el.resultsHead.hidden = false;
    el.resultsTitle.textContent = "Keine Verbindung angezeigt";
    el.resultsSub.textContent = "";
    el.resultsBody.innerHTML = `
      <div class="state-card is-error">
        <h3>${esc(error.message)}</h3>
        ${error.hint ? `<p class="state-hint">${esc(error.hint)}</p>` : ""}
        ${
          details.length
            ? `<p class="state-hint">Meldung der Schnittstelle: ${esc(details.join(" · "))}</p>`
            : ""
        }
        <div class="state-actions">
          <button type="button" class="ghost-btn" id="retryBtn">Erneut versuchen</button>
        </div>
      </div>`;
    const retry = document.getElementById("retryBtn");
    if (retry) retry.addEventListener("click", () => submitSearch());
  }

  function lineBadge(leg) {
    if (leg.kind === "footway") {
      return `<span class="line-badge is-walk" data-mot="walk"><span class="badge-icon">🚶</span>Fußweg</span>`;
    }
    const line = leg.line || leg.product || motName(leg.mot);
    return `<span class="line-badge" data-mot="${esc(leg.mot)}"><span class="badge-icon">${esc(
      motLabel(leg.mot)
    )}</span>${esc(line)}</span>`;
  }

  function summaryMarkup(journey, index) {
    const departure = journey.departure || {};
    const arrival = journey.arrival || {};

    const depDelay =
      departure.delay_min > 0 ? `<span class="tag tag-delay">+${departure.delay_min} Min</span>` : "";
    const arrDelay =
      arrival.delay_min > 0 && !departure.delay_min
        ? `<span class="tag tag-delay">+${arrival.delay_min} Min</span>`
        : "";
    const realtimeTag = journey.realtime
      ? `<span class="tag tag-live"><span class="dot"></span>Echtzeit</span>`
      : `<span class="tag tag-planned">Fahrplanzeit</span>`;

    const interchanges =
      journey.interchanges > 0
        ? `${journey.interchanges} Umstieg${journey.interchanges > 1 ? "e" : ""}`
        : "direkt";
    const walkMinutes = (journey.legs || [])
      .filter((leg) => leg.kind === "footway")
      .reduce((sum, leg) => sum + (leg.duration_min || 0), 0);

    return `<button type="button" class="journey-summary" aria-expanded="false" aria-controls="journey-details-${index}">
        <span class="jc-time">
          <span class="jc-time-main">${esc(departure.time || "–")}${depDelay}</span>
          <span class="jc-time-sub">ab ${esc(departure.name || "")}</span>
        </span>

        <span class="jc-mid">
          <span class="jc-badges">${(journey.legs || []).map(lineBadge).join("")}${realtimeTag}</span>
          <span class="jc-route">
            <span class="jc-route-track"></span>
            <span class="jc-meta">
              <span><b>${esc(journey.duration_min ?? "–")} Min</b></span>
              <span>${esc(interchanges)}</span>
              ${walkMinutes ? `<span>Fußweg ${walkMinutes} Min</span>` : ""}
              ${(() => {
                const count = (journey.legs || []).length;
                return `<span>${count} Abschnitt${count === 1 ? "" : "e"}</span>`;
              })()}
            </span>
          </span>
        </span>

        <span class="jc-time arrival">
          <span class="jc-time-main">${esc(arrival.time || "–")}${arrDelay}</span>
          <span class="jc-time-sub">an ${esc(arrival.name || "")}${
            arrival.day_offset > 0
              ? ` (+${arrival.day_offset} Tag${arrival.day_offset > 1 ? "e" : ""})`
              : ""
          }</span>
        </span>
        <svg class="jc-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>`;
  }

  function endpointMarkup(point, label) {
    if (!point) return "";
    const delayed = point.delay_min > 0;
    const estimated = point.estimated_time
      ? `<span class="meta">${esc(point.estimated_time)}${
          delayed ? ` (+${point.delay_min})` : ""
        }</span>`
      : "";
    return `<div class="endpoint">
        <span class="time${delayed ? " is-delayed" : ""}">${esc(point.time || "–")}</span>
        <span class="place">${esc(label)}: <strong>${esc(point.full_name || point.name || "")}</strong>${
          point.platform ? ` · Gleis/Steig ${esc(point.platform)}` : ""
        }</span>
        ${estimated}
      </div>`;
  }

  function stopsListMarkup(stops) {
    return `<ul class="stop-list">${stops
      .map((stop) => {
        const time = stop.departure_time || stop.arrival_time || "–";
        const delayed = stop.departure_delay_min > 0 || stop.arrival_delay_min > 0;
        const delayValue = stop.departure_delay_min || stop.arrival_delay_min || 0;
        const estimated = stop.departure_estimated_time || stop.arrival_estimated_time;
        const meta = [
          stop.platform ? `Steig ${stop.platform}` : "",
          estimated && delayValue > 0 ? `${estimated} (+${delayValue})` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        return `<li${stop.cancelled ? ' class="is-cancelled"' : ""}>
            <span class="s-time${delayed ? " is-delayed" : ""}">${esc(time)}</span>
            <span class="s-name">${esc(stop.full_name || stop.name)}${
              stop.cancelled ? " · fällt aus" : ""
            }</span>
            <span class="s-meta">${esc(meta)}</span>
          </li>`;
      })
      .join("")}</ul>`;
  }

  function legMarkup(leg, journeyIndex, legIndex) {
    const isWalk = leg.kind === "footway";
    const heading = isWalk
      ? `Fußweg · ${esc(leg.duration_min ?? "?")} Min`
      : `${esc(leg.line || motName(leg.mot))}${leg.direction ? ` nach ${esc(leg.direction)}` : ""}`;
    const sub = isWalk
      ? ""
      : `${esc(motName(leg.mot))}${leg.operator ? ` · ${esc(leg.operator)}` : ""}${
          leg.duration_min ? ` · ${esc(leg.duration_min)} Min` : ""
        }`;

    const infos = (leg.infos || [])
      .slice(0, 3)
      .map(
        (info) =>
          `<div class="leg-warning"><strong>${esc(info.title || "Hinweis")}</strong>${esc(
            info.text || ""
          )}</div>`
      )
      .join("");

    const stopsAvailable = Array.isArray(leg.stops) && leg.stops.length > 0;
    const stopsBlock = stopsAvailable
      ? `<button type="button" class="stops-toggle" data-leg="${journeyIndex}-${legIndex}">
           ${leg.stops.length} Haltestellen dieser Fahrt anzeigen
         </button>
         <div class="stops-wrap" data-leg-wrap="${journeyIndex}-${legIndex}" hidden>
           ${stopsListMarkup(leg.stops)}
         </div>`
      : isWalk
      ? ""
      : `<button type="button" class="stops-toggle" data-fetch-leg="${journeyIndex}-${legIndex}">
           Zwischenhalte laden
         </button>
         <p class="leg-note" data-leg-note="${journeyIndex}-${legIndex}">
           Die Fahrplan-Schnittstelle hat für diesen Abschnitt keine Haltestellenfolge mitgeliefert.
         </p>`;


    return `<div class="leg" data-mot="${esc(leg.mot)}">
        <div class="leg-rail">
          <span class="rail-icon" aria-hidden="true">${esc(motLabel(leg.mot))}</span>
          <span class="rail-line"></span>
        </div>
        <div class="leg-body">
          <div class="leg-head">
            <h4>${heading}</h4>
            ${sub ? `<span class="leg-sub">${sub}</span>` : ""}
          </div>
          <div class="leg-endpoints">
            ${endpointMarkup(leg.origin, "ab")}
            ${endpointMarkup(leg.destination, "an")}
          </div>
          ${infos}
          ${stopsBlock}
        </div>
      </div>`;
  }

  function detailsMarkup(journey, index) {
    const notes = [];
    if (!journey.realtime) {
      notes.push(
        "Für diese Verbindung liegen keine Echtzeitdaten vor – angezeigt werden die Fahrplanzeiten."
      );
    }
    (journey.infos || []).slice(0, 3).forEach((info) => {
      if (info.text) notes.push(info.text);
    });

    return `
      ${
        notes.length
          ? `<div class="notes-row">${notes
              .map((note) => `<span class="note-pill">${esc(note)}</span>`)
              .join("")}</div>`
          : ""
      }
      ${(journey.legs || []).map((leg, legIndex) => legMarkup(leg, index, legIndex)).join("")}`;
  }

  function renderJourneys(data) {
    const { journeys } = data;

    el.resultsHead.hidden = false;
    el.resultsTitle.textContent = `${journeys.length} Verbindung${
      journeys.length === 1 ? "" : "en"
    } gefunden`;
    el.resultsSub.innerHTML = `${esc(data.resolved.origin.label)} <span aria-hidden="true">→</span> ${esc(
      data.resolved.destination.label
    )} · ${esc(formatGermanDate(data.request.date))} · ${
      data.request.arrive_by ? "Ankunft" : "Abfahrt"
    } ab ${esc(data.request.time)} Uhr`;

    const notesMarkup = (data.notes || []).length
      ? `<div class="notes-row">${data.notes
          .map((note) => `<span class="note-pill">${esc(note)}</span>`)
          .join("")}</div>`
      : "";

    el.resultsBody.innerHTML = `${notesMarkup}
      <div class="journey-list">
        ${journeys.map((journey, index) => summaryMarkup(journey, index)).join("")}
      </div>`;

    state.lastJourneys = journeys;

    el.resultsBody.querySelectorAll(".journey-summary").forEach((button, index) => {
      const card = button.closest(".journey-card");
      button.addEventListener("click", () => {
        const open = card.classList.toggle("is-open");
        button.setAttribute("aria-expanded", open ? "true" : "false");
        let details = card.querySelector(".journey-details");
        if (!details) {
          details = document.createElement("div");
          details.className = "journey-details";
          details.id = `journey-details-${index}`;
          details.innerHTML = detailsMarkup(journeys[index], index);
          card.appendChild(details);
          bindDetails(card, index);
        }
        details.hidden = !open;
      });
    });

    el.resultsBody.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function bindDetails(card, index) {
    card.querySelectorAll(".stops-toggle[data-leg]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.leg;
        const wrap = card.querySelector(`[data-leg-wrap="${key}"]`);
        if (!wrap) return;
        const willShow = wrap.hidden;
        wrap.hidden = !willShow;
        button.textContent = willShow
          ? "Haltestellen ausblenden"
          : button.textContent.replace("ausblenden", "dieser Fahrt anzeigen");
      });
    });

    card.querySelectorAll(".stops-toggle[data-fetch-leg]").forEach((button) => {
      button.addEventListener("click", async () => {
        const [journeyIndex, legIndex] = button.dataset.fetchLeg.split("-").map(Number);
        const leg = (state.lastJourneys[journeyIndex] || {}).legs?.[legIndex];
        if (!leg) return;
        const note = card.querySelector(`[data-leg-note="${journeyIndex}-${legIndex}"]`);

        button.disabled = true;
        button.textContent = "Lade Zwischenhalte …";
        try {
          const payload = {
            line_stateless: leg.line_stateless || null,
            line_id: leg.line_id || null,
            line: leg.line || null,
            stop_id: leg.origin?.stop_id || leg.origin?.id || null,
            trip_code: leg.trip_code ?? null,
            date: (state.lastData && state.lastData.request && state.lastData.request.date) || el.date.value,
            time: (leg.origin?.planned || "").slice(11, 16) || el.time.value,
          };
          const response = await apiFetch("/api/leg-stops", {
            method: "POST",
            body: JSON.stringify(payload),
          });

          if (response.available && response.stops.length) {
            const wrap = document.createElement("div");
            wrap.className = "stops-wrap";
            wrap.innerHTML = stopsListMarkup(response.stops);
            button.replaceWith(wrap);
            if (note) note.remove();
          } else {
            button.remove();
            if (note) {
              note.textContent =
                "Die Fahrplan-Schnittstelle liefert für diese Fahrt keine Haltestellenfolge.";
            }
          }
        } catch (error) {
          button.disabled = false;
          button.textContent = "Zwischenhalte laden";
          if (note) note.textContent = `Zwischenhalte konnten nicht geladen werden: ${error.message}`;
        }
      });
    });

    void index;
  }

  async function submitSearch(overrides = {}) {
    const originText = el.origin.value.trim();
    const destinationText = el.destination.value.trim();

    markInvalid("origin", !originText);
    markInvalid("destination", !destinationText);
    if (!originText || !destinationText) {
      renderError({
        message: "Bitte Start und Ziel angeben.",
        hint: "Tipp: Nutze die Vorschlagsliste, damit die Haltestelle eindeutig ist.",
      });
      return;
    }

    const payload = buildPayload(overrides);
    const token = ++state.searchToken;

    setLoading(true);
    renderSkeletons();

    try {
      const data = await apiFetch("/api/journeys", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (token !== state.searchToken) return;
      state.lastSearch = data;
      state.lastData = data;
      renderJourneys(data);
      persistLastSearch();
    } catch (error) {
      if (token !== state.searchToken) return;
      if (error instanceof ApiFailure) {
        renderError(error);
      } else {
        renderError({
          message:
            "Die Verbindungssuche konnte nicht ausgeführt werden. Läuft das Backend (app.py)?",
          hint: String(error && error.message ? error.message : error),
        });
      }
    } finally {
      if (token === state.searchToken) setLoading(false);
    }
  }

  function shiftSearch(minutes) {
    const base = el.time.value || hhmm(localNow());
    const [hours, mins] = base.split(":").map(Number);
    const reference = new Date();
    reference.setHours(hours || 0, mins || 0, 0, 0);
    const shifted = addMinutes(reference, minutes);
    el.time.value = hhmm(shifted);
    el.date.value = isoDate(shifted);
    submitSearch();
  }

  function bindSearch() {
    el.form.addEventListener("submit", (event) => {
      event.preventDefault();
      submitSearch();
    });

    el.swap.addEventListener("click", () => {
      const originValue = el.origin.value;
      const destinationValue = el.destination.value;
      const originSelected = state.selected.origin;
      const destinationSelected = state.selected.destination;

      el.origin.value = destinationValue;
      el.destination.value = originValue;
      state.selected.origin = destinationSelected;
      state.selected.destination = originSelected;

      el.swap.classList.add("spin");
      window.setTimeout(() => el.swap.classList.remove("spin"), 450);
      toggleClearButtons();
    });

    document.querySelectorAll(".chip[data-quick]").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.querySelectorAll(".chip").forEach((other) => other.classList.remove("is-active"));
        chip.classList.add("is-active");
        const mode = chip.dataset.quick;
        const now = localNow();
        if (mode === "now") {
          el.date.value = isoDate(now);
          el.time.value = hhmm(addMinutes(now, 1));
        } else if (mode === "tomorrow") {
          const tomorrow = addMinutes(now, 24 * 60);
          el.date.value = isoDate(tomorrow);
          el.time.value = "08:00";
        } else {
          el.date.value = isoDate(now);
          el.time.value = hhmm(addMinutes(now, Number(mode)));
        }
      });
    });

    el.earlier.addEventListener("click", () => {
      if (!state.lastJourneys.length) return;
      el.arriveBy.checked = false;
      const first = state.lastJourneys[0];
      const [h, m] = (first.departure.time || "00:00").split(":").map(Number);
      const reference = el.date.value;
      const target = new Date();
      const [y, mo, d] = reference.split("-").map(Number);
      target.setFullYear(y, (mo || 1) - 1, d || 1);
      target.setHours(h || 0, m || 0, 0, 0);
      const earlier = addMinutes(target, -1);
      el.date.value = isoDate(earlier);
      el.time.value = hhmm(earlier);
      submitSearch({ arrive_by: false });
    });

    el.later.addEventListener("click", () => {
      if (!state.lastJourneys.length) return;
      el.arriveBy.checked = false;
      const last = state.lastJourneys[state.lastJourneys.length - 1];
      const [h, m] = (last.departure.time || "00:00").split(":").map(Number);
      const [y, mo, d] = el.date.value.split("-").map(Number);
      const target = new Date();
      target.setFullYear(y, (mo || 1) - 1, d || 1);
      target.setHours(h || 0, m || 0, 0, 0);
      const later = addMinutes(target, 1);
      el.date.value = isoDate(later);
      el.time.value = hhmm(later);
      submitSearch({ arrive_by: false });
    });
  }

  /* ---------------------------------------------------------------- Status */

  async function checkHealth() {
    try {
      const data = await apiFetch("/api/health");
      if (data.efa_reachable) {
        el.statusPill.dataset.state = "ok";
        el.statusText.textContent = "Fahrplan-Schnittstelle verbunden";
      } else {
        el.statusPill.dataset.state = "error";
        el.statusText.textContent = "Schnittstelle derzeit nicht erreichbar";
      }
      if (el.sourceEndpoint && data.efa_base_url) {
        el.sourceEndpoint.textContent = data.efa_base_url.replace(/^https?:\/\//, "");
      }
    } catch (error) {
      el.statusPill.dataset.state = "error";
      el.statusText.textContent = "Backend nicht erreichbar";
    }
  }

  /* ----------------------------------------------------------------- Start */

  function init() {
    setDefaults({ keepValues: restoreLastSearch() });
    bindAutocomplete();
    bindSearch();
    toggleClearButtons();
    if (el.year) el.year.textContent = String(new Date().getFullYear());

    if (location.protocol === "file:") {
      renderError({
        message: "Diese Seite wurde direkt aus dem Dateisystem geöffnet.",
        hint:
          "Bitte den Flask-Server starten (python app.py) und die Seite unter http://127.0.0.1:5000 öffnen.",
      });
      el.statusPill.dataset.state = "error";
      el.statusText.textContent = "Backend nicht verbunden";
      return;
    }

    checkHealth();
    window.setInterval(checkHealth, 300000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
