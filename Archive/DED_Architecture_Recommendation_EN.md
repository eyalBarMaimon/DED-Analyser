# DED Analyser — Architecture Recommendation
## Before Adding 3D Distortion Module

---

## The Core Problem in One Sentence

`meltio_ded_analyzer.py` is a 2,588-line class that does three completely different things: **parses RAPID code**, **simulates physics**, and **generates HTML/CSS/JavaScript**. This is the source of almost every maintenance pain.

---

## What the Code Actually Looks Like Today

```
meltio_ded_analyzer.py  (2,588 lines)
├── Lines 81–803   → CORE: parse RAPID, thermal sim     (723 lines — the real engine)
└── Lines 804–2524 → OUTPUT: generate HTML/SVG/CSV/animations (1,721 lines — rendering)

app.py  (1,596 lines)
├── Routes + job management                              (~400 lines — appropriate)
├── _run_analysis_job()                                  (~200 lines)
├── _run_m600_analysis_job()                             (~200 lines, ~90% identical)
├── _run_stress_compute()                                (~130 lines — business logic leaked into routes)
└── DirectoryWatcher, SensorAnalyzer wiring, etc.

m600_gcode_parser.py  (208 lines)  ← M600GcodeAnalyzer inherits MeltioDEDAnalyzer ✓
sensor_analyzer.py   (149 lines)  ← clean, focused ✓
```

**The two files that need work: `app.py` and `meltio_ded_analyzer.py`**

---

## Target Architecture

```
ded_analyser/
│
├── app.py                        ← routes + job wiring only (~300 lines)
│
├── engines/
│   ├── __init__.py
│   ├── stress_engine.py          ← _run_stress_compute() + MECH_PROPS dict
│   ├── distortion_engine.py      ← NEW: STL parser, displaced vertices, 3D payload
│   └── thermal_engine.py         ← extracted from analyzer (optional, Phase 2)
│
├── jobs/
│   ├── __init__.py
│   ├── base_job.py               ← shared: material matching, stress auto-estimate, result builder
│   ├── analysis_job.py           ← _run_analysis_job (RAPID)
│   ├── m600_job.py               ← _run_m600_analysis_job
│   └── dashboard_job.py          ← _run_dashboard_job
│
├── parsers/
│   ├── __init__.py
│   ├── rapid_parser.py           ← extracted core of MeltioDEDAnalyzer (lines 81–803)
│   ├── m600_parser.py            ← M600GcodeAnalyzer (already separate)
│   ├── sensor_parser.py          ← SensorAnalyzer (already separate)
│   └── stl_parser.py             ← NEW
│
├── renderers/                    ← the 1,721 generate_* lines, now clearly separated
│   ├── __init__.py
│   ├── html_renderer.py          ← generate_html_heatmap, generate_3d_html
│   ├── animation_renderer.py     ← generate_animation_html
│   ├── report_renderer.py        ← generate_report, save_report
│   └── csv_renderer.py           ← generate_csv
│
├── watcher/
│   └── directory_watcher.py      ← DirectoryWatcher class
│
├── materials_database.json
├── settings.json
│
└── frontend/
    ├── ux.html
    ├── ux_m600.html
    ├── hero.html
    └── preview_report.html
```

---

## The Three Changes That Give the Most Value

### Change 1 — Extract `stress_engine.py` (1–2 hours, zero risk)

Move `_run_stress_compute()` and the `_MECH` dict out of `app.py` into a standalone module.

**Before:**
```python
# app.py — line 267
def _run_stress_compute(data: dict):
    _MECH = { 'SS316L': {...}, 'Ti-6Al-4V': {...}, ... }  # defined twice in app.py
    ...
```

**After:**
```python
# engines/stress_engine.py
MECH_PROPS = {
    'SS316L':      {'E_GPa': 193, 'yield_MPa': 310, 'alpha_1e6': 16.0},
    'Ti-6Al-4V':   {'E_GPa': 114, 'yield_MPa': 880, 'alpha_1e6':  8.6},
    'Inconel 625': {'E_GPa': 205, 'yield_MPa': 490, 'alpha_1e6': 12.8},
    'H13':         {'E_GPa': 210, 'yield_MPa':1200, 'alpha_1e6': 11.5},
    'ER70S-6':     {'E_GPa': 200, 'yield_MPa': 480, 'alpha_1e6': 12.0},
    'copper':      {'E_GPa': 128, 'yield_MPa': 340, 'alpha_1e6': 17.0},
}

def compute_stress(data: dict) -> dict | None:
    ...

# app.py — now just:
from engines.stress_engine import compute_stress, MECH_PROPS
```

This immediately eliminates the duplicated `_MECH` dict and makes `distortion_engine.py` easy to add next to it.

---

### Change 2 — Unify the two analysis jobs into `base_job.py` (3–4 hours, medium risk)

`_run_analysis_job` and `_run_m600_analysis_job` share ~90% of their logic. Every bug fix and every new feature (like 3D distortion) currently has to be applied twice.

**Before:** 400 lines of near-identical code in two functions.

**After:**
```python
# jobs/base_job.py
def build_user_params(form: dict, analyzer, defaults: dict) -> dict:
    """Shared: normalise form input into user params dict."""
    ...

def match_materials(analyzer, db) -> None:
    """Shared: fuzzy match T0/T1 against materials DB."""
    ...

def run_auto_stress(analyzer) -> dict | None:
    """Shared: build stress payload from analyzer state and run ISM."""
    from engines.stress_engine import compute_stress, MECH_PROPS
    ...

def build_result(analyzer, viz, report_path, auto_stress) -> dict:
    """Shared: assemble the final result dict returned to the frontend."""
    ...

# jobs/analysis_job.py
from jobs.base_job import build_user_params, match_materials, run_auto_stress, build_result

def run(jid, zip_path, filename, form):
    analyzer = MeltioDEDAnalyzer(zip_path)
    ...
    build_user_params(form, analyzer, defaults={...})
    match_materials(analyzer, db)
    ...
    result = build_result(analyzer, viz, report_path, run_auto_stress(analyzer))
    _job_update(jid, status='done', result=result)
```

When you add 3D distortion to the analysis pipeline, you add it **once** in `base_job.py` and it works for both RAPID and M600.

---

### Change 3 — Separate rendering from parsing in `meltio_ded_analyzer.py` (4–6 hours, low risk)

The 1,721 lines of `generate_*` methods have no business being inside the parser class. They take `self.thermal_data` as input and produce HTML strings as output — they are pure rendering functions.

**Before:**
```python
class MeltioDEDAnalyzer:          # does everything
    def parse_rapid_code(self): ...
    def calculate_thermal_data(self): ...
    def generate_3d_html(self): ...   # 566 lines of embedded JS/CSS
    def generate_animation_html(self): ...  # 245 lines of embedded JS
```

**After:**
```python
# parsers/rapid_parser.py
class RapidParser:
    def parse(self): ...
    def calculate_thermal_data(self): ...
    # thermal_data, waypoints, num_layers — that's it

# renderers/html_renderer.py
def generate_3d_html(thermal_data: list, user_params: dict, timestamp: str) -> str: ...
def generate_animation_html(thermal_data: list, timestamp: str) -> str: ...

# renderers/report_renderer.py
def generate_report(thermal_data, viz_files, user_params) -> str: ...
```

The 3D distortion viewer you want to build fits naturally into `renderers/` — and the `distortion_engine.py` that feeds it fits naturally into `engines/`.

---

## What NOT to Rewrite

- **`sensor_analyzer.py`** — already clean and focused. Leave it.
- **`m600_gcode_parser.py`** — short (208 lines), inheritance works fine. Leave it.
- **The ISM physics** (`_run_stress_compute`) — the math is correct. Move it, don't rewrite it.
- **The RAPID parser logic** (lines 81–803) — works well. Extract it, don't rewrite it.
- **All `generate_*` HTML content** — don't rewrite the embedded JS/CSS, just move the functions.

---

## Suggested Execution Order

| Step | What | Time | Risk | Value |
|------|------|------|------|-------|
| 1 | Create `engines/stress_engine.py`, import in `app.py` | 1–2 h | Very low | Unblocks distortion engine |
| 2 | Create `engines/distortion_engine.py` (STL parser + new endpoint) | 1 day | Low | **The actual feature** |
| 3 | Create `jobs/base_job.py`, refactor the two job functions | 3–4 h | Medium | Eliminates duplication |
| 4 | Move `DirectoryWatcher` to `watcher/directory_watcher.py` | 1 h | Very low | Cleans up `app.py` |
| 5 | Separate `renderers/` from `meltio_ded_analyzer.py` | 4–6 h | Low-medium | Cleanest long-term |

**Recommended: do Steps 1 → 2 now** (this unblocks the 3D feature immediately), then Step 3 in the next session, and Steps 4–5 whenever it bothers you.

---

## What This Architecture Makes Easy in the Future

- **Add a new machine type** → new file in `parsers/`, new file in `jobs/`, zero changes elsewhere
- **Swap rendering engine** (e.g. replace Plotly with Three.js everywhere) → change only `renderers/`
- **Unit test the physics** → `engines/stress_engine.py` has no Flask, no file I/O — pure functions, easy to test
- **Add the 3D distortion feature** → `engines/distortion_engine.py` + one new route in `app.py` + one new section in `ux.html`

---

*Based on code review of DED Analyser v2.0 — 26/05/2026*
