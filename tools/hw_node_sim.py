#!/usr/bin/env python3
"""Stand-in for the ESP32 sensor node (hardware/sensor_node): same MQTT topics,
same JSON, so the path from a real node to the twin is exercised end to end.
Publishes a retained registration, then BME280 + PMS5003 + JSN-SR04T readings."""
import json
import math
import random
import sys
import time

import paho.mqtt.client as mqtt

host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60
NODE = "hw-node-01"
LAT, LON = 44.38905, -79.68650          # on the waterfront, near the marina
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NODE)
c.connect(host, 1883, 30)
c.loop_start()
reg = {"id": NODE, "type": "env_node", "unit": "mixed", "latlon": [LAT, LON], "period": 10.0,
       "power": "battery", "hardware": True, "sensors": ["bme280", "pms5003", "jsn-sr04t"]}
c.publish(f"case/registry/{NODE}", json.dumps(reg), qos=1, retain=True)
end, n = time.time() + seconds, 0
while time.time() < end:
    now = time.time()
    m = {"id": NODE, "type": "env_node", "t": round(now, 3), "ts": round(now, 4), "unit": "mixed",
         "v": round(1.5 + 0.3 * math.sin(now / 300) + random.gauss(0, 0.05), 2),            # air temperature, C
         "humidity_pct": round(82 + random.gauss(0, 1), 1), "pressure_hpa": round(1008.2 + random.gauss(0, 0.2), 1),
         "pm25_ugm3": round(6 + random.gauss(0, 0.8), 1), "water_gap_m": round(1.42 + random.gauss(0, 0.005), 3),
         "battery_v": 3.91}
    c.publish(f"case/sensor/{NODE}", json.dumps(m), qos=1)
    n += 1
    time.sleep(2)
c.loop_stop()
print(json.dumps({"node": NODE, "published": n}))
