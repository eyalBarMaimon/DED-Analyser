"""
Shared job logic for RAPID and M600 analysis pipelines.
"""
import math
from pathlib import Path
from engines.stress_engine import compute_stress, lookup_mech, estimate_wall_thickness


def build_user_params(form: dict, analyzer, defaults: dict) -> dict:
    """Normalise form input into user params dict, falling back to defaults."""
    def _f(key, fallback=0):
        val = form.get(key) or defaults.get(key, fallback)
        try:
            return float(val)
        except (TypeError, ValueError):
            return float(fallback)

    return {
        "part_name":          form.get("part_name") or analyzer.part_name,
        "laser_power":        form.get("laser_power") or str(defaults.get("laser_power", 1000)),
        "feed_speed":         _f("feed_speed",         defaults.get("feed_speed",         12.5)),
        "layer_height":       _f("layer_height",       defaults.get("layer_height",         0.6)),
        "layer_width":        _f("layer_width",        defaults.get("layer_width",          2.0)),
        "wire_diameter":      _f("wire_diameter",      defaults.get("wire_diameter",        1.2)),
        "material_T0":        form.get("material_T0", ""),
        "material_T1":        form.get("material_T1", ""),
        "inert_environment":  form.get("inert_environment", "false").lower() == "true",
        "ambient_temp":       _f("ambient_temp",       25),
        "min_layer_dwell":    _f("min_layer_dwell",     0),
        "beam_spot_diameter": _f("beam_spot_diameter",  1.2),
    }


def match_materials(analyzer, db) -> None:
    """Fuzzy-match T0/T1 names against materials DB and populate analyzer.db_materials."""
    from meltio_ded_analyzer import fuzzy_match_material
    for feeder in ["T0", "T1"]:
        raw = analyzer.user.get(f"material_{feeder}", "")
        match = fuzzy_match_material(raw, db) if raw else None
        analyzer.db_materials[feeder] = match or (
            {"display_name": raw or "Unknown", "thermal_conductivity": 15.0,
             "density": 7000, "specific_heat": 500, "melting_point": 1400, "notes": "Custom"}
            if raw else None
        )


def run_auto_stress(analyzer) -> dict | None:
    """Build stress payload from analyzer state and run ISM engine."""
    try:
        db_mat_T0 = analyzer.db_materials.get("T0") or {}
        mat_name  = db_mat_T0.get('display_name', '')
        mech      = lookup_mech(mat_name)

        dep_speeds_ms = [d['speed'] / 1000 for d in analyzer.thermal_data if d.get('is_deposition')]
        avg_scan = (sum(dep_speeds_ms) / len(dep_speeds_ms)) if dep_speeds_ms else 0.010

        try:
            laser_p_f = float(str(analyzer.user.get('laser_power', '1000')).replace('W', '').strip())
        except (ValueError, AttributeError):
            laser_p_f = 1000.0

        wire_d = analyzer.user.get('wire_diameter', 1.2) or 1.2

        # Sample waypoints evenly across ALL layers (max 2000 for performance)
        all_td = analyzer.thermal_data
        total  = len(all_td)
        step   = max(1, total // 2000)
        wp_sample = all_td[::step][:2000]

        # Auto-compute wall thickness from first 500 sampled waypoints
        wt_input = [
            {'x': d['x'], 'y': d['y'], 'z': d['z'],
             'layer': d['layer_num'], 'is_deposition': True}
            for d in wp_sample[:500]
        ]
        wall_t_mm = estimate_wall_thickness(wt_input, wire_d)

        # Use material DB's absorption if available, else default
        absorb = db_mat_T0.get('absorption_450nm', 0.35)

        payload = {
            'material': {
                'E_GPa':     mech.get('E_GPa',     200),
                'yield_MPa': mech.get('yield_MPa', 400),
                'alpha_1e6': mech.get('alpha_1e6', db_mat_T0.get('CTE', 12.0)),
                'k':         db_mat_T0.get('thermal_conductivity', 20),
                'density':   db_mat_T0.get('density', 7800),
                'Cp':        db_mat_T0.get('specific_heat', 490),
                'T_melt':    db_mat_T0.get('melting_point', 1400),
            },
            'process': {
                'laser_power':     laser_p_f,
                'scan_speed':      avg_scan * 1000,
                'layer_height':    analyzer.user.get('layer_height', 0.5),
                'bead_width':      analyzer.user.get('layer_width', 2.0),
                'absorption':      absorb,
                'ambient_temp':    analyzer.user.get('ambient_temp', 25),
                'dwell_time':      max(analyzer.user.get('min_layer_dwell', 5), 5),
                'wire_diameter':   wire_d,
                'wire_feed_speed': analyzer.user.get('feed_speed', 80) or 80,
            },
            'geometry': {
                'num_layers':     analyzer.num_layers or 50,
                'wall_thickness': wall_t_mm,
            },
            'waypoints': [
                {'x': d['x'], 'y': d['y'], 'z': d['z'], 'layer': d['layer_num']}
                for d in wp_sample
            ],
        }
        return compute_stress(payload)
    except Exception:
        return None


def build_result(analyzer, viz: dict, report_path: str, auto_stress: dict | None,
                 platform: str = 'Robot', extra: dict | None = None) -> dict:
    """Assemble the final result dict returned to the frontend."""
    pt = analyzer._print_time_summary()

    # Single pass over thermal_data — compute all aggregates at once
    td = analyzer.thermal_data
    if td:
        hi_min = hi_max = hi_sum = 0.0
        t_min = t_max = t_sum = 0.0
        spd_sum = 0.0
        curv_zones = lof = kh = oh = lof_d = 0
        ved_sum = nh_sum = cr_max = 0.0
        first = True
        n = len(td)
        for d in td:
            hi = d["heat_index"]; hi_sum += hi
            t  = d["temp_C"];     t_sum  += t
            spd_sum += d["speed"]
            if first:
                hi_min = hi_max = hi
                t_min  = t_max  = t
                first  = False
            else:
                if hi < hi_min: hi_min = hi
                if hi > hi_max: hi_max = hi
                if t  < t_min:  t_min  = t
                if t  > t_max:  t_max  = t
            if d["curvature_deg"] > 30:  curv_zones += 1
            if d.get("lof_risk"):        lof   += 1
            if d.get("keyhole_risk"):    kh    += 1
            if d.get("overheat_risk"):   oh    += 1
            if d.get("lof_depth_risk"):  lof_d += 1
            ved_sum += d.get("VED", 0)
            nh_sum  += d.get("norm_H", 0)
            cr = d.get("cracking_score", 0)
            if cr > cr_max: cr_max = cr
        hotspot_thresh = t_min + (t_max - t_min) * 0.8
        hotspots = sum(1 for d in td if d["temp_C"] > hotspot_thresh)
        avg_speed = spd_sum / n
    else:
        hi_min = hi_max = hi_sum = 0
        t_min = t_max = t_sum = 25
        spd_sum = avg_speed = 0
        curv_zones = lof = kh = oh = lof_d = 0
        ved_sum = nh_sum = cr_max = 0.0
        hotspots = 0
        n = 1

    result = {
        "part_name":              analyzer.user["part_name"],
        "num_layers":             analyzer.num_layers,
        "materials_found":        sorted(analyzer.materials_found),
        "total_waypoints":        len(analyzer.waypoints),
        "deposition_waypoints":   len(td),
        "material_changes":       analyzer.material_changes,
        "warnings":               [e["warning"] for e in analyzer.material_changes if e.get("warning")],
        "speeds_detected":        sorted(set(round(s, 1) for s in analyzer.all_speeds)),
        "speed_range":            [min(analyzer.all_speeds), max(analyzer.all_speeds)] if analyzer.all_speeds else [0, 0],
        "heat_index":             {"min": round(hi_min, 4), "max": round(hi_max, 4),
                                   "avg": round(hi_sum / max(n, 1), 4)},
        "temperature":            {"min": round(t_min, 1), "max": round(t_max, 1),
                                   "avg": round(t_sum / max(n, 1), 1), "unit": "°C",
                                   "hotspots":   hotspots,
                                   "curv_zones": curv_zones,
                                   "model": "Rykalin moving heat source + Ar convection (35 W/m²K)"},
        "volume_v1":  round(math.pi * (analyzer.user["wire_diameter"] / 2) ** 2 * analyzer.user["feed_speed"], 4),
        "volume_v2":  round(analyzer.user["layer_width"] * analyzer.user["layer_height"] * avg_speed, 4),
        "db_materials": {
            k: ({"display_name": v["display_name"], "thermal_conductivity": v["thermal_conductivity"],
                 "density": v["density"], "specific_heat": v["specific_heat"]} if v else None)
            for k, v in analyzer.db_materials.items()
        },
        "io_signals":         sorted(set(s for sigs in analyzer.digital_ios.values() for s in sigs))
                              if hasattr(analyzer, 'digital_ios') else [],
        "print_time":         pt,
        "inert_environment":  analyzer.user["inert_environment"],
        "anomalies": {
            "lof_zones":          lof,
            "keyhole_zones":      kh,
            "overheat_zones":     oh,
            "lof_depth_zones":    lof_d,
            "max_cracking_score": round(cr_max, 3),
            "avg_VED":            round(ved_sum / max(n, 1), 1),
            "avg_norm_H":         round(nh_sum  / max(n, 1), 2),
            "beam_spot_mm":       analyzer.user.get("beam_spot_diameter", 1.2),
        },
        "viz_files": {
            "csv":  f"/outputs/{Path(viz['csv']).name}"  if viz.get("csv")  else None,
            "3d":   f"/outputs/{Path(viz['3d']).name}"   if viz.get("3d")   else None,
            "pw":   f"/outputs/{Path(viz['pw']).name}"   if viz.get("pw")   else None,
            "anim": f"/outputs/{Path(viz['anim']).name}" if viz.get("anim") else None,
        },
        "report_path": f"/outputs/{Path(report_path).name}",
        "report_md":   getattr(analyzer, '_last_report_md', ''),
        "platform":    platform,
    }
    if auto_stress:
        result['stress'] = auto_stress

    # Process params snapshot — used by frontend to sync between pages
    try:
        lp = float(str(analyzer.user.get('laser_power', 1000)).replace('W', '').strip())
    except (ValueError, TypeError):
        lp = 1000.0
    result['user_params'] = {
        'laser_power':   lp,
        'feed_speed':    analyzer.user.get('feed_speed',   12),
        'scan_speed':    round(avg_speed, 2),   # actual avg deposition speed from RAPID
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

    if extra:
        result.update(extra)
    return result
