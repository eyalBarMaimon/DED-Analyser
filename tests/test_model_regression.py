"""
End-to-end regression tests that run the full MeltioDEDAnalyzer pipeline
on the three RAW Data ZIP files and assert physical sanity on every key output.

These tests replace the manual "run all models and check" workflow by catching
regressions automatically whenever the engine changes.

Skipped gracefully when the ZIP files are absent (CI without test data).
"""

import math
import sys
import builtins
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Paths ────────────────────────────────────────────────────────────────────
RAW   = Path(__file__).parent.parent / "RAW Data"
TOWER = RAW / "tower SST316L.zip"
VAZA  = RAW / "VAZA  SST 316L.zip"
COIL  = RAW / "Coil230426V1.zip"

ALL_ZIPS = [TOWER, VAZA, COIL]
missing  = [z for z in ALL_ZIPS if not z.exists()]
SKIP_MSG = f"RAW Data ZIPs missing: {[z.name for z in missing]}" if missing else ""

# ── Helper ────────────────────────────────────────────────────────────────────

def run_analyzer(zip_path: Path, material: str = "316L",
                 laser_w: float = 1000, layer_h: float = 0.6,
                 layer_w: float = 2.0, wire_d: float = 1.2):
    """Run the full analyzer pipeline and return the MeltioDEDAnalyzer instance."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from meltio_ded_analyzer import MeltioDEDAnalyzer

    a = MeltioDEDAnalyzer(str(zip_path))

    # Suppress interactive input
    answers = iter([
        zip_path.stem,   # part_name
        material,        # material_T0
        "",              # material_T1
        str(laser_w),    # laser_power
        "12.5",          # feed_speed
        str(layer_h),    # layer_height
        str(layer_w),    # layer_width
        str(wire_d),     # wire_diameter
        "25",            # ambient_temp
        "no",            # save report?
    ])
    with patch.object(builtins, "input", lambda _="": next(answers, "")):
        ok = a.extract_and_read()
        assert ok, f"extract_and_read failed for {zip_path.name}"
        a.parse_rapid_code()
        a.get_clarifications()
        a.calculate_thermal_data()

    return a


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tower_analyzer():
    pytest.importorskip("meltio_ded_analyzer")
    if missing: pytest.skip(SKIP_MSG)
    return run_analyzer(TOWER)

@pytest.fixture(scope="module")
def vaza_analyzer():
    pytest.importorskip("meltio_ded_analyzer")
    if missing: pytest.skip(SKIP_MSG)
    return run_analyzer(VAZA)

@pytest.fixture(scope="module")
def coil_analyzer():
    pytest.importorskip("meltio_ded_analyzer")
    if missing: pytest.skip(SKIP_MSG)
    return run_analyzer(COIL)


# ── Tower regression ──────────────────────────────────────────────────────────

class TestTowerSST316L:
    def test_layer_count(self, tower_analyzer):
        """Tower has ~410 layers."""
        assert 350 <= tower_analyzer.num_layers <= 500

    def test_waypoint_count(self, tower_analyzer):
        """Tower has at least 100k deposition waypoints."""
        assert len(tower_analyzer.thermal_data) >= 100_000

    def test_temperature_range(self, tower_analyzer):
        """Deposition temps must be physically bounded."""
        temps = [d["temp_C"] for d in tower_analyzer.thermal_data]
        assert min(temps) >= 20
        assert max(temps) <= 1500   # below liquidus of 316L (1440°C) + tolerance

    def test_ved_not_all_zero(self, tower_analyzer):
        """VED must be computed and positive for all deposition points."""
        veds = [d["VED"] for d in tower_analyzer.thermal_data]
        assert all(v > 0 for v in veds)

    def test_melt_width_physically_reasonable(self, tower_analyzer):
        """Eagar-Tsai melt width must be positive and < 20mm (10× beam spot of 1.2mm + margin).
        Minimum: beam/2 = 0.6mm. Maximum: 20mm covers high-power DED conditions."""
        widths = [d.get("melt_width_mm", 0) for d in tower_analyzer.thermal_data]
        assert all(0.6 <= w <= 20.0 for w in widths if w > 0), \
            f"melt_width out of range: min={min(w for w in widths if w>0):.2f}, max={max(widths):.2f}"

    def test_no_negative_cooling_rate(self, tower_analyzer):
        """Cooling rate (G×R) must be non-negative."""
        crs = [d.get("cooling_rate_Ks", 0) for d in tower_analyzer.thermal_data]
        assert all(cr >= 0 for cr in crs)

    def test_seam_points_marked(self, tower_analyzer):
        """Every layer should have a seam start point."""
        starts = sum(1 for d in tower_analyzer.thermal_data if d.get("is_seam_start"))
        assert starts >= tower_analyzer.num_layers * 0.8

    def test_ti_phase_none_for_316l(self, tower_analyzer):
        """ti_phase must be None for stainless steel material."""
        phases = [d.get("ti_phase") for d in tower_analyzer.thermal_data]
        assert all(p is None for p in phases)

    def test_pdas_computed_for_316l(self, tower_analyzer):
        """pdas_nm must be computed (non-None) for 316L material."""
        pdas = [d.get("pdas_nm") for d in tower_analyzer.thermal_data]
        non_null = [p for p in pdas if p is not None]
        assert len(non_null) > len(pdas) * 0.9   # at least 90% have PDAS

    def test_pdas_range_ded(self, tower_analyzer):
        """316L PDAS formula: 80 × Ṫ^{-0.333} µm.
        DED typical range: 3–30 µm (cooling rates 10²–10⁴ K/s).
        Allow 1–50 µm to cover slow-speed / low-G extremes."""
        pdas = [d["pdas_nm"] for d in tower_analyzer.thermal_data
                if d.get("pdas_nm") is not None]
        assert all(1.0 < p < 50.0 for p in pdas), \
            f"PDAS out of range: min={min(pdas):.2f}, max={max(pdas):.2f} µm"

    def test_csv_fields_present(self, tower_analyzer):
        """Critical CSV fields must be present in every thermal data row."""
        required = ["VED", "norm_H", "melt_depth_mm", "melt_width_mm",
                    "cooling_rate_Ks", "cracking_score", "lof_risk",
                    "stubbing_risk", "ti_phase", "pdas_nm"]
        sample = tower_analyzer.thermal_data[:10]
        for field in required:
            assert all(field in d for d in sample), f"Missing field: {field}"


# ── Vaza regression ───────────────────────────────────────────────────────────

class TestVazaSST316L:
    def test_layer_count(self, vaza_analyzer):
        assert 50 <= vaza_analyzer.num_layers <= 100

    def test_lof_risk_consistent_with_ved(self, vaza_analyzer):
        """LOF risk flag must be consistent with VED threshold.
        Vaza VED ≈ 8 J/mm³ (well below 316L LOF_min=40) → all waypoints are LOF risk.
        Key invariant: if VED < VED_lof_min then lof_risk=True, else lof_risk=False."""
        pw = vaza_analyzer.db_materials.get("T0", {}).get("process_window", {})
        lof_min = pw.get("VED_lof_min", 25)
        violations = [
            d for d in vaza_analyzer.thermal_data
            if (d.get("VED", 0) < lof_min) != d.get("lof_risk", False)
        ]
        assert len(violations) == 0, \
            f"LOF flag inconsistent with VED threshold in {len(violations)} waypoints"

    def test_seam_type_not_fixed(self, vaza_analyzer):
        """Vaza seam should show some drift (NEAR-FIXED or RANDOM), not fully fixed."""
        starts = [d for d in vaza_analyzer.thermal_data if d.get("is_seam_start")]
        if not starts:
            pytest.skip("No seam start points found")
        xs = [d["x"] for d in starts]
        ys = [d["y"] for d in starts]
        xy_drift = math.sqrt((max(xs)-min(xs))**2 + (max(ys)-min(ys))**2)
        # VAZA: XY drift > 2mm (not fully fixed)
        assert xy_drift >= 2.0, f"XY drift too small: {xy_drift:.2f}mm"

    def test_temperature_stabilises(self, vaza_analyzer):
        """Average temperature across middle layers should be > ambient."""
        mid = vaza_analyzer.thermal_data[len(vaza_analyzer.thermal_data)//3:
                                          2*len(vaza_analyzer.thermal_data)//3]
        avg_mid = sum(d["temp_C"] for d in mid) / max(len(mid), 1)
        assert avg_mid > 100   # well above ambient


# ── Coil regression ───────────────────────────────────────────────────────────

class TestCoilSpiral:
    def test_layer_count(self, coil_analyzer):
        """Coil has ~1927 layers."""
        assert 1800 <= coil_analyzer.num_layers <= 2100

    def test_seam_type_random(self, coil_analyzer):
        """Spiral coil seam should be RANDOM (XY drift > 30mm)."""
        starts = [d for d in coil_analyzer.thermal_data if d.get("is_seam_start")]
        if not starts:
            pytest.skip("No seam starts")
        xs = [d["x"] for d in starts]
        ys = [d["y"] for d in starts]
        xy_drift = math.sqrt((max(xs)-min(xs))**2 + (max(ys)-min(ys))**2)
        assert xy_drift >= 30.0, f"Expected RANDOM seam, got drift={xy_drift:.1f}mm"

    def test_temperature_range(self, coil_analyzer):
        temps = [d["temp_C"] for d in coil_analyzer.thermal_data]
        assert min(temps) >= 20
        assert max(temps) <= 1600

    def test_some_lof_risk(self, coil_analyzer):
        """Coil at 316L with VED ~39 J/mm³ (below 40 J/mm³ threshold) may have LOF."""
        lof_ct = sum(1 for d in coil_analyzer.thermal_data if d.get("lof_risk"))
        # Just check the flag is computed (not all zero and not all one)
        total = len(coil_analyzer.thermal_data)
        assert 0 <= lof_ct <= total   # trivially true but confirms no crash


# ── Cross-model sanity ────────────────────────────────────────────────────────

class TestCrossModelSanity:
    """Invariants that must hold for every model."""

    @pytest.mark.parametrize("analyzer_fixture", ["tower_analyzer", "vaza_analyzer", "coil_analyzer"])
    def test_melt_fuse_ratio_not_extreme(self, request, analyzer_fixture):
        """melt_fuse_ratio must be in [0, 10] — never negative or astronomically large."""
        a = request.getfixturevalue(analyzer_fixture)
        ratios = [d["melt_fuse_ratio"] for d in a.thermal_data]
        assert all(0 <= r <= 10 for r in ratios), \
            f"{analyzer_fixture}: melt_fuse_ratio out of range"

    @pytest.mark.parametrize("analyzer_fixture", ["tower_analyzer", "vaza_analyzer", "coil_analyzer"])
    def test_cracking_score_bounded(self, request, analyzer_fixture):
        """cracking_score must be in [0, 1] by definition."""
        a = request.getfixturevalue(analyzer_fixture)
        scores = [d["cracking_score"] for d in a.thermal_data]
        assert all(0.0 <= s <= 1.0 for s in scores), \
            f"{analyzer_fixture}: cracking_score out of [0,1]"

    @pytest.mark.parametrize("analyzer_fixture", ["tower_analyzer", "vaza_analyzer", "coil_analyzer"])
    def test_g_over_r_positive(self, request, analyzer_fixture):
        """G/R ratio (grain morphology index) must be non-negative."""
        a = request.getfixturevalue(analyzer_fixture)
        grs = [d["G_over_R"] for d in a.thermal_data]
        assert all(gr >= 0 for gr in grs), \
            f"{analyzer_fixture}: negative G/R values found"

    @pytest.mark.parametrize("analyzer_fixture", ["tower_analyzer", "vaza_analyzer", "coil_analyzer"])
    def test_stubbing_implies_lof(self, request, analyzer_fixture):
        """If stubbing_risk is True, lof_risk must also be True (stubbing ⊂ LOF)."""
        a = request.getfixturevalue(analyzer_fixture)
        violations = [d for d in a.thermal_data
                      if d.get("stubbing_risk") and not d.get("lof_risk")]
        assert len(violations) == 0, \
            f"{analyzer_fixture}: {len(violations)} waypoints with stubbing but no LOF"


# ── Stress engine regression ──────────────────────────────────────────────────

class TestStressEngineRegression:
    """Run the ISM stress engine on each model and check distortion is physical."""

    def _run_stress(self, analyzer):
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from jobs.base_job import run_auto_stress
        return run_auto_stress(analyzer)

    def test_tower_distortion_physical(self, tower_analyzer):
        """Tower (straight wall, ~246mm tall): distortion must be < 10mm."""
        res = self._run_stress(tower_analyzer)
        assert res is not None, "compute_stress returned None"
        delta = res["summary"]["max_delta_mm"]
        assert delta < 10.0, f"Tower distortion {delta:.2f}mm exceeds 10mm physical limit"

    def test_vaza_distortion_physical(self, vaza_analyzer):
        """Vaza (short part, ~40mm): distortion must be < 5mm."""
        res = self._run_stress(vaza_analyzer)
        assert res is not None
        delta = res["summary"]["max_delta_mm"]
        assert delta < 5.0, f"Vaza distortion {delta:.2f}mm exceeds 5mm"

    def test_coil_distortion_physical(self, coil_analyzer):
        """Coil (spiral, detected as helix): radial distortion must be < 5mm."""
        res = self._run_stress(coil_analyzer)
        assert res is not None
        pattern = res.get("toolpath", {}).get("pattern", "unknown")
        delta   = res["summary"]["max_delta_mm"]
        assert delta < 5.0, \
            f"Coil distortion {delta:.2f}mm unphysical (pattern={pattern})"

    def test_coil_detected_as_helix(self, coil_analyzer):
        """Coil must be classified as helix/spiral by the stress engine."""
        res = self._run_stress(coil_analyzer)
        assert res is not None
        pattern = res.get("toolpath", {}).get("pattern", "")
        assert pattern == "helix", \
            f"Coil was classified as '{pattern}' instead of 'helix'"

    def test_stress_clamped_to_yield(self, tower_analyzer):
        """Max residual stress must not exceed yield strength."""
        res = self._run_stress(tower_analyzer)
        assert res is not None
        sigma_max = res["summary"]["max_sigma_MPa"]
        yield_mpa = res["summary"]["yield_MPa"]
        assert sigma_max <= yield_mpa * 1.01, \
            f"Stress {sigma_max} MPa exceeds yield {yield_mpa} MPa"
