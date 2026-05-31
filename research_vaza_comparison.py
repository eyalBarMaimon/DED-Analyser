"""
Research script: FEM thermal comparison of VAZA SST 316L toolpath
across three materials — Aluminium 6061, Titanium CP Grade 2, Stainless Steel 316L.

Usage:  python research_vaza_comparison.py
Output: research_output/vaza_comparison_<timestamp>.html  (Plotly report)
        research_output/vaza_comparison_<timestamp>.json  (raw data)
"""
import sys, json, pathlib, zipfile, time
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from datetime import datetime
from meltio_ded_analyzer import MeltioDEDAnalyzer, load_materials_db, fuzzy_match_material
from engines.reduced_fem import build_grid, run_simulation

# ── Config ────────────────────────────────────────────────────────────────────

VAZA_ZIP   = pathlib.Path(__file__).parent / "RAW Data" / "VAZA  SST 316L.zip"
OUTPUT_DIR = pathlib.Path(__file__).parent / "research_output"
RESOLUTION = "standard"    # change to 'fine' for more detail

MATERIALS_TO_TEST = [
    "Aluminium 6061",
    "Titanium CP Grade 2",
    "Stainless Steel 316L",
]

# Shared process parameters (Meltio M600 defaults for VAZA)
PROCESS_PARAMS = {
    "laser_power":   1500,
    "scan_speed":    10,
    "layer_height":  0.6,
    "bead_width":    2.0,
    "wire_diameter": 1.2,
    "beam_spot":     1.2,
    "ambient_temp":  25,
    "absorption":    0.35,
    "dwell_time":    5,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mat_props(mat_entry: dict) -> dict:
    return {
        "k":       mat_entry["thermal_conductivity"],
        "density": mat_entry["density"],
        "Cp":      mat_entry["specific_heat"],
        "T_melt":  mat_entry["melting_point"],
    }


def _run_one(waypoints: list, mat_entry: dict, params: dict, resolution: str) -> dict:
    mat = _mat_props(mat_entry)
    fem_params = dict(params,
                      num_layers=max(wp["layer"] for wp in waypoints),
                      absorption=mat_entry.get("absorption_450nm", params["absorption"]))
    grid = build_grid(waypoints, fem_params, resolution)

    t0 = time.time()
    progress_log = []

    def _cb(pct, stage):
        if int(pct) % 20 == 0 or pct >= 99:
            elapsed = time.time() - t0
            progress_log.append(f"  {pct:5.1f}%  {stage}  ({elapsed:.0f}s)")
            print(f"  [{mat_entry['display_name']}] {pct:.0f}%  {stage}")

    result = run_simulation(grid, waypoints, mat, fem_params, progress_cb=_cb)
    result["elapsed_s"] = round(time.time() - t0, 1)
    result["progress_log"] = progress_log
    result["material_name"] = mat_entry["display_name"]
    result["grid_shape_str"] = "×".join(str(x) for x in result["grid_shape"])
    return result


def _extract_waypoints(zip_path: pathlib.Path, params: dict) -> list:
    import tempfile, os
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.write(zip_path.read_bytes()); tmp.close()
    ana = MeltioDEDAnalyzer(tmp.name)
    if not ana.extract_and_read():
        raise RuntimeError("Failed to extract ZIP")
    ana.parse_rapid_code()
    ana.user = {"part_name": "VAZA", "laser_power": str(params["laser_power"]),
                "feed_speed": 12.5, "layer_height": params["layer_height"],
                "layer_width": params["bead_width"],
                "wire_diameter": params["wire_diameter"],
                "inert_environment": False, "ambient_temp": params["ambient_temp"],
                "min_layer_dwell": params["dwell_time"],
                "beam_spot_diameter": params["beam_spot"]}
    ana.calculate_thermal_data()
    os.unlink(tmp.name)

    wps = []
    step = max(1, len(ana.thermal_data) // 4000)
    for d in ana.thermal_data[::step]:
        wps.append({"x": d["x"], "y": d["y"], "z": d["z"],
                    "layer": d["layer_num"], "is_deposition": True})
    print(f"  Waypoints extracted: {len(wps)} (from {len(ana.thermal_data)} total)")
    return wps


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    db = load_materials_db()

    print(f"\n{'='*60}")
    print(f"  VAZA FEM Comparison — {RESOLUTION} resolution")
    print(f"{'='*60}\n")

    # Extract waypoints once (same toolpath for all materials)
    print("Extracting waypoints from VAZA ZIP…")
    waypoints = _extract_waypoints(VAZA_ZIP, PROCESS_PARAMS)
    num_layers = max(wp["layer"] for wp in waypoints)
    print(f"  Layers: {num_layers}  |  Waypoints: {len(waypoints)}\n")

    results = {}
    for mat_name in MATERIALS_TO_TEST:
        mat_entry = fuzzy_match_material(mat_name, db)
        if not mat_entry:
            print(f"WARNING: material '{mat_name}' not found in DB — skipping")
            continue
        print(f"Running FEM: {mat_entry['display_name']} …")
        r = _run_one(waypoints, mat_entry, PROCESS_PARAMS, RESOLUTION)
        results[mat_name] = r
        print(f"  Done in {r['elapsed_s']}s  |  grid {r['grid_shape_str']}"
              f"  |  active voxels: {r['active_voxels']:,}\n")

    # ── Save JSON ─────────────────────────────────────────────────────────────
    json_path = OUTPUT_DIR / f"vaza_comparison_{ts}.json"
    summary = {}
    for name, r in results.items():
        summary[name] = {
            "material":      r["material_name"],
            "elapsed_s":     r["elapsed_s"],
            "grid":          r["grid_shape_str"],
            "element_mm":    r["element_size_mm"],
            "active_voxels": r["active_voxels"],
            "T_max_C":       round(r["T_max_final"], 1),
            "T_avg_C":       round(r["T_avg_final"], 1),
            "max_cool_rate": round(r["max_cool_rate"], 1),
            "max_remelt":    r["max_remelt"],
            "risk_HIGH":     r["risk_counts"]["HIGH"],
            "risk_MEDIUM":   r["risk_counts"]["MEDIUM"],
            "risk_LOW":      r["risk_counts"]["LOW"],
        }
    json_path.write_text(json.dumps({"summary": summary}, indent=2), encoding="utf-8")
    print(f"JSON saved: {json_path.name}")

    # ── Build HTML report ─────────────────────────────────────────────────────
    html_path = OUTPUT_DIR / f"vaza_comparison_{ts}.html"
    _build_html(results, summary, html_path, ts)
    print(f"HTML saved: {html_path.name}")

    # ── Print summary table ───────────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print(f"{'Material':<28} {'T_max':>8} {'T_avg':>8} {'CoolRate':>10} {'Remelt':>8} {'HIGH':>8} {'Time':>8}")
    print(f"{'─'*80}")
    for name, s in summary.items():
        print(f"{s['material']:<28} {s['T_max_C']:>7.0f}°C {s['T_avg_C']:>7.1f}°C "
              f"{s['max_cool_rate']:>9.0f}/s {s['max_remelt']:>8} "
              f"{s['risk_HIGH']:>8,} {s['elapsed_s']:>7.0f}s")
    print(f"{'─'*80}\n")


def _build_html(results: dict, summary: dict, out_path: pathlib.Path, ts: str):
    """Build a self-contained Plotly HTML comparison report."""
    import json as _json

    mat_names   = list(results.keys())
    colors      = ["#2563eb", "#dc2626", "#16a34a"]
    color_map   = dict(zip(mat_names, colors))

    # Prepare final_voxels traces for each material
    voxel_traces = []
    T_global_min = min(min(r["final_voxels"]["t"]) for r in results.values() if r["final_voxels"]["t"])
    T_global_max = max(max(r["final_voxels"]["t"]) for r in results.values() if r["final_voxels"]["t"])

    for i, (name, r) in enumerate(results.items()):
        fv = r["final_voxels"]
        voxel_traces.append({
            "type": "scatter3d", "mode": "markers",
            "name": r["material_name"],
            "x": fv["x"], "y": fv["y"], "z": fv["z"],
            "marker": {
                "color": fv["t"],
                "colorscale": [[0,"#0000ff"],[0.25,"#00ffff"],[0.5,"#00ff00"],
                               [0.7,"#ffff00"],[0.85,"#ff4400"],[1,"#ff0000"]],
                "cmin": T_global_min, "cmax": T_global_max,
                "size": 3, "opacity": 0.8,
                "colorbar": {"title": {"text": "°C"}, "thickness": 12, "len": 0.6,
                             "x": 1.0 + i * 0.12}
            },
            "visible": (i == 0),
            "hovertemplate": "T=%{marker.color:.0f}°C<extra></extra>",
        })

    # Bar chart data — risk counts
    risk_data = {
        "materials": [r["material_name"] for r in results.values()],
        "HIGH":   [r["risk_counts"]["HIGH"]   for r in results.values()],
        "MEDIUM": [r["risk_counts"]["MEDIUM"] for r in results.values()],
        "LOW":    [r["risk_counts"]["LOW"]    for r in results.values()],
        "T_max":  [round(r["T_max_final"])    for r in results.values()],
        "T_avg":  [round(r["T_avg_final"],1)  for r in results.values()],
        "cool":   [round(r["max_cool_rate"])  for r in results.values()],
        "remelt": [r["max_remelt"]            for r in results.values()],
    }

    # Snapshot T_max curves (one line per material)
    snap_traces = []
    for name, r in results.items():
        snaps = r["snapshots"]
        layers = [s["layer"] + 1 for s in snaps]
        t_maxs = [s["T_max"] for s in snaps]
        snap_traces.append({
            "type": "scatter", "mode": "lines",
            "name": r["material_name"],
            "x": layers, "y": t_maxs,
            "line": {"color": color_map[name], "width": 2},
        })

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>VAZA FEM Comparison — {ts}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:-apple-system,'Segoe UI',sans-serif; background:#f0f4f8; color:#0f172a; font-size:13.5px; }}
.header {{ background:#0f172a; color:#e2e8f0; padding:20px 32px; }}
.header h1 {{ font-size:1.3rem; font-weight:700; }}
.header p {{ font-size:.82rem; color:#94a3b8; margin-top:4px; }}
.main {{ padding:24px 32px; max-width:1400px; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }}
.grid4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px; }}
.card {{ background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:16px; }}
.card-title {{ font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.05em; color:#64748b; margin-bottom:10px; }}
.stat-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }}
.stat {{ text-align:center; padding:10px; background:#f8fafc; border-radius:6px; }}
.stat-val {{ font-size:1.4rem; font-weight:700; }}
.stat-lbl {{ font-size:.65rem; color:#94a3b8; text-transform:uppercase; margin-top:2px; }}
.blue {{ color:#2563eb; }} .red {{ color:#dc2626; }} .green {{ color:#16a34a; }}
table {{ width:100%; border-collapse:collapse; font-size:.82rem; }}
th {{ text-align:left; padding:8px 12px; background:#f8fafc; color:#64748b; font-size:.72rem; text-transform:uppercase; border-bottom:2px solid #e2e8f0; }}
td {{ padding:8px 12px; border-bottom:1px solid #f1f5f9; }}
.toggle-bar {{ display:flex; gap:8px; margin-bottom:10px; }}
.toggle-btn {{ padding:5px 14px; border-radius:5px; border:1px solid #e2e8f0; background:#fff; cursor:pointer; font-size:.78rem; transition:all .15s; }}
.toggle-btn.active {{ background:#2563eb; color:#fff; border-color:#2563eb; }}
#plot3d {{ height:480px; }}
#plotSnap {{ height:300px; }}
#plotRisk {{ height:300px; }}
</style>
</head>
<body>
<div class="header">
  <h1>VAZA FEM Thermal Comparison</h1>
  <p>Resolution: {RESOLUTION} &nbsp;|&nbsp; Generated: {ts} &nbsp;|&nbsp; Materials: {", ".join(r["material_name"] for r in results.values())}</p>
</div>
<div class="main">

  <!-- Summary cards -->
  <div class="grid4" style="margin-bottom:16px">
"""
    for name, s in summary.items():
        risk_pct = round(s["risk_HIGH"] / max(s["active_voxels"], 1) * 100, 1)
        html += f"""    <div class="card">
      <div class="card-title">{s["material"]}</div>
      <div class="stat-grid">
        <div class="stat"><div class="stat-val blue">{s["T_max_C"]:.0f}°C</div><div class="stat-lbl">Peak T</div></div>
        <div class="stat"><div class="stat-val" style="color:#d97706">{s["max_cool_rate"]:.0f}</div><div class="stat-lbl">°C/s max</div></div>
        <div class="stat"><div class="stat-val red">{risk_pct}%</div><div class="stat-lbl">HIGH risk</div></div>
        <div class="stat"><div class="stat-val">{s["T_avg_C"]:.1f}°C</div><div class="stat-lbl">Avg T</div></div>
        <div class="stat"><div class="stat-val">{s["max_remelt"]}</div><div class="stat-lbl">Max remelt</div></div>
        <div class="stat"><div class="stat-val">{s["elapsed_s"]:.0f}s</div><div class="stat-lbl">Sim time</div></div>
      </div>
    </div>
"""

    html += f"""  </div>

  <!-- 3D view with material toggle -->
  <div class="card" style="margin-bottom:16px">
    <div class="card-title">3D Heat Map — Final State</div>
    <div class="toggle-bar" id="matToggle">
"""
    for i, (name, r) in enumerate(results.items()):
        active = "active" if i == 0 else ""
        html += f'      <button class="toggle-btn {active}" onclick="showMat({i})">{r["material_name"]}</button>\n'

    html += f"""    </div>
    <div id="plot3d"></div>
  </div>

  <div class="grid2">
    <!-- T_max per layer curves -->
    <div class="card">
      <div class="card-title">Peak Temperature per Layer</div>
      <div id="plotSnap"></div>
    </div>
    <!-- Risk distribution bar -->
    <div class="card">
      <div class="card-title">Risk Voxel Distribution</div>
      <div id="plotRisk"></div>
    </div>
  </div>

  <!-- Comparison table -->
  <div class="card">
    <div class="card-title">Comparison Table</div>
    <table>
      <thead><tr>
        <th>Material</th><th>T_max (°C)</th><th>T_avg (°C)</th>
        <th>Max cool rate (°C/s)</th><th>Max remelts</th>
        <th>HIGH voxels</th><th>MEDIUM</th><th>LOW</th>
        <th>Grid</th><th>Time (s)</th>
      </tr></thead>
      <tbody>
"""
    for name, s in summary.items():
        html += f"""        <tr>
          <td><strong>{s["material"]}</strong></td>
          <td>{s["T_max_C"]:.0f}</td><td>{s["T_avg_C"]:.1f}</td>
          <td>{s["max_cool_rate"]:.0f}</td><td>{s["max_remelt"]}</td>
          <td style="color:#dc2626">{s["risk_HIGH"]:,}</td>
          <td style="color:#d97706">{s["risk_MEDIUM"]:,}</td>
          <td style="color:#16a34a">{s["risk_LOW"]:,}</td>
          <td>{s["grid"]}</td><td>{s["elapsed_s"]:.0f}</td>
        </tr>
"""
    html += f"""      </tbody>
    </table>
  </div>

</div>

<script>
const TRACES_3D = {_json.dumps(voxel_traces)};
const SNAP_TRACES = {_json.dumps(snap_traces)};
const RISK_DATA = {_json.dumps(risk_data)};

const LAYOUT_3D = {{
  paper_bgcolor:'#f8fafc', plot_bgcolor:'#f8fafc',
  scene:{{ bgcolor:'#f8fafc', aspectmode:'data',
    xaxis:{{title:'X (mm)',titlefont:{{size:10}},tickfont:{{size:9}}}},
    yaxis:{{title:'Y (mm)',titlefont:{{size:10}},tickfont:{{size:9}}}},
    zaxis:{{title:'Z (mm)',titlefont:{{size:10}},tickfont:{{size:9}}}},
    camera:{{eye:{{x:1.6,y:1.6,z:1.0}}}},
  }},
  margin:{{l:0,r:80,t:10,b:0}}, uirevision:'3d',
}};

Plotly.newPlot('plot3d', TRACES_3D, LAYOUT_3D, {{responsive:true, displayModeBar:false}});

Plotly.newPlot('plotSnap', SNAP_TRACES, {{
  paper_bgcolor:'#fff', plot_bgcolor:'#fff',
  xaxis:{{title:'Layer', gridcolor:'#f1f5f9', titlefont:{{size:10}}, tickfont:{{size:9}}}},
  yaxis:{{title:'T_max (°C)', gridcolor:'#f1f5f9', titlefont:{{size:10}}, tickfont:{{size:9}}}},
  margin:{{l:50,r:20,t:10,b:40}}, legend:{{font:{{size:10}}}},
}}, {{responsive:true, displayModeBar:false}});

Plotly.newPlot('plotRisk', [
  {{type:'bar', name:'HIGH',   x:RISK_DATA.materials, y:RISK_DATA.HIGH,   marker:{{color:'#ef4444'}}}},
  {{type:'bar', name:'MEDIUM', x:RISK_DATA.materials, y:RISK_DATA.MEDIUM, marker:{{color:'#f59e0b'}}}},
  {{type:'bar', name:'LOW',    x:RISK_DATA.materials, y:RISK_DATA.LOW,    marker:{{color:'#22c55e'}}}},
], {{
  barmode:'stack', paper_bgcolor:'#fff', plot_bgcolor:'#fff',
  xaxis:{{tickfont:{{size:10}}}},
  yaxis:{{title:'Voxels', titlefont:{{size:10}}, tickfont:{{size:9}}, gridcolor:'#f1f5f9'}},
  margin:{{l:50,r:20,t:10,b:60}}, legend:{{font:{{size:10}}}},
}}, {{responsive:true, displayModeBar:false}});

function showMat(idx) {{
  const vis = TRACES_3D.map((_,i) => i===idx);
  Plotly.restyle('plot3d', {{visible: vis}});
  document.querySelectorAll('#matToggle .toggle-btn').forEach((b,i) => {{
    b.classList.toggle('active', i===idx);
  }});
}}
</script>
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
