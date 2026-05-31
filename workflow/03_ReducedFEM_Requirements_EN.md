# DED Analyser — Reduced-Order Thermal FEM (Heat Map)
## Updated: 29/05/2026 | Version: 1.0.0

---

## Status: COMPLETE ✅

Backend and frontend both fully implemented and released in v1.0.0.

---

## Backend — `engines/reduced_fem.py`

| Component | Status | Notes |
|-----------|--------|-------|
| `VoxelGrid` dataclass | ✅ | T, T_peak, cool_rate, n_remelt, active |
| `build_grid()` | ✅ | Resolution-aware element size |
| `run_simulation()` | ✅ | Layer-by-layer, per-waypoint |
| `_deposit_heat()` | ✅ | Gaussian laser, ±3 voxel neighbourhood |
| `_diffuse_z()` | ✅ | Explicit FDM in Z, Fo ≤ 0.45 |
| `_apply_convection()` | ✅ | h = 35 W/m²K top surface |
| `_compute_melt_pool()` | ✅ | Rosenthal analytical |
| `_classify_risk()` | ✅ | HIGH / MEDIUM / LOW |
| `_voxel_caps()` | ✅ | Scales caps with resolution |
| `_sample_voxels()` | ✅ | Random shuffle → uniform distribution; returns severity + remelts |
| `_build_result()` | ✅ | Full result with element_size_mm |
| `POST /api/fem/simulate` | ✅ | Async job |
| `POST /api/jobs/<jid>/stop` | ✅ | User-cancellable |

---

## Grid Design

```python
RESOLUTION_ELEMENT_SIZE = {
    'fast':     2.0,   # coarser, fastest
    'standard': 1.0,   # 1×1 mm baseline
    'fine':     0.5,   # finer, slowest
}
MAX_ELEMENTS = 300
```

Point caps scale as `1/dx²`:

| Resolution | dx | Snap cap | Final cap |
|---|---|---|---|
| fast | 2 mm | 500 | 2,000 |
| standard | 1 mm | 1,000 | 5,000 |
| fine | 0.5 mm | 4,000 | 20,000 |

**Spatial uniformity fix (v1.0.0):** `np.argwhere` returns indices sorted by (ix,iy,iz). Without shuffling, step-sampling produces column-like artifacts. Fixed with `rng.shuffle(indices)` before slicing — seed=42 for reproducibility.

---

## Result Structure

```python
{
  'ok', 'element_size_mm', 'grid_shape', 'grid_spacing',
  'active_voxels', 'T_max_final', 'T_avg_final',
  'max_cool_rate', 'max_remelt', 'risk_counts',
  'risk_voxels',    # max 2000, full detail
  'snapshots',      # per-layer: T_max, T_avg, active_count, voxels{x,y,z,t,severity,remelts}
  'num_layers',
  'final_voxels',   # sampled to final_cap, includes severity + remelts
}
```

---

## Risk Classification

| Severity | Condition |
|----------|-----------|
| HIGH | T ≥ T_melt OR cool_rate > 500 °C/s OR n_remelt > 2 |
| MEDIUM | T ≥ 0.85×T_melt OR cool_rate > 100 °C/s OR n_remelt > 0 |
| LOW | All other active voxels |

**Remelts:** number of times a voxel exceeded T_melt and re-solidified. Typical range 0–5 in DED wire; values > 2 flag HIGH risk (thermal fatigue, grain coarsening). The UI filter "Min remelts" shows only voxels remelted ≥ N times.

---

## Frontend — `ux.html` Section: FEM Thermal Heat Map

### Controls
| Control | Behaviour |
|---------|-----------|
| Resolution | fast / standard / fine — changes element size and point count |
| Run | Starts simulation; button replaced by Stop during run |
| ■ Stop | Cancels job server-side (`POST /api/jobs/<jid>/stop`) |
| ▶ Play | Starts layer animation from layer 0 (or current if not at end) |
| ⏸ Pause | Freezes on current layer |
| ⌖ Center | Resets camera (increments uirevision) |
| Layer slider | Scrubs to any layer with current filters applied |

### Filter Panel
Client-side, instant. Appears after simulation completes.

| Filter | Behaviour |
|--------|-----------|
| T (°C) min/max | Hide voxels outside temperature range |
| Min remelts ⓘ | Show only voxels remelted ≥ N times (tooltip explains concept) |
| Reset | Clears all filters |
| Counter | Shows "X / Y voxels" |

### Camera
- Auto-centers on new simulation (uirevision++)
- User rotation preserved during playback (uirevision stable)
- Colorbar: vertical, right side, y=0.5 (middle height)

### Layout
- Full viewport width via negative margin override (`margin-left/right: -36px`)
- Height: 560px fixed
- Background: white

---

## Tests — `tests/test_reduced_fem.py`

10 tests, all passing (184 total in suite):
- Resolution ordering (fast < standard < fine)
- Element size ≈ target ±15%
- NZ identical across resolutions
- Domain coverage
- MAX_ELEMENTS cap
- Unknown resolution fallback
- Array shapes correct

---

## Performance (tower SST316L, 410 layers)

| Resolution | Grid | Active voxels | Time |
|---|---|---|---|
| fast | ~59×58×410 | ~50k | ~20s |
| standard | ~117×116×410 | ~120k | ~90s |
| fine | ~234×232×410 | ~380k | ~3–5 min |
