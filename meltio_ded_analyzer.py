#!/usr/bin/env python3
"""
Meltio DED Code Analysis Agent - v2.0
Analyzes RAPID .mod files from Meltio DED printing processes.
Includes thermal analysis and heat map visualizations.
"""

import os
import sys
import re

# Ensure stdout can handle Unicode (α, β, ✅, etc.) on Windows cp1252 terminals.
# Applied at module level so it works whether called from CLI or Flask.
import io as _io
if hasattr(sys.stdout, 'buffer') and getattr(sys.stdout, 'encoding', 'utf-8').lower() not in ('utf-8', 'utf8'):
    try:
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass
if hasattr(sys.stderr, 'buffer') and getattr(sys.stderr, 'encoding', 'utf-8').lower() not in ('utf-8', 'utf8'):
    try:
        sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass
import json
import math
import zipfile
import csv
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────
# MATERIALS DATABASE
# ─────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "materials_database.json"

def load_materials_db() -> List[Dict]:
    if not DB_PATH.exists():
        print(f"⚠️  materials_database.json not found at {DB_PATH}")
        return []
    with open(DB_PATH, "r") as f:
        return json.load(f).get("materials", [])

def fuzzy_match_material(name: str, db: List[Dict]) -> Optional[Dict]:
    """Match user input to a material DB entry using a priority-ordered 4-pass search.

    Pass 1: exact id match
    Pass 2: exact display_name match
    Pass 3: exact alias match
    Pass 4: substring alias match (most permissive, last resort)

    Each pass scans ALL materials before falling through to the next, so a specific
    display_name like "Titanium CP Grade 2" is never shadowed by a short alias
    (e.g. "titanium") that appears earlier in the DB.
    """
    name_lower = name.strip().lower()
    if not name_lower:
        return None
    for mat in db:
        if name_lower == mat["id"].lower():
            return mat
    for mat in db:
        if name_lower == mat["display_name"].lower():
            return mat
    for mat in db:
        for alias in mat.get("aliases", []):
            if name_lower == alias.lower():
                return mat
    for mat in db:
        for alias in mat.get("aliases", []):
            if name_lower in alias.lower() or alias.lower() in name_lower:
                return mat
    return None


# ─────────────────────────────────────────────
# COLOR UTILITIES FOR HEAT MAPS
# ─────────────────────────────────────────────

def heat_color(value: float, min_val: float, max_val: float) -> str:
    """Map a value to a blue→yellow→red CSS color."""
    if max_val == min_val:
        t = 0.5
    else:
        t = (value - min_val) / (max_val - min_val)
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        s = t * 2
        r = int(0 + s * 255)
        g = int(0 + s * 255)
        b = int(255 - s * 255)
    else:
        s = (t - 0.5) * 2
        r = 255
        g = int(255 - s * 255)
        b = 0
    return f"rgb({r},{g},{b})"

def heat_color_hex(value: float, min_val: float, max_val: float) -> str:
    css = heat_color(value, min_val, max_val)
    nums = re.findall(r'\d+', css)
    r, g, b = int(nums[0]), int(nums[1]), int(nums[2])
    return f"#{r:02x}{g:02x}{b:02x}"


# ─────────────────────────────────────────────
# MAIN ANALYZER CLASS
# ─────────────────────────────────────────────

class MeltioDEDAnalyzer:

    # ENGINE I/O mapping (Shoham-specific)
    IO_MAP = {
        "DO_ENGINE_02": "Start Deposition - T0 Feeder",
        "DO_ENGINE_03": "Start Deposition - T1 Feeder",
        "DO_ENGINE_05": "Change to T0 (Material Feeder)",
        "DO_ENGINE_06": "Change to T1 (Material Feeder)",
        "DI_ENGINE_02": "Confirmation - Start Deposition T0",
        "DI_ENGINE_03": "Confirmation - Start Deposition T1",
        "DI_ENGINE_04": "End Deposition / FERS",
        "DI_ENGINE_05": "Confirmation - Change to T0",
        "DI_ENGINE_06": "Confirmation - Change to T1",
    }

    def __init__(self, zip_path: str):
        self.zip_path = zip_path
        self.part_name = Path(zip_path).stem
        self.mod_files: Dict[str, str] = {}          # filename → content
        self.materials_db = load_materials_db()

        # Analysis results
        self.num_layers = 0
        self.all_speeds: List[float] = []
        self.materials_found: set = set()
        self.digital_ios: Dict[str, List[str]] = defaultdict(list)
        self.print_sequence: List[str] = []
        self.waypoints: List[Dict] = []              # {layer_num,x,y,z,speed,material,is_deposition}
        self.material_changes: List[Dict] = []       # per-layer change events
        self.thermal_data: List[Dict] = []           # per waypoint thermal values
        self.db_materials: Dict[str, Optional[Dict]] = {"T0": None, "T1": None}
        self.layer_times: Dict[int, float] = {}      # layer_num → time in seconds

        # User inputs
        self.user: Dict = {}

    # ─── STEP 1: EXTRACT ZIP ────────────────────────────────────────────────

    def extract_and_read(self) -> bool:
        try:
            with zipfile.ZipFile(self.zip_path, "r") as zf:
                mod_names = sorted([f for f in zf.namelist() if f.endswith(".mod")])
                if not mod_names:
                    print("❌ No .mod files found in ZIP")
                    return False
                for name in mod_names:
                    self.mod_files[name] = zf.read(name).decode("utf-8", errors="ignore")
            self.num_layers = len(self.mod_files)
            print(f"✅ Extracted {self.num_layers} layer files")
            return True
        except zipfile.BadZipFile:
            print("❌ Invalid ZIP file")
            return False
        except Exception as e:
            print(f"❌ Error extracting ZIP: {e}")
            return False

    # ─── STEP 2: PARSE RAPID CODE ───────────────────────────────────────────

    def parse_rapid_code(self):
        """Parse all .mod files: extract coordinates, speeds, I/O, material changes."""

        # MoveL: skip exactly 3 inner brackets (orientation, config, extax) then read speed block.
        # This avoids .*? greedily matching [9E+09,...] as the speed block.
        move_re = re.compile(
            r'MoveL\s+\[\[(-?\d+\.?\d*),\s*(-?\d+\.?\d*),\s*(-?\d+\.?\d*)\]'
            r'(?:[^\[]*\[[^\]]*\]){3}'
            r'\],\[([-\d.]+),'
        )
        signal_re     = re.compile(r'Set[DI]O[^,]*,?\s*(DO_ENGINE_\d+),?\s*(\d)')
        comment_re    = re.compile(r'!(.*?)$', re.MULTILINE)
        layer_num_re  = re.compile(r'!Layer:\s*(\d+)\s*;')
        layer_time_re = re.compile(r'!Layer Time:\s*([\d.]+)\s*;')

        active_material   = "T0"   # default feeder at start
        deposition_active = False  # True between DO_ENGINE_02/03=1 and DO_ENGINE_04=1
        pending_change: Optional[str] = None

        for layer_idx, (layer_name, content) in enumerate(self.mod_files.items()):
            layer_num = layer_idx + 1

            # ── Extract layer number and time from comments ───────────────
            ln_match = layer_num_re.search(content)
            lt_match = layer_time_re.search(content)
            parsed_layer_num = int(ln_match.group(1)) if ln_match else layer_num
            if lt_match:
                self.layer_times[parsed_layer_num] = float(lt_match.group(1))

            # ── Single pass: process signals and MoveL in document order ──
            for line in content.split("\n"):

                # ── ENGINE signals ────────────────────────────────────────
                sig_m = signal_re.search(line)
                if sig_m:
                    signal = sig_m.group(1)
                    value  = sig_m.group(2)
                    self.digital_ios[layer_name].append(signal)

                    if signal == "DO_ENGINE_02" and value == "1":
                        # Start deposition — T0 feeder
                        deposition_active = True
                        self.materials_found.add("T0")
                        if pending_change == "T0":
                            for evt in reversed(self.material_changes):
                                if evt["changed_to"] == "T0" and not evt["followed_by_deposition"]:
                                    evt["followed_by_deposition"] = True
                                    evt["confirmed_in_layer"]     = layer_num
                                    break
                            pending_change = None

                    elif signal == "DO_ENGINE_03" and value == "1":
                        # Start deposition — T1 feeder
                        deposition_active = True
                        self.materials_found.add("T1")
                        if pending_change == "T1":
                            for evt in reversed(self.material_changes):
                                if evt["changed_to"] == "T1" and not evt["followed_by_deposition"]:
                                    evt["followed_by_deposition"] = True
                                    evt["confirmed_in_layer"]     = layer_num
                                    break
                            pending_change = None

                    elif signal == "DO_ENGINE_04" and value == "1":
                        # End deposition
                        deposition_active = False

                    elif signal == "DO_ENGINE_05" and value == "1":
                        active_material   = "T0"
                        pending_change    = "T0"
                        self.materials_found.add("T0")
                        self.material_changes.append({
                            "layer_name": layer_name,
                            "layer_num":  layer_num,
                            "changed_to": "T0",
                            "confirmed_in_layer": None,
                            "followed_by_deposition": False,
                            "warning": None,
                        })

                    elif signal == "DO_ENGINE_06" and value == "1":
                        active_material   = "T1"
                        pending_change    = "T1"
                        self.materials_found.add("T1")
                        self.material_changes.append({
                            "layer_name": layer_name,
                            "layer_num":  layer_num,
                            "changed_to": "T1",
                            "confirmed_in_layer": None,
                            "followed_by_deposition": False,
                            "warning": None,
                        })

                # ── Comments ─────────────────────────────────────────────
                for comment in comment_re.findall(line):
                    c = comment.strip()
                    if "MACRO" in c or "StartDeposition" in c:
                        self.print_sequence.append(c)

                # ── MoveL waypoint ────────────────────────────────────────
                mov_m = move_re.search(line)
                if mov_m:
                    x     = float(mov_m.group(1))
                    y     = float(mov_m.group(2))
                    z     = float(mov_m.group(3))
                    speed = float(mov_m.group(4))
                    self.all_speeds.append(speed)
                    self.waypoints.append({
                        "layer_num":      layer_num,
                        "layer_name":     layer_name,
                        "x": x, "y": y, "z": z,
                        "speed":          speed,
                        "material":       active_material,
                        "is_deposition":  deposition_active,
                        "is_seam_start":  False,   # filled below
                        "is_seam_end":    False,   # filled below
                    })

        # ── Mark seam start/end points per layer ─────────────────────────
        # Seam start = first deposition waypoint of each layer
        # Seam end   = last  deposition waypoint of each layer
        from collections import defaultdict
        layer_dep_indices: dict = defaultdict(list)
        for idx, wp in enumerate(self.waypoints):
            if wp["is_deposition"]:
                layer_dep_indices[wp["layer_num"]].append(idx)
        seam_start_indices = set()
        seam_end_indices   = set()
        for ln, idxs in layer_dep_indices.items():
            if idxs:
                self.waypoints[idxs[0]]["is_seam_start"] = True
                self.waypoints[idxs[-1]]["is_seam_end"]  = True
                seam_start_indices.add(idxs[0])
                seam_end_indices.add(idxs[-1])

        # ── After ALL layers: flag any change never confirmed ─────────────
        for evt in self.material_changes:
            if not evt["followed_by_deposition"]:
                expected_sig = "DO_ENGINE_02" if evt["changed_to"] == "T0" else "DO_ENGINE_03"
                evt["warning"] = (
                    f"⚠️  Layer {evt['layer_num']}: changed feeder to {evt['changed_to']} "
                    f"but {expected_sig} (start deposition) was never detected in any subsequent layer"
                )

        total_secs = sum(self.layer_times.values())
        print(f"   ✅ Found {len(self.materials_found)} materials: {', '.join(sorted(self.materials_found))}")
        print(f"   ✅ Extracted {len(self.waypoints)} waypoints")
        print(f"   ✅ Found {len(self.material_changes)} material change events")
        print(f"   ✅ Total print time from code: {total_secs/60:.1f} min ({len(self.layer_times)} layers with timing)")
        print(f"   ✅ Seam points marked: {len(seam_start_indices)} layer starts, {len(seam_end_indices)} layer ends")

    # ─── STEP 3: USER CLARIFICATIONS ────────────────────────────────────────

    def get_clarifications(self):
        print("\n" + "=" * 60)
        print("MELTIO DED CODE ANALYSIS — USER INPUT")
        print("=" * 60)

        self.user["part_name"] = input(f"\n📝 Part name [{self.part_name}]: ").strip() or self.part_name

        print(f"\n🔍 Found {len(self.materials_found)} material(s) in code: {', '.join(sorted(self.materials_found))}")

        for feeder in ["T0", "T1"]:
            if feeder in self.materials_found:
                raw = input(f"   Material {feeder} type: ").strip()
                self.user[f"material_{feeder}"] = raw or "Unknown"
                match = fuzzy_match_material(raw, self.materials_db)
                if match:
                    self.db_materials[feeder] = match
                    print(f"   ✅ Matched: {match['display_name']} (k={match['thermal_conductivity']} W/m·K)")
                else:
                    print(f"   ⚠️  '{raw}' not found in database — using generic values. You can add it to materials_database.json.")
                    self.db_materials[feeder] = {
                        "display_name": raw,
                        "thermal_conductivity": 15.0,
                        "density": 7000,
                        "specific_heat": 500,
                        "melting_point": 1400,
                        "notes": "Custom material — values estimated"
                    }
            else:
                self.user[f"material_{feeder}"] = None

        self.user["laser_power"]  = input("\n⚡ Laser power (W): ").strip() or "Not specified"
        self.user["feed_speed"]   = float(input("🔄 Wire feed speed (mm/sec): ").strip() or 0)
        self.user["layer_height"] = float(input("📏 Layer height (mm): ").strip() or 0)
        self.user["layer_width"]  = float(input("📐 Layer width (mm): ").strip() or 0)
        self.user["wire_diameter"]= float(input("🧵 Wire diameter (mm): ").strip() or 0)

        inert_raw = input("\n🛡️  Inert environment? (yes/no) [no]: ").strip().lower()
        self.user["inert_environment"] = inert_raw in ("yes", "y", "1", "true")

        print("\n✅ Input captured!")

    # ─── STEP 4: THERMAL CALCULATIONS ───────────────────────────────────────

    def calculate_thermal_data(self):
        """
        Physics-based temperature model for Meltio 450 nm blue-diode DED:

        ΔT per traversal (Rykalin-derived moving heat source):
            E_lin [J/m] = (absorption × laser_power) / speed_m_s
            ΔT [°C]     = E_lin / (π × e × ρ × Cp × q_eff)
            q_eff       = 8 × layer_width_m × layer_height_m   (empirical heat-sink factor)

        Cooling between waypoints (argon convection, 15 L/min → h ≈ 35 W/m²·K):
            τ_cool [s]  = (ρ × Cp × A_cross_m²) / (h × perimeter_m)
            T_cooled    = T_ambient + (T_hot − T_ambient) × exp(−dt / τ_cool)

        Temperature is accumulated across waypoints and reset (with cooling) at layer boundaries.
        Curvature is measured as angle between consecutive movement vectors [°].
        """
        wire_r  = self.user["wire_diameter"] / 2.0
        V1_base = math.pi * wire_r ** 2 * self.user["feed_speed"]  # mm³/s

        # Laser power — handle "1000 W" or "1000" strings
        try:
            laser_W = float(str(self.user.get("laser_power", "1000"))
                            .replace("W", "").replace("w", "").strip())
        except (ValueError, AttributeError):
            laser_W = 1000.0

        w_mm  = self.user["layer_width"]   # mm
        h_mm  = self.user["layer_height"]  # mm
        w_m   = w_mm  * 1e-3              # m
        h_m   = h_mm  * 1e-3              # m
        A_cross_m2  = w_m * h_m           # m²
        perimeter_m = 2.0 * (w_m + h_m)  # m
        q_eff       = 8.0 * A_cross_m2   # effective heat-sink area [m²]

        ambient    = float(self.user.get("ambient_temp",     25.0))
        min_dwell  = float(self.user.get("min_layer_dwell",   0.0))
        h_conv     = 35.0   # W/(m²·K) — 15 L/min argon from print head

        T_current  = ambient
        prev_layer = None
        prev_wp    = None
        t_elapsed  = 0.0  # cumulative print time [seconds]

        # Work only on deposition waypoints (pre-filtered for speed)
        dep_wps = [wp for wp in self.waypoints if wp["is_deposition"]]

        for i, wp in enumerate(dep_wps):
            feeder = wp["material"]
            mat    = self.db_materials.get(feeder) or {}

            k_SI       = mat.get("thermal_conductivity", 15.0)   # W/(m·K)
            rho        = mat.get("density",              7000)    # kg/m³
            Cp         = mat.get("specific_heat",         500)    # J/(kg·K)
            absorption = mat.get("absorption_450nm",      0.45)   # 450 nm blue laser

            # ── Volume deposition rates (retain original heat-index) ──────────
            V1    = V1_base
            V2    = w_mm * h_mm * wp["speed"]
            V_avg = (V1 + V2) / 2.0
            heat_index   = V_avg / k_SI if k_SI > 0 else 0.0
            thermal_mass = V1 * (rho / 1e9) * Cp

            # ── Segment distance & time ───────────────────────────────────────
            if prev_wp is not None:
                dx = wp["x"] - prev_wp["x"]
                dy = wp["y"] - prev_wp["y"]
                dz = wp["z"] - prev_wp["z"]
                dist_mm = math.sqrt(dx*dx + dy*dy + dz*dz)
                dt      = dist_mm / max(wp["speed"], 0.01)
            else:
                dist_mm = dt = 0.0

            # ── Temperature-dependent h_eff (convection + radiation) ──────────
            # At >500 °C radiation dominates and naturally caps accumulation.
            # h_rad linearised: ε×σ×(T_K + T_amb_K)×(T_K²+T_amb_K²)
            EMISSIVITY = 0.8      # oxidised metal surface
            SIGMA      = 5.67e-8  # W/(m²·K⁴)
            T_K     = T_current + 273.15
            T_amb_K = ambient    + 273.15
            h_rad   = EMISSIVITY * SIGMA * (T_K + T_amb_K) * (T_K**2 + T_amb_K**2)
            h_eff   = h_conv + h_rad
            denom   = h_eff * perimeter_m
            tau_bead = (rho * Cp * A_cross_m2) / denom if denom > 0 else 30.0

            # ── Inter-layer boundary: apply dwell cooling ─────────────────────
            if prev_layer is not None and wp["layer_num"] != prev_layer:
                dwell_t = max(min_dwell, 5.0)
                t_elapsed += dwell_t  # include dwell in cumulative print time
                # Cooling during dwell uses the same temperature-dependent tau
                T_current = ambient + (T_current - ambient) * math.exp(-dwell_t / tau_bead)
                # Recalculate tau for new T after dwell
                T_K2    = T_current + 273.15
                h_rad2  = EMISSIVITY * SIGMA * (T_K2 + T_amb_K) * (T_K2**2 + T_amb_K**2)
                h_eff2  = h_conv + h_rad2
                denom2  = h_eff2 * perimeter_m
                tau_bead = (rho * Cp * A_cross_m2) / denom2 if denom2 > 0 else 30.0

            # ── Heat input (Rykalin-derived) ──────────────────────────────────
            speed_ms   = max(wp["speed"], 0.01) * 1e-3     # mm/s → m/s
            E_lin_SI   = (absorption * laser_W) / speed_ms  # J/m

            if rho > 0 and Cp > 0 and q_eff > 0:
                delta_T = E_lin_SI / (math.pi * math.e * rho * Cp * q_eff)
            else:
                delta_T = 0.0

            # ── Apply heat then cool during traversal ─────────────────────────
            T_hot = T_current + delta_T
            if dt > 0:
                T_cooled = ambient + (T_hot - ambient) * math.exp(-dt / tau_bead)
            else:
                T_cooled = T_hot

            melting_pt = mat.get("melting_point", 3500)
            T_current  = max(ambient, min(T_cooled, melting_pt * 0.98))

            # ── Curvature detection [°] ───────────────────────────────────────
            curvature = 0.0
            if i > 0 and i < len(dep_wps) - 1:
                pw_c = dep_wps[i - 1]
                nw_c = dep_wps[i + 1]
                if pw_c["layer_num"] == wp["layer_num"] == nw_c["layer_num"]:
                    v1 = (wp["x"]-pw_c["x"], wp["y"]-pw_c["y"], wp["z"]-pw_c["z"])
                    v2 = (nw_c["x"]-wp["x"], nw_c["y"]-wp["y"], nw_c["z"]-wp["z"])
                    mag1 = math.sqrt(sum(c*c for c in v1))
                    mag2 = math.sqrt(sum(c*c for c in v2))
                    if mag1 > 0.1 and mag2 > 0.1:
                        cos_a = sum(v1[j]*v2[j] for j in range(3)) / (mag1 * mag2)
                        curvature = math.degrees(math.acos(max(-1.0, min(1.0, cos_a))))

            # ── ANOMALY METRICS (Ansys/Simufact-equivalent) ───────────────────
            beam_d_mm  = float(self.user.get("beam_spot_diameter", 1.2))
            beam_d_m   = beam_d_mm * 1e-3                              # m

            # (a) Volumetric Energy Density [J/mm³]
            VED = (laser_W * absorption) / max(wp["speed"] * w_mm * h_mm, 1e-9)

            # (b) Normalized Enthalpy ΔH/h_s  (King et al., Nature Comms 2021)
            #     Thresholds: <6 = LOF risk | 6–25 = conduction | >25 = keyhole onset
            T_liq  = mat.get("T_liquidus", melting_pt)
            h_s    = rho * Cp * T_liq                                  # J/m³
            alpha  = k_SI / (rho * Cp) if (rho * Cp) > 0 else 1e-6   # m²/s
            v_ms   = max(wp["speed"], 0.01) * 1e-3                    # m/s
            denom_nh = h_s * math.sqrt(math.pi * alpha * v_ms) * (beam_d_m ** 1.5)
            norm_H = (absorption * laser_W) / denom_nh if denom_nh > 0 else 0.0

            # (c) Melt pool geometry — Eagar-Tsai (1983) Gaussian surface source
            #
            # Rosenthal (point source) gives 20-50 % error for DED beam spots >1 mm
            # (Honarmandi 2021, DOI 10.1016/j.addma.2021.102300; Parida 2025).
            # Eagar-Tsai replaces the singularity with a 2-D Gaussian heat flux of
            # radius σ (= beam_d/2), giving ~10-15 % error for width in conduction mode.
            #
            # Closed-form approximation (Hann 2011 / Hunt 1984 low-Pe limit):
            #   Pe = v·σ / (2·α)               (thermal Péclet number)
            #   For Pe < 5  (DED typical): width dominated by diffusion + beam spread
            #     W ≈ 2·σ·√(1 + A·P / (π·k·σ·ΔT_melt))       [Gaussian half-width, m]
            #   Depth (conduction-mode assumption, semi-circular pool):
            #     D ≈ W / 2    (safe lower bound; convection elongates pool)
            #     Capped at Rosenthal depth as upper bound for physical consistency.
            #
            # NOTE: For the Meltio coaxial multi-beam geometry an annular heat source
            # (Zapata 2023, DOI 10.1063/6.0002614) would be more accurate, but requires
            # FEM and cannot be computed analytically here.
            delta_T_melt  = max(T_liq - T_current, 1.0)
            sigma_m        = beam_d_m / 2.0                    # Gaussian 1-σ radius [m]

            # Eagar-Tsai width (conduction mode, low-Pe closed-form)
            Pe_et = v_ms * sigma_m / (2.0 * max(alpha, 1e-9))
            # Dimensionless heat input normalised by beam radius conduction path
            Q_star = (absorption * laser_W) / (math.pi * k_SI * sigma_m * delta_T_melt)
            # Half-width of melt pool [m] — from Gaussian source energy balance
            et_half_width_m = sigma_m * math.sqrt(max(1.0 + Q_star, 1.0))
            # Péclet correction: at higher Pe the pool elongates and narrows slightly
            pe_corr = 1.0 / (1.0 + 0.3 * Pe_et)              # empirical, valid Pe<5
            melt_width_mm  = round(et_half_width_m * 2.0 * pe_corr * 1e3, 3)  # full width [mm]

            # Depth: conduction-mode assumption (semi-circular) capped by Rosenthal
            rosenthal_depth_m = (absorption * laser_W) / (math.pi * k_SI * delta_T_melt)
            et_depth_m        = min(et_half_width_m * pe_corr,  # semi-circular lower bound
                                    rosenthal_depth_m)           # Rosenthal upper bound
            melt_depth_mm  = round(min(et_depth_m * 1e3, h_mm * 3.0), 3)
            melt_fuse_ratio = melt_depth_mm / h_mm if h_mm > 0 else 0.0

            # (d) Thermal gradient G [K/m] and solidification rate R [m/s]
            G = abs(delta_T) / max(dist_mm, 0.1) * 1000.0  # K/m (approx along path)
            R = wp["speed"] * 1e-3                          # m/s ≈ solidification front speed
            cooling_rate_Ks = G * R                         # K/s
            G_over_R = G / max(R, 1e-9)                    # K·s/m² — grain morphology index

            # (e) Anomaly flags
            pw_mat             = mat.get("process_window", {})
            solidif_range      = T_liq - mat.get("T_solidus", T_liq - 50)
            lof_risk           = VED < pw_mat.get("VED_lof_min", 25)
            # norm_H keyhole threshold >25 validated for LPBF (spot 50–300 µm) only.
            # Wire-laser DED uses 1–3 mm spots → norm_H is structurally lower;
            # keyhole-like vapour dynamics still occur but at different thresholds.
            # Flag is retained as indicative; label caveat shown in report.
            keyhole_risk       = norm_H > 25.0
            # Wire-DED stubbing risk: VED well below LOF minimum → wire won't melt cleanly.
            # Threshold 60 % of LOF minimum is empirical (McLain 2024, DOI 10.3390/ma17215311).
            stubbing_risk      = VED < pw_mat.get("VED_lof_min", 25) * 0.6
            lof_depth_risk     = melt_fuse_ratio < 1.1
            # Eagar-Tsai width vs nominal bead width: ratio < 0.7 → bead too narrow (poor overlap)
            # ratio > 2.0 → excessive melt pool spread (distortion risk)
            melt_width_ratio   = round(melt_width_mm / max(w_mm, 0.1), 3)
            cracking_score     = round(min(1.0,
                                    (cooling_rate_Ks / 1e5) * (solidif_range / 100.0)), 3)

            # Ti-6Al-4V phase prediction from cooling rate (3-level, literature-validated).
            # Thresholds: Ahmed & Rack 1998 (onset 410 K/s); Kenel 2017 synchrotron
            # (full martensite ~4 500 K/s); Xiao 2025 Gleeble (~7 000 K/s).
            # 410 K/s = martensite ONSET, not full martensite (common misconception).
            mat_name = mat.get("display_name", "")
            mat_name_lc = mat_name.lower()
            if "Ti" in mat_name or "ti" in mat_name_lc:
                if cooling_rate_Ks > 4500:
                    ti_phase = "α′ martensite (full)"
                elif cooling_rate_Ks > 410:
                    ti_phase = "α′ martensite (onset)"
                elif cooling_rate_Ks > 20:
                    ti_phase = "α+β Widmanstätten"
                else:
                    ti_phase = "α+β lamellar"
                # Alpha lath width proxy for Ti-6Al-4V (Eliseeva 2024, DOI 10.3390/ma17133307).
                # lath_width [µm] ≈ 0.23 + 1.27 × exp(−cooling_rate / 3000)
                # Validated range: 0.23 µm (fast, 300s dwell) – 0.49 µm (no dwell).
                # Extended empirically: coarser at very low cooling rates (slow layers).
                if cooling_rate_Ks > 0:
                    lath_width_um = round(0.23 + 1.27 * math.exp(-cooling_rate_Ks / 3000.0), 3)
                else:
                    lath_width_um = None
            else:
                ti_phase      = None
                lath_width_um = None

            # PDAS for 316L stainless steel (Sing et al. 2022, PMC9625081).
            # λ₁ [nm] = 80 × Ṫ^{-0.333},  Ṫ = G × R [K/s]
            # Valid for austenitic stainless steels in DED (Ṫ range 10³–10⁵ K/s).
            # Ti-6Al-4V does NOT form classical dendrites → PDAS not applicable.
            if ("316" in mat_name or "stainless" in mat_name_lc or "steel" in mat_name_lc) \
                    and cooling_rate_Ks > 0:
                pdas_nm = round(80.0 * (cooling_rate_Ks ** -0.333), 1)
            else:
                pdas_nm = None

            # (f) Seam metrics — extra energy & risk at layer start/end points
            is_seam_start = wp.get("is_seam_start", False)
            is_seam_end   = wp.get("is_seam_end",   False)
            is_seam       = is_seam_start or is_seam_end

            # Overlap energy: at seam start the bead overlaps with the end of the
            # previous layer (~1 bead-width worth of double-heating).
            # E_overlap [J/mm] = E_linear × (overlap_length / bead_width)
            # overlap_length estimated as 1 bead width (conservative)
            seam_overlap_energy = round((absorption * laser_W) / max(wp["speed"], 0.01), 2) if is_seam_start else 0.0

            # Gap distance to previous layer's seam end (inter-layer seam distance).
            # Search backward by layer_num, not a fixed point window, so large layers work correctly.
            seam_gap_mm = 0.0
            if is_seam_start and i > 0:
                target_layer = wp["layer_num"] - 1
                for back in range(i - 1, -1, -1):
                    bwp = dep_wps[back]
                    if bwp["layer_num"] < target_layer:
                        break  # overshot — give up
                    if bwp.get("is_seam_end") and bwp["layer_num"] == target_layer:
                        seam_gap_mm = round(math.sqrt(
                            (wp["x"] - bwp["x"])**2 +
                            (wp["y"] - bwp["y"])**2 +
                            (wp["z"] - bwp["z"])**2), 2)
                        break

            # Seam risk score: higher when seam is always at same XY (low drift)
            # Computed globally after all waypoints; per-point flag here.
            seam_risk = "high" if is_seam else "none"

            self.thermal_data.append({
                **wp,
                # ── original metrics ──────────────────────────────────────
                "V1_wire":             round(V1,           4),
                "V2_geometry":         round(V2,           4),
                "heat_index":          round(heat_index,   4),
                "thermal_mass":        round(thermal_mass, 6),
                "thermal_conductivity": k_SI,
                "temp_C":              round(T_current,    1),
                "delta_T":             round(delta_T,      1),
                "curvature_deg":       round(curvature,    1),
                "E_linear_Jmm":        round((absorption * laser_W) / max(wp["speed"], 0.01), 2),
                "absorption":          absorption,
                "tau_cool_s":          round(tau_bead,     1),
                # ── anomaly metrics ───────────────────────────────────────
                "VED":                 round(VED,          2),
                "norm_H":              round(norm_H,       3),
                "melt_depth_mm":       round(melt_depth_mm, 3),
                "melt_width_mm":       round(melt_width_mm, 3),
                "melt_width_ratio":    melt_width_ratio,
                "melt_fuse_ratio":     round(melt_fuse_ratio, 3),
                "G_Km":                round(G,            1),
                "R_ms":                round(R,            5),
                "cooling_rate_Ks":     round(cooling_rate_Ks, 1),
                "G_over_R":            round(G_over_R,     1),
                "lof_risk":            lof_risk,
                "keyhole_risk":        keyhole_risk,
                "stubbing_risk":       stubbing_risk,
                "overheat_risk":       False,   # updated after FDM residual-temp pass
                "lof_depth_risk":      lof_depth_risk,
                "cracking_score":      cracking_score,
                "ti_phase":            ti_phase,
                "lath_width_um":       lath_width_um,
                "pdas_nm":             pdas_nm,
                "t_elapsed":           round(t_elapsed, 3),
                # ── seam metrics ──────────────────────────────────────────
                "is_seam_start":       is_seam_start,
                "is_seam_end":         is_seam_end,
                "seam_overlap_energy": seam_overlap_energy,
                "seam_gap_mm":         seam_gap_mm,
                "seam_risk":           seam_risk,
            })

            t_elapsed += dt   # accumulate print time after storing current stamp
            prev_layer = wp["layer_num"]
            prev_wp    = wp

        # ── 3D FDM THERMAL SOLVER ─────────────────────────────────────────────
        # Full physics: Gaussian heat source + wire energy + 3D conduction +
        # convection + radiation + latent heat + substrate heat sink.
        # Solves  ρ·cp·∂T/∂t = k·∇²T + q_laser − q_surface
        # on a Cartesian grid covering the part + substrate.

        import numpy as np

        print("   🔥 Starting 3D FDM thermal solver...")

        # ── Material properties ──────────────────────────────────────────
        dom_f   = "T0" if self.db_materials.get("T0") else next(iter(self.db_materials), "T0")
        dom     = self.db_materials.get(dom_f) or {}
        rho_m   = dom.get("density",              7000)
        cp_m    = dom.get("specific_heat",          500)
        k_m     = dom.get("thermal_conductivity",    15.0)
        Lf      = dom.get("latent_heat_fusion",  260000)
        T_sol   = dom.get("T_solidus",             1390)
        T_liq   = dom.get("T_liquidus",            1440)
        eta_m   = dom.get("absorption_450nm",       0.45)
        mp_m    = dom.get("melting_point",          1500)
        eps     = 0.8
        SB      = 5.67e-8
        h_c     = h_conv  # 35 W/m²K argon

        alpha_m = k_m / (rho_m * cp_m) if (rho_m * cp_m) > 0 else 5e-6

        # ── Derived: wire energy ─────────────────────────────────────────
        d_wire_m  = self.user["wire_diameter"] * 1e-3
        v_wire_ms = self.user["feed_speed"] * 1e-3
        A_wire    = math.pi * (d_wire_m / 2) ** 2
        m_dot     = rho_m * A_wire * v_wire_ms               # kg/s
        Q_wire    = m_dot * (cp_m * (T_liq - ambient) + Lf)  # W to melt wire
        P_abs     = eta_m * laser_W                           # W absorbed
        P_net     = max(0.0, P_abs - Q_wire)                  # W into melt pool
        beam_r_mm = float(self.user.get("beam_spot_diameter", 1.2)) / 2.0

        print(f"      P_absorbed={P_abs:.0f} W, Q_wire={Q_wire:.0f} W, P_net={P_net:.0f} W")

        # ── Grid setup from waypoint bounding box ────────────────────────
        wxs = [wp["x"] for wp in dep_wps]
        wys = [wp["y"] for wp in dep_wps]
        wzs = [wp["z"] for wp in dep_wps]

        pad   = 10.0  # mm padding
        sub_h = 5.0   # mm substrate below part

        x0, x1 = min(wxs) - pad, max(wxs) + pad
        y0, y1 = min(wys) - pad, max(wys) + pad
        z0     = min(wzs) - sub_h
        z1     = max(wzs) + 3.0

        # Adaptive cell size — target 15K–30K cells for fast solve
        vol = (x1 - x0) * (y1 - y0) * (z1 - z0)
        cell = max(1.0, (vol / 20000.0) ** (1.0 / 3.0))
        cell = max(cell, h_mm)  # at least one cell per layer height
        cell = round(cell * 2) / 2  # snap to 0.5mm
        cell = max(cell, 0.5)

        dx = dy = dz = cell
        dx_m = dx * 1e-3

        Nx = max(4, int(math.ceil((x1 - x0) / dx)) + 1)
        Ny = max(4, int(math.ceil((y1 - y0) / dy)) + 1)
        Nz = max(4, int(math.ceil((z1 - z0) / dz)) + 1)
        total_cells = Nx * Ny * Nz

        print(f"      Grid: {Nx}×{Ny}×{Nz} = {total_cells} cells, cell={cell:.1f} mm")

        # CFL for 3D explicit scheme: dt < dx²/(6·α)
        dt_cfl = 0.8 * dx_m ** 2 / (6.0 * alpha_m)
        dt_sim = min(dt_cfl, 0.5)

        # ── Allocate arrays ──────────────────────────────────────────────
        T_grid  = np.full((Nx, Ny, Nz), ambient, dtype=np.float64)
        active  = np.zeros((Nx, Ny, Nz), dtype=bool)

        # Mark substrate cells as active + at ambient
        z_sub_top = max(1, int(math.ceil(sub_h / dz)))
        active[:, :, :z_sub_top] = True

        # Pre-compute coordinate arrays (mm, world coords)
        xc = np.arange(Nx) * dx + x0  # cell centre x
        yc = np.arange(Ny) * dy + y0
        zc = np.arange(Nz) * dz + z0

        # 2D meshgrids for Gaussian heat source (x-y plane)
        XC, YC = np.meshgrid(xc, yc, indexing='ij')  # (Nx, Ny)

        cell_vol_m3 = dx_m ** 3
        beam_r_m    = beam_r_mm * 1e-3
        beam_r_m2   = beam_r_m ** 2

        # ── Effective cp with latent heat in mushy zone ──────────────────
        def get_cp_eff(T_arr):
            cp_e = np.full_like(T_arr, cp_m)
            if T_liq > T_sol:
                mushy = (T_arr >= T_sol) & (T_arr <= T_liq)
                cp_e[mushy] = cp_m + Lf / (T_liq - T_sol)
            return cp_e

        # ── Surface mask (cells with ≥1 inactive neighbour) ─────────────
        def surface_mask(act):
            s = np.zeros_like(act)
            s[1:, :, :]  |= act[1:, :, :] & ~act[:-1, :, :]
            s[:-1, :, :] |= act[:-1, :, :] & ~act[1:, :, :]
            s[:, 1:, :]  |= act[:, 1:, :] & ~act[:, :-1, :]
            s[:, :-1, :] |= act[:, :-1, :] & ~act[:, 1:, :]
            s[:, :, 1:]  |= act[:, :, 1:] & ~act[:, :, :-1]
            s[:, :, :-1] |= act[:, :, :-1] & ~act[:, :, 1:]
            s[:, :, -1]  |= act[:, :, -1]
            return s & act

        # ── FDM time step (vectorised) ───────────────────────────────────
        def fdm_step(T_g, act, surf, dt_s):
            """One explicit Euler step: conduction + surface cooling."""
            cp_eff = get_cp_eff(T_g)

            # 3D Laplacian via shifted slicing (no roll — faster)
            Lap = np.zeros_like(T_g)
            Lap[1:-1, :, :] += T_g[:-2, :, :] + T_g[2:, :, :] - 2.0 * T_g[1:-1, :, :]
            Lap[:, 1:-1, :] += T_g[:, :-2, :] + T_g[:, 2:, :] - 2.0 * T_g[:, 1:-1, :]
            Lap[:, :, 1:-1] += T_g[:, :, :-2] + T_g[:, :, 2:] - 2.0 * T_g[:, :, 1:-1]
            Lap /= (dx_m ** 2)

            # Conduction [K/s]
            cond = (k_m / (rho_m * cp_eff)) * Lap

            # Surface cooling [K/s]  (convection + radiation, only on surface cells)
            T_K      = T_g + 273.15
            T_amb_K  = ambient + 273.15
            q_conv   = h_c * (T_g - ambient)
            q_rad    = eps * SB * (T_K ** 4 - T_amb_K ** 4)
            cooling  = surf * (q_conv + q_rad) / (rho_m * cp_eff * dx_m)

            # Update (only active cells)
            dTdt = (cond - cooling) * act
            T_new = T_g + dt_s * dTdt

            # Boundary: bottom row of substrate held at ambient (large thermal mass)
            T_new[:, :, 0] = ambient

            # Cap at melting point
            T_new = np.clip(T_new, ambient, mp_m * 0.99)

            return T_new

        # ── Convert world (x,y,z) to grid index ─────────────────────────
        def w2i(wx, wy, wz):
            ix = max(0, min(Nx - 1, int(round((wx - x0) / dx))))
            iy = max(0, min(Ny - 1, int(round((wy - y0) / dy))))
            iz = max(0, min(Nz - 1, int(round((wz - z0) / dz))))
            return ix, iy, iz

        # ── Group waypoints by layer ─────────────────────────────────────
        layer_nums = sorted(set(wp["layer_num"] for wp in dep_wps))
        layer_wps  = {ln: [wp for wp in dep_wps if wp["layer_num"] == ln]
                      for ln in layer_nums}

        # Bead activation radius (cells)
        bead_cx = max(1, int(round(w_mm / (2.0 * dx))))
        bead_cy = max(1, int(round(w_mm / (2.0 * dy))))

        total_sim_steps = 0
        MAX_TOTAL_FDM_STEPS = 5000  # hard cap — prevents multi-minute freezes

        # ── MAIN LOOP: layer by layer ────────────────────────────────────
        surf = surface_mask(active)          # cache; recompute only when active changes
        prev_iz = -1

        for l_idx, ln in enumerate(layer_nums):
            if total_sim_steps >= MAX_TOTAL_FDM_STEPS:
                print(f"      ⚡ FDM cap reached ({MAX_TOTAL_FDM_STEPS} steps) — remaining layers skipped")
                break
            wps = layer_wps[ln]
            prev_wp_sim = None

            for wi, wp in enumerate(wps):
                ix, iy, iz = w2i(wp["x"], wp["y"], wp["z"])

                # ── Activate bead cells ──────────────────────────────────
                ix_lo = max(0, ix - bead_cx)
                ix_hi = min(Nx, ix + bead_cx + 1)
                iy_lo = max(0, iy - bead_cy)
                iy_hi = min(Ny, iy + bead_cy + 1)
                newly_activated = not active[ix_lo:ix_hi, iy_lo:iy_hi, iz].all()
                active[ix_lo:ix_hi, iy_lo:iy_hi, iz] = True
                if newly_activated or iz != prev_iz:
                    surf = surface_mask(active)
                    prev_iz = iz

                # ── Transit time ─────────────────────────────────────────
                if prev_wp_sim is not None:
                    dd = math.sqrt((wp["x"] - prev_wp_sim["x"]) ** 2 +
                                   (wp["y"] - prev_wp_sim["y"]) ** 2 +
                                   (wp["z"] - prev_wp_sim["z"]) ** 2)
                    t_transit = dd / max(wp["speed"], 0.01)
                else:
                    t_transit = 0.05

                # ── Apply Gaussian heat source to z-layer ────────────────
                r2 = ((XC - wp["x"]) ** 2 + (YC - wp["y"]) ** 2) * 1e-6  # m²
                q_gauss = (2.0 * P_net / (math.pi * beam_r_m2)) * np.exp(-2.0 * r2 / beam_r_m2)
                dT_heat = q_gauss * t_transit * (dx_m * dx_m) / (rho_m * cp_m * cell_vol_m3)
                act_slice = active[:, :, iz]
                T_grid[:, :, iz] += dT_heat * act_slice

                # ── Run FDM diffusion for transit time ───────────────────
                n_sub = max(1, int(math.ceil(t_transit / dt_sim)))
                dt_actual = t_transit / n_sub
                for _ in range(n_sub):
                    T_grid = fdm_step(T_grid, active, surf, dt_actual)
                total_sim_steps += n_sub

                prev_wp_sim = wp

            # ── Inter-layer dwell cooling (analytical — avoids 100+ FDM steps) ──
            dwell = max(min_dwell, 5.0)
            # Bead-scale tau: ρ·Cp·(h/2) / (2·h_c)  [seconds]
            tau_dwell = max((rho_m * cp_m * (h_mm * 0.5e-3)) / (2.0 * h_c), 2.0)
            decay_dwell = math.exp(-dwell / tau_dwell)
            T_grid = np.where(active, ambient + (T_grid - ambient) * decay_dwell, T_grid)
            total_sim_steps += 1  # analytical step counts as 1

            # Progress
            if (l_idx + 1) % 5 == 0 or l_idx == len(layer_nums) - 1:
                t_vals = T_grid[active]
                print(f"      Layer {ln}/{layer_nums[-1]}: "
                      f"T range {t_vals.min():.0f}–{t_vals.max():.0f} °C, "
                      f"{total_sim_steps} FDM steps so far")

        print(f"      ✅ FDM complete: {total_sim_steps} total steps")

        # ── Map grid temperatures to each waypoint ───────────────────────
        for d in self.thermal_data:
            ix, iy, iz = w2i(d["x"], d["y"], d["z"])
            d["temp_C_final"] = round(float(T_grid[ix, iy, iz]), 1)

        # ── State tracking per waypoint ──────────────────────────────────
        for d in self.thermal_data:
            tf = d["temp_C_final"]
            if tf >= T_liq:
                d["state"] = "liquid"
            elif tf >= T_sol:
                d["state"] = "mushy"
            else:
                d["state"] = "solid"

        # ── Residual temperature: how hot each point is at END of print ──
        # The melt pool is only at the robot position; once it moves away,
        # each deposited point cools exponentially.
        # Key: use PART-SCALE τ (whole part thermal mass), not bead-scale τ.
        # τ_part = (ρ × Cp × V_part) / (h_eff × A_surface)
        if self.thermal_data:
            t_total   = self.thermal_data[-1]["t_elapsed"]
            T_melt_r  = mp_m  # melting point from dominant material

            # Part-scale cooling time constant from bounding box geometry
            wxs = [d["x"] for d in self.thermal_data]
            wys = [d["y"] for d in self.thermal_data]
            wzs = [d["z"] for d in self.thermal_data]
            dx_mm = max(wxs) - min(wxs) + w_mm
            dy_mm = max(wys) - min(wys) + w_mm
            dz_mm = max(wzs) - min(wzs) + h_mm
            V_part_m3 = (dx_mm * dy_mm * dz_mm * 1e-9) * 0.4   # ~40% fill factor
            A_surf_m2 = 2.0 * (dx_mm*dy_mm + dx_mm*dz_mm + dy_mm*dz_mm) * 1e-6

            # Representative h_eff at ~400°C (midway through cooling)
            T_rep_K  = 400 + 273.15
            h_rad_rep = eps * SB * (T_rep_K + 298.15) * (T_rep_K**2 + 298.15**2)
            h_eff_rep = h_c + h_rad_rep

            tau_part = (rho_m * cp_m * V_part_m3) / (h_eff_rep * A_surf_m2) if A_surf_m2 > 0 else 300.0
            tau_part = max(tau_part, 60.0)  # floor: no instant cooling
            print(f"   🧊 Part-scale τ = {tau_part:.1f}s (V={V_part_m3*1e6:.0f} mm³, A={A_surf_m2*1e6:.0f} mm²)")

            for d in self.thermal_data:
                t_since = max(0.0, t_total - d["t_elapsed"])
                d["temp_C_residual"] = round(
                    ambient + (T_melt_r - ambient) * math.exp(-t_since / tau_part), 1
                )
                d["overheat_risk"] = d["temp_C_residual"] > T_melt_r * 0.85

        print(f"   ✅ Calculated thermal data for {len(self.thermal_data)} deposition waypoints")
        if self.thermal_data:
            temps   = [d["temp_C"]        for d in self.thermal_data]
            temps_f = [d["temp_C_final"]  for d in self.thermal_data]
            hi_cv   = [d for d in self.thermal_data if d["curvature_deg"] > 30]
            lof_ct  = sum(1 for d in self.thermal_data if d["lof_risk"])
            kh_ct   = sum(1 for d in self.thermal_data if d["keyhole_risk"])
            oh_ct   = sum(1 for d in self.thermal_data if d["overheat_risk"])
            print(f"   🌡️  Deposition temp range:    {min(temps):.0f} – {max(temps):.0f} °C")
            print(f"   🌡️  FDM final temp range:     {min(temps_f):.0f} – {max(temps_f):.0f} °C")
            print(f"   📐  High-curvature zones: {len(hi_cv)}")
            print(f"   ⚠️  LOF risk zones: {lof_ct} | Keyhole risk: {kh_ct} | Overheat: {oh_ct}")

            # ── Seam summary ─────────────────────────────────────────────
            seam_starts = [d for d in self.thermal_data if d.get("is_seam_start")]
            seam_ends   = [d for d in self.thermal_data if d.get("is_seam_end")]
            if seam_starts:
                # XY drift across all seam start points
                xs = [d["x"] for d in seam_starts]
                ys = [d["y"] for d in seam_starts]
                xy_drift = math.sqrt((max(xs)-min(xs))**2 + (max(ys)-min(ys))**2)
                # Gap statistics (inter-layer seam distance)
                gaps = [d["seam_gap_mm"] for d in seam_starts if d["seam_gap_mm"] > 0]
                avg_gap = sum(gaps)/len(gaps) if gaps else 0.0
                max_gap = max(gaps) if gaps else 0.0
                # Seam temperature statistics
                seam_temps = [d["temp_C"] for d in seam_starts]
                avg_seam_t = sum(seam_temps)/len(seam_temps)
                # Seam type classification
                if xy_drift < 5:
                    seam_type = "FIXED (high risk)"
                elif xy_drift < 30:
                    seam_type = "NEAR-FIXED (medium risk)"
                else:
                    seam_type = "RANDOM (low risk)"
                print(f"   🧵 Seam points: {len(seam_starts)} starts | {len(seam_ends)} ends")
                print(f"   🧵 Seam XY drift: {xy_drift:.1f} mm → {seam_type}")
                print(f"   🧵 Seam gap (inter-layer): avg={avg_gap:.2f} mm, max={max_gap:.2f} mm")
                print(f"   🧵 Seam avg temp: {avg_seam_t:.0f} °C")

            # ── Interpass temperature & dwell analysis ────────────────────
            # For each layer-start waypoint, estimate dwell time needed to
            # cool to the material interpass target before depositing next layer.
            # T_target: Ti-6Al-4V = 400°C (WAAM convention, not wire-laser validated);
            #           316L = 300°C; others = 350°C.
            # Formula: t_dwell = τ_bead × ln((T_current − T_amb) / (T_target − T_amb))
            # Capped at 600 s to avoid infinite waits on cold parts.
            # (Eliseeva 2024, DOI 10.3390/ma17133307)
            layer_starts = {}
            for d in self.thermal_data:
                ln = d["layer_num"]
                if ln not in layer_starts:
                    layer_starts[ln] = d   # first waypoint of each layer

            dwell_warnings = []
            for ln, d in sorted(layer_starts.items()):
                mat_n = d.get("material", "")
                T_amb = float(self.user.get("ambient_temp", 25))
                tau   = d.get("tau_cool_s", 30.0)
                T_cur = d.get("temp_C_final", d.get("temp_C", T_amb))
                if "Ti" in mat_n or "ti" in mat_n.lower():
                    T_target = 400.0
                    note = "Ti-6Al-4V target (WAAM convention; not wire-laser validated)"
                elif "316" in mat_n or "stainless" in mat_n.lower():
                    T_target = 300.0
                    note = "316L target"
                else:
                    T_target = 350.0
                    note = "default target"
                if T_cur > T_target and tau > 0:
                    ratio = max((T_cur - T_amb) / max(T_target - T_amb, 1.0), 1.001)
                    t_dwell_needed = min(tau * math.log(ratio), 600.0)
                    current_dwell  = float(self.user.get("min_layer_dwell", 5.0))
                    if t_dwell_needed > current_dwell * 1.5:
                        dwell_warnings.append((ln, T_cur, T_target, t_dwell_needed, note))

            if dwell_warnings:
                print(f"   ⏱️  Interpass dwell warnings ({len(dwell_warnings)} layers exceed target):")
                for ln, T_cur, T_tgt, t_needed, note in dwell_warnings[:5]:
                    print(f"      Layer {ln:4d}: T={T_cur:.0f}°C → target {T_tgt:.0f}°C "
                          f"({note}) needs ~{t_needed:.0f}s dwell")
                if len(dwell_warnings) > 5:
                    print(f"      … and {len(dwell_warnings)-5} more layers")
            else:
                print(f"   ✅ Interpass temperatures within target on all layers")

            # Store dwell summary on instance for HTML report
            self._dwell_warnings = dwell_warnings

    # ─── STEP 5a: HTML COLOR-CODED HEATMAP ─────────────────────────────────

    def generate_html_heatmap(self, timestamp: str) -> str:
        if not self.thermal_data:
            return ""

        temps   = [d["temp_C"] for d in self.thermal_data]
        t_min, t_max = min(temps), max(temps)

        # O(n) pre-index by layer
        by_layer: dict = defaultdict(list)
        for d in self.thermal_data:
            by_layer[d["layer_num"]].append(d)
        layers = sorted(by_layer.keys())

        # Points per layer for the gradient-line canvas.
        # Goal: ≥ 60 pts/layer so the gradient path is smooth and readable.
        # Budget cap: 4 MB of JS ≈ 44_000 serialised points (≈ 90 chars each).
        # For models with many short layers (e.g. coil/helix, 1927 layers × 66 pts),
        # every layer already has ≤ 66 pts → step=1 → include all points; if the
        # total still exceeds budget we sub-sample globally (step over layers array).
        n_layers       = max(len(layers), 1)
        MAX_TOTAL_PTS  = 44_000   # hard ceiling on total serialised points (~4 MB)
        pts_per_layer  = max(60, MAX_TOTAL_PTS // n_layers)
        pts_per_layer  = min(pts_per_layer, 300)

        # If total points after per-layer sampling still exceeds budget,
        # sub-sample layers themselves (keep every Nth layer).
        # This preserves full detail on visible layers while keeping file small.
        # Minimum 200 layers always included to maintain animation smoothness.
        avg_pts   = sum(len(by_layer[ln]) for ln in layers) / n_layers
        total_est = n_layers * min(avg_pts, pts_per_layer)
        layer_step = max(1, int(total_est / MAX_TOTAL_PTS))
        sampled_layers = layers[::layer_step] if layer_step > 1 else layers

        # Build per-layer JS arrays
        layer_data_js = []
        for ln in sampled_layers:
            pts = by_layer[ln]
            step = max(1, len(pts) // pts_per_layer)
            arr = []
            for p in pts[::step]:
                color = heat_color(p["temp_C"], t_min, t_max)
                arr.append(
                    f'{{x:{p["x"]:.2f},y:{p["y"]:.2f},z:{p["z"]:.2f},'
                    f'speed:{p["speed"]},material:"{p["material"]}",'
                    f'tc:{p["temp_C"]},cv:{p["curvature_deg"]},'
                    f'hi:{p["heat_index"]:.4f},'
                    f'ved:{p.get("VED",0):.1f},'
                    f'lof:{"true" if p.get("lof_risk") else "false"},'
                    f'kh:{"true" if p.get("keyhole_risk") else "false"},'
                    f'stub:{"true" if p.get("stubbing_risk") else "false"},'
                    f'mw:{p.get("melt_width_mm",0):.2f},'
                    f'color:"{color}"}}'
                )
            layer_data_js.append(f'[{",".join(arr)}]')

        layers_js   = f'[{",".join(layer_data_js)}]'
        layers_list = str(sampled_layers)   # matches layer_data_js order

        # Build seam markers data (start = seam point, stop = layer end)
        markers_data = []
        for ln in layers:
            pts = by_layer[ln]
            if pts:
                sp = pts[0]
                ep = pts[-1]
                markers_data.append({
                    "layer":    ln,
                    "start_x":  sp["x"],
                    "start_y":  sp["y"],
                    "start_z":  sp["z"],
                    "stop_x":   ep["x"],
                    "stop_y":   ep["y"],
                    "stop_z":   ep["z"],
                    "start_tc": sp["temp_C"],
                    "stop_tc":  ep["temp_C"],
                    "gap_mm":   sp.get("seam_gap_mm", 0.0),
                    "overlap_e":sp.get("seam_overlap_energy", 0.0),
                })
        markers_js = json.dumps(markers_data)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Heat Map — {self.user['part_name']}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  html, body {{ margin:0; padding:0; height:100%; overflow:hidden;
                font-family:'Segoe UI',system-ui,sans-serif;
                background:#ffffff; color:#1e293b; }}
  #shell {{ display:flex; flex-direction:column; height:100vh; padding:10px 14px 6px; }}
  h1 {{ font-size:1.05rem; font-weight:700; color:#1e40af; margin:0 0 2px; }}
  .subtitle {{ color:#64748b; font-size:.72rem; margin-bottom:8px; }}
  .controls {{ display:flex; gap:12px; align-items:center; margin-bottom:8px;
               flex-wrap:wrap; padding:6px 10px; background:#f1f5f9;
               border-radius:6px; border:1px solid #e2e8f0; }}
  label {{ font-size:.78rem; color:#475569; display:flex; align-items:center; gap:5px; }}
  input[type=range] {{ width:140px; accent-color:#2563eb; }}
  select {{ background:#fff; color:#1e293b; border:1px solid #cbd5e1;
            border-radius:4px; padding:2px 6px; font-size:.78rem; }}
  #canvasWrap {{ flex:1; position:relative; min-height:0; }}
  canvas {{ width:100%; height:100%; display:block; border-radius:6px;
            border:1px solid #e2e8f0; background:#f8fafc; cursor:crosshair; }}
  #tooltip {{ position:fixed; background:rgba(255,255,255,.97); border:1px solid #cbd5e1;
               padding:9px 13px; border-radius:8px; font-size:.76rem; pointer-events:none;
               display:none; line-height:1.8; min-width:190px;
               box-shadow:0 4px 16px rgba(0,0,0,.12); color:#1e293b; }}
  .legend {{ display:flex; align-items:center; gap:8px; margin-top:5px; font-size:.73rem; color:#64748b; flex-wrap:wrap; }}
  .grad {{ width:180px; height:12px; border-radius:3px; flex-shrink:0;
           background:linear-gradient(to right,#1a3aff,#00bcd4,#00c853,#ffd600,#ff4422); }}
  .mat-badge {{ display:inline-block; padding:1px 7px; border-radius:8px; font-size:.7rem; margin-right:4px; }}
  .T0 {{ background:#dbeafe; color:#1e40af; }}
  .T1 {{ background:#fce7f3; color:#9d174d; }}
</style>
</head>
<body>
<div id="shell">
<h1>🌡️ Thermal Heat Map — {self.user['part_name']}</h1>
<div class="subtitle">
  {datetime.now().strftime('%Y-%m-%d %H:%M')} &nbsp;·&nbsp;
  {len(self.thermal_data)} waypoints &nbsp;·&nbsp;
  Eagar-Tsai melt pool + Rykalin thermal
</div>

<div class="controls">
  <label>Layer:
    <input type="range" id="layerSlider" min="0" max="{len(sampled_layers)-1}" value="0" oninput="drawLayer(+this.value)">
    <span id="layerLabel" style="min-width:72px;display:inline-block;font-weight:600;color:#1e293b"></span>
  </label>
  <label><input type="checkbox" id="showAll" onchange="toggleAll()"> All layers</label>
  <label>Width:
    <input type="range" id="lineWidth" min="1" max="10" value="3" oninput="drawLayer(currentLayer)">
    <span id="lwLabel">3</span>px
  </label>
  <label>Color:
    <select id="colorMode" onchange="drawLayer(currentLayer)">
      <option value="temp">Temperature</option>
      <option value="ved">VED</option>
      <option value="speed">Speed</option>
      <option value="curv">Curvature</option>
    </select>
  </label>
  <label><input type="checkbox" id="showDots" onchange="drawLayer(currentLayer)"> Dots</label>
  <label><input type="checkbox" id="showBoundaries" checked onchange="drawLayer(currentLayer)"> Seam</label>
  <label><input type="checkbox" id="showRisk" checked onchange="drawLayer(currentLayer)"> Risk</label>
  <span style="font-size:.72rem;color:#94a3b8;margin-left:auto">← → keys to navigate</span>
</div>

<div id="canvasWrap">
  <canvas id="cv"></canvas>
  <div id="tooltip"></div>
</div>

<div class="legend">
  <span class="grad-label">{t_min:.0f}°C</span>
  <div class="grad"></div>
  <span class="grad-label">{t_max:.0f}°C</span>
  &nbsp;
  <span style="color:#e53e3e">■</span> <span class="grad-label">LOF/Stub</span> &nbsp;
  <span style="color:#dd6b20">●</span> <span class="grad-label">Keyhole</span> &nbsp;
  <span style="color:#276749">◆</span> <span class="grad-label">Seam start</span> &nbsp;
  <span style="color:#6b46c1">■</span> <span class="grad-label">Seam end</span>
  &nbsp;
  {''.join(f'<span class="mat-badge {f}">{f}={self.user.get("material_"+f,"?")}</span>' for f in ["T0","T1"] if f in self.materials_found)}
  <span class="grad-label" style="margin-left:auto">{len(sampled_layers)}/{len(layers)} layers shown · {self.user["laser_power"]}W · {self.user["layer_height"]}×{self.user["layer_width"]}mm</span>
</div>
</div><!-- /shell -->

<script>
const LAYERS = {layers_js};
const LAYER_NUMS = {layers_list};
const MARKERS = {markers_js};
let currentLayer = 0;
let showAll = false;

const cv      = document.getElementById('cv');
const ctx     = cv.getContext('2d');
const wrap    = document.getElementById('canvasWrap');
const slider  = document.getElementById('layerSlider');
const tooltip = document.getElementById('tooltip');

// ── Resize ────────────────────────────────────────────────────────────────
function resizeCanvas() {{
  const r = wrap.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) {{
    cv.width  = Math.floor(r.width);
    cv.height = Math.floor(r.height);
    drawLayer(currentLayer);
  }}
}}
window.addEventListener('resize', resizeCanvas);

// ── Data ranges ───────────────────────────────────────────────────────────
const allPts = LAYERS.flat();
const xs = allPts.map(p=>p.x), ys = allPts.map(p=>p.y);
const zs3d = allPts.map(p=>p.z||0);
const xMin=Math.min(...xs), xMax=Math.max(...xs);
const yMin=Math.min(...ys), yMax=Math.max(...ys);
const zMin=Math.min(...zs3d), zMax=Math.max(...zs3d);
const vedMin  = Math.min(...allPts.map(p=>p.ved||0));
const vedMax  = Math.max(...allPts.map(p=>p.ved||0)) || 1;
const spdMin  = Math.min(...allPts.map(p=>p.speed||0));
const spdMax  = Math.max(...allPts.map(p=>p.speed||0)) || 1;
const cvMax   = Math.max(...allPts.map(p=>p.cv||0)) || 1;
const pad = 36;

// ── Quaternion helpers ────────────────────────────────────────────────────
function quatFromAxisAngle(ax, ay, az, angle) {{
  const s = Math.sin(angle/2);
  return [Math.cos(angle/2), ax*s, ay*s, az*s];
}}
function quatMul(a, b) {{
  const [aw,ax,ay,az] = a, [bw,bx,by,bz] = b;
  return [
    aw*bw - ax*bx - ay*by - az*bz,
    aw*bx + ax*bw + ay*bz - az*by,
    aw*by - ax*bz + ay*bw + az*bx,
    aw*bz + ax*by - ay*bx + az*bw,
  ];
}}
function quatNorm(q) {{
  const n = Math.sqrt(q[0]*q[0]+q[1]*q[1]+q[2]*q[2]+q[3]*q[3]);
  return n > 0 ? q.map(v=>v/n) : [1,0,0,0];
}}
function rotateVec(q, v) {{
  const [w,qx,qy,qz] = q, [vx,vy,vz] = v;
  const tx = 2*(qy*vz - qz*vy);
  const ty = 2*(qz*vx - qx*vz);
  const tz = 2*(qx*vy - qy*vx);
  return [vx+w*tx+qy*tz-qz*ty, vy+w*ty+qz*tx-qx*tz, vz+w*tz+qx*ty-qy*tx];
}}

// ── 3D state ──────────────────────────────────────────────────────────────
// Default: isometric-ish view — tilted 25° around X, 20° around Z
let quat = quatNorm(quatMul(quatFromAxisAngle(1,0,0,-0.44),
                             quatFromAxisAngle(0,0,1, 0.35)));
let zoom = 1.0;
let panX = 0, panY = 0;

// Centre + scale of 3D data
const cx3 = (xMin+xMax)/2, cy3 = (yMin+yMax)/2, cz3 = (zMin+zMax)/2;
const span3 = Math.max(xMax-xMin, yMax-yMin, zMax-zMin, 1);

// ── Project 3D → canvas 2D (perspective) ─────────────────────────────────
const FOV = 2.8;
function toCanvas(x, y, z) {{
  const nx = (x-cx3)/span3, ny = (y-cy3)/span3, nz = ((z||0)-cz3)/span3;
  const [rx,ry,rz] = rotateVec(quat, [nx, ny, nz]);
  const s = zoom / (1 + rz/FOV);
  const W = (cv.width -2*pad)*0.48, H = (cv.height-2*pad)*0.48;
  return [cv.width/2  + panX + rx*s*W,
          cv.height/2 + panY - ry*s*H,
          rz];
}}

// ── Inertia ───────────────────────────────────────────────────────────────
let velX = 0, velY = 0;   // angular velocity (rad/frame)
let animId = null;
function applyInertia() {{
  if (Math.hypot(velX, velY) < 0.0003) {{ velX = velY = 0; animId = null; return; }}
  velX *= 0.88; velY *= 0.88;
  const speed = Math.hypot(velX, velY);
  // Rotate around screen-space axes: horizontal drag → world Y, vertical → world X
  const worldY = rotateVec([quat[0],-quat[1],-quat[2],-quat[3]], [0,1,0]);
  const worldX = rotateVec([quat[0],-quat[1],-quat[2],-quat[3]], [1,0,0]);
  const dqH = quatFromAxisAngle(...worldY, velY);
  const dqV = quatFromAxisAngle(...worldX, velX);
  quat = quatNorm(quatMul(dqH, quatMul(dqV, quat)));
  drawLayer(currentLayer);
  animId = requestAnimationFrame(applyInertia);
}}

// ── Mouse drag: rotate (left) or pan (middle/Ctrl) ────────────────────────
let drag = false, dragX = 0, dragY = 0, dragBtn = 0;
cv.style.cursor = 'grab';

cv.addEventListener('mousedown', e => {{
  if (animId) {{ cancelAnimationFrame(animId); animId=null; }}
  drag=true; dragX=e.clientX; dragY=e.clientY; dragBtn=e.button;
  cv.style.cursor = (dragBtn===1||e.ctrlKey) ? 'move' : 'grabbing';
  tooltip.style.display='none';
  e.preventDefault();
}});
window.addEventListener('mouseup', () => {{
  if (drag && dragBtn!==1 && !dragCtrl) {{
    // kick off inertia when releasing rotate drag
    if (!animId && Math.hypot(velX,velY)>0.0005)
      animId = requestAnimationFrame(applyInertia);
  }}
  drag=false; cv.style.cursor='grab';
}});
let dragCtrl=false;
window.addEventListener('mousemove', e => {{
  if (!drag) return;
  const dx = e.clientX-dragX, dy = e.clientY-dragY;
  dragX=e.clientX; dragY=e.clientY;
  dragCtrl = e.ctrlKey;

  if (dragBtn===1 || dragCtrl) {{
    // Middle-click or Ctrl → pan
    panX+=dx; panY+=dy;
  }} else {{
    // Left drag → arcball-style rotation
    // Horizontal mouse → rotate around screen-up (world Y in camera space)
    // Vertical mouse   → rotate around screen-right (world X in camera space)
    const sens = 2.8 / Math.min(cv.width, cv.height);
    const ax = dy * sens;   // pitch
    const ay = dx * sens;   // yaw
    velX = ax * 0.4;
    velY = ay * 0.4;
    // World-space axes so rotation stays intuitive at any orientation
    const worldUp    = rotateVec([quat[0],-quat[1],-quat[2],-quat[3]], [0,1,0]);
    const worldRight = rotateVec([quat[0],-quat[1],-quat[2],-quat[3]], [1,0,0]);
    const dqYaw   = quatFromAxisAngle(...worldUp,    ay);
    const dqPitch = quatFromAxisAngle(...worldRight, ax);
    quat = quatNorm(quatMul(dqYaw, quatMul(dqPitch, quat)));
  }}
  drawLayer(currentLayer);
}});

// ── Scroll wheel: zoom toward cursor ─────────────────────────────────────
cv.addEventListener('wheel', e => {{
  e.preventDefault();
  const rect = cv.getBoundingClientRect();
  const mx = e.clientX-rect.left, my = e.clientY-rect.top;
  const factor = e.deltaY<0 ? 1.13 : 0.885;
  // Shift pan so the point under the cursor stays fixed
  panX = mx + (panX-mx)*factor;
  panY = my + (panY-my)*factor;
  zoom = Math.max(0.12, Math.min(zoom*factor, 12.0));
  drawLayer(currentLayer);
}}, {{passive:false}});

// ── Right-click drag → pan (alternative to Ctrl+drag) ────────────────────
cv.addEventListener('contextmenu', e => e.preventDefault());

// ── Touch: 1 finger = rotate, 2 fingers = pinch-zoom + pan ───────────────
let touchX=0, touchY=0, touchDist=0, touchMidX=0, touchMidY=0;
cv.addEventListener('touchstart', e => {{
  if (animId) {{ cancelAnimationFrame(animId); animId=null; }}
  if (e.touches.length===1) {{
    touchX=e.touches[0].clientX; touchY=e.touches[0].clientY;
  }} else if (e.touches.length===2) {{
    const dx=e.touches[0].clientX-e.touches[1].clientX;
    const dy=e.touches[0].clientY-e.touches[1].clientY;
    touchDist=Math.hypot(dx,dy);
    touchMidX=(e.touches[0].clientX+e.touches[1].clientX)/2;
    touchMidY=(e.touches[0].clientY+e.touches[1].clientY)/2;
  }}
}}, {{passive:true}});
cv.addEventListener('touchmove', e => {{
  e.preventDefault();
  if (e.touches.length === 1) {{
    const dx = e.touches[0].clientX - touchX;
    const dy = e.touches[0].clientY - touchY;
    touchX = e.touches[0].clientX; touchY = e.touches[0].clientY;
    const dqX = quatFromEuler(-dy/cv.height*Math.PI, 0, 0);
    const dqY = quatFromEuler(0, dx/cv.width*Math.PI, 0);
    // 1-finger rotate (world-space arcball)
    const sens = 2.8 / Math.min(cv.width, cv.height);
    const ax = (e.touches[0].clientY-touchY)*sens;
    const ay = (e.touches[0].clientX-touchX)*sens;
    touchX=e.touches[0].clientX; touchY=e.touches[0].clientY;
    const wUp    = rotateVec([quat[0],-quat[1],-quat[2],-quat[3]], [0,1,0]);
    const wRight = rotateVec([quat[0],-quat[1],-quat[2],-quat[3]], [1,0,0]);
    quat = quatNorm(quatMul(quatFromAxisAngle(...wUp,ay),
                             quatMul(quatFromAxisAngle(...wRight,ax), quat)));
  }} else if (e.touches.length===2) {{
    const dx=e.touches[0].clientX-e.touches[1].clientX;
    const dy=e.touches[0].clientY-e.touches[1].clientY;
    const newDist=Math.hypot(dx,dy);
    const newMidX=(e.touches[0].clientX+e.touches[1].clientX)/2;
    const newMidY=(e.touches[0].clientY+e.touches[1].clientY)/2;
    if (touchDist>0) {{
      const factor = newDist/touchDist;
      // Zoom toward pinch midpoint
      panX = newMidX + (panX-touchMidX)*factor + (newMidX-touchMidX);
      panY = newMidY + (panY-touchMidY)*factor + (newMidY-touchMidY);
      zoom = Math.max(0.12, Math.min(zoom*factor, 12.0));
    }}
    touchDist=newDist; touchMidX=newMidX; touchMidY=newMidY;
  }}
  drawLayer(currentLayer);
}}, {{passive:false}});

// ── Double-click / double-tap: reset all ──────────────────────────────────
function resetView() {{
  if (animId) {{ cancelAnimationFrame(animId); animId=null; }}
  velX=0; velY=0;
  quat = quatNorm(quatMul(quatFromAxisAngle(1,0,0,-0.44),
                           quatFromAxisAngle(0,0,1, 0.35)));
  zoom=1.0; panX=0; panY=0;
  drawLayer(currentLayer);
}}
cv.addEventListener('dblclick', resetView);

// ── View preset buttons (Top / Front / Side) ──────────────────────────────
// Injected as overlay buttons inside canvasWrap
(function(){{
  const btns = [
    ['T', 'Top',   quatFromAxisAngle(1,0,0,-Math.PI/2)],
    ['F', 'Front', [1,0,0,0]],
    ['S', 'Side',  quatFromAxisAngle(0,1,0, Math.PI/2)],
    ['I', 'Iso',   quatNorm(quatMul(quatFromAxisAngle(1,0,0,-0.44),
                                     quatFromAxisAngle(0,0,1, 0.35)))],
  ];
  const bar = document.createElement('div');
  bar.style.cssText='position:absolute;top:6px;right:8px;display:flex;gap:4px;z-index:10';
  btns.forEach(([label,title,q])=>{{
    const b=document.createElement('button');
    b.textContent=label; b.title=title;
    b.style.cssText='width:26px;height:26px;border:1px solid #cbd5e1;border-radius:5px;'+
      'background:rgba(255,255,255,0.85);color:#475569;font-size:.72rem;font-weight:700;'+
      'cursor:pointer;line-height:1;padding:0;backdrop-filter:blur(4px)';
    b.addEventListener('click',()=>{{
      if (animId) {{ cancelAnimationFrame(animId); animId=null; }}
      velX=0; velY=0; quat=quatNorm(q); zoom=1.0; panX=0; panY=0;
      drawLayer(currentLayer);
    }});
    bar.appendChild(b);
  }});
  wrap.style.position='relative';
  wrap.appendChild(bar);
}})();

// Map a 0-1 fraction to a colour on the blue→cyan→green→yellow→red spectrum
function fracToColor(t) {{
  const stops = [
    [0.00, [26,  58, 255]],
    [0.25, [0,  212, 255]],
    [0.50, [0,  255, 136]],
    [0.75, [255,204,  0]],
    [1.00, [255, 68,  34]],
  ];
  for (let i=1; i<stops.length; i++) {{
    if (t <= stops[i][0]) {{
      const lo=stops[i-1], hi=stops[i];
      const f=(t-lo[0])/(hi[0]-lo[0]);
      const r=lo[1].map((v,j)=>Math.round(v+(hi[1][j]-v)*f));
      return `rgb(${{r[0]}},${{r[1]}},${{r[2]}})`;
    }}
  }}
  return 'rgb(255,68,34)';
}}

function ptColor(p) {{
  const mode = document.getElementById('colorMode').value;
  if (mode==='ved')   return fracToColor(Math.max(0,Math.min(1,(p.ved-vedMin)/(vedMax-vedMin+1e-9))));
  if (mode==='speed') return fracToColor(Math.max(0,Math.min(1,(p.speed-spdMin)/(spdMax-spdMin+1e-9))));
  if (mode==='curv')  return fracToColor(Math.max(0,Math.min(1,p.cv/Math.max(cvMax,1))));
  return p.color; // default: temperature
}}

function drawLayer(idx) {{
  currentLayer = idx;
  const lwEl = document.getElementById('lineWidth');
  const lw   = +lwEl.value;
  document.getElementById('lwLabel').textContent = lw;
  document.getElementById('layerLabel').textContent = ' Layer ' + LAYER_NUMS[idx];
  slider.value = idx;

  // White background fill
  ctx.fillStyle='#f8fafc';
  ctx.fillRect(0,0,cv.width,cv.height);

  // Subtle light grid
  ctx.strokeStyle='rgba(100,116,139,0.10)';
  ctx.lineWidth=1;
  for(let gx=pad;gx<cv.width-pad;gx+=(cv.width-2*pad)/8){{ctx.beginPath();ctx.moveTo(gx,pad);ctx.lineTo(gx,cv.height-pad);ctx.stroke();}}
  for(let gy=pad;gy<cv.height-pad;gy+=(cv.height-2*pad)/6){{ctx.beginPath();ctx.moveTo(pad,gy);ctx.lineTo(cv.width-pad,gy);ctx.stroke();}}

  const layerSets = showAll ? LAYERS : [LAYERS[idx]];
  const showDots  = document.getElementById('showDots').checked;
  const showRisk  = document.getElementById('showRisk').checked;

  layerSets.forEach((pts, li) => {{
    if (!pts || pts.length < 2) return;

    // ── Continuous gradient line (3D projected) ──────────────────────────
    ctx.lineCap  = 'round';
    ctx.lineJoin = 'round';
    for (let i=1; i<pts.length; i++) {{
      const p0=pts[i-1], p1=pts[i];
      const [ax,ay,az] = toCanvas(p0.x, p0.y, p0.z||0);
      const [bx,by,bz] = toCanvas(p1.x, p1.y, p1.z||0);

      if (Math.hypot(bx-ax,by-ay) < 0.3) continue;

      // Depth-fade: back points slightly lighter, front points full colour
      const depthAlpha = showAll ? Math.max(0.15, 0.55 - (az+bz)/2 * 0.25) : 1.0;
      const grad = ctx.createLinearGradient(ax,ay,bx,by);
      if (showAll) {{
        grad.addColorStop(0, `rgba(${{hexToRgb(ptColor(p0))}},${{depthAlpha.toFixed(2)}})`);
        grad.addColorStop(1, `rgba(${{hexToRgb(ptColor(p1))}},${{depthAlpha.toFixed(2)}})`);
      }} else {{
        grad.addColorStop(0, ptColor(p0));
        grad.addColorStop(1, ptColor(p1));
      }}
      ctx.beginPath();
      ctx.moveTo(ax,ay);
      ctx.lineTo(bx,by);
      ctx.strokeStyle = grad;
      ctx.lineWidth   = lw;
      ctx.stroke();
    }}

    // ── Risk overlay ──────────────────────────────────────────────────────
    if (showRisk) {{
      pts.forEach(p => {{
        if (!p.lof && !p.kh && !p.stub) return;
        const [cx,cy] = toCanvas(p.x, p.y, p.z||0);
        const riskColor = p.stub ? 'rgba(255,30,30,0.55)'
                        : p.lof  ? 'rgba(255,80,0,0.45)'
                                 : 'rgba(255,165,0,0.40)';
        ctx.beginPath();
        ctx.arc(cx, cy, lw*2.5, 0, Math.PI*2);
        ctx.fillStyle = riskColor;
        ctx.fill();
      }});
    }}

    // ── Optional dots ─────────────────────────────────────────────────────
    if (showDots) {{
      pts.forEach(p => {{
        const [cx,cy] = toCanvas(p.x, p.y, p.z||0);
        const r = lw * 0.9 + Math.min(p.cv/90,1)*lw*1.2;
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI*2);
        ctx.fillStyle = ptColor(p);
        ctx.fill();
        if (p.cv > 30) {{
          ctx.beginPath();
          ctx.arc(cx, cy, r+2, 0, Math.PI*2);
          ctx.strokeStyle='rgba(255,255,255,0.5)';
          ctx.lineWidth=1;
          ctx.stroke();
        }}
      }});
    }}
  }});

  // ── Seam markers (3D projected) ──────────────────────────────────────
  if (document.getElementById('showBoundaries').checked) {{
    MARKERS.forEach(marker => {{
      if (!showAll && LAYER_NUMS[idx] !== marker.layer) return;
      const [sx,sy] = toCanvas(marker.start_x, marker.start_y, marker.start_z||0);
      const tc = marker.start_tc || 0;
      const seamColor = tc > 1190 ? '#ff3344' : tc > 850 ? '#ffaa00' : '#00ff88';
      ctx.beginPath();
      ctx.moveTo(sx, sy-9); ctx.lineTo(sx+9, sy);
      ctx.lineTo(sx, sy+9); ctx.lineTo(sx-9, sy);
      ctx.closePath();
      ctx.fillStyle=seamColor; ctx.fill();
      ctx.strokeStyle='#fff'; ctx.lineWidth=1.5; ctx.stroke();
      const [ex,ey] = toCanvas(marker.stop_x, marker.stop_y, marker.stop_z||0);
      ctx.fillStyle='rgba(180,50,255,0.9)';
      ctx.fillRect(ex-5,ey-5,10,10);
      ctx.strokeStyle='#fff'; ctx.lineWidth=1;
      ctx.strokeRect(ex-5,ey-5,10,10);
    }});
  }}

  // ── 3D Axis gizmo (bottom-left corner) ──────────────────────────────────
  const gx = pad + 30, gy = cv.height - pad - 30, gLen = 28;
  const axes = [
    [[1,0,0], '#e53e3e', 'X'],
    [[0,1,0], '#276749', 'Y'],
    [[0,0,1], '#2563eb', 'Z'],
  ];
  axes.forEach(([vec, col, label]) => {{
    const [ex,ey] = toCanvas(
      cx3 + vec[0]*span3*0.55,
      cy3 + vec[1]*span3*0.55,
      cz3 + vec[2]*span3*0.55
    );
    // Convert to gizmo-local coords
    const [ox,oy] = toCanvas(cx3, cy3, cz3);
    const dx = ex-ox, dy = ey-oy;
    const len = Math.hypot(dx,dy) || 1;
    const nx = dx/len*gLen, ny = dy/len*gLen;
    ctx.beginPath();
    ctx.moveTo(gx, gy);
    ctx.lineTo(gx+nx, gy+ny);
    ctx.strokeStyle=col; ctx.lineWidth=2.5; ctx.stroke();
    ctx.fillStyle=col; ctx.font='bold 11px monospace';
    ctx.fillText(label, gx+nx*1.25-4, gy+ny*1.25+4);
  }});
  // Origin dot
  ctx.beginPath(); ctx.arc(gx,gy,4,0,Math.PI*2);
  ctx.fillStyle='rgba(71,85,105,0.6)'; ctx.fill();

  // Scale + drag hint (bottom-right)
  const xSpan = (xMax - xMin), ySpan = (yMax - yMin), zSpan = (zMax - zMin);
  ctx.fillStyle='rgba(71,85,105,0.65)'; ctx.font='9px monospace';
  ctx.textAlign='right';
  ctx.fillText(`${{xSpan.toFixed(0)}}×${{ySpan.toFixed(0)}}×${{zSpan.toFixed(0)}} mm`, cv.width-pad, cv.height-14);
  ctx.fillText(`drag=rotate · scroll=zoom(${{zoom.toFixed(1)}}x) · mid/Ctrl=pan · dbl-click=reset`, cv.width-pad, cv.height-4);
  ctx.textAlign='left';

  // Layer info (top-left)
  const pts = showAll ? LAYERS.flat() : LAYERS[idx];
  const layerInfo = showAll
    ? 'All ' + LAYERS.length + ' layers (' + pts.length + ' pts)'
    : 'Layer ' + LAYER_NUMS[idx] + ' / ' + LAYER_NUMS[LAYERS.length-1] + '  (' + pts.length + ' pts)';
  ctx.fillStyle='rgba(30,64,175,0.70)';
  ctx.font='bold 12px monospace';
  ctx.fillText(layerInfo, 8, 18);
}}

// Helper: extract r,g,b from any css color string
function hexToRgb(color) {{
  const m = color.match(/\d+/g);
  return m ? m.join(',') : '200,200,200';
}}

function toggleAll() {{
  showAll = document.getElementById('showAll').checked;
  drawLayer(currentLayer);
}}

// ── Keyboard navigation ───────────────────────────────────────────────────
document.addEventListener('keydown', e => {{
  if (e.key==='ArrowRight' || e.key==='ArrowUp')   drawLayer(Math.min(currentLayer+1, LAYERS.length-1));
  if (e.key==='ArrowLeft'  || e.key==='ArrowDown')  drawLayer(Math.max(currentLayer-1, 0));
}});

// ── Tooltip ───────────────────────────────────────────────────────────────
cv.addEventListener('mousemove', e => {{
  const rect = cv.getBoundingClientRect();
  const mx = e.clientX-rect.left, my = e.clientY-rect.top;
  const hitR = Math.max(6, +document.getElementById('lineWidth').value * 2);

  // Seam markers first
  if (document.getElementById('showBoundaries').checked) {{
    const active = showAll ? MARKERS : MARKERS.filter(m=>m.layer===LAYER_NUMS[currentLayer]);
    for (const m of active) {{
      const [sx,sy]=toCanvas(m.start_x,m.start_y,m.start_z||0);
      if (Math.hypot(mx-sx,my-sy)<13) {{
        tooltip.style.display='block';
        tooltip.style.left=(e.clientX+14)+'px'; tooltip.style.top=(e.clientY+14)+'px';
        tooltip.innerHTML=`<b>🧵 SEAM START — Layer ${{m.layer}}</b><br>
          Temp: <b style="color:#fab432">${{m.start_tc?.toFixed(0)||'—'}} °C</b><br>
          Gap prev layer: ${{m.gap_mm>0?m.gap_mm.toFixed(2)+' mm':'—'}}<br>
          Overlap energy: ${{m.overlap_e>0?m.overlap_e.toFixed(1)+' J/mm':'—'}}<br>
          XY: ${{m.start_x.toFixed(2)}}, ${{m.start_y.toFixed(2)}}`;
        return;
      }}
      const [ex,ey]=toCanvas(m.stop_x,m.stop_y,m.stop_z||0);
      if (Math.hypot(mx-ex,my-ey)<10) {{
        tooltip.style.display='block';
        tooltip.style.left=(e.clientX+14)+'px'; tooltip.style.top=(e.clientY+14)+'px';
        tooltip.innerHTML=`<b>🧵 SEAM END — Layer ${{m.layer}}</b><br>
          Temp: <b style="color:#c070ff">${{m.stop_tc?.toFixed(0)||'—'}} °C</b><br>
          XY: ${{m.stop_x.toFixed(2)}}, ${{m.stop_y.toFixed(2)}}`;
        return;
      }}
    }}
  }}

  // Nearest waypoint within hitR
  const pts = showAll ? LAYERS.flat() : LAYERS[currentLayer];
  let best=null, bestD=hitR;
  for (const p of pts) {{
    const [cx,cy]=toCanvas(p.x,p.y,p.z||0);
    const d=Math.hypot(mx-cx,my-cy);
    if (d<bestD) {{ bestD=d; best=p; }}
  }}
  if (best) {{
    tooltip.style.display='block';
    tooltip.style.left=(e.clientX+14)+'px'; tooltip.style.top=(e.clientY+14)+'px';
    const riskFlags = [
      best.stub ? '<span style="color:#ff4422">⚠ STUBBING</span>' : '',
      best.lof  ? '<span style="color:#ff8800">⚠ LOF</span>'      : '',
      best.kh   ? '<span style="color:#ffcc00">⚠ KEYHOLE</span>'  : '',
    ].filter(Boolean).join(' ');
    tooltip.innerHTML=`<b style="color:#00d4ff">Layer point</b> ${{riskFlags?'— '+riskFlags:''}}<br>
      Temp: <b style="color:#fab432">${{best.tc}} °C</b><br>
      VED: <b>${{best.ved?.toFixed(1)||'—'}} J/mm³</b> &nbsp; Melt W: <b>${{best.mw?.toFixed(2)||'—'}} mm</b><br>
      Speed: ${{best.speed}} mm/s &nbsp; Curv: ${{best.cv}}°${{best.cv>30?' ⚠':''}}<br>
      Material: ${{best.material}}<br>
      XYZ: ${{best.x}}, ${{best.y}}, ${{best.z}}`;
  }} else {{
    tooltip.style.display='none';
  }}
}});
cv.addEventListener('mouseleave',()=>tooltip.style.display='none');

// Initial size — must run after layout is computed
requestAnimationFrame(() => {{ resizeCanvas(); drawLayer(0); }});
</script>
</body>
</html>"""

        out = Path(self.zip_path).parent / f"../../Desktop/Clude code/outputs/heatmap_{self.user['part_name']}_{timestamp}.html"
        out = (Path(__file__).parent / "outputs" / f"heatmap_{self.user['part_name']}_{timestamp}.html").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"   ✅ HTML heatmap: {out}")
        return str(out)

    # ─── STEP 5b: CSV ───────────────────────────────────────────────────────

    def generate_csv(self, timestamp: str) -> str:
        out = (Path(__file__).parent / "outputs" / f"heatmap_{self.user['part_name']}_{timestamp}.csv").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "layer_num", "x", "y", "z", "speed", "material",
            "temp_C", "temp_C_residual", "delta_T_C", "curvature_deg",
            "E_linear_Jmm", "absorption", "tau_cool_s",
            "V1_wire_mm3s", "V2_geometry_mm3s", "heat_index", "thermal_mass",
            # anomaly metrics
            "VED", "norm_H", "melt_depth_mm", "melt_width_mm", "melt_width_ratio", "melt_fuse_ratio",
            "G_Km", "R_ms", "cooling_rate_Ks", "G_over_R", "cracking_score",
            "lof_risk", "keyhole_risk", "stubbing_risk", "overheat_risk", "lof_depth_risk",
            "ti_phase", "lath_width_um", "pdas_nm",
            # seam metrics
            "is_seam_start", "is_seam_end", "seam_gap_mm", "seam_overlap_energy", "seam_risk",
        ]
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for d in self.thermal_data:
                w.writerow({
                    "layer_num":           d["layer_num"],
                    "x": d["x"], "y": d["y"], "z": d["z"],
                    "speed":               d["speed"],
                    "material":            d["material"],
                    "temp_C":              d["temp_C"],
                    "temp_C_residual":     d.get("temp_C_residual", ""),
                    "delta_T_C":           d.get("delta_T", 0),
                    "curvature_deg":       d.get("curvature_deg", 0),
                    "E_linear_Jmm":        d.get("E_linear_Jmm", 0),
                    "absorption":          d.get("absorption", 0),
                    "tau_cool_s":          d.get("tau_cool_s", 0),
                    "V1_wire_mm3s":        d["V1_wire"],
                    "V2_geometry_mm3s":    d["V2_geometry"],
                    "heat_index":          d["heat_index"],
                    "thermal_mass":        d["thermal_mass"],
                    # anomaly metrics
                    "VED":                 d.get("VED", ""),
                    "norm_H":              d.get("norm_H", ""),
                    "melt_depth_mm":       d.get("melt_depth_mm", ""),
                    "melt_width_mm":       d.get("melt_width_mm", ""),
                    "melt_width_ratio":    d.get("melt_width_ratio", ""),
                    "melt_fuse_ratio":     d.get("melt_fuse_ratio", ""),
                    "G_Km":                d.get("G_Km", ""),
                    "R_ms":                d.get("R_ms", ""),
                    "cooling_rate_Ks":     d.get("cooling_rate_Ks", ""),
                    "G_over_R":            d.get("G_over_R", ""),
                    "cracking_score":      d.get("cracking_score", ""),
                    "lof_risk":            d.get("lof_risk", False),
                    "keyhole_risk":        d.get("keyhole_risk", False),
                    "stubbing_risk":       d.get("stubbing_risk", False),
                    "overheat_risk":       d.get("overheat_risk", False),
                    "lof_depth_risk":      d.get("lof_depth_risk", False),
                    "ti_phase":            d.get("ti_phase", ""),
                    "lath_width_um":       d.get("lath_width_um", ""),
                    "pdas_nm":             d.get("pdas_nm", ""),
                    # seam metrics
                    "is_seam_start":       d.get("is_seam_start", False),
                    "is_seam_end":         d.get("is_seam_end", False),
                    "seam_gap_mm":         d.get("seam_gap_mm", 0.0),
                    "seam_overlap_energy": d.get("seam_overlap_energy", 0.0),
                    "seam_risk":           d.get("seam_risk", "none"),
                })
        print(f"   ✅ CSV data: {out}")
        return str(out)

    # ─── STEP 5c: SVG ───────────────────────────────────────────────────────

    def generate_svg(self, timestamp: str) -> str:
        if not self.thermal_data:
            return ""

        MAX_SVG_PTS = 5000
        td_svg = self.thermal_data
        if len(td_svg) > MAX_SVG_PTS:
            step_svg = len(td_svg) // MAX_SVG_PTS
            td_svg = td_svg[::step_svg][:MAX_SVG_PTS]

        xs = [d["x"] for d in td_svg]
        ys = [d["y"] for d in td_svg]
        hi_vals = [d["heat_index"] for d in td_svg]
        hi_min, hi_max = min(hi_vals), max(hi_vals)
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)

        W, H, pad = 900, 600, 40
        def tx(x): return pad + (x - xmin) / (xmax - xmin + 1e-9) * (W - 2 * pad)
        def ty(y): return H - pad - (y - ymin) / (ymax - ymin + 1e-9) * (H - 2 * pad)

        circles = []
        for d in td_svg:
            color = heat_color_hex(d["heat_index"], hi_min, hi_max)
            circles.append(
                f'<circle cx="{tx(d["x"]):.1f}" cy="{ty(d["y"]):.1f}" r="4" '
                f'fill="{color}" opacity="0.8">'
                f'<title>Material:{d["material"]} X:{d["x"]} Y:{d["y"]} Z:{d["z"]} '
                f'Speed:{d["speed"]} HeatIdx:{d["heat_index"]:.4f}</title></circle>'
            )

        svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H+80}" style="background:#f5f7fb">
  <text x="20" y="24" fill="#00d4ff" font-family="Segoe UI,sans-serif" font-size="15">
    Meltio DED Heat Map — {self.user['part_name']} (Top View)
  </text>
  <text x="20" y="44" fill="#888" font-family="Segoe UI,sans-serif" font-size="11">
    Heat Index: {hi_min:.4f} – {hi_max:.4f} | Deposition points: {len(self.thermal_data)}
  </text>
  <!-- Gradient legend -->
  <defs>
    <linearGradient id="hg" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="rgb(0,0,255)"/>
      <stop offset="50%" stop-color="rgb(255,255,0)"/>
      <stop offset="100%" stop-color="rgb(255,0,0)"/>
    </linearGradient>
  </defs>
  <text x="20" y="{H+30}" fill="#ccc" font-family="Segoe UI,sans-serif" font-size="11">Low</text>
  <rect x="52" y="{H+18}" width="180" height="14" fill="url(#hg)" rx="4"/>
  <text x="238" y="{H+30}" fill="#ccc" font-family="Segoe UI,sans-serif" font-size="11">High</text>
  <!-- Data points -->
  {"".join(circles)}
</svg>"""

        out = (Path(__file__).parent / "outputs" / f"heatmap_{self.user['part_name']}_{timestamp}.svg").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(svg, encoding="utf-8")
        print(f"   ✅ SVG graphic: {out}")
        return str(out)

    # ─── STEP 5d: 3D PLOTLY HTML ────────────────────────────────────────────

    # ─── STEP 5e: PROCESS WINDOW CHART ─────────────────────────────────────

    def generate_process_window_html(self, timestamp: str) -> str:
        """
        P-v process window scatter plot (Ansys Additive Science style).
        X = robot speed [mm/s], Y = effective power A×P [W].
        Green zone = safe VED window per material.
        Blue zone  = LOF risk (VED too low).
        Red zone   = keyhole risk (VED too high / norm_H > 25).
        Iso-lines for VED and normalised enthalpy overlaid.
        """
        if not self.thermal_data:
            return ""

        try:
            laser_W = float(str(self.user.get("laser_power", "1000"))
                            .replace("W", "").replace("w", "").strip())
        except (ValueError, AttributeError):
            laser_W = 1000.0

        w_mm  = self.user["layer_width"]
        h_mm  = self.user["layer_height"]
        part  = self.user["part_name"]
        beam_d_mm = float(self.user.get("beam_spot_diameter", 1.2))
        beam_d_m  = beam_d_mm * 1e-3

        # Collect per-point data for scatter
        scatter_pts = []
        for d in self.thermal_data:
            mat = self.db_materials.get(d["material"]) or {}
            abs_val = mat.get("absorption_450nm", 0.45)
            eff_power = abs_val * laser_W
            anomaly = ("keyhole" if d.get("keyhole_risk")
                       else "lof" if (d.get("lof_risk") or d.get("lof_depth_risk"))
                       else "overheat" if d.get("overheat_risk")
                       else "safe")
            scatter_pts.append({
                "v": d["speed"],
                "p": round(eff_power, 1),
                "VED": d.get("VED", 0),
                "norm_H": d.get("norm_H", 0),
                "layer": d["layer_num"],
                "mat": d["material"],
                "temp": d["temp_C"],
                "anomaly": anomaly,
            })

        # Build separate arrays by anomaly type
        def pts_of(typ):
            return [p for p in scatter_pts if p["anomaly"] == typ]

        def build_trace(pts, name, color, symbol):
            if not pts:
                return "null"
            tip_list = [
                "Layer {} {}<br>VED:{} J/mm\u00b3<br>\u0394H/h_s:{:.1f}<br>T:{}°C".format(
                    p['layer'], p['mat'], p['VED'], p['norm_H'], p['temp']
                ) for p in pts
            ]
            return (
                f"{{type:'scatter',mode:'markers',name:'{name}',"
                f"x:{[p['v'] for p in pts]},"
                f"y:{[p['p'] for p in pts]},"
                f"marker:{{color:'{color}',symbol:'{symbol}',size:7,opacity:0.85,"
                f"line:{{color:'rgba(255,255,255,0.3)',width:0.5}}}},"
                f"text:{json.dumps(tip_list)},"
                f"hovertemplate:'Speed:%{{x}} mm/s | A×P:%{{y}} W<br>%{{text}}<extra></extra>'}}"
            )

        traces_js = "[" + ",".join(filter(None, [
            build_trace(pts_of("safe"),     "Safe zone",          "#00d97e", "circle"),
            build_trace(pts_of("lof"),      "LOF risk",           "#3399ff", "triangle-down"),
            build_trace(pts_of("keyhole"),  "Keyhole risk",       "#ff4455", "diamond"),
            build_trace(pts_of("overheat"), "Overheating",        "#ffaa00", "star"),
        ])) + "]"

        # VED iso-lines (constant VED curves in P-v space: P = VED × v × h × w / A)
        if not scatter_pts:
            return ""
        v_range = [max(0.5, min(p["v"] for p in scatter_pts) * 0.5),
                   max(p["v"] for p in scatter_pts) * 1.5]
        v_steps = [v_range[0] + i * (v_range[1] - v_range[0]) / 50 for i in range(51)]

        abs_avg = sum(
            (self.db_materials.get(d["material"]) or {}).get("absorption_450nm", 0.45)
            for d in self.thermal_data
        ) / max(len(self.thermal_data), 1)

        # Get process window from primary material
        primary_mat = self.db_materials.get("T0") or {}
        pw = primary_mat.get("process_window", {"VED_lof_min": 50, "VED_keyhole_max": 140})
        ved_lof = pw.get("VED_lof_min", 50)
        ved_kh  = pw.get("VED_keyhole_max", 140)

        def ved_line(ved_val, label, color, dash):
            p_vals = [round(ved_val * v * w_mm * h_mm / max(abs_avg, 0.01), 1) for v in v_steps]
            return (
                f"{{type:'scatter',mode:'lines',name:'{label}',"
                f"x:{[round(v, 2) for v in v_steps]},"
                f"y:{p_vals},"
                f"line:{{color:'{color}',dash:'{dash}',width:2}},"
                f"hovertemplate:'VED={ved_val} J/mm³ | Speed:%{{x}} | Power:%{{y}}<extra></extra>'}}"
            )

        # Normalised enthalpy iso-lines (ΔH/h_s = const → P = const × √v × ...)
        rho_avg = sum((self.db_materials.get(d["material"]) or {}).get("density", 7000)
                      for d in self.thermal_data) / max(len(self.thermal_data), 1)
        Cp_avg  = sum((self.db_materials.get(d["material"]) or {}).get("specific_heat", 500)
                      for d in self.thermal_data) / max(len(self.thermal_data), 1)
        k_avg   = sum((self.db_materials.get(d["material"]) or {}).get("thermal_conductivity", 15)
                      for d in self.thermal_data) / max(len(self.thermal_data), 1)
        T_liq_avg = sum((self.db_materials.get(d["material"]) or {}).get("T_liquidus", 1400)
                        for d in self.thermal_data) / max(len(self.thermal_data), 1)
        h_s_avg   = rho_avg * Cp_avg * T_liq_avg
        alpha_avg = k_avg / (rho_avg * Cp_avg) if (rho_avg * Cp_avg) > 0 else 1e-6

        def nh_line(nh_val, label, color):
            # P = nh_val × h_s × √(π × α × v_ms) × d^1.5 / absorption
            p_vals = []
            for v in v_steps:
                v_ms = max(v, 0.01) * 1e-3
                p_needed = nh_val * h_s_avg * math.sqrt(math.pi * alpha_avg * v_ms) * (beam_d_m ** 1.5) / max(abs_avg, 0.01)
                p_vals.append(round(p_needed, 1))
            return (
                f"{{type:'scatter',mode:'lines',name:'{label}',"
                f"x:{[round(v, 2) for v in v_steps]},"
                f"y:{p_vals},"
                f"line:{{color:'{color}',dash:'dot',width:1.5}},"
                f"hovertemplate:'ΔH/h_s={nh_val} | Speed:%{{x}} | Power:%{{y}}<extra></extra>'}}"
            )

        iso_traces = (
            ved_line(ved_lof, f"VED={ved_lof} J/mm³ (LOF limit)", "#3399ff", "dash") + "," +
            ved_line(ved_kh,  f"VED={ved_kh} J/mm³ (keyhole limit)", "#ff4455", "dash") + "," +
            nh_line(6,  "ΔH/h_s=6 (LOF onset)",     "#88ccff") + "," +
            nh_line(15, "ΔH/h_s=15 (conduction)",   "#aaffaa") + "," +
            nh_line(25, "ΔH/h_s=25 (keyhole onset)", "#ffaa88")
        )

        lof_ct = sum(1 for p in scatter_pts if p["anomaly"] == "lof")
        kh_ct  = sum(1 for p in scatter_pts if p["anomaly"] == "keyhole")
        oh_ct  = sum(1 for p in scatter_pts if p["anomaly"] == "overheat")
        sf_ct  = sum(1 for p in scatter_pts if p["anomaly"] == "safe")

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Process Window — {part}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#f8f9fc;font-family:'Segoe UI',sans-serif;color:#222;display:flex;flex-direction:column;height:100vh}}
  .hdr{{padding:10px 20px;background:#eaeff8;border-bottom:1px solid #c8d4e8;display:flex;align-items:center;gap:20px;flex-wrap:wrap}}
  .hdr h2{{color:#1e40af;font-size:1.1rem}}
  .stats{{display:flex;gap:12px;flex-wrap:wrap}}
  .stat{{background:#1e1e40;border:1px solid #333;border-radius:6px;padding:4px 12px;font-size:.78rem;text-align:center}}
  .stat .v{{font-size:1rem;font-weight:700}}
  .legend{{font-size:.7rem;color:#555;padding:5px 20px;background:#eaeff8;border-top:1px solid #c8d4e8}}
  #plot{{flex:1}}
</style>
</head>
<body>
<div class="hdr">
  <h2>📊 Process Window — {part}</h2>
  <div class="stats">
    <div class="stat"><div class="v" style="color:#00d97e">{sf_ct}</div><div>Safe</div></div>
    <div class="stat"><div class="v" style="color:#3399ff">{lof_ct}</div><div>LOF Risk</div></div>
    <div class="stat"><div class="v" style="color:#ff4455">{kh_ct}</div><div>Keyhole</div></div>
    <div class="stat"><div class="v" style="color:#ffaa00">{oh_ct}</div><div>Overheat</div></div>
    <div class="stat"><div class="v" style="color:#ccc">{beam_d_mm} mm</div><div>Beam ⌀</div></div>
  </div>
</div>
<div id="plot"></div>
<div class="legend">
  Dashed lines = VED iso-lines (LOF & keyhole limits for primary material) &nbsp;|&nbsp;
  Dotted lines = Normalised Enthalpy ΔH/h_s iso-lines (King et al.) &nbsp;|&nbsp;
  Beam spot ⌀ = {beam_d_mm} mm &nbsp;|&nbsp;
  Primary material: {primary_mat.get('display_name','—')} &nbsp;|&nbsp;
  Laser: 450 nm blue diode
</div>
<script>
const dataTraces = {traces_js};
const isoTraces  = [{iso_traces}];
const allTraces  = dataTraces.concat(isoTraces);

const layout = {{
  paper_bgcolor: '#f8f9fc',
  plot_bgcolor:  '#f0f3f9',
  xaxis: {{
    title: 'Robot Speed v (mm/s)',
    color: '#444', gridcolor: '#c4cedf', zerolinecolor: '#aab'
  }},
  yaxis: {{
    title: 'Effective Power A×P (W)',
    color: '#444', gridcolor: '#c4cedf', zerolinecolor: '#aab'
  }},
  legend: {{
    bgcolor: 'rgba(248,250,252,0.92)', bordercolor: '#ccd4e0', borderwidth: 1,
    font: {{color:'#333', size:10}}
  }},
  margin: {{l:60, r:20, t:10, b:50}},
  font: {{color:'#333', family:'Segoe UI,sans-serif'}}
}};

Plotly.newPlot('plot', allTraces, layout, {{responsive:true, displaylogo:false}});
</script>
</body>
</html>"""

        out = (Path(__file__).parent / "outputs" /
               f"process_window_{self.user['part_name']}_{timestamp}.html").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"   ✅ Process window chart: {out}")
        return str(out)

    def generate_3d_html(self, timestamp: str) -> str:
        if not self.thermal_data:
            return ""

        # ── Use residual temperature (heat state at END of print) ────────────
        # Each point cools exponentially from the moment the robot moves away.
        # Bottom layers = cooler (printed first), top layers = hotter (most recent).
        temps   = [d.get("temp_C_residual", d["temp_C"]) for d in self.thermal_data]
        curvs   = [d["curvature_deg"] for d in self.thermal_data]
        t_avg   = round(sum(temps) / len(temps), 1)
        hi_curv = sum(1 for c in curvs if c > 30)

        ambient_temp = float(self.user.get("ambient_temp", 25.0))
        dom_mat  = self.db_materials.get("T0") or self.db_materials.get("T1") or {}
        T_melt_c = dom_mat.get("melting_point", 1400)
        # Use actual data range so colorscale shows real variation (not ambient→melt)
        cmin_phys = min(temps)
        cmax_phys = max(temps)

        part    = self.user["part_name"]

        # Group by layer — single O(n) pass
        layer_data = defaultdict(list)
        for d in self.thermal_data:
            layer_data[d["layer_num"]].append(d)
        layers = sorted(layer_data.keys())

        # Downsample each layer to MAX_PTS_PER_LAYER to keep file size manageable
        MAX_PTS_PER_LAYER = max(1, 8000 // max(len(layers), 1))
        MAX_PTS_PER_LAYER = min(MAX_PTS_PER_LAYER, 1500)

        # Build ONE merged temperature trace (all layers, None gaps) instead of per-layer
        all_x, all_y, all_z, all_tc = [], [], [], []
        all_hover = []
        for idx, ln in enumerate(layers):
            pts = layer_data[ln]
            step = max(1, len(pts) // MAX_PTS_PER_LAYER)
            pts_ds = pts[::step]
            if idx > 0:
                all_x.append(None); all_y.append(None); all_z.append(None)
                all_tc.append(None); all_hover.append("")
            for p in pts_ds:
                tc_val = p.get("temp_C_residual", p["temp_C"])
                all_x.append(round(p["x"], 2))
                all_y.append(round(p["y"], 2))
                all_z.append(round(p["z"], 2))
                all_tc.append(round(tc_val, 0))
                all_hover.append(
                    f"Layer {ln} | {p['material']}<br>"
                    f"<b>{tc_val:.0f} °C</b> | {p['speed']} mm/s"
                )

        traces_js = (
            "[{"
            + f"type:'scatter3d',mode:'lines',name:'Toolpath',"
            + f"x:{json.dumps(all_x)},y:{json.dumps(all_y)},z:{json.dumps(all_z)},"
            + f"line:{{color:{json.dumps(all_tc)},colorscale:CS,"
            + f"cmin:{cmin_phys},cmax:{cmax_phys},width:6}},"
            + f"text:{json.dumps(all_hover)},"
            + f"hovertemplate:'%{{text}}<extra></extra>',visible:true"
            + "}]"
        )
        n_layer_traces = 1

        # ── Material view traces (T0 vs T1 in distinct colors) ────────────
        mat_names = sorted(self.materials_found)
        mat_colors = {"T0": "#00ccff", "T1": "#ff6633"}
        mat_traces_parts = []
        for mat_id in mat_names:
            mx, my, mz, mt = [], [], [], []
            mat_db = self.db_materials.get(mat_id)
            mat_display = mat_db["display_name"] if mat_db else mat_id
            for idx2, ln in enumerate(layers):
                pts = layer_data[ln]
                step = max(1, len(pts) // MAX_PTS_PER_LAYER)
                pts_ds = pts[::step]
                seg_started = False
                for p in pts_ds:
                    if p["material"] == mat_id:
                        mx.append(round(p["x"], 2)); my.append(round(p["y"], 2)); mz.append(round(p["z"], 2))
                        mt.append(f"Layer {ln} | {mat_display}<br>"
                                  f"{p.get('temp_C_residual', p['temp_C']):.0f}°C | {p['speed']} mm/s")
                        seg_started = True
                    elif seg_started:
                        mx.append(None); my.append(None); mz.append(None); mt.append("")
                        seg_started = False
                if seg_started:
                    # Gap between layers
                    mx.append(None); my.append(None); mz.append(None); mt.append("")

            color = mat_colors.get(mat_id, "#ffffff")
            mat_traces_parts.append(
                "{"
                + f"type:'scatter3d',mode:'lines',"
                + f"name:{json.dumps(mat_id + ' — ' + mat_display)},"
                + f"x:{json.dumps(mx)},y:{json.dumps(my)},z:{json.dumps(mz)},"
                + f"line:{{color:'{color}',width:5}},"
                + f"text:{json.dumps(mt)},"
                + f"hovertemplate:'%{{text}}<extra></extra>',"
                + f"visible:false,connectgaps:false"
                + "}"
            )
        mat_traces_js = "[" + ",".join(mat_traces_parts) + "]"
        n_mat_traces = len(mat_traces_parts)

        # ── Solid mesh surface (mesh3d) — connects adjacent layers ────────────
        # Creates a solid part view with temperature color mapped to the surface.
        mesh_verts_x, mesh_verts_y, mesh_verts_z = [], [], []
        mesh_intensity = []
        mesh_i, mesh_j, mesh_k = [], [], []  # triangle face indices
        vert_offset = 0

        MAX_MESH_PTS = 150  # points per layer in solid mesh — keep total vertices low
        for li in range(len(layers) - 1):
            ln_a, ln_b = layers[li], layers[li + 1]
            pts_a = layer_data[ln_a]
            pts_b = layer_data[ln_b]

            # Resample to same length, capped at MAX_MESH_PTS
            n = min(len(pts_a), len(pts_b), MAX_MESH_PTS)
            if n < 3:
                continue
            step_a = max(1, len(pts_a) // n)
            step_b = max(1, len(pts_b) // n)
            sa = pts_a[::step_a][:n]
            sb = pts_b[::step_b][:n]

            base = vert_offset
            for p in sa:
                mesh_verts_x.append(p["x"])
                mesh_verts_y.append(p["y"])
                mesh_verts_z.append(p["z"])
                mesh_intensity.append(p.get("temp_C_residual", p["temp_C"]))
            for p in sb:
                mesh_verts_x.append(p["x"])
                mesh_verts_y.append(p["y"])
                mesh_verts_z.append(p["z"])
                mesh_intensity.append(p.get("temp_C_residual", p["temp_C"]))

            # Create triangles: quad(i, i+1, i+n, i+n+1) → 2 triangles
            for j in range(n - 1):
                a0 = base + j
                a1 = base + j + 1
                b0 = base + n + j
                b1 = base + n + j + 1
                mesh_i.extend([a0, a0])
                mesh_j.extend([a1, b0])
                mesh_k.extend([b0, b1])

            # Close the loop (connect last point to first)
            a0 = base + n - 1
            a1 = base
            b0 = base + n + n - 1
            b1 = base + n
            mesh_i.extend([a0, a0])
            mesh_j.extend([a1, b0])
            mesh_k.extend([b0, b1])

            vert_offset += 2 * n

        mesh_trace = (
            f"{{type:'mesh3d',"
            f"x:{mesh_verts_x},y:{mesh_verts_y},z:{mesh_verts_z},"
            f"i:{mesh_i},j:{mesh_j},k:{mesh_k},"
            f"intensity:{mesh_intensity},"
            f"colorscale:CS,cmin:{cmin_phys},cmax:{cmax_phys},"
            f"colorbar:{{title:'°C',tickfont:{{color:'#333'}},titlefont:{{color:'#333'}}}},"
            f"opacity:0.85,name:'Solid View',visible:false,"
            f"hovertemplate:'Temp: %{{intensity:.0f}} °C<extra></extra>'}}"
        )

        # ── Seam start/end markers with thermal & overlap data ────────────
        start_xs, start_ys, start_zs, start_labels, start_colors = [], [], [], [], []
        stop_xs,  stop_ys,  stop_zs,  stop_labels               = [], [], [], []

        for ln in layers:
            pts = layer_data[ln]
            if not pts:
                continue
            sp = pts[0]   # seam start
            ep = pts[-1]  # seam end

            sp_tc   = sp.get("temp_C_residual", sp["temp_C"])
            ep_tc   = ep.get("temp_C_residual", ep["temp_C"])
            gap     = sp.get("seam_gap_mm", 0.0)
            overlap = sp.get("seam_overlap_energy", 0.0)

            # Color seam start by overlap risk: red if high temp, green if low
            sp_risk_color = "#ff3344" if sp_tc > 0.7 * T_melt_c else "#ffaa00" if sp_tc > 0.5 * T_melt_c else "#00ff88"

            start_xs.append(sp["x"]); start_ys.append(sp["y"]); start_zs.append(sp["z"])
            start_colors.append(sp_risk_color)
            start_labels.append(
                f"<b>SEAM START — Layer {ln}</b><br>"
                f"Temp: {sp_tc:.0f}°C | Speed: {sp['speed']} mm/s<br>"
                f"Gap from prev layer: {gap:.2f} mm<br>"
                f"Overlap energy: {overlap:.1f} J/mm<br>"
                f"Material: {sp['material']}"
            )

            stop_xs.append(ep["x"]); stop_ys.append(ep["y"]); stop_zs.append(ep["z"])
            stop_labels.append(
                f"<b>SEAM END — Layer {ln}</b><br>"
                f"Temp: {ep_tc:.0f}°C | Speed: {ep['speed']} mm/s<br>"
                f"Material: {ep['material']}"
            )

        start_trace = (
            f"{{type:'scatter3d',mode:'markers',name:'Seam Start ▶',"
            f"x:{start_xs},y:{start_ys},z:{start_zs},"
            f"marker:{{symbol:'diamond',size:7,color:{json.dumps(start_colors)},opacity:1.0,"
            f"line:{{color:'white',width:1}}}},"
            f"text:{json.dumps(start_labels)},"
            f"hovertemplate:'%{{text}}<extra></extra>',visible:true}}"
        )
        stop_trace = (
            f"{{type:'scatter3d',mode:'markers',name:'Seam End ■',"
            f"x:{stop_xs},y:{stop_ys},z:{stop_zs},"
            f"marker:{{symbol:'square',size:5,color:'#cc3366',opacity:0.8,"
            f"line:{{color:'white',width:1}}}},"
            f"text:{json.dumps(stop_labels)},"
            f"hovertemplate:'%{{text}}<extra></extra>',visible:true}}"
        )

        # ── Direction arrows (cone trace — shows robot travel direction) ────
        arrow_xs, arrow_ys, arrow_zs = [], [], []
        arrow_us, arrow_vs, arrow_ws = [], [], []
        arrow_step = max(1, len(self.thermal_data) // 40)  # ~40 arrows total

        for ln in layers:
            pts = layer_data[ln]
            step = max(1, len(pts) // max(1, 40 // max(len(layers), 1)))
            for i in range(0, len(pts) - 1, max(step, 5)):
                p0, p1 = pts[i], pts[i + 1]
                dx = p1["x"] - p0["x"]
                dy = p1["y"] - p0["y"]
                dz = p1["z"] - p0["z"]
                mag = math.sqrt(dx*dx + dy*dy + dz*dz)
                if mag > 0.1:
                    arrow_xs.append(p0["x"])
                    arrow_ys.append(p0["y"])
                    arrow_zs.append(p0["z"])
                    arrow_us.append(dx / mag)
                    arrow_vs.append(dy / mag)
                    arrow_ws.append(dz / mag)

        arrow_trace = (
            f"{{type:'cone',"
            f"x:{arrow_xs},y:{arrow_ys},z:{arrow_zs},"
            f"u:{arrow_us},v:{arrow_vs},w:{arrow_ws},"
            f"sizemode:'absolute',sizeref:0.04,"
            f"colorscale:[[0,'#00d4ff'],[1,'#00d4ff']],"
            f"showscale:false,opacity:0.7,"
            f"name:'Print Direction',visible:true,"
            f"hoverinfo:'skip'}}"
        )

        # Trace order: [layers...] [start] [stop] [arrows] [mesh]
        n_layers = len(layers)
        # Trace order: [layers * n] [mat_traces * m] [start] [stop] [arrows] [mesh]
        n_l = n_layer_traces
        n_m = n_mat_traces
        tail = ["true", "true", "true", "false"]   # start, stop, arrows, mesh

        # Temperature lines view (default)
        temp_vis  = ["true"] * n_l + ["false"] * n_m + tail
        # Material view — show material traces, hide temp traces
        mat_vis   = ["false"] * n_l + ["true"] * n_m + ["true", "true", "true", "false"]
        # Solid view
        solid_vis = ["false"] * n_l + ["false"] * n_m + ["true", "true", "false", "true"]
        # Both (temp lines + solid)
        both_vis  = ["true"] * n_l + ["false"] * n_m + ["true", "true", "true", "true"]

        buttons_js = (
            "[{label:'Temperature',method:'restyle',args:[{visible:[" + ",".join(temp_vis) + "]}]},"
            "{label:'Material',method:'restyle',args:[{visible:[" + ",".join(mat_vis) + "]}]},"
            "{label:'Solid',method:'restyle',args:[{visible:[" + ",".join(solid_vis) + "]}]},"
            "{label:'Both',method:'restyle',args:[{visible:[" + ",".join(both_vis) + "]}]}]"
        )

        # ── Animation data (embedded in same page) ──────────────────────
        td = self.thermal_data
        ambient_a = float(self.user.get("ambient_temp", 25.0))
        dom_mat_a = self.db_materials.get("T0") or self.db_materials.get("T1") or {}
        T_melt_a  = dom_mat_a.get("melting_point", 1400)

        # Part-scale tau
        wxs_a = [d["x"] for d in td]; wys_a = [d["y"] for d in td]; wzs_a = [d["z"] for d in td]
        w_mm_a = self.user["layer_width"]; h_mm_a = self.user["layer_height"]
        dx_mm_a = max(wxs_a) - min(wxs_a) + w_mm_a
        dy_mm_a = max(wys_a) - min(wys_a) + w_mm_a
        dz_mm_a = max(wzs_a) - min(wzs_a) + h_mm_a
        V_part_a = (dx_mm_a * dy_mm_a * dz_mm_a * 1e-9) * 0.4
        A_surf_a = 2.0 * (dx_mm_a*dy_mm_a + dx_mm_a*dz_mm_a + dy_mm_a*dz_mm_a) * 1e-6
        rho_a = dom_mat_a.get("density", 7000); cp_a = dom_mat_a.get("specific_heat", 500)
        T_rep_K_a = 400 + 273.15
        h_rad_a = 0.8 * 5.67e-8 * (T_rep_K_a + 298.15) * (T_rep_K_a**2 + 298.15**2)
        h_eff_a = 35.0 + h_rad_a
        tau_a = (rho_a * cp_a * V_part_a) / (h_eff_a * A_surf_a) if A_surf_a > 0 else 300.0
        tau_a = max(tau_a, 60.0)

        # Downsample animation to 3000 pts — temps computed JS-side at runtime
        MAX_ANIM_3D = 3000
        n_total_a_raw = len(td)
        ds_a = max(1, n_total_a_raw // MAX_ANIM_3D)
        td_a = td[::ds_a][:MAX_ANIM_3D]
        n_total_a = len(td_a)

        anim_all_x  = json.dumps([round(d["x"],         2) for d in td_a])
        anim_all_y  = json.dumps([round(d["y"],         2) for d in td_a])
        anim_all_z  = json.dumps([round(d["z"],         2) for d in td_a])
        anim_all_te = json.dumps([round(d["t_elapsed"], 1) for d in td_a])
        anim_all_ly = json.dumps([d["layer_num"]           for d in td_a])
        anim_all_sp = json.dumps([round(d.get("speed", 0), 1) for d in td_a])

        t_total_a = round(td[-1]["t_elapsed"], 1)
        total_layers_a = max(d["layer_num"] for d in td)
        def fmt_t(s):
            m, sec = divmod(int(s), 60)
            h, m = divmod(m, 60)
            return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>3D Temperature Map — {part}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#f8f9fc;font-family:'Segoe UI',sans-serif;color:#222;height:100vh;display:flex;flex-direction:column;overflow:hidden}}
  .top-bar{{padding:8px 20px;background:#eaeff8;border-bottom:1px solid #c8d4e8;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
  .top-bar h1{{color:#1e40af;font-size:1.15rem;white-space:nowrap}}
  .stats{{display:flex;gap:10px;flex-wrap:wrap}}
  .stat{{background:#1e1e40;border:1px solid #333;border-radius:6px;padding:4px 10px;font-size:.72rem;text-align:center}}
  .stat .v{{font-size:.95rem;font-weight:700;color:#fab432}}
  .stat .l{{color:#888;font-size:.65rem}}
  /* ── Stacked panels ── */
  .panels{{flex:1;display:flex;flex-direction:column;overflow:hidden}}
  .panel{{flex:1;display:flex;flex-direction:column;min-height:0}}
  .panel-divider{{height:4px;width:100%;background:linear-gradient(90deg,#00d4ff,#fab432,#ff4444);flex-shrink:0}}
  .panel-hdr{{padding:6px 14px;background:#eaeff8;border-bottom:1px solid #c8d4e8;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
  .panel-hdr h2{{font-size:.95rem;white-space:nowrap}}
  .panel-hdr.left h2{{color:#1e40af}}
  .panel-hdr.right h2{{color:#fab432}}
  #plot{{flex:1;min-height:0}}
  #animPlot{{flex:1;min-height:0}}
  .legend{{font-size:.68rem;color:#555;line-height:1.5;padding:5px 14px;background:#eaeff8;border-top:1px solid #c8d4e8}}
  .controls{{padding:6px 14px;background:#eaeff8;border-top:1px solid #c8d4e8;display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
  .controls button{{background:#1e1e40;border:1px solid #444;color:#eee;padding:4px 12px;border-radius:5px;cursor:pointer;font-size:.8rem;transition:all .15s}}
  .controls button:hover{{background:#2a2a60}}
  .controls button.active{{background:#00d4ff;color:#000;border-color:#00d4ff}}
  .slider-wrap{{flex:1;display:flex;align-items:center;gap:8px}}
  .slider-wrap input{{flex:1;accent-color:#00d4ff}}
  .slider-wrap label{{font-size:.7rem;color:#aaa;white-space:nowrap}}
</style>
</head>
<body>
<!-- ═══ TOP BAR ═══ -->
<div class="top-bar">
  <h1>🌡️ Thermal Analysis — {part}</h1>
  <div class="stats">
    <div class="stat"><div class="v">{min(temps):.0f}–{max(temps):.0f}°C</div><div class="l">Residual Range</div></div>
    <div class="stat"><div class="v">{t_avg:.0f}°C</div><div class="l">Avg</div></div>
    <div class="stat"><div class="v">{len(layers)}</div><div class="l">Layers</div></div>
    <div class="stat"><div class="v">{len(self.thermal_data)}</div><div class="l">Waypoints</div></div>
    <div class="stat"><div class="v">{fmt_t(t_total_a)}</div><div class="l">Print Time</div></div>
  </div>
</div>

<!-- ═══ SIDE-BY-SIDE PANELS ═══ -->
<div class="panels">
  <!-- LEFT: STATIC HEATMAP -->
  <div class="panel">
    <div class="panel-hdr left">
      <h2>🌡️ Heatmap (End of Print)</h2>
    </div>
    <div id="plot"></div>
    <div class="legend">
      Blue = {cmin_phys:.0f}°C → Red = {cmax_phys:.0f}°C &nbsp;|&nbsp;
      ◆ Print head &nbsp;|&nbsp; ✦ Trail (last 50 pts) &nbsp;|&nbsp;
      Views: Temperature / Material / Solid / Both
    </div>
  </div>

  <div class="panel-divider"></div>

  <!-- RIGHT: ANIMATION -->
  <div class="panel">
    <div class="panel-hdr right">
      <h2>🎬 Print Animation</h2>
      <div class="stats">
        <div class="stat"><div class="v" id="sLayer">1</div><div class="l">Layer</div></div>
        <div class="stat"><div class="v" id="sTime">0h00m</div><div class="l">Time</div></div>
        <div class="stat"><div class="v" id="sTemp">—</div><div class="l">Melt °C</div></div>
        <div class="stat"><div class="v" id="sFrame">1/{n_total_a}</div><div class="l">Frame</div></div>
        <div class="stat"><div class="v" id="sSpeed">—</div><div class="l">mm/s</div></div>
      </div>
    </div>
    <div id="animPlot"></div>
    <div class="controls">
      <button onclick="setSpeed(1)">1x</button>
      <button onclick="setSpeed(10)">10x</button>
      <button onclick="setSpeed(50)">50x</button>
      <button onclick="setSpeed(100)" class="active">100x</button>
      <input id="customSpeed" type="number" min="1" max="9999" placeholder="×?" title="Custom speed multiplier"
        style="width:52px;padding:3px 5px;border:1px solid #c8d4e8;border-radius:4px;
               font-size:.75rem;text-align:center;background:#fff;color:#333"
        onchange="setSpeed(Math.max(1,+this.value));this.blur()"
        onkeydown="if(event.key==='Enter')setSpeed(Math.max(1,+this.value))">
      <div class="slider-wrap">
        <input type="range" id="slider" min="0" max="{n_total_a - 1}" value="0" oninput="goToFrame(+this.value)">
      </div>
    </div>
    <div class="legend" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:5px 8px">
      <b>Layer:</b>
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:.7rem">First</span>
        <div style="width:120px;height:10px;border-radius:5px;background:linear-gradient(to right,rgb(0,0,180),rgb(0,160,255),rgb(255,255,0),rgb(255,120,0),rgb(220,0,0));border:1px solid #bbb"></div>
        <span style="font-size:.7rem">Last</span>
      </div>
      &nbsp;|&nbsp; ⚪ Melt pool &nbsp;|&nbsp; τ = {tau_a:.0f}s
    </div>
  </div>
</div>

<script>
/* ═══ Shared colorscale ═══ */
const CS = [
  [0,   'rgb(0,0,180)'],
  [0.25,'rgb(0,160,255)'],
  [0.5, 'rgb(255,255,0)'],
  [0.75,'rgb(255,120,0)'],
  [1,   'rgb(220,0,0)']
];

/* ═══ STATIC HEATMAP ═══ */
const layerTraces = {traces_js};
const matTraces  = {mat_traces_js};
const startTrace = {start_trace};
const stopTrace  = {stop_trace};
const arrowTrace = {arrow_trace};
const meshTrace  = {mesh_trace};
const allTraces  = layerTraces.concat(matTraces, [startTrace, stopTrace, arrowTrace, meshTrace]);

const layout1 = {{
  paper_bgcolor: '#f8f9fc',
  scene: {{
    bgcolor: '#f0f3f9',
    xaxis: {{title:'X (mm)', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    yaxis: {{title:'Y (mm)', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    zaxis: {{title:'Z (mm)', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    camera: {{eye:{{x:1.4, y:1.4, z:0.8}}}}
  }},
  margin: {{l:0, r:20, t:40, b:0}},
  font: {{color:'#333', family:'Segoe UI,sans-serif'}},
  showlegend: false,
  updatemenus: [{{
    type: 'buttons',
    direction: 'right',
    x: 0.0, y: 1.08,
    xanchor: 'left',
    yanchor: 'top',
    buttons: {buttons_js},
    bgcolor: '#dde4f0',
    bordercolor: '#444',
    font: {{color:'#333', size:10}},
    pad: {{r:4, t:4}}
  }}]
}};

Plotly.newPlot('plot', allTraces, layout1,
  {{responsive:true, displayModeBar:true, modeBarButtonsToRemove:['toImage'], displaylogo:false}});

/* ═══ PRINT ANIMATION (JS-side temp computation) ═══ */
const CMIN2 = {ambient_a}, CMAX2 = {T_melt_a}, TAU2 = {tau_a:.1f};
const T_TOT2 = {t_total_a};
const animX  = {anim_all_x};
const animY  = {anim_all_y};
const animZ  = {anim_all_z};
const animTe = new Float32Array({anim_all_te});
const animLy = new Int32Array({anim_all_ly});
const animSp = new Float32Array({anim_all_sp});

let aNow = 0, aPlaying = false, aSpeed = 100, aTimer = null, aBusy = false, lastE = 0;

function aFindEnd(t) {{
  let lo = 0, hi = animTe.length;
  while (lo < hi) {{ const mid = (lo+hi)>>1; if (animTe[mid]<=t) lo=mid+1; else hi=mid; }}
  return Math.max(1, lo);
}}
function aRender(t) {{
  if (aBusy) return;
  aBusy = true;
  const e = aFindEnd(t);
  const ri = Math.max(0, e - 1);
  const layMax = animLy[animLy.length - 1] || 1;
  const toColor = i => Math.round(CMIN2 + (CMAX2 - CMIN2) * animLy[i] / layMax);

  /* Trail: last TRAIL_LEN points before current position */
  const ts = Math.max(0, e - TRAIL_LEN);
  const trailX = Array.from(animX.slice(ts, e));
  const trailY = Array.from(animY.slice(ts, e));
  const trailZ = Array.from(animZ.slice(ts, e));
  const n = trailX.length;
  const trailSizes  = Array.from({{length:n}}, (_,i) => 2 + 4*(i/Math.max(n-1,1)));
  const trailColors = Array.from({{length:n}}, (_,i) => {{
    const a = (0.15 + 0.85*(i/Math.max(n-1,1))).toFixed(2);
    return `rgba(255,220,0,${{a}})`;
  }});

  if (e > lastE) {{
    /* Fast path — append only the new delta of points */
    const nx = animX.slice(lastE, e);
    const ny = animY.slice(lastE, e);
    const nz = animZ.slice(lastE, e);
    const nc = nx.map((_, i) => toColor(lastE + i));
    Promise.all([
      Plotly.extendTraces('animPlot', {{x:[nx], y:[ny], z:[nz], 'line.color':[nc]}}, [0]),
      Plotly.restyle('animPlot', {{x:[trailX],y:[trailY],z:[trailZ],'marker.size':[trailSizes],'marker.color':[trailColors]}}, [1]),
      Plotly.restyle('animPlot', {{x:[[animX[ri]]], y:[[animY[ri]]], z:[[animZ[ri]]]}}, [2])
    ]).then(() => {{ aBusy = false; }});
    lastE = e;
  }} else if (e < lastE) {{
    /* Backward scrub — full restyle */
    const allC = animX.slice(0, e).map((_, i) => toColor(i));
    Promise.all([
      Plotly.restyle('animPlot', {{
        x:[animX.slice(0,e)], y:[animY.slice(0,e)], z:[animZ.slice(0,e)],
        'line.color':[allC]
      }}, [0]),
      Plotly.restyle('animPlot', {{x:[trailX],y:[trailY],z:[trailZ],'marker.size':[trailSizes],'marker.color':[trailColors]}}, [1]),
      Plotly.restyle('animPlot', {{x:[[animX[ri]]], y:[[animY[ri]]], z:[[animZ[ri]]]}}, [2])
    ]).then(() => {{ aBusy = false; }});
    lastE = e;
  }} else {{
    /* Same frame — just update head */
    Plotly.restyle('animPlot', {{x:[[animX[ri]]], y:[[animY[ri]]], z:[[animZ[ri]]]}}, [2])
      .then(() => {{ aBusy = false; }});
  }}

  document.getElementById('sLayer').textContent = animLy[ri];
  const hh = Math.floor(t/3600), mm = Math.floor((t%3600)/60);
  document.getElementById('sTime').textContent = hh + 'h' + String(mm).padStart(2,'0') + 'm';
  document.getElementById('sTemp').textContent =
    Math.round(CMIN2 + (CMAX2 - CMIN2) * animLy[ri] / layMax) + '°C';
  document.getElementById('sFrame').textContent = Math.round(t/T_TOT2*100) + '%';
  document.getElementById('sSpeed').textContent = animSp[ri].toFixed(1);
  document.getElementById('slider').value = Math.round(t/T_TOT2*(slider.max||100));
}}

const TRAIL_LEN = 50;  // number of recent points shown as fading trail
const pathTrace2 = {{
  type:'scatter3d', mode:'lines', name:'Deposited',
  x:[], y:[], z:[], scene:'scene2',
  line:{{color:[], colorscale:CS, cmin:CMIN2, cmax:CMAX2, width:8}},
  hoverinfo:'skip'
}};
const trailTrace2 = {{  // fading trail behind print head
  type:'scatter3d', mode:'lines+markers', name:'Trail',
  x:[], y:[], z:[], scene:'scene2',
  line:{{color:'rgba(255,255,100,0.6)', width:4}},
  marker:{{
    size: Array.from({{length:TRAIL_LEN}}, (_,i) => 2 + 4*(i/TRAIL_LEN)),
    color: Array.from({{length:TRAIL_LEN}}, (_,i) => `rgba(255,220,0,${{(0.15 + 0.85*(i/TRAIL_LEN)).toFixed(2)}})`)
  }},
  hoverinfo:'skip'
}};
const robotTrace2 = {{  // print head marker
  type:'scatter3d', mode:'markers', name:'Print Head',
  x:[animX[0]], y:[animY[0]], z:[animZ[0]], scene:'scene2',
  marker:{{size:14, color:'#00ffff', symbol:'diamond',
           line:{{color:'#ffffff',width:2}}, opacity:1}},
  hoverinfo:'skip'
}};
const layout2 = {{
  paper_bgcolor:'#f8f9fc',
  scene2:{{
    bgcolor:'#f0f3f9',
    xaxis:{{title:'X (mm)',color:'#555',gridcolor:'#c4cedf',showbackground:true,backgroundcolor:'#e8ecf5'}},
    yaxis:{{title:'Y (mm)',color:'#555',gridcolor:'#c4cedf',showbackground:true,backgroundcolor:'#e8ecf5'}},
    zaxis:{{title:'Z (mm)',color:'#555',gridcolor:'#c4cedf',showbackground:true,backgroundcolor:'#e8ecf5'}},
    camera:{{eye:{{x:1.4,y:1.4,z:0.8}}}}
  }},
  margin:{{l:0,r:20,t:10,b:0}},
  font:{{color:'#333',family:'Segoe UI,sans-serif'}},
  showlegend:false
}};
Plotly.newPlot('animPlot', [pathTrace2, trailTrace2, robotTrace2], layout2,
  {{responsive:true, displayModeBar:true, modeBarButtonsToRemove:['toImage'], displaylogo:false}});

const ATICK = 100;
function aTick() {{
  aNow = Math.min(aNow + aSpeed*ATICK*10, T_TOT2);
  aRender(aNow);
  if (aNow >= T_TOT2) {{ clearInterval(aTimer); aTimer=null; aPlaying=false; }}
}}
function togglePlay() {{
  aPlaying = !aPlaying;
  if (aPlaying) {{ if (aTimer) clearInterval(aTimer); aTimer = setInterval(aTick, ATICK); }}
  else {{ clearInterval(aTimer); aTimer=null; }}
}}
function setSpeed(s) {{
  aSpeed = s;
  document.querySelectorAll('.controls button').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  if (aPlaying) {{ clearInterval(aTimer); aTimer=setInterval(aTick,ATICK); }}
}}
function goToFrame(v) {{
  aNow = (+v/+(document.getElementById('slider').max||100)) * T_TOT2;
  aRender(aNow);
}}
aRender(0);
</script>
</body>
</html>"""

        out = (Path(__file__).parent / "outputs" / f"heatmap_{self.user['part_name']}_{timestamp}_3d.html").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"   ✅ 3D temperature map: {out}")
        return str(out)

    # ─── STEP 5c: 3D PRINT ANIMATION ──────────────────────────────────────

    def generate_animation_html(self, timestamp: str) -> str:
        """Generate an interactive 3D animation showing the robot printing
        sequence with live thermal behavior — melt pool moves, deposited
        material cools in real time."""
        if not self.thermal_data:
            return ""

        td = self.thermal_data
        ambient = float(self.user.get("ambient_temp", 25.0))
        dom_mat = self.db_materials.get("T0") or self.db_materials.get("T1") or {}
        T_melt  = dom_mat.get("melting_point", 1400)
        part    = self.user["part_name"]

        # Part-scale tau (reuse same calculation as residual temp)
        wxs = [d["x"] for d in td]; wys = [d["y"] for d in td]; wzs = [d["z"] for d in td]
        w_mm = self.user["layer_width"]; h_mm = self.user["layer_height"]
        dx_mm = max(wxs) - min(wxs) + w_mm
        dy_mm = max(wys) - min(wys) + w_mm
        dz_mm = max(wzs) - min(wzs) + h_mm
        V_part_m3 = (dx_mm * dy_mm * dz_mm * 1e-9) * 0.4
        A_surf_m2 = 2.0 * (dx_mm*dy_mm + dx_mm*dz_mm + dy_mm*dz_mm) * 1e-6
        rho_m = dom_mat.get("density", 7000); cp_m = dom_mat.get("specific_heat", 500)
        T_rep_K = 400 + 273.15
        h_rad_rep = 0.8 * 5.67e-8 * (T_rep_K + 298.15) * (T_rep_K**2 + 298.15**2)
        h_eff_rep = 35.0 + h_rad_rep
        tau_part = (rho_m * cp_m * V_part_m3) / (h_eff_rep * A_surf_m2) if A_surf_m2 > 0 else 300.0
        tau_part = max(tau_part, 60.0)

        # Downsample per-layer with None separators so Plotly never draws cross-layer
        # lines (without separators a global stride can connect the end of one contour
        # to the start of the next, producing a "star" artefact).
        MAX_ANIM_PTS = 15000
        n_total = len(td)

        layer_buckets: dict = defaultdict(list)
        for d in td:
            layer_buckets[d["layer_num"]].append(d)
        sorted_layers = sorted(layer_buckets.keys())
        n_layers = len(sorted_layers)
        pts_per_layer = max(1, MAX_ANIM_PTS // max(n_layers, 1))

        all_x: list = []
        all_y: list = []
        all_z: list = []
        all_te: list = []
        all_ly: list = []
        for i, ln in enumerate(sorted_layers):
            pts = layer_buckets[ln]
            step = max(1, len(pts) // pts_per_layer)
            pts_ds = pts[::step]
            if i > 0:
                all_x.append(None); all_y.append(None); all_z.append(None)
                all_te.append(None)
                all_ly.append(None)
            for p in pts_ds:
                all_x.append(round(p["x"],  2))
                all_y.append(round(p["y"],  2))
                all_z.append(round(p["z"],  2))
                all_te.append(round(p["t_elapsed"], 1))
                all_ly.append(p["layer_num"])

        n_anim = sum(1 for v in all_x if v is not None)

        t_total      = round(td[-1]["t_elapsed"], 1)
        total_layers = max(v for v in all_ly if v is not None)

        def fmt_time(s):
            m, sec = divmod(int(s), 60)
            h, m   = divmod(m, 60)
            return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Print Animation — {part}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#f8f9fc;font-family:'Segoe UI',sans-serif;color:#222;display:flex;flex-direction:column;height:100vh}}
  .hdr{{padding:10px 20px;background:#eaeff8;border-bottom:1px solid #c8d4e8;display:flex;align-items:center;gap:20px;flex-wrap:wrap}}
  .hdr h2{{color:#00d4ff;font-size:1.1rem;white-space:nowrap}}
  .stats{{display:flex;gap:14px;flex-wrap:wrap}}
  .stat{{background:#1e1e40;border:1px solid #333;border-radius:6px;padding:5px 12px;font-size:.78rem;text-align:center}}
  .stat .v{{font-size:1.1rem;font-weight:700;color:#fab432}}
  .stat .l{{color:#888;font-size:.7rem}}
  #plot{{flex:1}}
  .controls{{padding:8px 20px;background:#eaeff8;border-top:1px solid #c8d4e8;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
  .controls button{{background:#1e1e40;border:1px solid #444;color:#eee;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:.85rem;transition:background .15s}}
  .controls button:hover{{background:#2a2a60}}
  .controls button.active{{background:#00d4ff;color:#000;border-color:#00d4ff}}
  .slider-wrap{{flex:1;display:flex;align-items:center;gap:10px;min-width:200px}}
  .slider-wrap input{{flex:1;accent-color:#00d4ff}}
  .slider-wrap label{{font-size:.75rem;color:#aaa;white-space:nowrap;min-width:60px}}
  .legend{{font-size:.72rem;color:#555;line-height:1.6;padding:4px 20px;background:#eaeff8}}
</style>
</head>
<body>
<div class="hdr">
  <h2>🎬 Print Animation — {part}</h2>
  <div class="stats">
    <div class="stat"><div class="v" id="sLayer">1</div><div class="l">Layer</div></div>
    <div class="stat"><div class="v" id="sTime">0h00m</div><div class="l">Elapsed</div></div>
    <div class="stat"><div class="v" id="sTemp">—</div><div class="l">Melt Pool °C</div></div>
    <div class="stat"><div class="v" id="sPct">0%</div><div class="l">Progress</div></div>
    <div class="stat"><div class="v">{total_layers}</div><div class="l">Total Layers</div></div>
    <div class="stat"><div class="v">{fmt_time(t_total)}</div><div class="l">Total Time</div></div>
  </div>
</div>
<div id="plot"></div>
<div class="controls">
  <button onclick="restart()">⏮ Restart</button>
  <button onclick="setSpeed(1)">1×</button>
  <button onclick="setSpeed(10)">10×</button>
  <button onclick="setSpeed(50)">50×</button>
  <button onclick="setSpeed(100)" class="active">100×</button>
  <input id="customSpeed" type="number" min="1" max="9999" placeholder="×?"
    style="width:52px;padding:3px 5px;border:1px solid #c8d4e8;border-radius:4px;
           font-size:.75rem;text-align:center;background:#fff;color:#333"
    onchange="setSpeed(Math.max(1,+this.value));this.blur()"
    onkeydown="if(event.key==='Enter')setSpeed(Math.max(1,+this.value))">
  <div class="slider-wrap">
    <label id="sliderLabel">0h00m / {fmt_time(t_total)}</label>
    <input type="range" id="slider" min="0" max="1000" value="0" oninput="onSlider(this.value)">
  </div>
</div>
<div class="legend" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
  <b>Layer:</b>
  <div style="display:flex;align-items:center;gap:6px">
    <span>First</span>
    <div style="width:130px;height:10px;border-radius:5px;background:linear-gradient(to right,rgb(0,0,180),rgb(0,160,255),rgb(255,255,0),rgb(255,120,0),rgb(220,0,0));border:1px solid #bbb"></div>
    <span>Last</span>
  </div>
  &nbsp;|&nbsp; ◆ <b>Print head</b> &nbsp;|&nbsp; ✦ <b>Trail</b>
  &nbsp;|&nbsp; <b>τ =</b> {tau_part:.0f}s
  &nbsp;|&nbsp; <b>Points:</b> {n_anim:,}/{n_total:,}
</div>

<script>
/* ── Constants ─────────────────────────────────────────── */
const CS   = [[0,'rgb(0,0,180)'],[0.25,'rgb(0,160,255)'],[0.5,'rgb(255,255,0)'],[0.75,'rgb(255,120,0)'],[1,'rgb(220,0,0)']];
const CMIN = {ambient}, CMAX = {T_melt}, TAU = {tau_part:.1f};
const T_TOTAL = {t_total};

/* ── Data arrays ────────────────────────────────────────── */
const allX  = {json.dumps(all_x)};
const allY  = {json.dumps(all_y)};
const allZ  = {json.dumps(all_z)};
const allTe = {json.dumps(all_te)};   // t_elapsed per point (s), null for layer separators
const allLy = {json.dumps(all_ly)};
const N     = allX.length;

/* ── State ──────────────────────────────────────────────── */
let tNow = 0, playing = false, speed = 100, timer = null;
let lastWpEnd = 0;   // cached search boundary
let busy = false;    // guard against overlapping restyles

/* ── Compute temperatures at current time (JS-side, O(n)) ── */
function computeTemps(wp_end, t_now) {{
  const out = new Array(wp_end);
  const dT  = CMAX - CMIN;
  for (let i = 0; i < wp_end; i++) {{
    if (allTe[i] === null) {{ out[i] = null; continue; }}
    const dt = t_now - allTe[i];
    out[i] = Math.round(CMIN + dT * Math.exp(-dt / TAU));
  }}
  return out;
}}

/* ── Find how many points have been deposited by t_now ─── */
function findWpEnd(t_now) {{
  // Binary search on non-null values; nulls (layer separators) compare as <= any number
  let lo = 0, hi = N;
  while (lo < hi) {{
    const mid = (lo + hi) >> 1;
    const te  = allTe[mid];
    if (te === null || te <= t_now) lo = mid + 1; else hi = mid;
  }}
  return Math.max(1, lo);
}}

/* ── Render at given time ────────────────────────────────── */
function renderAt(t_now) {{
  if (busy) return;
  busy = true;
  const wp_end  = findWpEnd(t_now);
  const temps   = computeTemps(wp_end, t_now);
  // Find last non-null index for HUD values
  let lastReal = wp_end - 1;
  while (lastReal > 0 && allTe[lastReal] === null) lastReal--;
  const curLy   = allLy[lastReal];
  const meltT   = temps[lastReal];

  const ts = Math.max(0, lastReal - TRAIL);
  const trailX = Array.from(allX.slice(ts, lastReal + 1));
  const trailY = Array.from(allY.slice(ts, lastReal + 1));
  const trailZ = Array.from(allZ.slice(ts, lastReal + 1));
  const n = trailX.length;
  const trailSizes  = Array.from({{length:n}}, (_,i) => 2 + 5*(i/Math.max(n-1,1)));
  const trailColors = Array.from({{length:n}}, (_,i) => {{
    const a = (0.1 + 0.9*(i/Math.max(n-1,1))).toFixed(2);
    return `rgba(255,220,0,${{a}})`;
  }});

  Promise.all([
    Plotly.restyle('plot', {{
      x: [allX.slice(0, wp_end)],
      y: [allY.slice(0, wp_end)],
      z: [allZ.slice(0, wp_end)],
      'line.color': [temps]
    }}, [0]),
    Plotly.restyle('plot', {{
      x: [trailX], y: [trailY], z: [trailZ],
      'marker.size': [trailSizes], 'marker.color': [trailColors]
    }}, [1]),
    Plotly.restyle('plot', {{
      x: [[allX[lastReal]]], y: [[allY[lastReal]]], z: [[allZ[lastReal]]]
    }}, [2])
  ]).then(() => {{ busy = false; }});

  /* Update HUD */
  document.getElementById('sLayer').textContent = curLy;
  document.getElementById('sTemp').textContent  = meltT + '°C';
  const pct = Math.round(t_now / T_TOTAL * 100);
  document.getElementById('sPct').textContent   = pct + '%';
  const timeStr = fmtT(t_now);
  document.getElementById('sTime').textContent  = timeStr;
  document.getElementById('sliderLabel').textContent = timeStr + ' / ' + fmtT(T_TOTAL);
  document.getElementById('slider').value = Math.round(t_now / T_TOTAL * 1000);
}}

function fmtT(s) {{
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  const h = Math.floor(m / 60), mm = m % 60;
  return h ? h + 'h ' + String(mm).padStart(2,'0') + 'm ' + String(sec).padStart(2,'0') + 's'
           : mm + 'm ' + String(sec).padStart(2,'0') + 's';
}}

/* ── Plotly init ─────────────────────────────────────────── */
const TRAIL = 50;
const pathTrace = {{
  type:'scatter3d', mode:'lines', name:'Deposited',
  x:[], y:[], z:[],
  line:{{color:[], colorscale:CS, cmin:CMIN, cmax:CMAX, width:8}},
  hoverinfo:'skip'
}};
const trailTrace = {{
  type:'scatter3d', mode:'lines+markers', name:'Trail',
  x:[], y:[], z:[],
  line:{{color:'rgba(255,255,100,0.6)', width:4}},
  marker:{{
    size: Array.from({{length:TRAIL}}, (_,i) => 2 + 5*(i/TRAIL)),
    color: Array.from({{length:TRAIL}}, (_,i) => `rgba(255,220,0,${{(0.1 + 0.9*(i/TRAIL)).toFixed(2)}})`),
  }},
  hoverinfo:'skip'
}};
const robotTrace = {{
  type:'scatter3d', mode:'markers', name:'Print Head',
  x:[allX[0]], y:[allY[0]], z:[allZ[0]],
  marker:{{size:14, color:'#00ffff', symbol:'diamond',
           line:{{color:'#ffffff', width:2}}, opacity:1}},
  hoverinfo:'skip'
}};

const layout = {{
  paper_bgcolor:'#f8f9fc',
  scene:{{
    bgcolor:'#f0f3f9',
    xaxis:{{title:'X (mm)', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    yaxis:{{title:'Y (mm)', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    zaxis:{{title:'Z (mm)', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    camera:{{eye:{{x:1.5, y:1.5, z:0.9}}}}
  }},
  margin:{{l:0, r:20, t:10, b:0}},
  font:{{color:'#333', family:'Segoe UI,sans-serif'}},
  showlegend:false
}};

Plotly.newPlot('plot', [pathTrace, trailTrace, robotTrace], layout,
  {{responsive:true, displayModeBar:true, modeBarButtonsToRemove:['toImage'], displaylogo:false}});

/* ── Playback controls ───────────────────────────────────── */
const TICK_MS = 100;   // render interval in ms

function tick() {{
  tNow = Math.min(tNow + speed * TICK_MS * 10, T_TOTAL);
  renderAt(tNow);
  if (tNow >= T_TOTAL) {{ stopPlay(); }}
}}

function startPlay() {{
  if (timer) clearInterval(timer);
  timer = setInterval(tick, TICK_MS);
  playing = true;
}}

function stopPlay() {{
  clearInterval(timer); timer = null;
  playing = false;
}}

function togglePlay() {{
  if (playing) stopPlay(); else startPlay();
}}

function restart() {{
  stopPlay();
  tNow = 0;
  renderAt(0);
}}

function setSpeed(s) {{
  speed = s;
  document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  if (playing) {{ clearInterval(timer); timer = setInterval(tick, TICK_MS); }}
}}

function onSlider(v) {{
  stopPlay();
  tNow = (+v / 1000) * T_TOTAL;
  renderAt(tNow);
}}

renderAt(0);
</script>
</body>
</html>"""

        out = (Path(__file__).parent / "outputs" / f"animation_{self.user['part_name']}_{timestamp}.html").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"   ✅ 3D print animation: {out}")
        return str(out)

    # ─── STEP 5c2: MESH OVERLAY (STL coloured by temp / stress) ───────────────

    def generate_mesh_overlay_html(self, stl_bytes: bytes, timestamp: str,
                                   stress_result: dict | None = None) -> str:
        """Colour the uploaded STL mesh by nearest-waypoint temperature and
        (optionally) ISM stress ratio.  Returns path to HTML or '' on failure."""
        if not self.thermal_data or not stl_bytes:
            return ""

        import struct, numpy as np

        # ── Parse binary STL ──────────────────────────────────────────────────
        try:
            n_tri = struct.unpack_from("<I", stl_bytes, 80)[0]
            expected = 84 + n_tri * 50
            if len(stl_bytes) < expected:
                return ""

            # Raw vertex soup: shape (n_tri*3, 3)
            raw_v = np.zeros((n_tri * 3, 3), dtype=np.float32)
            for i in range(n_tri):
                base = 84 + i * 50 + 12          # skip normal (12 bytes)
                for j in range(3):
                    raw_v[i * 3 + j] = struct.unpack_from("<fff", stl_bytes, base + j * 12)

            # Build unique vertex list + index array
            v_rounded = np.round(raw_v, 4)
            _, inv, counts = np.unique(v_rounded, axis=0,
                                       return_inverse=True, return_counts=True)
            unique_v = np.zeros((counts.shape[0], 3), dtype=np.float32)
            # recompute unique coords via averaging (handles floating-point near-dups)
            np.add.at(unique_v, inv, raw_v)
            unique_v /= counts[:, None]

            n_v = unique_v.shape[0]
            tri_idx = inv.reshape(n_tri, 3)        # (n_tri, 3) index into unique_v

        except Exception:
            return ""

        # ── Nearest-waypoint mapping ──────────────────────────────────────────
        # Thermal waypoints: already deposition-only
        td = self.thermal_data

        wp_xyz  = np.array([[d["x"], d["y"], d["z"]] for d in td], dtype=np.float32)
        wp_temp = np.array([d.get("temp_C_residual", d["temp_C"]) for d in td], dtype=np.float32)

        # Stress per waypoint (ratio vs yield, 0-1+)
        have_stress = False
        wp_stress: np.ndarray | None = None
        if stress_result and stress_result.get("ok") and stress_result.get("stress_wps"):
            swps = stress_result["stress_wps"]
            # stress_wps are per-layer; broadcast to waypoints by layer_num
            layer_stress: dict[int, float] = {
                s["layer"]: s.get("sigma_ratio", 0.0) for s in swps
            }
            wp_stress = np.array(
                [layer_stress.get(d["layer_num"], 0.0) for d in td], dtype=np.float32
            )
            have_stress = True

        # Auto-detect coordinate offset between STL and waypoints
        # (STL may be in absolute machine coords, waypoints centred on part origin)
        stl_centre  = (unique_v.max(axis=0) + unique_v.min(axis=0)) / 2
        wp_centre   = (wp_xyz.max(axis=0)   + wp_xyz.min(axis=0))   / 2
        offset      = stl_centre - wp_centre          # add to wp_xyz to align
        wp_xyz_aln  = wp_xyz + offset

        # Subsample waypoints to keep NN search fast (cap at 4000 for O(n_v*4000) max)
        MAX_WP = 4000
        n_wp = len(wp_xyz_aln)
        if n_wp > MAX_WP:
            step = n_wp // MAX_WP
            wp_xyz_aln = wp_xyz_aln[::step]
            wp_temp    = wp_temp[::step]
            if have_stress and wp_stress is not None:
                wp_stress = wp_stress[::step]

        # Chunk-based nearest-neighbour (no scipy) — O(n_v * n_wp / chunk)
        CHUNK = 500
        v_temp   = np.zeros(n_v, dtype=np.float32)
        v_stress = np.zeros(n_v, dtype=np.float32)
        for start in range(0, n_v, CHUNK):
            end   = min(start + CHUNK, n_v)
            vchk  = unique_v[start:end]               # (chunk, 3)
            # distances² to all waypoints
            diff  = vchk[:, None, :] - wp_xyz_aln[None, :, :]   # (chunk, n_wp, 3)
            d2    = (diff ** 2).sum(axis=2)                       # (chunk, n_wp)
            nn    = d2.argmin(axis=1)                             # (chunk,)
            v_temp[start:end]   = wp_temp[nn]
            if have_stress and wp_stress is not None:
                v_stress[start:end] = wp_stress[nn]

        # ── Build Plotly mesh3d JSON ──────────────────────────────────────────
        vx = unique_v[:, 0].tolist()
        vy = unique_v[:, 1].tolist()
        vz = unique_v[:, 2].tolist()
        ti = tri_idx[:, 0].tolist()
        tj = tri_idx[:, 1].tolist()
        tk = tri_idx[:, 2].tolist()

        t_vals = v_temp.tolist()
        s_vals = v_stress.tolist() if have_stress else []

        ambient  = float(self.user.get("ambient_temp", 25.0))
        dom_mat  = self.db_materials.get("T0") or self.db_materials.get("T1") or {}
        T_melt   = dom_mat.get("melting_point", 1400)
        part     = self.user["part_name"]

        stress_js = json.dumps(s_vals) if have_stress else "[]"
        have_stress_js = "true" if have_stress else "false"

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Mesh Overlay — {part}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#f8f9fc;font-family:'Segoe UI',sans-serif;color:#222;
       display:flex;flex-direction:column;height:100vh}}
  .hdr{{padding:10px 20px;background:#eaeff8;border-bottom:1px solid #c8d4e8;
        display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
  .hdr h2{{color:#2563eb;font-size:1.05rem;font-weight:700}}
  .btn-group{{display:flex;gap:6px}}
  .btn{{padding:5px 14px;border-radius:5px;border:1px solid #c8d4e8;background:#fff;
        cursor:pointer;font-size:.8rem;font-weight:500;transition:all .15s}}
  .btn.active{{background:#2563eb;color:#fff;border-color:#2563eb}}
  .legend-bar{{display:flex;align-items:center;gap:8px;font-size:.72rem;color:#555}}
  .grad{{width:120px;height:10px;border-radius:4px;border:1px solid #bbb}}
  #plot{{flex:1}}
</style>
</head>
<body>
<div class="hdr">
  <h2>Mesh Overlay — {part}</h2>
  <div class="btn-group">
    <button class="btn active" id="btnTemp" onclick="show('temp')">Temperature</button>
    <button class="btn" id="btnStress" onclick="show('stress')"
      {'style="opacity:.35;cursor:default"' if not have_stress else ''}>Stress Ratio</button>
  </div>
  <div class="legend-bar" id="legendTemp">
    <span id="lblMin">{ambient:.0f} °C</span>
    <div class="grad" style="background:linear-gradient(to right,
      rgb(0,0,180),rgb(0,160,255),rgb(255,255,0),rgb(255,120,0),rgb(220,0,0))"></div>
    <span id="lblMax">{T_melt:.0f} °C</span>
  </div>
  <div class="legend-bar" id="legendStress" style="display:none">
    <span>0.0</span>
    <div class="grad" style="background:linear-gradient(to right,#1a9641,#a6d96a,#ffffbf,#fdae61,#d7191c)"></div>
    <span>≥1.0 (yield)</span>
  </div>
</div>
<div id="plot"></div>
<script>
const VX = {json.dumps(vx)};
const VY = {json.dumps(vy)};
const VZ = {json.dumps(vz)};
const TI = {json.dumps(ti)};
const TJ = {json.dumps(tj)};
const TK = {json.dumps(tk)};
const TEMP_VALS   = {json.dumps(t_vals)};
const STRESS_VALS = {stress_js};
const HAVE_STRESS = {have_stress_js};
const T_MIN = {ambient}, T_MAX = {T_melt};

const CS_TEMP = [
  [0,'rgb(0,0,180)'],[0.25,'rgb(0,160,255)'],
  [0.5,'rgb(255,255,0)'],[0.75,'rgb(255,120,0)'],[1,'rgb(220,0,0)']
];
const CS_STRESS = [
  [0,'rgb(26,150,65)'],[0.4,'rgb(166,217,106)'],
  [0.5,'rgb(255,255,191)'],[0.75,'rgb(253,174,97)'],[1,'rgb(215,25,28)']
];

let currentMode = 'temp';

const trace = {{
  type:'mesh3d',
  x:VX, y:VY, z:VZ,
  i:TI, j:TJ, k:TK,
  intensity: TEMP_VALS,
  colorscale: CS_TEMP,
  cmin: T_MIN, cmax: T_MAX,
  showscale: false,
  flatshading: false,
  lighting:{{ambient:0.6, diffuse:0.8, specular:0.2, roughness:0.5}},
  lightposition:{{x:1, y:1, z:2}},
  hovertemplate: '%{{intensity:.0f}}<extra></extra>'
}};

const layout = {{
  paper_bgcolor:'#f8f9fc',
  scene:{{
    bgcolor:'#f0f3f9',
    xaxis:{{title:'X (mm)',color:'#555',gridcolor:'#c4cedf',showbackground:true,backgroundcolor:'#e8ecf5'}},
    yaxis:{{title:'Y (mm)',color:'#555',gridcolor:'#c4cedf',showbackground:true,backgroundcolor:'#e8ecf5'}},
    zaxis:{{title:'Z (mm)',color:'#555',gridcolor:'#c4cedf',showbackground:true,backgroundcolor:'#e8ecf5'}},
    aspectmode:'data',
    camera:{{eye:{{x:1.4,y:1.4,z:0.8}}}}
  }},
  margin:{{l:0,r:0,t:0,b:0}},
  font:{{color:'#333',family:'Segoe UI,sans-serif'}}
}};

Plotly.newPlot('plot', [trace], layout,
  {{responsive:true, displayModeBar:true, displaylogo:false}});

function show(mode) {{
  if (mode === 'stress' && !HAVE_STRESS) return;
  currentMode = mode;
  document.getElementById('btnTemp').classList.toggle('active',   mode==='temp');
  document.getElementById('btnStress').classList.toggle('active', mode==='stress');
  document.getElementById('legendTemp').style.display   = mode==='temp'   ? 'flex' : 'none';
  document.getElementById('legendStress').style.display = mode==='stress' ? 'flex' : 'none';

  if (mode === 'temp') {{
    Plotly.restyle('plot', {{
      intensity: [TEMP_VALS], colorscale: [CS_TEMP],
      cmin: T_MIN, cmax: T_MAX,
      hovertemplate: ['%{{intensity:.0f}} °C<extra></extra>']
    }});
  }} else {{
    Plotly.restyle('plot', {{
      intensity: [STRESS_VALS], colorscale: [CS_STRESS],
      cmin: 0, cmax: 1,
      hovertemplate: ['σ/σ_y = %{{intensity:.2f}}<extra></extra>']
    }});
  }}
}}
</script>
</body>
</html>"""

        out = (Path(__file__).parent / "outputs" /
               f"mesh_overlay_{part}_{timestamp}.html").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"   ✅ Mesh overlay: {out}")
        return str(out)

    # ─── STEP 5d: DISTORTION ANIMATIONS ────────────────────────────────────────

    def generate_distortion_animation_html(self, stress_result: dict, timestamp: str) -> str:
        """Three-panel distortion animation:
          A — Distortion growth chart (δ_mm vs layer)
          B — 3D stress-ratio heatmap (layer-by-layer reveal)
          C — Displacement animation (nominal vs displaced, magnified)
        All panels sync to a shared 'current layer' slider.
        """
        if not stress_result or not stress_result.get('ok'):
            return ""

        per_layer  = stress_result.get('per_layer',  [])
        stress_wps = stress_result.get('stress_wps', [])
        summary    = stress_result.get('summary',    {})
        if not per_layer or not stress_wps:
            return ""

        yield_MPa = summary.get('yield_MPa', 400)
        go_nogo   = stress_result.get('go_nogo', 'CAUTION')
        max_delta = summary.get('max_delta_mm', 0)
        max_sigma = summary.get('max_sigma_MPa', 0)
        util_pct  = summary.get('utilization_pct', 0)
        part      = self.user["part_name"]

        # Sort wps by layer; downsample to 2000 for JS speed
        wps = sorted(stress_wps, key=lambda w: w['layer'])
        if len(wps) > 2000:
            step = max(1, len(wps) // 2000)
            wps  = wps[::step][:2000]

        # JS-ready arrays
        wp_x     = json.dumps([round(w['x'],      2) for w in wps])
        wp_y     = json.dumps([round(w['y'],      2) for w in wps])
        wp_z     = json.dumps([round(w['z'],      2) for w in wps])
        wp_dx    = json.dumps([round(w['disp_x'], 3) for w in wps])
        wp_dy    = json.dumps([round(w['disp_y'], 3) for w in wps])
        wp_ratio = json.dumps([round(w['ratio'],  3) for w in wps])
        wp_sigma = json.dumps([round(w.get('sigma_MPa', 0), 1) for w in wps])
        wp_layer = json.dumps([w['layer']              for w in wps])

        pl_layers = json.dumps([p['layer']      for p in per_layer])
        pl_h      = json.dumps([p['height_mm']  for p in per_layer])
        pl_delta  = json.dumps([p['delta_mm']   for p in per_layer])
        pl_sigma  = json.dumps([p['sigma_MPa']  for p in per_layer])
        pl_ratio  = json.dumps([p['ratio']      for p in per_layer])
        pl_risk   = json.dumps([p['risk']       for p in per_layer])

        max_layer = per_layer[-1]['layer']

        # Auto-magnification for displacement panel
        if max_delta < 0.05:    default_mag = 200
        elif max_delta < 0.2:   default_mag = 50
        elif max_delta < 1.0:   default_mag = 20
        elif max_delta < 3.0:   default_mag = 5
        else:                   default_mag = 2

        go_color = {'GO': '#16a34a', 'CAUTION': '#d97706', 'NO-GO': '#dc2626'}.get(go_nogo, '#64748b')
        go_icon  = {'GO': '✅', 'CAUTION': '⚠️', 'NO-GO': '🚫'}.get(go_nogo, '—')

        sens_sweep = stress_result.get('sens_sweep', [])
        sens_json  = json.dumps(sens_sweep)

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Distortion Analysis — {part}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#f8f9fc;font-family:'Segoe UI',sans-serif;color:#222;display:flex;flex-direction:column;height:100vh;overflow:hidden}}
.hdr{{padding:6px 16px;background:#eaeff8;border-bottom:1px solid #c8d4e8;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.hdr h2{{color:#1e40af;font-size:.95rem;white-space:nowrap}}
.badge{{padding:3px 12px;border-radius:20px;font-size:.8rem;font-weight:700;background:{go_color};color:#fff}}
.stats{{display:flex;gap:8px;flex-wrap:wrap}}
.stat{{background:#1e1e40;border:1px solid #333;border-radius:6px;padding:4px 10px;font-size:.72rem;text-align:center}}
.stat .v{{font-size:.9rem;font-weight:700;color:#fab432}}
.stat .l{{color:#888;font-size:.65rem}}
/* ── Tabs ── */
.tabs{{display:flex;gap:4px;margin-left:auto}}
.tab{{background:#1e1e40;border:1px solid #444;color:#aaa;padding:4px 14px;border-radius:5px;cursor:pointer;font-size:.8rem;transition:all .15s}}
.tab:hover{{background:#2a2a60;color:#eee}}
.tab.active{{background:#00d4ff;color:#000;border-color:#00d4ff;font-weight:700}}
/* ── 3-panel grid ── */
#animView{{flex:1;display:flex;flex-direction:column;overflow:hidden}}
.panels{{flex:1;display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:2px;background:#d8e0ec;overflow:hidden}}
.panel{{display:flex;flex-direction:column;background:#f8f9fc;min-height:0;min-width:0}}
.panel-b{{grid-row:1/3}}
.panel-hdr{{padding:4px 12px;background:#eaeff8;font-size:.72rem;color:#1e40af;font-weight:600;border-bottom:1px solid #c8d4e8;display:flex;align-items:center;gap:8px}}
.panel-hdr span{{color:#888;font-weight:400}}
.panel-body{{flex:1;min-height:0}}
/* ── Controls ── */
.ctrl{{padding:6px 16px;background:#eaeff8;border-top:1px solid #c8d4e8;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.ctrl button{{background:#1e1e40;border:1px solid #444;color:#eee;padding:4px 12px;border-radius:5px;cursor:pointer;font-size:.8rem;transition:background .15s}}
.ctrl button:hover{{background:#2a2a60}}
.ctrl button.active{{background:#00d4ff;color:#000;border-color:#00d4ff}}
.sl-wrap{{flex:1;display:flex;align-items:center;gap:8px;min-width:160px}}
.sl-wrap input{{flex:1;accent-color:#00d4ff}}
.sl-wrap label{{font-size:.72rem;color:#aaa;white-space:nowrap;min-width:100px}}
.mag-wrap{{display:flex;align-items:center;gap:6px;font-size:.72rem;color:#aaa}}
.mag-wrap select{{background:#1e1e40;border:1px solid #444;color:#eee;border-radius:4px;padding:2px 6px;font-size:.75rem}}
/* ── Sensitivity view ── */
#sensView{{flex:1;display:none;grid-template-columns:1fr;grid-template-rows:1fr 1fr;gap:2px;background:#d8e0ec;overflow:hidden}}
#sensView.visible{{display:grid}}
.sens-panel{{display:flex;flex-direction:column;background:#f8f9fc;min-height:0}}
.sens-panel-hdr{{padding:5px 14px;background:#eaeff8;font-size:.75rem;color:#1e40af;font-weight:600;border-bottom:1px solid #c8d4e8}}
.sens-body{{flex:1;min-height:0}}
</style>
</head>
<body>

<!-- ══ HEADER ══ -->
<div class="hdr">
  <h2>🔩 Distortion Analysis — {part}</h2>
  <span class="badge">{go_icon} {go_nogo}</span>
  <div class="stats">
    <div class="stat"><div class="v" id="sLayer">1 / {max_layer}</div><div class="l">Layer</div></div>
    <div class="stat"><div class="v" id="sDelta">—</div><div class="l">δ tip (mm)</div></div>
    <div class="stat"><div class="v" id="sSigma">—</div><div class="l">σ (MPa)</div></div>
    <div class="stat"><div class="v" id="sUtil">—</div><div class="l">Utilisation</div></div>
    <div class="stat"><div class="v" id="sRisk">—</div><div class="l">Risk</div></div>
    <div class="stat"><div class="v">{max_delta:.2f} mm</div><div class="l">Max δ</div></div>
    <div class="stat"><div class="v">{util_pct:.1f}%</div><div class="l">Max Util</div></div>
  </div>
  <div class="tabs">
    <button id="tab-anim"   class="tab active" onclick="showTab('anim')">🎬 Animation</button>
    <button id="tab-sens"   class="tab"        onclick="showTab('sens')">📊 Sensitivity</button>
  </div>
</div>

<!-- ══ ANIMATION VIEW ══ -->
<div id="animView">
  <div class="panels">

    <!-- B: 3D Stress Heatmap (left full height) -->
    <div class="panel panel-b">
      <div class="panel-hdr">📊 B — 3D Stress Ratio <span>(σ/σ_yield, layer by layer)</span></div>
      <div class="panel-body" id="plotB"></div>
    </div>

    <!-- A: Distortion Growth Chart (top right) -->
    <div class="panel">
      <div class="panel-hdr">📈 A — Distortion Growth <span>(tip deflection δ vs layer)</span></div>
      <div class="panel-body" id="plotA"></div>
    </div>

    <!-- C: Displacement Animation (bottom right) -->
    <div class="panel">
      <div class="panel-hdr">🔀 C — Geometry Displacement
        <span>(nominal vs displaced)</span>
        <div class="mag-wrap" style="margin-left:auto">
          Mag:
          <select id="magSel" onchange="setMag(+this.value)">
            <option value="1">1×</option>
            <option value="2">2×</option>
            <option value="5">5×</option>
            <option value="10">10×</option>
            <option value="20" {' selected' if default_mag==20 else ''}>20×</option>
            <option value="50" {' selected' if default_mag==50 else ''}>50×</option>
            <option value="100" {' selected' if default_mag==100 else ''}>100×</option>
            <option value="200" {' selected' if default_mag==200 else ''}>200×</option>
          </select>
        </div>
      </div>
      <div class="panel-body" id="plotC"></div>
    </div>

  </div>

  <!-- ══ CONTROLS ══ -->
  <div class="ctrl">
    <button id="btnPlay" onclick="togglePlay()">▶ Play</button>
    <button onclick="restart()">⏮</button>
    <button onclick="setSpeed(1)">1×</button>
    <button onclick="setSpeed(10)">10×</button>
    <button onclick="setSpeed(50)">50×</button>
    <button onclick="setSpeed(100)" class="active">100×</button>
    <input type="number" min="1" max="9999" placeholder="×?"
      style="width:52px;padding:3px 5px;border:1px solid #c8d4e8;border-radius:4px;
             font-size:.75rem;text-align:center;background:#fff;color:#333"
      onchange="setSpeed(Math.max(1,+this.value));this.blur()"
      onkeydown="if(event.key==='Enter')setSpeed(Math.max(1,+this.value))">
    <div class="sl-wrap">
      <label id="slLbl">Layer 1 / {max_layer}</label>
      <input type="range" id="slider" min="0" max="{max_layer - 1}" value="0" oninput="onSlider(this.value)">
    </div>
  </div>
</div>

<!-- ══ SENSITIVITY VIEW ══ -->
<div id="sensView">
  <!-- Left: 3D part static stress view -->
  <div class="sens-panel">
    <div class="sens-panel-hdr">📐 3D Stress Distribution — full build</div>
    <div class="sens-body" id="plotSens3d"></div>
  </div>
  <!-- Right: tornado charts (stacked) -->
  <div class="sens-panel">
    <div class="sens-panel-hdr">🌪 Deflection Sensitivity (Δδ at ±10%) — top panel | Stress Sensitivity (Δσ at ±10%) — bottom panel</div>
    <div class="sens-body" style="display:flex;flex-direction:column">
      <div id="plotSensDelta" style="flex:1;min-height:0"></div>
      <div id="plotSensSigma" style="flex:1;min-height:0"></div>
    </div>
  </div>
</div>


<script>
/* ══ Data ══ */
const WP_X     = {wp_x};
const WP_Y     = {wp_y};
const WP_Z     = {wp_z};
const WP_DX    = {wp_dx};
const WP_DY    = {wp_dy};
const WP_RATIO = {wp_ratio};
const WP_SIGMA = {wp_sigma};
const WP_LAYER = new Int32Array({wp_layer});

const PL_LAYERS = {pl_layers};
const PL_H      = {pl_h};
const PL_DELTA  = {pl_delta};
const PL_SIGMA  = {pl_sigma};
const PL_RATIO  = {pl_ratio};
const PL_RISK   = {pl_risk};

const MAX_LAYER = {max_layer};
const YIELD_MPA = {yield_MPa};
const N_WPS     = WP_X.length;

/* ══ Colorscale: blue→green→yellow→red by ratio ══ */
const CS_STRESS = [
  [0,    'rgb(30,100,200)'],
  [0.4,  'rgb(0,200,120)'],
  [0.7,  'rgb(255,200,0)'],
  [0.85, 'rgb(255,80,0)'],
  [1,    'rgb(220,0,0)']
];

/* ══ State ══ */
let curLayer = 1, playing = false, speed = 100, timer = null, mag = {default_mag};

/* ══ Helpers ══ */
function riskColor(r) {{
  if (r >= 0.85) return '#dc2626';
  if (r >= 0.5)  return '#d97706';
  return '#16a34a';
}}

// Binary search: first index where WP_LAYER > layer
function wpEndIdx(layer) {{
  let lo = 0, hi = N_WPS;
  while (lo < hi) {{ const m = (lo+hi)>>1; if (WP_LAYER[m] <= layer) lo=m+1; else hi=m; }}
  return lo;
}}

// Find per_layer entry for current layer (nearest)
function getPlEntry(layer) {{
  let best = 0;
  for (let i = 0; i < PL_LAYERS.length; i++) {{
    if (PL_LAYERS[i] <= layer) best = i;
    else break;
  }}
  return best;
}}

/* ══ Panel A: Distortion Growth (2D line) ══ */
const traceA_full = {{
  type:'scatter', mode:'lines+markers', name:'δ tip (mm)',
  x: PL_H, y: PL_DELTA,
  line:{{color:'rgba(100,160,255,0.3)', width:1}},
  marker:{{
    color: PL_RATIO,
    colorscale: CS_STRESS,
    cmin:0, cmax:1,
    size:6, opacity:0.6
  }},
  hovertemplate:'Layer %{{text}}<br>Height: %{{x:.1f}} mm<br>δ: %{{y:.3f}} mm<extra></extra>',
  text: PL_LAYERS
}};
const traceA_done = {{
  type:'scatter', mode:'lines+markers', name:'Done',
  x:[], y:[],
  line:{{color:'#00d4ff', width:2.5}},
  marker:{{color:[], colorscale:CS_STRESS, cmin:0, cmax:1, size:7}},
  hoverinfo:'skip'
}};
const traceA_cursor = {{
  type:'scatter', mode:'markers', name:'Current',
  x:[], y:[],
  marker:{{color:'#ffffff', size:12, symbol:'circle', line:{{color:'#ffcc00',width:2}}}},
  hoverinfo:'skip'
}};
const layoutA = {{
  paper_bgcolor:'#f8f9fc', plot_bgcolor:'#f0f3f9',
  xaxis:{{title:'Height (mm)', color:'#555', gridcolor:'#c4cedf', zeroline:false}},
  yaxis:{{title:'Tip Deflection δ (mm)', color:'#555', gridcolor:'#c4cedf', zeroline:false}},
  margin:{{l:50,r:10,t:10,b:40}},
  font:{{color:'#333', family:'Segoe UI,sans-serif'}},
  showlegend:false,
  shapes:[{{  // yield line at max_sigma/yield = utilisation
    type:'line', x0:0, x1:1, xref:'paper',
    y0:{max_delta:.3f}, y1:{max_delta:.3f},
    line:{{color:'rgba(220,0,0,0.4)', width:1, dash:'dot'}}
  }}]
}};
Plotly.newPlot('plotA', [traceA_full, traceA_done, traceA_cursor], layoutA,
  {{responsive:true, displayModeBar:false}});

/* ══ Panel B: 3D Stress Heatmap ══ */
const traceBall = {{  // full toolpath (faded)
  type:'scatter3d', mode:'markers', name:'Full path',
  x: WP_X, y: WP_Y, z: WP_Z,
  marker:{{color:'rgba(60,80,120,0.15)', size:2}},
  hoverinfo:'skip'
}};
const traceBvis = {{  // revealed portion
  type:'scatter3d', mode:'markers', name:'σ/σ_yield',
  x:[], y:[], z:[],
  marker:{{
    color:[], colorscale:CS_STRESS, cmin:0, cmax:1,
    size:4, opacity:0.85,
    colorbar:{{title:'σ/σ_y', len:0.6, thickness:10,
              tickfont:{{color:'#333',size:9}}, titlefont:{{color:'#333',size:10}}}}
  }},
  hovertemplate:'(%{{x:.1f}}, %{{y:.1f}}, %{{z:.1f}})<br>σ/σ_y = %{{marker.color:.2f}}<extra></extra>'
}};
const layoutB = {{
  paper_bgcolor:'#f8f9fc',
  scene:{{
    bgcolor:'#f0f3f9',
    xaxis:{{title:'X', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    yaxis:{{title:'Y', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    zaxis:{{title:'Z', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    camera:{{eye:{{x:1.5,y:1.5,z:0.8}}}}
  }},
  margin:{{l:0,r:0,t:0,b:0}},
  showlegend:false
}};
Plotly.newPlot('plotB', [traceBall, traceBvis], layoutB,
  {{responsive:true, displayModeBar:true, modeBarButtonsToRemove:['toImage'], displaylogo:false}});

/* ══ Panel C: Displacement Animation ══ */
const traceCnom = {{  // nominal positions
  type:'scatter3d', mode:'markers', name:'Nominal',
  x: WP_X, y: WP_Y, z: WP_Z,
  marker:{{color:'rgba(100,140,200,0.25)', size:3}},
  hoverinfo:'skip'
}};
const traceCdisp = {{  // displaced (current layer, magnified)
  type:'scatter3d', mode:'markers', name:'Displaced',
  x:[], y:[], z:[],
  marker:{{
    color:[], colorscale:CS_STRESS, cmin:0, cmax:1,
    size:4, opacity:0.9
  }},
  hovertemplate:'Δx=%{{customdata[0]:.3f}}mm Δy=%{{customdata[1]:.3f}}mm<br>σ/σ_y=%{{marker.color:.2f}}<extra></extra>',
  customdata:[]
}};
const traceCvec = {{  // displacement arrows (cone)
  type:'cone', name:'Displacement',
  x:[], y:[], z:[], u:[], v:[], w:[],
  sizemode:'absolute', sizeref:0.08,
  colorscale:CS_STRESS, cmin:0, cmax:1,
  showscale:false, opacity:0.7,
  hoverinfo:'skip'
}};
const layoutC = {{
  paper_bgcolor:'#f8f9fc',
  scene:{{
    bgcolor:'#f0f3f9',
    xaxis:{{title:'X', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    yaxis:{{title:'Y', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    zaxis:{{title:'Z', color:'#555', gridcolor:'#c4cedf', showbackground:true, backgroundcolor:'#e8ecf5'}},
    camera:{{eye:{{x:1.5,y:1.5,z:0.8}}}}
  }},
  margin:{{l:0,r:0,t:0,b:0}},
  showlegend:false
}};
Plotly.newPlot('plotC', [traceCnom, traceCdisp, traceCvec], layoutC,
  {{responsive:true, displayModeBar:true, modeBarButtonsToRemove:['toImage'], displaylogo:false}});

/* ══ RENDER function — updates all 3 panels ══ */
let busy = false;
function renderLayer(layer) {{
  if (busy) return;
  busy = true;

  const e   = wpEndIdx(layer);
  const pli = getPlEntry(layer);
  const pl  = {{ layer:PL_LAYERS[pli], h:PL_H[pli], delta:PL_DELTA[pli], sigma:PL_SIGMA[pli], ratio:PL_RATIO[pli], risk:PL_RISK[pli] }};

  /* — Panel A update — */
  const doneX = PL_H.slice(0, pli+1);
  const doneY = PL_DELTA.slice(0, pli+1);
  const doneR = PL_RATIO.slice(0, pli+1);
  Plotly.restyle('plotA', {{
    x: [doneX, [pl.h]],
    y: [doneY, [pl.delta]],
    'marker.color': [doneR, null]
  }}, [1, 2]);

  /* — Panel B update — */
  const bx = WP_X.slice(0,e), by = WP_Y.slice(0,e), bz = WP_Z.slice(0,e);
  const br = WP_RATIO.slice(0,e);
  Plotly.restyle('plotB', {{x:[bx], y:[by], z:[bz], 'marker.color':[br]}}, [1]);

  /* — Panel C update — */
  const cx  = [], cy  = [], cz  = [], cr  = [], ccd = [];
  const cvx = [], cvy = [], cvz = [], cuu = [], cvv = [], cww = [];
  const CONE_STEP = Math.max(1, Math.floor(e / 40));
  for (let i = 0; i < e; i++) {{
    const dx = WP_DX[i]*mag, dy = WP_DY[i]*mag;
    cx.push(WP_X[i]+dx); cy.push(WP_Y[i]+dy); cz.push(WP_Z[i]);
    cr.push(WP_RATIO[i]);
    ccd.push([WP_DX[i], WP_DY[i]]);
    if (i % CONE_STEP === 0 && (Math.abs(WP_DX[i]) + Math.abs(WP_DY[i])) > 0.001) {{
      cvx.push(WP_X[i]); cvy.push(WP_Y[i]); cvz.push(WP_Z[i]);
      cuu.push(WP_DX[i]*mag); cvv.push(WP_DY[i]*mag); cww.push(0);
    }}
  }}
  Promise.all([
    Plotly.restyle('plotC', {{x:[cx],y:[cy],z:[cz],'marker.color':[cr],customdata:[ccd]}}, [1]),
    Plotly.restyle('plotC', {{x:[cvx],y:[cvy],z:[cvz],u:[cuu],v:[cvv],w:[cww],'marker.color':[cr.slice(0,cvx.length)]}}, [2])
  ]).then(() => {{ busy=false; }});

  /* — HUD — */
  document.getElementById('sLayer').textContent = layer+' / {max_layer}';
  document.getElementById('sDelta').textContent = pl.delta.toFixed(3);
  document.getElementById('sSigma').textContent = pl.sigma.toFixed(0);
  document.getElementById('sUtil').textContent  = (pl.ratio*100).toFixed(1)+'%';
  document.getElementById('sRisk').textContent  = pl.risk;
  document.getElementById('sRisk').style.color  = riskColor(pl.ratio);
  document.getElementById('slLbl').textContent  = 'Layer '+layer+' / {max_layer}';
  document.getElementById('slider').value = layer - 1;
  if (!busy) busy = false;  // reset if promises resolved synchronously
}}

/* ══ Controls ══ */
const TICK_MS = 150;
function tick() {{
  curLayer = Math.min(curLayer + speed, MAX_LAYER);
  renderLayer(Math.round(curLayer));
  if (curLayer >= MAX_LAYER) stopPlay();
}}
function startPlay() {{ if(timer) clearInterval(timer); timer=setInterval(tick,TICK_MS); playing=true; document.getElementById('btnPlay').textContent='⏸ Pause'; }}
function stopPlay()  {{ clearInterval(timer);timer=null;playing=false;document.getElementById('btnPlay').textContent='▶ Play'; }}
function togglePlay() {{ playing ? stopPlay() : startPlay(); }}
function restart()    {{ stopPlay(); curLayer=1; renderLayer(1); }}
function setSpeed(s)  {{ speed=s; document.querySelectorAll('.ctrl button').forEach(b=>b.classList.remove('active')); event.target.classList.add('active'); if(playing){{clearInterval(timer);timer=setInterval(tick,TICK_MS);}} }}
function onSlider(v)  {{ stopPlay(); curLayer=+v+1; renderLayer(Math.round(curLayer)); }}
function setMag(m)    {{ mag=m; renderLayer(Math.round(curLayer)); }}

/* Initial render */
renderLayer(1);

/* ══ TAB SWITCHING ══ */
function showTab(tab) {{
  document.getElementById('animView').style.display = tab === 'anim' ? 'flex' : 'none';
  document.getElementById('sensView').classList.toggle('visible', tab === 'sens');
  ['anim','sens'].forEach(t =>
    document.getElementById('tab-'+t).classList.toggle('active', tab === t));
  if (tab === 'sens' && !sensInited) initSensitivity();
}}

/* ══ SENSITIVITY CHARTS ══ */
let sensInited = false;
const SENS_DATA = {sens_json};

function initSensitivity() {{
  sensInited = true;

  const BG  = '#f8f9fc';
  const BGP = '#f0f3f9';
  const FONT = {{color:'#222', family:'Segoe UI,sans-serif', size:11}};
  const GRID = '#c4cedf';

  /* — 3D full-build stress (same as Panel B, static) — */
  Plotly.newPlot('plotSens3d', [
    {{type:'scatter3d', mode:'markers', name:'Full path',
      x:WP_X, y:WP_Y, z:WP_Z,
      marker:{{color:WP_RATIO, colorscale:CS_STRESS, cmin:0, cmax:1,
               size:3, opacity:0.85,
               colorbar:{{title:'σ/σ_y', len:0.7, thickness:10,
                          tickfont:{{color:'#333',size:9}}, titlefont:{{color:'#333',size:10}}}}}},
      hovertemplate:'(%{{x:.1f}}, %{{y:.1f}}, %{{z:.1f}})<br>σ/σ_y=%{{marker.color:.2f}}<extra></extra>'
    }}
  ], {{
    paper_bgcolor:BG,
    scene:{{bgcolor:BGP,
      xaxis:{{title:'X', color:'#555', gridcolor:GRID, showbackground:true, backgroundcolor:'#e8ecf5'}},
      yaxis:{{title:'Y', color:'#555', gridcolor:GRID, showbackground:true, backgroundcolor:'#e8ecf5'}},
      zaxis:{{title:'Z', color:'#555', gridcolor:GRID, showbackground:true, backgroundcolor:'#e8ecf5'}},
      camera:{{eye:{{x:1.4,y:1.4,z:0.9}}}}
    }},
    margin:{{l:0,r:0,t:30,b:0}},
    title:{{text:'Full-build stress ratio (σ/σ_yield)', font:{{color:'#2255aa',size:12}}, x:0.5}},
    showlegend:false
  }}, {{responsive:true, displayModeBar:false}});

  if (!SENS_DATA || SENS_DATA.length === 0) return;

  /* — Friendly labels for each sweep parameter — */
  const LABEL_MAP = {{
    'layer_height':    '🔧 Layer Height',
    'bead_width':      '🔧 Bead Width',
    'wire_diameter':   '🔧 Wire Diameter',
    'wire_feed_speed': '🔧 Wire Feed Speed',
    'laser_power':     '🔧 Laser Power',
    'scan_speed':      '🔧 Scan Speed',
    'dwell_time':      '🔧 Dwell Time / Layer',
    'ambient_temp':    '🌡 Ambient Temperature',
    'wall_thickness':  '📐 Wall Thickness (auto)',
    'alpha_1e6':       '🧱 CTE α (material)',
    'T_melt':          '🧱 Melting Point (material)',
    'yield_MPa':       '🧱 Yield Strength (material)',
  }};

  /* — Build tornado data — */
  const params  = SENS_DATA.map(d => (LABEL_MAP[d.param] || d.param) + '  [' + d.unit + ']');
  const d_lo    = SENS_DATA.map(d => d.d_delta_lo);
  const d_hi    = SENS_DATA.map(d => d.d_delta_hi);
  const s_lo    = SENS_DATA.map(d => d.d_sigma_lo);
  const s_hi    = SENS_DATA.map(d => d.d_sigma_hi);

  /* sort by max absolute delta impact */
  const order = [...Array(params.length).keys()].sort(
    (a,b) => Math.max(Math.abs(d_hi[b]),Math.abs(d_lo[b])) - Math.max(Math.abs(d_hi[a]),Math.abs(d_lo[a]))
  );
  const pSorted  = order.map(i=>params[i]);
  const dloS     = order.map(i=>d_lo[i]);
  const dhiS     = order.map(i=>d_hi[i]);
  const sloS     = order.map(i=>s_lo[i]);
  const shiS     = order.map(i=>s_hi[i]);

  const tornadoCfg = (loArr, hiArr, title, xTitle, color1, color2) => ({{
    data:[
      {{type:'bar', name:'-10%', x:loArr, y:pSorted, orientation:'h',
        marker:{{color:color1, opacity:0.85}},
        hovertemplate:'%{{y}}<br>-10%: %{{x:+.3f}}<extra></extra>'}},
      {{type:'bar', name:'+10%', x:hiArr, y:pSorted, orientation:'h',
        marker:{{color:color2, opacity:0.85}},
        hovertemplate:'%{{y}}<br>+10%: %{{x:+.3f}}<extra></extra>'}}
    ],
    layout:{{
      title:{{text:title, font:{{color:'#2255aa',size:12}}, x:0.5}},
      paper_bgcolor:BG, plot_bgcolor:BGP,
      barmode:'overlay',
      xaxis:{{title:xTitle, color:'#555', gridcolor:GRID, zeroline:true, zerolinecolor:'#555', zerolinewidth:2}},
      yaxis:{{color:'#555', tickfont:{{size:10}}, automargin:true}},
      margin:{{l:220,r:20,t:35,b:40}},
      font:FONT, showlegend:true,
      legend:{{x:0.75, y:0.02, bgcolor:'rgba(248,250,252,0.9)', bordercolor:'#bbc8d8', borderwidth:1,
               font:{{color:'#333',size:10}}}}
    }}
  }});

  const cfgDelta = tornadoCfg(dloS, dhiS,
    'Deflection Sensitivity — Δδ (mm) at ±10%', 'Δδ (mm)',
    'rgb(59,130,246)', 'rgb(239,68,68)');
  const cfgSigma = tornadoCfg(sloS, shiS,
    'Stress Sensitivity — Δσ (MPa) at ±10%', 'Δσ (MPa)',
    'rgb(34,197,94)', 'rgb(251,146,60)');

  Plotly.newPlot('plotSensDelta', cfgDelta.data, cfgDelta.layout, {{responsive:true, displayModeBar:false}});
  Plotly.newPlot('plotSensSigma', cfgSigma.data, cfgSigma.layout, {{responsive:true, displayModeBar:false}});
}}
</script>
</body>
</html>"""

        out = (Path(__file__).parent / "outputs" / f"distortion_{self.user['part_name']}_{timestamp}.html").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"   ✅ Distortion animation: {out}")
        return str(out)

    # ─── STEP 6: GENERATE REPORT ────────────────────────────────────────────

    def _print_time_summary(self) -> dict:
        """Compute print time totals and apply inert environment offset."""
        raw_secs   = sum(self.layer_times.values())
        dwell_secs = self.user.get("min_layer_dwell", 0) * self.num_layers
        inert_secs = 2 * 3600 if self.user.get("inert_environment") else 0
        total_secs = raw_secs + dwell_secs + inert_secs

        def fmt(s):
            h, m = divmod(int(s), 3600)
            m, sec = divmod(m, 60)
            return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"

        return {
            "raw_secs":    raw_secs,
            "dwell_secs":  dwell_secs,
            "inert_secs":  inert_secs,
            "total_secs":  total_secs,
            "raw_fmt":     fmt(raw_secs),
            "total_fmt":   fmt(total_secs),
            "layer_count": len(self.layer_times),
            "per_layer_avg": round(raw_secs / len(self.layer_times), 2) if self.layer_times else 0,
        }

    # ─── ANOMALY REPORT HELPERS ─────────────────────────────────────────────

    def _anomaly_report_table(self) -> str:
        if not self.thermal_data:
            return "No thermal data available."
        td = self.thermal_data
        n = len(td)
        lof       = sum(1 for d in td if d.get("lof_risk"))
        stub      = sum(1 for d in td if d.get("stubbing_risk"))
        kh        = sum(1 for d in td if d.get("keyhole_risk"))
        oh        = sum(1 for d in td if d.get("overheat_risk"))
        ld        = sum(1 for d in td if d.get("lof_depth_risk"))
        mw_narrow = sum(1 for d in td if d.get("melt_width_ratio", 1) < 0.7)
        mw_wide   = sum(1 for d in td if d.get("melt_width_ratio", 1) > 2.0)
        avg_mw    = sum(d.get("melt_width_mm", 0) for d in td) / n
        cr_vals   = [d.get("cracking_score", 0) for d in td]
        max_cr    = max(cr_vals)
        avg_ved   = sum(d.get("VED", 0) for d in td) / n
        avg_nh    = sum(d.get("norm_H", 0) for d in td) / n

        # Ti-6Al-4V phase distribution (only if Ti material detected)
        ti_phases = [d.get("ti_phase") for d in td if d.get("ti_phase")]
        ti_phase_summary = ""
        if ti_phases:
            pc = Counter(ti_phases)
            ti_phase_summary = " | ".join(f"{ph}: {cnt}" for ph, cnt in pc.most_common())

        def risk_icon(count, warn_thresh=1, high_thresh=10):
            if count == 0:    return "✅"
            if count < high_thresh: return "⚠️"
            return "🔴"

        cr_icon = "✅" if max_cr < 0.4 else ("⚠️" if max_cr < 0.7 else "🔴")

        lines = [
            "| Anomaly Type | Count | % of WPs | Risk |",
            "|---|---|---|---|",
            f"| LOF — low VED (lack-of-fusion risk) | {lof} | {100*lof/n:.1f}% | {risk_icon(lof)} |",
            f"| Wire stubbing risk (VED < 60% of LOF min) | {stub} | {100*stub/n:.1f}% | {risk_icon(stub)} |",
            f"| Keyhole porosity (ΔH/h_s > 25, LPBF-calibrated*) | {kh} | {100*kh/n:.1f}% | {risk_icon(kh)} |",
            f"| Inter-layer overheating (T > 0.85 × T_melt) | {oh} | {100*oh/n:.1f}% | {risk_icon(oh,1,5)} |",
            f"| Melt depth insufficient (depth/h < 1.1) | {ld} | {100*ld/n:.1f}% | {risk_icon(ld)} |",
            f"| Melt pool too narrow (E-T width < 70% bead) | {mw_narrow} | {100*mw_narrow/n:.1f}% | {risk_icon(mw_narrow)} |",
            f"| Melt pool too wide (E-T width > 2× bead) | {mw_wide} | {100*mw_wide/n:.1f}% | {risk_icon(mw_wide)} |",
            f"| Max solidification cracking score | {max_cr:.3f} / 1.0 | — | {cr_icon} |",
            "",
            f"**Avg VED:** {avg_ved:.1f} J/mm³ | **Avg ΔH/h_s:** {avg_nh:.2f} | **Avg melt width (E-T):** {avg_mw:.2f} mm",
        ]
        if ti_phase_summary:
            lines.append(f"\n**Ti-6Al-4V phase prediction (from cooling rate):** {ti_phase_summary}")
            lines.append("*Thresholds: >4 500 K/s = full α′ martensite | 410–4 500 K/s = onset | 20–410 K/s = Widmanstätten | <20 K/s = lamellar*")
            lines.append("*(Ahmed & Rack 1998; Kenel et al. 2017 synchrotron)*")
        lines.append("\n*ΔH/h_s > 25 keyhole threshold validated for LPBF (spot 50–300 µm). "
                     "Wire-laser DED uses 1–3 mm spots → computed ΔH/h_s is structurally lower. "
                     "Flag is indicative only. (Weaver et al. 2022, DOI 10.1016/j.jmapro.2021.10.053)*")
        return "\n".join(lines)

    def _microstructure_report(self) -> str:
        if not self.thermal_data:
            return "No data."
        td = self.thermal_data
        g_vals  = [d.get("G", 0) for d in td if d.get("G", 0) > 0]
        r_vals  = [d.get("R", 0) for d in td if d.get("R", 0) > 0]
        cr_vals = [d.get("cooling_rate_Ks", 0) for d in td if d.get("cooling_rate_Ks", 0) > 0]
        gr_vals = [d.get("G_over_R", 0) for d in td if d.get("G_over_R", 0) > 0]
        if not gr_vals:
            return "Insufficient data for microstructure prediction."
        avg_gr   = sum(gr_vals) / len(gr_vals)
        avg_cr   = sum(cr_vals) / len(cr_vals) if cr_vals else 0
        avg_g    = sum(g_vals) / len(g_vals) if g_vals else 0
        avg_r    = sum(r_vals) / len(r_vals) if r_vals else 0
        if avg_gr > 1e8:
            structure = "**Columnar** — high G/R; elongated grains along build direction; anisotropic mechanical properties expected"
        elif avg_gr > 1e6:
            structure = "**Mixed columnar/equiaxed** — moderate G/R; transitional microstructure"
        else:
            structure = "**Equiaxed** — low G/R; fine isotropic grains; more uniform properties"
        lines = [
            f"- **Avg thermal gradient G:** {avg_g:.0f} K/m",
            f"- **Avg solidification rate R:** {avg_r*1000:.2f} mm/s",
            f"- **Avg cooling rate dT/dt:** {avg_cr:.0f} K/s",
            f"- **Avg G/R index:** {avg_gr:.2e} K·s/m²",
            f"- **Predicted grain structure:** {structure}",
        ]
        return "\n".join(lines)

    def _anomaly_recommendations(self) -> str:
        if not self.thermal_data:
            return "Run analysis to generate recommendations."
        td = self.thermal_data
        n  = len(td)
        lof   = sum(1 for d in td if d.get("lof_risk"))
        stub  = sum(1 for d in td if d.get("stubbing_risk"))
        kh    = sum(1 for d in td if d.get("keyhole_risk"))
        oh    = sum(1 for d in td if d.get("overheat_risk"))
        ld    = sum(1 for d in td if d.get("lof_depth_risk"))
        max_cr = max((d.get("cracking_score", 0) for d in td), default=0)

        recs = []
        if stub > 0:
            pct = 100 * stub / n
            recs.append(
                f"🔴 **Wire Stubbing Risk ({stub} zones, {pct:.1f}%):** VED is critically low "
                "(below 60% of LOF minimum). The wire will not melt fully and may stub against "
                "the substrate. Increase laser power significantly or reduce wire feed speed. "
                "(McLain et al. 2024, DOI 10.3390/ma17215311)"
            )
        if lof > 0:
            pct = 100 * lof / n
            recs.append(
                f"🔵 **LOF Risk ({lof} zones, {pct:.1f}%):** VED is below the material minimum process window. "
                "Consider increasing laser power, reducing travel speed, or decreasing layer height/width. "
                "Lack-of-fusion porosity will appear as irregular voids aligned with the deposition path."
            )
        if kh > 0:
            pct = 100 * kh / n
            recs.append(
                f"🔴 **Keyhole Risk ({kh} zones, {pct:.1f}%):** Normalized enthalpy ΔH/h_s > 25. "
                "Note: this threshold is validated for LPBF (small spot). For wire-laser DED with "
                "1–3 mm beam spots, ΔH/h_s is structurally lower and this flag may be conservative. "
                "If flagged: reduce laser power or increase travel speed. "
                "(Weaver et al. 2022, DOI 10.1016/j.jmapro.2021.10.053)"
            )
        if oh > 0:
            pct = 100 * oh / n
            recs.append(
                f"🟠 **Overheating ({oh} zones, {pct:.1f}%):** Local temperature exceeds 85% of melting point on re-passes. "
                "Add inter-layer dwell time or active cooling. Risk of grain coarsening and geometric distortion."
            )
        if ld > 0:
            pct = 100 * ld / n
            recs.append(
                f"🟣 **Melt Depth Insufficient ({ld} zones, {pct:.1f}%):** Estimated melt pool depth < 1.1 × layer height. "
                "Layer-to-layer bonding may be incomplete. Increase laser power or reduce print speed."
            )
        if max_cr > 0.7:
            recs.append(
                f"⚡ **High Cracking Risk (score {max_cr:.3f}):** Combination of high cooling rate and wide solidification range. "
                "Consider pre-heating the substrate, reducing scan speed at boundaries, or using a lower-solidification-range alloy."
            )
        elif max_cr > 0.4:
            recs.append(
                f"⚠️  **Moderate Cracking Score ({max_cr:.3f}):** Monitor for hot-cracking especially at layer start/stop points and sharp corners."
            )

        # Interpass dwell warnings (computed during calculate_thermal_data)
        dwell_warns = getattr(self, "_dwell_warnings", [])
        if dwell_warns:
            worst = max(dwell_warns, key=lambda x: x[3])
            recs.append(
                f"⏱️ **Interpass Dwell Insufficient ({len(dwell_warns)} layers):** "
                f"Worst case: Layer {worst[0]} at {worst[1]:.0f}°C needs ~{worst[3]:.0f}s dwell "
                f"to reach {worst[2]:.0f}°C target ({worst[4]}). "
                "Increase min_layer_dwell or add active cooling between layers. "
                "Excessive interpass temperature causes grain coarsening and residual stress accumulation. "
                "(Eliseeva 2024, DOI 10.3390/ma17133307)"
            )

        if not recs:
            recs.append("✅ **All anomaly metrics within safe process window.** No immediate corrective action required.")

        return "\n\n".join(recs)

    def generate_report(self, viz_files: Dict[str, str]) -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hi_vals = [d["heat_index"] for d in self.thermal_data] or [0]
        peak = max(self.thermal_data, key=lambda d: d["heat_index"]) if self.thermal_data else {}
        low  = min(self.thermal_data, key=lambda d: d["heat_index"]) if self.thermal_data else {}
        pt   = self._print_time_summary()

        # Material change summary lines
        change_lines = []
        warning_lines = []
        for evt in self.material_changes:
            change_lines.append(f"  - Layer {evt['layer_num']}: changed to {evt['changed_to']}")
            if evt.get("warning"):
                warning_lines.append(f"  - {evt['warning']}")

        # Unique I/O signals across all layers
        all_ios = sorted(set(sig for sigs in self.digital_ios.values() for sig in sigs))

        # Material DB table rows
        mat_rows = []
        for feeder in ["T0", "T1"]:
            mat = self.db_materials.get(feeder)
            label = self.user.get(f"material_{feeder}") or "Not used"
            if mat:
                mat_rows.append(
                    f"| {feeder} — {mat['display_name']} "
                    f"| {mat['thermal_conductivity']} W/m·K "
                    f"| {mat['density']} kg/m³ "
                    f"| {mat['specific_heat']} J/kg·K "
                    f"| {mat.get('melting_point','—')} °C |"
                )
            else:
                mat_rows.append(f"| {feeder} — {label} | — | — | — | — |")

        wire_r = self.user.get("wire_diameter", 0) / 2
        V1_example = round(math.pi * wire_r**2 * self.user.get("feed_speed", 0), 4)
        # Use average print speed for V2 example
        dep_speeds = [d["speed"] for d in self.thermal_data]
        avg_speed = round(sum(dep_speeds) / len(dep_speeds), 2) if dep_speeds else 0
        V2_example = round(self.user.get("layer_width", 0) * self.user.get("layer_height", 0) * avg_speed, 4)

        report = f"""# Meltio DED Print Analysis Report

**Part Name:** {self.user['part_name']}
**Analysis Date:** {ts}
**ZIP File:** {self.zip_path}

---

## Print Configuration

### Materials
- **T0 (Feeder 0):** {self.user.get('material_T0') or 'Not detected'}
- **T1 (Feeder 1):** {self.user.get('material_T1') or 'Not detected'}

### Process Parameters
- **Laser Power:** {self.user['laser_power']} W
- **Wire Feed Speed:** {self.user['feed_speed']} mm/sec
- **Wire Diameter:** {self.user['wire_diameter']} mm
- **Layer Height:** {self.user['layer_height']} mm
- **Layer Width:** {self.user['layer_width']} mm
- **Environment:** {'🛡️ Inert (Argon/Nitrogen)' if self.user.get('inert_environment') else '🌫️ Regular (Open Air)'}

### Print Time
- **Print time (from code):** {pt['raw_fmt']} ({pt['layer_count']} layers)
- **Avg time per layer:** {pt['per_layer_avg']} sec
{f"- **Inert environment setup:** +2h 00m 00s" if self.user.get('inert_environment') else ""}
- **Total estimated time:** **{pt['total_fmt']}**

---

## Code Analysis Summary

### Layers & Structure
- **Total Layers:** {self.num_layers}
- **First Files:** {', '.join(list(self.mod_files.keys())[:4])}{'...' if self.num_layers > 4 else ''}

### Robot Motion Profile
- **Deposition Speeds:** {sorted(set(round(w['speed'],1) for w in self.waypoints if w['is_deposition'])) if self.waypoints else 'N/A'} mm/min
- **Travel Speeds:** {sorted(set(round(w['speed'],1) for w in self.waypoints if not w['is_deposition'])) if self.waypoints else 'N/A'} mm/min
- **All Detected Speeds:** {sorted(set(round(s,1) for s in self.all_speeds))} mm/min
- **Speed Range:** {min(self.all_speeds) if self.all_speeds else 'N/A'} – {max(self.all_speeds) if self.all_speeds else 'N/A'} mm/min
- **Total Deposition Waypoints:** {len(self.thermal_data)}

### Digital I/O Configuration

**Shoham ENGINE Communication Protocol:**

| Output | Function | Input | Function |
|---|---|---|---|
| DO_ENGINE_02 | Start Deposition — T0 Feeder | DI_ENGINE_02 | Confirmation |
| DO_ENGINE_03 | Start Deposition — T1 Feeder | DI_ENGINE_03 | Confirmation |
| DO_ENGINE_05 | Change to T0 Feeder | DI_ENGINE_05 | Confirmation |
| DO_ENGINE_06 | Change to T1 Feeder | DI_ENGINE_06 | Confirmation |
| — | — | DI_ENGINE_04 | End Deposition / FERS |

**Materials Detected:** {', '.join(sorted(self.materials_found))}
**Total Unique I/O Signals:** {len(all_ios)}
**Signals Found in Code:** {', '.join(all_ios)}

### Print Sequence
- **Key Operations:** {', '.join(sorted(set(self.print_sequence))[:12])}

### Material Change Summary
- **Total Material Changes:** {len(self.material_changes)}
- **Changes by Layer:**
{chr(10).join(change_lines) if change_lines else '  - None detected'}
{('- **⚠️ Warnings:**' + chr(10) + chr(10).join(warning_lines)) if warning_lines else '- **✅ All material changes followed by deposition start**'}

### I/O Validation
- **Deposition Start (T0):** DO_ENGINE_02 → DI_ENGINE_02
- **Deposition Start (T1):** DO_ENGINE_03 → DI_ENGINE_03
- **Material Changes:** DO_ENGINE_05 (T0) → DI_ENGINE_05 | DO_ENGINE_06 (T1) → DI_ENGINE_06
- **End Deposition FERS:** DI_ENGINE_04

---

## Thermal Analysis

### Process Parameters Summary
| Parameter | Value |
|---|---|
| Wire Diameter | {self.user['wire_diameter']} mm |
| Layer Height | {self.user['layer_height']} mm |
| Layer Width | {self.user['layer_width']} mm |
| Wire Feed Speed | {self.user['feed_speed']} mm/sec |
| Laser Power | {self.user['laser_power']} W |

### Material Properties Used
| Feeder | Thermal Conductivity | Density | Specific Heat | Melting Point |
|---|---|---|---|---|
{chr(10).join(mat_rows)}

### Volume Deposition Rates

**Method 1 — Wire geometry:**
```
V1 = π × (wire_diameter / 2)² × wire_feed_rate
V1 = π × ({self.user['wire_diameter']/2:.3f})² × {self.user['feed_speed']} = {V1_example} mm³/s
```

**Method 2 — Layer geometry (at average print speed {avg_speed} mm/s):**
```
V2 = layer_width × layer_height × robot_speed
V2 = {self.user['layer_width']} × {self.user['layer_height']} × {avg_speed} = {V2_example} mm³/s
```

**Heat Index formula:**
```
heat_index = avg(V1, V2) / thermal_conductivity
Higher = more heat accumulation (low conductivity + high deposition rate)
```

### Heat Map Summary
- **Peak Heat Index:** {max(hi_vals):.4f} — Layer {peak.get('layer_num','?')} at ({peak.get('x','?')}, {peak.get('y','?')}, {peak.get('z','?')})
- **Lowest Heat Index:** {min(hi_vals):.4f} — Layer {low.get('layer_num','?')} at ({low.get('x','?')}, {low.get('y','?')}, {low.get('z','?')})
- **Average Heat Index:** {sum(hi_vals)/len(hi_vals):.4f}
- **Higher Risk Material:** {'T1 ('+self.user.get('material_T1','?')+')' if (self.db_materials.get('T1') or {}).get('thermal_conductivity',99) < (self.db_materials.get('T0') or {}).get('thermal_conductivity',99) else 'T0 ('+self.user.get('material_T0','?')+')'}

### Visualization Files
- **3D Thermal Visualization:** {viz_files.get('3d','N/A')}
- **CSV Data Table:** {viz_files.get('csv','N/A')}
- **Process Window Chart:** {viz_files.get('pw','N/A')}

---

## Anomaly Prediction

### Metrics Used
| Metric | Formula | Threshold |
|---|---|---|
| Volumetric Energy Density (VED) | A×P / (v×h×w) | Material-specific LOF/keyhole window |
| Normalized Enthalpy (ΔH/h_s) | A×P / (h_s×√(π×α×v)×d^1.5) | < 6 = LOF risk · > 25 = keyhole |
| Melt pool width (Eagar-Tsai 1983) | 2σ√(1 + A·P/(π·k·σ·ΔT)) × Pe-correction | Compared to nominal bead width |
| Melt pool depth / layer height | Eagar-Tsai (semi-circular, capped by Rosenthal) | < 1.1 = LOF depth risk |
| Cooling rate (dT/dt) | G × R [K/s] | > 10⁵ K/s + wide solidif. range → cracking |
| Cracking score | (dT/dt / 10⁵) × (ΔT_solidif / 100) | 0–1 scale; > 0.7 = high risk |
| G/R ratio | Thermal gradient / solidif. rate | High → columnar; Low → equiaxed grains |

### Anomaly Summary
{self._anomaly_report_table()}

### Microstructure Prediction
{self._microstructure_report()}

### Recommendations
{self._anomaly_recommendations()}

---

## Initial Assessment

✅ **Code structure valid**
- {self.num_layers} layers detected
- {len(self.materials_found)} feeder(s) active: {', '.join(sorted(self.materials_found))}
- {len(self.thermal_data)} deposition waypoints analyzed
- {len(self.material_changes)} material change event(s)
{('- ⚠️  ' + str(len(warning_lines)) + ' material change warning(s) — review above') if warning_lines else '- ✅ All material transitions validated'}

⚠️ **Next Steps:**
1. Review heat map visualizations for thermal hotspots
2. Verify speed consistency across layers
3. Cross-check V1 vs V2 volume deposition rates
4. Adjust laser power or feed speed if heat index is uneven
5. Validate material transitions in flagged layers

---

*Generated by Meltio DED Analysis Agent v2.0*
"""
        return report

    # ─── STEP 7: SAVE ALL OUTPUTS ───────────────────────────────────────────

    def save_report(self, report: str, timestamp: str) -> str:
        out_dir = Path(__file__).parent / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        filepath = out_dir / f"meltio-analysis_{self.user['part_name']}_{timestamp}.md"
        filepath.write_text(report, encoding="utf-8")
        return str(filepath)

    # ─── MAIN RUN ───────────────────────────────────────────────────────────

    def run(self):
        print("\n" + "=" * 60)
        print("🚀 MELTIO DED CODE ANALYSIS AGENT v2.0")
        print("=" * 60)

        print("\n📂 Step 1: Reading ZIP file...")
        if not self.extract_and_read():
            return False

        print("\n🔍 Step 2: Parsing RAPID code...")
        self.parse_rapid_code()

        print("\n❓ Step 3: Gathering clarifications...")
        self.get_clarifications()

        print("\n🌡️  Step 4: Calculating thermal data...")
        self.calculate_thermal_data()

        print("\n🗺️  Step 5: Generating visualizations...")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        viz = {
            "html": self.generate_html_heatmap(ts),
            "csv":  self.generate_csv(ts),
            "svg":  self.generate_svg(ts),
            "3d":   self.generate_3d_html(ts),
        }

        print("\n📄 Step 6: Generating report...")
        report = self.generate_report(viz)

        print("\n💾 Step 7: Saving report...")
        report_path = self.save_report(report, ts)
        print(f"   ✅ Report: {report_path}")

        print("\n" + "=" * 60)
        print(report)
        print("=" * 60)
        return True


# ─── CLI ENTRY POINT ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 meltio_ded_analyzer.py <path_to_zip>")
        sys.exit(1)
    zip_path = sys.argv[1]
    if not os.path.exists(zip_path):
        print(f"❌ File not found: {zip_path}")
        sys.exit(1)
    analyzer = MeltioDEDAnalyzer(zip_path)
    sys.exit(0 if analyzer.run() else 1)


if __name__ == "__main__":
    main()
