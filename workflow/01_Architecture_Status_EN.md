# DED Analyser — Architecture Status
## Updated: 29/05/2026 | Version: 1.0.1

---

## Current File Structure

```
app.py                        Flask server, routes, job queue, API endpoints
meltio_ded_analyzer.py        RAPID parser + thermal sim + HTML generators
m600_gcode_parser.py          M600GcodeAnalyzer (inherits MeltioDEDAnalyzer)
sensor_analyzer.py            Sensor CSV analyser
engines/
  stress_engine.py            ISM + Rosenthal, f_c=0.045, sensitivity sweep
  distortion_engine.py        STL parsing utilities only (parse_stl, _deduplicate, _subsample)
  reduced_fem.py              Voxel-based 3D thermal FEM — resolution-aware, random sampling
jobs/
  base_job.py                 build_user_params, match_materials, run_auto_stress, build_result
materials_database.json       Material properties DB
ux.html                       Main frontend — v1.0.0
ux_m600.html                  M600 variant
hero.html                     Platform selector landing page
VERSION                       Current version string (e.g. "1.0.0")
release.py                    Release packager — creates releases/<version>.zip
releases/                     Archived release ZIPs
DED Analyser-Heat map.bat     Launcher (full Python path, 40s health-check loop)
tests/                        184 tests — all passing
workflow/                     Architecture & requirements docs (this folder)
```

---

## Version Management

Version is tracked in `VERSION` (plain text, e.g. `1.0.0`) and displayed in the UI sidebar.

### Release process (run when told "bump version")
1. Update `VERSION` to new semver (e.g. `1.1.0`)
2. Update version string in `ux.html` sidebar (`Heat Map · vX.Y.Z`)
3. Run `python release.py` → creates `releases/DED-Analyser-Heat-map-vX.Y.Z.zip`

### What `release.py` packages
All source files: `app.py`, analyzers, engines, jobs, tests, workflow docs, HTML, BAT, VERSION.
Excludes: `__pycache__`, `.pytest_cache`, `.claude`, `*.pyc`, `outputs/`.

### Release history
| Version | Date | Notes |
|---------|------|-------|
| 1.0.0 | 29/05/2026 | Initial release — FEM heat map, Part Geometry, Parameter Sensitivity |
| 1.0.1 | 29/05/2026 | FEM voxel sampling fix (random shuffle), Part Geometry colour/opacity controls, run analysis clears previous results, version management |

---

## API Endpoints

| Method | Route | Description |
|--------|-------|-------------|
| POST | `/api/analyze` | Submit RAPID ZIP job |
| POST | `/api/analyze_m600` | Submit M600 G-code job |
| GET  | `/api/jobs/<jid>` | Poll job status |
| GET  | `/api/jobs/<jid>/result` | Full job result |
| POST | `/api/jobs/<jid>/stop` | Stop a running job |
| GET  | `/api/jobs/<jid>/geometry` | Grey mesh from deposition waypoints |
| POST | `/api/fem/simulate` | Start FEM heat map simulation |
| GET  | `/api/overlay/html/<jid>` | Toolpath overlay HTML |
| GET/POST | `/api/settings` | Read/write settings |
| POST | `/api/rapid/preview` | Preview parameters from ZIP |
| POST | `/api/stress/compute` | Run ISM stress calculation |
| POST | `/api/sensitivity` | Run parameter sensitivity sweep |
| GET  | `/api/materials` | List materials DB |
| POST | `/api/materials` | Add material |

---

## Frontend Sections (ux.html, post-analysis)

| Section | Trigger | Content |
|---------|---------|---------|
| Upload & Configure | always | ZIP upload, compact 3-col params, presets |
| Part Geometry | after analysis | Grey mesh3d, colour picker, opacity slider, ⌖ Center |
| Parameter Sensitivity | after analysis (stress) | Tornado charts, full sweep table |
| FEM Thermal Heat Map | after analysis (stress) | Voxel scatter3d, playback ▶/⏸/■, filters, ⌖ Center |

**On new analysis:** all result sections clear immediately (Plotly.purge, state reset).

---

## What Was Done (29/05/2026)

### FEM
- Resolution now functional: fast=2mm, standard=1mm, fine=0.5mm element size
- `_sample_voxels()` uses random shuffle before slicing — uniform spatial distribution
- Point caps scale with resolution (fast→2k, standard→5k, fine→20k)
- `_sample_voxels()` returns `severity` + `remelts` per voxel for client-side filtering
- Plotly scatter3d (replaced Three.js), white background, colorbar right-centre
- ▶ Play / ⏸ Pause / ■ Stop buttons; Play restarts from layer 0 at end
- Filter panel: T(°C) range + Min remelts (with tooltip explaining remelts)
- Camera: auto-center on new sim, ⌖ Center button, uirevision pattern
- FEM section moved to bottom (after Parameter Sensitivity)
- Full viewport width (negative margin override)

### Part Geometry
- `GET /api/jobs/<jid>/geometry` — quad-strip mesh from overlay_pts
- Colour picker + opacity slider (live update via Plotly.react)
- ⌖ Center button

### UI
- Sidebar: single "Analysis" nav item
- Compact 3-column parameter form
- Part name + presets in one row; Environment as 2-button toggle
- Run Analysis clears all previous results immediately
- Browse material → clears input and shows full list

### Versioning
- `VERSION` file, `release.py`, `releases/` directory
- UI shows `Heat Map · v1.0.0`

---

## What Still Needs Refactoring

| Item | Priority |
|------|----------|
| Job runners inline in `app.py` | LOW |
| `generate_*` HTML methods in `meltio_ded_analyzer.py` | MEDIUM |
