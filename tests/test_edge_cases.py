"""
Edge case tests for compute_stress:
  - num_layers=1
  - very thin wall (thin_factor)
  - ambient_temp ≥ T_melt
  - extreme parameter values
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engines.stress_engine import compute_stress


def _payload(**overrides):
    base = {
        "material": {
            "E_GPa": 193, "yield_MPa": 310, "alpha_1e6": 16.0,
            "density": 7900, "Cp": 490, "k": 15.0, "T_melt": 1400,
        },
        "process": {
            "laser_power": 1500, "scan_speed": 11, "layer_height": 0.5,
            "bead_width": 2.0, "absorption": 0.35,
            "ambient_temp": 25, "dwell_time": 10,
            "wire_diameter": 1.2, "wire_feed_speed": 80,
        },
        "geometry": {"num_layers": 50, "wall_thickness": 5},
        "waypoints": [],
    }
    for k, v in overrides.items():
        section, key = k.split(".", 1)
        base[section][key] = v
    return base


class TestSingleLayer:
    def test_one_layer_returns_result(self):
        r = compute_stress(_payload(**{"geometry.num_layers": 1}), _sweep=False)
        assert r is not None
        assert r["ok"] is True

    def test_one_layer_per_layer_has_one_entry(self):
        r = compute_stress(_payload(**{"geometry.num_layers": 1}), _sweep=False)
        assert len(r["per_layer"]) == 1

    def test_one_layer_height_is_layer_height(self):
        r = compute_stress(_payload(**{"geometry.num_layers": 1,
                                       "process.layer_height": 0.5}), _sweep=False)
        assert abs(r["per_layer"][0]["height_mm"] - 0.5) < 0.01

    def test_one_layer_sigma_positive(self):
        r = compute_stress(_payload(**{"geometry.num_layers": 1}), _sweep=False)
        assert r["per_layer"][0]["sigma_MPa"] > 0


class TestThinWall:
    def test_1mm_wall_activates_thin_factor(self):
        """1 mm wall should give higher stress than 5 mm wall (thin_factor=1.20)."""
        r_thin  = compute_stress(_payload(**{"geometry.wall_thickness": 1}),  _sweep=False)
        r_thick = compute_stress(_payload(**{"geometry.wall_thickness": 5}),  _sweep=False)
        assert r_thin["summary"]["max_sigma_MPa"] >= r_thick["summary"]["max_sigma_MPa"]

    def test_2mm_wall_higher_delta_than_10mm(self):
        """Thinner wall → larger bending delta."""
        r2  = compute_stress(_payload(**{"geometry.wall_thickness": 2}),  _sweep=False)
        r10 = compute_stress(_payload(**{"geometry.wall_thickness": 10}), _sweep=False)
        assert r2["summary"]["max_delta_mm"] > r10["summary"]["max_delta_mm"]

    def test_very_thin_0_5mm_does_not_crash(self):
        r = compute_stress(_payload(**{"geometry.wall_thickness": 0.5}), _sweep=False)
        assert r is not None

    def test_wall_risk_zone_generated_for_thin(self):
        """Wall < 3 mm should produce a thin-wall risk zone."""
        r = compute_stress(_payload(**{"geometry.wall_thickness": 2}), _sweep=False)
        zones = [z["zone"] for z in r["risk_zones"]]
        assert any("thin" in z.lower() or "wall" in z.lower() for z in zones)


class TestAmbientTempEdgeCases:
    def test_ambient_near_melt_does_not_crash(self):
        """ambient_temp = T_melt - 1 should not raise."""
        r = compute_stress(_payload(**{"process.ambient_temp": 1399}), _sweep=False)
        assert r is not None

    def test_ambient_above_melt_does_not_crash(self):
        """ambient_temp > T_melt → dT_melt clamped to 1°C; must not raise."""
        r = compute_stress(_payload(**{"process.ambient_temp": 1500}), _sweep=False)
        assert r is not None

    def test_ambient_above_melt_very_low_stress(self):
        """If ambient > T_melt, dT_melt=1 → eps_in tiny → sigma very small."""
        r = compute_stress(_payload(**{"process.ambient_temp": 1500}), _sweep=False)
        # sigma should be much less than yield
        assert r["summary"]["max_sigma_MPa"] < r["summary"]["yield_MPa"] * 0.5

    def test_high_preheat_reduces_sigma(self):
        r25  = compute_stress(_payload(**{"process.ambient_temp": 25}),  _sweep=False)
        r300 = compute_stress(_payload(**{"process.ambient_temp": 300}), _sweep=False)
        assert r300["summary"]["max_sigma_MPa"] <= r25["summary"]["max_sigma_MPa"]


class TestExtremeParams:
    def test_zero_layer_height_does_not_crash(self):
        """layer_height=0 should fall back gracefully."""
        r = compute_stress(_payload(**{"process.layer_height": 0}), _sweep=False)
        # May return None or a valid result — must not raise
        assert r is None or isinstance(r, dict)

    def test_very_high_power_does_not_crash(self):
        r = compute_stress(_payload(**{"process.laser_power": 10000}), _sweep=False)
        assert r is not None

    def test_very_low_scan_speed_does_not_crash(self):
        r = compute_stress(_payload(**{"process.scan_speed": 0.001}), _sweep=False)
        assert r is not None

    def test_many_layers_does_not_crash(self):
        r = compute_stress(_payload(**{"geometry.num_layers": 500}), _sweep=False)
        assert r is not None
        assert len(r["per_layer"]) == 500

    def test_sigma_never_exceeds_yield_under_any_param(self):
        """No matter what, sigma ≤ yield (plasticity clamp)."""
        for power in [500, 1500, 5000]:
            r = compute_stress(_payload(**{"process.laser_power": power,
                                           "geometry.num_layers": 100}), _sweep=False)
            assert r is not None
            yield_mpa = r["summary"]["yield_MPa"]
            for layer in r["per_layer"]:
                assert layer["sigma_MPa"] <= yield_mpa + 0.1  # +0.1 for rounding
