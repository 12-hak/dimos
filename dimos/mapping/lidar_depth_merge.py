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

from __future__ import annotations

import time

import numpy as np
from reactivex.disposable import Disposable

from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In, Out
from dimos.msgs.sensor_msgs import PointCloud2
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class LidarDepthMapMerge(Module):
    """Concatenate lidar and optional depth-derived points (both world frame) for mapping."""

    lidar_scan: In[PointCloud2]
    depth_scan: In[PointCloud2]
    lidar: Out[PointCloud2]

    _last_depth: PointCloud2 | None = None
    _last_depth_mono: float = 0.0
    _depth_max_age_s: float = 0.35

    @rpc
    def start(self) -> None:
        super().start()

        self._disposables.add(
            Disposable(self.depth_scan.subscribe(self._on_depth)),
        )
        self._disposables.add(
            Disposable(self.lidar_scan.subscribe(self._on_lidar)),
        )

    def _on_depth(self, pc: PointCloud2) -> None:
        self._last_depth = pc
        self._last_depth_mono = time.monotonic()

    def _on_lidar(self, primary: PointCloud2) -> None:
        merged = self._merge(primary, self._last_depth)
        self.lidar.publish(merged)

    def _merge(self, lidar_pc: PointCloud2, depth_pc: PointCloud2 | None) -> PointCloud2:
        lp = self._positions_numpy(lidar_pc)
        parts: list[np.ndarray] = [lp] if lp.shape[0] else []

        if depth_pc is not None:
            age = time.monotonic() - self._last_depth_mono
            dp = self._positions_numpy(depth_pc)
            if age <= self._depth_max_age_s and dp.shape[0] > 0:
                parts.append(dp)
            elif dp.shape[0] > 0 and age > self._depth_max_age_s:
                logger.debug("Skipping stale depth scan for map merge.", age_s=round(age, 3))

        if not parts:
            return PointCloud2(
                pointcloud=lidar_pc.pointcloud_tensor,
                frame_id=lidar_pc.frame_id,
                ts=lidar_pc.ts,
            )

        combined = np.concatenate(parts, axis=0)
        out = PointCloud2.from_numpy(
            combined,
            frame_id=lidar_pc.frame_id,
            timestamp=lidar_pc.ts,
        )
        return out

    def _positions_numpy(self, pc: PointCloud2) -> np.ndarray:
        t = pc.pointcloud_tensor
        if not t.point or "positions" not in t.point:
            return np.zeros((0, 3), dtype=np.float32)
        return t.point["positions"].numpy().astype(np.float32, copy=False)


class DepthScanPlaceholder(Module):
    """Publishes an empty cloud so depth_scan input is wired; replace via remapping for real depth."""

    depth_scan: Out[PointCloud2]

    @rpc
    def start(self) -> None:
        super().start()
        empty = PointCloud2.from_numpy(
            np.zeros((0, 3), dtype=np.float32),
            frame_id="world",
            timestamp=time.time(),
        )
        self.depth_scan.publish(empty)


lidar_depth_map_merge = LidarDepthMapMerge.blueprint
depth_scan_placeholder = DepthScanPlaceholder.blueprint

__all__ = [
    "DepthScanPlaceholder",
    "LidarDepthMapMerge",
    "depth_scan_placeholder",
    "lidar_depth_map_merge",
]
