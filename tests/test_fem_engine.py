"""
Tests for engines/reduced_fem.py — simulation logic beyond build_grid.
Covers: _voxel_caps, _classify_risk, _sample_voxels, run_simulation.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest
from engines.reduced_fem import (
    VoxelGrid, build_grid, run_simulation,
    _voxel_caps, _classify_risk, _sample_voxels,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_grid(NX=10, NY=10, NZ=5, dx=1.0):
    return VoxelGrid(NX, NY, NZ, dx, dx, 0.8, 0.0, 0.0, 0.0)

MATERIAL = {
    'k': 15.0, 'density': 7900, 'Cp': 500, 'T_melt': 1375,
}

PARAMS = {
    'laser_power': 1000, 'scan_speed': 10, 'absorption': 0.35,
    'beam_spot': 1.2, 'ambient_temp': 25, 'layer_height': 0.8,
    'bead_width': 2.0, 'num_layers': 3, 'dwell_time': 0,
}

def _make_waypoints(num_layers=3):
    wps = []
    for layer in range(1, num_layers + 1):
        z = layer * 0.8
        for x, y in [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]:
            wps.append({'x': x, 'y': y, 'z': z, 'layer': layer, 'is_deposition': True})
    return wps


# ── _voxel_caps ───────────────────────────────────────────────────────────────

class TestVoxelCaps:
    def test_standard_1mm_baseline(self):
        g = _make_grid(dx=1.0)
        snap, final = _voxel_caps(g)
        assert snap  == 1000
        assert final == 5000

    def test_fast_2mm_coarser(self):
        g = _make_grid(dx=2.0)
        snap, final = _voxel_caps(g)
        # scale = 1/4 → 250, clamped to min 500
        assert snap  == 500
        assert final == 2000

    def test_fine_05mm_finer(self):
        g = _make_grid(dx=0.5)
        snap, final = _voxel_caps(g)
        # scale = 4 → 4000 snap, 20000 final
        assert snap  == 4000
        assert final == 20000

    def test_caps_never_exceed_max(self):
        g = _make_grid(dx=0.1)   # very fine — would exceed if uncapped
        snap, final = _voxel_caps(g)
        assert snap  <= 5000
        assert final <= 20000

    def test_caps_never_below_min(self):
        g = _make_grid(dx=10.0)  # very coarse
        snap, final = _voxel_caps(g)
        assert snap  >= 500
        assert final >= 2000


# ── _classify_risk ────────────────────────────────────────────────────────────

class TestClassifyRisk:
    def _grid_with_voxels(self):
        g = _make_grid(NX=3, NY=3, NZ=3)
        # Voxel (0,0,0): above T_melt → HIGH (T_peak must match T for test to work)
        g.active[0, 0, 0] = True
        g.T[0, 0, 0] = 1400.0; g.T_peak[0, 0, 0] = 1400.0  # > T_melt (1375)
        g.cool_rate[0, 0, 0] = 10.0
        g.n_remelt[0, 0, 0] = 0
        # Voxel (1,1,1): high cool rate → HIGH
        g.active[1, 1, 1] = True
        g.T[1, 1, 1] = 500.0; g.T_peak[1, 1, 1] = 500.0
        g.cool_rate[1, 1, 1] = 600.0   # > 500
        g.n_remelt[1, 1, 1] = 0
        # Voxel (2,2,2): above solidus → MEDIUM
        g.active[2, 2, 2] = True
        g.T[2, 2, 2] = 1200.0; g.T_peak[2, 2, 2] = 1200.0  # > 0.85 * 1375 = 1168.75
        g.cool_rate[2, 2, 2] = 10.0
        g.n_remelt[2, 2, 2] = 0
        # Voxel (0,1,0): remelt once → LOW (thermal metrics low)
        g.active[0, 1, 0] = True
        g.T[0, 1, 0] = 100.0; g.T_peak[0, 1, 0] = 100.0
        g.cool_rate[0, 1, 0] = 10.0
        g.n_remelt[0, 1, 0] = 1
        # Voxel (1,0,2): normal → LOW
        g.active[1, 0, 2] = True
        g.T[1, 0, 2] = 200.0; g.T_peak[1, 0, 2] = 200.0
        g.cool_rate[1, 0, 2] = 10.0
        g.n_remelt[1, 0, 2] = 0
        return g

    def test_high_above_tmelt(self):
        g = self._grid_with_voxels()
        risks = _classify_risk(g, MATERIAL)
        high = [r for r in risks if r['ix'] == 0 and r['iy'] == 0 and r['iz'] == 0]
        assert len(high) == 1
        assert high[0]['severity'] == 'HIGH'

    def test_high_cool_rate(self):
        g = self._grid_with_voxels()
        risks = _classify_risk(g, MATERIAL)
        high = [r for r in risks if r['ix'] == 1 and r['iy'] == 1 and r['iz'] == 1]
        assert high[0]['severity'] == 'HIGH'

    def test_medium_above_solidus(self):
        g = self._grid_with_voxels()
        risks = _classify_risk(g, MATERIAL)
        med = [r for r in risks if r['ix'] == 2 and r['iy'] == 2 and r['iz'] == 2]
        assert med[0]['severity'] == 'MEDIUM'

    def test_remelt_alone_not_medium(self):
        """n_remelt alone must not elevate a cold voxel — only T and cool_rate decide."""
        g = self._grid_with_voxels()
        risks = _classify_risk(g, MATERIAL)
        # Voxel (0,1,0): T=100°C (below solidus 1168°C), cr=10 (<100), n_remelt=1
        vox = [r for r in risks if r['ix'] == 0 and r['iy'] == 1 and r['iz'] == 0]
        assert vox[0]['severity'] == 'LOW'
        assert vox[0]['n_remelt'] == 1   # still reported

    def test_low_normal(self):
        g = self._grid_with_voxels()
        risks = _classify_risk(g, MATERIAL)
        low = [r for r in risks if r['ix'] == 1 and r['iy'] == 0 and r['iz'] == 2]
        assert low[0]['severity'] == 'LOW'

    def test_only_active_voxels_returned(self):
        g = self._grid_with_voxels()
        risks = _classify_risk(g, MATERIAL)
        assert len(risks) == 5   # exactly the 5 active voxels

    def test_world_coords_correct(self):
        g = self._grid_with_voxels()
        risks = _classify_risk(g, MATERIAL)
        r = next(r for r in risks if r['ix'] == 1 and r['iy'] == 1 and r['iz'] == 1)
        assert abs(r['x'] - (g.x_min + 1 * g.dx)) < 0.01
        assert abs(r['y'] - (g.y_min + 1 * g.dy)) < 0.01

    def test_cooled_voxel_still_high_via_t_peak(self):
        """Voxel that reached T_melt then cooled must still be HIGH (uses T_peak)."""
        g = _make_grid(NX=2, NY=2, NZ=2)
        g.active[0, 0, 0] = True
        g.T[0, 0, 0] = 100.0       # cooled down now
        g.T_peak[0, 0, 0] = 1400.0  # but peaked above T_melt (1375)
        g.cool_rate[0, 0, 0] = 5.0
        risks = _classify_risk(g, MATERIAL)
        assert risks[0]['severity'] == 'HIGH', "Cooled voxel that peaked above T_melt must be HIGH"

    def test_high_remelt_does_not_override_temperature_low(self):
        """Many remelts but low T and low cr must remain LOW."""
        g = _make_grid(NX=2, NY=2, NZ=2)
        g.active[0, 0, 0] = True
        g.T[0, 0, 0] = 100.0
        g.cool_rate[0, 0, 0] = 5.0
        g.n_remelt[0, 0, 0] = 50
        risks = _classify_risk(g, MATERIAL)
        assert risks[0]['severity'] == 'LOW'
        assert risks[0]['n_remelt'] == 50

    def test_remelt_reported_in_all_voxels(self):
        """n_remelt must be present in every risk dict regardless of severity."""
        g = self._grid_with_voxels()
        risks = _classify_risk(g, MATERIAL)
        for r in risks:
            assert 'n_remelt' in r


# ── _sample_voxels ────────────────────────────────────────────────────────────

class TestSampleVoxels:
    def _grid_full(self, N=20):
        """Grid with all voxels active at various temperatures."""
        g = _make_grid(NX=N, NY=N, NZ=N)
        g.active[:] = True
        # Temperature varies by position
        for ix in range(N):
            g.T[ix, :, :] = 25.0 + ix * 50
        return g

    def test_returns_all_keys(self):
        g = _make_grid(NX=3, NY=3, NZ=3)
        g.active[0, 0, 0] = True
        g.T[0, 0, 0] = 500.0
        result = _sample_voxels(g, max_voxels=100)
        assert set(result.keys()) == {'x', 'y', 'z', 't', 'severity', 'remelts', 'overshoot', 'fatigue'}

    def test_empty_grid_returns_empty(self):
        g = _make_grid()
        result = _sample_voxels(g, max_voxels=100)
        assert result['x'] == []
        assert result['severity'] == []

    def test_no_sampling_when_few_voxels(self):
        g = _make_grid(NX=3, NY=3, NZ=3)
        g.active[0, 0, 0] = True
        g.active[1, 1, 1] = True
        g.T[0, 0, 0] = 100.0
        g.T[1, 1, 1] = 200.0
        result = _sample_voxels(g, max_voxels=100)
        assert len(result['x']) == 2

    def test_sampling_respects_max(self):
        g = self._grid_full(N=20)  # 8000 active voxels
        result = _sample_voxels(g, max_voxels=500)
        assert len(result['x']) <= 500

    def test_sampling_uniform_distribution(self):
        """Shuffled sampling should spread across full X range, not cluster."""
        g = self._grid_full(N=20)
        result = _sample_voxels(g, max_voxels=200)
        xs = result['x']
        x_min, x_max = min(xs), max(xs)
        # With 20 steps of 1mm, X range should be 0–19; sampled points should span it
        assert x_max - x_min > 10.0, "Sampling is not spatially uniform (column artifact)"

    def test_severity_with_material(self):
        g = _make_grid(NX=2, NY=2, NZ=2)
        g.active[0, 0, 0] = True
        g.T[0, 0, 0] = 1400.0; g.T_peak[0, 0, 0] = 1400.0   # above T_melt → HIGH
        g.active[1, 1, 1] = True
        g.T[1, 1, 1] = 100.0; g.T_peak[1, 1, 1] = 100.0     # LOW
        result = _sample_voxels(g, max_voxels=100, material=MATERIAL)
        sevs = dict(zip(
            [round(x, 1) for x in result['x']],
            result['severity']
        ))
        # The HIGH voxel is at ix=0 → x=0.0
        assert sevs.get(0.0) == 'HIGH'

    def test_remelts_returned(self):
        g = _make_grid(NX=2, NY=2, NZ=2)
        g.active[0, 0, 0] = True
        g.n_remelt[0, 0, 0] = 3
        result = _sample_voxels(g, max_voxels=100)
        assert result['remelts'][0] == 3

    def test_reproducible_seed(self):
        g = self._grid_full(N=15)
        r1 = _sample_voxels(g, max_voxels=100)
        r2 = _sample_voxels(g, max_voxels=100)
        assert r1['x'] == r2['x']   # seed=42 → deterministic

    def test_remelt_does_not_affect_severity_in_sample(self):
        """In _sample_voxels, remelt count must not elevate cold voxels."""
        g = _make_grid(NX=2, NY=2, NZ=2)
        g.active[0, 0, 0] = True
        g.T[0, 0, 0] = 100.0
        g.cool_rate[0, 0, 0] = 5.0
        g.n_remelt[0, 0, 0] = 30
        result = _sample_voxels(g, max_voxels=100, material=MATERIAL)
        assert result['severity'][0] == 'LOW'
        assert result['remelts'][0] == 30


# ── run_simulation ────────────────────────────────────────────────────────────

class TestRunSimulation:
    def _setup(self):
        wps = _make_waypoints(num_layers=3)
        grid = build_grid(wps, PARAMS, 'fast')
        return grid, wps

    def test_returns_ok(self):
        grid, wps = self._setup()
        result = run_simulation(grid, wps, MATERIAL, PARAMS)
        assert result['ok'] is True

    def test_required_keys_present(self):
        grid, wps = self._setup()
        result = run_simulation(grid, wps, MATERIAL, PARAMS)
        for key in ('element_size_mm', 'grid_shape', 'grid_spacing', 'active_voxels',
                    'T_max_final', 'T_avg_final', 'max_cool_rate', 'max_remelt',
                    'risk_counts', 'snapshots', 'num_layers', 'final_voxels'):
            assert key in result, f"Missing key: {key}"

    def test_snapshot_count_equals_layers(self):
        grid, wps = self._setup()
        result = run_simulation(grid, wps, MATERIAL, PARAMS)
        assert result['num_layers'] == len(result['snapshots'])
        assert result['num_layers'] == grid.NZ

    def test_snapshot_keys(self):
        grid, wps = self._setup()
        result = run_simulation(grid, wps, MATERIAL, PARAMS)
        snap = result['snapshots'][-1]
        for key in ('layer', 't_elapsed', 'T_max', 'T_avg', 'active_count', 'voxels'):
            assert key in snap

    def test_active_voxels_positive(self):
        grid, wps = self._setup()
        result = run_simulation(grid, wps, MATERIAL, PARAMS)
        assert result['active_voxels'] > 0

    def test_temperature_above_ambient(self):
        grid, wps = self._setup()
        result = run_simulation(grid, wps, MATERIAL, PARAMS)
        assert result['T_max_final'] > PARAMS['ambient_temp']

    def test_risk_counts_sum_to_active(self):
        grid, wps = self._setup()
        result = run_simulation(grid, wps, MATERIAL, PARAMS)
        rc = result['risk_counts']
        total_risk = rc['HIGH'] + rc['MEDIUM'] + rc['LOW']
        # risk_voxels is capped at 2000, active_voxels may be larger
        assert total_risk <= result['active_voxels']

    def test_final_voxels_has_severity(self):
        grid, wps = self._setup()
        result = run_simulation(grid, wps, MATERIAL, PARAMS)
        fv = result['final_voxels']
        assert 'severity' in fv
        assert 'remelts' in fv

    def test_progress_callback_fires(self):
        grid, wps = self._setup()
        calls = []
        def cb(pct, msg):
            calls.append(pct)
        run_simulation(grid, wps, MATERIAL, PARAMS, progress_cb=cb)
        assert len(calls) == grid.NZ
        assert calls[0] == 0.0
