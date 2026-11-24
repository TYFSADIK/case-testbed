# 1. Physical setup: your part

The whole synthetic environment runs in Docker on one machine (see
`local/rehearse.sh`). The physical parts make it a *distributed* exercise with
*real* sensors in it:

* **Two sites.** Site A (the city simulation, the twin, the database) on one
  machine; site B (the UAV and convoy federates, the ROS 2 robot, the DIS
  gateway) on a second machine, joined over a real network you can impair.
* **A real sensor node** on the waterfront, reporting into the same twin as
  the 95 virtual sensors, on the same 3D map.
* **Optionally a real ROS 2 robot** replacing the simulated patrol robot.

## 1.1 Shopping list

| Item | Example | About (CAD) | Why |
| --- | --- | --- | --- |
| Site A machine | mini PC or Proxmox VM: 4 cores, 8 GB, 40 GB | - | Twin backend, PostGIS, MQTT, site A simulation |
| Site B machine | any second PC, Raspberry Pi 5 (8 GB) or VM | 120 | Second training site: federates and DIS gateway |
| Ethernet switch or two NICs | unmanaged gigabit switch | 25 | The "WAN" between the sites |
| ESP32 dev board | ESP32-DevKitC (WROOM-32) | 15 | The field sensor node |
| BME280 breakout (I2C) | | 12 | Temperature, humidity, pressure |
| PMS5003 particulate sensor + adapter | Plantower PMS5003 | 35 | PM2.5, same unit as the twin's air-quality sensors |
| JSN-SR04T waterproof ultrasonic | | 15 | Distance to the water: a real water-level gauge |
| 18650 cell + holder, TP4056 charger, 6 V 2 W solar panel | | 30 | A solar node like the twin's water gauges |
| 2 x 100 kOhm resistors | | 1 | Battery voltage divider |
| IP65 junction box, cable glands | | 15 | It lives outside |
| (Optional) ROS 2 robot | TurtleBot 4 Lite, or the GPS-denied rover from my other project | - | A real robot instead of the simulated patrol |

## 1.2 Wiring the sensor node

```
ESP32                       BME280 (I2C)          PMS5003 (5 V!)          JSN-SR04T (5 V)
  3V3 ───────────────────── VIN
  GND ───────────────────── GND ───────────────── GND ─────────────────── GND
  GPIO21 (SDA) ──────────── SDA
  GPIO22 (SCL) ──────────── SCL
  5V (VIN pin) ──────────────────────────────────  VCC ─────────────────── 5V
  GPIO16 (UART2 RX) ──────────────────────────────  TX
  GPIO17 (UART2 TX) ──────────────────────────────  RX
  GPIO5 ───────────────────────────────────────────────────────────────── TRIG
  GPIO18 ── 10k ──┬─────────────────────────────────────────────────────── ECHO   (5 V echo: divide to 3.3 V)
                 20k
                  │
                 GND
Battery + ── 100k ──┬── 100k ── GND        (GPIO35 reads half the battery voltage)
                    └── GPIO35
```

The PMS5003 and the JSN-SR04T need 5 V; the ESP32's 5V pin provides it when
the board is powered by USB or the charger's 5 V boost. The JSN-SR04T's echo
pin swings to 5 V, so it goes through the 10k/20k divider before GPIO18.

Mount the ultrasonic sensor pointing straight down at the water, 1 to 3 m
above it (its blind zone is 25 cm). Put `MOUNT_HEIGHT_M` in `config.py`. A
rising `water_gap_m` means the water is falling.

## 1.3 Flash it

1. Flash MicroPython for ESP32 (`esptool.py --chip esp32 write_flash -z 0x1000 ESP32_GENERIC-*.bin`).
2. Copy the files: `mpremote cp hardware/sensor_node/main.py :` and a BME280
   driver (`mpremote mip install bme280` or the common `bme280.py`), then
   `cp config.example.py config.py`, edit it, `mpremote cp config.py :`.
3. `mpremote umip install umqtt.simple` (or `mip install umqtt.simple`).
4. Reset. The node registers itself (`case/registry/hw-node-01`, retained)
   and appears on the 3D map as a larger cyan dot at the position in
   `config.py`; its readings are stored in PostGIS with `hardware = true`.

The rehearsal exercises exactly this path with `tools/hw_node_sim.py`, which
publishes the same topics and JSON from inside Docker.

## 1.4 Two sites on two machines

On site A:

```bash
cd stacks
docker compose up -d mosquitto postgis backend sim
```

On site B, copy the repository and point the gateway at site A:

```bash
cd stacks
TWIN_HOST=<site A address> docker compose up -d gateway siteb ros2
```

(publish UDP 3000 on site A: add `"3000:3000/udp"` to the backend's ports.)
Now the WAN is real. Impair it on site B's gateway with the same command the
rehearsal uses:

```bash
docker compose exec gateway tc qdisc replace dev eth1 root netem delay 150ms 15ms loss 2%
```

and watch the convoy and UAV in the 3D view lag and correct themselves; the
dead-reckoning numbers in the README tell you what to expect.

## 1.5 A real ROS 2 robot (optional)

The bridge (`services/ros2/case_ros/dis_bridge.py`) subscribes to
`/patrol/pose` (`geometry_msgs/PoseStamped`, local east/north metres) and
`/patrol/cmd_vel`. On a real robot, publish the robot's pose in that frame
(for example from `robot_localization` with the origin set to the exercise
origin, 44.3850 N 79.6850 W) on `/patrol/pose`, run only `dis_bridge.py`, and
the robot joins the exercise as entity 3-1 next to the simulated traffic.

## Safety

The node is low voltage. Seal the box, keep the cell away from heat, and
mount the sensor where it cannot fall into the water. Use public waterfront
only with permission.
