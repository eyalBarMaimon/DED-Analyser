"""
Tier 1 — Unit tests for scan-direction anisotropy and displacement vectors.
"""
import math
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stress_engine import _scan_anisotropy, compute_stress
from conftest import make_raster_waypoints, make_contour_waypoints, make_helix_waypoints


class TestScanAnisotropy:
    def test_raster_factors(self):
        f_t, f_p = _scan_anisotropy('raster')
        assert abs(f_t - 1.35) < 0.01
        assert abs(f_p - 1.00) < 0.01

    def test_contour_factors(self):
        f_t, f_p = _scan_anisotropy('contour')
        assert abs(f_t - 1.15) < 0.01
        assert abs(f_p - 1.05) < 0.01

    def test_helix_factors(self):
        f_t, f_p = _scan_anisotropy('helix')
        assert abs(f_t - 1.20) < 0.01
        assert abs(f_p - 1.20) < 0.01

    def test_raster_higher_than_contour(self):
        f_t_r, _ = _scan_anisotropy('raster')
        f_t_c, _ = _scan_anisotropy('contour')
        assert f_t_r > f_t_c


class TestDisplacementVectors:
    def _payload(self, wps):
        return {
            'material': {
                'E_GPa': 193, 'yield_MPa': 900, 'alpha_1e6': 16,
                'density': 7900, 'Cp': 490, 'k': 15, 'T_melt': 1400,
            },
            'process': {
                'laser_power': 1500, 'scan_speed': 10, 'wire_feed_speed': 80,
                'layer_height': 0.5, 'bead_width': 2.0, 'absorption': 0.35,
                'ambient_temp': 25, 'dwell_time': 10, 'wire_diameter': 1.2,
            },
            'geometry': {'num_layers': 10, 'wall_thickness': 5.0},
            'waypoints': wps,
        }

    def test_stress_wps_populated(self):
        """compute_stress with waypoints should populate stress_wps."""
        wps = make_raster_waypoints(layers=3)
        r = compute_stress(self._payload(wps), _sweep=False)
        assert r is not None
        assert len(r['stress_wps']) == len(wps)

    def test_stress_wps_fields(self):
        """Each stress_wp should have all required fields."""
        wps = make_raster_waypoints(layers=2)
        r = compute_stress(self._payload(wps), _sweep=False)
        required = ('x', 'y', 'z', 'sigma_MPa', 'delta_mm',
                    'disp_x', 'disp_y', 'disp_z', 'ratio', 'layer', 'arc_frac')
        for wp in r['stress_wps']:
            for f in required:
                assert f in wp, f"stress_wp missing: {f}"

    def test_helix_vectors_radial(self):
        """For helix, displacement should point radially (away from centroid)."""
        wps = make_helix_waypoints(turns=5, radius_mm=20, total_height_mm=10)
        r = compute_stress(self._payload(wps), _sweep=False)
        assert r is not None
        assert r['geometry_type'] == 'helix', "Helix not detected"

        # Engine uses tp['centroid'] which is the bounding box centre
        all_x = [w['x'] for w in wps]
        all_y = [w['y'] for w in wps]
        cx = (min(all_x) + max(all_x)) / 2
        cy = (min(all_y) + max(all_y)) / 2

        checked = 0
        for wp in r['stress_wps']:
            rx = wp['x'] - cx
            ry = wp['y'] - cy
            r_dist = math.sqrt(rx*rx + ry*ry)
            if r_dist < 5.0 or (abs(wp['disp_x']) < 1e-6 and abs(wp['disp_y']) < 1e-6):
                continue
            disp_mag = math.sqrt(wp['disp_x']**2 + wp['disp_y']**2)
            if disp_mag < 1e-6:
                continue
            # Normalised dot: should be ≥ 0 (outward)
            dot = (rx * wp['disp_x'] + ry * wp['disp_y']) / (r_dist * disp_mag)
            assert dot >= -0.05, f"Helix: disp not radial at ({wp['x']:.1f},{wp['y']:.1f}), dot={dot:.3f}"
            checked += 1
        assert checked > 0, "No helix waypoints had non-zero XY displacement"

    def test_disp_z_increases_with_height(self):
        """Z displacement should increase with build height (cantilever bowing)."""
        wps = make_raster_waypoints(layers=5)
        r = compute_stress(self._payload(wps), _sweep=False)
        sw = r['stress_wps']
        # Average disp_z for bottom 20% vs top 20%
        sorted_sw = sorted(sw, key=lambda w: w['z'])
        n = max(len(sorted_sw)//5, 1)
        avg_bot = sum(w['disp_z'] for w in sorted_sw[:n]) / n
        avg_top = sum(w['disp_z'] for w in sorted_sw[-n:]) / n
        assert avg_top >= avg_bot, f"Top disp_z {avg_top:.4f} < bottom {avg_bot:.4f}"

    def test_no_waypoints_gives_empty_stress_wps(self):
        r = compute_stress(self._payload([]), _sweep=False)
        assert r is not None
        assert r['stress_wps'] == []

    def test_ratio_between_0_and_1(self):
        """stress_wp ratio = sigma/yield should be in [0, 1]."""
        wps = make_raster_waypoints(layers=3)
        r = compute_stress(self._payload(wps), _sweep=False)
        for wp in r['stress_wps']:
            assert 0.0 <= wp['ratio'] <= 1.0 + 0.001, \
                f"ratio {wp['ratio']} out of [0,1]"
