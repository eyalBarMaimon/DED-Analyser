"""
Stress engine — ISM (Inherent Strain Method) + Rosenthal thermal field.
Physics improvements:
  - Scan-direction anisotropy: transverse constraint > parallel
  - Gravity sag: self-weight of deposited material (dense alloys)
  - Rosenthal per-point cooling rate → local ε_in per waypoint
  - Toolpath pattern detection (raster vs contour) from waypoints
  - Layer-by-layer accumulated height uses actual Z from waypoints
Pure Python, no Flask dependency.
"""
import math
from collections import defaultdict

MECH_PROPS = {
    'SS316L':      {'E_GPa': 193, 'yield_MPa':  310, 'alpha_1e6': 16.0},
    '316L':        {'E_GPa': 193, 'yield_MPa':  310, 'alpha_1e6': 16.0},
    '316LSi':      {'E_GPa': 193, 'yield_MPa':  310, 'alpha_1e6': 16.0},
    'Ti-6Al-4V':   {'E_GPa': 114, 'yield_MPa':  880, 'alpha_1e6':  8.6},
    'Ti64':        {'E_GPa': 114, 'yield_MPa':  880, 'alpha_1e6':  8.6},
    'Titanium CP': {'E_GPa': 105, 'yield_MPa':  275, 'alpha_1e6':  8.4},
    'Inconel 625': {'E_GPa': 205, 'yield_MPa':  490, 'alpha_1e6': 12.8},
    'IN625':       {'E_GPa': 205, 'yield_MPa':  490, 'alpha_1e6': 12.8},
    'H13':         {'E_GPa': 210, 'yield_MPa': 1200, 'alpha_1e6': 11.5},
    'ER70S-6':     {'E_GPa': 200, 'yield_MPa':  480, 'alpha_1e6': 12.0},
    'ER70S6':      {'E_GPa': 200, 'yield_MPa':  480, 'alpha_1e6': 12.0},
    'copper':      {'E_GPa': 128, 'yield_MPa':  340, 'alpha_1e6': 17.0},
    'Hastelloy':     {'E_GPa': 205, 'yield_MPa':  414, 'alpha_1e6': 12.8},
    'Aluminium 6061':{'E_GPa':  68, 'yield_MPa':  276, 'alpha_1e6': 23.6},
    'Al6061':        {'E_GPa':  68, 'yield_MPa':  276, 'alpha_1e6': 23.6},
}


def lookup_mech(display_name: str) -> dict:
    name_lower = display_name.lower()
    return next((v for k, v in MECH_PROPS.items() if k.lower() in name_lower), {})


def estimate_wall_thickness(waypoints: list, wire_diameter_mm: float = 1.2) -> float:
    """
    Auto-estimate wall thickness from deposition waypoints.
    Delegates to _analyse_toolpath for geometry detection.
    Returns value in mm.
    """
    if not waypoints:
        return 5.0
    tp = _analyse_toolpath(waypoints)
    if tp['coil_radius_mm'] > 0:
        return wire_diameter_mm
    dx, dy = tp['xy_extent']
    wall = min(dx, dy) / 4
    return max(wall, wire_diameter_mm)


# ── Toolpath geometry analysis ────────────────────────────────────────────────

def _analyse_toolpath(waypoints: list) -> dict:
    """
    Derive geometry and scan-pattern from deposition waypoints.
    Returns:
      pattern        : 'contour' | 'raster' | 'helix'
      build_axis     : 'z' (always for Meltio — vertical build)
      scan_dirs      : list of (ux,uy) unit vectors per segment (XY plane)
      dominant_dir   : (ux,uy) — most common scan direction
      z_per_layer    : average Z increment per layer [mm]
      centroid       : (cx, cy, cz)
      xy_extent      : (dx, dy) bounding box [mm]
      z_extent       : total build height [mm]
      coil_radius_mm : mean radius for helix geometry (0 if not helix)
      layer_z        : {layer_num → mean_z_mm}
      gravity_vec    : (0, 0, -1) — always downward
    """
    dep = [wp for wp in waypoints if wp.get('is_deposition', True)]
    if not dep:
        dep = waypoints or []
    if not dep:
        return {
            'pattern': 'raster', 'scan_dirs': [], 'dominant_dir': (1.0, 0.0),
            'z_per_layer': 0.5, 'centroid': (0, 0, 0),
            'xy_extent': (10, 10), 'z_extent': 1.0,
            'coil_radius_mm': 0.0, 'layer_z': {}, 'gravity_vec': (0, 0, -1),
        }

    # Centroid
    xs = [wp['x'] for wp in dep]
    ys = [wp['y'] for wp in dep]
    zs = [wp['z'] for wp in dep]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    cz = (min(zs) + max(zs)) / 2
    dx_ext = max(xs) - min(xs)
    dy_ext = max(ys) - min(ys)
    z_ext  = max(zs) - min(zs)

    # Layer → mean Z
    by_layer = defaultdict(list)
    for wp in dep:
        by_layer[wp.get('layer_num', wp.get('layer', 1))].append(wp['z'])
    layer_z = {ln: sum(zlist) / len(zlist) for ln, zlist in by_layer.items()}

    # Z per layer from consecutive layer mean-Z differences
    layers_sorted = sorted(layer_z.keys())
    dz_list = []
    for i in range(1, len(layers_sorted)):
        dz = abs(layer_z[layers_sorted[i]] - layer_z[layers_sorted[i-1]])
        if dz > 0.05:   # ignore near-zero (same z level)
            dz_list.append(dz)
    z_per_layer = (sum(dz_list) / len(dz_list)) if dz_list else 0.5

    # Scan direction vectors per segment (XY only)
    scan_dirs = []
    for i in range(1, min(len(dep), 2000)):
        dx = dep[i]['x'] - dep[i-1]['x']
        dy = dep[i]['y'] - dep[i-1]['y']
        seg_len = math.sqrt(dx*dx + dy*dy)
        if seg_len > 0.5:   # skip micro-moves
            scan_dirs.append((dx / seg_len, dy / seg_len))

    # Dominant direction — bin by angle into 36 × 5° buckets
    dominant_dir = (1.0, 0.0)
    if scan_dirs:
        angle_bins = defaultdict(float)
        for ux, uy in scan_dirs:
            ang = math.degrees(math.atan2(uy, ux)) % 180  # fold ±180 → 0..180
            bucket = round(ang / 5) * 5
            angle_bins[bucket] += 1
        top_ang = max(angle_bins, key=angle_bins.__getitem__)
        rad = math.radians(top_ang)
        dominant_dir = (math.cos(rad), math.sin(rad))

    # Pattern detection
    # Helix: Z span within a single layer > 3× z_per_layer
    z_spans_in_layer = []
    for pts in list(by_layer.values())[:10]:
        if len(pts) > 1:
            z_spans_in_layer.append(max(pts) - min(pts))
    avg_inlayer_zspan = sum(z_spans_in_layer) / len(z_spans_in_layer) if z_spans_in_layer else 0

    coil_radius_mm = 0.0
    if z_per_layer > 0 and avg_inlayer_zspan > 3 * z_per_layer:
        pattern = 'helix'
        radii = [math.sqrt((x - cx)**2 + (y - cy)**2) for x, y in zip(xs[:200], ys[:200])]
        coil_radius_mm = sum(radii) / len(radii) if radii else 0.0
    else:
        # Raster: dominant direction changes sign frequently (back-and-forth)
        # Contour: direction rotates smoothly (always turning same way)
        sign_changes = 0
        for i in range(1, len(scan_dirs)):
            dot = scan_dirs[i][0]*scan_dirs[i-1][0] + scan_dirs[i][1]*scan_dirs[i-1][1]
            if dot < -0.5:    # near-reversal
                sign_changes += 1
        raster_ratio = sign_changes / max(len(scan_dirs), 1)
        pattern = 'raster' if raster_ratio > 0.05 else 'contour'

    return {
        'pattern':        pattern,
        'scan_dirs':      scan_dirs,
        'dominant_dir':   dominant_dir,
        'z_per_layer':    z_per_layer,
        'centroid':       (cx, cy, cz),
        'xy_extent':      (dx_ext, dy_ext),
        'z_extent':       z_ext,
        'coil_radius_mm': coil_radius_mm,
        'layer_z':        layer_z,
        'gravity_vec':    (0.0, 0.0, -1.0),
    }


def _rosenthal_cooling_rate(laser_P, absorb, scan_v_ms, k_therm, alpha_diff, r_m=0.001):
    """
    Rosenthal solution: peak cooling rate at distance r from melt-pool centre.
    dT/dt [K/s] = (absorb·P · scan_v) / (2π·k·r²)  ×  exp(−scan_v·r/(2α))
    Wire-DED: r ≈ bead half-width (~1 mm).
    """
    Q = absorb * laser_P
    if Q <= 0 or k_therm <= 0 or scan_v_ms <= 0:
        return 1000.0   # fallback
    exponent = -scan_v_ms * r_m / (2 * max(alpha_diff, 1e-9))
    dTdt = (Q * scan_v_ms) / (2 * math.pi * k_therm * r_m**2) * math.exp(exponent)
    return max(dTdt, 10.0)


# ── Scan-direction anisotropy factor ─────────────────────────────────────────

def _scan_anisotropy(pattern: str) -> tuple:
    """
    Return (f_transverse, f_parallel) stress scaling factors.
    Transverse to scan = higher constraint → higher σ.
    Literature (Colegrove 2017, Ding 2014):
      raster:  σ_trans ≈ 1.35×σ_parallel
      contour: σ_trans ≈ 1.15× (more symmetric)
      helix:   σ_hoop  ≈ 1.20× (circumferential dominates)
    """
    if pattern == 'raster':
        return 1.35, 1.00
    elif pattern == 'contour':
        return 1.15, 1.05
    else:   # helix
        return 1.20, 1.20


# ── Gravity sag ───────────────────────────────────────────────────────────────

def _gravity_sag(rho, g, h_m, wall_t_m, E):
    """
    Self-weight tip deflection of a cantilever beam of height h [m].
    For a uniformly distributed load w = ρ·g per unit length (unit width):
      δ = w·h⁴ / (8·E·I)  where I = wall_t³/12 per unit width
        = ρ·g·h⁴ / (2·E·wall_t²)
    (Gere & Goodno §9.4; previous formula had h³ instead of h⁴ and used I/A incorrectly)
    Returns sag in mm.
    """
    g_acc = 9.81
    wt    = max(wall_t_m, 0.001)
    sag_m = rho * g_acc * h_m**4 / (2 * E * wt**2)
    return sag_m * 1000   # → mm


# ── Main stress computation ───────────────────────────────────────────────────

# (section, key, label, unit, printer_fixed)
SWEEP_PARAMS = [
    ('process',  'laser_power',    'Laser Power',        'W',       True),
    ('process',  'scan_speed',     'Scan Speed',         'mm/s',    True),
    ('process',  'wire_feed_speed','Wire Feed Speed',    'mm/s',    True),
    ('process',  'layer_height',   'Layer Height',       'mm',      True),
    ('geometry', 'num_layers',     'Num Layers',         '-',       True),
    ('process',  'absorption',     'Absorption',         '-',       False),
    ('process',  'ambient_temp',   'Ambient/Preheat',    'C',       False),
    ('process',  'dwell_time',     'Dwell Time',         's',       False),
    ('process',  'bead_width',     'Bead Width',         'mm',      False),
    ('process',  'wire_diameter',  'Wire Diameter',      'mm',      False),
    ('geometry', 'wall_thickness', 'Wall Thickness',     'mm',      False),
]

# Minimum influence threshold to consider a param "responsive" at a given range
_INFL_THRESH_SIG = 1.0   # MPa
_INFL_THRESH_DEL = 0.05  # mm
# Adaptive expansion tiers: if ±10% gives nothing, try these
_ADAPTIVE_TIERS  = [10, 30, 60, 90, 200]


def run_sensitivity_sweep(payload: dict) -> dict:
    """
    Adaptive sensitivity sweep:
    - Runs ±10% on every parameter first.
    - For user-adjustable params where ±10% produces < threshold change,
      expands automatically to ±30%, ±60%, ±90%, ±200% until an effect is found.
    - Computes optimal dwell to cross GO/CAUTION threshold.
    - Returns structured dict ready for the UI.
    """
    import copy

    stripped = {k: copy.deepcopy(v) for k, v in payload.items() if k != 'waypoints'}
    stripped['waypoints'] = []
    base = compute_stress(stripped, _sweep=False)
    if not base:
        return {}

    b_sig  = base['summary']['max_sigma_MPa']
    b_del  = base['summary']['max_delta_mm']
    b_util = base['summary']['utilization_pct']
    b_nogo = base['go_nogo']
    b_yld  = base['summary']['yield_MPa']

    rows = []
    for section, key, label, unit, printer_fixed in SWEEP_PARAMS:
        bv = stripped.get(section, {}).get(key)
        if bv is None or bv == 0:
            continue

        # Determine scan range — adaptive for user params
        tiers = [10] if printer_fixed else _ADAPTIVE_TIERS
        used_pct = 10
        ds_lo = ds_hi = dd_lo = dd_hi = 0.0

        for pct in tiers:
            f = pct / 100.0
            lo_p = copy.deepcopy(stripped); lo_p[section][key] = bv * (1 - f)
            hi_p = copy.deepcopy(stripped); hi_p[section][key] = bv * (1 + f)
            lo_r = compute_stress(lo_p, _sweep=False)
            hi_r = compute_stress(hi_p, _sweep=False)
            if not lo_r or not hi_r:
                continue
            ds_lo = lo_r['summary']['max_sigma_MPa'] - b_sig
            ds_hi = hi_r['summary']['max_sigma_MPa'] - b_sig
            dd_lo = lo_r['summary']['max_delta_mm']  - b_del
            dd_hi = hi_r['summary']['max_delta_mm']  - b_del
            used_pct = pct
            sig_range = abs(ds_hi - ds_lo)
            del_range = abs(dd_hi - dd_lo)
            # Stop expanding once we see a meaningful response
            if sig_range >= _INFL_THRESH_SIG or del_range >= _INFL_THRESH_DEL:
                break

        infl_sig = abs(ds_hi - ds_lo) / (b_sig + 1e-9) * 100
        infl_del = abs(dd_hi - dd_lo) / (b_del + 1e-9) * 100

        rows.append({
            'key':           key,
            'label':         label,
            'unit':          unit,
            'printer_fixed': printer_fixed,
            'base':          round(bv, 4),
            'scan_pct':      used_pct,
            'val_lo':        round(bv * (1 - used_pct/100), 4),
            'val_hi':        round(bv * (1 + used_pct/100), 4),
            'd_sigma_lo':    round(ds_lo, 2),
            'd_sigma_hi':    round(ds_hi, 2),
            'd_delta_lo':    round(dd_lo, 3),
            'd_delta_hi':    round(dd_hi, 3),
            'infl_sigma':    round(infl_sig, 1),
            'infl_delta':    round(infl_del, 1),
            'expanded':      used_pct > 10 and not printer_fixed,
        })

    # Sort by combined influence descending
    rows.sort(key=lambda r: -(r['infl_sigma'] + r['infl_delta']))

    # ── Optimal dwell recommendation ─────────────────────────────────────────
    dwell_rec = None
    dwell_key_present = any(r['key'] == 'dwell_time' for r in rows)
    if dwell_key_present:
        base_dwell = stripped.get('process', {}).get('dwell_time', 10.0)
        target_util = 75.0   # aim for CAUTION (util < 80%)
        best_dwell = None
        for d in [10, 20, 30, 45, 60, 90, 120, 180, 240]:
            p2 = copy.deepcopy(stripped)
            p2['process']['dwell_time'] = d
            r2 = compute_stress(p2, _sweep=False)
            if r2 and r2['summary']['utilization_pct'] <= target_util:
                best_dwell = d
                break
        if best_dwell and best_dwell > base_dwell * 1.1:
            verdict_at = compute_stress(
                {**copy.deepcopy(stripped),
                 'process': {**stripped.get('process',{}), 'dwell_time': best_dwell}},
                _sweep=False)
            dwell_rec = {
                'current_s':    round(base_dwell, 1),
                'recommended_s': best_dwell,
                'sigma_at':     round(verdict_at['summary']['max_sigma_MPa'], 1) if verdict_at else None,
                'util_at':      round(verdict_at['summary']['utilization_pct'], 1) if verdict_at else None,
                'verdict_at':   verdict_at['go_nogo'] if verdict_at else None,
            }

    # ── Early-warning flags ───────────────────────────────────────────────────
    warnings = []
    for r in rows:
        if not r['printer_fixed'] and r['infl_sigma'] < 0.5 and r['infl_delta'] < 1.0:
            warnings.append({
                'param':     r['label'],
                'scan_pct':  r['scan_pct'],
                'message':   (f"No significant response even at ±{r['scan_pct']}% "
                              f"— this parameter does not control stress/distortion "
                              f"in this regime"),
            })

    return {
        'base_sigma':   round(b_sig, 1),
        'base_delta':   round(b_del, 3),
        'base_util':    round(b_util, 1),
        'base_verdict': b_nogo,
        'yield_MPa':    b_yld,
        'rows':         rows,
        'dwell_rec':    dwell_rec,
        'warnings':     warnings,
    }


def compute_stress(data: dict, _sweep: bool = True, tolerances: dict = None) -> dict | None:
    """
    ISM residual-stress + distortion prediction with toolpath-aware physics.
    Input keys: material{}, process{}, geometry{}, waypoints[].
    """
    try:
        mat       = data.get('material', {})
        E_GPa     = float(mat.get('E_GPa',    200))
        yield_MPa = float(mat.get('yield_MPa', 400))
        alpha     = float(mat.get('alpha_1e6', 12.0)) * 1e-6
        rho       = float(mat.get('density',   7800))
        Cp        = float(mat.get('Cp',         490))
        k_therm   = float(mat.get('k',           20))
        T_melt    = float(mat.get('T_melt',    1400))

        proc      = data.get('process', {})
        laser_P   = float(proc.get('laser_power',    1000))
        scan_v    = float(proc.get('scan_speed',       10)) / 1000   # mm/s → m/s
        h_layer   = float(proc.get('layer_height',    0.5)) / 1000   # mm → m
        w_bead    = float(proc.get('bead_width',      2.0)) / 1000
        absorb    = float(proc.get('absorption',      0.35))
        T_amb     = float(proc.get('ambient_temp',      25))
        dwell_s   = float(proc.get('dwell_time',        5))
        wire_d    = float(proc.get('wire_diameter',   1.2)) / 1000
        wire_v    = float(proc.get('wire_feed_speed',  80)) / 1000

        geom       = data.get('geometry', {})
        num_layers = int(geom.get('num_layers',       50))
        wall_t     = float(geom.get('wall_thickness',  5)) / 1000
        waypoints  = data.get('waypoints', [])

        E       = E_GPa * 1e9
        sigma_Y = yield_MPa * 1e6
        dT_melt = max(1.0, T_melt - T_amb)

        # ── Toolpath geometry analysis ────────────────────────────────────
        tp = _analyse_toolpath(waypoints)
        pattern          = tp['pattern']
        dominant_dir     = tp['dominant_dir']   # (ux, uy) scan direction
        coil_radius_m    = tp['coil_radius_mm'] / 1000
        # Use actual Z increment from toolpath when waypoints exist; otherwise use process param.
        if waypoints and tp['z_per_layer'] > 0.05:
            z_per_layer_mm = tp['z_per_layer']
        else:
            z_per_layer_mm = h_layer * 1000   # mm (h_layer is already in m)
        h_layer_actual   = z_per_layer_mm / 1000

        # ── Thermal diffusivity & Rosenthal cooling rate ─────────────────
        alpha_diff  = k_therm / (rho * Cp)
        # Characteristic radius = bead half-width (melt-pool edge where solidification stress locks in)
        r_bead      = w_bead / 2
        dTdt        = _rosenthal_cooling_rate(laser_P, absorb, scan_v, k_therm, alpha_diff, r_bead)

        # Inherent strain factor f_c from Rosenthal cooling rate.
        # Reference calibration: 1500W, 10mm/s, 2mm bead (r=1mm), SS316L
        # → dTdt ≈ 15000 K/s at melt-pool edge (high, but typical for fine-mesh DED)
        # Literature f_c for wire-DED: 0.03–0.08 (Colegrove 2017, Williams 2016)
        # Use log-scaling (not sqrt) to compress the wide dTdt range
        dTdt_ref = 15000.0   # K/s at reference conditions
        f_c_base = 0.045
        f_c      = f_c_base * (math.log10(max(dTdt, 10)) / math.log10(max(dTdt_ref, 100)))
        f_c      = max(0.015, min(f_c, 0.095))

        V_dot         = max(math.pi * (wire_d / 2)**2 * wire_v, 1e-15)
        ETA_SUPERHEAT = 0.20
        dT_superheat  = max(50.0, min((laser_P * absorb * ETA_SUPERHEAT) / (rho * Cp * V_dot), 350.0))
        dT_actual     = dT_melt + dT_superheat

        # Inherent strain (Ueda/Luo ISM):
        #   ε_in = α · ΔT_melt · f_c
        # where α·ΔT_melt is the free thermal strain at solidification (~0.015–0.025
        # for common alloys), and f_c is the fraction that gets locked in as plastic
        # misfit (calibrated 0.015–0.095 from literature).
        # This gives ε_in ≈ 0.0003–0.002, σ_pass ≈ 60–400 MPa — physically correct range.
        # NOTE: f_c itself is NOT a strain; it is a dimensionless retention coefficient.
        eps_in     = alpha * dT_melt * f_c
        sigma_pass = E * eps_in

        # ── Scan-direction anisotropy ─────────────────────────────────────
        f_trans, f_para = _scan_anisotropy(pattern)
        # Blend: use transverse for σ accumulation (dominant failure mode)
        scan_factor = f_trans

        # ── Stress relaxation per dwell ───────────────────────────────────
        # Characteristic stress-relaxation time for wire-DED is dominated by
        # conduction cooling through prior layers.  For a layer of thickness h,
        # τ_cond = h_total / (π² α_diff) is the diffusive timescale, but for
        # typical wire-DED parts (h_total ~ 5-50 mm) this yields 30-3000 s —
        # consistent with observation that 30-120 s dwell matters.
        # Use h_layer × num_deposited as a proxy for heated zone depth.
        tau_relax = max(20.0, (h_layer_actual * 5)**2 / (math.pi**2 * max(alpha_diff, 1e-12)))
        relief    = 1.0 - math.exp(-dwell_s / tau_relax)

        # ── Helical curvature bending stress ─────────────────────────────
        is_helix = (pattern == 'helix')
        sigma_bend_base = 0.0
        if is_helix and coil_radius_m > 0:
            sigma_bend_base = min(E * (wire_d / 2) / coil_radius_m, sigma_Y * 0.3)

        # ── Per-layer accumulation ────────────────────────────────────────
        # sigma_pass is the elastic mismatch stress (driving force, can exceed yield).
        # sigma_cum is the accumulated residual stress, clamped to yield (plasticity).
        # The driving force sigma_pass * factors determines how fast sigma_cum builds;
        # relief from dwell controls how much it relaxes between layers.
        per_layer = []
        sigma_cum = 0.0
        h_cum_m   = 0.0
        for n in range(1, num_layers + 1):
            h_cum_m += h_layer_actual

            # Substrate constraint: layers 1-3 have extra constraint from cold substrate
            base_factor = 1.40 if n <= 3 else (1.15 if n <= 6 else 1.0)
            # Thin-wall amplification
            thin_factor = 1.20 if wall_t < 0.004 else 1.0

            # Per-layer increment = fraction of sigma_pass that becomes locked residual
            # scan_factor amplifies transverse direction (raster effect)
            sigma_inc  = sigma_pass * base_factor * thin_factor * scan_factor * 0.65
            sigma_cum  = sigma_cum * (1.0 - relief) + sigma_inc
            sigma_cum  = min(sigma_cum, sigma_Y)

            sigma_total = min(sigma_cum + sigma_bend_base, sigma_Y)

            # Bending distortion — ISM cantilever beam (Luo & Ueda 1993, eq. 12)
            # δ = 3·ε_in·t_layer·h² / wall_t²
            # t_layer = deposited layer thickness, h = current build height, wall_t = wall thickness
            # (previous formula had missing t_layer and wrong wall_t power)
            delta_bend_mm = (3 * eps_in * h_layer_actual * h_cum_m**2
                             / max(wall_t, 0.001)**2) * 1000

            # Gravity sag (self-weight of dense material)
            delta_sag_mm  = _gravity_sag(rho, 9.81, h_cum_m, wall_t, E)

            # Total distortion = bending + sag (additive when same direction, else RSS)
            # For vertical build, sag is lateral and bending is also lateral → additive
            delta_mm = delta_bend_mm + delta_sag_mm

            ratio = sigma_total / sigma_Y
            per_layer.append({
                'layer':          n,
                'height_mm':      round(h_cum_m * 1000, 2),
                'sigma_MPa':      round(sigma_total / 1e6, 1),
                'delta_mm':       round(delta_mm, 3),
                'delta_bend_mm':  round(delta_bend_mm, 3),
                'delta_sag_mm':   round(delta_sag_mm, 3),
                'ratio':          round(ratio, 3),
                'risk':           'HIGH' if ratio > 0.80 else ('MEDIUM' if ratio > 0.50 else 'LOW'),
            })

        max_sigma = max(l['sigma_MPa'] for l in per_layer)
        avg_sigma = round(sum(l['sigma_MPa'] for l in per_layer) / len(per_layer), 1)
        max_delta = max(l['delta_mm'] for l in per_layer)
        high_risk = sum(1 for l in per_layer if l['risk'] == 'HIGH')

        # ── Per-waypoint stress + displacement field ──────────────────────
        # Includes intra-layer thermal gradient (feature A):
        #   Within a single layer, the first waypoint is deposited hot; each
        #   subsequent waypoint has had more time to cool relative to the layer
        #   start.  A hotter deposition point locks in more inherent strain →
        #   higher local residual stress.  We model this with a cooldown factor
        #   based on the arc-length position within the layer segment:
        #     cool_frac = arc_pos / layer_arc_length  (0 = start, 1 = end)
        #     intra_factor = 1 + INTRA_GRAD * (1 - cool_frac)
        #   INTRA_GRAD = 0.25 → ±12.5% variation within a layer.
        INTRA_GRAD = 0.25

        stress_wps = []
        if waypoints:
            max_layer   = max((wp.get('layer_num', wp.get('layer', 1)) for wp in waypoints), default=1)
            layer_sigma = {l['layer']: l['sigma_MPa'] for l in per_layer}
            layer_delta = {l['layer']: l['delta_mm']  for l in per_layer}
            layer_sag   = {l['layer']: l['delta_sag_mm'] for l in per_layer}

            # Centroid from toolpath
            cx, cy, _ = tp['centroid']

            # Transverse-to-scan unit vector (perpendicular to dominant scan dir in XY)
            tx, ty = dominant_dir
            perp_x, perp_y = -ty, tx   # 90° CCW rotation

            all_xs = [wp['x'] for wp in waypoints]
            all_ys = [wp['y'] for wp in waypoints]
            x_min, x_max = min(all_xs), max(all_xs)
            y_min, y_max = min(all_ys), max(all_ys)
            x_span = max(x_max - x_min, 1.0)
            y_span = max(y_max - y_min, 1.0)

            # Pre-compute cumulative arc-length within each layer for intra-layer gradient
            # layer_arc[lay] = total XYZ arc length of deposition points in that layer
            layer_arc: dict[int, float] = {}
            layer_wp_idx: dict[int, list] = defaultdict(list)
            for i, wp in enumerate(waypoints):
                lay_key = wp.get('layer_num', wp.get('layer', 1))
                layer_wp_idx[lay_key].append(i)
            # cumulative arc per waypoint index within its layer
            wp_arc_frac: list[float] = [0.0] * len(waypoints)
            for lay_key, idxs in layer_wp_idx.items():
                cum = 0.0
                arcs = [0.0]
                for k in range(1, len(idxs)):
                    a = waypoints[idxs[k]]
                    b = waypoints[idxs[k-1]]
                    seg = math.sqrt((a['x']-b['x'])**2 + (a['y']-b['y'])**2 + (a['z']-b['z'])**2)
                    cum += seg
                    arcs.append(cum)
                total = max(cum, 1e-9)
                for k, idx in enumerate(idxs):
                    wp_arc_frac[idx] = arcs[k] / total   # 0 = layer start, 1 = layer end

            for i, wp in enumerate(waypoints):
                lay    = wp.get('layer_num', wp.get('layer', 1))
                mapped = max(1, min(num_layers, round(lay / max(max_layer, 1) * num_layers)))
                sigma_base = layer_sigma.get(mapped, avg_sigma)
                delta  = layer_delta.get(mapped, 0.0)
                sag    = layer_sag.get(mapped, 0.0)

                # Intra-layer gradient: stress peaks at turnaround points (start and end
                # of scan track) where velocity decelerates — consistent with Colegrove 2017
                # and Ding et al. 2011 (both ends of raster tracks show higher residual stress).
                # Symmetric parabolic form: max at arc_frac=0 and arc_frac=1, min at 0.5
                cool_frac    = wp_arc_frac[i]
                intra_factor = 1.0 + INTRA_GRAD * abs(1.0 - 2.0 * cool_frac)
                sigma = min(sigma_base * intra_factor, yield_MPa)

                # Displacement vectors — physics-based decomposition:
                #
                # Dominant distortion modes in DED (Ding et al. 2011, Hönnige 2018):
                #   (a) Z-bending: tip deflects in Z (cantilever bending of build column)
                #       → disp_z = +delta (bending lifts the tip upward relative to base)
                #   (b) Lateral bowing: transverse to scan direction
                #       → disp in perp_x/perp_y direction (f_trans > f_para so perp dominates)
                #   (c) Radial expansion valid only for closed-contour (helix) geometry
                #
                # For raster/contour: lateral bowing is primary in-plane mode.
                # For helix: radial expansion is valid.
                rx = wp['x'] - cx
                ry = wp['y'] - cy
                r_dist = max(math.sqrt(rx*rx + ry*ry), 1e-6)

                if pattern == 'helix':
                    # Closed contour → radial expansion
                    disp_x = (rx / r_dist) * delta
                    disp_y = (ry / r_dist) * delta
                else:
                    # Open wall/raster → lateral bowing transverse to scan
                    # perp_x/perp_y already defined as 90° CCW from dominant scan dir
                    # Scale by position along transverse axis (zero at centre, max at edges)
                    trans_pos = abs(rx * perp_x + ry * perp_y) / max(x_span, y_span, 1.0)
                    disp_x = perp_x * delta * trans_pos
                    disp_y = perp_y * delta * trans_pos

                # Z: cantilever tip lift (+Z) proportional to height fraction
                h_frac = wp.get('z', 0) / max(tp['z_extent'], 1.0)
                disp_z = delta * h_frac  # tip lifts, base stays fixed

                stress_wps.append({
                    'x':         wp['x'],
                    'y':         wp['y'],
                    'z':         wp['z'],
                    'sigma_MPa': round(sigma, 1),
                    'delta_mm':  round(delta, 3),
                    'disp_x':    round(disp_x, 3),
                    'disp_y':    round(disp_y, 3),
                    'disp_z':    round(disp_z, 3),
                    'ratio':     round(sigma / yield_MPa, 3),
                    'layer':     lay,
                    'arc_frac':  round(cool_frac, 3),
                })

        # ── Risk zones ────────────────────────────────────────────────────
        risk_zones = []
        scan_spd = proc.get('scan_speed', 10)
        lh_cur   = proc.get('layer_height', 0.5)
        amb_cur  = proc.get('ambient_temp', 25)

        if high_risk:
            dwell_fix = max(30, int(round(dwell_s * 1.5 / 5)) * 5)
            scan_fix  = round(scan_spd * 0.8, 1)
            risk_zones.append({
                'zone': 'High-stress layers', 'severity': 'HIGH',
                'detail': f'{high_risk} layer(s) exceed 80% of yield ({yield_MPa} MPa)',
                'recommendation': f'Increase dwell time to ≥{dwell_fix}s or reduce scan speed to {scan_fix} mm/s',
                'param_key': 'minLayerDwell', 'param_suggested': dwell_fix,
                'param_unit': 's', 'param_label': 'Dwell Time',
            })
        if wall_t < 0.003:
            lh_fix = round(lh_cur * 0.8, 2)
            risk_zones.append({
                'zone': f'Thin wall ({wall_t*1000:.1f} mm)', 'severity': 'HIGH',
                'detail': 'Reduced lateral constraint → elevated deformation risk',
                'recommendation': f'Reduce layer height to {lh_fix} mm, increase inter-pass cooling',
                'param_key': 'layerHeight', 'param_suggested': lh_fix,
                'param_unit': 'mm', 'param_label': 'Layer Height',
            })
        if max_delta > 0.5:
            dwell_fix2 = max(60, int(round(dwell_s * 2 / 5)) * 5)
            risk_zones.append({
                'zone': 'Bending + gravity distortion', 'severity': 'HIGH' if max_delta >= 2 else 'MEDIUM',
                'detail': f'Predicted tip deflection: {max_delta:.2f} mm (bend: {per_layer[-1]["delta_bend_mm"]:.2f} mm + sag: {per_layer[-1]["delta_sag_mm"]:.2f} mm)',
                'recommendation': f'Increase dwell to ≥{dwell_fix2}s; consider pre-deformation compensation',
                'param_key': 'minLayerDwell', 'param_suggested': dwell_fix2,
                'param_unit': 's', 'param_label': 'Dwell Time',
            })
        if pattern == 'raster':
            risk_zones.append({
                'zone': 'Raster scan pattern', 'severity': 'MEDIUM',
                'detail': f'Raster increases transverse σ by ~{round((f_trans-1)*100)}% vs. parallel direction',
                'recommendation': 'Consider contour or alternating-angle strategy to reduce anisotropic stress',
            })
        if is_helix and coil_radius_m > 0:
            risk_zones.append({
                'zone': 'Helical geometry — curvature stress', 'severity': 'MEDIUM',
                'detail': f'Coil radius ≈ {coil_radius_m*1000:.0f} mm; wire bending adds σ_bend component',
                'recommendation': 'Verify wire diameter matches path radius; consider inter-winding cooling',
            })
        max_sag = max(l['delta_sag_mm'] for l in per_layer)
        if max_sag > 0.1:
            risk_zones.append({
                'zone': 'Gravity sag (self-weight)', 'severity': 'MEDIUM' if max_sag > 0.5 else 'LOW',
                'detail': f'Material density {rho:.0f} kg/m³ → gravity sag ≈ {max_sag:.2f} mm at full height',
                'recommendation': 'Add interlayer supports or reduce unsupported span for tall thin-wall sections',
            })
        amb_fix = max(150, int(amb_cur)) if amb_cur < 150 else int(amb_cur)
        risk_zones.append({
            'zone': 'Base–substrate interface', 'severity': 'MEDIUM',
            'detail': 'Layers 1–3: ~1.4× stress amplification from substrate constraint',
            'recommendation': f'Pre-heat substrate to ≥150 °C (currently {int(amb_cur)} °C)',
            'param_key': 'ambientTemp', 'param_suggested': amb_fix,
            'param_unit': '°C', 'param_label': 'Substrate Temp',
        })

        # ── Go / No-Go ────────────────────────────────────────────────────
        utilization = max_sigma / yield_MPa * 100
        if utilization >= 80 or high_risk > num_layers * 0.3:
            go_nogo = 'NO-GO'
        elif utilization >= 50 or max_delta > 1.0:
            go_nogo = 'CAUTION'
        else:
            go_nogo = 'GO'

        sensitivity = [
            {'param': 'Yield strength σ_Y',      'influence': 40, 'detail': f'{yield_MPa} MPa'},
            {'param': 'Thermal expansion α',       'influence': 30, 'detail': f'{mat.get("alpha_1e6", 12)} ×10⁻⁶ /K'},
            {'param': 'Cooling rate (scan speed)', 'influence': 15, 'detail': f'{proc.get("scan_speed", 10)} mm/s'},
            {'param': 'Layer height',              'influence':  8, 'detail': f'{proc.get("layer_height", 0.5)} mm'},
            {'param': 'Dwell time',                'influence':  7, 'detail': f'{dwell_s:.0f} s'},
        ]
        sens_sweep = run_sensitivity_sweep(data) if _sweep else []

        return {
            'ok': True,
            'go_nogo': go_nogo,
            'geometry_type': pattern,
            'toolpath': {
                'pattern':       pattern,
                'dominant_dir':  dominant_dir,
                'scan_aniso':    round(f_trans, 3),
                'z_per_layer':   round(z_per_layer_mm, 3),
                'f_c_effective': round(f_c, 4),
                'cooling_rate':  round(dTdt, 0),
            },
            'summary': {
                'max_sigma_MPa':    round(max_sigma, 1),
                'avg_sigma_MPa':    avg_sigma,
                'yield_MPa':        yield_MPa,
                'utilization_pct':  round(utilization, 1),
                'max_delta_mm':     round(max_delta, 3),
                'max_sag_mm':       round(max(l['delta_sag_mm'] for l in per_layer), 3),
                'sigma_pass_MPa':   round(sigma_pass / 1e6, 1),
                'dT_actual_C':      round(dT_actual, 0),
                'epsilon_in_ue':    round(eps_in * 1e6, 1),
                'relief_pct':       round(relief * 100, 1),
                'high_risk_layers': high_risk,
                'wall_t_mm':        round(wall_t * 1000, 1),
            },
            'per_layer':    per_layer,
            'stress_wps':   stress_wps,
            'risk_zones':   risk_zones,
            'sensitivity':  sensitivity,
            'sens_sweep':   sens_sweep,
        }
    except Exception:
        return None
