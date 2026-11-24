"""Site B federate: a second training site joining over a WAN.

Two kinds of entity, in real time:
  * a survey UAV flying a racetrack over Kempenfelt Bay at 60 m, 18 m/s
  * a three-vehicle emergency convoy driving a loop on the road network,
    braking for corners and stopping at a scene for 40 s each lap

They are published as DIS Entity State PDUs (site 2) to the site gateway,
which forwards them across the WAN to the twin. Ground truth positions are
logged at 20 Hz to /data/dr/truth.csv so the dead-reckoning error the twin
sees can be measured (a measurement harness only; a real site has no such
side channel).
"""
import math
import os
import socket
import sys
import time
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from case import dis                                    # noqa: E402
from case.geo import to_local                           # noqa: E402
from case.world import load                             # noqa: E402

GW = (os.environ.get("GATEWAY", "gateway"), int(os.environ.get("GATEWAY_PORT", "3001")))
THRESH = float(os.environ.get("DR_THRESHOLD", "1.0"))
TRUTH = Path(os.environ.get("TRUTH_DIR", "/data/dr"))
TRUTH.mkdir(parents=True, exist_ok=True)

world = load()
g = world.roads
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sender = dis.Sender(threshold_m=THRESH)


class Uav:
    """Racetrack: two straights joined by 180-degree turns (radius 150 m)."""

    def __init__(self, center, length=900.0, radius=150.0, speed=18.0, heading_deg=60):
        self.c, self.L, self.r, self.v = center, length, radius, speed
        self.h = math.radians(heading_deg)
        self.perim = 2 * length + 2 * math.pi * radius
        self.s = 0.0

    def state(self, t):
        s = (self.v * t) % self.perim
        L, r = self.L, self.r
        if s < L:
            x, y, hd = -L / 2 + s, -r, 0.0
        elif s < L + math.pi * r:
            a = (s - L) / r
            x, y, hd = L / 2 + r * math.sin(a), -r * math.cos(a), a
        elif s < 2 * L + math.pi * r:
            x, y, hd = L / 2 - (s - L - math.pi * r), r, math.pi
        else:
            a = (s - 2 * L - math.pi * r) / r
            x, y, hd = -L / 2 - r * math.sin(a), r * math.cos(a), math.pi + a
        ch, sh = math.cos(self.h), math.sin(self.h)
        X, Y = self.c[0] + x * ch - y * sh, self.c[1] + x * sh + y * ch
        hh = hd + self.h
        return (X, Y), (self.v * math.cos(hh), self.v * math.sin(hh)), hh


class Convoy:
    """Vehicles following one route with a 35 m gap; slow for corners, stop at the scene."""

    def __init__(self, route_nodes, n=3, vmax=16.0):
        pts = [g.nodes[u]["xy"] for u in route_nodes]
        self.pts = pts
        self.cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            self.cum.append(self.cum[-1] + math.dist(a, b))
        self.total = self.cum[-1]
        self.n, self.vmax = n, vmax
        self.s, self.v, self.stop_until, self.last = 0.0, 0.0, 0.0, None
        self.scene = self.total * 0.5

    def _pos(self, s):
        s %= self.total
        i = max(0, min(len(self.cum) - 2, next(k for k in range(len(self.cum) - 1) if self.cum[k + 1] >= s)))
        a, b = self.pts[i], self.pts[i + 1]
        seg = self.cum[i + 1] - self.cum[i]
        f = (s - self.cum[i]) / seg if seg else 0
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f), math.atan2(b[1] - a[1], b[0] - a[0])

    def _turn_ahead(self, s, look=40.0):
        (_, h1), (_, h2) = self._pos(s), self._pos(s + look)
        d = abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi)
        return d

    def step(self, t, dt):
        if t < self.stop_until:
            self.v = 0.0
        else:
            turn = self._turn_ahead(self.s)
            target = self.vmax if turn < 0.3 else max(5.0, self.vmax * (1 - turn / 1.6))
            dist_scene = (self.scene - self.s) % self.total
            if dist_scene < 60 and self.v > 0:
                target = min(target, max(0.0, dist_scene / 5))
                if dist_scene < 2:
                    self.stop_until = t + 40.0
                    self.s += 3.0
            acc = 2.5 if target > self.v else -4.0
            self.v = max(0.0, min(target, self.v + acc * dt)) if target > self.v else max(target, self.v + acc * dt)
        self.s = (self.s + self.v * dt) % self.total
        out = []
        for k in range(self.n):
            (x, y), h = self._pos(self.s - 35.0 * k)
            out.append(((x, y), (self.v * math.cos(h), self.v * math.sin(h)), h))
        return out


def convoy_route():
    fire = next(a.xy for a in world.assets if a.kind == "fire_station")
    shore = world.sensor("wl-10").xy

    def near(xy):
        return min(g.nodes, key=lambda n: math.dist(g.nodes[n]["xy"], xy))

    a, b = near(fire), near(shore)
    w = lambda u, v, d: d["length"] / d["speed"]          # noqa: E731
    return nx.shortest_path(g, a, b, weight=w) + nx.shortest_path(g, b, a, weight=w)[1:]


uav = Uav(to_local(44.3855, -79.6800))
convoy = Convoy(convoy_route())
truth = open(TRUTH / "truth.csv", "a", buffering=1)          # line-buffered: a killed federate loses nothing
if truth.tell() == 0:
    truth.write("t,entity,x,y\n")

t0 = time.time()
last = t0
next_truth = t0
while True:
    now = time.time()
    dt = now - last
    last = now
    ents = []
    xy, vel, hd = uav.state(now - t0)
    ents.append((1, "uav", "UAV-SURVEY", xy, vel, hd, 60.0))
    for k, (xy2, vel2, hd2) in enumerate(convoy.step(now, dt)):
        ents.append((10 + k, "emergency", f"FIRE-{k + 1}", xy2, vel2, hd2, 0.0))
    for eid, kind, mark, p, v, h, alt in ents:
        es = dis.state_from_local(2, 1, eid, kind, mark, p, v, h, now, alt=alt)
        if sender.due(es, now):
            udp.sendto(dis.encode(es), GW)
            sender.mark(es)
    if now >= next_truth:
        truth.write("".join(f"{now:.4f},2-{eid},{p[0]:.3f},{p[1]:.3f}\n" for eid, _, _, p, _, _, _ in ents))
        next_truth = now + 0.05
    time.sleep(0.01)
