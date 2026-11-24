"""Vehicle agents driving on the OpenStreetMap road graph.

Each vehicle picks a random destination, routes by travel time, drives at the
road's speed scaled by its driver factor and the weather, and re-routes when
a road ahead is closed (for example by a flood). Traffic counters and bridge
strain gauges read these agents, so what the sensors report is caused by
simulated traffic, not drawn from a distribution.
"""
from __future__ import annotations

import math
import random
from collections import Counter

import networkx as nx


class Vehicle:
    __slots__ = ("id", "path", "i", "s", "factor", "xy", "heading", "speed", "vel", "trips", "kind")

    def __init__(self, vid: int, factor: float, kind: str = "car"):
        self.id, self.factor, self.kind = vid, factor, kind
        self.path: list = []
        self.i = 0            # index of the current edge's start node in path
        self.s = 0.0          # metres along the current edge
        self.xy = (0.0, 0.0)
        self.heading = 0.0
        self.speed = 0.0
        self.vel = (0.0, 0.0)
        self.trips = 0


class Traffic:
    def __init__(self, g: nx.DiGraph, n: int, seed: int = 11):
        self.g = g
        self.rng = random.Random(seed)
        self.nodes = sorted(g.nodes)
        self.closed: set[tuple] = set()
        self.speed_factor = 1.0
        self.entries: Counter = Counter()      # (u, v) -> vehicles that entered the edge
        self.vehicles = []
        for k in range(n):
            v = Vehicle(k + 1, self.rng.uniform(0.85, 1.05))
            self._new_trip(v, self.rng.choice(self.nodes))
            # start part-way along the first edge so traffic is spread out
            if len(v.path) > 1:
                v.s = self.rng.uniform(0, self._edge(v)["length"])
            self.vehicles.append(v)

    # -- routing
    def _weight(self, u, v, d):
        if (u, v) in self.closed:
            return None
        return d["length"] / d["speed"]

    def _route(self, a, b):
        try:
            return nx.shortest_path(self.g, a, b, weight=self._weight)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def _new_trip(self, v: Vehicle, start):
        for _ in range(20):
            dest = self.rng.choice(self.nodes)
            if dest == start:
                continue
            p = self._route(start, dest)
            if p and len(p) > 3:
                v.path, v.i, v.s = p, 0, 0.0
                v.trips += 1
                self.entries[(p[0], p[1])] += 1
                return
        v.path, v.i, v.s = [start], 0, 0.0      # stuck (isolated by closures): wait

    def _edge(self, v):
        return self.g.edges[v.path[v.i], v.path[v.i + 1]]

    def close(self, edges: set[tuple]):
        """Close roads; vehicles whose remaining route uses one re-route."""
        self.closed = set(edges)
        for v in self.vehicles:
            if len(v.path) < 2:
                continue
            ahead = list(zip(v.path[v.i + 1:], v.path[v.i + 2:]))
            if any(e in self.closed for e in ahead):
                p = self._route(v.path[v.i + 1], v.path[-1])
                if p:
                    v.path = v.path[: v.i + 1] + p
                else:
                    v.path = v.path[: v.i + 2]          # finish this edge, then pick a new trip

    # -- stepping
    def step(self, dt: float):
        g = self.g
        for v in self.vehicles:
            if len(v.path) < 2:
                self._new_trip(v, v.path[0])
                if len(v.path) < 2:
                    continue
            d = self._edge(v)
            speed = d["speed"] * v.factor * self.speed_factor
            v.s += speed * dt
            while v.s >= d["length"]:
                v.s -= d["length"]
                v.i += 1
                if v.i + 1 >= len(v.path):
                    self._new_trip(v, v.path[-1])
                    if len(v.path) < 2:
                        v.s = 0.0
                        break
                else:
                    self.entries[(v.path[v.i], v.path[v.i + 1])] += 1
                d = self._edge(v)
                speed = d["speed"] * v.factor * self.speed_factor
            if len(v.path) < 2:
                v.speed, v.vel = 0.0, (0.0, 0.0)
                continue
            a = g.nodes[v.path[v.i]]["xy"]
            b = g.nodes[v.path[v.i + 1]]["xy"]
            f = v.s / d["length"]
            v.xy = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            v.heading = math.atan2(b[1] - a[1], b[0] - a[0])
            v.speed = speed
            v.vel = (speed * math.cos(v.heading), speed * math.sin(v.heading))

    def take_count(self, edge) -> int:
        """Vehicles that entered this road segment (either direction) since the last read."""
        u, v = edge
        n = self.entries.pop((u, v), 0) + self.entries.pop((v, u), 0)
        return n

    def on_edges(self, edges: set) -> int:
        return sum(1 for v in self.vehicles if len(v.path) > 1 and (v.path[v.i], v.path[v.i + 1]) in edges)
