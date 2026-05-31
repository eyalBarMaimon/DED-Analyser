"""
Shared fixtures and helpers for stress/distortion engine tests.
"""
import math
import struct
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Material fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def ss316l():
    return {
        'E_GPa':      193.0,
        'yield_MPa':  310.0,
        'alpha_1e6':   16.0,
        'density':    7900.0,
        'Cp':          490.0,
        'k':            15.0,
        'T_melt':     1400.0,
    }


@pytest.fixture
def ti64():
    return {
        'E_GPa':      114.0,
        'yield_MPa':  880.0,
        'alpha_1e6':    8.6,
        'density':    4430.0,
        'Cp':          560.0,
        'k':             7.0,
        'T_melt':     1660.0,
    }


@pytest.fixture
def inconel625():
    return {
        'E_GPa':      205.0,
        'yield_MPa':  490.0,
        'alpha_1e6':   12.8,
        'density':    8440.0,
        'Cp':          410.0,
        'k':            10.0,
        'T_melt':     1350.0,
    }


# ── Process + geometry fixtures ───────────────────────────────────────────────

@pytest.fixture
def std_process():
    """Standard DED process parameters (1500W, 10mm/s, 50 layers, 5mm wall)."""
    return {
        'laser_power':     1500.0,
        'scan_speed':        10.0,
        'wire_feed_speed':   80.0,
        'layer_height':       0.5,
        'bead_width':         2.0,
        'absorption':         0.35,
        'ambient_temp':       25.0,
        'dwell_time':         10.0,
        'wire_diameter':       1.2,
    }


@pytest.fixture
def std_geometry():
    return {
        'num_layers':      50,
        'wall_thickness':   5.0,
    }


@pytest.fixture
def std_payload_ss316l(ss316l, std_process, std_geometry):
    return {
        'material':  ss316l,
        'process':   std_process,
        'geometry':  std_geometry,
        'waypoints': [],
    }


@pytest.fixture
def std_payload_ti64(ti64, std_process, std_geometry):
    return {
        'material':  ti64,
        'process':   std_process,
        'geometry':  std_geometry,
        'waypoints': [],
    }


# ── Waypoint generators ────────────────────────────────────────────────────────

def make_raster_waypoints(layers=5, passes_per_layer=4, length_mm=40.0,
                          layer_height_mm=0.5, y_step_mm=0.1, steps_per_pass=8):
    """
    Back-and-forth raster in X only (same Y), Z rises per layer.
    Each pass has intermediate points so _analyse_toolpath can detect sign reversals.
    y_step_mm kept small so Y moves don't register as valid scan_dirs.
    """
    wps = []
    z = 0.0
    for lay in range(1, layers + 1):
        z += layer_height_mm
        for p in range(passes_per_layer):
            y = p * y_step_mm
            forward = (p % 2 == 0)
            for s in range(steps_per_pass + 1):
                frac = s / steps_per_pass
                x = (frac * length_mm) if forward else (length_mm - frac * length_mm)
                wps.append({'x': x, 'y': y, 'z': z,
                            'layer': lay, 'layer_num': lay,
                            'is_deposition': True})
    return wps


def make_contour_waypoints(layers=5, radius_mm=20.0,
                           layer_height_mm=0.5, pts_per_layer=32):
    """Circular contour path, Z rising per layer."""
    wps = []
    z = 0.0
    for lay in range(1, layers + 1):
        z += layer_height_mm
        for i in range(pts_per_layer):
            angle = 2 * math.pi * i / pts_per_layer
            wps.append({
                'x': radius_mm * math.cos(angle),
                'y': radius_mm * math.sin(angle),
                'z': z,
                'layer': lay, 'layer_num': lay,
                'is_deposition': True,
            })
    return wps


def make_helix_waypoints(turns=5, radius_mm=20.0,
                         total_height_mm=10.0, pts_per_turn=64):
    """
    True helical path: Z increases continuously, all points in a single layer.
    This satisfies _analyse_toolpath's helix detection criterion:
    avg_inlayer_zspan > 3 * z_per_layer.
    """
    wps = []
    total_pts = turns * pts_per_turn
    for i in range(total_pts):
        angle = 2 * math.pi * i / pts_per_turn
        z = total_height_mm * i / total_pts
        wps.append({
            'x': radius_mm * math.cos(angle),
            'y': radius_mm * math.sin(angle),
            'z': z,
            'layer': 1, 'layer_num': 1,
            'is_deposition': True,
        })
    return wps


# ── STL builder ───────────────────────────────────────────────────────────────

def make_binary_stl(triangles: list) -> bytes:
    """
    Build a minimal binary STL from a list of triangles.
    Each triangle = [(x0,y0,z0), (x1,y1,z1), (x2,y2,z2)].
    """
    header = b'\x00' * 80
    count = struct.pack('<I', len(triangles))
    body = b''
    for tri in triangles:
        # Normal (zeros — many tools accept this)
        body += struct.pack('<fff', 0.0, 0.0, 1.0)
        for v in tri:
            body += struct.pack('<fff', *v)
        body += b'\x00\x00'  # attribute byte count
    return header + count + body


def make_cube_stl(side_mm=10.0) -> bytes:
    """12-triangle binary STL of an axis-aligned cube."""
    s = side_mm
    tris = [
        # +Z face
        [(0,0,s),(s,0,s),(s,s,s)], [(0,0,s),(s,s,s),(0,s,s)],
        # -Z face
        [(0,0,0),(s,s,0),(s,0,0)], [(0,0,0),(0,s,0),(s,s,0)],
        # +X face
        [(s,0,0),(s,s,0),(s,s,s)], [(s,0,0),(s,s,s),(s,0,s)],
        # -X face
        [(0,0,0),(0,0,s),(0,s,s)], [(0,0,0),(0,s,s),(0,s,0)],
        # +Y face
        [(0,s,0),(0,s,s),(s,s,s)], [(0,s,0),(s,s,s),(s,s,0)],
        # -Y face
        [(0,0,0),(s,0,0),(s,0,s)], [(0,0,0),(s,0,s),(0,0,s)],
    ]
    return make_binary_stl(tris)
