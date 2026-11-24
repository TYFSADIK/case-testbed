# Copy to config.py on the board and fill in. Never commit config.py.
WIFI_SSID = "your-network"
WIFI_PASSWORD = "your-password"
MQTT_HOST = "192.168.1.50"        # the machine running the twin (stacks/docker-compose.yml publishes 1883 on 127.0.0.1;
                                  # bind it to the LAN address for a real node, and add a password, see docs/03-production.md)
NODE_ID = "hw-node-01"
LAT, LON = 44.38905, -79.68650     # where you mounted it
MOUNT_HEIGHT_M = 1.80              # ultrasonic sensor height above the water at a normal level
PERIOD_S = 10
