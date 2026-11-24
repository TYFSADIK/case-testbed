# CASE field sensor node: ESP32 + BME280 + PMS5003 + JSN-SR04T, MicroPython.
#
# Publishes to the twin's MQTT broker with exactly the topics and JSON the
# simulated sensors use, so a real node appears on the 3D map next to them:
#   case/registry/<NODE_ID>   retained: who and where I am
#   case/sensor/<NODE_ID>     every PERIOD_S: temperature, humidity, pressure,
#                             PM2.5, distance to the water, battery voltage
import json
import time

import machine
import network
from umqtt.simple import MQTTClient

import bme280
import config

i2c = machine.I2C(0, scl=machine.Pin(22), sda=machine.Pin(21), freq=100000)
bme = bme280.BME280(i2c=i2c)
pms = machine.UART(2, baudrate=9600, rx=16, tx=17, timeout=1500)
trig = machine.Pin(5, machine.Pin.OUT, value=0)
echo = machine.Pin(18, machine.Pin.IN)
vbat = machine.ADC(machine.Pin(35))
vbat.atten(machine.ADC.ATTN_11DB)


def wifi():
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if not w.isconnected():
        w.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        for _ in range(40):
            if w.isconnected():
                break
            time.sleep(0.5)
    return w.isconnected()


def pm25():
    """Read one 32-byte PMS5003 frame; return PM2.5 (atmospheric) in ug/m3, or None."""
    pms.read()                                  # drop stale bytes
    for _ in range(3):
        b = pms.read(32)
        if b and len(b) == 32 and b[0] == 0x42 and b[1] == 0x4D:
            if sum(b[:30]) & 0xFFFF == (b[30] << 8 | b[31]):
                return b[12] << 8 | b[13]
    return None


def distance_m():
    """JSN-SR04T: 10 us trigger, echo pulse width -> metres (speed of sound corrected for temperature)."""
    trig.value(1)
    time.sleep_us(10)
    trig.value(0)
    t = machine.time_pulse_us(echo, 1, 30000)
    if t < 0:
        return None
    temp = float(bme.values[0][:-1]) if bme else 10.0
    c = 331.3 + 0.606 * temp
    return round(t / 1e6 * c / 2, 3)


def battery_v():
    return round(vbat.read_uv() / 1e6 * 2, 2)   # 1:1 divider on GPIO35


def main():
    while not wifi():
        time.sleep(5)
    c = MQTTClient(config.NODE_ID, config.MQTT_HOST, keepalive=60)
    c.connect()
    reg = {"id": config.NODE_ID, "type": "env_node", "unit": "mixed", "latlon": [config.LAT, config.LON],
           "period": config.PERIOD_S, "power": "battery", "hardware": True, "sensors": ["bme280", "pms5003", "jsn-sr04t"]}
    c.publish("case/registry/" + config.NODE_ID, json.dumps(reg), retain=True, qos=1)
    while True:
        t, p, h = bme.read_compensated_data()
        now = time.time() + 946684800             # MicroPython epoch is 2000-01-01; NTP-set by ntptime below
        msg = {"id": config.NODE_ID, "type": "env_node", "t": now, "ts": now, "unit": "mixed",
               "v": round(t / 100, 2), "humidity_pct": round(h / 1024, 1), "pressure_hpa": round(p / 25600, 1),
               "pm25_ugm3": pm25(), "water_gap_m": distance_m(), "battery_v": battery_v()}
        try:
            c.publish("case/sensor/" + config.NODE_ID, json.dumps(msg), qos=1)
        except OSError:
            wifi()
            c.connect()
        time.sleep(config.PERIOD_S)


try:
    import ntptime
    wifi()
    ntptime.settime()
except Exception:
    pass
main()
