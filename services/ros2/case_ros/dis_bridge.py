"""ROS 2 to DIS bridge: the robot joins the distributed exercise.

Subscribes to /patrol/pose and /patrol/cmd_vel and publishes the robot as a
DIS Entity State PDU (site 3, unmanned ground vehicle) to the site gateway,
with the same threshold dead reckoning as every other federate. Also logs the
true pose at 20 Hz for the dead-reckoning measurement.
"""
import math
import os
import socket
import sys
import time
from pathlib import Path

import rclpy
from geometry_msgs.msg import PoseStamped, Twist
from rclpy.node import Node

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from case import dis  # noqa: E402

GW = (os.environ.get("GATEWAY", "gateway"), int(os.environ.get("GATEWAY_PORT", "3001")))


class Bridge(Node):
    def __init__(self):
        super().__init__("dis_bridge")
        self.v = 0.0
        self.pose = None
        self.sender = dis.Sender(threshold_m=float(os.environ.get("DR_THRESHOLD", "1.0")))
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        Path(os.environ.get("TRUTH_DIR", "/data/dr")).mkdir(parents=True, exist_ok=True)
        self.truth = open(Path(os.environ.get("TRUTH_DIR", "/data/dr")) / "truth-ros.csv", "a", buffering=1)
        self.create_subscription(PoseStamped, "/patrol/pose", self.on_pose, 10)
        self.create_subscription(Twist, "/patrol/cmd_vel", lambda m: setattr(self, "v", m.linear.x), 10)
        self.create_timer(0.05, self.tick)
        self.sent = 0

    def on_pose(self, m):
        q = m.pose.orientation
        self.pose = (m.pose.position.x, m.pose.position.y, 2 * math.atan2(q.z, q.w))

    def tick(self):
        if self.pose is None:
            return
        now = time.time()
        x, y, th = self.pose
        es = dis.state_from_local(3, 1, 1, "robot", "ROS2-PATROL", (x, y), (self.v * math.cos(th), self.v * math.sin(th)), th, now)
        if self.sender.due(es, now):
            self.udp.sendto(dis.encode(es), GW)
            self.sender.mark(es)
        self.truth.write(f"{now:.4f},3-1,{x:.3f},{y:.3f}\n")


def main():
    rclpy.init()
    rclpy.spin(Bridge())


if __name__ == "__main__":
    main()
