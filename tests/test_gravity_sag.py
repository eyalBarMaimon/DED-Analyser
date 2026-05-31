"""
Tier 1 — Unit tests for _gravity_sag (cantilever beam formula).
Reference: Gere & Goodno §9.4 — uniformly distributed load on cantilever.
δ = ρ·g·h⁴ / (2·E·wall_t²)
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import _gravity_sag


class TestGravitySag:
    # ── Manual reference calculation ─────────────────────────────────────────
    def _manual_sag_mm(self, rho, h_m, wall_t_m, E):
        """Direct implementation of Gere & Goodno §9.4 cantilever sag."""
        g = 9.81
        return rho * g * h_m**4 / (2 * E * wall_t_m**2) * 1000

    def test_formula_match_ss316l(self):
        """SS316L: ρ=7900, h=25mm, wall_t=5mm, E=193GPa → match to 4 sig figs."""
        rho, h_m, wt_m, E = 7900, 0.025, 0.005, 193e9
        expected = self._manual_sag_mm(rho, h_m, wt_m, E)
        result   = _gravity_sag(rho, 9.81, h_m, wt_m, E)
        assert abs(result - expected) / max(abs(expected), 1e-12) < 1e-4

    def test_height_exponent_is_4(self):
        """Doubling height should increase sag by 2⁴ = 16×."""
        rho, wt, E = 7900, 0.005, 193e9
        s1 = _gravity_sag(rho, 9.81, 0.010, wt, E)
        s2 = _gravity_sag(rho, 9.81, 0.020, wt, E)
        ratio = s2 / s1
        assert abs(ratio - 16.0) < 0.01, f"Expected 16×, got {ratio:.3f}"

    def test_wall_exponent_is_2(self):
        """Doubling wall thickness should reduce sag by 2² = 4×."""
        rho, h, E = 7900, 0.025, 193e9
        s1 = _gravity_sag(rho, 9.81, h, 0.005, E)
        s2 = _gravity_sag(rho, 9.81, h, 0.010, E)
        ratio = s1 / s2
        assert abs(ratio - 4.0) < 0.01, f"Expected 4×, got {ratio:.3f}"

    def test_density_linear(self):
        """Doubling density should double sag."""
        h, wt, E = 0.025, 0.005, 193e9
        s1 = _gravity_sag(4000, 9.81, h, wt, E)
        s2 = _gravity_sag(8000, 9.81, h, wt, E)
        assert abs(s2 / s1 - 2.0) < 0.001

    def test_modulus_inverse_linear(self):
        """Doubling E should halve sag."""
        rho, h, wt = 7900, 0.025, 0.005
        s1 = _gravity_sag(rho, 9.81, h, wt, 100e9)
        s2 = _gravity_sag(rho, 9.81, h, wt, 200e9)
        assert abs(s1 / s2 - 2.0) < 0.001

    def test_returns_mm_not_m(self):
        """A 25mm tall SS316L part should have non-trivial sag in mm units."""
        sag = _gravity_sag(7900, 9.81, 0.025, 0.005, 193e9)
        # Expected ~10⁻⁴ mm range — much less than 1 mm but positive
        assert 1e-6 < sag < 1.0, f"Sag out of expected range: {sag}"

    def test_tall_thin_wall_higher_sag(self):
        """h=50mm, wall=2mm should have dramatically higher sag than h=10mm, wall=10mm."""
        tall_thin  = _gravity_sag(7900, 9.81, 0.050, 0.002, 193e9)
        short_wide = _gravity_sag(7900, 9.81, 0.010, 0.010, 193e9)
        assert tall_thin > short_wide * 100

    def test_zero_height_returns_near_zero(self):
        """h=0 should give essentially zero sag."""
        sag = _gravity_sag(7900, 9.81, 0.0, 0.005, 193e9)
        assert sag == 0.0

    def test_wall_minimum_clamp(self):
        """Extremely thin wall (below 1mm) uses minimum clamp, doesn't divide by zero."""
        sag = _gravity_sag(7900, 9.81, 0.025, 0.0, 193e9)
        # Should use wall_t = 0.001 m minimum
        expected = _gravity_sag(7900, 9.81, 0.025, 0.001, 193e9)
        assert abs(sag - expected) < 1e-10
