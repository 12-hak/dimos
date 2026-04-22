#!/usr/bin/env python3
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

from dimos.core.blueprints import autoconnect
from dimos.mapping.costmapper import cost_mapper
from dimos.mapping.lidar_depth_merge import (
    LidarDepthMapMerge,
    depth_scan_placeholder,
    lidar_depth_map_merge,
)
from dimos.mapping.pointclouds.occupancy import GeneralOccupancyConfig
from dimos.mapping.voxels import voxel_mapper
from dimos.navigation.frontier_exploration import wavefront_frontier_explorer
from dimos.navigation.replanning_a_star.module import replanning_a_star_planner
from dimos.robot.unitree.go2.connection import GO2Connection
from dimos.robot.unitree.go2.blueprints.basic.unitree_go2_basic import unitree_go2_basic

_RAW_LIDAR_TOPIC = "robot_lidar_raw"

unitree_go2_custom_avoid = (
    autoconnect(
        unitree_go2_basic,
        lidar_depth_map_merge(),
        depth_scan_placeholder(),
        voxel_mapper(voxel_size=0.1),
        cost_mapper(
            algo="general",
            config=GeneralOccupancyConfig(
                resolution=0.05,
                min_height=0.22,
                max_height=2.2,
                mark_free_radius=0.45,
            ),
        ),
        replanning_a_star_planner(),
        wavefront_frontier_explorer(),
    )
    .remappings(
        [
            (GO2Connection, "lidar", _RAW_LIDAR_TOPIC),
            (LidarDepthMapMerge, "lidar_scan", _RAW_LIDAR_TOPIC),
        ]
    )
    .global_config(
        n_workers=8,
        robot_model="unitree_go2",
        obstacle_avoidance=False,
        robot_width=0.44,
        robot_rotation_diameter=0.82,
        planner_strategy="mixed",
        planner_robot_speed=0.42,
        blocked_movement_seconds=5.0,
        path_obstacle_cost_threshold=90,
        replan_max_attempts=24,
        replan_attempt_reset_distance_m=1.2,
        replan_mission_time_limit_s=300.0,
    )
)

__all__ = ["unitree_go2_custom_avoid"]
