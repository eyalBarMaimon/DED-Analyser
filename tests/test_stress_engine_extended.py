"""
Extended stress engine tests: _rosenthal_cooling_rate, compute_stress stress_wps fields,
run_sensitivity_sweep empty waypoints, _group_by_layer clamping.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import pytest
from engines.stress_engine import (
    _rosenthal_cooling_rate, compute_stress, run_sensitivity_sweep,
)
from engines.reduced_fem import _group_by_layer


MATERIAL = {'E_GPa': 193, 'yield_MPa': 310, 'alpha_1e6': 16.0,
            'k': 15.0, 'density': 7900, 'Cp': 500, 'T_melt': 1375}
PROCESS  = {'laser_power': 1500, 'scan_speed': 10, 'wire_feed_speed': 80,
            'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
            'ambient_temp': 25, 'dwell_time': 10, 'wire_diameter': 1.2}
GEOMETRY = {'num_layers': 10, 'wall_thickness': 5.0}
PAYLOAD  = {'material': MATERIAL, 'process': PROCESS, 'geometry': GEOMETRY, 'waypoints': []}


# ── _rosenthal_cooling_rate ───────────────────────────────────────────────────

class TestRosenthalCoolingRate:
    def test_zero_power_returns_fallback(self):
        assert _rosenthal_cooling_rate(0, 0.35, 0.01, 15, 4e-6) == 1000.0

    def test_zero_conductivity_returns_fallback(self):
        assert _rosenthal_cooling_rate(1500, 0.35, 0.01, 0, 4e-6) == 1000.0

    def test_zero_scan_speed_returns_fallback(self):
        assert _rosenthal_cooling_rate(1500, 0.35, 0, 15, 4e-6) == 1000.0

    def test_typical_params_reasonable_range(self):
        # SS316L: P=1500W, v=10mm/s=0.01m/s, k=15, alpha=4e-6 m²/s
        rate = _rosenthal_cooling_rate(1500, 0.35, 0.01, 15, 4e-6, r_m=0.001)
        assert 100 < rate < 1e7, f"Cooling rate {rate:.0f} K/s out of expected range"

    def test_larger_distance_lower_rate(self):
        r_near = _rosenthal_cooling_rate(1500, 0.35, 0.01, 15, 4e-6, r_m=0.0005)
        r_far  = _rosenthal_cooling_rate(1500, 0.35, 0.01, 15, 4e-6, r_m=0.002)
        assert r_near > r_far, "Cooling rate should decrease with distance from melt pool"

    def test_higher_power_higher_rate(self):
        low  = _rosenthal_cooling_rate(500,  0.35, 0.01, 15, 4e-6)
        high = _rosenthal_cooling_rate(2000, 0.35, 0.01, 15, 4e-6)
        assert high > low

    def test_result_always_finite(self):
        rate = _rosenthal_cooling_rate(1500, 0.35, 0.01, 15, 4e-6)
        assert math.isfinite(rate)

    def test_minimum_clamp_applied(self):
        # Very small power should still return ≥ 10.0
        rate = _rosenthal_cooling_rate(0.001, 0.35, 0.01, 15, 4e-6)
        assert rate >= 10.0


# ── compute_stress — stress_wps displacement fields ───────────────────────────

class TestStressWpsFields:
    def _wps(self, n=10):
        return [{'x': float(i), 'y': 0.0, 'z': float(i % 5) * 0.5,
                 'layer': (i % 5) + 1} for i in range(n)]

    def test_stress_wps_populated_with_waypoints(self):
        payload = dict(PAYLOAD, waypoints=self._wps())
        result = compute_stress(payload)
        assert len(result.get('stress_wps', [])) > 0

    def test_stress_wps_has_displacement_fields(self):
        payload = dict(PAYLOAD, waypoints=self._wps())
        result = compute_stress(payload)
        for wp in result['stress_wps'][:5]:
            for field in ('disp_x', 'disp_y', 'disp_z', 'sigma_MPa'):
                assert field in wp, f"Missing field '{field}' in stress_wps entry"

    def test_stress_wps_displacements_finite(self):
        payload = dict(PAYLOAD, waypoints=self._wps())
        result = compute_stress(payload)
        for wp in result['stress_wps']:
            assert math.isfinite(wp['disp_x'])
            assert math.isfinite(wp['disp_y'])
            assert math.isfinite(wp['disp_z'])


# ── run_sensitivity_sweep — empty waypoints ───────────────────────────────────

class TestSensitivitySweepEdgeCases:
    def test_empty_waypoints_does_not_crash(self):
        result = run_sensitivity_sweep(PAYLOAD)
        assert result is not None
        assert 'rows' in result

    def test_explicit_empty_waypoints_key(self):
        payload = dict(PAYLOAD, waypoints=[])
        result = run_sensitivity_sweep(payload)
        assert isinstance(result.get('rows'), list)

    def test_no_waypoints_key(self):
        payload = {k: v for k, v in PAYLOAD.items() if k != 'waypoints'}
        result = run_sensitivity_sweep(payload)
        assert result is not None


# ── _group_by_layer — clamping ────────────────────────────────────────────────

class TestGroupByLayer:
    def test_layer_zero_clamped_to_index_0(self):
        wps = [{'x': 0.0, 'y': 0.0, 'z': 0.0, 'layer': 0}]
        result = _group_by_layer(wps, num_layers=5)
        assert 0 in result
        assert len(result[0]) == 1

    def test_negative_layer_clamped_to_index_0(self):
        wps = [{'x': 0.0, 'y': 0.0, 'z': 0.0, 'layer': -10}]
        result = _group_by_layer(wps, num_layers=5)
        assert 0 in result

    def test_layer_exceeds_num_layers_clamped_to_last(self):
        wps = [{'x': 0.0, 'y': 0.0, 'z': 0.0, 'layer': 999}]
        result = _group_by_layer(wps, num_layers=5)
        assert 4 in result   # clamped to num_layers-1

    def test_normal_layer_1_maps_to_index_0(self):
        wps = [{'x': 0.0, 'y': 0.0, 'z': 0.0, 'layer': 1}]
        result = _group_by_layer(wps, num_layers=5)
        assert 0 in result

    def test_missing_layer_key_defaults_to_1(self):
        wps = [{'x': 0.0, 'y': 0.0, 'z': 0.0}]   # no 'layer' key
        result = _group_by_layer(wps, num_layers=5)
        # layer defaults to 1 → index 0
        assert 0 in result

    def test_all_layers_distributed_correctly(self):
        wps = [{'x': 0.0, 'y': 0.0, 'z': 0.0, 'layer': i} for i in range(1, 6)]
        result = _group_by_layer(wps, num_layers=5)
        assert set(result.keys()) == {0, 1, 2, 3, 4}


# ── Aluminium 6061 in MECH_PROPS ─────────────────────────────────────────────

class TestAluminiumMechProps:
    def test_aluminium_6061_in_mech_props(self):
        from engines.stress_engine import MECH_PROPS
        assert 'Aluminium 6061' in MECH_PROPS

    def test_aluminium_props_values(self):
        from engines.stress_engine import MECH_PROPS
        props = MECH_PROPS['Aluminium 6061']
        assert props['E_GPa']     == 68
        assert props['yield_MPa'] == 276
        assert abs(props['alpha_1e6'] - 23.6) < 0.01

    def test_lookup_mech_finds_aluminium(self):
        from engines.stress_engine import lookup_mech
        props = lookup_mech('Aluminium 6061')
        assert props.get('E_GPa') == 68, "lookup_mech returned empty — Al missing from MECH_PROPS"

    def test_compute_stress_does_not_crash_for_aluminium(self):
        from engines.stress_engine import compute_stress
        al_mat = {'E_GPa': 68, 'yield_MPa': 276, 'alpha_1e6': 23.6,
                  'k': 167.0, 'density': 2700, 'Cp': 896, 'T_melt': 652}
        process = {'laser_power': 800, 'scan_speed': 15, 'wire_feed_speed': 60,
                   'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.22,
                   'ambient_temp': 25, 'dwell_time': 0, 'wire_diameter': 1.2}
        payload = {'material': al_mat, 'process': process,
                   'geometry': {'num_layers': 5, 'wall_thickness': 3.0},
                   'waypoints': []}
        result = compute_stress(payload)
        assert result.get('ok') is True
        assert result['summary']['max_sigma_MPa'] > 0
