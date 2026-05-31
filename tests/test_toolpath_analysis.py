"""
Tier 1 — Unit tests for _analyse_toolpath: pattern detection and geometry extraction.
"""
import math
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import _analyse_toolpath
from conftest import make_raster_waypoints, make_contour_waypoints, make_helix_waypoints


class TestToolpathPattern:
    def test_raster_detected(self):
        # Use dense intermediate points so sign reversals are detected
        wps = make_raster_waypoints(layers=3, passes_per_layer=4, length_mm=40,
                                    y_step_mm=0.1, steps_per_pass=10)
        tp = _analyse_toolpath(wps)
        assert tp['pattern'] == 'raster', f"Expected raster, got {tp['pattern']}"

    def test_contour_detected(self):
        wps = make_contour_waypoints(layers=5, radius_mm=20)
        tp = _analyse_toolpath(wps)
        assert tp['pattern'] == 'contour', f"Expected contour, got {tp['pattern']}"

    def test_helix_detected(self):
        # make_helix_waypoints uses all layer=1 so z-span-within-layer triggers helix detection
        wps = make_helix_waypoints(turns=5, radius_mm=20, total_height_mm=10)
        tp = _analyse_toolpath(wps)
        assert tp['pattern'] == 'helix', f"Expected helix, got {tp['pattern']}"


class TestToolpathGeometry:
    def test_z_extent(self):
        wps = make_raster_waypoints(layers=10, layer_height_mm=0.5)
        tp = _analyse_toolpath(wps)
        assert abs(tp['z_extent'] - 5.0) < 0.6   # 10 × 0.5mm ±1 layer tolerance

    def test_xy_extent_raster(self):
        """Raster with 40mm length and 4 passes × 2mm step → X≈40mm, Y≈6mm."""
        wps = make_raster_waypoints(layers=1, passes_per_layer=4, length_mm=40, y_step_mm=2.0)
        tp = _analyse_toolpath(wps)
        dx, dy = tp['xy_extent']
        assert 38 < dx < 42, f"X extent {dx:.1f}"
        assert 4  < dy < 8,  f"Y extent {dy:.1f}"

    def test_coil_radius_helix(self):
        wps = make_helix_waypoints(turns=5, radius_mm=25.0, total_height_mm=10)
        tp = _analyse_toolpath(wps)
        assert tp['pattern'] == 'helix', f"Helix not detected: {tp['pattern']}"
        assert tp['coil_radius_mm'] > 0
        assert abs(tp['coil_radius_mm'] - 25.0) < 5.0

    def test_centroid_raster(self):
        """Centroid should be near centre of raster bounding box."""
        wps = make_raster_waypoints(layers=4, passes_per_layer=4, length_mm=40, y_step_mm=2.0)
        tp = _analyse_toolpath(wps)
        cx, cy, _ = tp['centroid']
        assert abs(cx - 20.0) < 3.0, f"cx={cx:.1f}"
        assert abs(cy -  3.0) < 3.0, f"cy={cy:.1f}"

    def test_z_per_layer(self):
        wps = make_raster_waypoints(layers=8, layer_height_mm=0.6)
        tp = _analyse_toolpath(wps)
        assert abs(tp['z_per_layer'] - 0.6) < 0.15

    def test_dominant_dir_raster_x(self):
        """Raster along X → dominant direction should be close to (±1, 0)."""
        wps = make_raster_waypoints(layers=2, passes_per_layer=4, length_mm=40)
        tp = _analyse_toolpath(wps)
        ux, uy = tp['dominant_dir']
        assert abs(abs(ux) - 1.0) < 0.3, f"dominant_dir=({ux:.2f},{uy:.2f})"

    def test_empty_waypoints_returns_defaults(self):
        tp = _analyse_toolpath([])
        assert tp['pattern'] == 'raster'
        assert tp['z_per_layer'] > 0
        assert len(tp['gravity_vec']) == 3

    def test_layer_z_mapping(self):
        """layer_z dict should have one entry per layer."""
        wps = make_raster_waypoints(layers=5)
        tp = _analyse_toolpath(wps)
        assert len(tp['layer_z']) == 5
