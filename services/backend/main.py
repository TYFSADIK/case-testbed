"""CASE twin backend: ingest, record, detect, store, serve.

Inputs   MQTT telemetry (case/sensor/#, case/clock, case/registry/#), DIS entity
         state (UDP 3000), instructor injects (POST /api/inject).
Order    Every input is recorded (hash-chained) and then processed by one
         worker in arrival order, so a recording replays to the same alerts.
Storage  PostGIS: the world (buildings, roads, water, assets, sensors),
         telemetry and alerts. Event footprints are intersected with assets
         and roads in PostGIS to say what an event threatens.
Output   WebSocket /ws for the 3D viewer, REST under /api.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import socket
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import paho.mqtt.client as mqtt
import psycopg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from case import dis                                                  # noqa: E402
from case.engine import sensor_registry                               # noqa: E402
from case.geo import to_latlon, to_local                              # noqa: E402
from case.pipeline import EPOCH0, Pipeline, Recorder                  # noqa: E402
from case.world import geojson, load, world_summary                   # noqa: E402

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
DB = os.environ.get("DATABASE_URL", "postgresql://twin:twin@postgis/twin")
DATA = Path(os.environ.get("DATA_DIR", "/data"))
STATIC = Path(__file__).resolve().parent / "static"

world = load()
registry = sensor_registry(world)
pipe = Pipeline(registry)
inbox: queue.Queue = queue.Queue(maxsize=200000)
(DATA / "rec").mkdir(parents=True, exist_ok=True)
(DATA / "dr").mkdir(parents=True, exist_ok=True)
recorder = Recorder(DATA / "rec" / f"session-{int(time.time())}.jsonl")
clients: set = set()
loop_ref: dict = {}
stats = {"started": time.time(), "lat_ms": deque(maxlen=5000), "processed": 0, "dropped": 0,
         "db_rows": 0, "ws_sent": 0}
dr_log = {"label": None, "rows": [], "file": None}
db_q: queue.Queue = queue.Queue()
mq: mqtt.Client | None = None
geo_world = geojson(world)


# ------------------------------------------------------------------ database

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS building (id bigint PRIMARY KEY, kind text, height real, geom geometry(Polygon,4326));
CREATE TABLE IF NOT EXISTS road (id bigint PRIMARY KEY, kind text, name text, bridge bool, geom geometry(LineString,4326));
CREATE TABLE IF NOT EXISTS water (id serial PRIMARY KEY, geom geometry(Polygon,4326));
CREATE TABLE IF NOT EXISTS asset (id text PRIMARY KEY, kind text, name text, geom geometry(Point,4326));
CREATE TABLE IF NOT EXISTS sensor (id text PRIMARY KEY, type text, unit text, power text, substation text,
                                   hardware bool DEFAULT false, geom geometry(Point,4326));
CREATE TABLE IF NOT EXISTS telemetry (sensor_id text, t double precision, v double precision, ts double precision,
                                      t_recv double precision, extra jsonb);
CREATE INDEX IF NOT EXISTS telemetry_sensor_t ON telemetry (sensor_id, t);
CREATE TABLE IF NOT EXISTS alert_log (id serial PRIMARY KEY, alert_id text, op text, kind text, cls text, detector text,
                                      t double precision, sensors text[], reason text, detail jsonb,
                                      footprint geometry(Geometry,4326), impacts jsonb);
CREATE INDEX IF NOT EXISTS building_geom ON building USING gist (geom);
CREATE TABLE IF NOT EXISTS shore_band (geom geometry(Geometry,4326));
CREATE INDEX IF NOT EXISTS road_geom ON road USING gist (geom);
CREATE INDEX IF NOT EXISTS asset_geom ON asset USING gist (geom);
CREATE INDEX IF NOT EXISTS sensor_geom ON sensor USING gist (geom);
"""


def db_connect():
    for _ in range(60):
        try:
            return psycopg.connect(DB, autocommit=True)
        except psycopg.OperationalError:
            time.sleep(1)
    raise SystemExit("database not reachable")


def load_world_into_db(conn):
    conn.execute(SCHEMA)
    if conn.execute("SELECT count(*) FROM building").fetchone()[0]:
        return
    _load_features(conn)
    # land within 200 m of the water: where a flood can reach first
    conn.execute("""INSERT INTO shore_band
                    SELECT ST_Difference(ST_Buffer(ST_Union(geom)::geography, 200)::geometry, ST_Union(geom)) FROM water""")
    conn.execute("CREATE INDEX IF NOT EXISTS shore_band_geom ON shore_band USING gist (geom)")


def _load_features(conn):
    with conn.cursor() as cur:
        for f in geo_world["features"]:
            p, g = f["properties"], json.dumps(f["geometry"])
            layer = p["layer"]
            if layer == "building":
                cur.execute("INSERT INTO building VALUES (%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) ON CONFLICT DO NOTHING",
                            (p["id"], p["kind"], p["h"], g))
            elif layer == "road":
                cur.execute("INSERT INTO road VALUES (%s,%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) ON CONFLICT DO NOTHING",
                            (p["id"], p["kind"], p["name"], p["bridge"], g))
            elif layer == "water":
                cur.execute("INSERT INTO water (geom) VALUES (ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)))", (g,))
            elif layer == "asset":
                cur.execute("INSERT INTO asset VALUES (%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) ON CONFLICT DO NOTHING",
                            (p["id"], p["kind"], p["name"], g))
            elif layer == "sensor":
                cur.execute("INSERT INTO sensor (id,type,unit,power,substation,geom) VALUES (%s,%s,%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) ON CONFLICT DO NOTHING",
                            (p["id"], p["type"], p["unit"], p["power"], p["substation"], g))


def impacts(conn, lat, lon, radius, cls="", alert_id=""):
    """What an event threatens.
    flood   the shoreline band (land within 200 m of the water) inside the evidence circle
    outage  everything whose nearest substation is the failed one
    other   the evidence circle"""
    if cls == "outage" and alert_id.startswith("outage:"):
        sub = alert_id.split(":", 1)[1]
        row = conn.execute("""
            WITH served AS (SELECT b.geom FROM building b
                            WHERE (SELECT a.id FROM asset a WHERE a.kind = 'substation' ORDER BY a.geom <-> b.geom LIMIT 1) = %s),
                 area AS (SELECT ST_ConvexHull(ST_Collect(geom)) AS g FROM served)
            SELECT (SELECT coalesce(json_agg(json_build_object('id', x.id, 'kind', x.kind, 'name', x.name)), '[]') FROM asset x
                    WHERE x.kind <> 'substation' AND (SELECT a.id FROM asset a WHERE a.kind = 'substation' ORDER BY a.geom <-> x.geom LIMIT 1) = %s),
                   (SELECT coalesce(round(sum(ST_Length(ST_Intersection(r.geom, area.g)::geography))::numeric / 1000, 2), 0) FROM road r, area WHERE ST_Intersects(r.geom, area.g)),
                   (SELECT count(*) FROM served),
                   (SELECT ST_AsGeoJSON(g) FROM area)""", (sub, sub)).fetchone()
        return {"assets": row[0], "road_km": float(row[1]), "buildings": row[2], "basis": "served by " + sub}, row[3]
    band = cls == "flood"
    row = conn.execute("""
        WITH c AS (SELECT ST_Buffer(ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s)::geometry AS g),
             f AS (SELECT CASE WHEN %s THEN ST_Intersection(c.g, (SELECT ST_Union(geom) FROM shore_band)) ELSE c.g END AS g FROM c)
        SELECT (SELECT coalesce(json_agg(json_build_object('id', a.id, 'kind', a.kind, 'name', a.name)), '[]')
                  FROM asset a, f WHERE ST_Intersects(a.geom, f.g)),
               (SELECT coalesce(round(sum(ST_Length(ST_Intersection(r.geom, f.g)::geography))::numeric / 1000, 2), 0)
                  FROM road r, f WHERE ST_Intersects(r.geom, f.g)),
               (SELECT count(*) FROM building b, f WHERE ST_Intersects(b.geom, f.g)),
               (SELECT ST_AsGeoJSON(ST_Envelope(f.g)) FROM f)
    """, (lon, lat, radius, band)).fetchone()
    return {"assets": row[0], "road_km": float(row[1]), "buildings": row[2],
            "basis": "shoreline band inside the evidence circle" if band else "evidence circle"}, row[3]


def db_writer():
    conn = db_connect()
    load_world_into_db(conn)
    ready.set()
    tele = []
    last = time.time()
    while True:
        try:
            item = db_q.get(timeout=0.5)
        except queue.Empty:
            item = None
        if item is not None:
            if item[0] == "tele":
                tele.append(item[1])
            elif item[0] == "alert":
                a = item[1]
                footprint, imp = None, None
                if a["kind"] == "event" and a["op"] in ("open", "update"):
                    imp, footprint = impacts(conn, a["lat"], a["lon"], max(a.get("radius", 0), 150), a["cls"], a["id"])
                    a["impacts"] = imp
                    broadcast({"type": "impacts", "id": a["id"], "impacts": imp})
                conn.execute("""INSERT INTO alert_log (alert_id, op, kind, cls, detector, t, sensors, reason, detail, footprint, impacts)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s::text IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s),4326) END, %s)""",
                             (a["id"], a["op"], a["kind"], a["cls"], a["detector"], a["t"], a["sensors"], a["reason"],
                              json.dumps(a.get("detail", {})), footprint, footprint, json.dumps(imp) if imp else None))
            elif item[0] == "register":
                r = item[1]
                lat, lon = r["latlon"]
                conn.execute("INSERT INTO sensor (id,type,unit,power,hardware,geom) VALUES (%s,%s,%s,%s,true,ST_SetSRID(ST_MakePoint(%s,%s),4326)) ON CONFLICT (id) DO NOTHING",
                             (r["id"], r["type"], r.get("unit", ""), r.get("power", "battery"), lon, lat))
        if tele and (len(tele) >= 500 or time.time() - last > 1.0):
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO telemetry VALUES (%s,%s,%s,%s,%s,%s)", tele)
            stats["db_rows"] += len(tele)
            tele, last = [], time.time()


ready = threading.Event()


# ------------------------------------------------------------------ inputs

def on_message(_c, _u, msg):
    t = time.time()
    try:
        inbox.put_nowait(("mqtt", msg.topic, msg.payload, t))
    except queue.Full:
        stats["dropped"] += 1


def mqtt_thread():
    global mq
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="twin-backend")
    c.on_message = on_message
    c.on_connect = lambda c, *_: c.subscribe([("case/sensor/#", 0), ("case/clock", 0), ("case/registry/#", 1),
                                              ("case/truth/#", 0)])
    while True:
        try:
            c.connect(MQTT_HOST, 1883, 30)
            break
        except OSError:
            time.sleep(1)
    mq = c
    c.loop_forever()


def dis_thread():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    s.bind(("0.0.0.0", 3000))
    while True:
        data, _ = s.recvfrom(2048)
        try:
            inbox.put_nowait(("dis", None, data, time.time()))
        except queue.Full:
            stats["dropped"] += 1


def worker():
    ready.wait()
    while True:
        kind, topic, payload, t_recv = inbox.get()
        if kind == "mqtt" and topic.startswith("case/truth/"):
            # instructor-only ground truth for the viewer overlay: not recorded, never reaches the detector
            try:
                broadcast({"type": "truth", **json.loads(payload)})
            except ValueError:
                pass
            continue
        recorder.write(kind, topic, payload, t_recv)
        outs = pipe.process(kind, topic, payload, t_recv)
        stats["processed"] += 1
        for o in outs:
            typ = o["type"]
            if typ == "sensor":
                if "ts" in o:
                    stats["lat_ms"].append((o["t_recv"] - o["ts"]) * 1000)
                db_q.put(("tele", (o["id"], o["t"], o["v"], o.get("ts"), o["t_recv"],
                                   json.dumps({k: v for k, v in o.items() if k in ("rain_mmh", "wind_ms", "wind_to_deg")}) or None)))
                broadcast({"type": "sensor", "id": o["id"], "v": o["v"], "t": o["t"], "ts": o.get("ts"),
                           **{k: o[k] for k in ("rain_mmh", "wind_ms", "wind_to_deg") if k in o}})
            elif typ == "alert":
                recorder.write("alert", None, json.dumps({k: o[k] for k in ("op", "id", "cls", "t", "sensors", "detector")},
                                                         sort_keys=True).encode(), t_recv)
                db_q.put(("alert", o))
                broadcast(o)
            elif typ == "registered":
                db_q.put(("register", o["sensor"]))
                broadcast(o)
            elif typ == "clock":
                broadcast(o)


# ------------------------------------------------------------------ outputs

def broadcast(msg: dict):
    loop = loop_ref.get("loop")
    if not loop or not clients:
        return
    data = json.dumps(msg, default=float)
    for ws in list(clients):
        asyncio.run_coroutine_threadsafe(_send(ws, data), loop)


async def _send(ws, data):
    try:
        await ws.send_text(data)
        stats["ws_sent"] += 1
    except Exception:
        clients.discard(ws)


def entity_positions(now_wall):
    """Dead-reckoned position of every entity, now. Site A runs on simulated time."""
    out = []
    for key, (es, t_recv) in list(pipe.entities.items()):
        if key[0] == 1:
            dt = (EPOCH0 + pipe.sim_t) - es.t
            if dt > 30:
                continue
        else:
            dt = now_wall - es.t                      # sender timestamp: clocks are synchronised
            if now_wall - t_recv > 15:
                continue
        x, y = dis.extrapolate(es, max(0.0, min(dt, 10.0)))
        lat, lon = to_latlon(x, y)
        out.append([key[0], key[2], es.kind, es.marking, round(lat, 7), round(lon, 7), round(es.alt, 1),
                    round(math.degrees(math.atan2(es.ve, es.vn)) % 360, 1)])
    return out


def entity_thread():
    """4 Hz entity snapshots for the viewer; 20 Hz dead-reckoning log during DIS experiments."""
    n = 0
    while True:
        time.sleep(0.05)
        now = time.time()
        if dr_log["label"]:
            for key, (es, t_recv) in list(pipe.entities.items()):
                if key[0] == 1:
                    continue
                x0, y0 = to_local(es.lat, es.lon)
                xr, yr = dis.extrapolate(es, now - t_recv)
                xt, yt = dis.extrapolate(es, now - es.t)
                dr_log["rows"].append(f"{now:.4f},{key[0]}-{key[2]},{x0:.3f},{y0:.3f},{xr:.3f},{yr:.3f},{xt:.3f},{yt:.3f}\n")
            if len(dr_log["rows"]) > 2000:
                flush_dr()
        n += 1
        if n % 5 == 0:
            broadcast({"type": "entities", "t": pipe.sim_t, "wall": now, "e": entity_positions(now)})


def flush_dr():
    if dr_log["file"] and dr_log["rows"]:
        with open(dr_log["file"], "a") as f:
            f.writelines(dr_log["rows"])
    dr_log["rows"] = []


@asynccontextmanager
async def lifespan(app):
    loop_ref["loop"] = asyncio.get_running_loop()
    for fn in (db_writer, mqtt_thread, dis_thread, worker, entity_thread):
        threading.Thread(target=fn, daemon=True).start()
    yield


app = FastAPI(title="CASE twin", lifespan=lifespan)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({"type": "hello", "sim_t": pipe.sim_t,
                                   "alerts": [dict(a, lat=to_latlon(*a["xy"])[0], lon=to_latlon(*a["xy"])[1]) for a in pipe.alerts()],
                                   "latest": list(pipe.latest.values())[-500:]}, default=float))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


@app.get("/api/world")
def api_world():
    return JSONResponse(geo_world)


@app.get("/api/summary")
def api_summary():
    return world_summary(world)


@app.get("/api/sensors")
def api_sensors():
    return {"registry": list(pipe.registry.values()), "latest": pipe.latest}


@app.get("/api/alerts")
def api_alerts(detector: str = "twin"):
    return {"open": pipe.alerts(detector), "history": (pipe.det if detector == "twin" else pipe.naive).history}


@app.get("/api/near")
def api_near(lat: float, lon: float, r: float = 500):
    conn = db_connect()
    rows = conn.execute("""SELECT id, type, round(ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography)) d
                           FROM sensor WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s)
                           ORDER BY d""", (lon, lat, lon, lat, r)).fetchall()
    conn.close()
    return [{"id": a, "type": b, "m": float(c)} for a, b, c in rows]


@app.get("/api/stats")
def api_stats():
    lat = sorted(stats["lat_ms"])
    pct = (lambda p: round(lat[min(len(lat) - 1, int(p * len(lat)))], 2)) if lat else (lambda p: None)
    return {"uptime_s": round(time.time() - stats["started"]), "processed": stats["processed"], "dropped": stats["dropped"],
            "queue": inbox.qsize(), "db_rows": stats["db_rows"], "ws_clients": len(clients), "ws_sent": stats["ws_sent"],
            "counts": pipe.counts, "sim_t": pipe.sim_t, "entities": len(pipe.entities),
            "ingest_latency_ms": {"n": len(lat), "p50": pct(0.5), "p95": pct(0.95), "p99": pct(0.99)},
            "recording": {"file": str(recorder.f.name), "records": recorder.seq, "head": recorder.prev}}


@app.post("/api/inject")
async def api_inject(body: dict):
    """Instructor inject: forwarded to the simulation, and recorded in the twin's log."""
    payload = json.dumps(body, sort_keys=True).encode()
    inbox.put(("inject", "case/control", payload, time.time()))
    if mq is not None:
        mq.publish("case/control", payload, qos=1)
    return {"ok": True}


@app.post("/api/dr")
def api_dr(body: dict):
    """Start/stop the dead-reckoning log used by the DIS latency experiment."""
    flush_dr()
    label = body.get("label")
    dr_log["label"] = label
    dr_log["file"] = str(DATA / "dr" / f"{label}.csv") if label else None
    if label:
        Path(dr_log["file"]).write_text("t,entity,hold_x,hold_y,dr_recv_x,dr_recv_y,dr_ts_x,dr_ts_y\n")
    return {"ok": True, "file": dr_log["file"]}


@app.get("/api/time")
def api_time():
    return {"t": time.time()}


@app.get("/api/health")
def health():
    return {"ok": ready.is_set()}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
