"""Full test suite for DED Analyser Heat-map project."""
import sys, io, py_compile, builtins, os, csv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
sys.path.insert(0, '.')

R = []
def chk(name, cond, detail=''):
    R.append((name, bool(cond), detail))

# ══ 1. SYNTAX ══════════════════════════════════════════════
for f in ['app.py','meltio_ded_analyzer.py',
          'engines/stress_engine.py','engines/reduced_fem.py',
          'engines/distortion_engine.py','m600_gcode_parser.py','sensor_analyzer.py']:
    try:
        py_compile.compile(f, doraise=True)
        chk(f'S01 syntax {f}', True)
    except py_compile.PyCompileError as e:
        chk(f'S01 syntax {f}', False, str(e)[:60])

# ══ 2. IMPORTS ═════════════════════════════════════════════
try:
    from meltio_ded_analyzer import MeltioDEDAnalyzer, load_materials_db, fuzzy_match_material
    from engines.stress_engine import compute_stress, lookup_mech, run_sensitivity_sweep
    from engines.reduced_fem import build_grid, run_simulation
    from m600_gcode_parser import M600GcodeAnalyzer
    from sensor_analyzer import SensorAnalyzer
    chk('S02 all imports', True)
except Exception as e:
    chk('S02 all imports', False, str(e)[:60])

# ══ 3. MATERIALS DB ════════════════════════════════════════
try:
    db = load_materials_db()
    chk('S03 db>=10 materials', len(db) >= 10, f'got {len(db)}')
    for mat in ['316L', 'Ti64', 'IN625']:
        m = fuzzy_match_material(mat, db)
        chk(f'S03 fuzzy {mat}', m is not None)
except Exception as e:
    chk('S03 materials_db', False, str(e)[:60])

# ══ 4. PARSING — all ZIPs ══════════════════════════════════
ZIPS = [
    ('RAW Data/VAZA  SST 316L.zip',           'VAZA',    67,  17369),
    ('RAW Data/tower SST316L.zip',            'Tower',  410, 148890),
    ('RAW Data/cylinder hollow.zip Titanium', 'CylH',   168,  26052),
    ('RAW Data/Cylinder solid.zip Titanium',  'CylS',   244, 176024),
    ('RAW Data/Coil230426V1.zip',             'Coil',  1927, 127694),
    ('RAW Data/DOME580mm/DOME580.zip',        'DOME',   968, 314453),
]
for zpath, name, exp_layers, exp_wps in ZIPS:
    try:
        vals = [name, '316L', '316L', '2000', '100', '1.2', '1.2', '1.0', 'no']
        vi = iter(vals)
        builtins.input = lambda p='': next(vi, '0')
        a = MeltioDEDAnalyzer(zpath)
        a.extract_and_read()
        a.parse_rapid_code()
        chk(f'S04 {name} layers={exp_layers}', a.num_layers == exp_layers, f'got {a.num_layers}')
        chk(f'S04 {name} wps={exp_wps}',       len(a.waypoints) == exp_wps,  f'got {len(a.waypoints)}')
        chk(f'S04 {name} seam_start',           any(w.get('is_seam_start') for w in a.waypoints))
        # speeds must be non-zero (unique count can be 1 for constant-speed parts like Ti cylinders)
        speeds_ok = bool(a.all_speeds) and max(a.all_speeds) > 0
        chk(f'S04 {name} speeds>0', speeds_ok, f'max={max(a.all_speeds) if a.all_speeds else 0}')
    except Exception as e:
        chk(f'S04 {name}', False, str(e)[:80])

# ══ 5. THERMAL — VAZA ══════════════════════════════════════
try:
    vals = ['VAZA', '316L', '2000', '100', '0.8', '1.5', '1.0', 'no']
    vi = iter(vals)
    builtins.input = lambda p='': next(vi, '0')
    a = MeltioDEDAnalyzer('RAW Data/VAZA  SST 316L.zip')
    a.extract_and_read()
    a.parse_rapid_code()
    a.get_clarifications()
    a.calculate_thermal_data()
    td = a.thermal_data
    chk('S05 thermal len=17237', len(td) == 17237, f'got {len(td)}')
    req_fields = ['VED','norm_H','cracking_score','lof_risk','keyhole_risk',
                  'overheat_risk','is_seam_start','is_seam_end','seam_gap_mm',
                  'seam_overlap_energy','seam_risk','temp_C_residual']
    for field in req_fields:
        chk(f'S05 field {field}', field in td[0])
    ss = [d for d in td if d.get('is_seam_start')]
    chk('S05 seam_starts=66', len(ss) == 66, f'got {len(ss)}')
    chk('S05 overlap_energy>0', any(d['seam_overlap_energy'] > 0 for d in ss))
    chk('S05 cracking in [0,1]', all(0 <= d['cracking_score'] <= 1 for d in td[:500]))
    chk('S05 overheat is bool',  all(isinstance(d['overheat_risk'], bool) for d in td[:500]))
    chk('S05 lof=0',     sum(1 for d in td if d['lof_risk']) == 0)
    chk('S05 keyhole=0', sum(1 for d in td if d['keyhole_risk']) == 0)
except Exception as e:
    chk('S05 thermal', False, str(e)[:120])

# ══ 6. CSV EXPORT ══════════════════════════════════════════
try:
    from datetime import datetime
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = a.generate_csv(ts)
    with open(csv_path) as fh:
        fields = csv.DictReader(fh).fieldnames
    for rf in ['VED','norm_H','cracking_score','lof_risk','keyhole_risk',
               'overheat_risk','is_seam_start','seam_gap_mm','temp_C_residual']:
        chk(f'S06 CSV {rf}', rf in fields)
    chk('S06 CSV cols>=30', len(fields) >= 30, f'got {len(fields)}')
    os.remove(csv_path)
except Exception as e:
    chk('S06 CSV', False, str(e)[:80])

# ══ 7. HTML STRUCTURE ══════════════════════════════════════
try:
    src = open('ux.html', encoding='utf-8').read()
    lines = src.split('\n')
    main_open  = next(i for i, l in enumerate(lines) if '<main class="main">' in l)
    main_close = next(i for i, l in enumerate(lines) if '</main>' in l)

    depth, neg = 0, 0
    for i in range(main_open, main_close + 1):
        depth += lines[i].count('<div') - lines[i].count('</div>')
        if depth < 0:
            neg += 1
            depth = 0
    chk('S07 no extra </div>', neg == 0, f'{neg} offenses')

    page_lines = [i for i, l in enumerate(lines) if 'id="page-' in l]
    chk('S07 all pages in main', all(main_open < p < main_close for p in page_lines))

    for pid in ['page-report','page-sensors','page-live','page-preprint','page-materials']:
        count = src.count(f'id="{pid}"')
        chk(f'S07 {pid} unique', count == 1, f'count={count}')

    for nav in ['Pre-Print Analysis','Live Dashboard','Post-Print Review',
                'Offline Replay','Print History','Materials DB']:
        chk(f'S07 nav {nav[:12]}', nav in src)

    for sec in ['sec-geometry-collapse','sec-sensitivity-collapse',
                'sec-distortion-collapse','sec-stress-collapse']:
        chk(f'S07 {sec[:22]}', sec in src)

    chk('S07 femShowSeam',          'id="femShowSeam"' in src)
    chk('S07 seamVisToggle',         'seamVisToggle' in src)
    chk('S07 toggleSeamVisibility', 'function toggleSeamVisibility' in src)
    chk('S07 _showSeam guard',       '_showSeam' in src)

    for fn in ['runFemSimulation','femApplyFilters','geomCenter','loadSensitivityView',
               '_femRenderPlotly','renderMaterialsDB','stressRun','showPage','renderResults']:
        chk(f'S07 fn {fn}', f'function {fn}' in src or f'async function {fn}' in src)

    app_src = open('app.py', encoding='utf-8').read()
    for route in ['/api/fem/simulate', '/api/jobs/<jid>/geometry',
                  '/api/sensitivity/<job_id>', '/api/sensitivity', '/api/materials']:
        chk(f'S07 route {route}', route in app_src)

except Exception as e:
    chk('S07 HTML', False, str(e)[:80])

# ══ RESULTS ════════════════════════════════════════════════
passed = sum(1 for r in R if r[1])
total  = len(R)
failed = [r for r in R if not r[1]]
print(f'\n{"="*55}')
print(f'RESULTS: {passed}/{total} passed', '✓' if not failed else '✗')
print('='*55)
for r in R:
    tag  = 'OK  ' if r[1] else 'FAIL'
    note = f'  [{r[2]}]' if len(r) > 2 and r[2] else ''
    print(f'  {tag} {r[0]}{note}')
if not failed:
    print('\nALL PASSED ✓')
else:
    print(f'\n{len(failed)} FAILURES:')
    for r in failed:
        print(f'  ✗ {r[0]}' + (f' [{r[2]}]' if len(r) > 2 else ''))
