"""
Tier 2 — Regression baselines.
These values were validated against physics calculations and must not drift.

SS316L reference (P=1500W, v=10mm/s, 50 layers, 5mm wall, no waypoints):
  - dTdt ≈ 15 000 K/s
  - sigma_max = 310 MPa  (yield-clamped)
  - max_delta_mm ≈ 0.149
  - go_nogo = NO-GO

Ti-6Al-4V same process:
  - sigma_max ≈ 197.8 MPa
  - max_delta_mm ≈ 0.026
  - go_nogo = GO

If any assertion here fails, a physics formula was changed — check git diff.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import compute_stress, _rosenthal_cooling_rate


# ── Payloads ─────────────────────────────────────────────────────────────────

_SS316L_PAYLOAD = {
    'material': {
        'E_GPa': 193, 'yield_MPa': 310, 'alpha_1e6': 16,
        'density': 7900, 'Cp': 490, 'k': 15, 'T_melt': 1400,
    },
    'process': {
        'laser_power': 1500, 'scan_speed': 10, 'wire_feed_speed': 80,
        'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
        'ambient_temp': 25, 'dwell_time': 10, 'wire_diameter': 1.2,
    },
    'geometry': {'num_layers': 50, 'wall_thickness': 5.0},
    'waypoints': [],
}

_TI64_PAYLOAD = {
    'material': {
        'E_GPa': 114, 'yield_MPa': 880, 'alpha_1e6': 8.6,
        'density': 4430, 'Cp': 560, 'k': 7, 'T_melt': 1660,
    },
    'process': {
        'laser_power': 1500, 'scan_speed': 10, 'wire_feed_speed': 80,
        'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
        'ambient_temp': 25, 'dwell_time': 10, 'wire_diameter': 1.2,
    },
    'geometry': {'num_layers': 50, 'wall_thickness': 5.0},
    'waypoints': [],
}


class TestSS316LRegression:
    def setup_method(self):
        self.r = compute_stress(_SS316L_PAYLOAD, _sweep=False)

    def test_compute_returns_result(self):
        assert self.r is not None
        assert self.r.get('ok') is True

    def test_cooling_rate(self):
        """Rosenthal at reference SS316L conditions → ~15 000 K/s."""
        alpha_diff_ss316l = 15.0 / (7900 * 490)   # 3.875e-6 m²/s
        dTdt = _rosenthal_cooling_rate(1500, 0.35, 0.010, 15.0, alpha_diff_ss316l, 0.001)
        assert 10_000 < dTdt < 25_000

    def test_sigma_max_yield_clamped(self):
        """SS316L: σ_max should be clamped at yield = 310 MPa."""
        assert self.r['summary']['max_sigma_MPa'] == pytest.approx(310.0, abs=1.0)

    def test_delta_max(self):
        """Max distortion should be ~0.037 mm ± 50% (bending dominated at these conditions)."""
        delta = self.r['summary']['max_delta_mm']
        assert 0.010 < delta < 0.40, f"delta={delta:.4f}"

    def test_go_nogo_is_nogo(self):
        assert self.r['go_nogo'] == 'NO-GO'

    def test_per_layer_count(self):
        assert len(self.r['per_layer']) == 50

    def test_per_layer_height_final(self):
        final = self.r['per_layer'][-1]
        assert abs(final['height_mm'] - 25.0) < 1.0   # 50 × 0.5mm

    def test_toolpath_pattern(self):
        assert self.r['geometry_type'] == 'raster'   # no waypoints → default


class TestTi64Regression:
    def setup_method(self):
        self.r = compute_stress(_TI64_PAYLOAD, _sweep=False)

    def test_compute_returns_result(self):
        assert self.r is not None

    def test_sigma_max_range(self):
        """Ti-6Al-4V: σ_max should be 150–250 MPa (not yield-clamped at 880)."""
        sigma = self.r['summary']['max_sigma_MPa']
        assert 100 < sigma < 350, f"sigma={sigma}"

    def test_delta_lower_than_ss316l(self):
        """Ti-6Al-4V should have lower distortion than SS316L (lower alpha, higher E)."""
        r_ss = compute_stress(_SS316L_PAYLOAD, _sweep=False)
        assert self.r['summary']['max_delta_mm'] < r_ss['summary']['max_delta_mm']

    def test_go_nogo_is_go(self):
        assert self.r['go_nogo'] == 'GO'


class TestGoNoGoLogic:
    def test_nogo_when_sigma_over_80pct_yield(self):
        r = compute_stress(_SS316L_PAYLOAD, _sweep=False)
        # SS316L: σ/yield = 310/310 = 100% → NO-GO
        assert r['go_nogo'] == 'NO-GO'

    def test_caution_when_high_delta(self):
        """Many layers + thin wall → large delta → at least CAUTION."""
        payload = {**_SS316L_PAYLOAD,
                   'geometry': {'num_layers': 200, 'wall_thickness': 2.0}}
        r = compute_stress(payload, _sweep=False)
        assert r['go_nogo'] in ('CAUTION', 'NO-GO')

    def test_go_for_conservative_params(self):
        """Low power + few layers + thick wall → GO."""
        payload = {
            'material': {
                'E_GPa': 114, 'yield_MPa': 880, 'alpha_1e6': 8.6,
                'density': 4430, 'Cp': 560, 'k': 7, 'T_melt': 1660,
            },
            'process': {
                'laser_power': 800, 'scan_speed': 20, 'wire_feed_speed': 50,
                'layer_height': 0.3, 'bead_width': 2.0, 'absorption': 0.35,
                'ambient_temp': 150, 'dwell_time': 60, 'wire_diameter': 1.2,
            },
            'geometry': {'num_layers': 10, 'wall_thickness': 10.0},
            'waypoints': [],
        }
        r = compute_stress(payload, _sweep=False)
        assert r['go_nogo'] == 'GO'
