# Site B's ROS 2 robot: a patrol robot node, and a bridge that turns its pose
# into DIS Entity State PDUs for the exercise network.
FROM ros:jazzy-ros-core
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip \
    && pip3 install --no-cache-dir --break-system-packages opendis==1.1.0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY case/__init__.py case/geo.py case/dis.py case/
COPY services/ros2/case_ros case_ros
COPY services/ros2/run.sh .
CMD ["bash", "run.sh"]
