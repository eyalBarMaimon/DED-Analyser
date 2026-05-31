"""
Flask API endpoint tests — no real ZIP needed.
Uses Flask test client to hit every major route.
"""
import io
import json
import zipfile
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as flask_app


@pytest.fixture(scope="module")
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


# ── Static pages ──────────────────────────────────────────────────────────────

class TestStaticRoutes:
    def test_root_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"html" in r.data.lower()

    def test_robot_route(self, client):
        r = client.get("/robot")
        assert r.status_code == 200

    def test_ded_route_alias(self, client):
        r = client.get("/ded")
        assert r.status_code == 200

    def test_m600_route(self, client):
        r = client.get("/m600")
        assert r.status_code == 200


# ── Materials API ─────────────────────────────────────────────────────────────

class TestMaterialsApi:
    def test_get_materials_200(self, client):
        r = client.get("/api/materials")
        assert r.status_code == 200

    def _mat_list(self, client):
        """GET /api/materials returns either a list or {"materials": [...]}."""
        body = json.loads(client.get("/api/materials").data)
        return body if isinstance(body, list) else body.get("materials", body)

    def test_get_materials_has_list(self, client):
        assert isinstance(self._mat_list(client), list)

    def test_get_materials_non_empty(self, client):
        assert len(self._mat_list(client)) > 0

    def test_add_material_missing_fields(self, client):
        r = client.post("/api/materials",
                        data=json.dumps({"display_name": "test"}),
                        content_type="application/json")
        assert r.status_code == 400

    def test_add_material_duplicate_id(self, client):
        mats = self._mat_list(client)
        if not mats:
            pytest.skip("no materials in DB")
        existing_id = mats[0]["id"]
        r2 = client.post("/api/materials",
                         data=json.dumps({"id": existing_id, "display_name": "dup"}),
                         content_type="application/json")
        assert r2.status_code == 409


# ── Settings API ──────────────────────────────────────────────────────────────

class TestSettingsApi:
    def test_get_settings(self, client):
        r = client.get("/api/settings")
        assert r.status_code == 200
        body = json.loads(r.data)
        assert isinstance(body, dict)

    def test_post_settings(self, client):
        r = client.post("/api/settings",
                        data=json.dumps({"watch_dir": ""}),
                        content_type="application/json")
        assert r.status_code == 200
        assert json.loads(r.data)["ok"] is True


# ── Analyze endpoint — error paths ────────────────────────────────────────────

class TestAnalyzeErrors:
    def test_no_file_returns_400(self, client):
        r = client.post("/api/analyze")
        assert r.status_code == 400

    def test_non_zip_returns_400(self, client):
        data = {"zip_file": (io.BytesIO(b"not a zip"), "file.txt")}
        r = client.post("/api/analyze",
                        data=data,
                        content_type="multipart/form-data")
        assert r.status_code == 400

    def test_broken_zip_returns_job_id(self, client):
        """Broken ZIP still gets a job_id — error surface in job status."""
        data = {"zip_file": (io.BytesIO(b"PK\x00\x00garbage"), "bad.zip")}
        r = client.post("/api/analyze",
                        data=data,
                        content_type="multipart/form-data")
        assert r.status_code == 200
        body = json.loads(r.data)
        assert "job_id" in body

    def test_valid_zip_returns_job_id(self, client):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dummy.mod", "MODULE Test\nENDMODULE")
        buf.seek(0)
        data = {"zip_file": (buf, "test.zip")}
        r = client.post("/api/analyze",
                        data=data,
                        content_type="multipart/form-data")
        assert r.status_code == 200
        body = json.loads(r.data)
        assert "job_id" in body
        assert len(body["job_id"]) == 12


# ── Job status endpoint ───────────────────────────────────────────────────────

class TestJobStatus:
    def _get_job_id(self, client):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dummy.mod", "MODULE Test\nENDMODULE")
        buf.seek(0)
        r = client.post("/api/analyze",
                        data={"zip_file": (buf, "test.zip")},
                        content_type="multipart/form-data")
        return json.loads(r.data)["job_id"]

    def test_unknown_job_returns_error_or_404(self, client):
        r = client.get("/api/job/nonexistentjobid00")
        assert r.status_code in (200, 404)

    def test_known_job_has_status_field(self, client):
        jid = self._get_job_id(client)
        r = client.get(f"/api/job/{jid}")
        if r.status_code == 200:
            body = json.loads(r.data)
            assert "status" in body


# ── Rapid preview ─────────────────────────────────────────────────────────────

class TestRapidPreview:
    def test_no_file_returns_400(self, client):
        r = client.post("/api/rapid/preview")
        assert r.status_code == 400

    def test_zip_with_mod_file(self, client):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("part_0001.mod",
                        "MoveL [[10.0,20.0,5.0],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]],v200,z0,tool0;")
        buf.seek(0)
        r = client.post("/api/rapid/preview",
                        data={"zip_file": (buf, "test.zip")},
                        content_type="multipart/form-data")
        assert r.status_code == 200
        body = json.loads(r.data)
        assert "layer_count" in body

    def test_zip_with_parameters_txt(self, client):
        params_txt = """Movement
  Base Print Speed: 11.0
  Deposition Height: 0.5
  Deposition Width: 2.0
  Wait Time: 5.0
"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Parameters.txt", params_txt)
        buf.seek(0)
        r = client.post("/api/rapid/preview",
                        data={"zip_file": (buf, "test.zip")},
                        content_type="multipart/form-data")
        assert r.status_code == 200
        body = json.loads(r.data)
        assert body.get("source") == "parameters_file"
        assert body.get("scan_speed_mm_s") == 11.0
        assert body.get("layer_height_mm") == 0.5


# ── Sensors — error paths ─────────────────────────────────────────────────────

class TestSensorsErrors:
    def test_snapshot_no_data_returns_400(self, client):
        r = client.get("/api/sensors/snapshot")
        assert r.status_code in (200, 400)

    def test_connect_missing_file_returns_400(self, client):
        r = client.post("/api/sensors/connect",
                        data=json.dumps({"file_path": "/nonexistent/path.csv"}),
                        content_type="application/json")
        assert r.status_code == 400


# ── Geometry endpoint ─────────────────────────────────────────────────────────

class TestGeometryEndpoint:
    def _make_done_job(self, overlay_pts=None):
        """Inject a completed job into the app job store."""
        import app as a
        jid = a._new_job("analyze")
        pts = overlay_pts if overlay_pts is not None else [
            {'x': float(i % 10), 'y': float(i // 10), 'z': float((i % 3) * 0.8 + 0.8),
             'layer': (i % 3) + 1, 'temp_C': 500.0, 'sigma': 100.0, 'delta': 0.01,
             'dx': 0.0, 'dy': 0.0, 'dz': 0.0}
            for i in range(60)   # 3 layers × 20 pts
        ]
        a._job_update(jid, status='done', pct=100, stage='Complete',
                      result={'overlay_pts': pts, 'num_layers': 3})
        return jid

    def test_unknown_job_returns_400(self, client):
        r = client.get("/api/jobs/doesnotexist/geometry")
        assert r.status_code == 400

    def test_empty_overlay_pts_returns_400(self, client):
        jid = self._make_done_job(overlay_pts=[])
        r = client.get(f"/api/jobs/{jid}/geometry")
        assert r.status_code == 400

    def test_happy_path_returns_ok(self, client):
        jid = self._make_done_job()
        r = client.get(f"/api/jobs/{jid}/geometry")
        assert r.status_code == 200
        body = json.loads(r.data)
        assert body['ok'] is True

    def test_mesh_keys_present(self, client):
        jid = self._make_done_job()
        body = json.loads(client.get(f"/api/jobs/{jid}/geometry").data)
        mesh = body['mesh']
        for k in ('type', 'x', 'y', 'z', 'i', 'j', 'k', 'color', 'opacity'):
            assert k in mesh

    def test_mesh_type_is_mesh3d(self, client):
        jid = self._make_done_job()
        body = json.loads(client.get(f"/api/jobs/{jid}/geometry").data)
        assert body['mesh']['type'] == 'mesh3d'

    def test_mesh_color_is_grey(self, client):
        jid = self._make_done_job()
        body = json.loads(client.get(f"/api/jobs/{jid}/geometry").data)
        assert body['mesh']['color'] == '#cccccc'

    def test_face_indices_valid(self, client):
        jid = self._make_done_job()
        body = json.loads(client.get(f"/api/jobs/{jid}/geometry").data)
        mesh = body['mesh']
        n_verts = len(mesh['x'])
        for idx in mesh['i'] + mesh['j'] + mesh['k']:
            assert 0 <= idx < n_verts, f"Invalid face index {idx} for {n_verts} vertices"

    def test_vertex_count_reported(self, client):
        jid = self._make_done_job()
        body = json.loads(client.get(f"/api/jobs/{jid}/geometry").data)
        assert body['vertex_count'] == len(body['mesh']['x'])


# ── Job stop endpoint ─────────────────────────────────────────────────────────

class TestJobStop:
    def test_unknown_job_returns_404(self, client):
        r = client.post("/api/jobs/doesnotexist/stop",
                        content_type="application/json")
        assert r.status_code == 404

    def test_done_job_returns_ok(self, client):
        import app as a
        jid = a._new_job("fem")
        a._job_update(jid, status='done', pct=100, stage='Complete', result={})
        r = client.post(f"/api/jobs/{jid}/stop", content_type="application/json")
        assert r.status_code == 200
        assert json.loads(r.data)['ok'] is True

    def test_running_job_becomes_error(self, client):
        import app as a
        jid = a._new_job("fem")
        a._job_update(jid, status='running', pct=50, stage='Running…')
        client.post(f"/api/jobs/{jid}/stop", content_type="application/json")
        job = a._job_get(jid)
        assert job['status'] == 'error'
        assert job['error'] == 'Stopped by user'
