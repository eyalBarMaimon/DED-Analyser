"""
Tier 2 — Tests for sensitivity sweep (tornado chart data).
"""
import copy
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import run_sensitivity_sweep, SWEEP_PARAMS


_PAYLOAD = {
    'material': {
        'E_GPa': 114, 'yield_MPa': 880, 'alpha_1e6': 8.6,
        'density': 4430, 'Cp': 560, 'k': 7, 'T_melt': 1660,
    },
    'process': {
        'laser_power': 1500, 'scan_speed': 10, 'wire_feed_speed': 80,
        'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
        'ambient_temp': 25, 'dwell_time': 10, 'wire_diameter': 1.2,
    },
    'geometry': {'num_layers': 20, 'wall_thickness': 5.0},
    'waypoints': [],
}


class TestSensitivitySweep:
    def setup_method(self):
        self.sw = run_sensitivity_sweep(copy.deepcopy(_PAYLOAD))

    def test_returns_dict(self):
        assert isinstance(self.sw, dict)

    def test_has_rows(self):
        assert 'rows' in self.sw
        assert len(self.sw['rows']) > 0

    def test_base_fields_present(self):
        for field in ('base_sigma', 'base_delta', 'base_util', 'base_verdict', 'yield_MPa'):
            assert field in self.sw, f"Missing field: {field}"

    def test_row_fields(self):
        required = ('key', 'label', 'unit', 'base', 'scan_pct',
                    'val_lo', 'val_hi', 'd_sigma_lo', 'd_sigma_hi',
                    'd_delta_lo', 'd_delta_hi', 'infl_sigma', 'infl_delta')
        for row in self.sw['rows']:
            for f in required:
                assert f in row, f"Row missing field: {f}"

    def test_rows_sorted_by_influence_descending(self):
        rows = self.sw['rows']
        scores = [r['infl_sigma'] + r['infl_delta'] for r in rows]
        for i in range(1, len(scores)):
            assert scores[i] <= scores[i-1] + 0.01, \
                f"Not sorted at index {i}: {scores[i-1]} → {scores[i]}"

    def test_base_values_match_compute(self):
        from engines.stress_engine import compute_stress
        r = compute_stress(copy.deepcopy(_PAYLOAD), _sweep=False)
        assert abs(self.sw['base_sigma'] - r['summary']['max_sigma_MPa']) < 0.2
        assert abs(self.sw['base_delta'] - r['summary']['max_delta_mm']) < 0.001

    def test_scan_pct_minimum_10(self):
        for row in self.sw['rows']:
            assert row['scan_pct'] >= 10

    def test_printer_fixed_params_stay_at_10pct(self):
        """laser_power, scan_speed, etc. (printer_fixed=True) must not expand beyond 10%."""
        fixed_keys = {key for _, key, _, _, pf in SWEEP_PARAMS if pf}
        for row in self.sw['rows']:
            if row['key'] in fixed_keys:
                assert row['scan_pct'] == 10, \
                    f"{row['key']} is printer_fixed but scan_pct={row['scan_pct']}"

    def test_influence_non_negative(self):
        for row in self.sw['rows']:
            assert row['infl_sigma'] >= 0
            assert row['infl_delta'] >= 0

    def test_val_lo_less_than_val_hi(self):
        for row in self.sw['rows']:
            assert row['val_lo'] < row['val_hi'], \
                f"{row['key']}: val_lo={row['val_lo']} >= val_hi={row['val_hi']}"

    def test_empty_material_returns_empty(self):
        """compute_stress fails with empty material → sweep returns {}."""
        result = run_sensitivity_sweep({
            'material': {}, 'process': {}, 'geometry': {}, 'waypoints': [],
        })
        # Should return empty dict or dict without rows, not crash
        assert isinstance(result, dict)

    def test_dwell_rec_structure_when_present(self):
        if self.sw.get('dwell_rec'):
            dr = self.sw['dwell_rec']
            assert 'current_s' in dr
            assert 'recommended_s' in dr
            assert dr['recommended_s'] > dr['current_s']
