/* ============================================================================
   mock.js — a self-contained mini-simulation that implements the BACKEND_DESIGN
   §2 API contract entirely in the browser. Lets the war room run (and the demo
   land) with no backend. Activate with ?mock=1 (or auto-fallback in app.js).

   It is a real little simulation: synthetic flights fly straight routes across
   CONUS, get binned into a grid of sectors, and conflicts (over-demand / weather
   / closed-sector) are computed per frame. Disruptions and mitigations actually
   change the numbers, so "Apply" visibly clears conflicts and lowers the curve.

   Mitigations are *flow* actions: a single suggestion can reroute/hold the whole
   stream of flights through a closed sector or the storm — the way real reroute
   advisories work — so one click clears one conflict and the room calms down.
   ========================================================================== */
(function () {
  "use strict";

  // ---- world geometry --------------------------------------------------
  const LON_MIN = -125, LON_MAX = -67, LAT_MIN = 25, LAT_MAX = 49;
  const NCOLS = 11, NROWS = 6;                 // 66 sectors per band
  const dLon = (LON_MAX - LON_MIN) / NCOLS;
  const dLat = (LAT_MAX - LAT_MIN) / NROWS;
  const CAPACITY = 10;                         // scaled per-sector capacity
  const STEP_MIN = 5;
  const WINDOW_HRS = 4;
  const HIGH_FLOOR = 35000;
  const N_FLIGHTS = 420;

  const AIRPORTS = {
    KSEA:[-122.31,47.45], KSFO:[-122.37,37.62], KLAX:[-118.40,33.94],
    KLAS:[-115.15,36.08], KPHX:[-112.01,33.43], KDEN:[-104.67,39.86],
    KDFW:[ -97.04,32.90], KIAH:[ -95.34,29.98], KMCI:[ -94.71,39.30],
    KMSP:[ -93.22,44.88], KSTL:[ -90.37,38.75], KMEM:[ -89.98,35.04],
    KORD:[ -87.90,41.98], KATL:[ -84.43,33.64], KDTW:[ -83.35,42.21],
    KMIA:[ -80.29,25.79], KBOS:[ -71.01,42.36], KJFK:[ -73.78,40.64],
    KEWR:[ -74.17,40.69], KCLT:[ -80.94,35.21], KPHL:[ -75.24,39.87],
  };
  const ICAOS = Object.keys(AIRPORTS);
  const AIRLINES = ["AAL","UAL","DAL","SWA","JBU","ASA","SKW","FFT","NKS"];

  // Storm footprint (lon/lat box + echo top). Placed over the Northern Plains /
  // high Rockies — heavy transcontinental overflight, but no hub airport sits
  // underneath (so its departures aren't trapped and the room can be cleared).
  const STORM = { lonMin: -108, lonMax: -100, latMin: 41, latMax: 46, echoTopFt: 41000 };

  // deterministic RNG so the same scenario looks identical every run
  function makeRng(seed) {
    let s = seed >>> 0;
    return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
  }

  const haversineNm = (a, b) => {
    const R = 3440.065, toR = Math.PI / 180;
    const dLa = (b[1]-a[1])*toR, dLo = (b[0]-a[0])*toR;
    const la1 = a[1]*toR, la2 = b[1]*toR;
    const h = Math.sin(dLa/2)**2 + Math.cos(la1)*Math.cos(la2)*Math.sin(dLo/2)**2;
    return 2 * R * Math.asin(Math.sqrt(h));
  };

  const sectorName = (band, row, col) =>
    `${band}_${String(row * NCOLS + col).padStart(3, "0")}`;

  function sectorOf(lon, lat, band) {
    if (lon < LON_MIN || lon > LON_MAX || lat < LAT_MIN || lat > LAT_MAX) return null;
    let col = Math.floor((lon - LON_MIN) / dLon);
    let row = Math.floor((LAT_MAX - lat) / dLat);          // row 0 = north
    col = Math.max(0, Math.min(NCOLS - 1, col));
    row = Math.max(0, Math.min(NROWS - 1, row));
    return sectorName(band, row, col);
  }

  // ---- snapshots (mirror the real bundle's asked_at dirs) ---------------
  const SNAPSHOTS = [
    "2025-05-29T21:00:00Z", "2025-06-10T17:00:00Z", "2025-07-08T22:00:00Z",
    "2025-08-21T18:00:00Z", "2026-01-13T18:00:00Z", "2026-04-08T18:00:00Z",
  ];

  const PRESETS = [
    { id: "midwest_storm", label: "Convective line over the Plains", kind: "STORM",
      description: "A line of severe storms over the Northern Plains / high Rockies. Closes the HIGH cells under the core." },
    { id: "ord_ground_stop", label: "ORD ground stop (45 min)", kind: "GROUND_STOP",
      description: "Hold all KORD departures for 45 minutes — watch the downstream bunching." },
    { id: "denver_closure", label: "Rockies sector closure", kind: "SECTOR_CLOSURE",
      description: "Close two HIGH sectors over the Rockies (military airspace activation)." },
  ];

  // ============================================================
  //  Scenario state
  // ============================================================
  class Scenario {
    constructor(snapshotId) {
      this.snapshotId = snapshotId;
      this.id = "sc_" + Math.random().toString(36).slice(2, 8);
      const seedBase = snapshotId.split("").reduce((a, c) => a + c.charCodeAt(0), 0);
      this.rng = makeRng(seedBase * 7919);

      this.t0 = new Date(snapshotId.replace("Z", "+00:00")).getTime();
      this.t1 = this.t0 + WINDOW_HRS * 3600 * 1000;
      this.nFrames = Math.floor((this.t1 - this.t0) / (STEP_MIN * 60000)) + 1;

      this.disruption = null;
      this.closedSectors = new Set();
      this.applied = [];                  // committed mitigations
      this.mitigationsById = {};
      this._buildFlights();
    }

    frameTimes() {
      return Array.from({ length: this.nFrames }, (_, i) =>
        new Date(this.t0 + i * STEP_MIN * 60000).toISOString());
    }

    _buildFlights() {
      this.flights = [];
      for (let i = 0; i < N_FLIGHTS; i++) {
        let o, d;
        do { o = ICAOS[Math.floor(this.rng() * ICAOS.length)];
             d = ICAOS[Math.floor(this.rng() * ICAOS.length)]; } while (o === d);
        const orig = AIRPORTS[o], dest = AIRPORTS[d];
        const dist = haversineNm(orig, dest);
        const alt = dist > 900
          ? 35000 + Math.floor(this.rng() * 6) * 1000          // long haul -> HIGH
          : 28000 + Math.floor(this.rng() * 9) * 1000;          // short -> mixed
        const speed = 420 + Math.floor(this.rng() * 100);
        const durMs = (dist / speed) * 3600 * 1000;
        const dep = this.t0 - 90 * 60000 + this.rng() * (WINDOW_HRS * 3600 * 1000 + 60 * 60000);
        const fn = AIRLINES[Math.floor(this.rng() * AIRLINES.length)] +
                   (100 + Math.floor(this.rng() * 8900));
        this.flights.push({
          flight_id: `${fn}|${new Date(dep).toISOString()}|${o}`,
          fn, origin: o, dest: d,
          baseT0: dep, baseT1: dep + durMs,
          baseWaypts: [orig.slice(), dest.slice()],
          baseAlt: alt, speed,
          t0: dep, t1: dep + durMs, waypts: [orig.slice(), dest.slice()], alt,
        });
      }
      this.flightIndex = {};
      this.flights.forEach(f => { this.flightIndex[f.flight_id] = f; });
    }

    _band(alt) { return alt >= HIGH_FLOOR ? "HIGH" : "LOW"; }

    _position(f, tMs) {
      if (tMs < f.t0 || tMs > f.t1) return null;
      const frac = (tMs - f.t0) / (f.t1 - f.t0);
      const pts = f.waypts;
      let total = 0; const seg = [];
      for (let i = 0; i < pts.length - 1; i++) {
        const dnm = haversineNm(pts[i], pts[i + 1]); seg.push(dnm); total += dnm;
      }
      let target = frac * total, acc = 0;
      for (let i = 0; i < seg.length; i++) {
        if (acc + seg[i] >= target || i === seg.length - 1) {
          const local = seg[i] === 0 ? 0 : (target - acc) / seg[i];
          const a = pts[i], b = pts[i + 1];
          return [a[0] + (b[0]-a[0])*local, a[1] + (b[1]-a[1])*local];
        }
        acc += seg[i];
      }
      return pts[pts.length - 1].slice();
    }

    _inStorm(lon, lat) {
      return lon >= STORM.lonMin && lon <= STORM.lonMax &&
             lat >= STORM.latMin && lat <= STORM.latMax;
    }

    // status of one flight at time tMs: null | {p, band, sec, status}
    _status(f, tMs) {
      const p = this._position(f, tMs);
      if (!p) return null;
      const band = this._band(f.alt);
      const sec = sectorOf(p[0], p[1], band);
      const stormActive = this.disruption && this.disruption.kind === "STORM";
      if (this.closedSectors.has(sec)) return { p, band, sec, status: "closed" };
      if (stormActive && this._inStorm(p[0], p[1]) && f.alt <= STORM.echoTopFt)
        return { p, band, sec, status: "weather" };
      return { p, band, sec, status: "ok" };
    }

    // full world state at a frame time (for the given map band)
    frameAt(tMs, band) {
      const flights = [], occ = {}, occFlights = {}, weatherFlights = [], closedFlights = [];
      for (const f of this.flights) {
        const st = this._status(f, tMs);
        if (!st) continue;
        if (st.sec) { occ[st.sec] = (occ[st.sec] || 0) + 1;
          (occFlights[st.sec] = occFlights[st.sec] || []).push(f.flight_id); }
        if (st.status === "weather") weatherFlights.push(f.flight_id);
        if (st.status === "closed") closedFlights.push(f.flight_id);
        if (st.band === band)
          flights.push({ id: f.flight_id, fn: f.fn, lat: st.p[1], lon: st.p[0],
                         band: st.band, alt_ft: f.alt, status: st.status });
      }

      const sectors = [];
      const seen = new Set();
      for (const [name, count] of Object.entries(occ)) {
        sectors.push({ name, count, capacity: CAPACITY,
          ratio: +(count / CAPACITY).toFixed(2), closed: this.closedSectors.has(name) });
        seen.add(name);
      }
      for (const name of this.closedSectors)
        if (!seen.has(name)) sectors.push({ name, count: 0, capacity: CAPACITY, ratio: 0, closed: true });

      // conflicts — one per problem (sector or the storm), not one per flight
      const conflicts = [];
      let closedSectorCount = 0;
      for (const s of sectors) {
        if (s.closed && s.count > 0) {
          closedSectorCount++;
          conflicts.push({ id: "cl_" + s.name, kind: "CLOSED_SECTOR", severity: 100 + s.count,
            sector_name: s.name, flight_ids: occFlights[s.name] || [],
            label: `${s.name} closed — ${s.count} flights inside` });
        } else if (s.count > s.capacity) {
          conflicts.push({ id: "od_" + s.name, kind: "OVER_DEMAND", severity: s.ratio,
            sector_name: s.name, flight_ids: occFlights[s.name] || [],
            label: `${s.name} over capacity (${s.count}/${s.capacity})` });
        }
      }
      if (weatherFlights.length)
        conflicts.push({ id: "wx", kind: "WEATHER", severity: 50 + weatherFlights.length,
          sector_name: null, flight_ids: weatherFlights,
          label: `${weatherFlights.length} flights penetrating the storm` });
      conflicts.sort((a, b) => b.severity - a.severity);

      const totalDelay = this.applied.reduce((s, m) => s + (m.impact.delay_min || 0), 0) +
        (this.disruption && this.disruption.kind === "GROUND_STOP" ? this._groundStopDelay() : 0);

      return {
        t: new Date(tMs).toISOString(),
        flights, sectors, conflicts,
        metrics: {
          over_demand_sectors: conflicts.filter(c => c.kind === "OVER_DEMAND").length,
          weather_flights: weatherFlights.length,
          closed_flights: closedFlights.length,
          closed_sectors: closedSectorCount,
          total_conflicts: conflicts.length,
          total_delay_min: Math.round(totalDelay),
          airborne: flights.length,
        },
      };
    }

    _groundStopDelay() {
      if (!this.disruption || this.disruption.kind !== "GROUND_STOP") return 0;
      return (this.disruption._heldCount || 0) * (this.disruption.params.hold_min || 0);
    }

    // per-frame conflict count (the timeline series; the headline number)
    timelineSeries(band) {
      return this.frameTimes().map(iso => {
        const fr = this.frameAt(new Date(iso).getTime(), band);
        return {
          over_demand: fr.metrics.over_demand_sectors,
          weather: fr.metrics.weather_flights > 0 ? 1 : 0,
          closed: fr.metrics.closed_sectors,
          total: fr.metrics.total_conflicts,
        };
      });
    }

    // total conflict load (scalar) — used to score mitigations
    _loadScore(band) { return this.timelineSeries(band).reduce((s, f) => s + f.total, 0); }
  }

  // ============================================================
  //  Disruptions
  // ============================================================
  function applyDisruption(sc, kind, params) {
    resetScenario(sc, false);
    sc.disruption = { kind, params: params || {} };
    sc.avoidBox = null;

    if (kind === "STORM") {
      for (let r = 0; r < NROWS; r++)
        for (let c = 0; c < NCOLS; c++) {
          const lon = LON_MIN + (c + 0.5) * dLon, lat = LAT_MAX - (r + 0.5) * dLat;
          if (sc._inStorm(lon, lat)) sc.closedSectors.add(sectorName("HIGH", r, c));
        }
      sc.disruption.storm = STORM;
      sc.avoidBox = STORM;
    } else if (kind === "SECTOR_CLOSURE") {
      const list = (params && params.sectors) || ["HIGH_013", "HIGH_014"];
      list.forEach(s => sc.closedSectors.add(s));
      sc.avoidBox = closedBox(sc);
    } else if (kind === "GROUND_STOP") {
      const ap = (params && params.airport) || "KORD";
      const hold = ((params && params.hold_min) || 45) * 60000;
      let held = 0;
      for (const f of sc.flights) {
        if (f.origin === ap && f.baseT0 >= sc.t0 - 30*60000 && f.baseT0 <= sc.t1) {
          f.t0 = f.baseT0 + hold; f.t1 = f.baseT1 + hold; held++;
        }
      }
      sc.disruption._heldCount = held;
      sc.disruption.params = { airport: ap, hold_min: (hold / 60000) };
    }
  }

  function presetToDisruption(presetId) {
    switch (presetId) {
      case "midwest_storm":   return ["STORM", { refc_dbz: 40, close_impacted_sectors: true }];
      case "ord_ground_stop": return ["GROUND_STOP", { airport: "KORD", hold_min: 45 }];
      case "denver_closure":  return ["SECTOR_CLOSURE", { sectors: ["HIGH_013", "HIGH_014"] }];
      default:                return ["STORM", {}];
    }
  }

  function resetScenario(sc, keepDisruption) {
    for (const f of sc.flights) {
      f.t0 = f.baseT0; f.t1 = f.baseT1;
      f.waypts = f.baseWaypts.map(p => p.slice());
      f.alt = f.baseAlt;
    }
    sc.applied = []; sc.mitigationsById = {};
    if (!keepDisruption) { sc.disruption = null; sc.closedSectors = new Set(); sc.avoidBox = null; }
    else if (sc.disruption) applyDisruption(sc, sc.disruption.kind, sc.disruption.params);
  }

  // ============================================================
  //  Mitigations — group/flow aware (evaluate = what-if; apply = commit)
  // ============================================================
  const flightById = (sc, id) => sc.flightIndex[id];
  const peakBand = () => "HIGH";
  const snapshotFlight = (f) => ({ t0: f.t0, t1: f.t1, waypts: f.waypts.map(p => p.slice()), alt: f.alt });
  const restoreFlight = (f, s) => { f.t0 = s.t0; f.t1 = s.t1; f.waypts = s.waypts; f.alt = s.alt; };

  // lon/lat bounding box of a grid sector, padded slightly
  function sectorBox(name) {
    const idx = parseInt(name.split("_")[1], 10);
    const row = Math.floor(idx / NCOLS), col = idx % NCOLS;
    const lon0 = LON_MIN + col * dLon, lat1 = LAT_MAX - row * dLat;
    return { lonMin: lon0, lonMax: lon0 + dLon, latMin: lat1 - dLat, latMax: lat1 };
  }
  // union bbox of the currently-closed sectors (the no-fly region to route around)
  function closedBox(sc) {
    let b = null;
    for (const name of sc.closedSectors) {
      const s = sectorBox(name);
      b = b ? { lonMin: Math.min(b.lonMin, s.lonMin), lonMax: Math.max(b.lonMax, s.lonMax),
                latMin: Math.min(b.latMin, s.latMin), latMax: Math.max(b.latMax, s.latMax) } : s;
    }
    return b;
  }

  // Where (if anywhere) the straight route O->D passes through the avoid box.
  function boxCrossing(o, d, box) {
    if (!box) return null;
    const N = 30; const inside = [];
    for (let i = 0; i <= N; i++) {
      const t = i / N, lon = o[0] + (d[0]-o[0])*t, lat = o[1] + (d[1]-o[1])*t;
      if (lon >= box.lonMin && lon <= box.lonMax && lat >= box.latMin && lat <= box.latMax)
        inside.push([lon, lat]);
    }
    return inside.length ? inside[Math.floor(inside.length / 2)] : null;
  }

  const hashStr = (s) => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0); };

  // Route a flight around the storm via a pair of gateway waypoints that bracket
  // the storm's longitude span, well clear of its lat edge. This guarantees the
  // detour leg avoids every closed cell. "split" sends it around the nearer edge,
  // and the gateway latitude is fanned across parallel tracks (stable hash) so
  // the displaced stream disperses instead of forming a new jam.
  function mutateReroute(f, side, box) {
    const o = f.baseWaypts[0], d = f.baseWaypts[1];
    const mid = boxCrossing(o, d, box);
    if (mid && box) {
      let s = side || "split";
      if (s === "split") s = (box.latMax - mid[1]) <= (mid[1] - box.latMin) ? "north" : "south";
      const track = hashStr(f.flight_id) % 3;                 // 0,1,2 parallel tracks
      const lat = s === "north" ? box.latMax + 1.8 + track * 1.3
                                : box.latMin - 1.8 - track * 1.3;
      f.waypts = [o.slice(), [box.lonMin - 1.5, lat], [box.lonMax + 1.5, lat], d.slice()];
    } else {
      f.waypts = [o.slice(), d.slice()];   // didn't cross — no detour needed
    }
    let dist = 0; for (let i = 0; i < f.waypts.length - 1; i++) dist += haversineNm(f.waypts[i], f.waypts[i+1]);
    const extraNm = Math.max(0, dist - haversineNm(o, d));
    const extraMs = (extraNm / f.speed) * 3600 * 1000;
    f.t1 = f.t1 + extraMs;
    return { extra_nm: Math.round(extraNm), delay_min: Math.round(extraMs / 60000) };
  }
  function mutateDelay(f, minutes) { f.t0 += minutes * 60000; f.t1 += minutes * 60000; return { extra_nm: 0, delay_min: minutes }; }
  function mutateAltitude(f, newAlt) { f.alt = newAlt; return { extra_nm: 0, delay_min: 0 }; }

  // Apply `action` to every flight in `ids`. DELAY supports staggering to
  // de-bunch a demand hotspot. Returns the max delay/extra across the stream.
  function mutateStream(sc, action, ids, params) {
    let maxDelay = 0, maxNm = 0;
    const box = sc.avoidBox || (sc.disruption && sc.disruption.kind === "STORM" ? STORM : null);
    ids.forEach((id, i) => {
      const f = flightById(sc, id); if (!f) return;
      let cost;
      if (action === "REROUTE") cost = mutateReroute(f, (params && params.side) || "north", box);
      else if (action === "DELAY") {
        const mins = (params && params.minutes || 25) + (params && params.stagger ? i * (params.step || 8) : 0);
        cost = mutateDelay(f, mins);
      } else if (action === "ALTITUDE") cost = mutateAltitude(f, (params && params.new_alt_ft) || (STORM.echoTopFt + 2000));
      maxDelay = Math.max(maxDelay, cost.delay_min); maxNm = Math.max(maxNm, cost.extra_nm);
    });
    return { delay_min: maxDelay, extra_nm: maxNm };
  }

  function evaluateStream(sc, action, ids, params, before) {
    const band = peakBand();
    if (before == null) before = sc._loadScore(band);
    const snaps = ids.map(id => [flightById(sc, id), null]).filter(x => x[0]);
    snaps.forEach(p => p[1] = snapshotFlight(p[0]));
    const cost = mutateStream(sc, action, ids, params);
    const after = sc._loadScore(band);
    snaps.forEach(([f, s]) => restoreFlight(f, s));
    const delta = before - after;
    return { impact: {
      conflicts_resolved: Math.max(0, delta), conflicts_created: Math.max(0, -delta),
      delay_min: cost.delay_min, extra_nm: cost.extra_nm, flights_affected: ids.length,
    } };
  }

  // ============================================================
  //  Co-pilot (mock) — target the worst conflict, reroute/hold its stream
  // ============================================================
  // flights that pass through sector X (or the storm) at ANY frame -> the stream
  function streamThrough(sc, predicate) {
    const times = sc.frameTimes(); const set = new Set();
    for (const f of sc.flights)
      for (const iso of times) {
        const st = sc._status(f, new Date(iso).getTime());
        if (st && predicate(st)) { set.add(f.flight_id); break; }
      }
    return [...set];
  }

  function rationaleFor(action, mit, label, peakIso) {
    const t = peakIso ? new Date(peakIso).toISOString().slice(11, 16) + "Z" : "the peak";
    const n = mit.flights_affected;
    if (action === "REROUTE")
      return `${n} flights are routed through ${label}, peaking at ${t}. Issuing a ${mit.params.side}bound reroute advisory clears the whole stream around it — about +${mit.impact.delay_min} min and +${mit.impact.extra_nm} nm on the worst-affected tail, and it opens ${label} back up.`;
    if (action === "DELAY")
      return `${label} tips over capacity at ${t} as ${n} flights converge. A staggered ground hold (up to +${mit.impact.delay_min} min) spreads the stream out so demand stays under the sector limit — no extra track miles.`;
    if (action === "ALTITUDE")
      return `${n} flights are penetrating the storm below the echo tops. Stepping the stream above the tops clears the weather conflict with no delay and no extra distance.`;
    return `Recommended flow action affecting ${n} flights.`;
  }

  function suggest(sc) {
    const band = peakBand();
    const series = sc.timelineSeries(band);
    const times = sc.frameTimes();
    let peakIdx = 0, peakVal = -1;
    series.forEach((s, i) => { if (s.total > peakVal) { peakVal = s.total; peakIdx = i; } });
    if (peakVal <= 0) return { mitigation: null, world_summary: "All clear — no active conflicts.", source: "fallback" };

    const peakT = new Date(times[peakIdx]).getTime();
    const fr = sc.frameAt(peakT, band);
    const worst = fr.conflicts[0];
    if (!worst) return { mitigation: null, world_summary: "All clear.", source: "fallback" };

    // build the candidate stream + actions for the worst conflict
    let ids = [], actions = [], label = worst.sector_name || "the storm";
    if (worst.kind === "CLOSED_SECTOR") {
      ids = streamThrough(sc, st => st.sec === worst.sector_name);
      actions = [["REROUTE", { side: "split" }], ["REROUTE", { side: "north" }], ["REROUTE", { side: "south" }]];
      label = `closed ${worst.sector_name}`;
    } else if (worst.kind === "WEATHER") {
      ids = streamThrough(sc, st => st.status === "weather");
      actions = [["ALTITUDE", { new_alt_ft: STORM.echoTopFt + 2000 }],
                 ["REROUTE", { side: "split" }]];
      label = "the storm core";
    } else { // OVER_DEMAND
      ids = (worst.flight_ids || []).slice(0, 12);
      actions = [["DELAY", { minutes: 12, stagger: true, step: 9 }],
                 ["REROUTE", { side: "split" }]];
      label = worst.sector_name;
    }
    if (!ids.length) return { mitigation: null, world_summary: "No actionable stream found.", source: "fallback" };

    const before = sc._loadScore(band);
    let best = null;
    for (const [act, p] of actions) {
      const ev = evaluateStream(sc, act, ids, p, before);
      const net = ev.impact.conflicts_resolved - 1.5 * ev.impact.conflicts_created - ev.impact.delay_min * 0.03;
      if (!best || net > best.net) best = { act, p, ev, net };
    }

    // Diminishing returns: if the best available move can't meaningfully relieve
    // anything, stop rather than loop on a zero-impact suggestion.
    if (!best || best.ev.impact.conflicts_resolved <= 0) {
      const m0 = fr.metrics;
      return {
        mitigation: null,
        world_summary: `Down to ${fr.conflicts.length} residual conflict(s) (${m0.closed_sectors} closed sector, ${m0.weather_flights} in weather). No further fix improves the picture without creating new hotspots — these are the unavoidable few under the disruption.`,
        source: "fallback",
      };
    }

    const lead = flightById(sc, ids[0]);
    const mid = "m_" + Math.random().toString(36).slice(2, 8);
    const mitigation = {
      id: mid, action: best.act, scope: ids.length > 1 ? "stream" : "single",
      flight_id: ids[0], flight_number: lead ? lead.fn : ids[0].split("|")[0],
      flight_ids: ids, sector_name: worst.sector_name,
      params: best.p, impact: best.ev.impact,
      flights_affected: ids.length,
    };
    mitigation.rationale = rationaleFor(best.act, mitigation, label, times[peakIdx]);
    sc.mitigationsById[mid] = mitigation;

    const m = fr.metrics;
    const summary = `${m.closed_sectors} closed sector(s), ${m.over_demand_sectors} over capacity, ${m.weather_flights} flights in the storm; ${fr.conflicts.length} active conflicts, peak ${times[peakIdx].slice(11,16)}Z.`;
    return { mitigation, world_summary: summary, source: "fallback" };
  }

  function applyMitigation(sc, mit) {
    mutateStream(sc, mit.action, mit.flight_ids || [mit.flight_id], mit.params);
    sc.applied.push(mit);
  }

  // ============================================================
  //  Sector GeoJSON (grid polygons) — sent once to the map
  // ============================================================
  function sectorsGeoJSON() {
    const features = [];
    for (const band of ["HIGH", "LOW"]) {
      for (let r = 0; r < NROWS; r++)
        for (let c = 0; c < NCOLS; c++) {
          const lon0 = LON_MIN + c * dLon, lon1 = lon0 + dLon;
          const lat1 = LAT_MAX - r * dLat, lat0 = lat1 - dLat;
          features.push({
            type: "Feature",
            geometry: { type: "Polygon", coordinates: [[
              [lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0],
            ]] },
            properties: {
              name: sectorName(band, r, c), band,
              alt_from_ft: band === "HIGH" ? 35000 : 0,
              alt_to_ft: band === "HIGH" ? 60000 : 35000,
              capacity: CAPACITY,
            },
          });
        }
    }
    return { type: "FeatureCollection", features };
  }

  // ============================================================
  //  Timeline payload (baseline vs current)
  // ============================================================
  function timelinePayload(sc, band) {
    const frames = sc.frameTimes();
    const savedApplied = sc.applied, savedMids = sc.mitigationsById;
    const savedFlights = sc.flights.map(snapshotFlight);
    // baseline = disruption present but NO mitigations applied
    for (const f of sc.flights) { f.t0 = f.baseT0; f.t1 = f.baseT1; f.waypts = f.baseWaypts.map(p=>p.slice()); f.alt = f.baseAlt; }
    if (sc.disruption && sc.disruption.kind === "GROUND_STOP")
      applyDisruption(sc, "GROUND_STOP", sc.disruption.params);
    const baseline = sc.timelineSeries(band);
    sc.flights.forEach((f, i) => restoreFlight(f, savedFlights[i]));
    sc.applied = savedApplied; sc.mitigationsById = savedMids;
    const current = sc.timelineSeries(band);

    let peakIdx = 0, peakVal = -1;
    current.forEach((s, i) => { if (s.total > peakVal) { peakVal = s.total; peakIdx = i; } });
    return {
      step_min: STEP_MIN, frames, baseline, current,
      peak_t: peakVal > 0 ? frames[peakIdx] : null, peak_total: Math.max(0, peakVal),
    };
  }

  // ============================================================
  //  Public mock backend — same async surface as the HTTP client
  // ============================================================
  const scenarios = {};

  class MockBackend {
    constructor() { this.isMock = true; }
    _delay(ms) { return new Promise(r => setTimeout(r, ms)); }

    async health() { return { ok: true, has_claude: false, model: "mock", mock: true }; }

    async scenarios() {
      return {
        snapshots: SNAPSHOTS.map(id => ({
          id, asked_at: id, window_start: id,
          window_end: new Date(new Date(id.replace("Z","+00:00")).getTime() + WINDOW_HRS*3600000).toISOString(),
          flight_count: N_FLIGHTS,
        })),
        presets: PRESETS,
      };
    }

    async load(snapshotId) {
      const sc = new Scenario(snapshotId);
      scenarios[sc.id] = sc;
      return {
        scenario_id: sc.id, snapshot_id: snapshotId,
        t_start: new Date(sc.t0).toISOString(), t_end: new Date(sc.t1).toISOString(),
        step_min: STEP_MIN, flight_count: sc.flights.length, has_disruption: false,
      };
    }

    async sectorsGeo(_id) { return sectorsGeoJSON(); }

    async frame(id, tIso, band) {
      const sc = scenarios[id]; if (!sc) throw new Error("unknown scenario");
      return sc.frameAt(new Date(tIso).getTime(), band || "HIGH");
    }

    async timeline(id, band) {
      const sc = scenarios[id]; if (!sc) throw new Error("unknown scenario");
      return timelinePayload(sc, band || "HIGH");
    }

    async disrupt(id, body, band) {
      const sc = scenarios[id]; if (!sc) throw new Error("unknown scenario");
      let kind, params;
      if (body.preset_id) [kind, params] = presetToDisruption(body.preset_id);
      else { kind = body.kind; params = body.params; }
      applyDisruption(sc, kind, params);
      return Object.assign({ disruption: sc.disruption }, timelinePayload(sc, band || "HIGH"));
    }

    async suggest(id) {
      const sc = scenarios[id]; if (!sc) throw new Error("unknown scenario");
      await this._delay(900 + Math.random() * 900);
      return suggest(sc);
    }

    async apply(id, mitigationId, band) {
      const sc = scenarios[id]; if (!sc) throw new Error("unknown scenario");
      const mit = sc.mitigationsById[mitigationId];
      if (!mit) throw new Error("unknown mitigation");
      applyMitigation(sc, mit);
      return Object.assign({ applied: mit, applied_count: sc.applied.length }, timelinePayload(sc, band || "HIGH"));
    }

    async reset(id, body, band) {
      const sc = scenarios[id]; if (!sc) throw new Error("unknown scenario");
      resetScenario(sc, body && body.keep_disruption);
      return timelinePayload(sc, band || "HIGH");
    }
  }

  window.MockBackend = MockBackend;
})();
