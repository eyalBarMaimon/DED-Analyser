"""
Tests for engines/reduced_fem.py — build_grid and resolution behaviour.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from engines.reduced_fem import build_grid, RESOLUTION_ELEMENT_SIZE, MAX_ELEMENTS


def _make_waypoints(x_span=50.0, y_span=40.0, num_layers=10, layer_height=0.8):
    """Return a minimal waypoint list covering an x_span × y_span × num_layers domain."""
    wps = []
    for layer in range(1, num_layers + 1):
        z = layer * layer_height
        wps.append({'x': 0.0,     'y': 0.0,     'z': z, 'layer': layer, 'is_deposition': True})
        wps.append({'x': x_span,  'y': y_span,  'z': z, 'layer': layer, 'is_deposition': True})
    return wps


PARAMS = {
    'bead_width':   2.0,
    'layer_height': 0.8,
    'num_layers':   10,
}


class TestBuildGridResolution:

    def test_fast_coarser_than_standard(self):
        """Fast grid must have fewer XY cells than standard."""
        wps = _make_waypoints()
        fast     = build_grid(wps, PARAMS, 'fast')
        standard = build_grid(wps, PARAMS, 'standard')
        assert fast.NX <= standard.NX
        assert fast.NY <= standard.NY
        assert fast.NX * fast.NY < standard.NX * standard.NY

    def test_fine_finer_than_standard(self):
        """Fine grid must have more XY cells than standard."""
        wps = _make_waypoints()
        standard = build_grid(wps, PARAMS, 'standard')
        fine     = build_grid(wps, PARAMS, 'fine')
        assert fine.NX >= standard.NX
        assert fine.NY >= standard.NY
        assert fine.NX * fine.NY > standard.NX * standard.NY

    def test_element_size_fast(self):
        """Fast element size ≈ RESOLUTION_ELEMENT_SIZE['fast']."""
        wps = _make_waypoints(x_span=100.0, y_span=100.0)
        g = build_grid(wps, PARAMS, 'fast')
        expected = RESOLUTION_ELEMENT_SIZE['fast']
        assert abs(g.dx - expected) < expected * 0.15  # within 15% (cap may adjust slightly)
        assert abs(g.dy - expected) < expected * 0.15

    def test_element_size_standard(self):
        """Standard element size ≈ RESOLUTION_ELEMENT_SIZE['standard']."""
        wps = _make_waypoints(x_span=100.0, y_span=100.0)
        g = build_grid(wps, PARAMS, 'standard')
        expected = RESOLUTION_ELEMENT_SIZE['standard']
        assert abs(g.dx - expected) < expected * 0.15
        assert abs(g.dy - expected) < expected * 0.15

    def test_element_size_fine(self):
        """Fine element size ≈ RESOLUTION_ELEMENT_SIZE['fine']."""
        wps = _make_waypoints(x_span=100.0, y_span=100.0)
        g = build_grid(wps, PARAMS, 'fine')
        expected = RESOLUTION_ELEMENT_SIZE['fine']
        assert abs(g.dx - expected) < expected * 0.15
        assert abs(g.dy - expected) < expected * 0.15

    def test_ultrafine_finer_than_fine(self):
        """Ultra-fine grid must have more XY cells than fine."""
        wps = _make_waypoints()
        fine      = build_grid(wps, PARAMS, 'fine')
        ultrafine = build_grid(wps, PARAMS, 'ultrafine')
        assert ultrafine.NX >= fine.NX
        assert ultrafine.NY >= fine.NY
        assert ultrafine.NX * ultrafine.NY > fine.NX * fine.NY

    def test_element_size_ultrafine(self):
        """Ultra-fine element size ≈ 0.25 mm."""
        wps = _make_waypoints(x_span=50.0, y_span=50.0)
        g = build_grid(wps, PARAMS, 'ultrafine')
        expected = RESOLUTION_ELEMENT_SIZE['ultrafine']
        assert abs(g.dx - expected) < expected * 0.15
        assert abs(g.dy - expected) < expected * 0.15

    def test_nz_same_across_resolutions(self):
        """NZ (layer count) must be identical regardless of resolution."""
        wps = _make_waypoints()
        fast      = build_grid(wps, PARAMS, 'fast')
        standard  = build_grid(wps, PARAMS, 'standard')
        fine      = build_grid(wps, PARAMS, 'fine')
        ultrafine = build_grid(wps, PARAMS, 'ultrafine')
        assert fast.NZ == standard.NZ == fine.NZ == ultrafine.NZ

    def test_grid_covers_domain(self):
        """Grid extent must cover the full waypoint bounding box (plus padding)."""
        x_span, y_span = 80.0, 60.0
        wps = _make_waypoints(x_span=x_span, y_span=y_span)
        for res in ('fast', 'standard', 'fine', 'ultrafine'):
            g = build_grid(wps, PARAMS, res)
            grid_x_extent = g.NX * g.dx
            grid_y_extent = g.NY * g.dy
            # domain = span + 2×pad; grid must be at least as wide
            pad = PARAMS['bead_width']
            assert grid_x_extent >= x_span, f"{res}: x extent {grid_x_extent:.2f} < {x_span}"
            assert grid_y_extent >= y_span, f"{res}: y extent {grid_y_extent:.2f} < {y_span}"

    def test_cap_not_exceeded(self):
        """NX and NY must never exceed MAX_ELEMENTS even for ultrafine on a large part."""
        wps = _make_waypoints(x_span=500.0, y_span=500.0)
        for res in ('fine', 'ultrafine'):
            g = build_grid(wps, PARAMS, res)
            assert g.NX <= MAX_ELEMENTS, f"{res}: NX={g.NX} > MAX_ELEMENTS"
            assert g.NY <= MAX_ELEMENTS, f"{res}: NY={g.NY} > MAX_ELEMENTS"

    def test_unknown_resolution_falls_back_to_standard(self):
        """An unrecognised resolution string should not crash and behaves like standard."""
        wps = _make_waypoints()
        g_unknown  = build_grid(wps, PARAMS, 'nonexistent')
        g_standard = build_grid(wps, PARAMS, 'standard')
        assert g_unknown.NX == g_standard.NX
        assert g_unknown.NY == g_standard.NY

    def test_arrays_initialised(self):
        """VoxelGrid state arrays must have the correct shape after build_grid."""
        wps = _make_waypoints()
        g = build_grid(wps, PARAMS, 'standard')
        assert g.T.shape        == (g.NX, g.NY, g.NZ)
        assert g.active.shape   == (g.NX, g.NY, g.NZ)
        assert g.T_peak.shape   == (g.NX, g.NY, g.NZ)
        assert g.cool_rate.shape == (g.NX, g.NY, g.NZ)
        assert g.n_remelt.shape  == (g.NX, g.NY, g.NZ)
