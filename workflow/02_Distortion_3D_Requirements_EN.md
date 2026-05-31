# DED Analyser — Part Geometry & Distortion
## Updated: 29/05/2026 | Version: 1.0.0

---

## Status

| Feature | Status |
|---------|--------|
| STL-based ISM distortion | ❌ Removed |
| Part Geometry from toolpath | ✅ v1.0.0 |
| Thermal distortion via FEM | → `03_ReducedFEM_Requirements_EN.md` |

---

## Part Geometry — `GET /api/jobs/<jid>/geometry`

Clean grey mesh directly from deposition waypoints — no STL, no thermal coloring.

### Algorithm
1. Group `overlay_pts` by layer
2. Resample each layer to 120 pts (even step)
3. Quad-strip triangulation between adjacent layer pairs
4. Close loop per layer pair
5. Return Plotly-ready `mesh3d`

```python
{
  'ok': True,
  'vertex_count': int,
  'face_count':   int,
  'mesh': {
    'type': 'mesh3d',
    'x','y','z', 'i','j','k',
    'color':    '#cccccc',   # user-adjustable
    'opacity':  0.92,        # user-adjustable
    'flatshading': True,
    'lighting': {'ambient':0.8,'diffuse':0.6,'specular':0.2},
  }
}
```

### Frontend Controls
- **Colour picker** — hex color input, live update via `Plotly.react`
- **Opacity slider** — 5%–100%, live update
- **⌖ Center** — resets camera (uirevision++)
- Auto-opens after analysis; controls hidden until geometry loaded

### Camera
- Default eye: (1.6, 1.6, 1.0)
- `uirevision` increments on new load → camera resets
- User rotation preserved during interaction

---

## What Was Removed

| Item | Reason |
|------|--------|
| `compute_distortion()` | Replaced by FEM |
| `_build_displacement_field()` | Not needed |
| `_fallback_displacement()` | Not needed |
| `POST /api/distortion/3d` | Removed |
| `POST /api/distortion/mesh` | Removed |
| STL upload + cache | Removed |
| Mesh Overlay UI section | Removed |
| `generate_distortion_animation_html()` | Removed from both pipelines |

---

## What Remains in `distortion_engine.py`

```python
parse_stl(data: bytes) → dict | None
_deduplicate(verts, faces, tol=0.01)
_subsample(verts, faces, max_faces)
```

Not called from any active pipeline — kept for potential future use.
