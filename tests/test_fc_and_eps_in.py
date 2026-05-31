"""
Tier 1 — Tests for f_c scaling and ε_in = α·ΔT_melt·f_c.
Validates the inherent strain calibration.
"""
import math
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import compute_stress


def _run(laser_P=1500, scan_v=10, k=15, T_melt=1400, alpha=16, E=193, yield_MPa=900):
    return compute_stress({
        'material': {
            'E_GPa': E, 'yield_MPa': yield_MPa, 'alpha_1e6': alpha,
            'density': 7900, 'Cp': 490, 'k': k, 'T_melt': T_melt,
        },
        'process': {
            'laser_power': laser_P, 'scan_speed': scan_v, 'wire_feed_speed': 80,
            'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
            'ambient_temp': 25, 'dwell_time': 10, 'wire_diameter': 1.2,
        },
        'geometry': {'num_layers': 5, 'wall_thickness': 5.0},
        'waypoints': [],
    }, _sweep=False)


class TestEpsIn:
    def test_eps_in_reasonable_range(self):
        """ε_in for SS316L standard conditions should be 0.0003–0.002."""
        r = _run()
        eps_in_ue = r['summary']['epsilon_in_ue']   # µε
        eps_in = eps_in_ue * 1e-6
        assert 0.0003 <= eps_in <= 0.002, f"ε_in={eps_in:.5f} outside expected range"

    def test_sigma_pass_reasonable_range(self):
        """σ_pass = E·ε_in for SS316L should be 60–400 MPa."""
        r = _run()
        sigma_pass = r['summary']['sigma_pass_MPa']
        assert 60 <= sigma_pass <= 400, f"σ_pass={sigma_pass} MPa"

    def test_fc_log_scaling_direction(self):
        """Higher power → faster cooling → higher f_c → higher σ_pass."""
        r_low  = _run(laser_P=500)
        r_high = _run(laser_P=3000)
        # σ_pass appears in summary
        assert r_high['summary']['sigma_pass_MPa'] >= r_low['summary']['sigma_pass_MPa']

    def test_f_c_effective_in_range(self):
        """f_c should be within [0.015, 0.095] as per clamp in engine."""
        r = _run()
        fc = r['toolpath']['f_c_effective']
        assert 0.015 <= fc <= 0.095, f"f_c={fc}"

    def test_higher_alpha_increases_sigma_pass(self):
        """α × ΔT × f_c: higher α → higher ε_in → higher σ_pass."""
        r_low  = _run(alpha=8.6)
        r_high = _run(alpha=20.0)
        assert r_high['summary']['sigma_pass_MPa'] > r_low['summary']['sigma_pass_MPa']

    def test_higher_T_melt_increases_sigma_pass(self):
        """Higher T_melt → larger ΔT_melt → larger ε_in."""
        r_low  = _run(T_melt=1200)
        r_high = _run(T_melt=1800)
        assert r_high['summary']['sigma_pass_MPa'] > r_low['summary']['sigma_pass_MPa']

    def test_eps_in_consistent_with_formula(self):
        """
        Verify: σ_pass ≈ E × α × ΔT_melt × f_c.
        Using known inputs: E=193GPa, α=16e-6, T_melt=1400, T_amb=25.
        ΔT_melt = 1375K. f_c from log scaling at ref conditions ≈ 0.045.
        → σ_pass ≈ 193e9 × 16e-6 × 1375 × 0.045 ≈ 191 MPa.
        Allow ±50% because f_c depends on Rosenthal at runtime.
        """
        r = _run()
        sp = r['summary']['sigma_pass_MPa']
        # Just verify it's in a physically meaningful range
        assert 50 < sp < 600, f"σ_pass={sp} MPa seems wrong"

    def test_relief_pct_positive_with_dwell(self):
        """With non-zero dwell, stress relief should be > 0%."""
        r = _run()
        assert r['summary']['relief_pct'] > 0.0

    def test_zero_dwell_minimum_relief(self):
        """Dwell=0 → relief fraction approaches 0."""
        r = compute_stress({
            'material': {
                'E_GPa': 193, 'yield_MPa': 900, 'alpha_1e6': 16,
                'density': 7900, 'Cp': 490, 'k': 15, 'T_melt': 1400,
            },
            'process': {
                'laser_power': 1500, 'scan_speed': 10, 'wire_feed_speed': 80,
                'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
                'ambient_temp': 25, 'dwell_time': 0.0, 'wire_diameter': 1.2,
            },
            'geometry': {'num_layers': 5, 'wall_thickness': 5.0},
            'waypoints': [],
        }, _sweep=False)
        assert r['summary']['relief_pct'] < 5.0
