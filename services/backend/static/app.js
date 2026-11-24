/* CASE twin viewer: CesiumJS over the OpenStreetMap-derived world. */
"use strict";
Cesium.Ion.defaultAccessToken = "";
const C = Cesium;
const viewer = new C.Viewer("globe", {
  baseLayer: false, baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false,
  navigationHelpButton: false, animation: false, timeline: false, fullscreenButton: false, infoBox: false,
  selectionIndicator: false, requestRenderMode: false,
});
const scene = viewer.scene;
scene.globe.baseColor = C.Color.fromCssColorString("#2a2f27");
scene.backgroundColor = C.Color.fromCssColorString("#0d0f12");
scene.skyAtmosphere.show = false; scene.fog.enabled = false; scene.globe.showGroundAtmosphere = false;
scene.globe.depthTestAgainstTerrain = false;
scene.screenSpaceCameraController.minimumZoomDistance = 80;

const S = { sensors: {}, reg: {}, state: {}, alerts: {}, ent: {}, truth: null, pick: null,
            lat: [], msgs: 0, lastRate: performance.now(), clockOffset: 0 };
window.__twin = S;
const COL = { ok: "#5fd38d", event: "#b98cff", fault: "#ff5c5c", anomaly: "#ffb347", silent: "#6b7280" };
const EVC = { flood: "#3fa7ff", plume: "#c58bff", storm: "#8fb3c9", outage: "#ffd166", traffic_disruption: "#ff9f6b" };
const sensorPts = scene.primitives.add(new C.PointPrimitiveCollection());
const entPts = scene.primitives.add(new C.PointPrimitiveCollection());
const labels = scene.primitives.add(new C.LabelCollection());
const footprints = new C.CustomDataSource("footprints"); viewer.dataSources.add(footprints);
const truthDs = new C.CustomDataSource("truth"); viewer.dataSources.add(truthDs);

function home(duration = 0) {
  viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(-79.6905, 44.3615, 1650),
    orientation: { heading: C.Math.toRadians(8), pitch: C.Math.toRadians(-38), roll: 0 }, duration });
}

async function loadWorld() {
  const gj = await (await fetch("/api/world")).json();
  const bInst = [], rInst = [], wInst = [];
  const roadW = { motorway: 9, trunk: 8, primary: 7, secondary: 6, tertiary: 5 };
  for (const f of gj.features) {
    const p = f.properties, g = f.geometry;
    if (p.layer === "building") {
      bInst.push(new C.GeometryInstance({ geometry: new C.PolygonGeometry({
        polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(g.coordinates[0].flat())),
        height: 0, extrudedHeight: p.h }),
        attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(C.Color.fromCssColorString(p.h > 12 ? "#c9cdd3" : "#a9aeb6")) } }));
    } else if (p.layer === "road") {
      rInst.push(new C.GeometryInstance({ geometry: new C.GroundPolylineGeometry({
        positions: C.Cartesian3.fromDegreesArray(g.coordinates.flat()), width: roadW[p.kind] || 3 }),
        attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(C.Color.fromCssColorString(p.bridge ? "#e0c070" : (roadW[p.kind] ? "#5a606a" : "#474c54"))) } }));
    } else if (p.layer === "water") {
      wInst.push(new C.GeometryInstance({ geometry: new C.PolygonGeometry({
        polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(g.coordinates[0].flat())), height: 0.2 }),
        attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(C.Color.fromCssColorString("#1d3d5c")) } }));
    } else if (p.layer === "asset") {
      labels.add({ position: C.Cartesian3.fromDegrees(g.coordinates[0], g.coordinates[1], 30), text: p.name,
        font: "12px system-ui", fillColor: C.Color.fromCssColorString("#f1e3b0"), outlineColor: C.Color.BLACK, outlineWidth: 3,
        style: C.LabelStyle.FILL_AND_OUTLINE, distanceDisplayCondition: new C.DistanceDisplayCondition(0, 4500),
        disableDepthTestDistance: Number.POSITIVE_INFINITY });
    } else if (p.layer === "sensor") {
      S.reg[p.id] = p;
      S.sensors[p.id] = sensorPts.add({ position: C.Cartesian3.fromDegrees(g.coordinates[0], g.coordinates[1], 25), pixelSize: 9,
        color: C.Color.fromCssColorString(COL.ok), outlineColor: C.Color.BLACK, outlineWidth: 1.5, id: { sensor: p.id },
        disableDepthTestDistance: Number.POSITIVE_INFINITY });
    }
  }
  const flat = (inst) => new C.Primitive({ geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ flat: true }), asynchronous: false });
  scene.primitives.add(flat(wInst));
  scene.primitives.add(new C.GroundPolylinePrimitive({ geometryInstances: rInst, appearance: new C.PolylineColorAppearance(), asynchronous: false }));
  scene.primitives.add(new C.Primitive({ geometryInstances: bInst, appearance: new C.PerInstanceColorAppearance({ translucent: false }), asynchronous: false }));
  S.worldLoaded = gj.features.length;
}

function sensorColor(id) {
  const st = S.state[id];
  return COL[st || "ok"];
}
function paintSensors() {
  for (const id in S.sensors) S.sensors[id].color = C.Color.fromCssColorString(sensorColor(id));
}

function fmtClock(t) {
  const m = Math.floor(t / 60) + 360, h = Math.floor(m / 60) % 24;
  return `${String(h).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

function renderAlerts() {
  const open = Object.values(S.alerts).filter((a) => !a.closed && a.detector !== "naive");
  open.sort((a, b) => (a.kind === "event" ? 0 : 1) - (b.kind === "event" ? 0 : 1) || b.t_open - a.t_open);
  const el = document.getElementById("alerts");
  if (!open.length) { el.innerHTML = '<div class="dim">None open.</div>'; return; }
  el.innerHTML = open.map((a) => {
    const imp = a.impacts ? `<div class="imp">Inside footprint: ${a.impacts.buildings} buildings, ${a.impacts.road_km} km of road${a.impacts.assets.length ? ", " + a.impacts.assets.map((x) => x.name).join(", ") : ""}</div>` : "";
    const title = a.kind === "event" ? a.cls.replace("_", " ") : a.kind === "fault" ? `sensor fault: ${a.cls}` : "unexplained";
    return `<div class="alert ${a.kind}"><div class="row"><b>${title}</b><span class="dim">${fmtClock(a.t_open)}</span></div>
      <div class="dim">${a.reason}</div><div>${a.sensors.slice(0, 8).join(", ")}${a.sensors.length > 8 ? " +" + (a.sensors.length - 8) : ""}</div>${imp}</div>`;
  }).join("");
}

function drawFootprint(a) {
  footprints.entities.removeById(a.id);
  if (a.kind !== "event" || a.closed) return;
  const col = C.Color.fromCssColorString(EVC[a.cls] || "#b98cff");
  footprints.entities.add({ id: a.id, position: C.Cartesian3.fromDegrees(a.lon, a.lat),
    ellipse: { semiMajorAxis: Math.max(a.radius, 150), semiMinorAxis: Math.max(a.radius, 150), material: col.withAlpha(0.18),
      outline: true, outlineColor: col, height: 1 },
    label: { text: a.cls.replace("_", " ").toUpperCase(), font: "bold 14px system-ui", fillColor: col, outlineColor: C.Color.BLACK,
      outlineWidth: 4, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -20),
      disableDepthTestDistance: Number.POSITIVE_INFINITY, heightReference: C.HeightReference.NONE },
  });
}

function recomputeStates() {
  const st = {};
  for (const a of Object.values(S.alerts)) {
    if (a.closed || a.detector === "naive") continue;
    for (const s of a.sensors) {
      const k = a.kind === "event" ? (a.cls === "outage" && !s.startsWith("pl-") ? "silent" : "event") : a.kind;
      if (!st[s] || k === "fault") st[s] = k;
    }
  }
  S.state = st;
  paintSensors();
}

function onAlert(a) {
  if (a.detector === "naive") return;
  if (a.op === "close") { delete S.alerts[a.id]; footprints.entities.removeById(a.id); }
  else S.alerts[a.id] = Object.assign(S.alerts[a.id] || {}, a);
  if (a.op !== "close") drawFootprint(S.alerts[a.id]);
  recomputeStates(); renderAlerts();
}

const ENT_COL = { car: "#e8eaed", emergency: "#ff4d4d", uav: "#ffe14d", robot: "#4dd9ff", truck: "#e8eaed" };
function onEntities(m) {
  const seen = new Set();
  for (const [site, id, kind, mark, lat, lon, alt] of m.e) {
    const k = `${site}-${id}`; seen.add(k);
    const pos = C.Cartesian3.fromDegrees(lon, lat, Math.max(alt, 3) + 2);
    let p = S.ent[k];
    if (!p) {
      p = S.ent[k] = entPts.add({ position: pos, pixelSize: site === 1 ? 5 : 11, color: C.Color.fromCssColorString(ENT_COL[kind] || "#fff"),
        outlineColor: C.Color.BLACK, outlineWidth: site === 1 ? 0 : 2, disableDepthTestDistance: Number.POSITIVE_INFINITY });
      if (site !== 1) p.label = labels.add({ position: pos, text: mark, font: "11px system-ui", fillColor: C.Color.WHITE,
        outlineColor: C.Color.BLACK, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -16),
        disableDepthTestDistance: Number.POSITIVE_INFINITY });
    }
    p.position = pos; if (p.label) p.label.position = pos;
  }
  for (const k in S.ent) if (!seen.has(k)) { entPts.remove(S.ent[k]); if (S.ent[k].label) labels.remove(S.ent[k].label); delete S.ent[k]; }
  document.getElementById("nent").textContent = m.e.length;
}

function onTruth(m) {
  S.truth = m;
  truthDs.entities.removeAll();
  if (!document.getElementById("truth").checked) return;
  for (const ring of m.flood || []) {
    truthDs.entities.add({ polygon: { hierarchy: C.Cartesian3.fromDegreesArray(ring.flat()), material: C.Color.fromCssColorString("#3fa7ff").withAlpha(0.35),
      height: 0.5, outline: true, outlineColor: C.Color.fromCssColorString("#9fd3ff") } });
  }
  for (const e of m.events) {
    if (e.kind === "plume" && e.lat) {
      const len = 0.012, r = C.Math.toRadians(e.toward_deg);
      truthDs.entities.add({ polyline: { positions: C.Cartesian3.fromDegreesArrayHeights([e.lon, e.lat, 40,
        e.lon + Math.sin(r) * len / Math.cos(C.Math.toRadians(e.lat)), e.lat + Math.cos(r) * len, 40]), width: 10,
        material: new C.PolylineArrowMaterialProperty(C.Color.fromCssColorString("#c58bff").withAlpha(0.8)) } });
    }
  }
}

let ws;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    S.msgs++;
    if (m.type === "sensor") {
      if (m.ts) { const l = (Date.now() / 1000 + S.clockOffset - m.ts) * 1000; S.lat.push(l); if (S.lat.length > 4000) S.lat.shift(); }
      S.last = S.last || {}; S.last[m.id] = m;
    } else if (m.type === "alert") onAlert(m);
    else if (m.type === "impacts") { if (S.alerts[m.id]) { S.alerts[m.id].impacts = m.impacts; renderAlerts(); } }
    else if (m.type === "entities") { onEntities(m); document.getElementById("clock").textContent = fmtClock(m.t); }
    else if (m.type === "truth") onTruth(m);
    else if (m.type === "hello") { for (const a of m.alerts) onAlert(Object.assign({ op: "open", detector: "twin" }, a)); }
    else if (m.type === "registered") addHardware(m.sensor);
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

function addHardware(r) {
  if (S.sensors[r.id]) return;
  const [lat, lon] = r.latlon; S.reg[r.id] = r;
  S.sensors[r.id] = sensorPts.add({ position: C.Cartesian3.fromDegrees(lon, lat, 25), pixelSize: 13, color: C.Color.fromCssColorString("#4dd9ff"),
    outlineColor: C.Color.WHITE, outlineWidth: 2, id: { sensor: r.id }, disableDepthTestDistance: Number.POSITIVE_INFINITY });
}

async function syncClock() {
  const best = [];
  for (let i = 0; i < 5; i++) {
    const t0 = Date.now() / 1000; const r = await (await fetch("/api/time")).json(); const t1 = Date.now() / 1000;
    best.push([t1 - t0, r.t - (t0 + t1) / 2]);
  }
  best.sort((a, b) => a[0] - b[0]); S.clockOffset = best[0][1]; S.clockRtt = best[0][0];
}

setInterval(() => {
  const now = performance.now(), dt = (now - S.lastRate) / 1000;
  document.getElementById("rate").textContent = `${Math.round(S.msgs / dt)} msg/s`;
  S.msgs = 0; S.lastRate = now;
  if (S.lat.length) { const s = [...S.lat].sort((a, b) => a - b); document.getElementById("lat").textContent = `sensor to screen p50 ${s[Math.floor(s.length / 2)].toFixed(0)} ms`; }
}, 2000);

// hover: sensor details
const handler = new C.ScreenSpaceEventHandler(scene.canvas);
const tip = document.getElementById("tip");
handler.setInputAction((mv) => {
  const o = scene.pick(mv.endPosition);
  if (o && o.id && o.id.sensor) {
    const id = o.id.sensor, r = S.reg[id], l = (S.last || {})[id];
    const v = l ? `${(+l.v).toFixed(r.type === "water_level" ? 3 : 1)} ${r.unit}` : "no data";
    tip.style.display = "block"; tip.style.left = mv.endPosition.x + 14 + "px"; tip.style.top = mv.endPosition.y + 8 + "px";
    tip.innerHTML = `<b>${id}</b> ${r.type.replace("_", " ")}<br>${v}<br><span class="dim">${S.state[id] || "normal"}</span>`;
    document.getElementById("sensor").innerHTML = tip.innerHTML;
  } else tip.style.display = "none";
}, C.ScreenSpaceEventType.MOUSE_MOVE);
handler.setInputAction((cl) => {
  const c = viewer.camera.pickEllipsoid(cl.position, scene.globe.ellipsoid);
  if (!c) return;
  const g = C.Cartographic.fromCartesian(c);
  S.pick = [C.Math.toDegrees(g.latitude), C.Math.toDegrees(g.longitude)];
  document.getElementById("pick").textContent = `Inject at: ${S.pick[0].toFixed(5)}, ${S.pick[1].toFixed(5)}`;
}, C.ScreenSpaceEventType.LEFT_CLICK);

function pickPoint() {
  if (S.pick) return S.pick;
  const c = viewer.camera.pickEllipsoid(new C.Cartesian2(scene.canvas.clientWidth / 2, scene.canvas.clientHeight / 2));
  const g = C.Cartographic.fromCartesian(c); return [C.Math.toDegrees(g.latitude), C.Math.toDegrees(g.longitude)];
}
document.querySelectorAll("[data-inj]").forEach((b) => b.addEventListener("click", async () => {
  const k = b.dataset.inj, [lat, lon] = pickPoint();
  let body;
  if (k === "flood" || k === "plume") body = { op: "event", kind: k, lat, lon, duration: 2400 };
  else if (k === "storm") body = { op: "event", kind: "storm", duration: 2400 };
  else if (k === "outage") body = { op: "event", kind: "outage", duration: 1200, params: { substation: "substation-1416868021", rise: 0, fall: 0 } };
  else {
    let best = null, bd = 1e9;
    for (const id in S.reg) { const r = S.reg[id]; const p = S.sensors[id]; if (!p) continue;
      const g = C.Cartographic.fromCartesian(p.position); const d = Math.hypot(C.Math.toDegrees(g.latitude) - lat, C.Math.toDegrees(g.longitude) - lon);
      if (d < bd) { bd = d; best = id; } }
    body = { op: "fault", sensor: best, kind: "stuck", duration: 1800 };
  }
  await fetch("/api/inject", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}));
document.getElementById("truth").addEventListener("change", () => S.truth && onTruth(S.truth));
document.getElementById("reset").addEventListener("click", () => home(1.2));

home();
loadWorld().then(() => { syncClock(); connect(); });
