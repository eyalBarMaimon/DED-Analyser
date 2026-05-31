"""
Tier 1 — Unit tests for STL parsing (binary and ASCII).
"""
import struct
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.distortion_engine import parse_stl
from conftest import make_binary_stl, make_cube_stl


class TestBinarySTL:
    def test_cube_parses(self):
        data = make_cube_stl(10.0)
        stl = parse_stl(data)
        assert stl is not None
        assert len(stl['vertices']) == 36   # 12 triangles × 3 verts
        assert len(stl['faces']) == 12

    def test_single_triangle(self):
        data = make_binary_stl([[(0,0,0), (1,0,0), (0,1,0)]])
        stl = parse_stl(data)
        assert stl is not None
        assert len(stl['faces']) == 1
        assert len(stl['vertices']) == 3

    def test_vertex_coordinates(self):
        """Verify vertex coordinates are read without corruption."""
        tris = [[(1.5, 2.5, 3.5), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)]]
        data = make_binary_stl(tris)
        stl = parse_stl(data)
        v0 = stl['vertices'][0]
        assert abs(v0[0] - 1.5) < 1e-5
        assert abs(v0[1] - 2.5) < 1e-5
        assert abs(v0[2] - 3.5) < 1e-5

    def test_empty_data_returns_none(self):
        assert parse_stl(b'') is None

    def test_truncated_data_handled(self):
        """Truncated binary STL should not crash."""
        data = make_cube_stl()[:50]   # truncated mid-header
        result = parse_stl(data)
        # May return None or partial result — just must not raise
        assert result is None or isinstance(result, dict)

    def test_face_indices_valid(self):
        """All face indices must be valid vertex indices."""
        data = make_cube_stl(20.0)
        stl = parse_stl(data)
        n_verts = len(stl['vertices'])
        for face in stl['faces']:
            for idx in face:
                assert 0 <= idx < n_verts


class TestAsciiSTL:
    def _make_ascii(self, triangles):
        lines = ['solid test']
        for tri in triangles:
            lines.append('  facet normal 0 0 1')
            lines.append('    outer loop')
            for v in tri:
                lines.append(f'      vertex {v[0]} {v[1]} {v[2]}')
            lines.append('    endloop')
            lines.append('  endfacet')
        lines.append('endsolid test')
        return '\n'.join(lines).encode('utf-8')

    def test_ascii_single_triangle(self):
        data = self._make_ascii([[(0,0,0),(1,0,0),(0,1,0)]])
        stl = parse_stl(data)
        assert stl is not None
        assert len(stl['faces']) == 1

    def test_ascii_vertex_coordinates(self):
        data = self._make_ascii([[(1.1, 2.2, 3.3), (4.4, 5.5, 6.6), (7.7, 8.8, 9.9)]])
        stl = parse_stl(data)
        v = stl['vertices'][0]
        assert abs(v[0] - 1.1) < 1e-4
        assert abs(v[2] - 3.3) < 1e-4

    def test_ascii_multiple_triangles(self):
        tris = [[(i,0,0),(i+1,0,0),(i,1,0)] for i in range(5)]
        data = self._make_ascii(tris)
        stl = parse_stl(data)
        assert len(stl['faces']) == 5
