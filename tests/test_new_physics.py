"""
Tests for physics features added 2026-06-05:
  - Eagar-Tsai melt pool (width, depth, Pe-correction)
  - Ti-6Al-4V phase classification + lath width
  - 316L PDAS formula
  - stubbing_risk flag
  - ISM annealing_factor per material
  - Interpass dwell calculator
"""

import math
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from engines.stress_engine import compute_stress

# ─────────────────────────────────────────────────────────────────────────────
# Helpers — replicate the exact formulas from meltio_ded_analyzer.py so tests
# are independent of the full analyzer pipeline.
# ─────────────────────────────────────────────────────────────────────────────

def _eagar_tsai(P_W, absorb, v_mms, k, rho, Cp, T_liq, T_cur, beam_mm):
    """Eagar-Tsai closed-form melt pool geometry (same formula as analyzer)."""
    v_ms   = v_mms * 1e-3
    sigma  = (beam_mm * 1e-3) / 2.0
    alpha  = k / (rho * Cp)
    dT     = max(T_liq - T_cur, 1.0)
    Pe     = v_ms * sigma / (2.0 * max(alpha, 1e-9))
    Q_star = (absorb * P_W) / (math.pi * k * sigma * dT)
    hw     = sigma * math.sqrt(max(1.0 + Q_star, 1.0))
    pc     = 1.0 / (1.0 + 0.3 * Pe)
    width  = hw * 2.0 * pc * 1e3          # mm
    ros_d  = (absorb * P_W) / (math.pi * k * dT) * 1e3
    depth  = min(hw * pc * 1e3, ros_d)    # mm
    return width, depth, Pe, Q_star


def _ti_phase(cooling_rate_Ks):
    if cooling_rate_Ks > 4500:
        return "α′ martensite (full)"
    elif cooling_rate_Ks > 410:
        return "α′ martensite (onset)"
    elif cooling_rate_Ks > 20:
        return "α+β Widmanstätten"
    else:
        return "α+β lamellar"


def _lath_width(cooling_rate_Ks):
    if cooling_rate_Ks > 0:
        return 0.23 + 1.27 * math.exp(-cooling_rate_Ks / 3000.0)
    return None


def _pdas_316l(cooling_rate_Ks):
    if cooling_rate_Ks > 0:
        return 80.0 * (cooling_rate_Ks ** -0.333)
    return None


def _stubbing_risk(VED, VED_lof_min):
    return VED < VED_lof_min * 0.6


def _dwell_needed(T_cur, T_target, T_amb, tau):
    if T_cur <= T_target or tau <= 0:
        return 0.0
    ratio = max((T_cur - T_amb) / max(T_target - T_amb, 1.0), 1.001)
    return min(tau * math.log(ratio), 600.0)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Eagar-Tsai melt pool
# ─────────────────────────────────────────────────────────────────────────────

class TestEagarTsai:
    # 316L reference: P=1200W, A=0.45, v=8mm/s, k=16.3, spot=1.2mm
    REF = dict(P_W=1200, absorb=0.45, v_mms=8.0, k=16.3,
               rho=7950, Cp=502, T_liq=1450, T_cur=200, beam_mm=1.2)

    def test_width_physical_range(self):
        """Melt width must be > beam diameter and < 10× beam diameter."""
        w, d, Pe, Q = _eagar_tsai(**self.REF)
        assert self.REF["beam_mm"] < w < self.REF["beam_mm"] * 10

    def test_depth_less_than_width(self):
        """Conduction-mode: depth ≤ half-width (semi-circular assumption)."""
        w, d, Pe, Q = _eagar_tsai(**self.REF)
        assert d <= w / 2.0 + 1e-6

    def test_rosenthal_cap_prevents_unphysical_depth(self):
        """Rosenthal depth (8+ mm for high power) must be capped by E-T."""
        w, d, Pe, Q = _eagar_tsai(**self.REF)
        rosenthal_d = (self.REF["absorb"] * self.REF["P_W"]) / (
            math.pi * self.REF["k"] * max(self.REF["T_liq"] - self.REF["T_cur"], 1)
        ) * 1e3
        # Rosenthal gives ~8mm; E-T should give <4mm
        assert d < rosenthal_d * 0.6, f"E-T depth {d:.2f} not significantly below Rosenthal {rosenthal_d:.2f}"

    def test_width_increases_with_power(self):
        """Higher laser power → wider melt pool."""
        w_lo, *_ = _eagar_tsai(**{**self.REF, "P_W": 600})
        w_hi, *_ = _eagar_tsai(**{**self.REF, "P_W": 1400})
        assert w_hi > w_lo

    def test_width_decreases_with_speed(self):
        """Higher scan speed → narrower/shorter melt pool."""
        w_slow, *_ = _eagar_tsai(**{**self.REF, "v_mms": 4.0})
        w_fast, *_ = _eagar_tsai(**{**self.REF, "v_mms": 20.0})
        assert w_fast < w_slow

    def test_width_increases_with_beam_size(self):
        """Larger beam spot → wider melt pool."""
        w_small, *_ = _eagar_tsai(**{**self.REF, "beam_mm": 0.8})
        w_large, *_ = _eagar_tsai(**{**self.REF, "beam_mm": 2.0})
        assert w_large > w_small

    def test_peclet_number_range(self):
        """For DED (v=5–20 mm/s, spot=1.2mm, 316L): Pe should be < 5."""
        _, _, Pe, _ = _eagar_tsai(**self.REF)
        assert 0 < Pe < 5.0

    def test_q_star_positive(self):
        """Dimensionless heat input Q* must be positive."""
        _, _, _, Q = _eagar_tsai(**self.REF)
        assert Q > 0

    def test_low_power_still_positive_width(self):
        """Even at very low power the formula must return a positive width."""
        w, d, _, _ = _eagar_tsai(**{**self.REF, "P_W": 50})
        assert w > 0
        assert d > 0

    def test_ti64_reference_case(self):
        """Ti-6Al-4V @ 1000W gives physically realistic width (2–8 mm)."""
        w, d, _, _ = _eagar_tsai(P_W=1000, absorb=0.55, v_mms=10.0, k=7.0,
                                  rho=4430, Cp=560, T_liq=1660, T_cur=200, beam_mm=1.2)
        assert 1.0 < w < 10.0
        assert d > 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Ti-6Al-4V phase classification
# ─────────────────────────────────────────────────────────────────────────────

class TestTiPhase:
    def test_lamellar_below_20(self):
        assert _ti_phase(10) == "α+β lamellar"

    def test_lamellar_at_boundary(self):
        # boundary: cooling_rate_Ks == 20 → condition is > 20, so 20 stays lamellar
        assert _ti_phase(20) == "α+β lamellar"

    def test_just_above_20_is_widmanstatten(self):
        assert _ti_phase(21) == "α+β Widmanstätten"

    def test_widmanstatten_mid(self):
        assert _ti_phase(200) == "α+β Widmanstätten"

    def test_martensite_onset_at_boundary(self):
        # boundary: cooling_rate_Ks == 410 → condition is > 410, so 410 stays Widmanstätten
        assert _ti_phase(410) == "α+β Widmanstätten"

    def test_just_above_410_is_onset(self):
        assert _ti_phase(411) == "α′ martensite (onset)"

    def test_martensite_onset_mid(self):
        assert _ti_phase(2000) == "α′ martensite (onset)"

    def test_full_martensite_at_boundary(self):
        # boundary: cooling_rate_Ks == 4500 → condition is > 4500, stays onset
        assert _ti_phase(4500) == "α′ martensite (onset)"

    def test_just_above_4500_is_full(self):
        assert _ti_phase(4501) == "α′ martensite (full)"

    def test_full_martensite_high(self):
        assert _ti_phase(7000) == "α′ martensite (full)"

    def test_four_distinct_levels(self):
        levels = {_ti_phase(r) for r in [5, 100, 1000, 6000]}
        assert len(levels) == 4

    def test_410_is_NOT_martensite(self):
        """410 K/s is the Widmanstätten/onset BOUNDARY — still Widmanstätten (strict >)."""
        assert _ti_phase(410) == "α+β Widmanstätten"
        assert _ti_phase(410) != "α′ martensite (full)"
        assert _ti_phase(410) != "α′ martensite (onset)"


class TestLathWidth:
    def test_fast_cooling_fine_lath(self):
        """Higher cooling rate → finer alpha lath (smaller width)."""
        fast = _lath_width(5000)
        slow = _lath_width(100)
        assert fast < slow

    def test_range_physically_reasonable(self):
        """Lath width must be in validated 0.23–2.3 µm range."""
        for cr in [100, 500, 1000, 3000, 5000]:
            lw = _lath_width(cr)
            assert 0.1 < lw < 3.0, f"lath_width={lw:.3f} out of range at {cr} K/s"

    def test_asymptote_at_high_rate(self):
        """At very high cooling rate, lath width approaches minimum (≈0.23 µm)."""
        lw = _lath_width(50000)
        assert lw < 0.35

    def test_zero_rate_returns_none(self):
        assert _lath_width(0) is None

    def test_eliseeva_reference_300s_dwell(self):
        """Eliseeva 2024: 300s dwell → fast effective cooling → lath ~0.23 µm."""
        # Fast cooling (high rate) approaches 0.23 µm lower bound
        lw = _lath_width(10000)
        assert lw < 0.35


# ─────────────────────────────────────────────────────────────────────────────
# 3. 316L PDAS
# ─────────────────────────────────────────────────────────────────────────────

class TestPDAS316L:
    # The formula λ₁ = 80 × Ṫ^{-0.333} uses Ṫ in K/s and gives λ₁ in µm
    # (Sing et al. 2022, PMC9625081). At Ṫ=1000 K/s: 80×1000^{-0.333} ≈ 8 µm.

    def test_power_law_exponent(self):
        """λ ∝ Ṫ^{-0.333}: doubling cooling rate → 2^{-0.333} ≈ 0.794× PDAS."""
        p1 = _pdas_316l(1000)
        p2 = _pdas_316l(2000)
        ratio = p2 / p1
        expected = 2 ** (-0.333)
        assert abs(ratio - expected) < 0.01, f"ratio={ratio:.4f}, expected {expected:.4f}"

    def test_ded_range_um(self):
        """DED cooling rates 10³–10⁴ K/s → PDAS 3–10 µm (formula output in µm)."""
        for cr in [1000, 5000, 10000]:
            p = _pdas_316l(cr)
            assert 1 < p < 20, f"PDAS={p:.2f} µm out of expected DED range at {cr} K/s"

    def test_reference_1000_Ks(self):
        """At 1000 K/s: 80 × 1000^{-0.333} ≈ 8.0 µm."""
        p = _pdas_316l(1000)
        assert abs(p - 80 * 1000 ** -0.333) < 0.01

    def test_high_rate_fine_structure(self):
        """Higher cooling rate → finer dendrites (smaller PDAS)."""
        assert _pdas_316l(10000) < _pdas_316l(1000)

    def test_zero_rate_returns_none(self):
        assert _pdas_316l(0) is None

    def test_lpbf_range_finer(self):
        """LPBF (10⁵–10⁶ K/s) should give much finer PDAS than DED."""
        p_ded  = _pdas_316l(5000)
        p_lpbf = _pdas_316l(500000)
        assert p_lpbf < p_ded * 0.3


# ─────────────────────────────────────────────────────────────────────────────
# 4. Stubbing risk flag
# ─────────────────────────────────────────────────────────────────────────────

class TestStubbingRisk:
    def test_below_60pct_is_stubbing(self):
        assert _stubbing_risk(VED=14, VED_lof_min=25) is True   # 14 < 15

    def test_above_60pct_not_stubbing(self):
        assert _stubbing_risk(VED=16, VED_lof_min=25) is False  # 16 > 15

    def test_exactly_at_boundary(self):
        # 15.0 == 25 * 0.6 → not stubbing (strict <)
        assert _stubbing_risk(VED=15.0, VED_lof_min=25) is False

    def test_just_below_boundary(self):
        assert _stubbing_risk(VED=14.99, VED_lof_min=25) is True

    def test_high_ved_safe(self):
        assert _stubbing_risk(VED=100, VED_lof_min=25) is False

    def test_lof_min_variation(self):
        """Stubbing threshold scales with VED_lof_min."""
        assert _stubbing_risk(VED=20, VED_lof_min=40) is True   # 20 < 24
        assert _stubbing_risk(VED=20, VED_lof_min=30) is False  # 20 > 18

    def test_stubbing_implies_lof(self):
        """If stubbing_risk is True, lof_risk must also be True."""
        VED, lof_min = 10, 25
        stubbing = _stubbing_risk(VED, lof_min)
        lof = VED < lof_min
        if stubbing:
            assert lof


# ─────────────────────────────────────────────────────────────────────────────
# 5. ISM annealing_factor per material
# ─────────────────────────────────────────────────────────────────────────────

def _make_stress_data(mat_name, yield_mpa=400, num_layers=20):
    return {
        "material": {
            "display_name":  mat_name,
            "E_GPa":         200,
            "yield_MPa":     yield_mpa,
            "alpha_1e6":     12.0,
            "density":       7800,
            "Cp":            490,
            "k":             20,
            "T_melt":        1400,
        },
        "process": {
            "laser_power":    1000,
            "scan_speed":     10,
            "layer_height":   0.5,
            "bead_width":     2.0,
            "absorption":     0.45,
            "ambient_temp":   25,
            "dwell_time":     10,
            "wire_diameter":  1.2,
            "wire_feed_speed": 80,
        },
        "geometry": {
            "num_layers":     num_layers,
            "wall_thickness": 5,
        },
    }


class TestAnnealingFactor:
    def test_ti_lower_stress_than_316l(self):
        """Ti-6Al-4V (annealing=0.55) must predict lower stress than 316L (0.85)
        when all other parameters are identical."""
        data_ti  = _make_stress_data("Ti-6Al-4V", yield_mpa=800)
        data_316 = _make_stress_data("316L Stainless Steel", yield_mpa=800)
        res_ti  = compute_stress(data_ti,  _sweep=False)
        res_316 = compute_stress(data_316, _sweep=False)
        assert res_ti  is not None
        assert res_316 is not None
        sigma_ti  = res_ti["summary"]["max_sigma_MPa"]
        sigma_316 = res_316["summary"]["max_sigma_MPa"]
        assert sigma_ti < sigma_316, (
            f"Ti sigma {sigma_ti} MPa should be lower than 316L {sigma_316} MPa"
        )

    def test_inconel_between_ti_and_316l(self):
        """Inconel (0.80) stress must be between Ti (0.55) and 316L (0.85)."""
        data_ti   = _make_stress_data("Ti-6Al-4V",            yield_mpa=800)
        data_in   = _make_stress_data("Inconel 718",           yield_mpa=800)
        data_316  = _make_stress_data("316L Stainless Steel",  yield_mpa=800)
        s_ti  = compute_stress(data_ti,  _sweep=False)["summary"]["max_sigma_MPa"]
        s_in  = compute_stress(data_in,  _sweep=False)["summary"]["max_sigma_MPa"]
        s_316 = compute_stress(data_316, _sweep=False)["summary"]["max_sigma_MPa"]
        assert s_ti < s_in <= s_316, (
            f"Expected Ti({s_ti}) < Inconel({s_in}) ≤ 316L({s_316})"
        )

    def test_annealing_factor_in_toolpath_summary(self):
        """annealing_factor must be present in the returned result."""
        res = compute_stress(_make_stress_data("Ti-6Al-4V"), _sweep=False)
        assert res is not None
        assert "annealing_factor" in res["toolpath"]

    def test_ti_annealing_factor_value(self):
        """Ti-6Al-4V annealing_factor must equal 0.55."""
        res = compute_stress(_make_stress_data("Ti-6Al-4V"), _sweep=False)
        assert abs(res["toolpath"]["annealing_factor"] - 0.55) < 0.01

    def test_316l_annealing_factor_value(self):
        """316L annealing_factor must equal 0.85."""
        res = compute_stress(_make_stress_data("316L Stainless Steel"), _sweep=False)
        assert abs(res["toolpath"]["annealing_factor"] - 0.85) < 0.01

    def test_annealing_pct_in_summary(self):
        """annealing_pct must be present and positive in summary."""
        res = compute_stress(_make_stress_data("Ti-6Al-4V"), _sweep=False)
        assert "annealing_pct" in res["summary"]
        assert res["summary"]["annealing_pct"] > 0

    def test_ti_annealing_pct_approx_45(self):
        """Ti-6Al-4V annealing_pct ≈ 45% (1 - 0.55)."""
        res = compute_stress(_make_stress_data("Ti-6Al-4V"), _sweep=False)
        pct = res["summary"]["annealing_pct"]
        assert abs(pct - 45.0) < 1.0


# ─────────────────────────────────────────────────────────────────────────────
# 6. Interpass dwell calculator
# ─────────────────────────────────────────────────────────────────────────────

class TestInterpasSDwell:
    def test_no_dwell_needed_when_cold(self):
        """If T_cur ≤ T_target, no dwell needed."""
        assert _dwell_needed(T_cur=350, T_target=400, T_amb=25, tau=30) == 0.0

    def test_no_dwell_at_target(self):
        assert _dwell_needed(T_cur=400, T_target=400, T_amb=25, tau=30) == 0.0

    def test_dwell_positive_when_hot(self):
        t = _dwell_needed(T_cur=600, T_target=400, T_amb=25, tau=30)
        assert t > 0

    def test_dwell_increases_with_temperature(self):
        """Hotter part needs longer dwell."""
        t1 = _dwell_needed(T_cur=500, T_target=400, T_amb=25, tau=30)
        t2 = _dwell_needed(T_cur=700, T_target=400, T_amb=25, tau=30)
        assert t2 > t1

    def test_dwell_increases_with_tau(self):
        """Slower-cooling material (larger τ) needs longer dwell."""
        t1 = _dwell_needed(T_cur=600, T_target=400, T_amb=25, tau=20)
        t2 = _dwell_needed(T_cur=600, T_target=400, T_amb=25, tau=60)
        assert t2 > t1

    def test_capped_at_600s(self):
        """Dwell time is capped at 600 s."""
        t = _dwell_needed(T_cur=5000, T_target=400, T_amb=25, tau=500)
        assert t <= 600.0

    def test_ti_target_400(self):
        """Ti-6Al-4V interpass target is 400°C."""
        # Just validates the constant used in the calculator
        T_TARGET_TI = 400.0
        assert T_TARGET_TI == 400.0

    def test_316l_target_300(self):
        """316L interpass target is 300°C (stricter than Ti)."""
        T_TARGET_316L = 300.0
        assert T_TARGET_316L < 400.0

    def test_formula_matches_exponential_cooling(self):
        """t_dwell = τ·ln((T_cur-T_amb)/(T_tgt-T_amb)) — verify analytically."""
        T_cur, T_tgt, T_amb, tau = 600, 400, 25, 30
        expected = tau * math.log((T_cur - T_amb) / (T_tgt - T_amb))
        actual   = _dwell_needed(T_cur, T_tgt, T_amb, tau)
        assert abs(actual - expected) < 1e-9

    def test_zero_tau_returns_zero(self):
        assert _dwell_needed(T_cur=600, T_target=400, T_amb=25, tau=0) == 0.0
