# Meltio DED Analyser — Technical Reference

## Overview

Flask web application (port 5050) for analysing Meltio wire-laser DED print jobs.  
Supports two input formats: **ABB Robot RAPID** (.mod files) and **Meltio M600 G-code** (.gcode).

---

## Architecture

```
app.py                        Flask server, job queue, API routes
meltio_ded_analyzer.py        RAPID parser + analyser (MeltioDEDAnalyzer class)
m600_gcode_parser.py          M600 G-code parser (M600GcodeAnalyzer class)
sensor_analyzer.py            Sensor CSV analyser
engines/
  stress_engine.py            ISM residual-stress + distortion model
jobs/
  base_job.py                 Shared: build_user_params, match_materials,
                              run_auto_stress, build_result
materials_database.json       Material properties DB (fuzzy-matched)
ux.html                       Main UI (RAPID jobs)
ux_m600.html                  M600 UI
```

---

## Analysis Pipeline

### Step 1 — Upload & Parse
- User uploads a ZIP (RAPID) or .gcode (M600) via web UI
- Parameters extracted automatically from `Parameters.txt` (if present in ZIP)
- Fields not found in file show **orange dashed border** as suggested defaults:
  - `laser_power` → 1000 W
  - `feed_speed` → 12 mm/s
  - `wire_diameter` → 1.2 mm
- File-sourced values show **green solid border**

### Step 2 — Thermal Simulation (`calculate_thermal_data`)
Per-waypoint temperature using **Rosenthal moving heat source**:
```
T = T_amb + (η·P) / (2π·k·r) · exp(−v(r+x)/(2α))
```
Anomaly flags computed per waypoint:
- `lof_risk` — lack-of-fusion (VED below threshold)
- `keyhole_risk` — keyhole porosity (VED above threshold)
- `overheat_risk` — temperature > 1.3× T_melt
- `cracking_score` — solidification cracking index

FDM solver (numpy) used for inter-layer dwell cooling; analytical approximation for long dwells:
```python
decay = exp(−dwell / tau_dwell)
T_grid = ambient + (T_grid − ambient) * decay
```
Hard cap: `MAX_TOTAL_FDM_STEPS = 5000`.

### Step 3 — Visualisation Generation

| Output | File | Notes |
|--------|------|-------|
| CSV | `*.csv` | All waypoints + thermal data |
| 3D Heatmap | `heatmap_*.html` | Merged Plotly trace (~2 MB); embedded JS animation with 5000pt, `setInterval(100ms)` |
| Process Window | `processwindow_*.html` | VED vs norm_H scatter |
| Thermal Animation | `animation_*.html` | JS-side T computation, binary-search waypoint reveal, speed controls |
| Distortion Animation | `distortion_*.html` | 3-panel: (A) distortion growth chart, (B) 3D stress heatmap layer reveal, (C) nominal vs displaced geometry |

### Step 4 — Stress & Distortion (ISM)

Engine: `engines/stress_engine.py`  
Method: Inherent Strain Method (ISM) with Rosenthal thermal field.

**Inherent strain:**
```
ε_in = α · ΔT_melt · f_c
σ_pass = min(E · ε_in, σ_Y)
```

**Wire-DED constraint factor:** `f_c = 0.045`  
(Literature: Ding et al. 2014, Colegrove et al. 2017)  
PBF uses f_c = 0.30–0.40; wire-DED uses 0.04–0.07 due to larger melt pool and slower cooling.

**Stress relief per layer:**
```
τ_relax = h² / (π² · α_diff)
relief = 1 − exp(−dwell / τ_relax)
σ_cum(n) = σ_cum(n-1) · (1 − relief) + σ_pass · base_factor
```
Layers 1–3: `base_factor = 1.40` (substrate constraint amplification).  
Thin walls < 4 mm: `thin_factor = 1.25`.

**Distortion (cantilever beam approximation):**
```
δ_tip = 3 · ε_in · h_cum² / (4 · wall_t)
```

**Go / No-Go thresholds:**
- util ≥ 80% → **NO-GO**
- util ≥ 50% → **CAUTION**
- util < 50% → **GO**

### Step 5 — Distortion Animation (`generate_distortion_animation_html`)

Three-panel interactive HTML:
- **Panel A** (top-right): 2D distortion growth chart — σ/σ_Y ratio vs layer
- **Panel B** (left): 3D scatter — waypoints coloured by stress ratio, revealed layer-by-layer
- **Panel C** (bottom-right): nominal vs displaced geometry, cone arrows, auto-magnification

Auto-magnification: `max_δ < 0.05mm → 200×`, `< 0.2mm → 50×`, `< 1.0mm → 20×`, `< 3.0mm → 5×`, else `2×`.

---

## Parameter Sensitivity (SS316L baseline, 50 layers, wall=5mm, dwell=60s)

| Parameter | Effect on σ | Effect on δ | Recommendation |
|-----------|-------------|-------------|----------------|
| `yield_MPa` (material) | Direct (1:1) | None | Ti64/H13 → GO vs SS316L → NO-GO |
| `alpha_1e6` (CTE) | Strong, linear | Strong, linear | Ti α=8.6 → 49% / SS α=16 → 97% |
| `dwell_time` | Moderate | None | 300s → −40 MPa vs baseline |
| `ambient_temp` (preheat) | Moderate | Weak | 300°C → CAUTION (from NO-GO) |
| `layer_height` | None | **Very strong** (∝h²) | 0.5mm → 0.09mm / 2.5mm → 2.27mm |
| `wall_thickness` | Small (thin walls) | **Strong** (∝1/t) | 1mm → 2.6mm / 20mm → 0.13mm |
| `laser_power` | None | None | ISM does not depend on power (correct) |
| `scan_speed` | None | None | ISM does not depend on scan speed |

---

## Performance Optimisations

| Bottleneck | Before | After |
|-----------|--------|-------|
| Plotly traces | ~400 traces per file (~20 MB) | 1 merged trace with None gaps (~2 MB) |
| FDM dwell cooling | 120 numpy steps per layer | 1 analytical exp() step |
| surface_mask | Recomputed every step | Cached per Z-level |
| build_result | 10 separate O(n) passes | 1 single-pass loop |
| Animation frames | Pre-computed in Python | JS-side computation, binary search |

---

## API Endpoints

| Method | Route | Description |
|--------|-------|-------------|
| POST | `/api/analyze` | Submit RAPID ZIP job |
| POST | `/api/analyze_m600` | Submit M600 G-code job |
| GET | `/api/jobs/<jid>` | Poll job status + result |
| GET | `/outputs/<filename>` | Download output file |
| GET | `/api/materials` | List materials DB |

---

## Job Result Structure

```json
{
  "part_name": "Tower SST316L",
  "num_layers": 50,
  "viz_files": {
    "csv": "/outputs/tower_*.csv",
    "3d":  "/outputs/heatmap_*.html",
    "pw":  "/outputs/processwindow_*.html",
    "anim":"/outputs/animation_*.html",
    "distort": "/outputs/distortion_*.html"
  },
  "stress": {
    "go_nogo": "NO-GO",
    "summary": { "max_sigma_MPa": 301.9, "utilization_pct": 97.4, "max_delta_mm": 0.523 },
    "per_layer": [...],
    "stress_wps": [...],
    "risk_zones": [...],
    "sensitivity": [...]
  }
}
```

---

## Materials Database

`materials_database.json` — fuzzy-matched at job start.  
Fallback for unmatched materials: `k=15, ρ=7000, Cp=500, T_melt=1400`.

Mechanical properties for ISM in `MECH_PROPS` (stress_engine.py):
SS316L, Ti-6Al-4V, Inconel 625, H13, ER70S-6, Copper, Hastelloy.

---

## Input Files

### RAPID (.mod per layer)
- Robot speed → `SpeedData` (print: ~12 mm/s, travel: ~60 mm/s)
- Material feeder → `DI05=T0`, `DI06=T1`
- Digital I/O signals extracted per layer

### M600 G-code
- Standard Meltio G-code format
- Layer detection via Z-changes
- Feed rate from `F` word (mm/min → mm/s)
- Laser power from `S` word or header
