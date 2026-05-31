"""Reduced-order thermal FEM for DED analysis — voxel-based 3D temperature field.

Grid resolution: fixed 1×1 mm element size (physical units), capped at MAX_ELEMENTS
per axis to bound memory. NX/NY are derived from the bounding box; NZ from layer count.
"""
import math
import numpy as np
from dataclasses import dataclass, field


# ── VoxelGrid ─────────────────────────────────────────────────────────────────

@dataclass
class VoxelGrid:
    NX: int; NY: int; NZ: int
    dx: float; dy: float; dz: float   # mm
    x_min: float; y_min: float; z_min: float

    T:            np.ndarray = field(init=False)
    T_peak:       np.ndarray = field(init=False)
    cool_rate:    np.ndarray = field(init=False)
    n_remelt:     np.ndarray = field(init=False)
    active:       np.ndarray = field(init=False)
    # Improvement 2: cumulative temperature overshoot above T_melt (°C·layers)
    T_overshoot:  np.ndarray = field(init=False)
    # Improvement 3: fatigue index = sum of ΔT per thermal cycle
    fatigue_idx:  np.ndarray = field(init=False)
    _T_prev_cycle: np.ndarray = field(init=False)   # internal: T at start of last cycle

    def __post_init__(self):
        shape = (self.NX, self.NY, self.NZ)
        self.T             = np.full(shape, 25.0)
        self.T_peak        = np.zeros(shape)
        self.cool_rate     = np.zeros(shape)
        self.n_remelt      = np.zeros(shape, dtype=int)
        self.active        = np.zeros(shape, dtype=bool)
        self.T_overshoot   = np.zeros(shape)
        self.fatigue_idx   = np.zeros(shape)
        self._T_prev_cycle = np.full(shape, 25.0)

    def world_to_grid(self, x, y, z):
        ix = int((x - self.x_min) / self.dx)
        iy = int((y - self.y_min) / self.dy)
        iz = int((z - self.z_min) / self.dz)
        return (max(0, min(self.NX-1, ix)),
                max(0, min(self.NY-1, iy)),
                max(0, min(self.NZ-1, iz)))


# ── Grid construction ─────────────────────────────────────────────────────────

MAX_ELEMENTS = 300   # cap per axis to bound memory

# element size per resolution: smaller = finer grid = slower + more detail
RESOLUTION_ELEMENT_SIZE = {
    'fast':       2.0,   # ~4× fewer voxels than standard, fastest
    'standard':   1.0,   # 1×1 mm baseline
    'fine':       0.5,   # ~4× more voxels than standard, slow
    'ultrafine':  0.25,  # ~16× more voxels than standard, very slow (15-30 min)
}

def build_grid(waypoints: list, params: dict, resolution: str = 'standard') -> VoxelGrid:
    """
    Build a voxel grid. Element size is controlled by resolution:
      fast=2mm, standard=1mm, fine=0.5mm — capped at MAX_ELEMENTS per axis.
    """
    dep = [wp for wp in waypoints if wp.get('is_deposition', True)] or waypoints
    xs = [wp['x'] for wp in dep]; ys = [wp['y'] for wp in dep]; zs = [wp['z'] for wp in dep]
    pad = params.get('bead_width', 2.0)
    x_min, x_max = min(xs) - pad, max(xs) + pad
    y_min, y_max = min(ys) - pad, max(ys) + pad
    z_min = min(zs)
    lh = max(params.get('layer_height', 0.8), 0.1)
    NZ = params.get('num_layers', int((max(zs) - z_min) / lh) + 1)

    element_size = RESOLUTION_ELEMENT_SIZE.get(resolution, 1.0)
    dx = element_size
    dy = element_size
    NX = min(MAX_ELEMENTS, max(1, math.ceil((x_max - x_min) / dx)))
    NY = min(MAX_ELEMENTS, max(1, math.ceil((y_max - y_min) / dy)))

    # If the cap kicked in, expand dx/dy so the grid still covers the full domain
    dx = (x_max - x_min) / NX
    dy = (y_max - y_min) / NY

    dz = lh
    return VoxelGrid(NX, NY, NZ, dx, dy, dz, x_min, y_min, z_min)


# ── Helper functions ──────────────────────────────────────────────────────────

def _group_by_layer(waypoints: list, num_layers: int) -> dict:
    """Group waypoints by 0-based layer index clamped to [0, num_layers-1]."""
    result = {}
    for wp in waypoints:
        layer_raw = wp.get('layer', 1)
        layer_idx = max(0, min(num_layers - 1, int(layer_raw) - 1))
        result.setdefault(layer_idx, []).append(wp)
    return result


def _estimate_layer_dt(wps: list, params: dict) -> float:
    """Estimate time in seconds to print one layer."""
    path_length = 0.0
    for i in range(1, len(wps)):
        dx = wps[i]['x'] - wps[i-1]['x']
        dy = wps[i]['y'] - wps[i-1]['y']
        dz = wps[i]['z'] - wps[i-1]['z']
        path_length += math.sqrt(dx*dx + dy*dy + dz*dz)
    scan_speed = params.get('scan_speed', 10)  # mm/s
    dwell = params.get('dwell_time', 0)
    dt = path_length / max(scan_speed, 1e-6) + dwell
    return max(dt, 1.0)


def _deposit_heat(grid, ix, iy, iz, material, params):
    """Apply Gaussian laser heat source to a voxel neighbourhood."""
    sigma_vox = max((params.get('beam_spot', 1.2) / 2) / grid.dx, 0.5)
    Q_total = params['laser_power'] * params.get('absorption', 0.35)
    voxel_vol = grid.dx * grid.dy * grid.dz * 1e-9
    rho_Cp = material['density'] * material['Cp']
    v = max(params.get('scan_speed', 10), 1.0)  # mm/s
    dt_phys = grid.dx / v
    dt = min(params.get('dt', dt_phys), dt_phys)

    weights = {}
    total_w = 0.0
    for di in range(-3, 4):
        for dj in range(-3, 4):
            ii, jj = ix + di, iy + dj
            if not (0 <= ii < grid.NX and 0 <= jj < grid.NY and 0 <= iz < grid.NZ):
                continue
            w = math.exp(-(di*di + dj*dj) / (2 * sigma_vox * sigma_vox))
            weights[(ii, jj)] = w
            total_w += w

    if total_w < 1e-30:
        return

    for (ii, jj), w in weights.items():
        w_norm = w / total_w
        dT = Q_total * w_norm * dt / (rho_Cp * voxel_vol)
        grid.T[ii, jj, iz] = min(grid.T[ii, jj, iz] + dT, material['T_melt'] * 1.3)
        grid.active[ii, jj, iz] = True


def _diffuse_z(grid, iz, dt, material):
    """1-D Fourier diffusion in Z for one layer slice."""
    alpha = material['k'] / (material['density'] * material['Cp'])
    dz_m = grid.dz / 1000
    Fo = alpha * dt / (dz_m ** 2)
    if Fo > 0.45: Fo = 0.45
    T = grid.T
    T_above = T[:, :, iz+1] if iz+1 < grid.NZ else np.full((grid.NX, grid.NY), 25.0)
    T_below = T[:, :, iz-1] if iz > 0 else np.full((grid.NX, grid.NY), 20.0)
    T[:, :, iz] += Fo * (T_above - 2*T[:, :, iz] + T_below)


def _diffuse_xy(grid, dt, material, scan_speed_mm_s=10.0):
    """2-D explicit Fourier diffusion in XY plane across all active layers.

    Models slow lateral heat conduction between adjacent beads (inter-bead
    thermal resistance due to grain boundaries and partial bonding).

    The time step used for XY is dt_bead = dx / scan_speed — the time the
    laser spends crossing one voxel width. This is physically correct:
    lateral diffusion during a single bead pass is limited to the bead-crossing
    time, not the full layer time. Using the full layer dt would vastly
    overestimate lateral spreading.

    Fo_xy target ≈ 0.02 (small enough to be a gentle correction, large enough
    to affect neighbours over many layers).
    """
    alpha = material['k'] / (material['density'] * material['Cp'])
    dx_m  = grid.dx / 1000  # mm → m
    dy_m  = grid.dy / 1000

    # Physical time for laser to cross one voxel width
    v_ms = max(scan_speed_mm_s, 1.0) / 1000   # mm/s → m/s
    dt_bead = dx_m / v_ms                       # seconds

    Fo_x = alpha * dt_bead / (dx_m ** 2)
    Fo_y = alpha * dt_bead / (dy_m ** 2)

    # Cap at 0.01 per axis — gentle correction that accumulates over many layers
    # without draining the melt pool. Fo=0.01 × 67 layers = 0.67 effective transfer.
    if Fo_x > 0.01: Fo_x = 0.01
    if Fo_y > 0.01: Fo_y = 0.01

    T = grid.T
    A = grid.active.astype(float)   # 1 where active, 0 elsewhere

    # Neighbour temperatures — use local T only where neighbour is active,
    # otherwise use the voxel's own T (no flux through inactive boundary)
    T_left  = np.where(np.roll(A, 1, axis=0) > 0, np.roll(T, 1, axis=0), T)
    T_right = np.where(np.roll(A,-1, axis=0) > 0, np.roll(T,-1, axis=0), T)
    T_front = np.where(np.roll(A, 1, axis=1) > 0, np.roll(T, 1, axis=1), T)
    T_back  = np.where(np.roll(A,-1, axis=1) > 0, np.roll(T,-1, axis=1), T)

    # Apply only to active voxels
    dT = (Fo_x * (T_left - 2*T + T_right) +
          Fo_y * (T_front - 2*T + T_back))
    grid.T += dT * A


def _apply_convection(grid, iz, dt, params):
    """Apply surface convective cooling to the top layer slice."""
    h_conv = 35.0  # W/m²K
    T_amb = params.get('ambient_temp', 25)
    voxel_area = grid.dx * grid.dy * 1e-6  # mm² → m²
    denom = max(1e-10, grid.dx * grid.dy * grid.dz * 1e-9 * 7800 * 500)
    grid.T[:, :, iz] -= h_conv * (grid.T[:, :, iz] - T_amb) * dt * voxel_area / denom
    np.clip(grid.T[:, :, iz], T_amb, None, out=grid.T[:, :, iz])


def _compute_melt_pool(wps: list, material: dict, params: dict) -> dict | None:
    """
    Estimate melt pool dimensions and temperature using the Rosenthal solution.
    Returns a dict with length, width, depth (mm) and peak temperature (°C),
    or None if no deposition waypoints are present.
    """
    dep = [wp for wp in wps if wp.get('is_deposition', True)]
    if not dep:
        return None

    P       = float(params.get('laser_power', 1000))
    absorb  = float(params.get('absorption', 0.35))
    v       = float(params.get('scan_speed', 10)) / 1000        # mm/s → m/s
    T_amb   = float(params.get('ambient_temp', 25))
    T_melt  = float(material.get('T_melt', 1400))
    k       = float(material.get('k', 20))                       # W/m·K
    rho     = float(material.get('density', 7800))               # kg/m³
    Cp      = float(material.get('Cp', 490))                     # J/kg·K

    alpha   = k / max(rho * Cp, 1e-6)                            # thermal diffusivity m²/s
    Q_eff   = P * absorb

    v_safe = max(v, 1e-6)
    l_char  = 2 * alpha / v_safe  # m

    dT_melt = max(T_melt - T_amb, 1.0)
    beam_r   = float(params.get('beam_spot', 1.2)) / 2 / 1000  # m
    r_min    = max(beam_r, 1e-4)
    T_peak_est = T_amb + Q_eff / (2 * math.pi * k * r_min)
    T_peak_est = min(T_peak_est, T_melt * 2.5)

    length_m = l_char * math.log(max(T_peak_est - T_amb, 1) / max(dT_melt, 1))
    width_m  = math.sqrt(2 * alpha * abs(length_m) / max(v_safe, 1e-6))
    depth_m  = width_m * 0.4

    return {
        'length_mm': round(abs(length_m) * 1000, 2),
        'width_mm':  round(abs(width_m)  * 1000, 2),
        'depth_mm':  round(abs(depth_m)  * 1000, 2),
        'T_peak_C':  round(T_peak_est, 0),
    }


def _classify_risk(grid, material) -> list:
    """
    Classify each active voxel by thermal risk.
    Returns list of dicts with voxel index, world coords, temperature, cool_rate,
    n_remelt, T_overshoot, fatigue_idx, and severity ('HIGH', 'MEDIUM', 'LOW').
    """
    T_melt   = float(material.get('T_melt', 1400))
    T_solid  = T_melt * 0.85
    indices  = np.argwhere(grid.active)
    risks    = []

    for idx in indices:
        ix, iy, iz = int(idx[0]), int(idx[1]), int(idx[2])
        T_val    = float(grid.T[ix, iy, iz])
        T_pk     = float(grid.T_peak[ix, iy, iz])   # peak ever reached
        cr       = float(grid.cool_rate[ix, iy, iz])
        nr       = int(grid.n_remelt[ix, iy, iz])
        t_over   = float(grid.T_overshoot[ix, iy, iz])
        fatigue  = float(grid.fatigue_idx[ix, iy, iz])

        # Use T_peak (max ever reached) not current T for severity —
        # a voxel that reached T_melt and cooled back is still HIGH risk.
        if T_pk >= T_melt or cr > 500:
            severity = 'HIGH'
        elif T_pk >= T_solid or cr > 100:
            severity = 'MEDIUM'
        else:
            severity = 'LOW'

        risks.append({
            'ix': ix, 'iy': iy, 'iz': iz,
            'x': round(grid.x_min + ix * grid.dx, 3),
            'y': round(grid.y_min + iy * grid.dy, 3),
            'z': round(grid.z_min + iz * grid.dz, 3),
            'T': round(T_val, 1),
            'T_peak': round(T_pk, 1),
            'cool_rate': round(cr, 1),
            'n_remelt': nr,
            'T_overshoot': round(t_over, 1),
            'fatigue_idx': round(fatigue, 1),
            'severity': severity,
        })

    return risks


def _sample_voxels(grid, max_voxels: int = 500, material: dict = None) -> dict:
    """
    Downsample active voxels to at most max_voxels for JSON transport.
    When material is provided, also returns severity and n_remelt per voxel.
    """
    indices = np.argwhere(grid.active)
    if len(indices) == 0:
        return {'x': [], 'y': [], 'z': [], 't': [], 'severity': [], 'remelts': []}
    if len(indices) > max_voxels:
        # Random shuffle before slicing so sampled points cover the full volume,
        # not just ordered slices along one axis (argwhere returns sorted by ix).
        rng = np.random.default_rng(seed=42)
        rng.shuffle(indices)
        indices = indices[:max_voxels]
    ix, iy, iz = indices[:, 0], indices[:, 1], indices[:, 2]
    xs = (grid.x_min + ix * grid.dx).tolist()
    ys = (grid.y_min + iy * grid.dy).tolist()
    zs = (grid.z_min + iz * grid.dz).tolist()
    ts = grid.T[ix, iy, iz].tolist()
    remelts   = grid.n_remelt[ix, iy, iz].tolist()
    overshoot = grid.T_overshoot[ix, iy, iz].tolist()
    fatigue   = grid.fatigue_idx[ix, iy, iz].tolist()

    if material:
        T_melt  = float(material.get('T_melt', 1400))
        T_solid = T_melt * 0.85
        cr_arr  = grid.cool_rate[ix, iy, iz]
        tp_arr  = grid.T_peak[ix, iy, iz]   # use peak, not current T
        severity = []
        for i in range(len(ts)):
            if tp_arr[i] >= T_melt or cr_arr[i] > 500:
                severity.append('HIGH')
            elif tp_arr[i] >= T_solid or cr_arr[i] > 100:
                severity.append('MEDIUM')
            else:
                severity.append('LOW')
    else:
        severity = ['LOW'] * len(ts)

    return {'x': xs, 'y': ys, 'z': zs, 't': ts,
            'severity': severity, 'remelts': remelts,
            'overshoot': overshoot, 'fatigue': fatigue}


def _voxel_caps(grid) -> tuple:
    """
    Return (snap_cap, final_cap) — max voxels per snapshot and for final view.
    Scales with grid density so finer resolutions show proportionally more points.
    Base unit: 1 mm element → snap=1000, final=5000.
    Coarser (2mm) → half, finer (0.5mm) → double.
    """
    # Use dx as proxy for element size; clamp to reasonable range
    element_mm = max(0.4, min(3.0, grid.dx))
    scale = 1.0 / (element_mm ** 2)   # 2mm→0.25×, 1mm→1×, 0.5mm→4×
    snap_cap  = int(min(5000,  max(500,  round(1000 * scale))))
    final_cap = int(min(20000, max(2000, round(5000 * scale))))
    return snap_cap, final_cap


def _make_snapshot(grid, layer_idx: int, melt_pool, wps: list, snap_cap: int = 1000) -> dict:
    """Create a compact per-layer snapshot dict for animation."""
    T_max = float(np.max(grid.T[grid.active])) if grid.active.any() else 25.0
    T_avg = float(np.mean(grid.T[grid.active])) if grid.active.any() else 25.0
    return {
        'layer':        layer_idx,
        't_elapsed':    0.0,          # caller overwrites this
        'melt_pool':    melt_pool,
        'T_max':        T_max,
        'T_avg':        T_avg,
        'active_count': int(grid.active.sum()),
        'voxels':       _sample_voxels(grid, max_voxels=snap_cap),
    }


def _build_result(grid, snapshots: list, material: dict, params: dict) -> dict:
    """Assemble the final simulation result dict."""
    risks   = _classify_risk(grid, material)
    n_high  = sum(1 for r in risks if r['severity'] == 'HIGH')
    n_med   = sum(1 for r in risks if r['severity'] == 'MEDIUM')
    n_low   = sum(1 for r in risks if r['severity'] == 'LOW')

    T_max_final = float(np.max(grid.T[grid.active])) if grid.active.any() else 25.0
    T_avg_final = float(np.mean(grid.T[grid.active])) if grid.active.any() else 25.0
    _, final_cap = _voxel_caps(grid)

    # Improvement 2: overshoot summary
    T_melt = float(material.get('T_melt', 1400))
    active_mask = grid.active
    max_overshoot = float(np.max(grid.T_overshoot[active_mask])) if active_mask.any() else 0.0
    avg_overshoot = float(np.mean(grid.T_overshoot[active_mask])) if active_mask.any() else 0.0

    # Improvement 3: fatigue summary
    max_fatigue = float(np.max(grid.fatigue_idx[active_mask])) if active_mask.any() else 0.0
    avg_fatigue = float(np.mean(grid.fatigue_idx[active_mask])) if active_mask.any() else 0.0

    return {
        'ok':              True,
        'element_size_mm': round(grid.dx, 3),
        'grid_shape':      [grid.NX, grid.NY, grid.NZ],
        'grid_spacing':    {'dx': round(grid.dx, 3), 'dy': round(grid.dy, 3), 'dz': round(grid.dz, 3)},
        'active_voxels':   int(grid.active.sum()),
        'T_max_final':     T_max_final,
        'T_avg_final':     T_avg_final,
        'max_cool_rate':   float(np.max(grid.cool_rate)),
        'max_remelt':      int(np.max(grid.n_remelt)),
        # Improvement 2: temperature overshoot above T_melt
        'max_T_overshoot': round(max_overshoot, 1),
        'avg_T_overshoot': round(avg_overshoot, 3),
        # Improvement 3: fatigue index (thermal cycling amplitude)
        'max_fatigue_idx': round(max_fatigue, 1),
        'avg_fatigue_idx': round(avg_fatigue, 3),
        'risk_counts':     {'HIGH': n_high, 'MEDIUM': n_med, 'LOW': n_low},
        'risk_voxels':     risks[:2000],
        'snapshots':       snapshots,
        'num_layers':      len(snapshots),
        'final_voxels':    _sample_voxels(grid, max_voxels=final_cap, material=material),
    }


# ── Main simulation entry point ───────────────────────────────────────────────

def run_simulation(grid, waypoints: list, material: dict, params: dict,
                   progress_cb=None) -> dict:
    """
    Run the reduced-order thermal FEM simulation.

    Parameters
    ----------
    grid        : VoxelGrid (from build_grid)
    waypoints   : list of waypoint dicts with x, y, z, layer, is_deposition keys
    material    : dict with density, Cp, k, T_melt
    params      : dict with laser_power, scan_speed, absorption, beam_spot, etc.
    progress_cb : optional callable(pct: float, msg: str) for UI progress updates

    Returns
    -------
    dict — full result from _build_result
    """
    num_layers = grid.NZ
    layer_wps  = _group_by_layer(waypoints, num_layers)
    snapshots  = []
    T_prev     = grid.T.copy()
    t_elapsed  = 0.0
    snap_cap, _ = _voxel_caps(grid)

    for layer_idx in range(num_layers):
        if progress_cb:
            progress_cb(layer_idx / num_layers * 100, f'Layer {layer_idx+1}/{num_layers}')

        wps = layer_wps.get(layer_idx, [])
        dt  = _estimate_layer_dt(wps, params)
        params_with_dt = dict(params, dt=dt / max(len(wps), 1))

        for wp in wps:
            if not wp.get('is_deposition', True): continue
            ix, iy, iz = grid.world_to_grid(wp['x'], wp['y'], wp['z'])
            _deposit_heat(grid, ix, iy, iz, material, params_with_dt)

        _diffuse_z(grid, layer_idx, dt, material)
        _diffuse_xy(grid, dt, material, params.get('scan_speed', 10.0))   # Improvement 1
        _apply_convection(grid, layer_idx, dt, params)

        T_melt = material['T_melt']

        newly_solidified = (T_prev > T_melt) & (grid.T <= T_melt)
        if newly_solidified.any():
            grid.cool_rate[newly_solidified] = ((T_prev - grid.T) / max(dt, 0.001))[newly_solidified]
        grid.n_remelt[(grid.T > T_melt) & grid.active] += 1
        np.maximum(grid.T_peak, grid.T, out=grid.T_peak)

        # Improvement 2: accumulate temperature overshoot above T_melt
        overshoot_mask = grid.T > T_melt
        grid.T_overshoot[overshoot_mask] += grid.T[overshoot_mask] - T_melt

        # Improvement 3: fatigue index — accumulate |ΔT| at each thermal reversal
        # A reversal occurs when temp direction flips (heating→cooling or vice versa)
        dT_now  = grid.T - T_prev
        dT_prev = T_prev - grid._T_prev_cycle
        reversal = (dT_now * dT_prev < 0) & grid.active   # sign flip = cycle boundary
        grid.fatigue_idx[reversal] += np.abs(dT_now[reversal])
        grid._T_prev_cycle = np.where(reversal, T_prev, grid._T_prev_cycle)

        T_prev = grid.T.copy()

        melt_pool = _compute_melt_pool(wps, material, params)
        snap = _make_snapshot(grid, layer_idx, melt_pool, wps, snap_cap=snap_cap)
        snap['t_elapsed'] = t_elapsed
        t_elapsed += dt
        snapshots.append(snap)

    return _build_result(grid, snapshots, material, params)
