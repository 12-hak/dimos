#!/usr/bin/env bash
# After your laptop is on the G1 WiFi (e.g. G1_xxxx) and 192.168.12.1 answers ping,
# run:  ./scripts/demo_g1_basic.sh
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROBOT_IP="${ROBOT_IP:-192.168.12.1}"
if ! command -v ping >/dev/null; then
  echo "ping not found" >&2
  exit 1
fi
if ! ping -c 1 -W 2 "$ROBOT_IP" >/dev/null 2>&1; then
  echo "Cannot reach $ROBOT_IP. Connect to the G1 WiFi first, then retry." >&2
  exit 1
fi
# Optional: second interface (e.g. USB Ethernet) for internet while on G1 WiFi
# set HUMBLE in your environment or use the default
: "${HUMBLE:=/opt/ros/humble}"
if [[ -f "$HUMBLE/setup.bash" ]]; then
  # shellcheck source=/dev/null
  source "$HUMBLE/setup.bash"
else
  echo "Warning: $HUMBLE/setup.bash not found; skip if you already sourced ROS2" >&2
fi
# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"
export ROBOT_IP
cd "$REPO_ROOT"
exec dimos run unitree-g1-basic --robot-ip "$ROBOT_IP"
