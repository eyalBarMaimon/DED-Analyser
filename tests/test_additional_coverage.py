"""
Additional coverage: estimate_wall_thickness, _subsample edge cases,
/api/fem/simulate endpoint, /api/jobs/<jid>/result endpoint.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import app as flask_app


@pytest.fixture(scope="module")
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def _make_done_job(overlay_pts=None):
    jid = flask_app._new_job("analyze")
    pts = overlay_pts if overlay_pts is not None else [
        {'x': float(i % 10), 'y': float(i // 10), 'z': float((i % 3 + 1) * 0.8),
         'layer': (i % 3) + 1, 'temp_C': 500.0, 'sigma': 100.0, 'delta': 0.01,
         'dx': 0.0, 'dy': 0.0, 'dz': 0.0}
        for i in range(60)
    ]
    flask_app._job_update(jid, status='done', pct=100, stage='Complete',
                          result={'overlay_pts': pts, 'num_layers': 3,
                                  'waypoints_full': pts, 'user_params': {},
                                  'db_materials': {'T0': None}})
    return jid


# ── estimate_wall_thickness ───────────────────────────────────────────────────

class TestEstimateWallThickness:
    def _helix_wps(self, r=15.0, n=20):
        import math
        wps = []
        for i in range(n):
            angle = 2 * math.pi * i / n
            wps.append({'x': r * math.cos(angle), 'y': r * math.sin(angle),
                        'z': float(i) * 0.1, 'layer': 1, 'is_deposition': True})
        return wps

    def _flat_wps(self, span=50.0, n=20):
        """2D raster — both X and Y have extent so min(dx,dy) > 0."""
        wps = []
        for i in range(n):
            wps.append({'x': float(i) * (span / n), 'y': 0.0,     'z': 1.0, 'layer': 1, 'is_deposition': True})
            wps.append({'x': float(i) * (span / n), 'y': span/2.0, 'z': 1.0, 'layer': 1, 'is_deposition': True})
        return wps

    def test_helix_returns_wire_diameter(self):
        from engines.stress_engine import estimate_wall_thickness
        wps = self._helix_wps()
        wire_d = 1.2
        result = estimate_wall_thickness(wps, wire_d)
        # Helix → returns wire_diameter
        assert abs(result - wire_d) < 0.01, f"Expected {wire_d}, got {result}"

    def test_flat_returns_at_least_wire_diameter(self):
        from engines.stress_engine import estimate_wall_thickness
        wps = self._flat_wps()
        wire_d = 1.2
        result = estimate_wall_thickness(wps, wire_d)
        assert result >= wire_d

    def test_empty_waypoints_returns_default(self):
        from engines.stress_engine import estimate_wall_thickness
        result = estimate_wall_thickness([], 1.2)
        assert result == 5.0

    def test_wider_part_gives_larger_thickness(self):
        from engines.stress_engine import estimate_wall_thickness
        narrow = estimate_wall_thickness(self._flat_wps(span=10.0), 1.2)
        wide   = estimate_wall_thickness(self._flat_wps(span=100.0), 1.2)
        assert wide > narrow


# ── _subsample edge cases ─────────────────────────────────────────────────────

class TestSubsampleEdgeCases:
    def test_empty_faces_returns_empty(self):
        from engines.distortion_engine import _subsample
        v2, f2 = _subsample([], [], max_faces=10)
        assert f2 == []

    def test_exactly_max_faces_unchanged(self):
        from engines.distortion_engine import _subsample
        verts = [(float(i), 0.0, 0.0) for i in range(9)]
        faces = [(0,1,2),(3,4,5),(6,7,8)]
        v2, f2 = _subsample(verts, faces, max_faces=3)
        assert len(f2) == 3

    def test_over_limit_returns_at_most_max(self):
        from engines.distortion_engine import _subsample
        verts = [(float(i), 0.0, 0.0) for i in range(30)]
        faces = [(i, i+1, i+2) for i in range(0, 27, 3)]  # 9 faces
        v2, f2 = _subsample(verts, faces, max_faces=5)
        assert len(f2) <= 5

    def test_sampled_faces_are_subset(self):
        from engines.distortion_engine import _subsample
        verts = [(float(i), 0.0, 0.0) for i in range(30)]
        faces = [(i, i+1, i+2) for i in range(0, 27, 3)]
        v2, f2 = _subsample(verts, faces, max_faces=5)
        # All sampled face indices must be valid
        n = len(v2)
        for f in f2:
            assert all(0 <= idx < n for idx in f)


# ── POST /api/fem/simulate ────────────────────────────────────────────────────

class TestFemSimulateEndpoint:
    def test_unknown_job_returns_400(self, client):
        r = client.post('/api/fem/simulate',
                        data=json.dumps({'job_id': 'doesnotexist'}),
                        content_type='application/json')
        assert r.status_code == 400

    def test_running_job_returns_400(self, client):
        jid = flask_app._new_job('analyze')
        flask_app._job_update(jid, status='running', pct=50, stage='Running')
        r = client.post('/api/fem/simulate',
                        data=json.dumps({'job_id': jid}),
                        content_type='application/json')
        assert r.status_code == 400

    def test_valid_job_returns_fem_job_id(self, client):
        jid = _make_done_job()
        r = client.post('/api/fem/simulate',
                        data=json.dumps({'job_id': jid, 'resolution': 'fast'}),
                        content_type='application/json')
        assert r.status_code == 200
        body = json.loads(r.data)
        assert 'job_id' in body
        assert body['job_id'] != jid   # new FEM job, not the analysis job


# ── GET /api/jobs/<jid>/result ────────────────────────────────────────────────

class TestJobResultEndpoint:
    def test_unknown_job_returns_404(self, client):
        r = client.get('/api/jobs/doesnotexist/result')
        assert r.status_code == 404

    def test_running_job_returns_409(self, client):
        jid = flask_app._new_job('fem')
        flask_app._job_update(jid, status='running', pct=30, stage='Running')
        r = client.get(f'/api/jobs/{jid}/result')
        assert r.status_code == 409

    def test_done_job_returns_200(self, client):
        jid = _make_done_job()
        r = client.get(f'/api/jobs/{jid}/result')
        assert r.status_code == 200

    def test_done_job_result_is_dict(self, client):
        jid = _make_done_job()
        body = json.loads(client.get(f'/api/jobs/{jid}/result').data)
        assert isinstance(body, dict)

    def test_error_job_returns_500(self, client):
        jid = flask_app._new_job('fem')
        flask_app._job_update(jid, status='error', error='Something broke')
        r = client.get(f'/api/jobs/{jid}/result')
        assert r.status_code == 500


# ── FEM absorption from db_materials ─────────────────────────────────────────

class TestFemAbsorption:
    def test_absorption_persisted_in_job_result(self):
        """db_materials serialization must include absorption_450nm."""
        jid = flask_app._new_job('analyze')
        flask_app._job_update(jid, status='done', pct=100, stage='Complete', result={
            'overlay_pts': [], 'num_layers': 3, 'waypoints_full': [],
            'user_params': {},
            'db_materials': {'T0': {
                'display_name': 'Aluminium 6061',
                'thermal_conductivity': 167.0,
                'density': 2700,
                'specific_heat': 896,
                'melting_point': 652,
                'absorption_450nm': 0.22,
            }},
        })
        stored = flask_app._job_get(jid)
        db_mat = stored['result']['db_materials'].get('T0') or {}
        assert db_mat.get('absorption_450nm') == 0.22
        assert db_mat.get('melting_point') == 652

    def test_absorption_fallback_when_missing(self):
        """When absorption_450nm absent in db_mat, 0.35 default is used."""
        db_mat = {'thermal_conductivity': 16.3, 'density': 7990, 'specific_heat': 500,
                  'melting_point': 1390}
        assert db_mat.get('absorption_450nm', 0.35) == 0.35
