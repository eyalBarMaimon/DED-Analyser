"""
STL mesh utilities — parsing, deduplication, and subsampling.
Used by overlay_html to render a ghost mesh aligned to the toolpath.
Distortion analysis has been replaced by the FEM heat-map engine (reduced_fem.py).
"""
import struct
import math


# ── STL parsing ───────────────────────────────────────────────────────────────

def parse_stl(data: bytes) -> dict | None:
    try:
        if len(data) < 6:
            return None
        if data[:5] == b'solid' and not _looks_binary(data):
            return _parse_ascii(data.decode('utf-8', errors='ignore'))
        return _parse_binary(data)
    except Exception:
        return None


def _looks_binary(data: bytes) -> bool:
    if len(data) < 84:
        return False
    count = struct.unpack_from('<I', data, 80)[0]
    return len(data) == 84 + count * 50


def _parse_binary(data: bytes) -> dict:
    count = struct.unpack_from('<I', data, 80)[0]
    count = min(count, (len(data) - 84) // 50)
    verts, faces = [], []
    vi, offset = 0, 84
    for _ in range(count):
        offset += 12
        tri = []
        for _ in range(3):
            x, y, z = struct.unpack_from('<fff', data, offset)
            verts.append([x, y, z])
            tri.append(vi)
            vi += 1
            offset += 12
        faces.append(tri)
        offset += 2
    return {'vertices': verts, 'faces': faces}


def _parse_ascii(text: str) -> dict:
    verts, faces = [], []
    vi, tri = 0, []
    for line in text.split('\n'):
        line = line.strip()
        if line.startswith('vertex'):
            parts = line.split()
            if len(parts) >= 4:
                verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
                tri.append(vi)
                vi += 1
                if len(tri) == 3:
                    faces.append(tri)
                    tri = []
    return {'vertices': verts, 'faces': faces}


# ── Mesh utilities ─────────────────────────────────────────────────────────────

def _deduplicate(verts: list, faces: list, tol: float = 0.01) -> tuple:
    if len(verts) > 60000:
        return verts, faces
    index_map, unique_verts, remap = {}, [], {}
    for i, v in enumerate(verts):
        key = (round(v[0] / tol), round(v[1] / tol), round(v[2] / tol))
        if key not in index_map:
            index_map[key] = len(unique_verts)
            unique_verts.append(v)
        remap[i] = index_map[key]
    new_faces = [[remap[f[0]], remap[f[1]], remap[f[2]]] for f in faces]
    return unique_verts, new_faces


def _subsample(verts: list, faces: list, max_faces: int = 4000) -> tuple:
    if len(faces) <= max_faces:
        return verts, faces
    step = math.ceil(len(faces) / max_faces)
    sampled = faces[::step]
    used = sorted(set(i for f in sampled for i in f))
    remap = {old: new for new, old in enumerate(used)}
    new_verts = [verts[i] for i in used]
    new_faces = [[remap[f[0]], remap[f[1]], remap[f[2]]] for f in sampled]
    return new_verts, new_faces
