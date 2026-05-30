/* ============================================================================
   app.js — AI Flight Disruption War Room (frontend)
   Consumes the BACKEND_DESIGN §2 contract. Runs against the live backend, or a
   fully self-contained in-browser simulation when ?mock=1 (or auto-fallback).
   ========================================================================== */
(function () {
  "use strict";

  // ---------------------------------------------------------------------
  //  API client — picks mock or HTTP. Auto-falls back to mock if the
  //  backend is unreachable, so the demo never hard-fails.
  // ---------------------------------------------------------------------
  const params = new URLSearchParams(location.search);
  const FORCE_MOCK = params.get("mock") === "1";

  class HttpBackend {
    constructor() { this.isMock = false; this.base = "/api"; }
    async _get(path) {
      const r = await fetch(this.base + path);
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
      return r.json();
    }
    async _post(path, body) {
      const r = await fetch(this.base + path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
      return r.json();
    }
    health() { return this._get("/health"); }
    scenarios() { return this._get("/scenarios"); }
    load(snapshotId) { return this._post("/scenario/load", { snapshot_id: snapshotId }); }
    sectorsGeo(id) { return this._get(`/scenario/${id}/sectors.geojson`); }
    frame(id, t, band) { return this._get(`/scenario/${id}/frame?t=${encodeURIComponent(t)}&band=${band}`); }
    timeline(id, band) { return this._get(`/scenario/${id}/timeline?band=${band}`); }
    disrupt(id, body, band) { return this._post(`/scenario/${id}/disrupt?band=${band}`, body); }
    suggest(id) { return this._post(`/scenario/${id}/copilot/suggest`, {}); }
    apply(id, mitId, band) { return this._post(`/scenario/${id}/copilot/apply?band=${band}`, { mitigation_id: mitId }); }
    reset(id, body, band) { return this._post(`/scenario/${id}/reset?band=${band}`, body); }
  }

  async function makeApi() {
    if (FORCE_MOCK) return new window.MockBackend();
    const http = new HttpBackend();
    try { await http.health(); return http; }
    catch { console.warn("[war-room] backend unreachable — using mock"); return new window.MockBackend(); }
  }

  // ---------------------------------------------------------------------
  //  State
  // ---------------------------------------------------------------------
  const S = {
    api: null,
    scenarioId: null,
    tStart: 0, tEnd: 0, stepMin: 5,
    band: "HIGH",
    currentT: 0,
    playing: false,
    playTimer: null,
    sectorsGeo: null,
    sectorLayer: null,
    sectorLayersByName: {},
    flightMarkers: {},     // id -> L.circleMarker
    stormLayer: null,
    timeline: null,
    suggestion: null,
    hasDisruption: false,
    map: null,
    frameReqSeq: 0,
  };

  const STATUS_COLORS = { ok: "#38bdf8", weather: "#fb923c", closed: "#ef4444" };

  // ---------------------------------------------------------------------
  //  DOM helpers
  // ---------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const fmtClock = (ms) => new Date(ms).toISOString().slice(11, 16) + "Z";

  function toast(msg, isErr) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast" + (isErr ? " err" : "");
    setTimeout(() => el.classList.add("hidden"), 2600);
  }

  // ---------------------------------------------------------------------
  //  Map
  // ---------------------------------------------------------------------
  function initMap() {
    const map = L.map("map", {
      center: [39, -96], zoom: 4, minZoom: 3, maxZoom: 7,
      zoomControl: true, attributionControl: true, preferCanvas: true,
      worldCopyJump: false,
    });
    L.tileLayer(
      "https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png",
      { attribution: "© OpenStreetMap, © CARTO", subdomains: "abcd", maxZoom: 19 }
    ).addTo(map);
    S.map = map;
    S.flightRenderer = L.canvas({ padding: 0.5 });
  }

  function sectorStyle(props, dyn) {
    // dyn: { ratio, closed } or undefined
    if (dyn && dyn.closed)
      return { color: "#ef4444", weight: 1.4, fillColor: "#7a1320", fillOpacity: 0.35, dashArray: "4 3" };
    const ratio = dyn ? dyn.ratio : 0;
    if (ratio > 1.0)  return { color: "#ef4444", weight: 1.2, fillColor: "#ef4444", fillOpacity: 0.34 };
    if (ratio >= 0.7) return { color: "#f5c542", weight: 0.8, fillColor: "#f5c542", fillOpacity: 0.20 };
    if (ratio > 0)    return { color: "#2a6f97", weight: 0.5, fillColor: "#2a6f97", fillOpacity: 0.12 };
    return { color: "#243653", weight: 0.4, fillColor: "#16314d", fillOpacity: 0.04 };
  }

  function drawSectors() {
    if (S.sectorLayer) { S.map.removeLayer(S.sectorLayer); }
    S.sectorLayersByName = {};
    S.sectorLayer = L.geoJSON(S.sectorsGeo, {
      filter: (f) => f.properties.band === S.band,
      style: (f) => sectorStyle(f.properties),
      onEachFeature: (f, layer) => {
        S.sectorLayersByName[f.properties.name] = layer;
        layer.bindTooltip(f.properties.name, { sticky: true, className: "sec-tip" });
      },
    }).addTo(S.map);
    S.sectorLayer.bringToBack();
  }

  function restyleSectors(frame) {
    const byName = {};
    for (const s of frame.sectors) byName[s.name] = s;
    for (const [name, layer] of Object.entries(S.sectorLayersByName)) {
      layer.setStyle(sectorStyle(layer.feature.properties, byName[name]));
    }
  }

  function renderFlights(frame) {
    const seen = new Set();
    for (const fl of frame.flights) {
      seen.add(fl.id);
      let m = S.flightMarkers[fl.id];
      const color = STATUS_COLORS[fl.status] || "#38bdf8";
      if (!m) {
        m = L.circleMarker([fl.lat, fl.lon], {
          radius: fl.status === "ok" ? 3 : 4, color, fillColor: color, fillOpacity: 0.9,
          weight: 1, renderer: S.flightRenderer,
        });
        m.on("click", () => showFlightPopup(fl, m));
        m.addTo(S.map);
        S.flightMarkers[fl.id] = m;
      } else {
        m.setLatLng([fl.lat, fl.lon]);
        m.setStyle({ color, fillColor: color, radius: fl.status === "ok" ? 3 : 4 });
      }
      m._fl = fl;
    }
    // remove markers no longer present
    for (const id of Object.keys(S.flightMarkers)) {
      if (!seen.has(id)) { S.map.removeLayer(S.flightMarkers[id]); delete S.flightMarkers[id]; }
    }
  }

  function showFlightPopup(fl, marker) {
    marker.bindPopup(
      `<b>${fl.fn}</b> · ${fl.band}<br>alt ${fl.alt_ft.toLocaleString()} ft<br>` +
      `status: <b style="color:${STATUS_COLORS[fl.status]}">${fl.status}</b>`
    ).openPopup();
  }

  function drawStorm(disruption) {
    if (S.stormLayer) { S.map.removeLayer(S.stormLayer); S.stormLayer = null; }
    if (!disruption || disruption.kind !== "STORM" || !disruption.storm) return;
    const s = disruption.storm;
    S.stormLayer = L.rectangle([[s.latMin, s.lonMin], [s.latMax, s.lonMax]], {
      color: "#a855f7", weight: 1.5, fillColor: "#fb923c", fillOpacity: 0.18,
      className: "storm-rect", interactive: false,
    }).addTo(S.map);
    S.stormLayer.bindTooltip("⛈ storm core · echo tops ~" + ((s.echoTopFt/1000)|0) + "kft",
      { permanent: false, sticky: true });
  }

  // ---------------------------------------------------------------------
  //  Frame fetch + render
  // ---------------------------------------------------------------------
  async function setT(tMs, { fetchFrame = true } = {}) {
    S.currentT = Math.max(S.tStart, Math.min(S.tEnd, tMs));
    $("clock-badge").textContent = fmtClock(S.currentT);
    $("time-readout").textContent = fmtClock(S.currentT);
    positionScrubHandle();
    if (!fetchFrame || !S.scenarioId) return;
    const seq = ++S.frameReqSeq;
    try {
      const frame = await S.api.frame(S.scenarioId, new Date(S.currentT).toISOString(), S.band);
      if (seq !== S.frameReqSeq) return;           // a newer request superseded this one
      renderFrame(frame);
    } catch (e) { console.error(e); }
  }

  function renderFrame(frame) {
    renderFlights(frame);
    restyleSectors(frame);
    renderConflicts(frame);
    renderChips(frame.metrics);
  }

  function renderChips(m) {
    const set = (id, val, pulse) => {
      const el = $(id); el.querySelector("b").textContent = val;
      el.classList.toggle("pulse", !!pulse && val > 0);
    };
    set("chip-overdemand", m.over_demand_sectors, true);
    set("chip-weather", m.weather_flights, true);
    set("chip-closed", m.closed_flights, true);
    set("chip-delay", m.total_delay_min, false);
  }

  // ---------------------------------------------------------------------
  //  Conflict feed
  // ---------------------------------------------------------------------
  const CONF_ICON = { OVER_DEMAND: "🔴", WEATHER: "🟠", CLOSED_SECTOR: "⛔" };
  const CONF_CLASS = { OVER_DEMAND: "over", WEATHER: "weather", CLOSED_SECTOR: "closed" };

  function renderConflicts(frame) {
    const feed = $("conflict-feed");
    const c = frame.conflicts || [];
    $("conflict-count").textContent = c.length;
    $("conflict-count").classList.toggle("zero", c.length === 0);
    if (!c.length) {
      feed.innerHTML = `<div class="empty">${S.hasDisruption ? "✓ Airspace clear at this time." : "No active conflicts. Inject a disruption to begin."}</div>`;
      return;
    }
    feed.innerHTML = "";
    for (const cf of c) {
      const row = document.createElement("div");
      row.className = "conflict " + (CONF_CLASS[cf.kind] || "");
      row.innerHTML =
        `<span class="c-icon">${CONF_ICON[cf.kind] || "•"}</span>` +
        `<span class="c-label">${cf.label}</span>` +
        `<span class="c-sev">${cf.kind === "OVER_DEMAND" ? "×" + cf.severity.toFixed(2) : cf.severity}</span>`;
      row.onclick = () => focusConflict(cf);
      feed.appendChild(row);
    }
  }

  function focusConflict(cf) {
    if (cf.sector_name && S.sectorLayersByName[cf.sector_name]) {
      const layer = S.sectorLayersByName[cf.sector_name];
      S.map.fitBounds(layer.getBounds().pad(1.2), { maxZoom: 6 });
      const orig = layer.options;
      layer.setStyle({ weight: 3, color: "#fff" });
      setTimeout(() => layer.setStyle(orig), 900);
    } else if (S.stormLayer) {
      S.map.fitBounds(S.stormLayer.getBounds().pad(0.4));
    }
  }

  // ---------------------------------------------------------------------
  //  Timeline canvas (baseline vs current, peak marker)
  // ---------------------------------------------------------------------
  function drawTimeline() {
    const tl = S.timeline;
    const cv = $("timeline-canvas");
    const track = $("timeline-track");
    const w = track.clientWidth, h = track.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    cv.width = w * dpr; cv.height = h * dpr;
    const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!tl || !tl.frames.length) return;

    const n = tl.frames.length;
    let max = 1;
    tl.baseline.concat(tl.current).forEach(f => { if (f.total > max) max = f.total; });
    const x = (i) => (i / (n - 1)) * w;
    const y = (v) => h - (v / max) * (h - 6) - 2;

    const area = (series, stroke, fill) => {
      ctx.beginPath(); ctx.moveTo(0, h);
      series.forEach((f, i) => ctx.lineTo(x(i), y(f.total)));
      ctx.lineTo(w, h); ctx.closePath();
      ctx.fillStyle = fill; ctx.fill();
      ctx.beginPath();
      series.forEach((f, i) => i ? ctx.lineTo(x(i), y(f.total)) : ctx.moveTo(x(i), y(f.total)));
      ctx.strokeStyle = stroke; ctx.lineWidth = 1.5; ctx.stroke();
    };
    area(tl.baseline, "#4a5870", "#4a587022");      // do-nothing (faint)
    area(tl.current, "#ef4444", "#ef444433");       // AI-managed (bright)

    // peak marker
    const peak = $("peak-marker");
    if (tl.peak_t) {
      const pi = tl.frames.indexOf(tl.peak_t);
      peak.classList.remove("hidden");
      peak.style.left = (x(pi >= 0 ? pi : 0)) + "px";
    } else peak.classList.add("hidden");
  }

  function positionScrubHandle() {
    const track = $("timeline-track");
    const frac = (S.tEnd === S.tStart) ? 0 : (S.currentT - S.tStart) / (S.tEnd - S.tStart);
    $("scrub-handle").style.left = (frac * track.clientWidth) + "px";
  }

  function wireTimeline() {
    const track = $("timeline-track");
    let dragging = false;
    const seek = (clientX) => {
      const r = track.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
      setT(S.tStart + frac * (S.tEnd - S.tStart));
    };
    track.addEventListener("mousedown", (e) => { dragging = true; stopPlay(); seek(e.clientX); });
    window.addEventListener("mousemove", (e) => { if (dragging) seek(e.clientX); });
    window.addEventListener("mouseup", () => { dragging = false; });
    $("peak-marker").addEventListener("click", (e) => {
      e.stopPropagation();
      if (S.timeline && S.timeline.peak_t) { stopPlay(); setT(new Date(S.timeline.peak_t).getTime()); }
    });
  }

  // ---------------------------------------------------------------------
  //  Playback
  // ---------------------------------------------------------------------
  function play() {
    if (S.playing || !S.scenarioId) return;
    S.playing = true; $("play-btn").textContent = "⏸";
    if (S.currentT >= S.tEnd) S.currentT = S.tStart;
    S.playTimer = setInterval(() => {
      const next = S.currentT + S.stepMin * 60000;
      if (next > S.tEnd) { setT(S.tEnd); stopPlay(); return; }
      setT(next);
    }, 420);
  }
  function stopPlay() {
    S.playing = false; $("play-btn").textContent = "▶";
    if (S.playTimer) { clearInterval(S.playTimer); S.playTimer = null; }
  }
  function togglePlay() { S.playing ? stopPlay() : play(); }

  // ---------------------------------------------------------------------
  //  Co-pilot panel
  // ---------------------------------------------------------------------
  const ACT_ICON = { REROUTE: "↪", DELAY: "⏱", ALTITUDE: "⤴" };

  async function askCopilot() {
    if (!S.scenarioId) return;
    const body = $("copilot-body");
    $("suggest-btn").disabled = true;
    body.innerHTML = `<div class="thinking"><div class="spinner"></div>Co-pilot is analyzing the airspace…</div>`;
    try {
      const res = await S.api.suggest(S.scenarioId);
      renderSuggestion(res);
    } catch (e) {
      body.innerHTML = `<div class="empty">Co-pilot error: ${e.message}</div>`;
    } finally {
      $("suggest-btn").disabled = false;
    }
  }

  function renderSuggestion(res) {
    const body = $("copilot-body");
    const srcBadge = $("copilot-src");
    srcBadge.classList.remove("hidden");
    srcBadge.textContent = res.source === "claude" ? "Claude" : "offline solver";
    srcBadge.className = "src-badge " + (res.source === "claude" ? "claude" : "fallback");

    if (!res.mitigation) {
      body.innerHTML = `<div class="empty">✓ ${res.world_summary || "All clear — no mitigations needed."}</div>`;
      return;
    }
    S.suggestion = res.mitigation;
    const m = res.mitigation, im = m.impact;
    const pills = [];
    if (im.conflicts_resolved > 0) pills.push(`<span class="pill good">−${im.conflicts_resolved} conflict-load</span>`);
    if (im.conflicts_created > 0)  pills.push(`<span class="pill warn">+${im.conflicts_created} new</span>`);
    if (im.delay_min > 0)          pills.push(`<span class="pill cost">+${im.delay_min} min</span>`);
    if (im.extra_nm > 0)           pills.push(`<span class="pill cost">+${im.extra_nm} nm</span>`);

    body.innerHTML = `
      <div class="card">
        <div class="card-title"><span class="act-icon">${ACT_ICON[m.action] || "•"}</span>${m.action} ${m.flight_number}</div>
        <div class="card-sub">${m.flight_id.split("|")[0]} · target: ${m.params && (m.params.side || m.params.minutes || m.params.new_alt_ft) || "—"}</div>
        <div class="pills">${pills.join("")}</div>
        <div class="rationale">${m.rationale}</div>
        <div class="card-actions">
          <button class="btn btn-primary" id="apply-mit">Apply</button>
          <button class="btn btn-ghost" id="skip-mit">Skip</button>
        </div>
      </div>`;
    $("world-summary-line") || body.insertAdjacentHTML("beforeend",
      `<div class="card-sub" style="margin-top:8px">${res.world_summary || ""}</div>`);
    $("apply-mit").onclick = () => applyMitigation(m);
    $("skip-mit").onclick = () => askCopilot();
  }

  async function applyMitigation(m) {
    try {
      const res = await S.api.apply(S.scenarioId, m.id, S.band);
      S.timeline = res; drawTimeline();
      logApplied(m);
      $("copilot-body").innerHTML = `<div class="empty">✓ Applied. ${res.peak_total > 0 ? "Conflicts remain — ask again." : "Airspace clear."}</div>`;
      S.suggestion = null;
      await setT(S.currentT);                  // refresh current frame
      toast(`Applied ${m.action} ${m.flight_number} — conflict curve updated`);
    } catch (e) { toast("Apply failed: " + e.message, true); }
  }

  function logApplied(m) {
    const log = $("applied-log");
    const item = document.createElement("div");
    item.className = "log-item";
    item.innerHTML = `<span class="ok">✓</span> ${ACT_ICON[m.action]} ${m.action} ${m.flight_number} <span style="color:var(--faint)">(−${m.impact.conflicts_resolved})</span>`;
    log.prepend(item);
  }

  // ---------------------------------------------------------------------
  //  Scenario / disruption actions
  // ---------------------------------------------------------------------
  async function loadScenario(snapshotId) {
    stopPlay();
    clearFlights();
    const res = await S.api.load(snapshotId);
    S.scenarioId = res.scenario_id;
    S.tStart = new Date(res.t_start).getTime();
    S.tEnd = new Date(res.t_end).getTime();
    S.stepMin = res.step_min;
    S.hasDisruption = false;
    S.sectorsGeo = await S.api.sectorsGeo(S.scenarioId);
    drawSectors();
    drawStorm(null);
    clearAppliedLog();
    S.timeline = await S.api.timeline(S.scenarioId, S.band);
    drawTimeline();
    $("disrupt-btn").disabled = false;
    $("reset-btn").disabled = false;
    $("suggest-btn").disabled = true;
    $("play-btn").disabled = false;
    await setT(S.tStart);
    toast(`Loaded ${snapshotId} · ${res.flight_count.toLocaleString()} flights`);
  }

  function clearFlights() {
    for (const id of Object.keys(S.flightMarkers)) S.map.removeLayer(S.flightMarkers[id]);
    S.flightMarkers = {};
  }
  function clearAppliedLog() { $("applied-log").innerHTML = ""; $("copilot-body").innerHTML =
    `<div class="empty">The co-pilot proposes one fix at a time once conflicts appear.</div>`;
    $("copilot-src").classList.add("hidden"); }

  async function injectDisruption() {
    const presetId = $("disruption-select").value;
    if (!presetId) { toast("Pick a disruption first", true); return; }
    const res = await S.api.disrupt(S.scenarioId, { preset_id: presetId }, S.band);
    S.timeline = res; drawTimeline();
    S.hasDisruption = true;
    drawStorm(res.disruption);
    $("suggest-btn").disabled = false;
    clearAppliedLog();
    // jump to the peak to show the worst moment
    if (res.peak_t) { stopPlay(); await setT(new Date(res.peak_t).getTime()); }
    else await setT(S.currentT);
    toast(`Disruption injected — peak ${res.peak_t ? fmtClock(new Date(res.peak_t).getTime()) : "n/a"} (${res.peak_total} conflicts)`);
  }

  async function resetScenario() {
    const res = await S.api.reset(S.scenarioId, { keep_disruption: true }, S.band);
    S.timeline = res; drawTimeline();
    clearAppliedLog();
    await setT(S.currentT);
    toast("Reset to baseline-with-disruption");
  }

  async function switchBand(band) {
    S.band = band;
    document.querySelectorAll(".seg-btn").forEach(b => b.classList.toggle("active", b.dataset.band === band));
    if (!S.scenarioId) return;
    drawSectors();
    S.timeline = await S.api.timeline(S.scenarioId, S.band); drawTimeline();
    await setT(S.currentT);
  }

  // ---------------------------------------------------------------------
  //  Init
  // ---------------------------------------------------------------------
  async function init() {
    initMap();
    wireTimeline();
    S.api = await makeApi();

    const health = await S.api.health().catch(() => ({ has_claude: false }));
    const aiChip = $("chip-ai");
    if (S.api.isMock) { aiChip.className = "chip ai-off"; aiChip.innerHTML = "◈ mock data"; }
    else if (health.has_claude) { aiChip.className = "chip ai-on"; aiChip.innerHTML = "◆ Claude co-pilot online"; }
    else { aiChip.className = "chip ai-off"; aiChip.innerHTML = "◇ offline solver"; }

    const { snapshots, presets } = await S.api.scenarios();
    const ssel = $("scenario-select");
    snapshots.forEach(s => {
      const o = document.createElement("option");
      o.value = s.id; o.textContent = `${s.id}  ·  ${s.flight_count.toLocaleString()} flights`;
      ssel.appendChild(o);
    });
    const dsel = $("disruption-select");
    presets.forEach(p => {
      const o = document.createElement("option");
      o.value = p.id; o.textContent = p.label; o.title = p.description;
      dsel.appendChild(o);
    });

    // events
    ssel.onchange = () => loadScenario(ssel.value);
    $("disrupt-btn").onclick = injectDisruption;
    $("reset-btn").onclick = resetScenario;
    $("suggest-btn").onclick = askCopilot;
    $("play-btn").onclick = togglePlay;
    document.querySelectorAll(".seg-btn").forEach(b => b.onclick = () => switchBand(b.dataset.band));
    window.addEventListener("resize", () => { drawTimeline(); positionScrubHandle(); });

    // auto-load the first scenario for an instant demo
    if (snapshots.length) { ssel.value = snapshots[0].id; await loadScenario(snapshots[0].id); }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
