# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
from threading import Event, RLock, Thread, current_thread
import time

from dimos_lcm.std_msgs import Bool
from reactivex import Subject
from reactivex.disposable import CompositeDisposable

from dimos.core.global_config import GlobalConfig
from dimos.core.resource import Resource
from dimos.mapping.occupancy.path_resampling import smooth_resample_path
from dimos.msgs.geometry_msgs import Twist
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import CostValues, OccupancyGrid
from dimos.msgs.nav_msgs.Path import Path
from dimos.navigation.base import NavigationState
from dimos.navigation.replanning_a_star.goal_validator import find_safe_goal
from dimos.navigation.replanning_a_star.local_planner import LocalPlanner, StopMessage
from dimos.navigation.replanning_a_star.min_cost_astar import min_cost_astar
from dimos.navigation.replanning_a_star.navigation_map import NavigationMap
from dimos.navigation.replanning_a_star.position_tracker import PositionTracker
from dimos.navigation.replanning_a_star.replan_limiter import ReplanLimiter
from dimos.utils.logging_config import setup_logger
from dimos.utils.trigonometry import angle_diff

logger = setup_logger()


class GlobalPlanner(Resource):
    path: Subject[Path]
    goal_reached: Subject[Bool]

    _current_odom: PoseStamped | None = None
    _current_goal: PoseStamped | None = None
    _goal_reached: bool = False
    _thread: Thread | None = None

    _global_config: GlobalConfig
    _navigation_map: NavigationMap
    _local_planner: LocalPlanner
    _position_tracker: PositionTracker
    _replan_limiter: ReplanLimiter
    _disposables: CompositeDisposable
    _stop_planner: Event
    _replan_event: Event
    _replan_reason: StopMessage | None
    _lock: RLock

    _safe_goal_tolerance: float = 4.0
    _goal_tolerance: float = 0.2
    _rotation_tolerance: float = math.radians(15)
    _replan_goal_tolerance: float = 0.5
    _max_replan_attempts: int = 10
    _max_path_deviation: float = 0.9

    def __init__(self, global_config: GlobalConfig) -> None:
        self.path = Subject()
        self.goal_reached = Subject()

        self._global_config = global_config
        self._navigation_map = NavigationMap(self._global_config)
        self._local_planner = LocalPlanner(
            self._global_config, self._navigation_map, self._goal_tolerance
        )
        self._position_tracker = PositionTracker(global_config.blocked_movement_seconds)
        self._replan_limiter = ReplanLimiter(
            max_attempts=global_config.replan_max_attempts,
            reset_distance_m=global_config.replan_attempt_reset_distance_m,
        )
        self._disposables = CompositeDisposable()
        self._stop_planner = Event()
        self._replan_event = Event()
        self._replan_reason = None
        self._lock = RLock()
        self._mission_start_mono: float | None = None
        self._retry_plan_after_failure: bool = False
        self._last_failed_plan_retry_mono: float = 0.0

    def start(self) -> None:
        self._local_planner.start()
        self._disposables.add(
            self._local_planner.stopped_navigating.subscribe(self._on_stopped_navigating)
        )
        self._stop_planner.clear()
        self._thread = Thread(target=self._thread_entrypoint, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.cancel_goal()
        self._local_planner.stop()
        self._disposables.dispose()
        self._stop_planner.set()
        self._replan_event.set()

        if self._thread is not None and self._thread is not current_thread():
            self._thread.join(2)
            if self._thread.is_alive():
                logger.error("GlobalPlanner thread did not stop in time.")
            self._thread = None

    def handle_odom(self, msg: PoseStamped) -> None:
        with self._lock:
            self._current_odom = msg

        self._local_planner.handle_odom(msg)
        self._position_tracker.add_position(msg)

    def handle_global_costmap(self, msg: OccupancyGrid) -> None:
        self._navigation_map.update(msg)

    def handle_goal_request(self, goal: PoseStamped) -> None:
        logger.info("Got new goal", goal=str(goal))
        with self._lock:
            self._current_goal = goal
            self._goal_reached = False
            self._mission_start_mono = time.monotonic()
            self._retry_plan_after_failure = False
            self._last_failed_plan_retry_mono = 0.0
        self._replan_limiter.reset()
        self._plan_path()

    def cancel_goal(self, *, but_will_try_again: bool = False, arrived: bool = False) -> None:
        logger.info("Cancelling goal.", but_will_try_again=but_will_try_again, arrived=arrived)

        with self._lock:
            self._position_tracker.reset_data()

            if not but_will_try_again:
                self._current_goal = None
                self._goal_reached = arrived
                self._replan_limiter.reset()
                self._mission_start_mono = None
                self._retry_plan_after_failure = False

        if not but_will_try_again:
            self.path.on_next(Path())
        self._local_planner.stop_planning()

        if not but_will_try_again:
            self.goal_reached.on_next(Bool(arrived))

    def get_state(self) -> NavigationState:
        return self._local_planner.get_state()

    def is_goal_reached(self) -> bool:
        with self._lock:
            return self._goal_reached

    @property
    def cmd_vel(self) -> Subject[Twist]:
        return self._local_planner.cmd_vel

    @property
    def navigation_costmap(self) -> Subject[OccupancyGrid]:
        return self._local_planner.navigation_costmap

    def _mission_time_exceeded(self) -> bool:
        limit = self._global_config.replan_mission_time_limit_s
        if limit <= 0:
            return False
        with self._lock:
            start = self._mission_start_mono
        if start is None:
            return False
        return time.monotonic() - start >= limit

    def _thread_entrypoint(self) -> None:
        """Monitor if the robot is stuck, veers off track, or stopped navigating."""

        last_id = -1
        last_stuck_check = time.perf_counter()

        while not self._stop_planner.is_set():
            # Wait for either timeout or replan signal from local planner.
            replanning_wanted = self._replan_event.wait(timeout=0.1)

            if self._stop_planner.is_set():
                break

            # Handle stop message from local planner (priority)
            if replanning_wanted:
                self._replan_event.clear()
                with self._lock:
                    reason = self._replan_reason
                    self._replan_reason = None

                if reason is not None:
                    self._handle_stop_message(reason)
                    last_stuck_check = time.perf_counter()
                    continue

            with self._lock:
                pending_fail = self._retry_plan_after_failure
                current_goal = self._current_goal
                current_odom = self._current_odom

            if pending_fail and current_goal:
                if self._mission_time_exceeded():
                    logger.info("Mission time budget exhausted during plan retries.")
                    with self._lock:
                        self._retry_plan_after_failure = False
                    self.cancel_goal()
                    continue
                now = time.monotonic()
                if now - self._last_failed_plan_retry_mono >= 0.4:
                    self._last_failed_plan_retry_mono = now
                    with self._lock:
                        self._retry_plan_after_failure = False
                    logger.info("Plan search failed; recovery + retry toward goal.")
                    self._local_planner.stop_planning()
                    self._pulse_smart_recovery()
                    self._plan_path()
                    last_stuck_check = time.perf_counter()
                    continue
                continue

            with self._lock:
                current_goal = self._current_goal
                current_odom = self._current_odom

            if not current_goal or not current_odom:
                continue

            if (
                current_goal.position.distance(current_odom.position) < self._goal_tolerance
                and abs(
                    angle_diff(current_goal.orientation.euler[2], current_odom.orientation.euler[2])
                )
                < self._rotation_tolerance
            ):
                logger.info("Close enough to goal. Accepting as arrived.")
                self.cancel_goal(arrived=True)
                continue

            # Check if robot has veered too far off the path
            deviation = self._local_planner.get_distance_to_path()
            if deviation is not None and deviation > self._max_path_deviation:
                logger.info(
                    "Robot veered off track. Replanning.",
                    deviation=round(deviation, 2),
                    threshold=self._max_path_deviation,
                )
                self._replan_path()
                last_stuck_check = time.perf_counter()
                continue

            _, new_id = self._local_planner.get_unique_state()

            if new_id != last_id:
                last_id = new_id
                last_stuck_check = time.perf_counter()
                continue

            if (
                time.perf_counter() - last_stuck_check > self._global_config.blocked_movement_seconds
                and self._position_tracker.is_stuck()
            ):
                logger.info("Robot is stuck. Recovering then replanning.")
                self._local_planner.stop_planning()
                self._pulse_smart_recovery()
                self._replan_path()
                last_stuck_check = time.perf_counter()

    def _cell_blocks_recovery(self, value: int) -> bool:
        if value == CostValues.OCCUPIED:
            return True
        thr = self._global_config.path_obstacle_cost_threshold
        if thr is not None and value >= thr:
            return True
        return False

    def _corridor_clear(
        self,
        costmap: OccupancyGrid,
        wx: float,
        wy: float,
        dir_x: float,
        dir_y: float,
        length_m: float,
        lateral_m: float,
        n_steps: int,
    ) -> bool:
        length = math.hypot(dir_x, dir_y)
        if length < 1e-6:
            return False
        ux, uy = dir_x / length, dir_y / length
        px, py = -uy, ux
        for step in range(1, n_steps + 1):
            t = (length_m / n_steps) * step
            for side in (-1.0, 0.0, 1.0):
                sx = wx + ux * t + px * side * lateral_m
                sy = wy + uy * t + py * side * lateral_m
                v = costmap.cell_value(Vector3(sx, sy, 0.0))
                if self._cell_blocks_recovery(v):
                    return False
        return True

    def _pulse_cmd_for(self, duration: float, linear_x: float, angular_z: float) -> None:
        if duration <= 0:
            return
        twist = Twist(
            linear=Vector3(x=linear_x, y=0.0, z=0.0),
            angular=Vector3(x=0.0, y=0.0, z=angular_z),
        )
        deadline = time.perf_counter() + duration
        while time.perf_counter() < deadline:
            self._local_planner.cmd_vel.on_next(twist)
            time.sleep(0.05)
        self._local_planner.cmd_vel.on_next(Twist())

    def _pulse_smart_recovery(self) -> None:
        duration = self._global_config.stuck_recovery_reverse_seconds
        if duration <= 0:
            return

        lv = float(self._global_config.stuck_recovery_linear_x)
        with self._lock:
            odom = self._current_odom

        try:
            costmap = self._navigation_map.binary_costmap
        except ValueError:
            costmap = None

        if odom is None or costmap is None or costmap.grid.size == 0:
            self._pulse_cmd_for(duration, lv, 0.0)
            return

        yaw = odom.orientation.euler[2]
        wx, wy = float(odom.position.x), float(odom.position.y)

        bx = math.cos(yaw + math.pi)
        by = math.sin(yaw + math.pi)

        def rot(vx: float, vy: float, delta: float) -> tuple[float, float]:
            c, s = math.cos(delta), math.sin(delta)
            return vx * c - vy * s, vx * s + vy * c

        lx_b, ly_b = rot(bx, by, 0.45)
        rx_b, ry_b = rot(bx, by, -0.45)

        straight_ok = self._corridor_clear(costmap, wx, wy, bx, by, 0.52, 0.14, 5)
        left_arc_ok = self._corridor_clear(costmap, wx, wy, lx_b, ly_b, 0.48, 0.14, 5)
        right_arc_ok = self._corridor_clear(costmap, wx, wy, rx_b, ry_b, 0.48, 0.14, 5)

        lyaw = yaw + math.pi / 2
        ryaw = yaw - math.pi / 2
        lx_f, ly_f = math.cos(lyaw), math.sin(lyaw)
        rx_f, ry_f = math.cos(ryaw), math.sin(ryaw)
        rot_left_clear = self._corridor_clear(costmap, wx, wy, lx_f, ly_f, 0.38, 0.12, 4)
        rot_right_clear = self._corridor_clear(costmap, wx, wy, rx_f, ry_f, 0.38, 0.12, 4)

        if straight_ok:
            logger.info("Recovery: straight reverse.")
            self._pulse_cmd_for(duration, lv, 0.0)
            return

        if left_arc_ok:
            logger.info("Recovery: reverse with left arc (clearance behind-left).")
            self._pulse_cmd_for(duration, lv * 0.78, 0.42)
            return

        if right_arc_ok:
            logger.info("Recovery: reverse with right arc (clearance behind-right).")
            self._pulse_cmd_for(duration, lv * 0.78, -0.42)
            return

        rot_part = min(0.55, duration * 0.65)
        rest = max(0.0, duration - rot_part)

        if rot_left_clear and not rot_right_clear:
            logger.info("Recovery: rotate left in place (side clearance).")
            self._pulse_cmd_for(rot_part, 0.0, 0.48)
            if rest > 0:
                self._pulse_cmd_for(rest, lv * 0.62, 0.32)
            return

        if rot_right_clear and not rot_left_clear:
            logger.info("Recovery: rotate right in place (side clearance).")
            self._pulse_cmd_for(rot_part, 0.0, -0.48)
            if rest > 0:
                self._pulse_cmd_for(rest, lv * 0.62, -0.32)
            return

        if rot_left_clear:
            logger.info("Recovery: rotate left then short reverse arc.")
            self._pulse_cmd_for(rot_part, 0.0, 0.42)
            self._pulse_cmd_for(rest, lv * 0.65, 0.38)
            return

        if rot_right_clear:
            logger.info("Recovery: rotate right then short reverse arc.")
            self._pulse_cmd_for(rot_part, 0.0, -0.42)
            self._pulse_cmd_for(rest, lv * 0.65, -0.38)
            return

        logger.info("Recovery: tight space — short skewed reverse.")
        self._pulse_cmd_for(duration * 0.85, lv * 0.55, 0.35)

    def _on_stopped_navigating(self, stop_message: StopMessage) -> None:
        with self._lock:
            self._replan_reason = stop_message
        # Signal the monitoring thread to do the replanning. This is so we don't have two
        # threads which could be replanning at the same time.
        self._replan_event.set()

    def _handle_stop_message(self, stop_message: StopMessage) -> None:
        # Note, this runs in the monitoring thread.

        if stop_message == "arrived":
            self.path.on_next(Path())
            logger.info("Arrived at goal.")
            self.cancel_goal(arrived=True)
            return

        if stop_message == "obstacle_found":
            logger.info("Obstacle ahead; recovering then replanning toward goal.")
            self._local_planner.stop_planning()
            self._pulse_smart_recovery()
            self._replan_path()
            return

        if stop_message == "error":
            logger.info("Navigation error; recovering then replanning.")
            self._local_planner.stop_planning()
            self._pulse_smart_recovery()
            self._replan_path()
            return

        logger.error(f"No code to handle '{stop_message}'.")
        self.path.on_next(Path())
        self.cancel_goal()

    def _replan_path(self) -> None:
        with self._lock:
            current_odom = self._current_odom
            current_goal = self._current_goal

        logger.info("Replanning.", attempt=self._replan_limiter.get_attempt())

        assert current_odom is not None
        assert current_goal is not None

        if current_goal.position.distance(current_odom.position) < self._replan_goal_tolerance:
            self.cancel_goal(arrived=True)
            return

        if self._mission_time_exceeded():
            logger.info("Mission time budget exhausted; stopping navigation.")
            self.cancel_goal()
            return

        if not self._replan_limiter.can_retry(current_odom.position):
            logger.info(
                "Replanner attempt cap for this zone; continuing while mission time remains."
            )

        self._replan_limiter.will_retry()

        self._plan_path()

    def _register_plan_failure_for_retry(self) -> None:
        if self._mission_time_exceeded():
            logger.info("No valid path and mission time expired.")
            self.cancel_goal()
            return
        with self._lock:
            self._retry_plan_after_failure = True

    def _plan_path(self) -> None:
        self.cancel_goal(but_will_try_again=True)

        with self._lock:
            current_odom = self._current_odom
            current_goal = self._current_goal

        assert current_goal is not None

        if current_odom is None:
            logger.warning("Cannot handle goal request: missing odometry.")
            return

        safe_goal = self._find_safe_goal(current_goal.position)

        if not safe_goal:
            logger.warning("No safe goal near target; scheduling retry.")
            self._register_plan_failure_for_retry()
            return

        path = self._find_wide_path(safe_goal, current_odom.position)

        if not path:
            logger.warning(
                "No path found to the goal.", x=round(safe_goal.x, 3), y=round(safe_goal.y, 3)
            )
            self._register_plan_failure_for_retry()
            return

        with self._lock:
            self._retry_plan_after_failure = False

        resampled_path = smooth_resample_path(path, current_goal, 0.1)

        self.path.on_next(resampled_path)

        self._local_planner.start_planning(resampled_path)

    def _find_wide_path(self, goal: Vector3, robot_pos: Vector3) -> Path | None:
        sizes_to_try: list[float] = [1.1, 1.45, 1.85, 2.3]

        for size in sizes_to_try:
            costmap = self._navigation_map.make_gradient_costmap(size)
            path = min_cost_astar(costmap, goal, robot_pos)
            if path and path.poses:
                logger.info(f"Found path {size}x robot width.")
                return path

        return None

    def _find_safe_goal(self, goal: Vector3) -> Vector3 | None:
        costmap = self._navigation_map.binary_costmap

        if costmap.cell_value(goal) == CostValues.UNKNOWN:
            return goal

        safe_goal = find_safe_goal(
            costmap,
            goal,
            algorithm="bfs_contiguous",
            cost_threshold=CostValues.OCCUPIED,
            min_clearance=self._global_config.robot_rotation_diameter / 2,
            max_search_distance=self._safe_goal_tolerance,
        )

        if safe_goal is None:
            logger.warning("No safe goal found near requested target.")
            return None

        goals_distance = safe_goal.distance(goal)
        if goals_distance > 0.2:
            logger.warning(f"Travelling to goal {goals_distance}m away from requested goal.")

        logger.info("Found safe goal.", x=round(safe_goal.x, 2), y=round(safe_goal.y, 2))

        return safe_goal
