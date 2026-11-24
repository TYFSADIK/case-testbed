"""The synthetic environment's world, built from OpenStreetMap data.

Everything here is deterministic: the same map extract and seed always give
the same buildings, road graph, assets and sensor layout, so every federate
(site A, site B, the backend, the evaluation tools) agrees on the world
without exchanging it.

Map data (c) OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import gzip
import json
import math
import random
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import networkx as nx
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union

from .geo import ORIGIN, dist, to_latlon, to_local

DATA = Path(__file__).resolve().parent.parent / "world" / "data" / "barrie-osm.json.gz"
BBOX = (44.372, -79.705, 44.398, -79.665)            # south, west, north, east (sensor area)

SPEED = {   # m/s by road class (posted limits, roughly)
    "motorway": 27.8, "motorway_link": 16.7, "trunk": 22.2, "primary": 16.7, "primary_link": 13.9,
    "secondary": 13.9, "secondary_link": 11.1, "tertiary": 13.9, "unclassified": 11.1,
    "residential": 11.1, "service": 5.6,
}
MAJOR = {"motorway", "trunk", "primary", "secondary", "tertiary"}


@dataclass
class Building:
    id: int
    poly: Polygon               # local metres
    height: float
    kind: str


@dataclass
class Asset:
    id: str
    kind: str                   # hospital | fire_station | police | school | substation | wastewater_plant
    name: str
    xy: tuple[float, float]
    poly: Polygon | None = None


@dataclass
class Sensor:
    id: str
    type: str                   # water_level | air_quality | traffic | bridge_strain | weather | power_load
    xy: tuple[float, float]
    unit: str
    period: float = 15.0        # seconds of simulated time between samples
    power: str = "mains"        # mains | solar
    substation: str | None = None
    edge: tuple | None = None   # road edge, for traffic and bridge sensors
    meta: dict = field(default_factory=dict)

    @property
    def latlon(self):
        return to_latlon(*self.xy)


@dataclass
class World:
    buildings: list[Building]
    roads: nx.DiGraph
    road_lines: list[dict]
    water: Polygon
    streams: list[LineString]
    assets: list[Asset]
    sensors: list[Sensor]
    bridges: list[dict]
    area: Polygon

    def sensor(self, sid: str) -> Sensor:
        return self._by_id[sid]

    def __post_init__(self):
        self._by_id = {s.id: s for s in self.sensors}


# ---------------------------------------------------------------- parsing

def _height(tags: dict) -> float:
    if "height" in tags:
        try:
            return float(str(tags["height"]).split()[0])
        except ValueError:
            pass
    if "building:levels" in tags:
        try:
            return 3.2 * float(tags["building:levels"]) + 1.0
        except ValueError:
            pass
    kind = tags.get("building", "yes")
    return {"house": 7.0, "detached": 7.0, "garage": 3.0, "shed": 3.0, "apartments": 15.0,
            "commercial": 10.0, "retail": 7.0, "industrial": 9.0, "church": 14.0}.get(kind, 8.0)


@lru_cache(maxsize=2)
def load(path: str = str(DATA), seed: int = 7) -> World:
    raw = json.load(gzip.open(path))["elements"]
    nodes = {e["id"]: to_local(e["lat"], e["lon"]) for e in raw if e["type"] == "node"}
    ways = {e["id"]: e for e in raw if e["type"] == "way"}
    area = box(*to_local(BBOX[0], BBOX[1]), *to_local(BBOX[2], BBOX[3]))
    area = Polygon([(area.bounds[0], area.bounds[1]), (area.bounds[2], area.bounds[1]),
                    (area.bounds[2], area.bounds[3]), (area.bounds[0], area.bounds[3])])

    def coords(w):
        return [nodes[n] for n in w["nodes"] if n in nodes]

    # water: Lake Simcoe multipolygon (outer rings) plus mapped ponds
    lines = []
    for e in raw:
        if e["type"] == "relation" and e.get("tags", {}).get("natural") == "water":
            for m in e["members"]:
                if m["type"] == "way" and m.get("role") in ("outer", "") and m["ref"] in ways:
                    c = coords(ways[m["ref"]])
                    if len(c) >= 2:
                        lines.append(LineString(c))
    polys = list(polygonize(unary_union(lines))) if lines else []
    for w in ways.values():
        t = w.get("tags", {})
        if t.get("natural") == "water":
            c = coords(w)
            if len(c) >= 4 and c[0] == c[-1]:
                polys.append(Polygon(c))
    big = area.buffer(1500)
    water = unary_union([p.buffer(0) for p in polys]).intersection(big)

    buildings, road_lines, bridges, streams, assets = [], [], [], [], []
    g = nx.DiGraph()
    for w in ways.values():
        t = w.get("tags", {})
        c = coords(w)
        if len(c) < 2:
            continue
        if "building" in t and len(c) >= 4:
            p = Polygon(c)
            if p.is_valid and p.area > 12 and area.contains(p.centroid):
                buildings.append(Building(w["id"], p, _height(t), t["building"]))
        hw = t.get("highway")
        if hw in SPEED:
            road_lines.append({"id": w["id"], "kind": hw, "name": t.get("name", ""),
                               "bridge": t.get("bridge") == "yes", "coords": c})
            oneway = t.get("oneway") in ("yes", "1", "true") or hw in ("motorway", "motorway_link")
            ns = [n for n in w["nodes"] if n in nodes]
            for a, b in zip(ns, ns[1:]):
                length = dist(nodes[a], nodes[b])
                if length < 0.5:
                    continue
                attrs = dict(length=length, speed=SPEED[hw], kind=hw, way=w["id"],
                             bridge=t.get("bridge") == "yes", name=t.get("name", ""))
                g.add_edge(a, b, **attrs)
                if not oneway:
                    g.add_edge(b, a, **attrs)
            if t.get("bridge") == "yes":
                bridges.append({"id": w["id"], "kind": hw, "name": t.get("name", ""), "coords": c})
        if t.get("waterway") in ("stream", "river", "canal"):
            streams.append(LineString(c))
        kind = t.get("amenity") or t.get("power") or t.get("man_made")
        if kind in ("hospital", "fire_station", "police", "school", "substation", "wastewater_plant"):
            p = Polygon(c) if len(c) >= 4 and c[0] == c[-1] else None
            xy = (p.centroid.x, p.centroid.y) if p else c[0]
            if area.buffer(300).contains(Point(xy)):
                assets.append(Asset(f"{kind}-{w['id']}", kind, t.get("name", kind.replace('_', ' ').title()), xy, p))
    for e in raw:
        t = e.get("tags", {})
        if e["type"] == "node" and t.get("amenity") in ("hospital", "fire_station", "police"):
            xy = nodes[e["id"]]
            if area.buffer(300).contains(Point(xy)):
                assets.append(Asset(f"{t['amenity']}-{e['id']}", t["amenity"], t.get("name", t["amenity"]), xy))
    for g_n, (x, y) in nodes.items():
        if g_n in g:
            g.nodes[g_n]["xy"] = (x, y)

    # keep the largest strongly connected part so every trip has a route
    comp = max(nx.strongly_connected_components(g), key=len)
    g = g.subgraph(comp).copy()
    assets.sort(key=lambda a: a.id)
    buildings.sort(key=lambda b: b.id)

    sensors = _place_sensors(g, water, streams, area, assets, buildings, seed)
    return World(buildings, g, road_lines, water, streams, assets, sensors, bridges, area)


# ---------------------------------------------------------------- sensors

def _farthest(points, k, rng):
    """Spread k sensors out: greedy farthest-point sampling."""
    if not points:
        return []
    pts = list(points)
    chosen = [pts[rng.randrange(len(pts))]]
    while len(chosen) < min(k, len(pts)):
        best = max(pts, key=lambda p: min(dist(p[0], c[0]) for c in chosen))
        chosen.append(best)
    return chosen


def nearest_substation(xy, subs):
    return min(subs, key=lambda s: dist(xy, s.xy)).id if subs else None


def _place_sensors(g, water, streams, area, assets, buildings, seed) -> list[Sensor]:
    rng = random.Random(seed)
    subs = [a for a in assets if a.kind == "substation"]
    out: list[Sensor] = []
    land = area.difference(water)

    # water level: along the shoreline inside the area, ~280 m apart, plus the creek
    shore = water.boundary.intersection(area.buffer(-40))
    shore_pts = []
    for part in getattr(shore, "geoms", [shore]):
        if part.length < 50:
            continue
        n = int(part.length // 280)
        for i in range(n + 1):
            p = part.interpolate(i / max(n, 1), normalized=True)
            shore_pts.append(((p.x, p.y), None))
    for i, (xy, _) in enumerate(_farthest(shore_pts, 12, rng)):
        out.append(Sensor(f"wl-{i+1:02d}", "water_level", xy, "m", power="solar"))
    creek = [s for s in streams if s.intersects(area)]
    creek_pts = []
    for s in creek:
        s = s.intersection(area)
        for part in getattr(s, "geoms", [s]):
            if part.length > 100:
                creek_pts.append(((part.interpolate(0.5, normalized=True).x, part.interpolate(0.5, normalized=True).y), None))
    for i, (xy, _) in enumerate(_farthest(creek_pts, 4, rng)):
        out.append(Sensor(f"wl-c{i+1}", "water_level", xy, "m", power="solar", meta={"creek": True}))

    # air quality: a 450 m grid over land
    minx, miny, maxx, maxy = area.bounds
    aq = []
    y = miny + 200
    while y < maxy:
        x = minx + 200
        while x < maxx:
            if land.contains(Point(x, y)):
                aq.append((x + rng.uniform(-60, 60), y + rng.uniform(-60, 60)))
            x += 450
        y += 450
    for i, xy in enumerate(aq):
        out.append(Sensor(f"aq-{i+1:02d}", "air_quality", xy, "ug/m3"))

    # traffic counters: on major road edges, spread out
    cands = []
    for u, v, d in g.edges(data=True):
        if d["kind"] in MAJOR and d["length"] > 25 and not d["bridge"]:
            a, b = g.nodes[u]["xy"], g.nodes[v]["xy"]
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            if area.contains(Point(mid)):
                cands.append((mid, (u, v)))
    cands.sort(key=lambda c: (round(c[0][0]), round(c[0][1])))
    for i, (xy, e) in enumerate(_farthest(cands, 40, rng)):
        out.append(Sensor(f"tc-{i+1:02d}", "traffic", xy, "veh/15s", edge=e))

    # bridge strain: one per bridge carrying a road edge in the graph
    seen = set()
    k = 0
    for u, v, d in sorted(g.edges(data=True), key=lambda e: (e[2]["way"], e[0], e[1])):
        if d["bridge"] and d["way"] not in seen:
            a, b = g.nodes[u]["xy"], g.nodes[v]["xy"]
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            if area.contains(Point(mid)):
                seen.add(d["way"])
                k += 1
                out.append(Sensor(f"br-{k:02d}", "bridge_strain", mid, "microstrain", edge=(u, v),
                                  meta={"way": d["way"], "name": d["name"]}))

    # weather: four stations at civic buildings, spread out
    civic = [(a.xy, a.id) for a in assets if a.kind in ("school", "fire_station", "police", "hospital", "wastewater_plant")]
    for i, (xy, aid) in enumerate(_farthest(civic, 4, rng)):
        out.append(Sensor(f"wx-{i+1}", "weather", xy, "mixed", power="solar", meta={"asset": aid}))

    # power load at each substation
    for s in subs:
        out.append(Sensor(f"pl-{s.id.split('-')[-1]}", "power_load", s.xy, "MW", meta={"asset": s.id}))

    for s in out:
        if s.power == "mains":
            s.substation = nearest_substation(s.xy, subs)
    return out


def building_loads(world: World) -> dict[str, float]:
    """Peak electrical load (MW) served by each substation, from floor area."""
    subs = [a for a in world.assets if a.kind == "substation"]
    load = {s.id: 0.0 for s in subs}
    for b in world.buildings:
        floors = max(1.0, round(b.height / 3.2))
        mw = b.poly.area * floors * 25e-6          # ~25 W per m2 of floor at peak
        load[nearest_substation((b.poly.centroid.x, b.poly.centroid.y), subs)] += mw
    return load


def world_summary(world: World) -> dict:
    from collections import Counter
    return {
        "buildings": len(world.buildings),
        "road_ways": len(world.road_lines),
        "road_graph": {"nodes": world.roads.number_of_nodes(), "edges": world.roads.number_of_edges(),
                       "km": round(sum(d["length"] for *_, d in world.roads.edges(data=True)) / 2000, 1)},
        "bridges": len({d["way"] for *_, d in world.roads.edges(data=True) if d["bridge"]}),
        "assets": dict(Counter(a.kind for a in world.assets)),
        "sensors": dict(Counter(s.type for s in world.sensors)),
        "sensor_total": len(world.sensors),
        "water_km2": round(world.water.intersection(world.area).area / 1e6, 2),
        "area_km2": round(world.area.area / 1e6, 2),
        "origin": ORIGIN,
    }


def geojson(world: World) -> dict:
    """The world as GeoJSON (lon/lat) for the 3D viewer and PostGIS."""
    def ring(poly):
        return [[round(v, 7) for v in to_latlon(x, y)[::-1]] for x, y in poly.exterior.coords]

    feats = []
    for b in world.buildings:
        feats.append({"type": "Feature", "properties": {"layer": "building", "id": b.id, "h": round(b.height, 1), "kind": b.kind},
                      "geometry": {"type": "Polygon", "coordinates": [ring(b.poly)]}})
    for r in world.road_lines:
        feats.append({"type": "Feature", "properties": {"layer": "road", "id": r["id"], "kind": r["kind"], "name": r["name"], "bridge": r["bridge"]},
                      "geometry": {"type": "LineString", "coordinates": [[round(v, 7) for v in to_latlon(*c)[::-1]] for c in r["coords"]]}})
    w = world.water.intersection(world.area.buffer(1200))
    for p in getattr(w, "geoms", [w]):
        if p.geom_type == "Polygon" and p.area > 500:
            feats.append({"type": "Feature", "properties": {"layer": "water"},
                          "geometry": {"type": "Polygon", "coordinates": [ring(p.simplify(3))]}})
    for a in world.assets:
        feats.append({"type": "Feature", "properties": {"layer": "asset", "id": a.id, "kind": a.kind, "name": a.name},
                      "geometry": {"type": "Point", "coordinates": list(to_latlon(*a.xy)[::-1])}})
    for s in world.sensors:
        feats.append({"type": "Feature", "properties": {"layer": "sensor", "id": s.id, "type": s.type, "unit": s.unit,
                                                        "power": s.power, "substation": s.substation},
                      "geometry": {"type": "Point", "coordinates": list(s.latlon[::-1])}})
    return {"type": "FeatureCollection", "features": feats}


def angle(a, b) -> float:
    return math.atan2(b[1] - a[1], b[0] - a[0])
