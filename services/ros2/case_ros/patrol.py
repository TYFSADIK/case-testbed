"""ROS 2 patrol robot: drives a rectangular patrol around the waterfront park.

Publishes geometry_msgs/PoseStamped on /patrol/pose and geometry_msgs/Twist on
/patrol/cmd_vel at 10 Hz, in the exercise's local east/north frame (metres).
Unicycle kinematics: 1.5 m/s on the straights, slows to turn at each corner.
"""
import math

import rclpy
from geometry_msgs.msg import PoseStamped, Twist
from rclpy.node import Node

CORNERS = [(-330.0, 300.0), (-130.0, 300.0), (-130.0, 420.0), (-330.0, 420.0)]


class Patrol(Node):
    def __init__(self):
        super().__init__("patrol_robot")
        self.pose_pub = self.create_publisher(PoseStamped, "/patrol/pose", 10)
        self.vel_pub = self.create_publisher(Twist, "/patrol/cmd_vel", 10)
        self.x, self.y = CORNERS[0]
        self.th = 0.0
        self.goal = 1
        self.dt = 0.1
        self.create_timer(self.dt, self.tick)

    def tick(self):
        gx, gy = CORNERS[self.goal]
        dx, dy = gx - self.x, gy - self.y
        if math.hypot(dx, dy) < 1.0:
            self.goal = (self.goal + 1) % len(CORNERS)
            return
        want = math.atan2(dy, dx)
        err = (want - self.th + math.pi) % (2 * math.pi) - math.pi
        w = max(-0.8, min(0.8, 2.0 * err))
        v = 1.5 if abs(err) < 0.3 else 0.3
        self.th += w * self.dt
        self.x += v * math.cos(self.th) * self.dt
        self.y += v * math.sin(self.th) * self.dt
        p = PoseStamped()
        p.header.stamp = self.get_clock().now().to_msg()
        p.header.frame_id = "exercise_local"
        p.pose.position.x, p.pose.position.y = self.x, self.y
        p.pose.orientation.z, p.pose.orientation.w = math.sin(self.th / 2), math.cos(self.th / 2)
        self.pose_pub.publish(p)
        t = Twist()
        t.linear.x, t.angular.z = v, w
        self.vel_pub.publish(t)


def main():
    rclpy.init()
    rclpy.spin(Patrol())


if __name__ == "__main__":
    main()
