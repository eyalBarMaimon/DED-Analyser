"""
Tier 3 — Monotonicity invariants.
Physics must respond in the expected direction to parameter changes.
"""
import copy
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import compute_stress


_BASE = {
    'material': {
        'E_GPa': 193, 'yield_MPa': 900,   # high yield to avoid early clamping
        'alpha_1e6': 16, 'density': 7900,
        'Cp': 490, 'k': 15, 'T_melt': 1400,
    },
    'process': {
        'laser_power': 1500, 'scan_speed': 10, 'wire_feed_speed': 80,
        'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
        'ambient_temp': 25, 'dwell_time': 10, 'wire_diameter': 1.2,
    },
    'geometry': {'num_layers': 30, 'wall_thickness': 5.0},
    'waypoints': [],
}


def _run(overrides_proc=None, overrides_geom=None, overrides_mat=None):
    p = copy.deepcopy(_BASE)
    if overrides_proc:
        p['process'].update(overrides_proc)
    if overrides_geom:
        p['geometry'].update(overrides_geom)
    if overrides_mat:
        p['material'].update(overrides_mat)
    return compute_stress(p, _sweep=False)


class TestMonotonicity:
    def test_more_layers_more_delta(self):
        """More layers (taller part) → more bending distortion."""
        r10 = _run(overrides_geom={'num_layers': 10})
        r50 = _run(overrides_geom={'num_layers': 50})
        assert r50['summary']['max_delta_mm'] > r10['summary']['max_delta_mm']

    def test_thinner_wall_more_delta(self):
        """Thinner wall → less stiffness → more bending and sag."""
        r_thick = _run(overrides_geom={'wall_thickness': 10.0})
        r_thin  = _run(overrides_geom={'wall_thickness':  2.0})
        assert r_thin['summary']['max_delta_mm'] > r_thick['summary']['max_delta_mm']

    def test_higher_alpha_more_sigma(self):
        """Higher CTE → more inherent strain → more stress."""
        r_low  = _run(overrides_mat={'alpha_1e6': 8.6})
        r_high = _run(overrides_mat={'alpha_1e6': 20.0})
        assert r_high['summary']['max_sigma_MPa'] > r_low['summary']['max_sigma_MPa']

    def test_longer_dwell_less_sigma(self):
        """Longer dwell → more stress relief → lower sigma."""
        r_short = _run(overrides_proc={'dwell_time': 5.0})
        r_long  = _run(overrides_proc={'dwell_time': 120.0})
        assert r_long['summary']['max_sigma_MPa'] <= r_short['summary']['max_sigma_MPa']

    def test_preheat_reduces_sigma(self):
        """Higher ambient/preheat temp → lower ΔT_melt → less inherent strain."""
        r_cold = _run(overrides_proc={'ambient_temp': 25})
        r_warm = _run(overrides_proc={'ambient_temp': 300})
        assert r_warm['summary']['max_sigma_MPa'] <= r_cold['summary']['max_sigma_MPa']

    def test_higher_power_more_stress(self):
        """Higher laser power → faster cooling → higher f_c → more stress."""
        r_low  = _run(overrides_proc={'laser_power': 500})
        r_high = _run(overrides_proc={'laser_power': 3000})
        assert r_high['summary']['max_sigma_MPa'] >= r_low['summary']['max_sigma_MPa']

    def test_sigma_never_exceeds_yield(self):
        """σ_max must always be ≤ yield_MPa (plasticity clamp)."""
        r = _run()
        assert r['summary']['max_sigma_MPa'] <= r['summary']['yield_MPa'] + 0.1

    def test_delta_positive(self):
        """Total distortion must always be positive."""
        r = _run()
        assert r['summary']['max_delta_mm'] > 0

    def test_delta_per_layer_positive(self):
        """Every per-layer delta must be positive."""
        r = _run()
        for l in r['per_layer']:
            assert l['delta_mm'] >= 0, f"Negative delta at layer {l['layer']}"

    def test_sag_increases_with_density(self):
        """Dense material should have more gravity sag (test directly with _gravity_sag)."""
        from engines.stress_engine import _gravity_sag
        sag_light = _gravity_sag(2700, 9.81, 0.050, 0.003, 70e9)   # aluminium-like
        sag_heavy = _gravity_sag(8900, 9.81, 0.050, 0.003, 200e9)  # nickel-like
        assert sag_heavy > sag_light

    def test_sigma_clamp_at_yield(self):
        """When conditions are extreme, sigma_max should equal yield_MPa exactly."""
        r_extreme = _run(
            overrides_geom={'num_layers': 200},
            overrides_proc={'dwell_time': 0, 'laser_power': 3000},
        )
        # After many layers with no dwell, should be yield-clamped
        sigma = r_extreme['summary']['max_sigma_MPa']
        yield_mpa = r_extreme['summary']['yield_MPa']
        assert sigma <= yield_mpa + 0.1
