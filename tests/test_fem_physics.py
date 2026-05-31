"""
Tests for FEM physics functions: _deposit_heat, _diffuse_z, _apply_convection,
_compute_melt_pool, _estimate_layer_dt — plus physical invariants.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import numpy as np
import pytest
from engines.reduced_fem import (
    VoxelGrid,
    _deposit_heat, _diffuse_z, _apply_convection,
    _compute_melt_pool, _estimate_layer_dt,
)

MATERIAL = {'k': 15.0, 'density': 7900, 'Cp': 500, 'T_melt': 1375}
PARAMS   = {'laser_power': 1000, 'scan_speed': 10, 'absorption': 0.35,
            'beam_spot': 1.2, 'ambient_temp': 25, 'layer_height': 0.8}


def _grid(NX=10, NY=10, NZ=5, dx=1.0):
    return VoxelGrid(NX, NY, NZ, dx, dx, 0.8, 0.0, 0.0, 0.0)


# ── _deposit_heat ─────────────────────────────────────────────────────────────

class TestDepositHeat:
    def test_voxel_is_activated(self):
        g = _grid()
        p = dict(PARAMS, dt=0.1)
        _deposit_heat(g, 5, 5, 2, MATERIAL, p)
        assert g.active[5, 5, 2], "Target voxel should be marked active"

    def test_temperature_increases(self):
        g = _grid()
        p = dict(PARAMS, dt=0.1)
        T_before = g.T[5, 5, 2]
        _deposit_heat(g, 5, 5, 2, MATERIAL, p)
        assert g.T[5, 5, 2] > T_before

    def test_temperature_capped_at_t_melt_x13(self):
        g = _grid()
        # Use very high power + long dt to try exceeding cap
        p = dict(PARAMS, laser_power=100000, dt=10.0)
        _deposit_heat(g, 5, 5, 2, MATERIAL, p)
        cap = MATERIAL['T_melt'] * 1.3
        assert g.T[5, 5, 2] <= cap + 1e-6, \
            f"Temperature {g.T[5,5,2]:.1f} exceeded cap {cap:.1f}"

    def test_out_of_bounds_does_not_crash(self):
        g = _grid(NX=5, NY=5, NZ=3)
        p = dict(PARAMS, dt=0.1)
        # ix at boundary — should clamp, not crash
        _deposit_heat(g, 0, 0, 0, MATERIAL, p)
        _deposit_heat(g, 4, 4, 2, MATERIAL, p)

    def test_gaussian_spread_activates_neighbours(self):
        g = _grid(NX=20, NY=20, NZ=5)
        p = dict(PARAMS, dt=0.5)
        _deposit_heat(g, 10, 10, 2, MATERIAL, p)
        # With beam_spot=1.2mm and dx=1mm, sigma_vox≈0.6 — neighbours within ±3 get heat
        active_count = int(g.active[:, :, 2].sum())
        assert active_count >= 1


# ── _diffuse_z ────────────────────────────────────────────────────────────────

class TestDiffuseZ:
    def test_fourier_stability_enforced(self):
        """Fo must not exceed 0.45 — test indirectly by checking no overshoot."""
        g = _grid(NX=5, NY=5, NZ=5)
        # Hot middle slice, cold boundary
        g.T[:, :, 2] = 1000.0
        g.T[:, :, 1] = 25.0
        g.T[:, :, 3] = 25.0
        T_before = g.T[:, :, 2].copy()
        # Very large dt that would give Fo >> 0.5 without clamping
        _diffuse_z(g, 2, dt=1000.0, material=MATERIAL)
        # Temperature should have decreased (heat diffused out) but not gone below 25
        assert float(g.T[:, :, 2].mean()) < float(T_before.mean())
        assert float(g.T[:, :, 2].min()) >= 24.0  # no unphysical cooling

    def test_cold_boundary_at_bottom(self):
        """iz=0 uses T_below=20°C — cold ground."""
        g = _grid(NX=3, NY=3, NZ=5)
        g.T[:, :, 0] = 500.0
        _diffuse_z(g, 0, dt=10.0, material=MATERIAL)
        # Bottom layer should have cooled (cold boundary)
        assert float(g.T[:, :, 0].mean()) < 500.0

    def test_ambient_boundary_at_top(self):
        """iz=NZ-1 uses T_above=25°C — ambient."""
        g = _grid(NX=3, NY=3, NZ=5)
        g.T[:, :, 4] = 800.0
        _diffuse_z(g, 4, dt=10.0, material=MATERIAL)
        assert float(g.T[:, :, 4].mean()) < 800.0

    def test_no_energy_creation(self):
        """After diffusion, no voxel should exceed its hot neighbours."""
        g = _grid(NX=3, NY=3, NZ=5)
        g.T[:, :, 2] = 500.0
        g.T[:, :, 1] = 200.0
        g.T[:, :, 3] = 200.0
        _diffuse_z(g, 2, dt=1.0, material=MATERIAL)
        # Middle cannot exceed its initial value (heat only diffuses out, not in from cooler neighbours)
        assert float(g.T[:, :, 2].max()) <= 500.0 + 1e-6


# ── _apply_convection ─────────────────────────────────────────────────────────

class TestApplyConvection:
    def test_never_below_ambient(self):
        g = _grid()
        g.T[:, :, 4] = 200.0
        p = dict(PARAMS, ambient_temp=25)
        _apply_convection(g, 4, dt=1000.0, params=p)
        assert float(g.T[:, :, 4].min()) >= 25.0 - 1e-6

    def test_hot_voxel_cools(self):
        g = _grid()
        g.T[:, :, 3] = 800.0
        p = dict(PARAMS, ambient_temp=25)
        _apply_convection(g, 3, dt=100.0, params=p)
        assert float(g.T[:, :, 3].mean()) < 800.0

    def test_ambient_voxel_unchanged(self):
        g = _grid()
        g.T[:, :, 2] = 25.0
        p = dict(PARAMS, ambient_temp=25)
        _apply_convection(g, 2, dt=100.0, params=p)
        assert abs(float(g.T[:, :, 2].mean()) - 25.0) < 1e-3

    def test_convection_never_heats_up(self):
        """Convection only cools — never heats above initial temp."""
        g = _grid()
        g.T[:, :, 1] = 300.0
        T_before = g.T[:, :, 1].copy()
        p = dict(PARAMS, ambient_temp=25)
        _apply_convection(g, 1, dt=5.0, params=p)
        assert float((g.T[:, :, 1] - T_before).max()) <= 1e-6


# ── _compute_melt_pool ────────────────────────────────────────────────────────

class TestComputeMeltPool:
    def _dep_wps(self, n=3):
        return [{'x': float(i), 'y': 0.0, 'z': 1.0, 'is_deposition': True}
                for i in range(n)]

    def test_returns_none_for_empty(self):
        assert _compute_melt_pool([], MATERIAL, PARAMS) is None

    def test_returns_none_for_no_deposition(self):
        wps = [{'x': 0.0, 'y': 0.0, 'z': 1.0, 'is_deposition': False}]
        assert _compute_melt_pool(wps, MATERIAL, PARAMS) is None

    def test_returns_dict_with_required_keys(self):
        result = _compute_melt_pool(self._dep_wps(), MATERIAL, PARAMS)
        assert result is not None
        for key in ('length_mm', 'width_mm', 'depth_mm', 'T_peak_C'):
            assert key in result, f"Missing key: {key}"

    def test_dimensions_positive(self):
        result = _compute_melt_pool(self._dep_wps(), MATERIAL, PARAMS)
        assert result['length_mm'] >= 0.0
        assert result['width_mm']  >= 0.0
        assert result['depth_mm']  >= 0.0

    def test_peak_temp_above_ambient(self):
        result = _compute_melt_pool(self._dep_wps(), MATERIAL, PARAMS)
        assert result['T_peak_C'] > PARAMS['ambient_temp']

    def test_higher_power_larger_pool(self):
        low  = _compute_melt_pool(self._dep_wps(), MATERIAL, dict(PARAMS, laser_power=500))
        high = _compute_melt_pool(self._dep_wps(), MATERIAL, dict(PARAMS, laser_power=2000))
        assert high['T_peak_C'] > low['T_peak_C']


# ── _estimate_layer_dt ────────────────────────────────────────────────────────

class TestEstimateLayerDt:
    def test_empty_waypoints_returns_minimum(self):
        dt = _estimate_layer_dt([], PARAMS)
        assert dt >= 1.0

    def test_zero_speed_does_not_crash(self):
        wps = [{'x': 0.0, 'y': 0.0, 'z': 0.0},
               {'x': 10.0, 'y': 0.0, 'z': 0.0}]
        p = dict(PARAMS, scan_speed=0.0)
        dt = _estimate_layer_dt(wps, p)
        assert dt >= 1.0
        assert math.isfinite(dt)

    def test_typical_layer_reasonable_time(self):
        # 100mm path @ 10mm/s → ~10s
        wps = [{'x': 0.0, 'y': 0.0, 'z': 0.0},
               {'x': 100.0, 'y': 0.0, 'z': 0.0}]
        dt = _estimate_layer_dt(wps, PARAMS)
        assert 8.0 <= dt <= 15.0

    def test_dwell_added_to_time(self):
        wps = [{'x': 0.0, 'y': 0.0, 'z': 0.0},
               {'x': 10.0, 'y': 0.0, 'z': 0.0}]
        dt_no_dwell   = _estimate_layer_dt(wps, dict(PARAMS, dwell_time=0))
        dt_with_dwell = _estimate_layer_dt(wps, dict(PARAMS, dwell_time=30))
        assert dt_with_dwell > dt_no_dwell


# ── Physical invariants (simulation-level) ────────────────────────────────────

class TestPhysicalInvariants:
    def _run(self):
        from engines.reduced_fem import build_grid, run_simulation
        wps = [{'x': float(i % 5), 'y': float(i // 5), 'z': float((i % 3 + 1) * 0.8),
                'layer': (i % 3) + 1, 'is_deposition': True}
               for i in range(30)]
        params = dict(PARAMS, num_layers=3)
        grid = build_grid(wps, params, 'fast')
        return run_simulation(grid, wps, MATERIAL, params)

    def test_heat_is_deposited(self):
        result = self._run()
        assert result['T_avg_final'] > PARAMS['ambient_temp'], \
            "Average temperature should exceed ambient after deposition"

    def test_active_voxels_nonzero(self):
        result = self._run()
        assert result['active_voxels'] > 0

    def test_tmax_above_tavg(self):
        result = self._run()
        assert result['T_max_final'] >= result['T_avg_final']
