"""
Full research matrix: FEM thermal comparison across all materials and parts.
Runs Fast + Fine resolutions.

Usage:  python research_full_matrix.py
Output: research_output/full_matrix_<timestamp>/
  - summary.json       (all results)
  - report.html        (interactive comparison)
  - <part>_<res>.csv   (per-combination detail)
"""
import sys, json, pathlib, time, traceback
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from datetime import datetime
from meltio_ded_analyzer import MeltioDEDAnalyzer, load_materials_db, fuzzy_match_material
from engines.reduced_fem import build_grid, run_simulation

# ── Config ────────────────────────────────────────────────────────────────────

PARTS = {
    "VAZA":     pathlib.Path(__file__).parent / "RAW Data" / "VAZA  SST 316L.zip",
    "Tower":    pathlib.Path(__file__).parent / "RAW Data" / "tower SST316L.zip",
    "Coil":     pathlib.Path(__file__).parent / "RAW Data" / "Coil230426V1.zip",
    "DOME580":  pathlib.Path(__file__).parent / "RAW Data" / "DOME580mm" / "DOME580.zip",
}

RESOLUTIONS = ["fast", "fine"]

# Parts with too many layers — limit to fast only to avoid hours-long runs
FAST_ONLY_PARTS = {"Coil", "DOME580"}  # 1927 and 968 layers — fast only

# For parts with many layers, cap waypoints more aggressively
MAX_WPS_PER_PART = {
    "VAZA":    4000,
    "Tower":   4000,
    "Coil":    500,    # 1927 layers — sample, compare trends not absolute values
    "DOME580": 500,    # 968 layers
}

# For heavy parts, cap num_layers too so FEM loop doesn't run 1927 empty iterations
MAX_LAYERS_PER_PART = {
    "Coil":    200,
    "DOME580": 200,
}

MATERIALS_TO_TEST = [
    "Titanium Ti-6Al-4V",
    "Titanium CP Grade 2",
    "Stainless Steel 316L",
    "Stainless Steel 304",
    "Aluminium 6061",
    "Inconel 625",
    "Inconel 718",
    "Hastelloy C-276",
    "Hastelloy X",
    "ER70S (Mild Steel Wire)",
    "Copper (Pure / ETP)",
]

PROCESS_PARAMS = {
    "laser_power": 1500, "scan_speed": 10, "layer_height": 0.6,
    "bead_width": 2.0, "wire_diameter": 1.2, "beam_spot": 1.2,
    "ambient_temp": 25, "dwell_time": 5,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mat_props(mat_entry):
    return {
        "k":       mat_entry["thermal_conductivity"],
        "density": mat_entry["density"],
        "Cp":      mat_entry["specific_heat"],
        "T_melt":  mat_entry["melting_point"],
    }


def _extract_waypoints(zip_path, params, max_wps=4000):
    import tempfile, os
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.write(zip_path.read_bytes()); tmp.close()
    try:
        ana = MeltioDEDAnalyzer(tmp.name)
        if not ana.extract_and_read():
            raise RuntimeError("Failed to extract ZIP")
        ana.parse_rapid_code()
        ana.user = {
            "part_name": zip_path.stem, "laser_power": str(params["laser_power"]),
            "feed_speed": 12.5, "layer_height": params["layer_height"],
            "layer_width": params["bead_width"], "wire_diameter": params["wire_diameter"],
            "inert_environment": False, "ambient_temp": params["ambient_temp"],
            "min_layer_dwell": params["dwell_time"], "beam_spot_diameter": params["beam_spot"],
        }
        ana.calculate_thermal_data()
        step = max(1, len(ana.thermal_data) // max_wps)
        wps = [{"x": d["x"], "y": d["y"], "z": d["z"],
                "layer": d["layer_num"], "is_deposition": True}
               for d in ana.thermal_data[::step]]
        layers = max((d["layer_num"] for d in ana.thermal_data), default=1)
        return wps, layers
    finally:
        os.unlink(tmp.name)


def _run_fem(waypoints, mat_entry, num_layers, resolution, max_layers=None):
    mat = _mat_props(mat_entry)
    effective_layers = min(num_layers, max_layers) if max_layers else num_layers
    # Re-index waypoints to fit within effective_layers
    if max_layers and num_layers > max_layers:
        step = num_layers / max_layers
        wps_filtered = [wp for wp in waypoints if wp["layer"] <= max_layers * step]
        waypoints = [{**wp, "layer": max(1, min(max_layers, int(wp["layer"] / step)))}
                     for wp in wps_filtered]
    params = dict(PROCESS_PARAMS,
                  num_layers=effective_layers,
                  absorption=mat_entry.get("absorption_450nm", 0.35))
    grid = build_grid(waypoints, params, resolution)
    t0 = time.time()
    result = run_simulation(grid, waypoints, mat, params)
    result["elapsed_s"] = round(time.time() - t0, 1)
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = pathlib.Path(__file__).parent / "research_output" / f"full_matrix_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    db = load_materials_db()

    # Resolve materials
    materials = {}
    for name in MATERIALS_TO_TEST:
        m = fuzzy_match_material(name, db)
        if m:
            materials[name] = m
        else:
            print(f"  WARNING: '{name}' not found in DB — skipped")

    print(f"\n{'='*70}")
    print(f"  Full Matrix Research — {ts}")
    print(f"  Parts: {list(PARTS.keys())}")
    print(f"  Materials: {len(materials)}")
    print(f"  Resolutions: {RESOLUTIONS}")
    print(f"  Total runs: {len(PARTS) * len(materials) * len(RESOLUTIONS)}")
    print(f"{'='*70}\n")

    # Extract waypoints per part (once, shared across all materials & resolutions)
    part_waypoints = {}
    for part_name, zip_path in PARTS.items():
        if not zip_path.exists():
            print(f"  SKIP {part_name} — file not found: {zip_path}")
            continue
        print(f"Extracting {part_name}…")
        try:
            max_wps = MAX_WPS_PER_PART.get(part_name, 4000)
            wps, layers = _extract_waypoints(zip_path, PROCESS_PARAMS, max_wps=max_wps)
            part_waypoints[part_name] = (wps, layers)
            print(f"  -> {len(wps)} waypoints, {layers} layers\n")
        except Exception as e:
            print(f"  ERROR extracting {part_name}: {e}\n")

    # Run FEM
    all_results = {}
    # Count actual runs respecting FAST_ONLY_PARTS
    total = sum(
        len(materials) * (1 if p in FAST_ONLY_PARTS else len(RESOLUTIONS))
        for p in part_waypoints
    )
    done = 0

    for part_name, (wps, layers) in part_waypoints.items():
        all_results[part_name] = {}
        resolutions = ["fast"] if part_name in FAST_ONLY_PARTS else RESOLUTIONS
        if part_name in FAST_ONLY_PARTS:
            print(f"  ({part_name} has {layers} layers — fast only)")
        for mat_name, mat_entry in materials.items():
            all_results[part_name][mat_name] = {}
            for resolution in resolutions:
                done += 1
                label = f"[{done:3d}/{total}] {part_name:<10} | {mat_entry['display_name']:<28} | {resolution}"
                print(label, end="  ", flush=True)
                try:
                    max_layers = MAX_LAYERS_PER_PART.get(part_name)
                    r = _run_fem(wps, mat_entry, layers, resolution, max_layers=max_layers)
                    rc = r["risk_counts"]
                    total_risk = rc["HIGH"] + rc["MEDIUM"] + rc["LOW"]
                    high_pct = round(rc["HIGH"] / max(total_risk, 1) * 100, 1)
                    print(f"T_max={r['T_max_final']:.0f}°C  HIGH={high_pct}%  t={r['elapsed_s']}s")
                    all_results[part_name][mat_name][resolution] = {
                        "T_max_C":       round(r["T_max_final"], 1),
                        "T_avg_C":       round(r["T_avg_final"], 1),
                        "max_cool_rate": round(r["max_cool_rate"], 1),
                        "max_remelt":    r["max_remelt"],
                        "HIGH":          rc["HIGH"],
                        "MEDIUM":        rc["MEDIUM"],
                        "LOW":           rc["LOW"],
                        "HIGH_pct":      high_pct,
                        "active_voxels": r["active_voxels"],
                        "grid":          "x".join(str(x) for x in r["grid_shape"]),
                        "element_mm":    r["element_size_mm"],
                        "elapsed_s":     r["elapsed_s"],
                    }
                except Exception as e:
                    print(f"ERROR: {e}")
                    all_results[part_name][mat_name][resolution] = {"error": str(e)}

    # Save JSON
    json_path = out_dir / "summary.json"
    json_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"\nJSON saved: {json_path}")

    # Build HTML report
    html_path = out_dir / "report.html"
    _build_html(all_results, materials, html_path, ts)
    print(f"HTML saved: {html_path}")

    # Print summary table per part
    _print_tables(all_results, materials)


def _print_tables(all_results, materials):
    for part_name, part_data in all_results.items():
        print(f"\n{'='*90}")
        print(f"  {part_name}")
        print(f"{'='*90}")
        print(f"{'Material':<28} {'Res':>8} {'T_max':>8} {'T_avg':>8} {'CoolRate':>10} {'HIGH%':>7} {'Time':>7}")
        print(f"{'-'*90}")
        for mat_name, res_data in part_data.items():
            for resolution, s in res_data.items():
                if "error" in s:
                    print(f"  {'ERROR'}")
                    continue
                mat_label = mat_name[:27]
                print(f"{mat_label:<28} {resolution:>8} {s['T_max_C']:>7.0f}°C "
                      f"{s['T_avg_C']:>7.1f}°C {s['max_cool_rate']:>9.0f}/s "
                      f"{s['HIGH_pct']:>6.1f}% {s['elapsed_s']:>6.0f}s")


def _build_html(all_results, materials, out_path, ts):
    import json as _json

    mat_names = list(materials.keys())
    parts = list(all_results.keys())
    colors_res = {"fast": "#2563eb", "fine": "#dc2626"}

    # Build flat table rows for all combinations
    rows = []
    for part in parts:
        for mat_name in mat_names:
            for res in RESOLUTIONS:
                s = all_results.get(part, {}).get(mat_name, {}).get(res, {})
                if "error" in s or not s:
                    continue
                rows.append({
                    "part": part,
                    "material": materials[mat_name]["display_name"],
                    "resolution": res,
                    **s
                })

    # Bar chart: T_max by material for each part (fast only)
    bar_data = {}
    for part in parts:
        bar_data[part] = {
            "materials": [],
            "T_max_fast": [], "T_max_fine": [],
            "HIGH_fast":  [], "HIGH_fine":  [],
        }
        for mat_name in mat_names:
            bar_data[part]["materials"].append(materials[mat_name]["display_name"])
            for res in ["fast", "fine"]:
                s = all_results.get(part, {}).get(mat_name, {}).get(res, {})
                bar_data[part][f"T_max_{res}"].append(s.get("T_max_C", 0))
                bar_data[part][f"HIGH_{res}"].append(s.get("HIGH_pct", 0))

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>FEM Full Matrix — {ts}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:-apple-system,'Segoe UI',sans-serif; background:#f0f4f8; color:#0f172a; font-size:13px; }}
.header {{ background:#0f172a; color:#e2e8f0; padding:18px 28px; }}
.header h1 {{ font-size:1.2rem; font-weight:700; }}
.header p  {{ font-size:.78rem; color:#94a3b8; margin-top:3px; }}
.main {{ padding:20px 28px; }}
.tabs {{ display:flex; gap:6px; margin-bottom:16px; flex-wrap:wrap; }}
.tab  {{ padding:6px 16px; border-radius:5px; border:1px solid #e2e8f0; background:#fff;
         cursor:pointer; font-size:.8rem; font-weight:500; transition:all .15s; }}
.tab.active {{ background:#2563eb; color:#fff; border-color:#2563eb; }}
.part-section {{ display:none; }}
.part-section.active {{ display:block; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:14px; }}
.card {{ background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:14px; }}
.card-title {{ font-size:.7rem; font-weight:700; text-transform:uppercase; letter-spacing:.05em;
               color:#64748b; margin-bottom:8px; }}
.chart {{ height:340px; }}
table {{ width:100%; border-collapse:collapse; font-size:.78rem; }}
th {{ text-align:center; padding:7px 10px; background:#f8fafc; color:#64748b;
      font-size:.68rem; text-transform:uppercase; border-bottom:2px solid #e2e8f0; }}
th:first-child {{ text-align:left; }}
td {{ padding:6px 10px; border-bottom:1px solid #f1f5f9; text-align:center; }}
td:first-child {{ text-align:left; font-weight:600; }}
.badge-fast {{ background:#dbeafe; color:#2563eb; padding:1px 6px; border-radius:4px; font-size:.68rem; }}
.badge-fine {{ background:#fee2e2; color:#dc2626; padding:1px 6px; border-radius:4px; font-size:.68rem; }}
.high {{ color:#dc2626; font-weight:700; }}
</style>
</head>
<body>
<div class="header">
  <h1>FEM Full Matrix Research</h1>
  <p>Parts: {', '.join(parts)} &nbsp;|&nbsp; Materials: {len(materials)} &nbsp;|&nbsp;
     Resolutions: fast (2mm), fine (0.5mm) &nbsp;|&nbsp; Generated: {ts}</p>
</div>
<div class="main">

  <div class="tabs" id="partTabs">
"""
    for i, part in enumerate(parts):
        active = "active" if i == 0 else ""
        html += f'    <div class="tab {active}" onclick="showPart(\'{part}\')">{part}</div>\n'

    html += "  </div>\n"

    for pi, part in enumerate(parts):
        active = "active" if pi == 0 else ""
        bd = bar_data[part]
        mat_labels = bd["materials"]

        # Table rows for this part
        table_rows_fast = [r for r in rows if r["part"] == part and r["resolution"] == "fast"]
        table_rows_fine = [r for r in rows if r["part"] == part and r["resolution"] == "fine"]
        # Merge by material
        by_mat = {}
        for r in table_rows_fast + table_rows_fine:
            by_mat.setdefault(r["material"], {})[r["resolution"]] = r

        html += f"""  <div class="part-section {active}" id="part-{part}">
    <div class="grid2">
      <div class="card">
        <div class="card-title">Peak Temperature by Material</div>
        <div id="chart-tmax-{part}" class="chart"></div>
      </div>
      <div class="card">
        <div class="card-title">HIGH Risk % by Material</div>
        <div id="chart-high-{part}" class="chart"></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Full Results Table</div>
      <table>
        <thead><tr>
          <th>Material</th>
          <th>Res</th><th>T_max (°C)</th><th>T_avg (°C)</th>
          <th>Cool rate (°C/s)</th><th>HIGH %</th><th>HIGH voxels</th>
          <th>Grid</th><th>Time (s)</th>
        </tr></thead>
        <tbody>
"""
        for mat_display, res_dict in by_mat.items():
            for res in ["fast", "fine"]:
                r = res_dict.get(res)
                if not r:
                    continue
                badge = f'<span class="badge-{res}">{res}</span>'
                high_class = ' class="high"' if r["HIGH_pct"] > 30 else ''
                html += f"""          <tr>
            <td>{mat_display}</td>
            <td>{badge}</td>
            <td>{r['T_max_C']:.0f}</td>
            <td>{r['T_avg_C']:.1f}</td>
            <td>{r['max_cool_rate']:.0f}</td>
            <td{high_class}>{r['HIGH_pct']:.1f}%</td>
            <td>{r['HIGH']:,}</td>
            <td>{r['grid']}</td>
            <td>{r['elapsed_s']:.0f}</td>
          </tr>
"""
        html += f"""        </tbody>
      </table>
    </div>
  </div>
"""

    # JavaScript
    html += f"""
<script>
const BAR_DATA = {_json.dumps(bar_data)};

function renderCharts(part) {{
  const bd = BAR_DATA[part];
  if (!bd) return;
  const layout = {{
    paper_bgcolor:'#fff', plot_bgcolor:'#fff',
    xaxis:{{tickfont:{{size:9}}, tickangle:-35}},
    yaxis:{{gridcolor:'#f1f5f9', tickfont:{{size:9}}}},
    margin:{{l:50,r:10,t:10,b:100}},
    legend:{{font:{{size:10}}, x:1, y:1}},
    barmode:'group',
  }};
  Plotly.newPlot('chart-tmax-' + part, [
    {{type:'bar', name:'fast (2mm)', x:bd.materials, y:bd.T_max_fast, marker:{{color:'#2563eb'}}}},
    {{type:'bar', name:'fine (0.5mm)', x:bd.materials, y:bd.T_max_fine, marker:{{color:'#dc2626'}}}},
  ], {{...layout, yaxis:{{...layout.yaxis, title:'°C'}}}}, {{responsive:true, displayModeBar:false}});
  Plotly.newPlot('chart-high-' + part, [
    {{type:'bar', name:'fast (2mm)', x:bd.materials, y:bd.HIGH_fast, marker:{{color:'#2563eb'}}}},
    {{type:'bar', name:'fine (0.5mm)', x:bd.materials, y:bd.HIGH_fine, marker:{{color:'#dc2626'}}}},
  ], {{...layout, yaxis:{{...layout.yaxis, title:'HIGH %'}}}}, {{responsive:true, displayModeBar:false}});
}}

function showPart(name) {{
  document.querySelectorAll('.part-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('part-' + name).classList.add('active');
  document.querySelectorAll('.tab').forEach(t => {{
    if (t.textContent === name) t.classList.add('active');
  }});
  renderCharts(name);
}}

// Render first part on load
renderCharts('{parts[0] if parts else ""}');
</script>
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
