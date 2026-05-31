"""
Tier 2 — Integration tests for distortion_engine STL mesh utilities.
compute_distortion was removed; distortion analysis now uses the FEM heat-map engine.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.distortion_engine import parse_stl, _deduplicate
from conftest import make_cube_stl, make_binary_stl


class TestDeduplicate:
    def test_cube_deduplication(self):
        """A cube has 8 unique vertices; raw binary STL has 36 (3 per triangle × 12)."""
        data = make_cube_stl(10.0)
        stl = parse_stl(data)
        unique_verts, new_faces = _deduplicate(stl['vertices'], stl['faces'])
        assert len(unique_verts) == 8, f"Expected 8 unique verts, got {len(unique_verts)}"

    def test_dedup_preserves_topology(self):
        """After dedup, face indices must still refer to valid vertices."""
        data = make_cube_stl(10.0)
        stl = parse_stl(data)
        verts, faces = _deduplicate(stl['vertices'], stl['faces'])
        n = len(verts)
        for face in faces:
            for idx in face:
                assert 0 <= idx < n

    def test_dedup_single_triangle(self):
        data = make_binary_stl([[(0,0,0),(1,0,0),(0,1,0)]])
        stl = parse_stl(data)
        verts, faces = _deduplicate(stl['vertices'], stl['faces'])
        assert len(verts) == 3
        assert len(faces) == 1
