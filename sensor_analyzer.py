#!/usr/bin/env python3
"""Meltio DED — Live Sensor Data Analyzer"""

import csv, io, math, threading
from pathlib import Path
from datetime import datetime


class SensorAnalyzer:
    GROUPS = {
        'process':      ['laserPower', 'feedSpeed'],
        'loadcell':     ['loadcell'],
        'temperatures': [f'temp{i}' for i in range(1, 10)],
        'currents':     [f'current{i}' for i in range(1, 10)],
        'machine':      ['argon', 'coolantFlow', 'mainCirculatingPressure'],
    }
    FLAGS = [
        ('startDeposition', 'rgba(34,197,94,0.9)',  'Deposition Start'),
        ('endDeposition',   'rgba(239,68,68,0.9)',  'Deposition End'),
        ('changeToT0',      'rgba(251,191,36,0.9)', 'Change → T0'),
        ('changeToT1',      'rgba(251,191,36,0.9)', 'Change → T1'),
    ]

    def __init__(self):
        self.path = None
        self.offset = 0
        self.header = None
        self.rows = []
        self.events = []
        self._lock = threading.Lock()

    def connect(self, path: str) -> int:
        self.path = str(path)
        rows = []
        with open(self.path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            self.header = [h for h in (reader.fieldnames or []) if h]
            for row in reader:
                p = self._parse_row(row)
                if p:
                    rows.append(p)
            self.offset = f.tell()
        with self._lock:
            self.rows = rows
            self.events = self._find_events(self.rows)
        return len(self.rows)

    def poll(self) -> list:
        if not self.path:
            return []
        new_rows = []
        try:
            with open(self.path, newline='', encoding='utf-8-sig') as f:
                f.seek(self.offset)
                new_content = f.read()
                self.offset = f.tell()
            if new_content.strip():
                full = ','.join(self.header) + '\n' + new_content
                reader = csv.DictReader(io.StringIO(full))
                for row in reader:
                    p = self._parse_row(row)
                    if p:
                        new_rows.append(p)
            with self._lock:
                prev = self.rows[-1] if self.rows else {}
                self.rows.extend(new_rows)
                new_events = self._find_events(new_rows, prev)
                self.events.extend(new_events)
        except Exception:
            pass
        return new_rows

    def _parse_row(self, row: dict):
        t_str = (row.get('tiempo') or '').strip()
        if not t_str:
            return None
        result = {'tiempo': t_str}
        for k, v in row.items():
            if k and k != 'tiempo' and v is not None:
                try:
                    result[k] = float(str(v).strip())
                except (ValueError, AttributeError):
                    result[k] = str(v).strip() if v else None
        return result

    def _find_events(self, rows: list, prev_row: dict = None) -> list:
        events = []
        prev = prev_row or {}
        for row in rows:
            for flag, color, label in self.FLAGS:
                pv = float(prev.get(flag, 0) or 0)
                cv = float(row.get(flag, 0) or 0)
                if pv == 0 and cv == 1:
                    events.append({'time': row['tiempo'], 'color': color, 'label': label, 'flag': flag})
            prev = row
        return events

    def stats(self) -> dict:
        with self._lock:
            rows = self.rows[:]
        result = {}
        for group, cols in self.GROUPS.items():
            result[group] = {}
            for col in cols:
                vals = [r[col] for r in rows if isinstance(r.get(col), float) and not math.isnan(r[col])]
                if vals:
                    result[group][col] = {'min': round(min(vals), 2), 'max': round(max(vals), 2), 'avg': round(sum(vals)/len(vals), 2)}
        return result

    def downsample(self, n: int = 2000) -> list:
        with self._lock:
            rows = self.rows[:]
        if len(rows) <= n:
            return rows
        step = max(1, len(rows) // n)
        return rows[::step]

    def chart_data(self, rows: list = None) -> dict:
        if rows is None:
            rows = self.downsample()
        times = [r['tiempo'] for r in rows]
        groups = {}
        for group, cols in self.GROUPS.items():
            groups[group] = {col: [r.get(col) for r in rows] for col in cols}
        return {'times': times, 'groups': groups}

    def summary(self) -> dict:
        with self._lock:
            rows = self.rows[:]
            events = self.events[:]
        if not rows:
            return {}
        t0, t1 = rows[0]['tiempo'], rows[-1]['tiempo']
        try:
            fmt = '%Y-%m-%d %H:%M:%S.%f'
            dt = datetime.strptime(t1, fmt) - datetime.strptime(t0, fmt)
            dur_secs = dt.total_seconds()
            duration = str(dt).split('.')[0]
        except Exception:
            dur_secs = 1
            duration = '—'
        return {
            'total_rows': len(rows),
            'start_time': t0,
            'end_time': t1,
            'duration': duration,
            'event_count': len(events),
            'sample_rate_hz': round(len(rows) / max(1, dur_secs), 1),
        }
