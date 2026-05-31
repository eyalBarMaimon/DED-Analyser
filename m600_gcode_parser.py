#!/usr/bin/env python3
"""
Meltio M600 G-code Analyzer
Parses REALvisionCore WelderServo G-code from Meltio M600 XYZ printer.
Compatible with MeltioDEDAnalyzer thermal pipeline.
"""

import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional
import sys

sys.path.insert(0, str(Path(__file__).parent))
from meltio_ded_analyzer import MeltioDEDAnalyzer, load_materials_db


class M600GcodeAnalyzer(MeltioDEDAnalyzer):
    """
    Meltio M600 XYZ printer analyzer.
    Parses G-code (REALvisionCore WelderServo dialect) instead of RAPID .mod files.
    Inherits thermal simulation, visualization, and reporting from MeltioDEDAnalyzer.
    """

    def __init__(self, gcode_path: str):
        super().__init__(gcode_path)   # stores path; we override extract_and_read
        self.gcode_path    = gcode_path
        self.gcode_content = ""
        self.header_meta: Dict = {}    # parsed from file header comments

    # ─── STEP 1: READ G-CODE ────────────────────────────────────────────────

    def extract_and_read(self) -> bool:
        """Read a bare .gcode file, or extract the first .gcode from a .zip."""
        try:
            p = Path(self.gcode_path)
            if self.gcode_path.lower().endswith('.zip'):
                with zipfile.ZipFile(self.gcode_path, 'r') as zf:
                    gcode_names = [f for f in zf.namelist()
                                   if f.lower().endswith('.gcode')]
                    if not gcode_names:
                        print("❌ No .gcode file found in ZIP")
                        return False
                    self.gcode_content = zf.read(gcode_names[0]).decode('utf-8', errors='ignore')
                    self.part_name = Path(gcode_names[0]).stem
            else:
                self.gcode_content = p.read_text(encoding='utf-8', errors='ignore')
                self.part_name = p.stem

            n_lines = len(self.gcode_content.splitlines())
            print(f"✅ Read G-code: {n_lines:,} lines from '{self.part_name}'")
            return True
        except Exception as e:
            print(f"❌ Error reading G-code: {e}")
            return False

    # ─── STEP 2: PARSE G-CODE (replaces parse_rapid_code) ──────────────────

    def parse_rapid_code(self):
        """Entry point called by the analysis pipeline — delegates to _parse_gcode."""
        self._parse_gcode()

    def _parse_gcode(self):
        """
        Parse REALvisionCore WelderServo G-code.

        Key patterns:
          ; Material Name T0: Meltio 316LSi
          ; Material Diameter T0: 0.98
          ; Layer height: 0.6
          ; Print Time Estimate: 18:27:07
          ; New Layer
          ; layer 0, Z = 0
          T0 / T1               — tool (material) select
          G108 P W1000 E        — laser ON at 1000 W
          G108 W0               — laser OFF
          G123 E I              — deposition process ON
          G123 D                — deposition process OFF
          G1 X82 Y157.6 Z0.6 F750  — movement (F in mm/min)
        """
        lines = self.gcode_content.splitlines()

        # ── Header scan (first 80 lines) ──────────────────────────────────
        for line in lines[:80]:
            line = line.strip()
            m = re.match(r';\s*Material Name T0:\s*(.+)', line, re.I)
            if m:
                self.header_meta['material_T0'] = m.group(1).strip()
            m = re.match(r';\s*Material Name T1:\s*(.+)', line, re.I)
            if m:
                self.header_meta['material_T1'] = m.group(1).strip()
            m = re.match(r';\s*Material Diameter T0:\s*([\d.]+)', line, re.I)
            if m:
                self.header_meta['wire_diameter'] = float(m.group(1))
            m = re.match(r';\s*Layer height:\s*([\d.]+)', line, re.I)
            if m:
                self.header_meta['layer_height'] = float(m.group(1))
            m = re.match(r';\s*Line Width:\s*([\d.]+)', line, re.I)
            if m:
                self.header_meta['layer_width'] = float(m.group(1))
            m = re.match(r';\s*Print Time Estimate:\s*(.+)', line, re.I)
            if m:
                self.header_meta['print_time_estimate'] = m.group(1).strip()
            m = re.match(r';\s*Laser power:\s*([\d.]+)', line, re.I)
            if m:
                self.header_meta['laser_power'] = float(m.group(1))

        # ── State machine ──────────────────────────────────────────────────
        cur_x, cur_y, cur_z = 0.0, 0.0, 0.0
        cur_f        = 750.0      # mm/min
        cur_layer    = 0
        cur_material = "T0"
        laser_on     = False
        deposition_on = False
        laser_watts  = 0

        # Track layer numbers seen to build material-change events
        seen_layers = set()

        for line in lines:
            line = line.strip()
            if not line or line.startswith(';;'):
                continue

            # ── Layer marker ────────────────────────────────
            if line == '; New Layer':
                continue   # layer number comes on the very next comment line
            m = re.match(r';\s*layer\s+(\d+)\s*,\s*Z\s*=\s*([\d.]+)', line, re.I)
            if m:
                cur_layer = int(m.group(1)) + 1    # 0-indexed in file → 1-indexed
                continue

            # ── Tool / material selection ────────────────────
            if line in ('T0', 'T1'):
                cur_material = line
                self.materials_found.add(cur_material)
                continue

            # ── Laser control:  G108 P W{watts} E  |  G108 W0 ──────────────
            if line.upper().startswith('G108'):
                w_m = re.search(r'W(\d+)', line, re.I)
                if w_m:
                    laser_watts = int(w_m.group(1))
                    laser_on = laser_watts > 0
                continue

            # ── Deposition: G123 E I = on,  G123 D = off ────────────────────
            if line.upper().startswith('G123'):
                if re.search(r'\bD\b', line, re.I):
                    deposition_on = False
                elif re.search(r'\bE\b', line, re.I):
                    deposition_on = True
                continue

            # ── Movement: G0/G1 ─────────────────────────────────────────────
            if re.match(r'G[01]\b', line, re.I):
                xm = re.search(r'X([-\d.]+)', line, re.I)
                ym = re.search(r'Y([-\d.]+)', line, re.I)
                zm = re.search(r'Z([-\d.]+)', line, re.I)
                fm = re.search(r'F([\d.]+)',  line, re.I)

                if fm:
                    cur_f = float(fm.group(1))

                new_x = float(xm.group(1)) if xm else cur_x
                new_y = float(ym.group(1)) if ym else cur_y
                new_z = float(zm.group(1)) if zm else cur_z

                if (new_x, new_y, new_z) == (cur_x, cur_y, cur_z):
                    continue     # no movement — skip

                speed_mm_s = cur_f / 60.0   # mm/min → mm/s
                is_dep     = deposition_on and laser_on

                layer = max(1, cur_layer)
                self.waypoints.append({
                    'layer_num':     layer,
                    'x':             round(new_x, 4),
                    'y':             round(new_y, 4),
                    'z':             round(new_z, 4),
                    'speed':         round(speed_mm_s, 3),
                    'material':      cur_material,
                    'is_deposition': is_dep,
                })
                self.all_speeds.append(speed_mm_s)
                cur_x, cur_y, cur_z = new_x, new_y, new_z

        # ── Finalize ──────────────────────────────────────────────────────
        if not self.materials_found:
            self.materials_found.add('T0')

        # Detect auto laser power from most common G108 watts value
        if 'laser_power' not in self.header_meta:
            # scan lines for G108 Wxxxx to find max watts seen
            all_watts = [int(m.group(1))
                         for m in (re.search(r'G108.*W(\d+)', l, re.I) for l in lines)
                         if m and int(m.group(1)) > 0]
            if all_watts:
                self.header_meta['laser_power'] = max(set(all_watts), key=all_watts.count)

        self.num_layers = max(
            (wp['layer_num'] for wp in self.waypoints), default=0
        )

        # Compute average deposition speed and store in header_meta
        if 'feed_speed' not in self.header_meta:
            dep_speeds = [wp['speed'] for wp in self.waypoints if wp['is_deposition'] and wp['speed'] > 0]
            if dep_speeds:
                self.header_meta['feed_speed'] = round(sum(dep_speeds) / len(dep_speeds), 2)

        dep_count = sum(1 for wp in self.waypoints if wp['is_deposition'])
        print(f"   ✅ {len(self.materials_found)} material(s): {', '.join(sorted(self.materials_found))}")
        print(f"   ✅ {len(self.waypoints):,} waypoints, {dep_count:,} deposition moves")
        print(f"   ✅ {self.num_layers} layers detected")
