#!/usr/bin/env python3
"""Sensor-to-subscriber latency: from the moment the site A simulator publishes a
reading to the moment a WebSocket client of the twin receives it (same clock)."""
import asyncio
import json
import sys
import time

import websockets


async def main(seconds):
    lat = []
    async with websockets.connect("ws://backend:8000/ws", max_size=None) as ws:
        end = time.time() + seconds
        while time.time() < end:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if m.get("type") == "sensor" and m.get("ts"):
                lat.append((time.time() - m["ts"]) * 1000)
    lat.sort()
    q = lambda p: round(lat[min(len(lat) - 1, int(p * len(lat)))], 2)  # noqa: E731
    print(json.dumps({"samples": len(lat), "p50_ms": q(0.5), "p95_ms": q(0.95), "p99_ms": q(0.99), "max_ms": round(lat[-1], 2)}))


asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 60))
