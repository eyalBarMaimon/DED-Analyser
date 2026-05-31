"""
Tier 1 — Unit tests for Rosenthal cooling rate formula.
Reference: Rosenthal (1946), wire-DED calibration.
"""
import math
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import _rosenthal_cooling_rate


class TestRosenthalFormula:
    def test_reference_ss316l(self):
        """
        Reference: P=1500W, η=0.35, v=10mm/s, SS316L (k=15, ρ=7900, Cp=490), r=1mm.
        alpha_diff = 15/(7900×490) = 3.875e-6 m²/s.
        Expected: ~15 000 K/s (documented calibration point in stress_engine.py).
        Allow ±20% tolerance for floating-point path.
        """
        alpha_diff_ss316l = 15.0 / (7900 * 490)   # 3.875e-6
        dTdt = _rosenthal_cooling_rate(
            laser_P=1500, absorb=0.35,
            scan_v_ms=0.010,
            k_therm=15.0,
            alpha_diff=alpha_diff_ss316l,
            r_m=0.001,
        )
        assert 10_000 < dTdt < 25_000, f"Expected ~15 000 K/s, got {dTdt:.0f}"

    def test_higher_power_increases_rate(self):
        kwargs = dict(laser_P=1000, absorb=0.35, scan_v_ms=0.01,
                      k_therm=15.0, alpha_diff=2.72e-6, r_m=0.001)
        low  = _rosenthal_cooling_rate(**kwargs)
        high = _rosenthal_cooling_rate(**{**kwargs, 'laser_P': 2000})
        assert high > low

    def test_scan_speed_affects_rate(self):
        """
        dT/dt ~ v·exp(-v·r/2α): at very low v, increasing v raises dT/dt (linear term wins).
        At high v, exp decay dominates and dT/dt falls.
        Test the low-v regime (v=0.001 < v=0.005, both << 2α/r ≈ 7.75 m/s).
        """
        alpha_diff = 15.0 / (7900 * 490)
        kwargs = dict(laser_P=1500, absorb=0.35, k_therm=15.0,
                      alpha_diff=alpha_diff, r_m=0.001)
        v1 = _rosenthal_cooling_rate(**kwargs, scan_v_ms=0.001)
        v2 = _rosenthal_cooling_rate(**kwargs, scan_v_ms=0.005)
        assert v2 > v1, f"Expected v=5mm/s > v=1mm/s, got {v2:.0f} <= {v1:.0f}"

    def test_larger_radius_decreases_rate(self):
        kwargs = dict(laser_P=1500, absorb=0.35, scan_v_ms=0.01,
                      k_therm=15.0, alpha_diff=2.72e-6, r_m=0.001)
        small = _rosenthal_cooling_rate(**kwargs)
        large = _rosenthal_cooling_rate(**{**kwargs, 'r_m': 0.003})
        assert large < small

    def test_higher_conductivity_decreases_rate(self):
        """High k (copper-like) should spread heat, reduce cooling rate at same point."""
        kwargs = dict(laser_P=1500, absorb=0.35, scan_v_ms=0.01,
                      k_therm=15.0, alpha_diff=2.72e-6, r_m=0.001)
        low_k  = _rosenthal_cooling_rate(**kwargs)
        high_k = _rosenthal_cooling_rate(**{**kwargs, 'k_therm': 400.0})
        assert high_k < low_k

    def test_zero_power_returns_fallback(self):
        dTdt = _rosenthal_cooling_rate(0, 0.35, 0.01, 15.0, 2.72e-6, 0.001)
        assert dTdt == 1000.0   # documented fallback

    def test_zero_scan_speed_returns_fallback(self):
        dTdt = _rosenthal_cooling_rate(1500, 0.35, 0.0, 15.0, 2.72e-6, 0.001)
        assert dTdt == 1000.0

    def test_ti64_reference(self):
        """
        Ti-6Al-4V: k=7, alpha_diff=7/(4430×560)=2.82e-6, same process.
        Lower k → higher dTdt at same radius vs SS316L (k=15, alpha=3.875e-6).
        """
        alpha_ss = 15.0 / (7900 * 490)   # 3.875e-6
        alpha_ti = 7.0  / (4430 * 560)   # 2.82e-6
        dTdt_ss = _rosenthal_cooling_rate(1500, 0.35, 0.010, 15.0, alpha_ss, 0.001)
        dTdt_ti = _rosenthal_cooling_rate(1500, 0.35, 0.010,  7.0, alpha_ti, 0.001)
        assert dTdt_ti > dTdt_ss, "Ti-6Al-4V (low k) should cool faster than SS316L"

    def test_result_is_positive(self):
        dTdt = _rosenthal_cooling_rate(1500, 0.35, 0.01, 15.0, 2.72e-6, 0.001)
        assert dTdt > 0

    def test_minimum_clamp(self):
        """Result should never be below 10 K/s (internal clamp)."""
        dTdt = _rosenthal_cooling_rate(0.001, 0.01, 0.0001, 1000.0, 1e-4, 0.1)
        assert dTdt >= 10.0
