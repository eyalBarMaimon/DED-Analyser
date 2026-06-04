#!/usr/bin/env python3
"""
Meltio DED Analyzer — Flask Web App
"""

import os
import re
import json
import math
import zipfile
import csv
import tempfile
import threading
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

BASE_DIR     = Path(__file__).parent
OUTPUT_DIR   = BASE_DIR / "outputs"
DB_PATH      = BASE_DIR / "materials_database.json"
SETTINGS_PATH = BASE_DIR / "settings.json"
HISTORY_DIR  = BASE_DIR / "history"
OUTPUT_DIR.mkdir(exist_ok=True)
HISTORY_DIR.mkdir(exist_ok=True)

def _cleanup_outputs():
    """Delete all output files from previous runs, keeping only the current run's files."""
    for f in OUTPUT_DIR.iterdir():
        if f.is_file():
            try:
                f.unlink()
            except Exception:
                pass

def load_settings():
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    return {"watch_dir": ""}

def save_settings_file(data):
    with open(SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)

# ── Import analyzer logic ────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(BASE_DIR))
from meltio_ded_analyzer import MeltioDEDAnalyzer, load_materials_db, fuzzy_match_material
from m600_gcode_parser  import M600GcodeAnalyzer
from sensor_analyzer import SensorAnalyzer
from engines.stress_engine import compute_stress, lookup_mech, estimate_wall_thickness, MECH_PROPS, run_sensitivity_sweep
from jobs.base_job import build_user_params, match_materials, run_auto_stress, build_result

_sensor        = SensorAnalyzer()
_dash_analyzer = None
_dash_sensor   = SensorAnalyzer()

# ── Directory Watcher ─────────────────────────────────────────────────────────
class DirectoryWatcher:
    """Polls a directory for a new sensors.csv, then waits for startDeposition."""
    def __init__(self):
        self.watch_dir  = None
        self.state      = "idle"   # idle|watching|file_found|printing|ended|error
        self.csv_path   = None
        self.di02_time  = None
        self._lock      = threading.Lock()
        self._stop      = threading.Event()
        self._thread    = None

    def start(self, directory):
        # Stop any running thread first
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._stop = threading.Event()
        with self._lock:
            self.watch_dir = directory
            self.state     = "watching"
            self.csv_path  = None
            self.di02_time = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def reset(self):
        self._stop.set()
        with self._lock:
            self.state     = "idle"
            self.csv_path  = None
            self.di02_time = None

    def status(self):
        with self._lock:
            return {"state": self.state, "csv_path": self.csv_path,
                    "di02_time": self.di02_time, "watch_dir": self.watch_dir}

    def _run(self):
        global _sensor
        d = self.watch_dir
        if not os.path.exists(d):
            with self._lock: self.state = "error"
            return
        known = set(os.listdir(d))
        while not self._stop.is_set():
            with self._lock: state = self.state
            try:
                if state == "watching":
                    current  = set(os.listdir(d)) if os.path.exists(d) else set()
                    new_csvs = [f for f in (current - known) if f.lower().endswith('.csv')]
                    if new_csvs:
                        fname = 'sensors.csv' if 'sensors.csv' in new_csvs else sorted(new_csvs)[-1]
                        fpath = os.path.join(d, fname)
                        ns    = SensorAnalyzer()
                        try: ns.connect(fpath)
                        except Exception: pass
                        _sensor = ns
                        with self._lock:
                            self.csv_path = fpath
                            self.state    = "file_found"
                elif state == "file_found":
                    _sensor.poll()
                    for ev in _sensor.events:
                        if ev['flag'] == 'startDeposition':
                            with self._lock:
                                self.state     = "printing"
                                self.di02_time = ev['time']
                            break
                elif state == "printing":
                    _sensor.poll()
                    for ev in _sensor.events:
                        if ev['flag'] == 'endDeposition':
                            with self._lock: self.state = "ended"
                            break
                elif state in ("ended", "error"):
                    break
            except Exception:
                pass
            self._stop.wait(2)

_watcher = DirectoryWatcher()

# ── Job Progress Tracking ─────────────────────────────────────────────────────
import uuid, re, sys, time as _time

_jobs = {}
_jobs_lock = threading.Lock()

def _new_job(kind: str) -> str:
    jid = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid, "kind": kind, "status": "queued",
            "stage": "Starting…", "pct": 0,
            "started": _time.time(), "elapsed_s": 0, "eta_s": None,
            "result": None, "error": None, "trace": None,
        }
    return jid

def _job_update(jid: str, **kw):
    with _jobs_lock:
        j = _jobs.get(jid)
        if not j:
            return
        j.update(kw)
        elapsed = _time.time() - j["started"]
        j["elapsed_s"] = elapsed
        pct = j.get("pct", 0) or 0
        if pct > 3:
            j["eta_s"] = max(0, elapsed * (100 - pct) / pct)

def _job_stage(jid: str, stage: str, pct: float):
    _job_update(jid, stage=stage, pct=pct, status="running")

def _job_get(jid: str):
    with _jobs_lock:
        return dict(_jobs.get(jid, {}))

_tee_local = threading.local()   # per-thread stdout override

class _ProgressTee:
    """Per-thread stdout proxy; parses 'Layer X/Y' lines to drive job progress.
    Uses threading.local so concurrent jobs never overwrite each other's stream."""
    def __init__(self, job_id: str, s0: float = 18, s1: float = 65):
        self.jid   = job_id
        self.s0    = s0
        self.s1    = s1
        self._buf  = ""
        self._real = sys.stdout   # capture true stdout at construction time
    def write(self, s: str):
        try: self._real.write(s)
        except Exception: pass
        self._buf += s
        if "\n" in self._buf:
            parts = self._buf.split("\n")
            for line in parts[:-1]:
                m = re.search(r"Layer (\d+)/(\d+)", line)
                if m:
                    cur, tot = int(m.group(1)), int(m.group(2))
                    pct = self.s0 + (self.s1 - self.s0) * cur / max(tot, 1)
                    _job_stage(self.jid, f"Thermal simulation · layer {cur}/{tot}", pct)
            self._buf = parts[-1]
        return len(s)
    def flush(self):
        try: self._real.flush()
        except Exception: pass

class _TeeStdout:
    """sys.stdout shim that routes writes to the current thread's _ProgressTee if set."""
    def __init__(self, real): self._real = real
    def write(self, s):
        tee = getattr(_tee_local, 'tee', None)
        return tee.write(s) if tee else self._real.write(s)
    def flush(self):
        tee = getattr(_tee_local, 'tee', None)
        (tee or self._real).flush()
    def __getattr__(self, name): return getattr(self._real, name)

sys.stdout = _TeeStdout(sys.stdout)   # install once; safe for concurrent threads

# Old jobs are cleaned up lazily — keep last 20
def _cleanup_jobs():
    with _jobs_lock:
        if len(_jobs) > 20:
            stale = sorted(_jobs.items(), key=lambda kv: kv[1]["started"])[:len(_jobs) - 20]
            for k, _ in stale:
                _jobs.pop(k, None)

# ── Routes ───────────────────────────────────────────────────────────────────

def _serve_html(filename: str):
    """Read an HTML file from BASE_DIR and return it as a Response (bypasses TCC restrictions on new files)."""
    from flask import make_response
    path = BASE_DIR / filename
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error loading {filename}: {e}", 500
    resp = make_response(content, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/")
def index():
    """Hero landing page — choose Robot or M600."""
    return _serve_html("hero.html")

@app.route("/robot")
@app.route("/ded")
@app.route("/DED")
def robot_app():
    """Robot (RAPID) analyser."""
    return _serve_html("ux.html")

@app.route("/m600")
def m600_app():
    """M600 G-code analyser."""
    return _serve_html("ux_m600.html")

@app.route("/api/materials")
def get_materials():
    db = load_materials_db()
    return jsonify(db)


def _parse_meltio_parameters(content: str) -> dict:
    """Parse Meltio Space Parameters.txt → relevant process params dict."""
    result = {}
    section = None
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        # Section header: no leading whitespace, no colon
        if not raw_line[0].isspace() and ':' not in stripped:
            section = stripped.lower()
            continue
        if ':' in stripped:
            key, _, val = stripped.partition(':')
            key_l = key.strip().lower()
            val = val.strip()
            try:
                val_f = float(val)
            except ValueError:
                val_f = None
            if key_l == 'deposition height' and val_f:
                result['layer_height_mm'] = val_f
            elif key_l == 'deposition width' and val_f:
                result['layer_width_mm'] = val_f
            elif key_l == 'base print speed' and val_f:
                result['scan_speed_mm_s'] = val_f
            elif 'wait time' in key_l and val_f and section == 'movement':
                result['min_layer_dwell_s'] = val_f
    return result


@app.route("/api/rapid/preview", methods=["POST"])
def rapid_preview():
    """
    Quick ZIP preview — reads Parameters.txt (Meltio Space) if present,
    otherwise falls back to extracting params from MoveL commands + Z deltas.
    Returns: layer_count, layer_height_mm, layer_width_mm, scan_speed_mm_s,
             min_layer_dwell_s, source ('parameters_file' | 'rapid_code').
    """
    import io, re as _re
    zf_file = request.files.get("zip_file")
    if not zf_file:
        return jsonify({"error": "No file"}), 400
    try:
        raw = zf_file.read()
        result = {"source": "rapid_code"}
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()

            # ── 1. Try Parameters.txt first (most reliable) ───────────
            param_file = next((n for n in names if n.endswith("Parameters.txt")), None)
            if param_file:
                txt = zf.read(param_file).decode("utf-8", errors="ignore")
                params = _parse_meltio_parameters(txt)
                result.update(params)
                result["source"] = "parameters_file"

            # ── 2. Count layer .mod files ─────────────────────────────
            all_mods  = sorted([f for f in names if f.endswith(".mod")])
            layer_mods = [f for f in all_mods if _re.search(r'_\d{4,}\.mod$', f)]
            if not layer_mods:
                layer_mods = [f for f in all_mods if _re.search(r'_\d+\.mod$', f)]
            result["layer_count"] = len(layer_mods)

            # ── 3. Fallback: extract from RAPID code if params missing ─
            if not result.get("layer_height_mm") or not result.get("scan_speed_mm_s"):
                move_re = _re.compile(
                    r'MoveL\s+\[\[(-?\d+\.?\d*),\s*(-?\d+\.?\d*),\s*(-?\d+\.?\d*)\]'
                    r'.*?\],\[(\d+\.?\d*),\s*\d+,\s*\d+,\s*\d+\]', _re.DOTALL)
                speeds, z_first = [], {}
                for idx, name in enumerate(layer_mods[:5]):
                    content = zf.read(name).decode("utf-8", errors="ignore")
                    layer_zs = []
                    for m in move_re.finditer(content):
                        layer_zs.append(float(m.group(3)))
                        speeds.append(float(m.group(4)))
                    if layer_zs:
                        z_first[idx] = layer_zs[0]
                if speeds and not result.get("scan_speed_mm_s"):
                    result["scan_speed_mm_s"] = round(sum(speeds) / len(speeds), 1)
                if len(z_first) >= 2 and not result.get("layer_height_mm"):
                    zs = [z_first[i] for i in sorted(z_first)]
                    diffs = [abs(zs[i+1] - zs[i]) for i in range(len(zs)-1)]
                    candidate = round(sum(diffs) / len(diffs), 2)
                    if 0.1 <= candidate <= 5.0:
                        result["layer_height_mm"] = candidate

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if "zip_file" not in request.files:
        return jsonify({"error": "No ZIP file provided"}), 400

    f = request.files["zip_file"]
    if not f.filename.endswith(".zip"):
        return jsonify({"error": "File must be a .zip"}), 400

    # Save ZIP + snapshot form data now (request objects aren't thread-safe)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    f.save(tmp.name); tmp.close()
    form_data = dict(request.form)
    filename  = f.filename

    _cleanup_outputs()
    _cleanup_jobs()
    jid = _new_job("analyze")

    t = threading.Thread(
        target=_run_analysis_job,
        args=(jid, tmp.name, filename, form_data),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": jid})


def _run_stress_compute(data: dict):
    """Delegate to engines.stress_engine.compute_stress."""
    return compute_stress(data)


def _build_seam_summary(analyzer) -> dict:
    """Extract seam start/end positions and thermal data for frontend display."""
    import math as _math
    td = analyzer.thermal_data
    if not td:
        return {}

    starts = [d for d in td if d.get("is_seam_start")]
    ends   = [d for d in td if d.get("is_seam_end")]
    if not starts:
        return {}

    xs = [d["x"] for d in starts]
    ys = [d["y"] for d in starts]
    xy_drift = _math.sqrt((max(xs) - min(xs))**2 + (max(ys) - min(ys))**2)

    gaps = [d["seam_gap_mm"] for d in starts if d["seam_gap_mm"] > 0]
    temps = [d["temp_C"] for d in starts]

    if xy_drift < 5:
        seam_type = "FIXED"
        risk = "HIGH"
    elif xy_drift < 30:
        seam_type = "NEAR-FIXED"
        risk = "MEDIUM"
    else:
        seam_type = "RANDOM"
        risk = "LOW"

    return {
        "count": len(starts),
        "xy_drift_mm": round(xy_drift, 1),
        "seam_type": seam_type,
        "risk": risk,
        "avg_gap_mm":  round(sum(gaps) / len(gaps), 2) if gaps else 0,
        "max_gap_mm":  round(max(gaps), 2) if gaps else 0,
        "avg_temp_C":  round(sum(temps) / len(temps), 0) if temps else 0,
        # Full arrays for 3D scatter overlay in frontend
        "start_x":     [round(d["x"], 2) for d in starts],
        "start_y":     [round(d["y"], 2) for d in starts],
        "start_z":     [round(d["z"], 2) for d in starts],
        "start_tc":    [round(d["temp_C"], 0) for d in starts],
        "start_gap":   [round(d["seam_gap_mm"], 2) for d in starts],
        "start_oe":    [round(d["seam_overlap_energy"], 1) for d in starts],
        "start_layer": [d["layer_num"] for d in starts],
        "end_x":       [round(d["x"], 2) for d in ends],
        "end_y":       [round(d["y"], 2) for d in ends],
        "end_z":       [round(d["z"], 2) for d in ends],
        "end_tc":      [round(d["temp_C"], 0) for d in ends],
        "end_layer":   [d["layer_num"] for d in ends],
    }


def _run_analysis_job(jid: str, zip_path: str, filename: str, form: dict):
    try:
        _job_stage(jid, "Extracting ZIP…", 3)
        analyzer = MeltioDEDAnalyzer(zip_path)
        analyzer.part_name = Path(filename).stem
        if not analyzer.extract_and_read():
            raise Exception("Failed to extract ZIP")

        _job_stage(jid, "Parsing RAPID code…", 8)
        analyzer.parse_rapid_code()

        _job_stage(jid, "Matching materials…", 14)
        db = load_materials_db()
        def _f(key, default):
            try: return float(form.get(key) or default)
            except (ValueError, TypeError): return float(default)
        analyzer.user = {
            "part_name":          form.get("part_name") or analyzer.part_name,
            "laser_power":        form.get("laser_power") or "1000",
            "feed_speed":         _f("feed_speed",         12.5),
            "layer_height":       _f("layer_height",        0.6),
            "layer_width":        _f("layer_width",         2.0),
            "wire_diameter":      _f("wire_diameter",       1.2),
            "material_T0":        form.get("material_T0", ""),
            "material_T1":        form.get("material_T1", ""),
            "inert_environment":  form.get("inert_environment", "false").lower() == "true",
            "ambient_temp":       _f("ambient_temp",        25),
            "min_layer_dwell":    _f("min_layer_dwell",      0),
            "beam_spot_diameter": _f("beam_spot_diameter",  1.2),
        }
        for feeder in ["T0", "T1"]:
            raw = analyzer.user.get(f"material_{feeder}", "")
            match = fuzzy_match_material(raw, db) if raw else None
            analyzer.db_materials[feeder] = match or ({
                "display_name": raw or "Unknown",
                "thermal_conductivity": 15.0,
                "density": 7000, "specific_heat": 500,
                "melting_point": 1400, "notes": "Custom"
            } if raw else None)

        _job_stage(jid, "Thermal simulation (starting)…", 18)
        _tee_local.tee = _ProgressTee(jid, 18, 65)
        try:
            analyzer.calculate_thermal_data()
        finally:
            _tee_local.tee = None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _job_stage(jid, "Exporting CSV…", 68)
        v_csv  = analyzer.generate_csv(ts)
        _job_stage(jid, "Generating 3D visualisation…", 75)
        v_3d   = analyzer.generate_3d_html(ts)
        _job_stage(jid, "Generating process window…", 82)
        v_pw   = analyzer.generate_process_window_html(ts)
        _job_stage(jid, "Generating animation…", 88)
        v_anim = analyzer.generate_animation_html(ts)
        _job_stage(jid, "Generating report…", 94)
        viz = {"csv": v_csv, "3d": v_3d, "pw": v_pw, "anim": v_anim}
        report_md   = analyzer.generate_report(viz)
        report_path = analyzer.save_report(report_md, ts)

        # ── Auto stress estimate ───────────────────────────────────────────
        _job_stage(jid, "Computing stress estimate…", 95)
        _auto_stress = run_auto_stress(analyzer)

        _job_stage(jid, "Generating distortion animation…", 97)
        v_distort = analyzer.generate_distortion_animation_html(_auto_stress or {}, ts) if _auto_stress else ""
        viz["distort"] = v_distort
        viz["mesh"] = ""

        hi_vals    = [d["heat_index"]    for d in analyzer.thermal_data] or [0]
        temp_vals  = [d["temp_C"]        for d in analyzer.thermal_data] if analyzer.thermal_data else [25]
        curv_vals  = [d["curvature_deg"] for d in analyzer.thermal_data] if analyzer.thermal_data else [0]
        ved_vals   = [d.get("VED", 0)    for d in analyzer.thermal_data] if analyzer.thermal_data else [0]
        nh_vals    = [d.get("norm_H", 0) for d in analyzer.thermal_data] if analyzer.thermal_data else [0]
        cr_vals    = [d.get("cracking_score", 0) for d in analyzer.thermal_data] if analyzer.thermal_data else [0]
        dep_speeds = [d["speed"]         for d in analyzer.thermal_data]
        pt = analyzer._print_time_summary()

        # Pre-compute scalar bounds once — avoids O(n²) inside generator expressions
        _t_min = min(temp_vals);  _t_max = max(temp_vals)
        _hi_min = min(hi_vals);   _hi_max = max(hi_vals)
        _hotspot_thresh = _t_min + (_t_max - _t_min) * 0.8

        result = {
            "part_name": analyzer.user["part_name"],
            "num_layers": analyzer.num_layers,
            "materials_found": sorted(analyzer.materials_found),
            "total_waypoints": len(analyzer.waypoints),
            "deposition_waypoints": len(analyzer.thermal_data),
            "material_changes": analyzer.material_changes,
            "warnings": [e["warning"] for e in analyzer.material_changes if e.get("warning")],
            "speeds_detected": sorted(set(round(s,1) for s in analyzer.all_speeds)),
            "speed_range": [min(analyzer.all_speeds), max(analyzer.all_speeds)] if analyzer.all_speeds else [0,0],
            "heat_index": {
                "min": round(_hi_min, 4),
                "max": round(_hi_max, 4),
                "avg": round(sum(hi_vals)/len(hi_vals), 4),
            },
            "temperature": {
                "min":        round(_t_min, 1),
                "max":        round(_t_max, 1),
                "avg":        round(sum(temp_vals)/len(temp_vals), 1),
                "unit":       "°C",
                "hotspots":   sum(1 for t in temp_vals if t > _hotspot_thresh),
                "curv_zones": sum(1 for c in curv_vals if c > 30),
                "model":      "Rykalin moving heat source + Ar convection (35 W/m²K)",
            },
            "volume_v1": round(math.pi * (analyzer.user["wire_diameter"]/2)**2 * analyzer.user["feed_speed"], 4),
            "volume_v2": round(analyzer.user["layer_width"] * analyzer.user["layer_height"] * (sum(dep_speeds)/len(dep_speeds) if dep_speeds else 0), 4),
            "db_materials": {
                k: ({"display_name": v["display_name"], "thermal_conductivity": v["thermal_conductivity"],
                     "density": v["density"], "specific_heat": v["specific_heat"],
                     "melting_point": v.get("melting_point", 1400),
                     "absorption_450nm": v.get("absorption_450nm", 0.35)}
                    if v else None)
                for k, v in analyzer.db_materials.items()
            },
            "io_signals": sorted(set(s for sigs in analyzer.digital_ios.values() for s in sigs)),
            "print_time": pt,
            "inert_environment": analyzer.user["inert_environment"],
            "seam_analysis": _build_seam_summary(analyzer),
            "anomalies": {
                "lof_zones":          sum(1 for d in analyzer.thermal_data if d.get("lof_risk")),
                "keyhole_zones":      sum(1 for d in analyzer.thermal_data if d.get("keyhole_risk")),
                "overheat_zones":     sum(1 for d in analyzer.thermal_data if d.get("overheat_risk")),
                "lof_depth_zones":    sum(1 for d in analyzer.thermal_data if d.get("lof_depth_risk")),
                "max_cracking_score": round(max(cr_vals), 3) if cr_vals else 0,
                "avg_VED":            round(sum(ved_vals)/len(ved_vals), 1) if ved_vals else 0,
                "avg_norm_H":         round(sum(nh_vals)/len(nh_vals), 2) if nh_vals else 0,
                "beam_spot_mm":       analyzer.user.get("beam_spot_diameter", 1.2),
            },
            "viz_files": {
                "csv":    f"/outputs/{Path(viz['csv']).name}"     if viz.get("csv")     else None,
                "3d":     f"/outputs/{Path(viz['3d']).name}"      if viz.get("3d")      else None,
                "pw":     f"/outputs/{Path(viz['pw']).name}"      if viz.get("pw")      else None,
                "anim":   f"/outputs/{Path(viz['anim']).name}"    if viz.get("anim")    else None,
                "distort":f"/outputs/{Path(viz['distort']).name}" if viz.get("distort") else None,
                "mesh":   f"/outputs/{Path(viz['mesh']).name}"   if viz.get("mesh")    else None,
            },
            "report_path": f"/outputs/{Path(report_path).name}",
            "report_md": report_md,
        }
        if _auto_stress:
            result['stress'] = _auto_stress

        # Process params snapshot for frontend cross-page sync
        try:
            _lp = float(str(analyzer.user.get('laser_power', 1000)).replace('W','').strip())
        except (ValueError, TypeError):
            _lp = 1000.0
        _avg_scan = (sum(dep_speeds) / len(dep_speeds)) if dep_speeds else 10.0
        result['user_params'] = {
            'laser_power':   _lp,
            'feed_speed':    analyzer.user.get('feed_speed',   12),
            'scan_speed':    round(_avg_scan, 2),
            'layer_height':  analyzer.user.get('layer_height',  0.5),
            'layer_width':   analyzer.user.get('layer_width',   2.0),
            'wire_diameter': analyzer.user.get('wire_diameter', 1.2),
            'ambient_temp':  analyzer.user.get('ambient_temp',  25),
            'dwell_time':    analyzer.user.get('min_layer_dwell', 0),
            'beam_spot':     analyzer.user.get('beam_spot_diameter', 1.2),
            'mat_T0':        analyzer.user.get('material_T0', ''),
            'mat_T1':        analyzer.user.get('material_T1', ''),
            'environment':   'inert' if analyzer.user.get('inert_environment') else 'regular',
        }

        # Full waypoints for FEM simulation — sampled to max 8000
        _step = max(1, len(analyzer.waypoints) // 8000)
        result['waypoints_full'] = [
            {'x': wp['x'], 'y': wp['y'], 'z': wp['z'],
             'layer': wp.get('layer_num', 0),
             'is_deposition': wp.get('is_deposition', False),
             'speed': wp.get('speed', 0)}
            for wp in analyzer.waypoints[::_step]
        ]

        # Thermal+stress overlay data — sampled to max 6000 deposition points
        # Stress values are looked up by layer (not by XYZ coordinate match)
        # so every td point gets displacement vectors correctly.
        _td = analyzer.thermal_data or []

        # Build per-layer lookup from per_layer table
        _layer_sigma = {}
        _layer_delta = {}
        if _auto_stress and _auto_stress.get('per_layer'):
            for pl in _auto_stress['per_layer']:
                _layer_sigma[pl['layer']] = pl['sigma_MPa']
                _layer_delta[pl['layer']] = pl['delta_mm']
        _num_layers_stress = max(_layer_sigma.keys()) if _layer_sigma else 1

        # Centroid and span for displacement direction calc
        _all_x = [d['x'] for d in _td] or [0]
        _all_y = [d['y'] for d in _td] or [0]
        _cx = (min(_all_x) + max(_all_x)) / 2
        _cy = (min(_all_y) + max(_all_y)) / 2
        _span = max(max(_all_x)-min(_all_x), max(_all_y)-min(_all_y), 1.0)

        # Per-layer arc-length fraction within layer (intra-layer gradient)
        import math as _math
        from collections import defaultdict as _dd
        _layer_pts_idx = _dd(list)
        for _i, _d in enumerate(_td):
            _layer_pts_idx[_d.get('layer_num', 1)].append(_i)
        _arc_frac = [0.0] * len(_td)
        for _lay, _idxs in _layer_pts_idx.items():
            _cum = 0.0; _arcs = [0.0]
            for _k in range(1, len(_idxs)):
                _a = _td[_idxs[_k]]; _b = _td[_idxs[_k-1]]
                _cum += _math.sqrt((_a['x']-_b['x'])**2+(_a['y']-_b['y'])**2+(_a['z']-_b['z'])**2)
                _arcs.append(_cum)
            _tot = max(_cum, 1e-9)
            for _k, _idx in enumerate(_idxs):
                _arc_frac[_idx] = _arcs[_k] / _tot

        INTRA_GRAD = 0.25
        _max_overlay = int(form.get('overlay_points', 50000))
        _td_step = max(1, len(_td) // _max_overlay)
        overlay_pts = []
        for _i, td in enumerate(_td[::_td_step]):
            _orig_i = _i * _td_step
            _lay = td.get('layer_num', 1)
            _mapped = max(1, min(_num_layers_stress,
                                 round(_lay / max(max(_layer_pts_idx.keys()) if _layer_pts_idx else 1, 1)
                                       * _num_layers_stress)))
            _sigma_base = _layer_sigma.get(_mapped, 0.0)
            _delta      = _layer_delta.get(_mapped, 0.0)
            _af         = _arc_frac[_orig_i] if _orig_i < len(_arc_frac) else 0.0
            _sigma      = _sigma_base * (1.0 + INTRA_GRAD * (1.0 - _af))

            # Radial displacement direction from centroid
            _rx = td['x'] - _cx; _ry = td['y'] - _cy
            _r  = _math.sqrt(_rx*_rx + _ry*_ry) or 1.0
            _ux = _rx / _r;       _uy = _ry / _r
            _moment = _r / _span * 0.5 + 0.5

            overlay_pts.append({
                'x':     round(td['x'], 2),
                'y':     round(td['y'], 2),
                'z':     round(td['z'], 2),
                'layer': _lay,
                'temp_C':round(td.get('temp_C', 25), 1),
                'sigma': round(_sigma, 1),
                'delta': round(_delta, 3),
                'dx':    round(_ux * _delta, 3),
                'dy':    round(_uy * _delta, 3),
                'dz':    round(-_delta * _moment * 0.3, 3),
                'af':    round(_af, 3),
            })
        result['overlay_pts'] = overlay_pts

        _job_update(jid, status="done", pct=100, stage="Complete", result=result)

        # Pre-compute sensitivity in background so it's ready when UI asks
        if _auto_stress:
            threading.Thread(target=_run_sensitivity_bg, args=(jid,), daemon=True).start()

    except Exception as e:
        import traceback
        _job_update(jid, status="error",
                    error=str(e), trace=traceback.format_exc())
    finally:
        try: os.unlink(zip_path)
        except Exception: pass


@app.route('/api/overlay/html/<job_id>')
def overlay_html(job_id):
    """
    Generate a self-contained Plotly HTML page:
      - STL mesh as grey semi-transparent ghost (if available)
      - Toolpath deposition points coloured by temp/sigma/delta
      - Layer slider + colour-mode toggle
    """
    job = _job_get(job_id)
    if not job or job.get('status') != 'done':
        return "Job not ready", 400

    result      = job.get('result', {})
    overlay_pts = result.get('overlay_pts', [])
    if not overlay_pts:
        return "<html><body style='color:#aaa;font-family:sans-serif;padding:40px'>No overlay data — re-run analysis.</body></html>", 200

    mesh_trace = ''

    import json as _json
    pts_json    = _json.dumps(overlay_pts)
    num_layers  = result.get('num_layers', 1)
    part_name   = result.get('part_name', '')
    temp_max    = max((p['temp_C'] for p in overlay_pts), default=1500)
    sigma_max   = max((p['sigma']  for p in overlay_pts), default=1)
    delta_max   = max((p['delta']  for p in overlay_pts), default=1) or 0.001
    import math as _math
    disp_max    = max((_math.sqrt(p.get('dx',0)**2+p.get('dy',0)**2+p.get('dz',0)**2)
                       for p in overlay_pts), default=0.001) or 0.001

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Overlay — {part_name}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0d1117; color:#c9d1d9; font-family:system-ui,sans-serif; height:100vh; display:flex; flex-direction:column; }}
#toolbar {{ display:flex; align-items:center; gap:12px; padding:8px 14px;
            background:#161b22; border-bottom:1px solid #30363d; flex-wrap:wrap; flex-shrink:0; }}
.label {{ font-size:.75rem; color:#8b949e; white-space:nowrap; }}
.btn {{ padding:5px 14px; border-radius:5px; border:1px solid #30363d; background:#21262d;
        color:#c9d1d9; cursor:pointer; font-size:.78rem; transition:border-color .15s; }}
.btn.active {{ border-color:#58a6ff; color:#58a6ff; background:rgba(88,166,255,.1); }}
.btn-play {{ padding:5px 16px; border-radius:5px; border:1px solid #238636; background:#238636;
             color:#fff; cursor:pointer; font-size:.82rem; font-weight:600; min-width:76px; }}
.btn-play:hover {{ background:#2ea043; }}
#layerSlider {{ flex:1; min-width:120px; max-width:280px; accent-color:#58a6ff; }}
#speedSlider  {{ width:70px; accent-color:#8b949e; }}
#plot {{ flex:1; position:relative; }}
#centerPlay {{
  position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
  width:72px; height:72px; border-radius:50%;
  background:rgba(35,134,54,.85); border:3px solid #2ea043;
  color:#fff; font-size:28px; cursor:pointer; z-index:10;
  display:flex; align-items:center; justify-content:center;
  box-shadow:0 4px 24px rgba(0,0,0,.5); transition:opacity .2s, transform .15s;
  pointer-events:auto;
}}
#centerPlay:hover {{ background:rgba(46,160,67,.95); transform:translate(-50%,-50%) scale(1.08); }}
#anim-bar {{ display:flex; align-items:center; gap:10px; padding:6px 14px;
             background:#0d1117; border-bottom:1px solid #21262d; flex-shrink:0; }}
</style>
</head>
<body>
<div id="toolbar">
  <span class="label">Color:</span>
  <button class="btn active" id="btnTemp"  onclick="setMode('temp')">Temp (°C)</button>
  <button class="btn"        id="btnSigma" onclick="setMode('sigma')">Stress σ</button>
  <button class="btn"        id="btnDelta" onclick="setMode('delta')">Distortion δ</button>
  <span style="width:1px;height:22px;background:#30363d;margin:0 2px"></span>
  <button class="btn"        id="btnVec"   onclick="toggleVectors()">Vectors off</button>
  <span style="width:1px;height:22px;background:#30363d;margin:0 2px"></span>
  <span class="label">Layer:</span>
  <input type="range" id="layerSlider" min="0" max="{num_layers}" value="{num_layers}"
         oninput="onSlider(+this.value)">
  <span id="layerLabel" style="font-size:.8rem;min-width:64px">All layers</span>
  <span style="width:1px;height:22px;background:#30363d;margin:0 2px"></span>
  <button class="btn" onclick="Plotly.relayout('plot',{{'scene.camera':{{eye:{{x:1.4,y:1.4,z:0.8}}}}}})">⌖ Center</button>
  <span style="width:1px;height:22px;background:#30363d;margin:0 2px"></span>
  <button class="btn-play" id="btnPlay" onclick="togglePlay()">&#9654; Play</button>
  <span class="label">Speed:</span>
  <input type="range" id="speedSlider" min="1" max="10" value="4"
         oninput="onSpeed(+this.value)" title="Animation speed">
  <span id="speedLabel" style="font-size:.78rem;min-width:28px;color:#8b949e">4×</span>
</div>
<div id="plot">
</div>

<script>
const PTS       = {pts_json};
const MESH      = {mesh_trace if mesh_trace else 'null'};
const NUM_LAYERS = {num_layers};
const TEMP_MAX  = {temp_max};
const SIGMA_MAX = {sigma_max};
const DELTA_MAX = {delta_max};
const DISP_MAX  = {disp_max};

let currentMode   = 'temp';
let currentLayer  = NUM_LAYERS;
let showVectors   = false;

// Animation state
let animPlaying  = false;
let animLayer    = 1;
let animSpeed    = 4;
let animTimer    = null;
let animPtIdx    = 0;   // current point index for head position
const ANIM_INTERVAL_MS = 80;

// ── colour helpers ───────────────────────────────────────────────────────────
function getColor(p) {{
  if (currentMode === 'temp')  return p.temp_C;
  if (currentMode === 'sigma') return p.sigma;
  return p.delta;
}}
function getScale() {{
  if (currentMode === 'temp')  return [[0,'#3b82f6'],[0.4,'#22c55e'],[0.7,'#f59e0b'],[1,'#ef4444']];
  if (currentMode === 'sigma') return [[0,'#22c55e'],[0.5,'#f59e0b'],[0.8,'#ef4444'],[1,'#7f1d1d']];
  return [[0,'#60a5fa'],[0.4,'#34d399'],[0.7,'#fbbf24'],[1,'#ef4444']];
}}
function getCmax() {{
  if (currentMode === 'temp')  return TEMP_MAX;
  if (currentMode === 'sigma') return SIGMA_MAX;
  return DELTA_MAX;
}}
function getLabel() {{
  if (currentMode === 'temp')  return '°C';
  if (currentMode === 'sigma') return 'MPa';
  return 'mm';
}}

// ── visible points ───────────────────────────────────────────────────────────
function visiblePts(upToLayer) {{
  const lim = (upToLayer === undefined) ? currentLayer : upToLayer;
  if (lim === 0 || lim === NUM_LAYERS) return PTS;
  return PTS.filter(p => p.layer <= lim);
}}

// ── cone (vector) trace ──────────────────────────────────────────────────────
// Subsample to at most MAX_CONES cones for performance
const MAX_CONES = 800;
function buildConeTrace(pts) {{
  const step = Math.max(1, Math.floor(pts.length / MAX_CONES));
  const sub  = pts.filter((_, i) => i % step === 0);
  // Scale: normalise displacement to a fraction of bounding-box span so cones
  // are always visible regardless of absolute displacement magnitude
  const xs = pts.map(p=>p.x), ys = pts.map(p=>p.y), zs = pts.map(p=>p.z);
  const span = Math.max(
    Math.max(...xs)-Math.min(...xs),
    Math.max(...ys)-Math.min(...ys),
    Math.max(...zs)-Math.min(...zs), 1);
  const scale = span * 0.08 / (DISP_MAX || 1);   // cones ~8% of part size

  // Cone colour = displacement magnitude (sigma-scale)
  const mags = sub.map(p => Math.sqrt(p.dx*p.dx + p.dy*p.dy + p.dz*p.dz));

  return {{
    type: 'cone',
    x: sub.map(p=>p.x), y: sub.map(p=>p.y), z: sub.map(p=>p.z),
    u: sub.map(p=>p.dx*scale),
    v: sub.map(p=>p.dy*scale),
    w: sub.map(p=>p.dz*scale),
    colorscale: [[0,'#22c55e'],[0.5,'#f59e0b'],[0.8,'#ef4444'],[1,'#7f1d1d']],
    cmin: 0, cmax: DISP_MAX,
    color: mags,
    sizemode: 'absolute',
    sizeref: span * 0.04,
    anchor: 'tail',
    showscale: false,
    opacity: 0.85,
    name: 'Displacement',
    hovertemplate: '|δ|=%{{customdata:.3f}} mm<extra></extra>',
    customdata: mags,
  }};
}}

const TRAIL_LEN = 60;

// ── build all traces ─────────────────────────────────────────────────────────
function buildTraces(upToLayer, headIdx) {{
  const pts = visiblePts(upToLayer);

  const pathTrace = {{
    type: 'scatter3d', mode: 'markers',
    x: pts.map(p=>p.x), y: pts.map(p=>p.y), z: pts.map(p=>p.z),
    marker: {{
      size: 3,
      color: pts.map(getColor),
      colorscale: getScale(),
      cmin: 0, cmax: getCmax(),
      colorbar: {{ title: {{ text: getLabel(), side:'right' }}, thickness: 12, len: 0.7,
                   tickfont: {{color:'#8b949e'}}, titlefont: {{color:'#8b949e'}} }},
      opacity: 0.9,
    }},
    name: 'Toolpath',
    hovertemplate: 'Layer %{{customdata}}<br>T=%{{text}} °C<br>σ=%{{meta[0]}} MPa<br>δ=%{{meta[1]}} mm<extra></extra>',
    customdata: pts.map(p=>p.layer),
    text: pts.map(p=>p.temp_C),
    meta: pts.map(p=>[p.sigma, p.delta]),
  }};

  const traces = [];
  traces.push(pathTrace);

  // Print head + trail (shown during animation)
  if (headIdx !== undefined && headIdx >= 0 && headIdx < PTS.length) {{
    const ts = Math.max(0, headIdx - TRAIL_LEN);
    const trail = PTS.slice(ts, headIdx + 1);
    const n = trail.length;
    traces.push({{
      type: 'scatter3d', mode: 'lines+markers',
      x: trail.map(p=>p.x), y: trail.map(p=>p.y), z: trail.map(p=>p.z),
      line: {{ color: 'rgba(255,220,0,0.5)', width: 3 }},
      marker: {{
        size: trail.map((_,i) => 2 + 5*(i/Math.max(n-1,1))),
        color: trail.map((_,i) => `rgba(255,220,0,${{(0.1+0.9*(i/Math.max(n-1,1))).toFixed(2)}})`)
      }},
      hoverinfo: 'skip', name: 'Trail'
    }});
    const h = PTS[headIdx];
    traces.push({{
      type: 'scatter3d', mode: 'markers',
      x: [h.x], y: [h.y], z: [h.z],
      marker: {{ size: 14, color: '#00ffff', symbol: 'diamond', line: {{ color: '#ffffff', width: 2 }} }},
      hoverinfo: 'skip', name: 'Print Head'
    }});
  }}

  if (showVectors) {{
    const hasDsp = pts.some(p => p.dx || p.dy || p.dz);
    if (hasDsp) traces.push(buildConeTrace(pts));
  }}
  return traces;
}}


function toggleVectors() {{
  showVectors = !showVectors;
  const btn = document.getElementById('btnVec');
  btn.textContent = showVectors ? 'Vectors on' : 'Vectors off';
  btn.classList.toggle('active', showVectors);
  if (!animPlaying) refresh();
}}

const layout = {{
  paper_bgcolor: '#0d1117',
  margin: {{l:0,r:0,t:0,b:0}},
  scene: {{
    bgcolor: '#0d1117',
    xaxis: {{color:'#484f58', gridcolor:'rgba(72,79,88,.3)', showbackground:false}},
    yaxis: {{color:'#484f58', gridcolor:'rgba(72,79,88,.3)', showbackground:false}},
    zaxis: {{color:'#484f58', gridcolor:'rgba(72,79,88,.3)', showbackground:false}},
    aspectmode: 'data',
  }},
  legend: {{font:{{color:'#8b949e'}}, bgcolor:'transparent'}},
}};

Plotly.newPlot('plot', buildTraces(), layout, {{responsive:true, displayModeBar:false}});

function refresh(upToLayer) {{
  Plotly.react('plot', buildTraces(upToLayer), layout);
}}

function setMode(m) {{
  currentMode = m;
  ['Temp','Sigma','Delta'].forEach(n => {{
    const b = document.getElementById('btn'+n);
    if (b) b.classList.remove('active');
  }});
  document.getElementById('btn'+m.charAt(0).toUpperCase()+m.slice(1))?.classList.add('active');
  if (!animPlaying) refresh();
}}

function onSlider(v) {{
  if (animPlaying) stopAnim();
  currentLayer = v;
  const lbl = document.getElementById('layerLabel');
  lbl.textContent = (v === 0 || v === NUM_LAYERS) ? 'All layers' : 'Layer ' + v;
  refresh();
}}

function onSpeed(v) {{
  animSpeed = v;
  document.getElementById('speedLabel').textContent = v + '×';
}}

// ── Animation ────────────────────────────────────────────────────────────────
function togglePlay() {{
  if (animPlaying) stopAnim(); else startAnim();
}}
function _setPlayState(playing) {{
  const btn = document.getElementById('btnPlay');
  if (playing) {{
    btn.textContent = '⏹ Stop';
    btn.style.background = btn.style.borderColor = '#b91c1c';
  }} else {{
    btn.innerHTML = '&#9654; Play';
    btn.style.background = btn.style.borderColor = '#238636';
  }}
}}
function startAnim() {{
  animPlaying = true;
  animLayer   = 1;
  animPtIdx   = 0;
  _setPlayState(true);
  animTimer = setInterval(animStep, ANIM_INTERVAL_MS);
}}
function stopAnim() {{
  animPlaying = false;
  clearInterval(animTimer);
  animTimer = null;
  _setPlayState(false);
  const slider = document.getElementById('layerSlider');
  slider.value = animLayer;
  document.getElementById('layerLabel').textContent =
    (animLayer >= NUM_LAYERS) ? 'All layers' : 'Layer ' + animLayer;
  currentLayer = animLayer;
  refresh();
}}
function animStep() {{
  if (animLayer > NUM_LAYERS) {{ stopAnim(); return; }}
  document.getElementById('layerSlider').value = animLayer;
  document.getElementById('layerLabel').textContent = 'Layer ' + animLayer;
  // Advance point index to last point in current layer
  while (animPtIdx < PTS.length - 1 && PTS[animPtIdx].layer <= animLayer) animPtIdx++;
  Plotly.react('plot', buildTraces(animLayer, animPtIdx), layout);
  animLayer += Math.max(1, Math.round(animSpeed));
}}
</script>
</body>
</html>"""
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


def _run_fem_job(fem_jid: str, source_jid: str, resolution: str):
    try:
        source      = _job_get(source_jid)
        result      = source.get('result', {})
        waypoints   = result.get('waypoints_full', [])
        user_params = result.get('user_params', {})
        db_mat      = result.get('db_materials', {}).get('T0') or {}

        material = {
            'k':       db_mat.get('thermal_conductivity', 16.3),
            'density': db_mat.get('density', 7990),
            'Cp':      db_mat.get('specific_heat', 500),
            'T_melt':  db_mat.get('melting_point', 1375),
        }
        params = {
            'laser_power':  user_params.get('laser_power', 1000),
            'scan_speed':   user_params.get('scan_speed', 10),
            'layer_height': user_params.get('layer_height', 0.8),
            'bead_width':   user_params.get('layer_width', 1.5),
            'beam_spot':    user_params.get('beam_spot', 1.2),
            'ambient_temp': user_params.get('ambient_temp', 25),
            'absorption':   db_mat.get('absorption_450nm', 0.35),
            'num_layers':   result.get('num_layers', 50),
            'dwell_time':   user_params.get('dwell_time', 0),
        }

        if not waypoints:
            raise ValueError('No waypoints in job result — re-run analysis first')

        from engines.reduced_fem import build_grid, run_simulation

        # Improvement 4: adaptive resolution
        # If user requested 'adaptive', run fast first; if HIGH% > 5% upgrade to fine.
        ADAPTIVE_HIGH_THRESHOLD = 5.0   # % of active voxels that are HIGH
        effective_resolution = resolution

        if resolution == 'adaptive':
            _job_stage(fem_jid, 'Adaptive: fast pass…', 5)
            grid_fast = build_grid(waypoints, params, 'fast')
            def _prog_fast(pct, stage):
                _job_stage(fem_jid, f'Adaptive fast: {stage}', 5 + pct * 0.4)
            fast_result = run_simulation(grid_fast, waypoints, material, params, _prog_fast)
            rc = fast_result.get('risk_counts', {})
            total = max(fast_result.get('active_voxels', 1), 1)
            high_pct = rc.get('HIGH', 0) / total * 100
            if high_pct > ADAPTIVE_HIGH_THRESHOLD:
                _job_stage(fem_jid, f'HIGH={high_pct:.1f}% > {ADAPTIVE_HIGH_THRESHOLD}% → upgrading to fine…', 46)
                effective_resolution = 'fine'
            else:
                _job_stage(fem_jid, f'HIGH={high_pct:.1f}% ≤ {ADAPTIVE_HIGH_THRESHOLD}% → fast is sufficient', 46)
                fast_result['adaptive_used'] = 'fast'
                fast_result['adaptive_high_pct'] = round(high_pct, 2)
                _job_update(fem_jid, status='done', pct=100, stage='Complete', result=fast_result)
                return

        _job_stage(fem_jid, f'Building {effective_resolution} voxel grid…', 5)
        grid = build_grid(waypoints, params, effective_resolution)

        def _progress(pct, stage):
            offset = 46 if resolution == 'adaptive' else 5
            _job_stage(fem_jid, stage, offset + pct * (0.9 if resolution != 'adaptive' else 0.5))

        fem_result = run_simulation(grid, waypoints, material, params, _progress)
        if resolution == 'adaptive':
            fem_result['adaptive_used'] = effective_resolution
        _job_update(fem_jid, status='done', pct=100, stage='Complete', result=fem_result)

    except Exception as e:
        import traceback
        _job_update(fem_jid, status='error', error=str(e), trace=traceback.format_exc())


@app.route('/api/fem/simulate', methods=['POST'])
def fem_simulate():
    data       = request.get_json() or {}
    job_id     = data.get('job_id')
    resolution = data.get('resolution', 'standard')

    job = _job_get(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'Analysis job not complete'}), 400

    _cleanup_jobs()
    fem_jid = _new_job('fem')
    threading.Thread(target=_run_fem_job, args=(fem_jid, job_id, resolution), daemon=True).start()
    return jsonify({'job_id': fem_jid})


@app.route('/api/jobs/<jid>/geometry', methods=['GET'])
def job_geometry(jid):
    """
    Return a clean grey Plotly mesh3d payload built from deposition waypoints.
    No thermal coloring — pure part geometry for shape inspection.
    """
    job = _job_get(jid)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'Job not ready'}), 400

    result = job.get('result', {})
    overlay_pts = result.get('overlay_pts', [])
    if not overlay_pts:
        return jsonify({'error': 'No waypoints available'}), 400

    # Group deposition points by layer
    from collections import defaultdict
    by_layer = defaultdict(list)
    for p in overlay_pts:
        by_layer[p['layer']].append(p)
    layers = sorted(by_layer.keys())

    if len(layers) < 2:
        return jsonify({'error': 'Need at least 2 layers for geometry'}), 400

    verts_x, verts_y, verts_z = [], [], []
    tri_i, tri_j, tri_k = [], [], []
    vert_offset = 0
    MAX_PTS = 120  # points per layer — keeps vertex count manageable

    for li in range(len(layers) - 1):
        pts_a = by_layer[layers[li]]
        pts_b = by_layer[layers[li + 1]]
        n = min(len(pts_a), len(pts_b), MAX_PTS)
        if n < 3:
            continue
        sa = pts_a[::max(1, len(pts_a) // n)][:n]
        sb = pts_b[::max(1, len(pts_b) // n)][:n]

        base = vert_offset
        for p in sa:
            verts_x.append(p['x']); verts_y.append(p['y']); verts_z.append(p['z'])
        for p in sb:
            verts_x.append(p['x']); verts_y.append(p['y']); verts_z.append(p['z'])

        for j in range(n - 1):
            a0, a1, b0, b1 = base+j, base+j+1, base+n+j, base+n+j+1
            tri_i += [a0, a0]; tri_j += [a1, b0]; tri_k += [b0, b1]
        # close loop
        a0, a1 = base+n-1, base
        b0, b1 = base+n+n-1, base+n
        tri_i += [a0, a0]; tri_j += [a1, b0]; tri_k += [b0, b1]
        vert_offset += 2 * n

    return jsonify({
        'ok': True,
        'vertex_count': len(verts_x),
        'face_count': len(tri_i),
        'mesh': {
            'type': 'mesh3d',
            'x': verts_x, 'y': verts_y, 'z': verts_z,
            'i': tri_i, 'j': tri_j, 'k': tri_k,
            'color': '#cccccc',
            'opacity': 0.92,
            'flatshading': True,
            'lighting': {'ambient': 0.8, 'diffuse': 0.6, 'specular': 0.2},
            'lightposition': {'x': 1, 'y': 2, 'z': 3},
            'showscale': False,
            'hoverinfo': 'none',
            'name': 'Part Geometry',
        }
    })


@app.route("/api/materials", methods=["POST"])
def add_material():
    mat = request.get_json()
    if not mat or not mat.get("id") or not mat.get("display_name"):
        return jsonify({"error": "Missing required fields"}), 400

    with open(DB_PATH, "r") as f:
        db = json.load(f)

    # Check for duplicate ID
    if any(m["id"] == mat["id"] for m in db["materials"]):
        return jsonify({"error": f"Material ID '{mat['id']}' already exists"}), 409

    db["materials"].append(mat)

    with open(DB_PATH, "w") as f:
        json.dump(db, f, indent=2)

    return jsonify({"ok": True, "total": len(db["materials"])})


@app.route("/outputs/<path:filename>")
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


# ── SENSOR ENDPOINTS ─────────────────────────────────────────────────────────

@app.route("/api/sensors/connect", methods=["POST"])
def sensors_connect():
    global _sensor
    _sensor = SensorAnalyzer()

    if "csv_file" in request.files:
        f = request.files["csv_file"]
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        f.save(tmp.name)
        tmp.close()
        path = tmp.name
    else:
        path = request.form.get("file_path", "").strip()
        if not path or not Path(path).exists():
            return jsonify({"error": "File not found"}), 400

    try:
        n = _sensor.connect(path)
        return jsonify({"ok": True, "rows": n, "summary": _sensor.summary()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensors/snapshot")
def sensors_snapshot():
    if not _sensor.rows:
        return jsonify({"error": "No sensor data loaded"}), 400
    return jsonify({
        "chart_data": _sensor.chart_data(),
        "events":     _sensor.events,
        "stats":      _sensor.stats(),
        "summary":    _sensor.summary(),
    })


@app.route("/api/sensors/stream")
def sensors_stream():
    """SSE endpoint — push new rows every 2s for live monitoring."""
    import time as _time

    def generate():
        while True:
            new_rows = _sensor.poll()
            if new_rows:
                payload = {
                    "chart_data": _sensor.chart_data(new_rows),
                    "events":     [e for e in _sensor.events if e['time'] >= new_rows[0]['tiempo']],
                    "summary":    _sensor.summary(),
                    "stats":      _sensor.stats(),
                }
                yield f"data: {json.dumps(payload)}\n\n"
            else:
                yield "data: {\"heartbeat\":true}\n\n"
            _time.sleep(2)

    return app.response_class(generate(), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── SETTINGS ─────────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.get_json() or {}
    save_settings_file(data)
    return jsonify({"ok": True})


# ── DIRECTORY WATCHER ─────────────────────────────────────────────────────────

@app.route("/api/watcher/start", methods=["POST"])
def watcher_start():
    data      = request.get_json() or {}
    watch_dir = data.get("watch_dir", "").strip()
    if not watch_dir:
        return jsonify({"error": "No directory provided"}), 400
    if not os.path.exists(watch_dir):
        return jsonify({"error": f"Directory not found: {watch_dir}"}), 400
    s = load_settings()
    s["watch_dir"] = watch_dir
    save_settings_file(s)
    _watcher.start(watch_dir)
    return jsonify({"ok": True, "status": _watcher.status()})

@app.route("/api/watcher/reset", methods=["POST"])
def watcher_reset():
    _watcher.reset()
    return jsonify({"ok": True})

@app.route("/api/watcher/stream")
def watcher_stream():
    """SSE: pushes watcher state + sensor data every 2 s."""
    import time as _time
    def generate():
        while True:
            w       = _watcher.status()
            payload = {"watcher": w}
            if w["state"] in ("file_found", "printing", "ended") and _sensor.rows:
                new_rows = _sensor.poll() if w["state"] == "printing" else []
                if new_rows:
                    payload["chart_data"] = _sensor.chart_data(new_rows)
                    payload["events"]     = [e for e in _sensor.events
                                             if e['time'] >= new_rows[0]['tiempo']]
                    payload["stats"]      = _sensor.stats()
                    payload["summary"]    = _sensor.summary()
                else:
                    payload["heartbeat"] = True
            else:
                payload["heartbeat"] = True
            yield f"data: {json.dumps(payload)}\n\n"
            _time.sleep(2)
    return app.response_class(generate(), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── HISTORY ───────────────────────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def get_history():
    entries = []
    for f in sorted(HISTORY_DIR.glob("*.json"), reverse=True):
        try:
            with open(f) as fh:
                entries.append(json.load(fh))
        except Exception:
            pass
    return jsonify(entries)

@app.route("/api/history/save", methods=["POST"])
def save_history():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    data["id"]       = ts
    data["saved_at"] = datetime.now().isoformat()
    path = HISTORY_DIR / f"{ts}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return jsonify({"ok": True, "id": ts})

@app.route("/api/history/load_csv", methods=["POST"])
def history_load_csv():
    global _sensor
    data     = request.get_json() or {}
    csv_path = data.get("csv_path", "").strip()
    if not csv_path or not Path(csv_path).exists():
        return jsonify({"error": "File not found"}), 400
    ns = SensorAnalyzer()
    n  = ns.connect(csv_path)
    _sensor = ns
    return jsonify({"ok": True, "rows": n,
                    "chart_data": _sensor.chart_data(),
                    "stats":      _sensor.stats(),
                    "events":     _sensor.events,
                    "summary":    _sensor.summary()})


# ── PRINT DASHBOARD ENDPOINT ─────────────────────────────────────────────────

@app.route("/api/dashboard/setup", methods=["POST"])
def dashboard_setup():
    if "zip_file" not in request.files:
        return jsonify({"error": "No ZIP file provided"}), 400
    zip_f = request.files["zip_file"]
    if not zip_f.filename.endswith(".zip"):
        return jsonify({"error": "File must be a .zip"}), 400

    tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    zip_f.save(tmp_zip.name); tmp_zip.close()
    filename  = zip_f.filename
    form_data = dict(request.form)

    tmp_csv_path = None
    if "csv_file" in request.files:
        csv_f = request.files["csv_file"]
        tmp_csv = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        csv_f.save(tmp_csv.name); tmp_csv.close()
        tmp_csv_path = tmp_csv.name

    _cleanup_jobs()
    jid = _new_job("dashboard")
    t = threading.Thread(
        target=_run_dashboard_job,
        args=(jid, tmp_zip.name, tmp_csv_path, filename, form_data),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": jid})


def _run_dashboard_job(jid: str, zip_path: str, tmp_csv_path, filename: str, form: dict):
    global _dash_analyzer, _dash_sensor, _sensor
    try:
        _job_stage(jid, "Extracting ZIP…", 3)
        analyzer = MeltioDEDAnalyzer(zip_path)
        analyzer.part_name = Path(filename).stem
        if not analyzer.extract_and_read():
            raise Exception("Failed to extract ZIP")

        _job_stage(jid, "Parsing RAPID code…", 8)
        analyzer.parse_rapid_code()

        _job_stage(jid, "Matching materials…", 14)
        db = load_materials_db()
        def _f(key, default):
            try: return float(form.get(key) or default)
            except (ValueError, TypeError): return float(default)
        analyzer.user = {
            "part_name":          form.get("part_name") or analyzer.part_name,
            "laser_power":        form.get("laser_power") or "1000",
            "feed_speed":         _f("feed_speed",         12.5),
            "layer_height":       _f("layer_height",        0.6),
            "layer_width":        _f("layer_width",         2.0),
            "wire_diameter":      _f("wire_diameter",       1.2),
            "material_T0":        form.get("material_T0", ""),
            "material_T1":        form.get("material_T1", ""),
            "inert_environment":  form.get("inert_environment", "false").lower() == "true",
            "ambient_temp":       _f("ambient_temp",        25),
            "min_layer_dwell":    _f("min_layer_dwell",      0),
            "beam_spot_diameter": _f("beam_spot_diameter",  1.2),
        }
        for feeder in ["T0", "T1"]:
            raw = analyzer.user.get(f"material_{feeder}", "")
            match = fuzzy_match_material(raw, db) if raw else None
            analyzer.db_materials[feeder] = match or ({
                "display_name": raw or "Unknown",
                "thermal_conductivity": 15.0,
                "density": 7000, "specific_heat": 500,
                "melting_point": 1400, "notes": "Custom"
            } if raw else None)

        # thermal_data is fully populated before the 3D FDM block starts; FDM failure is non-fatal
        _job_stage(jid, "Thermal simulation (starting)…", 18)
        _tee_local.tee = _ProgressTee(jid, 18, 85)
        try:
            try:
                analyzer.calculate_thermal_data()
            except Exception:
                pass
        finally:
            _tee_local.tee = None

        _dash_analyzer = analyzer

        # Connect sensor CSV
        sensor_snapshot = None
        if tmp_csv_path:
            _job_stage(jid, "Loading sensor CSV…", 88)
            new_sensor = SensorAnalyzer()
            new_sensor.connect(tmp_csv_path)
            _dash_sensor = new_sensor
            _sensor = new_sensor   # share with /api/sensors/stream
            sensor_snapshot = {
                "chart_data": _dash_sensor.chart_data(),
                "events":     _dash_sensor.events,
                "stats":      _dash_sensor.stats(),
                "summary":    _dash_sensor.summary(),
            }

        # Build waypoints payload (deposition points only)
        _job_stage(jid, "Building waypoints payload…", 92)
        waypoints = [
            {
                "x": d["x"], "y": d["y"], "z": d["z"],
                "layer":      d["layer_num"],
                "temp_C":     d["temp_C"],
                "heat_index": d["heat_index"],
                "t_elapsed":  d["t_elapsed"],
                "speed":      d["speed"],
            }
            for d in analyzer.thermal_data
        ]

        # Ghost full-path (downsampled, all waypoints)
        _job_stage(jid, "Downsampling ghost path…", 96)
        MAX_GHOST = 10000
        all_wps = analyzer.waypoints
        step = max(1, len(all_wps) // MAX_GHOST)
        full_path = [
            {"x": wp["x"], "y": wp["y"], "z": wp["z"]}
            for wp in all_wps[::step]
        ]

        temps = [d["temp_C"] for d in analyzer.thermal_data] if analyzer.thermal_data else [25]
        thermal_range = {"min_temp": round(min(temps), 1), "max_temp": round(max(temps), 1)}

        result = {
            "waypoints":        waypoints,
            "full_path":        full_path,
            "num_layers":       analyzer.num_layers,
            "total_waypoints":  len(all_wps),
            "deposition_count": len(analyzer.thermal_data),
            "thermal_range":    thermal_range,
            "part_name":        analyzer.user["part_name"],
            "sensor_snapshot":  sensor_snapshot,
        }
        _job_update(jid, status="done", pct=100, stage="Complete", result=result)

    except Exception as e:
        import traceback
        _job_update(jid, status="error",
                    error=str(e), trace=traceback.format_exc())
    finally:
        try: os.unlink(zip_path)
        except Exception: pass
        if tmp_csv_path:
            try: os.unlink(tmp_csv_path)
            except Exception: pass


@app.route("/api/jobs/<jid>", methods=["GET"])
def job_status(jid):
    j = _job_get(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    # Return progress without heavy result payload
    return jsonify({
        "id":        j.get("id"),
        "kind":      j.get("kind"),
        "status":    j.get("status"),
        "stage":     j.get("stage"),
        "pct":       round(j.get("pct") or 0, 1),
        "elapsed_s": round(j.get("elapsed_s") or 0, 1),
        "eta_s":     round(j["eta_s"], 1) if j.get("eta_s") is not None else None,
        "error":     j.get("error"),
    })


@app.route("/api/jobs/<jid>/stop", methods=["POST"])
def job_stop(jid):
    j = _job_get(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    if j.get("status") == "running":
        _job_update(jid, status="error", error="Stopped by user")
    return jsonify({"ok": True})


@app.route("/api/jobs/<jid>/result", methods=["GET"])
def job_result(jid):
    j = _job_get(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    if j.get("status") == "error":
        return jsonify({"error": j.get("error"), "trace": j.get("trace")}), 500
    if j.get("status") != "done":
        return jsonify({"error": "job not complete", "status": j.get("status")}), 409
    return jsonify(j.get("result") or {})


# ── OFFLINE REPLAY ────────────────────────────────────────────────────────────

def _is_sensor_csv(path: str) -> bool:
    """Quick header check: does this CSV look like a Meltio sensor file?"""
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            hdrs = {h.lower().strip() for h in (csv.DictReader(f).fieldnames or []) if h}
        sensor_indicators = {'laserpower', 'feedspeed', 'temp1', 'loadcell', 'argon', 'current1'}
        return 'tiempo' in hdrs and bool(hdrs & sensor_indicators)
    except Exception:
        return False


@app.route("/api/offline/load", methods=["POST"])
def offline_load():
    """Fast RAPID + sensor sync. Accepts uploaded CSV files OR a server directory path."""
    if "zip_file" not in request.files:
        return jsonify({"error": "No RAPID ZIP file provided"}), 400

    zip_f              = request.files["zip_file"]
    dir_path           = request.form.get("dir_path", "").strip()
    csv_files_uploaded = request.files.getlist("csv_files")

    has_uploads = any(f.filename for f in csv_files_uploaded)
    if not dir_path and not has_uploads:
        return jsonify({"error": "Provide either a directory path or sensor CSV files"}), 400

    tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    zip_f.save(tmp_zip.name); tmp_zip.close()
    tmp_csv_paths = []
    skipped_files = []

    try:
        # ── 1. Parse RAPID ───────────────────────────────────────────────────
        analyzer = MeltioDEDAnalyzer(tmp_zip.name)
        analyzer.part_name = Path(zip_f.filename).stem
        if not analyzer.extract_and_read():
            return jsonify({"error": "Failed to extract ZIP — no .mod files found"}), 400
        analyzer.parse_rapid_code()

        dep_wps = [wp for wp in analyzer.waypoints if wp["is_deposition"]]
        t_acc, wp_payload, prev = 0.0, [], None
        for wp in dep_wps:
            if prev:
                dist = math.sqrt((wp["x"]-prev["x"])**2+(wp["y"]-prev["y"])**2+(wp["z"]-prev["z"])**2)
                t_acc += dist / max(0.1, wp["speed"])
            wp_payload.append({"x":wp["x"],"y":wp["y"],"z":wp["z"],"layer":wp["layer_num"],
                                "t_elapsed":round(t_acc,3),"speed":wp["speed"],"material":wp["material"]})
            prev = wp

        wp_display = wp_payload[::max(1, len(wp_payload)//6000)]
        all_wps    = analyzer.waypoints
        full_path  = [{"x":w["x"],"y":w["y"],"z":w["z"]} for w in all_wps[::max(1,len(all_wps)//8000)]]

        # ── 2. Collect sensor CSV paths ──────────────────────────────────────
        sensor_paths = []   # list of (display_name, file_path)

        if dir_path:
            p = Path(dir_path)
            if not p.is_dir():
                return jsonify({"error": f"Directory not found: {dir_path}"}), 400
            candidates = sorted(set(p.glob("*.csv")) | set(p.glob("**/*.csv")))
            for cp in candidates:
                if _is_sensor_csv(str(cp)):
                    sensor_paths.append((cp.name, str(cp)))
                else:
                    skipped_files.append(cp.name)
            if not sensor_paths:
                return jsonify({
                    "error": f"No sensor CSV files found in directory",
                    "skipped_files": skipped_files,
                    "hint": f"Scanned {len(candidates)} CSV file(s) — none had the expected sensor columns (tiempo, laserPower, temp1…)"
                }), 400
        else:
            for csv_f in csv_files_uploaded:
                if not csv_f.filename:
                    continue
                tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
                csv_f.save(tmp.name); tmp.close()
                tmp_csv_paths.append(tmp.name)
                if _is_sensor_csv(tmp.name):
                    sensor_paths.append((csv_f.filename, tmp.name))
                else:
                    skipped_files.append(csv_f.filename)

        if not sensor_paths:
            return jsonify({"error": "No sensor CSV files found", "skipped_files": skipped_files}), 400

        # ── 3. Parse + sort + merge ──────────────────────────────────────────
        def _parse_ts(s):
            for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%H:%M:%S.%f', '%H:%M:%S'):
                try: return datetime.strptime(s, fmt)
                except ValueError: pass
            return None

        per_file      = []
        file_summaries = []
        for fname, fpath in sensor_paths:
            sa = SensorAnalyzer()
            sa.connect(fpath)
            if not sa.rows:
                skipped_files.append(f"{fname} (empty)")
                continue
            first_ts = _parse_ts(sa.rows[0]["tiempo"])
            per_file.append((fname, sa.rows, first_ts))
            file_summaries.append({"filename": fname, "rows": len(sa.rows),
                                    "start_time": sa.rows[0]["tiempo"],
                                    "end_time":   sa.rows[-1]["tiempo"]})

        if not per_file:
            return jsonify({"error": "All sensor CSV files were empty after parsing"}), 400

        per_file.sort(key=lambda x: x[2] or datetime.min)

        seen_ts, merged_rows = set(), []
        for _, rows, _ in per_file:
            for row in rows:
                ts = row.get("tiempo", "")
                if ts and ts not in seen_ts:
                    seen_ts.add(ts); merged_rows.append(row)
        merged_rows.sort(key=lambda r: r.get("tiempo", ""))

        sensor        = SensorAnalyzer()
        sensor.rows   = merged_rows
        sensor.events = sensor._find_events(merged_rows)

        # ── 4. Sync to startDeposition ───────────────────────────────────────
        start_ev = next((e for e in sensor.events if e["flag"] == "startDeposition"), None)
        if not start_ev:
            return jsonify({"error": "No startDeposition event found — cannot sync to RAPID"}), 400

        t_start_dt = _parse_ts(start_ev["time"])
        if t_start_dt is None:
            return jsonify({"error": f"Cannot parse start timestamp: {start_ev['time']}"}), 400

        # ── 5. Build relative timeline ───────────────────────────────────────
        rows_rel = []
        for row in sensor.downsample(3000):
            t_abs = _parse_ts(row["tiempo"])
            if t_abs is None: continue
            t_rel = (t_abs - t_start_dt).total_seconds()
            if t_rel < -60: continue
            r = {k: v for k, v in row.items() if k != "tiempo"}
            r["t_rel"] = round(t_rel, 2)
            rows_rel.append(r)

        t_vals = [r["t_rel"] for r in rows_rel]
        groups = {}
        for group, cols in SensorAnalyzer.GROUPS.items():
            groups[group] = {col: [r.get(col) for r in rows_rel] for col in cols}

        events_rel = []
        for ev in sensor.events:
            t_abs = _parse_ts(ev["time"])
            if t_abs is None: continue
            events_rel.append({"t_rel": round((t_abs-t_start_dt).total_seconds(),2),
                                "color": ev["color"], "label": ev["label"], "flag": ev["flag"]})

        rapid_dur  = wp_payload[-1]["t_elapsed"] if wp_payload else 0
        t_last_abs = _parse_ts(merged_rows[-1]["tiempo"])
        sensor_dur = (t_last_abs - t_start_dt).total_seconds() if t_last_abs else 0

        return jsonify({
            "ok": True, "part_name": analyzer.part_name,
            "num_layers": analyzer.num_layers,
            "waypoints": wp_display, "full_path": full_path,
            "total_dep_wps": len(dep_wps),
            "sensor_timeline": {"times": t_vals, "groups": groups},
            "events": events_rel,
            "sensor_summary": sensor.summary(),
            "sensor_stats":   sensor.stats(),
            "file_summaries": file_summaries,
            "skipped_files":  skipped_files,
            "sync": {
                "start_event_time":    start_ev["time"],
                "rapid_duration_sec":  round(rapid_dur, 1),
                "sensor_duration_sec": round(sensor_dur, 1),
                "files_merged":        len(per_file),
                "total_rows_merged":   len(merged_rows),
            },
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
    finally:
        try: os.unlink(tmp_zip.name)
        except Exception: pass
        for p in tmp_csv_paths:
            try: os.unlink(p)
            except Exception: pass


# ── STRESS ANALYSIS ───────────────────────────────────────────────────────────

@app.route("/api/viz/3d-combined", methods=["GET"])
def viz_3d_combined():
    """Return downsampled thermal+stress data for combined 3D visualization."""
    import glob, pathlib

    # Find the latest thermal heatmap CSV
    out_dir = BASE_DIR / "outputs"
    csvs = sorted(glob.glob(str(out_dir / "heatmap_*.csv")), key=os.path.getmtime, reverse=True)
    if not csvs:
        return jsonify({'error': 'No heatmap CSV found'}), 404
    csv_path = csvs[0]

    # Read + downsample thermal data
    points = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i % 43 == 0:
                points.append({
                    'x': float(row['x']),
                    'y': float(row['y']),
                    'z': float(row['z']),
                    'temp_C': float(row['temp_C']),
                    'layer': int(row['layer_num']),
                    'material': row.get('material', 'T0'),
                })

    if not points:
        return jsonify({'error': 'Empty CSV'}), 500

    # Compute stress per layer using the ISM engine with coil params
    stress_payload = {
        'material': {'E_GPa': 200, 'yield_MPa': 480, 'alpha_1e6': 12,
                     'density': 7850, 'Cp': 490, 'k': 50, 'T_melt_C': 1480},
        'process':  {'laser_power': 900, 'scan_speed': 11, 'layer_height': 0.6,
                     'bead_width': 2.0, 'absorptivity': 0.35, 'ambient_T': 25,
                     'dwell_time': 25, 'wire_diameter': 1.2, 'wire_feed_speed': 15},
        'geometry': {'num_layers': max(p['layer'] for p in points), 'wall_thickness': 2.0},
    }
    stress_result = _run_stress_compute(stress_payload) or {}
    # Build layer → stress AND distortion lookups
    stress_by_layer = {
        pl['layer']: pl['sigma_MPa']
        for pl in stress_result.get('per_layer', [])
    }
    distort_by_layer = {
        pl['layer']: pl['delta_mm']
        for pl in stress_result.get('per_layer', [])
    }

    # Attach stress + distortion to each point (map layer proportionally if needed)
    max_layer  = max(p['layer'] for p in points)
    n_computed = len(stress_result.get('per_layer', []))
    for p in points:
        # Map actual layer → computed layer index (handles coil layer numbering)
        mapped = max(1, min(n_computed, round(p['layer'] / max(max_layer, 1) * n_computed)))
        p['stress_MPa']  = stress_by_layer.get(mapped, 0)
        p['distort_mm']  = distort_by_layer.get(mapped, 0)

    return jsonify({
        'ok': True,
        'points': points,
        'n_total': len(points),
        'thermal_range':  [round(min(p['temp_C']     for p in points), 1), round(max(p['temp_C']     for p in points), 1)],
        'stress_range':   [round(min(p['stress_MPa'] for p in points), 1), round(max(p['stress_MPa'] for p in points), 1)],
        'distort_range':  [round(min(p['distort_mm'] for p in points), 2), round(max(p['distort_mm'] for p in points), 2)],
        'csv_file': pathlib.Path(csv_path).name,
    })

@app.route("/api/dev/cached-validation", methods=["GET"])
def dev_cached_validation():
    """Return the last cached validation result (dev helper)."""
    import pathlib
    p = pathlib.Path(tempfile.gettempdir()) / 'validation_compact.json'
    if not p.exists():
        return jsonify({'error': 'No cached result'}), 404
    return app.response_class(p.read_text(), mimetype='application/json')

@app.route("/api/postprint/validate", methods=["POST"])
def postprint_validate():
    """
    Compare pre-print predicted temperatures to actual sensor readings.
    Body: { thermal_layers: [{layer, avg_temp_C}], sensor_csv: "<csv text>" }
    Returns per-layer predicted vs actual comparison.
    """
    try:
        data = request.get_json() or {}
        thermal_layers = data.get('thermal_layers', [])   # [{layer, avg_temp_C}, ...]
        sensor_csv_text = data.get('sensor_csv', '')

        if not thermal_layers or not sensor_csv_text:
            return jsonify({'error': 'Need thermal_layers and sensor_csv'}), 400

        # Parse sensor CSV
        import io
        reader = csv.DictReader(io.StringIO(sensor_csv_text))
        rows = []
        for row in reader:
            try:
                temp_vals = []
                for ch in ['temp1','temp2','temp3','temp4','temp5','temp6','temp7','temp8','temp9']:
                    v = row.get(ch, '').strip()
                    if v:
                        temp_vals.append(float(v))
                if temp_vals:
                    t_raw = row.get('tiempo', row.get('time', ''))
                    try:
                        t_val = float(t_raw)
                    except (ValueError, TypeError):
                        t_val = float(len(rows))  # use row index as time proxy
                    rows.append({
                        'time': t_val,
                        'avg_temp': sum(temp_vals) / len(temp_vals),
                        'max_temp': max(temp_vals),
                        'channels': len(temp_vals),
                    })
            except (ValueError, KeyError):
                continue

        if not rows:
            return jsonify({'error': 'No valid temperature rows in CSV'}), 400

        n_layers = len(thermal_layers)
        n_rows   = len(rows)
        comparison = []
        max_dev = 0.0
        total_dev = 0.0

        for i, tl in enumerate(thermal_layers):
            # Map layer → sensor row bucket (linear distribution across print duration)
            frac_start = i / n_layers
            frac_end   = (i + 1) / n_layers
            idx_start  = int(frac_start * n_rows)
            idx_end    = max(idx_start + 1, int(frac_end * n_rows))
            bucket     = rows[idx_start:idx_end]

            actual_C = sum(r['avg_temp'] for r in bucket) / len(bucket) if bucket else None
            pred_C   = tl.get('avg_temp_C', 0)
            if actual_C is None:
                continue

            dev_C  = round(pred_C - actual_C, 1)
            dev_pct = round(abs(dev_C) / max(actual_C, 1) * 100, 1)
            max_dev = max(max_dev, abs(dev_C))
            total_dev += abs(dev_C)

            comparison.append({
                'layer':        tl.get('layer', i + 1),
                'predicted_C':  round(pred_C, 1),
                'actual_C':     round(actual_C, 1),
                'deviation_C':  dev_C,
                'deviation_pct': dev_pct,
                'risk': 'HIGH' if dev_pct > 20 else ('MEDIUM' if dev_pct > 10 else 'LOW'),
            })

        n = len(comparison)
        accuracy_pct = round(max(0, 100 - (total_dev / n / max(max(tl.get('avg_temp_C',1) for tl in thermal_layers), 1) * 100)), 1) if n else 0

        return jsonify({
            'ok': True,
            'comparison': comparison,
            'summary': {
                'n_layers':       n,
                'accuracy_pct':   accuracy_pct,
                'max_deviation_C':  round(max_dev, 1),
                'avg_deviation_C':  round(total_dev / n, 1) if n else 0,
                'sensor_rows':    len(rows),
            }
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route("/api/stress/compute", methods=["POST"])
def stress_compute():
    """Residual-stress + distortion prediction."""
    try:
        result = compute_stress(request.get_json() or {})
        if result is None:
            return jsonify({'error': 'Stress computation failed'}), 500
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


def _build_sensitivity_payload(job: dict) -> dict:
    """Build the stress payload for a sensitivity sweep from a completed job."""
    result     = job.get('result', {})
    stress_res = result.get('stress') or {}
    up         = result.get('user_params', {})
    db_mat     = result.get('db_materials', {}).get('T0') or {}
    # Try mat_T0 key (stored by frontend sync) then fallback to db display_name
    mat_name   = up.get('mat_T0') or db_mat.get('display_name', '')
    mech       = lookup_mech(mat_name)
    return {
        'material': {
            'E_GPa':     mech.get('E_GPa',      db_mat.get('E_GPa',      200)),
            'yield_MPa': mech.get('yield_MPa',  db_mat.get('yield_MPa',  400)),
            'alpha_1e6': mech.get('alpha_1e6',  db_mat.get('CTE',         12)),
            'k':         db_mat.get('thermal_conductivity', 20),
            'density':   db_mat.get('density',  7800),
            'Cp':        db_mat.get('specific_heat', 490),
            'T_melt':    db_mat.get('melting_point', 1400),
        },
        'process': {
            'laser_power':    up.get('laser_power',    1000),
            'scan_speed':     up.get('scan_speed',       10),
            'wire_feed_speed':up.get('feed_speed',       80),
            'layer_height':   up.get('layer_height',    0.8),
            'absorption':     db_mat.get('absorption_450nm', 0.35),
            'ambient_temp':   up.get('ambient_temp',     25),
            'dwell_time':     up.get('dwell_time',       10),
            'bead_width':     up.get('layer_width',      2.0),
            'wire_diameter':  up.get('wire_diameter',    1.2),
        },
        'geometry': {
            'num_layers':     result.get('num_layers', 50),
            'wall_thickness': stress_res.get('summary', {}).get('wall_t_mm', 5.0),
        },
        'waypoints': [],
    }


def _run_sensitivity_bg(job_id: str):
    """Background thread: run sweep and cache result back into the job."""
    job = _job_get(job_id)
    if not job:
        return
    try:
        payload = _build_sensitivity_payload(job)
        sweep   = run_sensitivity_sweep(payload)
        # Store into result so future GET hits the cache immediately
        with _jobs_lock:
            j = _jobs.get(job_id)
            if j:
                j.setdefault('result', {})['_sensitivity'] = sweep
    except Exception:
        pass


@app.route('/api/sensitivity/<job_id>', methods=['GET'])
def sensitivity_for_job(job_id):
    """
    Return adaptive sensitivity sweep for a completed job.
    First call triggers a background computation; subsequent calls
    return the cached result immediately (~instant).
    """
    job = _job_get(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'Job not ready'}), 400

    cached = job.get('result', {}).get('_sensitivity')
    if cached:
        return jsonify(cached)

    # Not cached yet — run synchronously (first call) and cache
    try:
        payload = _build_sensitivity_payload(job)
        sweep   = run_sensitivity_sweep(payload)
        with _jobs_lock:
            j = _jobs.get(job_id)
            if j:
                j.setdefault('result', {})['_sensitivity'] = sweep
        return jsonify(sweep)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route("/api/sensitivity", methods=["POST"])
def sensitivity_check():
    """
    Fast pre-print sensitivity sweep (~100 ms, no ZIP).
    Body JSON: {material_name?, material?, process, geometry, tolerances: {param: pct}}.
    If material_name is provided, resolves mechanical properties via lookup_mech.
    Returns {summary, go_nogo, sens_sweep}.
    """
    try:
        body            = request.get_json(force=True) or {}
        tolerances      = body.pop('tolerances', None)
        material_name   = body.pop('material_name', None)

        # Resolve material from name if not already supplied
        if material_name and not body.get('material', {}).get('E_GPa'):
            mech   = lookup_mech(material_name)
            db_all = load_materials_db()
            db_mat = next((m for m in db_all
                           if material_name.lower() in m.get('display_name', '').lower()
                           or any(material_name.lower() in a for a in m.get('aliases', []))),
                          {})
            body['material'] = {
                'E_GPa':     mech.get('E_GPa',      200),
                'yield_MPa': mech.get('yield_MPa',  400),
                'alpha_1e6': mech.get('alpha_1e6',   12),
                'k':         db_mat.get('thermal_conductivity', 20),
                'density':   db_mat.get('density',  7800),
                'Cp':        db_mat.get('specific_heat', 490),
                'T_melt':    db_mat.get('melting_point', 1400),
            }

        result = compute_stress(body, _sweep=True, tolerances=tolerances)
        if result is None:
            return jsonify({'error': 'Computation failed — check parameters'}), 400
        return jsonify({
            'summary':    result.get('summary', {}),
            'go_nogo':    result.get('go_nogo', '—'),
            'sens_sweep': result.get('sens_sweep', []),
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# M600 G-CODE ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/m600/analyze", methods=["POST"])
def m600_analyze():
    """
    Accept a .gcode file (or .zip containing one) and run the full
    M600 analysis pipeline (same thermal + stress engine as Robot).
    """
    if "gcode_file" not in request.files:
        return jsonify({"error": "No gcode_file field in request"}), 400

    f = request.files["gcode_file"]
    suffix = ".zip" if f.filename.lower().endswith(".zip") else ".gcode"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.save(tmp.name)
    tmp.close()

    form_data = dict(request.form)
    filename  = f.filename

    _cleanup_outputs()
    _cleanup_jobs()
    jid = _new_job("m600_analyze")
    t = threading.Thread(
        target=_run_m600_analysis_job,
        args=(jid, tmp.name, filename, form_data),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": jid})


def _run_m600_analysis_job(jid: str, gcode_path: str, filename: str, form: dict):
    """Background job — M600 analysis using M600GcodeAnalyzer."""
    try:
        _job_stage(jid, "Reading G-code…", 3)
        analyzer = M600GcodeAnalyzer(gcode_path)
        analyzer.part_name = Path(filename).stem.replace('.gcode', '')

        if not analyzer.extract_and_read():
            raise Exception("Failed to read G-code file")

        _job_stage(jid, "Parsing G-code…", 8)
        analyzer.parse_rapid_code()   # calls _parse_gcode internally

        _job_stage(jid, "Matching materials…", 14)
        db = load_materials_db()

        # Auto-fill form params from G-code header when not supplied
        hdr = analyzer.header_meta
        def _f(key, default):
            try: return float(form.get(key) or default)
            except (ValueError, TypeError): return float(default)
        analyzer.user = {
            "part_name":          form.get("part_name") or analyzer.part_name,
            "laser_power":        form.get("laser_power") or str(hdr.get("laser_power", 1000)),
            "feed_speed":         _f("feed_speed",         hdr.get("feed_speed",  12.5)),
            "layer_height":       _f("layer_height",       hdr.get("layer_height", 0.6)),
            "layer_width":        _f("layer_width",        hdr.get("layer_width",  2.0)),
            "wire_diameter":      _f("wire_diameter",      hdr.get("wire_diameter", 0.98)),
            "material_T0":        form.get("material_T0") or hdr.get("material_T0", ""),
            "material_T1":        form.get("material_T1") or hdr.get("material_T1", ""),
            "inert_environment":  form.get("inert_environment", "false").lower() == "true",
            "ambient_temp":       _f("ambient_temp",        25),
            "min_layer_dwell":    _f("min_layer_dwell",      0),
            "beam_spot_diameter": _f("beam_spot_diameter",  1.2),
        }

        for feeder in ["T0", "T1"]:
            raw = analyzer.user.get(f"material_{feeder}", "")
            match = fuzzy_match_material(raw, db) if raw else None
            analyzer.db_materials[feeder] = match or ({
                "display_name": raw or "Unknown",
                "thermal_conductivity": 15.0,
                "density": 7000, "specific_heat": 500,
                "melting_point": 1400, "notes": "Custom"
            } if raw else None)

        _job_stage(jid, "Thermal simulation (starting)…", 18)
        _tee_local.tee = _ProgressTee(jid, 18, 65)
        try:
            analyzer.calculate_thermal_data()
        finally:
            _tee_local.tee = None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _job_stage(jid, "Exporting CSV…", 68)
        v_csv  = analyzer.generate_csv(ts)
        _job_stage(jid, "Generating 3D visualisation…", 75)
        v_3d   = analyzer.generate_3d_html(ts)
        _job_stage(jid, "Generating process window…", 82)
        v_pw   = analyzer.generate_process_window_html(ts)
        _job_stage(jid, "Generating animation…", 88)
        v_anim = analyzer.generate_animation_html(ts)
        _job_stage(jid, "Generating report…", 94)
        viz = {"csv": v_csv, "3d": v_3d, "pw": v_pw, "anim": v_anim}
        report_md   = analyzer.generate_report(viz)
        report_path = analyzer.save_report(report_md, ts)

        # ── Auto stress estimate ───────────────────────────────────────────
        _job_stage(jid, "Computing stress estimate…", 95)
        import time as _time_mod; _t97 = _time_mod.time()
        _auto_stress = run_auto_stress(analyzer)

        _job_stage(jid, "Generating distortion animation…", 97)
        v_distort = analyzer.generate_distortion_animation_html(_auto_stress or {}, ts) if _auto_stress else ""
        viz["distort"] = v_distort
        viz["mesh"] = ""

        hi_vals    = [d["heat_index"]    for d in analyzer.thermal_data] or [0]
        temp_vals  = [d["temp_C"]        for d in analyzer.thermal_data] if analyzer.thermal_data else [25]
        curv_vals  = [d["curvature_deg"] for d in analyzer.thermal_data] if analyzer.thermal_data else [0]
        ved_vals   = [d.get("VED", 0)    for d in analyzer.thermal_data] if analyzer.thermal_data else [0]
        nh_vals    = [d.get("norm_H", 0) for d in analyzer.thermal_data] if analyzer.thermal_data else [0]
        cr_vals    = [d.get("cracking_score", 0) for d in analyzer.thermal_data] if analyzer.thermal_data else [0]
        dep_speeds = [d["speed"]         for d in analyzer.thermal_data]
        pt = analyzer._print_time_summary()

        # Pre-compute scalar bounds once — avoids O(n²) inside generator expressions
        _t_min = min(temp_vals);  _t_max = max(temp_vals)
        _hi_min = min(hi_vals);   _hi_max = max(hi_vals)
        _hotspot_thresh = _t_min + (_t_max - _t_min) * 0.8

        result = {
            "part_name": analyzer.user["part_name"],
            "num_layers": analyzer.num_layers,
            "materials_found": sorted(analyzer.materials_found),
            "total_waypoints": len(analyzer.waypoints),
            "deposition_waypoints": len(analyzer.thermal_data),
            "material_changes": analyzer.material_changes,
            "warnings": [e["warning"] for e in analyzer.material_changes if e.get("warning")],
            "speeds_detected": sorted(set(round(s,1) for s in analyzer.all_speeds)),
            "speed_range": [min(analyzer.all_speeds), max(analyzer.all_speeds)] if analyzer.all_speeds else [0,0],
            "heat_index": {
                "min": round(_hi_min, 4),
                "max": round(_hi_max, 4),
                "avg": round(sum(hi_vals)/len(hi_vals), 4),
            },
            "temperature": {
                "min":        round(_t_min, 1),
                "max":        round(_t_max, 1),
                "avg":        round(sum(temp_vals)/len(temp_vals), 1),
                "unit":       "°C",
                "hotspots":   sum(1 for t in temp_vals if t > _hotspot_thresh),
                "curv_zones": sum(1 for c in curv_vals if c > 30),
                "model":      "Rykalin moving heat source + Ar convection (35 W/m²K)",
            },
            "volume_v1": round(math.pi * (analyzer.user["wire_diameter"]/2)**2 * analyzer.user["feed_speed"], 4),
            "volume_v2": round(analyzer.user["layer_width"] * analyzer.user["layer_height"] * (sum(dep_speeds)/len(dep_speeds) if dep_speeds else 0), 4),
            "db_materials": {
                k: ({"display_name": v["display_name"], "thermal_conductivity": v["thermal_conductivity"],
                     "density": v["density"], "specific_heat": v["specific_heat"],
                     "melting_point": v.get("melting_point", 1400),
                     "absorption_450nm": v.get("absorption_450nm", 0.35)}
                    if v else None)
                for k, v in analyzer.db_materials.items()
            },
            "io_signals": [],      # M600 has no ABB I/O signals
            "print_time": pt,
            "inert_environment": analyzer.user["inert_environment"],
            "anomalies": {
                "lof_zones":          sum(1 for d in analyzer.thermal_data if d.get("lof_risk")),
                "keyhole_zones":      sum(1 for d in analyzer.thermal_data if d.get("keyhole_risk")),
                "overheat_zones":     sum(1 for d in analyzer.thermal_data if d.get("overheat_risk")),
                "lof_depth_zones":    sum(1 for d in analyzer.thermal_data if d.get("lof_depth_risk")),
                "max_cracking_score": round(max(cr_vals), 3) if cr_vals else 0,
                "avg_VED":            round(sum(ved_vals)/len(ved_vals), 1) if ved_vals else 0,
                "avg_norm_H":         round(sum(nh_vals)/len(nh_vals), 2) if nh_vals else 0,
                "beam_spot_mm":       analyzer.user.get("beam_spot_diameter", 1.2),
            },
            "viz_files": {
                "csv":    f"/outputs/{Path(viz['csv']).name}"     if viz.get("csv")     else None,
                "3d":     f"/outputs/{Path(viz['3d']).name}"      if viz.get("3d")      else None,
                "pw":     f"/outputs/{Path(viz['pw']).name}"      if viz.get("pw")      else None,
                "anim":   f"/outputs/{Path(viz['anim']).name}"    if viz.get("anim")    else None,
                "distort":f"/outputs/{Path(viz['distort']).name}" if viz.get("distort") else None,
                "mesh":   f"/outputs/{Path(viz['mesh']).name}"   if viz.get("mesh")    else None,
            },
            "report_path": f"/outputs/{Path(report_path).name}",
            "report_md": report_md,
            # M600-specific extras
            "platform": "M600",
            "gcode_header": analyzer.header_meta,
        }
        if _auto_stress:
            result['stress'] = _auto_stress

        # Process params snapshot for frontend cross-page sync
        try:
            _lp_m = float(str(analyzer.user.get('laser_power', 1000)).replace('W','').strip())
        except (ValueError, TypeError):
            _lp_m = 1000.0
        _avg_scan_m = (sum(dep_speeds) / len(dep_speeds)) if dep_speeds else 10.0
        result['user_params'] = {
            'laser_power':   _lp_m,
            'feed_speed':    analyzer.user.get('feed_speed',   12),
            'scan_speed':    round(_avg_scan_m, 2),
            'layer_height':  analyzer.user.get('layer_height',  0.5),
            'layer_width':   analyzer.user.get('layer_width',   2.0),
            'wire_diameter': analyzer.user.get('wire_diameter', 1.2),
            'ambient_temp':  analyzer.user.get('ambient_temp',  25),
            'dwell_time':    analyzer.user.get('min_layer_dwell', 0),
            'beam_spot':     analyzer.user.get('beam_spot_diameter', 1.2),
            'mat_T0':        analyzer.user.get('material_T0', ''),
            'mat_T1':        analyzer.user.get('material_T1', ''),
            'environment':   'inert' if analyzer.user.get('inert_environment') else 'regular',
        }

        _job_update(jid, status="done", pct=100, stage="Complete", result=result)
        print(f"[M600] job complete in {_time_mod.time()-_t97:.1f}s at 97%→100%")

    except Exception as e:
        import traceback
        print(f"[M600] EXCEPTION: {e}")
        _job_update(jid, status="error",
                    error=str(e), trace=traceback.format_exc())
    finally:
        try: os.unlink(gcode_path)
        except Exception: pass


@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    """Gracefully stop all background jobs then kill the process."""
    # Cancel any pending/running jobs
    with _jobs_lock:
        for jid, job in list(_jobs.items()):
            if job.get("status") not in ("done", "error"):
                job["status"] = "error"
                job["error"]  = "Server shutting down"
    # Stop the live-file watcher if running
    try:
        _watcher.reset()
    except Exception:
        pass
    # Delay exit slightly so the HTTP response is flushed first
    def _do_exit():
        import time
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_do_exit, daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    import socket, io
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    port = int(os.environ.get("PORT", 5050))
    local_ip = socket.gethostbyname(socket.gethostname())
    print("Meltio DED Analyzer")
    print(f"   Local:   http://localhost:{port}/ded")
    print(f"   Network: http://{local_ip}:{port}/ded")
    app.run(debug=False, host="0.0.0.0", port=port)
