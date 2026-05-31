"""
Tier 1 — Unit tests for intra-layer thermal gradient (Feature A).
Physics: stress peaks at both turnaround ends of scan track.
Model: intra_factor = 1 + INTRA_GRAD × |1 − 2·arc_frac|
  → max at arc_frac=0 (start) and arc_frac=1 (end)
  → min at arc_frac=0.5 (midpoint)
"""
import math
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import compute_stress
from conftest import make_raster_waypoints, make_contour_waypoints


INTRA_GRAD = 0.25   # must match engine constant


class TestIntraLayerGradient:
    def _payload_with_wps(self, wps):
        return {
            'material': {
                'E_GPa': 193, 'yield_MPa': 900,  # high yield → no clamping
                'alpha_1e6': 16, 'density': 7900,
                'Cp': 490, 'k': 15, 'T_melt': 1400,
            },
            'process': {
                'laser_power': 1500, 'scan_speed': 10, 'wire_feed_speed': 80,
                'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
                'ambient_temp': 25, 'dwell_time': 10, 'wire_diameter': 1.2,
            },
            'geometry': {'num_layers': 10, 'wall_thickness': 5.0},
            'waypoints': wps,
        }

    def test_start_end_higher_than_middle(self):
        """
        Within a single layer, the first and last waypoints should have
        higher stress than the midpoint waypoint.
        """
        # Use many steps per pass so arc coverage is good
        wps = make_raster_waypoints(layers=1, passes_per_layer=1,
                                    length_mm=40, steps_per_pass=30)
        r = compute_stress(self._payload_with_wps(wps), _sweep=False)
        assert r is not None

        sw = r['stress_wps']
        if len(sw) < 3:
            pytest.skip("Not enough stress_wps to test gradient")

        mid_idx = len(sw) // 2
        sigma_start = sw[0]['sigma_MPa']
        sigma_mid   = sw[mid_idx]['sigma_MPa']
        sigma_end   = sw[-1]['sigma_MPa']

        assert sigma_start >= sigma_mid, \
            f"Start σ={sigma_start} should be ≥ mid σ={sigma_mid}"
        assert sigma_end >= sigma_mid, \
            f"End σ={sigma_end} should be ≥ mid σ={sigma_mid}"

    def test_arc_frac_range(self):
        """All arc_frac values must be in [0, 1]."""
        wps = make_raster_waypoints(layers=5, passes_per_layer=4)
        r = compute_stress(self._payload_with_wps(wps), _sweep=False)
        for wp in r['stress_wps']:
            assert 0.0 <= wp['arc_frac'] <= 1.0, \
                f"arc_frac {wp['arc_frac']} out of range"

    def test_intra_factor_formula(self):
        """Verify intra_factor at arc_frac=0 is 1+INTRA_GRAD, at 0.5 is 1.0."""
        # Direct formula test (no engine call needed)
        def intra(af):
            return 1.0 + INTRA_GRAD * abs(1.0 - 2.0 * af)

        assert abs(intra(0.0) - (1.0 + INTRA_GRAD)) < 1e-9
        assert abs(intra(1.0) - (1.0 + INTRA_GRAD)) < 1e-9
        assert abs(intra(0.5) - 1.0)                 < 1e-9
        assert abs(intra(0.25) - 1.125)              < 1e-9

    def test_symmetry(self):
        """intra_factor at arc_frac=x should equal that at arc_frac=1-x."""
        def intra(af):
            return 1.0 + INTRA_GRAD * abs(1.0 - 2.0 * af)
        for x in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]:
            assert abs(intra(x) - intra(1.0 - x)) < 1e-9, \
                f"Not symmetric at x={x}"

    def test_stress_variation_within_layer(self):
        """
        With INTRA_GRAD=0.25, stress should vary by up to ±12.5% within a layer.
        The ratio max/min within one layer's waypoints should be ≤ 1+INTRA_GRAD.
        """
        wps = make_raster_waypoints(layers=3, passes_per_layer=1, length_mm=40)
        r = compute_stress(self._payload_with_wps(wps), _sweep=False)
        sw = r['stress_wps']
        if not sw:
            pytest.skip("No stress_wps")

        # Group by layer
        from collections import defaultdict
        by_layer = defaultdict(list)
        for wp in sw:
            by_layer[wp['layer']].append(wp['sigma_MPa'])

        for lay, sigmas in by_layer.items():
            if len(sigmas) < 2:
                continue
            ratio = max(sigmas) / max(min(sigmas), 1e-9)
            assert ratio <= 1.0 + INTRA_GRAD + 0.02, \
                f"Layer {lay}: max/min ratio {ratio:.3f} exceeds 1+INTRA_GRAD"
