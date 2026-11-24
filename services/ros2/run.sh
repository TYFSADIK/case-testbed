#!/usr/bin/env bash
# Start the ROS 2 patrol robot and its DIS bridge in one container.
# ROS setup scripts reference unset variables, so strict mode starts after them
# shellcheck disable=SC1091
source /opt/ros/jazzy/setup.bash
set -euo pipefail
export PYTHONPATH=/app:${PYTHONPATH:-}
python3 case_ros/patrol.py &
python3 case_ros/dis_bridge.py &
status=0
wait -n || status=$?
echo "a ROS 2 process exited (status $status); restarting the container" >&2
exit 1
