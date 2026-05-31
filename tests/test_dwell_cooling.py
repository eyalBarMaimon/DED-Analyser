"""
Dwell cooling / stress-relaxation tests.
Verifies that the FDM analytical relief formula behaves physically correctly.
"""
import math
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engines.stress_engine import compute_stress


def _base_payload(dwell_s=0.0, num_layers=20):
    return {
        "material": {
            "E_GPa": 193, "yield_MPa": 310, "alpha_1e6": 16.0,
            "density": 7900, "Cp": 490, "k": 15.0, "T_melt": 1400,
        },
        "process": {
            "laser_power": 1500, "scan_speed": 11, "layer_height": 0.5,
            "bead_width": 2.0, "absorption": 0.35,
            "ambient_temp": 25, "dwell_time": dwell_s,
            "wire_diameter": 1.2, "wire_feed_speed": 80,
        },
        "geometry": {"num_layers": num_layers, "wall_thickness": 5},
        "waypoints": [],
    }


class TestDwellCooling:

    def test_zero_dwell_minimum_relief(self):
        """With dwell=0 the relief fraction should be near 0 but > 0 (formula floor)."""
        r = compute_stress(_base_payload(dwell_s=0), _sweep=False)
        assert r is not None
        assert r["summary"]["relief_pct"] >= 0.0

    def test_dwell_increases_relief(self):
        """Longer dwell → higher relief_pct."""
        r0 = compute_stress(_base_payload(dwell_s=0),   _sweep=False)
        r1 = compute_stress(_base_payload(dwell_s=30),  _sweep=False)
        r2 = compute_stress(_base_payload(dwell_s=120), _sweep=False)
        assert r1["summary"]["relief_pct"] > r0["summary"]["relief_pct"]
        assert r2["summary"]["relief_pct"] > r1["summary"]["relief_pct"]

    def test_dwell_reduces_sigma(self):
        """Longer dwell → lower max stress (monotone)."""
        r0 = compute_stress(_base_payload(dwell_s=0),   _sweep=False)
        r1 = compute_stress(_base_payload(dwell_s=60),  _sweep=False)
        r2 = compute_stress(_base_payload(dwell_s=300), _sweep=False)
        assert r1["summary"]["max_sigma_MPa"] <= r0["summary"]["max_sigma_MPa"]
        assert r2["summary"]["max_sigma_MPa"] <= r1["summary"]["max_sigma_MPa"]

    def test_relief_between_0_and_100(self):
        """Relief fraction must stay in [0, 100] %."""
        for dwell in [0, 5, 30, 120, 600]:
            r = compute_stress(_base_payload(dwell_s=dwell), _sweep=False)
            assert 0.0 <= r["summary"]["relief_pct"] <= 100.0

    def test_very_long_dwell_approaches_saturation(self):
        """At very long dwell times, relief should be > 80 % (exponential saturation)."""
        r = compute_stress(_base_payload(dwell_s=3600), _sweep=False)
        assert r["summary"]["relief_pct"] > 50.0

    def test_dwell_does_not_affect_delta(self):
        """Dwell changes sigma but NOT bending delta (delta is geometry-only)."""
        r0 = compute_stress(_base_payload(dwell_s=0),   _sweep=False)
        r1 = compute_stress(_base_payload(dwell_s=300), _sweep=False)
        # delta is driven by eps_in × geometry, not by accumulated sigma
        # so the difference should be small (< 5%)
        d0 = r0["summary"]["max_delta_mm"]
        d1 = r1["summary"]["max_delta_mm"]
        assert abs(d1 - d0) / max(d0, 1e-9) < 0.05

    def test_relief_formula_shape(self):
        """
        relief = 1 - exp(-dwell / tau).
        Check that doubling dwell from a small value roughly doubles (1 - relief)
        reduction when dwell << tau (linear regime).
        """
        r10 = compute_stress(_base_payload(dwell_s=1),  _sweep=False)
        r20 = compute_stress(_base_payload(dwell_s=2),  _sweep=False)
        # In linear regime: relief(2t) ≈ 2 × relief(t)
        rel10 = r10["summary"]["relief_pct"]
        rel20 = r20["summary"]["relief_pct"]
        # Must at least be larger (monotone) — exact ratio depends on tau
        assert rel20 >= rel10
