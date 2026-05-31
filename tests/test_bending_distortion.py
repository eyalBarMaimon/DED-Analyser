"""
Tier 1 — Unit tests for bending distortion formula.
Reference: Luo & Ueda (1993) ISM cantilever, eq. 12:
  δ = 3 · ε_in · t_layer · h² / wall_t²
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import compute_stress


def _bend_reference(eps_in, t_layer_m, h_m, wall_t_m):
    """Direct implementation of Luo & Ueda (1993) bending formula → mm."""
    return (3 * eps_in * t_layer_m * h_m**2 / wall_t_m**2) * 1000


class TestBendingDistortion:
    def _base_payload(self, **overrides):
        p = {
            'material': {
                'E_GPa': 193, 'yield_MPa': 310, 'alpha_1e6': 16,
                'density': 7900, 'Cp': 490, 'k': 15, 'T_melt': 1400,
            },
            'process': {
                'laser_power': 1500, 'scan_speed': 10, 'wire_feed_speed': 80,
                'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
                'ambient_temp': 25, 'dwell_time': 10, 'wire_diameter': 1.2,
            },
            'geometry': {'num_layers': 10, 'wall_thickness': 5.0},
            'waypoints': [],
        }
        for k, v in overrides.items():
            p[k].update(v)
        return p

    def test_height_squared_scaling(self):
        """
        At same wall_t, doubling num_layers (doubling final height) should ~4× max delta_bend.
        Using 50 vs 100 layers to get sufficient precision (values rounded to 3dp).
        """
        r50  = compute_stress(self._base_payload(geometry={'num_layers': 50, 'wall_thickness': 5.0}), _sweep=False)
        r100 = compute_stress(self._base_payload(geometry={'num_layers': 100, 'wall_thickness': 5.0}), _sweep=False)
        d50  = r50['per_layer'][-1]['delta_bend_mm']
        d100 = r100['per_layer'][-1]['delta_bend_mm']
        ratio = d100 / d50
        assert 3.0 < ratio < 5.5, f"Expected ~4×, got {ratio:.2f}"

    def test_wall_thickness_inverse_square(self):
        """
        Halving wall thickness should ~4× bending distortion (wall_t² in denominator).
        Using 100 layers for sufficient value precision.
        """
        r10 = compute_stress(self._base_payload(geometry={'num_layers': 100, 'wall_thickness': 10.0}), _sweep=False)
        r5  = compute_stress(self._base_payload(geometry={'num_layers': 100, 'wall_thickness':  5.0}), _sweep=False)
        d10 = r10['per_layer'][-1]['delta_bend_mm']
        d5  = r5['per_layer'][-1]['delta_bend_mm']
        ratio = d5 / d10
        assert 3.0 < ratio < 5.5, f"Expected ~4×, got {ratio:.2f}"

    def test_layer_height_increases_delta(self):
        """
        Doubling layer_height with same num_layers: delta grows by ~8×
        (t_layer × h_cum² both scale with layer_height).
        Using 50 layers at same wall_t for sufficient precision.
        """
        r05 = compute_stress(self._base_payload(
            geometry={'num_layers': 50, 'wall_thickness': 5.0},
            process={'layer_height': 0.5}), _sweep=False)
        r10 = compute_stress(self._base_payload(
            geometry={'num_layers': 50, 'wall_thickness': 5.0},
            process={'layer_height': 1.0}), _sweep=False)
        d05 = r05['per_layer'][-1]['delta_bend_mm']
        d10 = r10['per_layer'][-1]['delta_bend_mm']
        ratio = d10 / d05
        # t_layer doubles (2×) and h_cum doubles (4×) → 8×
        assert 5.0 < ratio < 12.0, f"Expected ~8×, got {ratio:.2f}"

    def test_bend_positive(self):
        """All layers must have positive bending distortion."""
        r = compute_stress(self._base_payload(), _sweep=False)
        for l in r['per_layer']:
            assert l['delta_bend_mm'] >= 0, f"Layer {l['layer']} has negative bend"

    def test_bend_monotone_with_height(self):
        """Bending distortion should be non-decreasing layer by layer."""
        r = compute_stress(self._base_payload(), _sweep=False)
        bends = [l['delta_bend_mm'] for l in r['per_layer']]
        for i in range(1, len(bends)):
            assert bends[i] >= bends[i-1] * 0.999, \
                f"Bend not monotone at layer {i}: {bends[i-1]:.4f} → {bends[i]:.4f}"

    def test_formula_h_squared_within_run(self):
        """
        Check h² scaling within a single run at higher layers (avoiding near-zero values).
        Layer 50 vs layer 100 in a 100-layer run: h50=25mm, h100=50mm → ratio should be 4×.
        """
        r = compute_stress(self._base_payload(geometry={'num_layers': 100, 'wall_thickness': 5.0}), _sweep=False)
        layers = r['per_layer']
        d50  = layers[49]['delta_bend_mm']   # layer 50
        d100 = layers[99]['delta_bend_mm']   # layer 100
        h50  = layers[49]['height_mm']
        h100 = layers[99]['height_mm']
        assert d50 > 0 and d100 > 0, f"Near-zero bends: {d50}, {d100}"
        expected = (h100 / h50) ** 2
        actual   = d100 / d50
        assert abs(actual - expected) / expected < 0.05, \
            f"h² ratio {expected:.2f} but bend ratio {actual:.2f}"
