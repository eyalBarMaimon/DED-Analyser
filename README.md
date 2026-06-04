# DED Analyser — Heat Map v1.0.3

Meltio wire-laser DED thermal analysis tool.  
Analyses RAPID toolpath code, runs FEM thermal simulation, and visualises residual stress and heat distribution.

---

## Quick Start

### Requirements
- Python 3.10 or newer — download from [python.org](https://www.python.org/downloads/)
- No other software needed

### Installation

**Step 1 — Install Python 3.10+**  
Download and install from [python.org](https://www.python.org/downloads/).  
During installation on Windows, check **"Add Python to PATH"**.

**Step 2 — Download the release**  
Download `DED-Analyser-Heat-map-v1.0.3.zip` from:  
https://github.com/eyalBarMaimon/DED-Analyser/releases/tag/v1.0.3

**Step 3 — Extract the ZIP**  
Unzip to any folder on your computer.

**Step 4 — Open a terminal in the folder**  
- Windows: right-click inside the folder → "Open in Terminal"  
- Mac/Linux: `cd` to the folder in your terminal

**Step 5 — Install dependencies (once only)**
```
pip install flask flask-cors numpy
```

**Step 6 — Start the server**
```
python app.py
```

**Step 7 — Open the app**  
Open your browser at: **http://localhost:5050/ded**

### Windows shortcut
Double-click **`DED Analyser-Heat map.bat`** — starts the server and opens the browser automatically.

### Mac shortcut
Double-click **`DED Analyser.command`** — installs dependencies automatically and opens the browser.  
If macOS blocks it: right-click → **Open** → confirm.

---

## How to Use

### Step 1 — Upload your print job
Upload a **ZIP file** containing your RAPID `.mod` layer files (one `.mod` per layer, as exported from ABB RobotStudio for Meltio).

Sample files are included in the `RAW Data/` folder.

### Step 2 — Set parameters
Fill in your process parameters:
| Parameter | Typical value |
|-----------|--------------|
| Laser Power | 1000–2000 W |
| Wire Feed Speed | 8–15 mm/s |
| Layer Height | 0.5–1.5 mm |
| Layer Width | 1.5–3.0 mm |
| Material T0 | e.g. SS316L, Ti-6Al-4V, Aluminium 6061 |

### Step 3 — Run Analysis
Click **Run Analysis**. The tool will:
- Parse the RAPID toolpath
- Run Rosenthal thermal simulation per waypoint
- Compute residual stress (ISM method)
- Generate part geometry view

### Step 4 — Run FEM Heat Map (optional)
After analysis completes, open the **FEM Thermal Heat Map** section and click **Run Heat Map Simulation**.

Resolution options:
- **Fast (2mm)** — seconds, good for quick check
- **Standard (1mm)** — ~1 min, recommended
- **Fine (0.5mm)** — ~5 min, detailed
- **Adaptive** — runs fast first, upgrades to fine automatically if HIGH risk > 5%

---

## Output Sections

| Section | What you see |
|---------|-------------|
| **Part Geometry** | Clean grey 3D mesh of the printed part |
| **Parameter Sensitivity** | Which parameters most affect stress and distortion |
| **FEM Thermal Heat Map** | 3D voxel temperature field, layer-by-layer animation |

### FEM colour scale
Blue (cold) → Cyan → Green → Yellow → Orange → Red (hot = near/above T_melt)

### FEM filters
- **T(°C) range** — show only voxels in a temperature window
- **Min remelts** — show voxels re-melted ≥ N times (thermal fatigue indicator)
- **Min fatigue** — show voxels with high thermal cycling amplitude

---

## Supported Materials
SS316L, SS304, Ti-6Al-4V, Ti CP Grade 2, Inconel 625, Inconel 718,
Hastelloy C-276, Hastelloy X, Aluminium 6061, ER70S Mild Steel, Copper

---

## Troubleshooting

**"No module named flask"** → run `pip install flask flask-cors numpy`

**Port already in use** → another instance is running; close it or change port in `app.py` line: `port = int(os.environ.get("PORT", 5050))`

**FEM takes too long** → use **Fast (2mm)** resolution or reduce Overlay Points to 6,000

**Browser shows blank page** → make sure you open `http://localhost:5050/ded` (not just `localhost:5050`)

---

## Version
**v1.0.3** — 4 Jun 2026  
- Seam (layer start/end) detection and thermal analysis — shows seam XY drift, overlap energy, risk classification  
- Full navigation sidebar: Live Dashboard, Post-Print Review, Offline Replay, Print History, Materials DB  
- Stress Analysis section with residual stress and distortion prediction  
- Part Geometry 3D viewer, Parameter Sensitivity tornado charts, FEM Thermal Heat Map with filters  
- CSV export expanded to 35 columns (VED, norm_H, cracking score, all anomaly flags, seam metrics)  
- Bug fixes: overheat_risk consistency, CSV missing fields, process window crash guard, seam gap search  

**v1.0.2** — 1 Jun 2026  
- Fixed crash when running from IDLE / Python shell (`sys.stdout.buffer` AttributeError)  
- Added Mac launcher (`DED Analyser.command`) — double-click to start  

**v1.0.1** — 31 May 2026  
Repository: https://github.com/eyalBarMaimon/DED-Analyser
