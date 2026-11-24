"""Site B's DIS gateway: forwards PDUs from the site network across the WAN.

In a distributed exercise each site has a gateway between its local
simulation network and the wide-area link. This one relays UDP from the site
network (port 3001) to the twin and counts PDUs and bytes per entity. The
WAN impairment (latency, jitter, loss) is applied to its WAN interface with
Linux tc/netem by the test harness, exactly as a real WAN would impose it.
"""
import json
import os
import socket
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = (os.environ.get("TWIN_HOST", "backend"), int(os.environ.get("TWIN_PORT", "3000")))
pdus, nbytes = Counter(), Counter()
started = time.time()


def relay():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("0.0.0.0", 3001))
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr, resolved = None, 0.0
    while True:
        data, _ = rx.recvfrom(2048)
        if addr is None or time.time() - resolved > 30:
            try:                                   # the twin may start after the gateway, or move
                addr, resolved = (socket.gethostbyname(TARGET[0]), TARGET[1]), time.time()
            except OSError:
                continue
        try:
            tx.sendto(data, addr)
        except OSError:
            addr = None
            continue
        if len(data) >= 18:
            key = f"{int.from_bytes(data[12:14], 'big')}-{int.from_bytes(data[16:18], 'big')}"
            pdus[key] += 1
            nbytes[key] += len(data)


class Stats(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"since": started, "now": time.time(), "pdus": pdus, "bytes": nbytes}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(target=relay, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", 8080), Stats).serve_forever()
