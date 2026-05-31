"""
Tests for meltio_ded_analyzer utilities: fuzzy_match_material, _print_time_summary.
Also covers /api/rapid/preview with invalid Parameters.txt values.
"""
import sys, os, io, json, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import app as flask_app
from meltio_ded_analyzer import fuzzy_match_material, load_materials_db


@pytest.fixture(scope="module")
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture(scope="module")
def db():
    return load_materials_db()


# ── fuzzy_match_material ──────────────────────────────────────────────────────

class TestFuzzyMatchMaterial:
    def test_exact_id_match(self, db):
        mat = fuzzy_match_material("SS316L", db)
        assert mat is not None
        assert "316" in mat["display_name"].lower() or "316" in mat["id"].lower()

    def test_case_insensitive(self, db):
        upper = fuzzy_match_material("SS316L", db)
        lower = fuzzy_match_material("ss316l", db)
        assert upper is not None
        assert lower is not None
        assert upper["id"] == lower["id"]

    def test_partial_alias_match(self, db):
        # "316L" should match SS316L via alias or name substring
        mat = fuzzy_match_material("316L", db)
        assert mat is not None

    def test_no_match_returns_none(self, db):
        result = fuzzy_match_material("Unobtanium9000", db)
        assert result is None

    def test_whitespace_stripped(self, db):
        mat = fuzzy_match_material("  SS316L  ", db)
        assert mat is not None

    def test_empty_string_returns_none(self, db):
        result = fuzzy_match_material("", db)
        assert result is None

    def test_titanium_match(self, db):
        mat = fuzzy_match_material("Ti-6Al-4V", db)
        assert mat is not None

    def test_returns_dict_with_required_keys(self, db):
        mat = fuzzy_match_material("SS316L", db)
        if mat:
            for key in ("id", "display_name", "thermal_conductivity", "density", "melting_point"):
                assert key in mat

    def test_titanium_cp_not_matched_as_ti64(self, db):
        """'Titanium CP Grade 2' must resolve to titanium_cp, not titanium_ti64."""
        mat = fuzzy_match_material("Titanium CP Grade 2", db)
        assert mat is not None
        assert mat["id"] == "titanium_cp", (
            f"Expected 'titanium_cp', got '{mat['id']}'. "
            "Short alias 'titanium' (ti64) must not shadow the exact display_name."
        )

    def test_exact_display_name_beats_substring_alias(self, db):
        """Exact display_name match wins over a shorter alias that is a substring."""
        mat = fuzzy_match_material("Titanium Ti-6Al-4V", db)
        assert mat is not None
        assert mat["id"] == "titanium_ti64"

    def test_ti64_still_matches_by_alias(self, db):
        """Ti64 must still match via alias."""
        mat = fuzzy_match_material("Ti64", db)
        assert mat is not None
        assert mat["id"] == "titanium_ti64"


# ── _print_time_summary ───────────────────────────────────────────────────────

class TestPrintTimeSummary:
    def _make_analyzer(self, layer_times, num_layers, inert=False, dwell=0):
        from meltio_ded_analyzer import MeltioDEDAnalyzer
        import tempfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dummy.mod", "MODULE Test\nENDMODULE")
        buf.seek(0)
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp.write(buf.read()); tmp.close()
        ana = MeltioDEDAnalyzer(tmp.name)
        ana.layer_times = layer_times
        ana.num_layers  = num_layers
        ana.user = {"min_layer_dwell": dwell, "inert_environment": inert}
        return ana

    def test_empty_layer_times_no_crash(self):
        ana = self._make_analyzer({}, num_layers=0)
        result = ana._print_time_summary()
        assert result["per_layer_avg"] == 0
        assert result["raw_secs"] == 0

    def test_single_layer(self):
        ana = self._make_analyzer({1: 100}, num_layers=1)
        result = ana._print_time_summary()
        assert result["raw_secs"] == 100
        assert result["per_layer_avg"] == 100.0

    def test_inert_adds_two_hours(self):
        ana = self._make_analyzer({1: 60, 2: 60}, num_layers=2, inert=True)
        result = ana._print_time_summary()
        assert result["inert_secs"] == 7200
        assert result["total_secs"] == 60 + 60 + 7200

    def test_dwell_multiplied_by_layers(self):
        ana = self._make_analyzer({1: 60, 2: 60}, num_layers=2, dwell=30)
        result = ana._print_time_summary()
        assert result["dwell_secs"] == 60   # 30s × 2 layers

    def test_total_fmt_is_string(self):
        ana = self._make_analyzer({1: 3661}, num_layers=1)
        result = ana._print_time_summary()
        assert isinstance(result["total_fmt"], str)
        assert len(result["total_fmt"]) > 0


# ── /api/rapid/preview with invalid Parameters.txt ───────────────────────────

class TestRapidPreviewInvalidParams:
    def _zip_with_params(self, content):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Parameters.txt", content)
        buf.seek(0)
        return buf

    def test_zero_layer_height_returns_200(self, client):
        buf = self._zip_with_params(
            "Deposition Height: 0\nBase Print Speed: 10\n")
        r = client.post("/api/rapid/preview",
                        data={"zip_file": (buf, "test.zip")},
                        content_type="multipart/form-data")
        assert r.status_code == 200   # should not crash

    def test_negative_speed_returns_200(self, client):
        buf = self._zip_with_params(
            "Base Print Speed: -10\nDeposition Height: 0.5\n")
        r = client.post("/api/rapid/preview",
                        data={"zip_file": (buf, "test.zip")},
                        content_type="multipart/form-data")
        assert r.status_code == 200

    def test_non_numeric_value_returns_200(self, client):
        buf = self._zip_with_params(
            "Base Print Speed: abc\nDeposition Height: xyz\n")
        r = client.post("/api/rapid/preview",
                        data={"zip_file": (buf, "test.zip")},
                        content_type="multipart/form-data")
        assert r.status_code == 200

    def test_empty_params_file_returns_200(self, client):
        buf = self._zip_with_params("")
        r = client.post("/api/rapid/preview",
                        data={"zip_file": (buf, "test.zip")},
                        content_type="multipart/form-data")
        assert r.status_code == 200
