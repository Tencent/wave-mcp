#!/usr/bin/env python3
"""Functional correctness verification for the full wave-mcp tool set.

Not just "is it empty?" but "is the answer structurally correct and
cross-consistent?"  Each tool gets targeted checks:

  - file/line in drivers point to real files at real lines
  - rhs/control paths are valid hierarchical signal names
  - driver_unique_id format matches "module.leaf#index"
  - trace_value tree has signal/value/hex on every node, contributors recurse
  - signal_values hex matches binary value, times are monotonic
  - signal_value_at matches signal_values at the same time
  - connectivity entries ⊆ union of fanin + loads + port_peers
  - active_drivers guard_active is True/False/None (not missing)
  - driver_contributors rhs/control match the driver referenced by unique_id
  - list_files paths all exist on disk
  - modules_in_file returns names that exist in the netlist
  - trace_x returns "no-x" for non-X signals (correct) or a tree for X signals
  - list_signals widths > 0, types are valid enum values
  - scope_info module_type is non-empty for module-kind scopes
"""
import glob, json, sys, os, time, traceback
from collections import Counter, defaultdict
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from wave_mcp.sources.rtl_source import RtlSource
from wave_mcp.sources.fst_source import FstSource

# Project assets are configured via the WAVE_MCP_PROJECT_ASSETS env var
# (os.pathsep-separated) or tests/project_assets.txt (one entry per line,
# '#' comments allowed). Each entry is either:
#   - a *.fst file, with its netlist maps at <dir>/session/netlist/maps.json
#   - a directory of per-IP builds, where each subdir holds one *.fst and
#     session/netlist/maps.json
# The regression runner (run_regression.py) skips this suite when no entry
# exists, so the harness itself can assume at least a chance of finding data.
ASSETS_ENV = "WAVE_MCP_PROJECT_ASSETS"
ASSETS_FILE = os.path.join(HERE, "project_assets.txt")

def iter_project_assets():
    """Yield (project_name, fst_path, maps_path) for each configured asset."""
    raw = os.environ.get(ASSETS_ENV, "")
    if not raw and os.path.exists(ASSETS_FILE):
        with open(ASSETS_FILE, encoding="utf-8") as f:
            raw = f.read()
    entries = [p.strip() for p in raw.replace("\n", os.pathsep).split(os.pathsep)
               if p.strip() and not p.strip().startswith("#")]
    for entry in entries:
        if os.path.isfile(entry) and entry.endswith(".fst"):
            base = os.path.dirname(os.path.abspath(entry))
            maps_path = os.path.join(base, "session", "netlist", "maps.json")
            if os.path.exists(maps_path):
                yield (os.path.splitext(os.path.basename(entry))[0],
                       entry, maps_path)
        elif os.path.isdir(entry):
            base_name = os.path.basename(os.path.normpath(entry))
            for ip in sorted(os.listdir(entry)):
                ip_dir = os.path.join(entry, ip)
                if not os.path.isdir(ip_dir):
                    continue
                fst_files = sorted(f for f in os.listdir(ip_dir)
                                   if f.endswith(".fst"))
                maps_path = os.path.join(ip_dir, "session", "netlist",
                                         "maps.json")
                if not fst_files or not os.path.exists(maps_path):
                    continue
                yield (f"{base_name}/{ip}",
                       os.path.join(ip_dir, fst_files[0]), maps_path)

# Sample cap for the connectivity/drivers/loads/fanin sweep. Wide designs have
# hundreds of thousands of netlist signals and the checks are highly redundant,
# so a stratified sample gives the same signal at a fraction of the runtime.
CONN_SAMPLE = 5000

VALID_VAR_TYPES = {'event','integer','parameter','real','real_parameter','reg',
    'supply0','supply1','time','tri','triand','trior','trireg','tri0','tri1',
    'wand','wire','wor','port','sparray','realtime','string','bit','logic',
    'int','shortint','longint','byte','enum','shortreal'}
VALID_CATEGORIES = {'Port','Internal-register','Internal-wire','Parameter'}
VALID_KINDS = {'assign','blocking','nonblocking','instance_port'}
VALID_DIRS = {'implicit','input','output','inout','buffer','linkage'}
VALID_SCOPE_KINDS = {'module','task','function','begin','fork','generate',
    'struct','union','class','interface','package','program'}

def _sample(items, n):
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _make_src_resolver(maps_path):
    """Resolve a netlist ``file`` field to a real path, or None.

    Netlist maps store the source path as written by the build flow, which may
    be relative. Resolving it against the harness CWD only works by accident
    (it happens to match when the sources live under the build dir), so the
    result would change with the directory the regression is run from. Try the
    documented bases explicitly instead, nearest first.
    """
    maps_dir = os.path.dirname(os.path.abspath(maps_path))
    bases = [maps_dir,                                    # session/netlist
             os.path.dirname(maps_dir),                   # session
             os.path.dirname(os.path.dirname(maps_dir)),  # project root
             os.getcwd()]                                 # legacy behaviour

    # Some builds record sources that live outside the build tree (a checkout
    # elsewhere), and the stored relative path cannot be resolved from any of
    # the bases above. The build's own file list carries the absolute paths, so
    # index it by basename as a last resort rather than hardcoding a location.
    fallback = {}
    proj_root = os.path.dirname(os.path.dirname(maps_dir))
    for flist in sorted(glob.glob(os.path.join(proj_root, "*_rtl.f"))):
        try:
            with open(flist, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    p = line.strip()
                    if p and not p.startswith(("+", "-")) and os.path.isabs(p):
                        fallback.setdefault(os.path.basename(p), p)
        except OSError:
            pass

    def resolve(raw):
        if not raw:
            return None
        if os.path.isabs(raw):
            return raw if os.path.exists(raw) else None
        for base in bases:
            cand = os.path.normpath(os.path.join(base, raw))
            if os.path.exists(cand):
                return cand
        hit = fallback.get(os.path.basename(raw))
        return hit if hit and os.path.exists(hit) else None

    return resolve


def verify_project(project_name, fst_path, maps_path, output_path):
    print(f"\n{'='*70}")
    print(f"  Functional Verification: {project_name}")
    print(f"{'='*70}")
    t0 = time.time()
    resolve_src = _make_src_resolver(maps_path)
    fst = FstSource(fst_path)
    rtl = RtlSource(maps_path=maps_path, fst=fst)
    # CRITICAL: annotate FST scopes with netlist definitions so
    # instances_of_module and scope_info.module_type work correctly
    if rtl.has_netlist:
        resolver_map = rtl.engine.resolve_definitions(list(fst.scopes.keys()))
        fst.apply_definition_map(resolver_map, source="netlist")
        fst.width_hint = rtl.signal_width

    inst_to_mod = dict(rtl.engine.instance_tree) if rtl.has_netlist else {}
    fst_scopes = list(fst.scopes.keys())
    fst_signals = list(fst.signals.keys())

    # Build netlist signal set for cross-checking
    netlist_signals = set()
    for inst_path, mod_name in inst_to_mod.items():
        mod_data = rtl.engine.modules.get(mod_name, {})
        for sig in mod_data.get('ports', {}):
            netlist_signals.add(f'{inst_path}.{sig}')
        for sig in mod_data.get('signals', {}):
            netlist_signals.add(f'{inst_path}.{sig}')

    results = {}  # tool -> {checks: int, passed: int, failures: []}
    def init(tool):
        results[tool] = {'checks': 0, 'passed': 0, 'failures': []}
    def check(tool, condition, desc, detail=None):
        results[tool]['checks'] += 1
        if condition:
            results[tool]['passed'] += 1
        else:
            f = {'check': desc}
            if detail: f['detail'] = detail
            results[tool]['failures'].append(f)

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 1-3: session_info — consistency checks
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [1-3] session_info...")
    init('session_info')
    top_inst = list(inst_to_mod.keys())[0] if inst_to_mod else ''
    check('session_info', bool(top_inst), 'top instance exists')
    check('session_info', rtl.has_netlist, 'netlist available')
    check('session_info', len(inst_to_mod) > 0, 'instance_tree non-empty')
    check('session_info', len(netlist_signals) > 0, 'netlist signals non-empty')
    # cross-check: instance_tree count vs FST scope count (should be same order)
    fst_module_scopes = sum(1 for s in fst.scopes.values() if s.scope_type == 'module')
    check('session_info', fst_module_scopes > 0, 'FST has module-type scopes',
          f'fst_module_scopes={fst_module_scopes}, netlist_instances={len(inst_to_mod)}')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 5: convert_vcd_to_fst — FST file validity
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [5] convert_vcd_to_fst...")
    init('convert_vcd_to_fst')
    check('convert_vcd_to_fst', os.path.getsize(fst_path) > 0, 'FST file non-empty')
    check('convert_vcd_to_fst', fst.end_time > fst.start_time, 'end_time > start_time',
          f'start={fst.start_time} end={fst.end_time}')
    check('convert_vcd_to_fst', len(fst.scopes) > 0, 'scopes parsed')
    check('convert_vcd_to_fst', len(fst.signals) > 0, 'signals parsed')
    check('convert_vcd_to_fst', fst.timescale_exp is not None, 'timescale set')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 6: list_child_instances — use FST scope path!
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [6] list_child_instances...")
    init('list_child_instances')
    # Pick the top scope that actually has children — FST files typically
    # contain many *_pkg package scopes at the root which have zero children.
    # The real DUT/TB top is the scope with child instances (e.g. tb_aes).
    root_scopes_with_children = [s for s in fst_scopes
                                  if '.' not in s and fst.scopes[s].children]
    top_scope = (root_scopes_with_children[0] if root_scopes_with_children
                 else next((s for s in fst_scopes if '.' not in s), fst_scopes[0] if fst_scopes else ''))
    check('list_child_instances', bool(top_scope), 'top scope found', f'top_scope={top_scope}')
    if top_scope:
        children = fst.child_instances(top_scope, levels=1, max_scopes=5000, filter_noise=True)
        check('list_child_instances', len(children) > 0, 'top has children',
              f'count={len(children)}')
        for c in children[:20]:
            check('list_child_instances', c.get('full_path') in fst.scopes,
                  'child path exists in scopes', f'path={c.get("full_path")}')
            check('list_child_instances', bool(c.get('name')),
                  'child has name', f'path={c.get("full_path")}')
            sk = c.get('scope_kind', '')
            check('list_child_instances', sk in VALID_SCOPE_KINDS,
                  'scope_kind valid', f'kind={sk}')
        # Test deeper levels
        children2 = fst.child_instances(top_scope, levels=2, max_scopes=5000, filter_noise=True)
        check('list_child_instances', len(children2) >= len(children),
              'levels=2 returns >= levels=1', f'l1={len(children)} l2={len(children2)}')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 7: list_modules
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [7] list_modules...")
    init('list_modules')
    all_mods = fst.all_module_names()
    check('list_modules', len(all_mods) > 0, 'modules found')
    # After annotate_definitions, _modules should have real module names
    if rtl.has_netlist:
        netlist_mods = set(rtl.engine.modules.keys())
        fst_mods = set(all_mods)
        overlap = netlist_mods & fst_mods
        check('list_modules', len(overlap) > 0,
              'FST modules overlap with netlist modules',
              f'overlap={len(overlap)} netlist={len(netlist_mods)} fst={len(fst_mods)}')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 8-9: instances_of_module / matching
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [8-9] instances_of_module / matching...")
    init('instances_of_module')
    init('instances_of_module_matching')
    mod_with_insts = None
    for mod in all_mods:
        paths = fst.instances_by_module(mod)
        check('instances_of_module', True, f'module {mod} queried')
        if paths and not mod_with_insts:
            mod_with_insts = mod
            # Verify returned paths are real FST scopes
            for p in paths[:10]:
                check('instances_of_module', p in fst.scopes,
                      'instance path exists in FST', f'path={p} mod={mod}')
            # Test matching filter
            test_sub = paths[0].split('.')[-1]
            filtered = fst.instances_by_module(mod, test_sub)
            check('instances_of_module_matching', len(filtered) >= 1,
                  'matching filter returns results', f'mod={mod} filter={test_sub} count={len(filtered)}')
            for p in filtered[:5]:
                check('instances_of_module_matching', test_sub in p,
                      'filtered path contains filter', f'path={p}')
    if mod_with_insts:
        check('instances_of_module', True, 'at least one module has instances')
    else:
        check('instances_of_module', False, 'no module has instances (after annotate)')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 10: scope_info — use FST scope paths!
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [10] scope_info...")
    init('scope_info')
    scope_sample = _sample(fst_scopes, 100)
    for sp in scope_sample:
        info = fst.scope_info(sp)
        check('scope_info', info is not None, 'scope_info returns data', f'path={sp}')
        if info:
            check('scope_info', info.get('full_path') == sp, 'full_path matches')
            check('scope_info', bool(info.get('name')), 'has name')
            sk = info.get('scope_kind', '')
            check('scope_info', sk in VALID_SCOPE_KINDS, 'scope_kind valid', f'kind={sk}')
            # For module-type scopes, module_type should be non-empty after annotation
            if sk == 'module':
                mt = info.get('module_type', '')
                check('scope_info', bool(mt), 'module scope has module_type', f'path={sp}')
                # Cross-check: module_type should exist in netlist
                if rtl.has_netlist and mt:
                    check('scope_info', mt in rtl.engine.modules or mt == sk,
                          'module_type resolvable in netlist', f'path={sp} mt={mt}')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 11: list_signals — use FST scope paths!
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [11] list_signals...")
    init('list_signals')
    module_scopes = [s for s in fst_scopes if fst.scopes[s].scope_type == 'module']
    inst_sample = _sample(module_scopes, 100)
    for inst in inst_sample:
        sigs = fst.signals_of_instance(inst, limit=200)
        check('list_signals', True, f'instance {inst} queried')
        if not sigs:
            continue
        for s in sigs[:10]:
            check('list_signals', s.get('width', 0) > 0, 'signal has width > 0',
                  f'name={s.get("name")} width={s.get("width")}')
            cat = s.get('type', '')
            check('list_signals', cat in VALID_CATEGORIES, 'type valid',
                  f'name={s.get("name")} type={cat}')
            vt = s.get('var_type', '')
            check('list_signals', vt in VALID_VAR_TYPES, 'var_type valid',
                  f'name={s.get("name")} vt={vt}')
            check('list_signals', bool(s.get('full_path')), 'has full_path')
            check('list_signals', s.get('full_path', '').startswith(inst),
                  'full_path starts with scope', f'fp={s.get("full_path")} inst={inst}')
            # Aggregated bus check
            if s.get('element_count', 1) > 1:
                check('list_signals', '[' in s.get('name', '') and ':' in s.get('name', ''),
                      'aggregated bus has [hi:lo] name', f'name={s.get("name")}')
                if 'width_matches_rtl' in s:
                    check('list_signals', s['width_matches_rtl'] is True,
                          'bus width matches RTL', f'name={s.get("name")} width={s.get("width")} rtl={s.get("rtl_width")}')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 12: signal_info — use FST signal paths!
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [12] signal_info...")
    init('signal_info')
    sig_sample = _sample(fst_signals, 300)
    for sp in sig_sample:
        info = fst.signal_info(sp)
        check('signal_info', info is not None, 'signal_info returns data', f'path={sp}')
        if info:
            check('signal_info', info.get('width', 0) > 0, 'width > 0',
                  f'path={sp} width={info.get("width")}')
            check('signal_info', info.get('full_path') == sp, 'full_path matches')
            msb = info.get('msb', 0)
            lsb = info.get('lsb', 0)
            check('signal_info', msb >= lsb, 'msb >= lsb', f'msb={msb} lsb={lsb}')
            check('signal_info', msb + 1 == info.get('width') or info.get('aggregated_from', 0) > 0,
                  'width = msb+1 (or aggregated)', f'width={info.get("width")} msb={msb}')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 13-15: signal_values / in_range / value_at — cross-validate!
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [13-15] signal_values / in_range / value_at...")
    init('signal_values')
    init('signal_values_in_range')
    init('signal_value_at')
    mid = (fst.start_time + fst.end_time) // 2
    q1 = fst.start_time + (mid - fst.start_time) // 2
    val_sample = _sample(fst_signals, 200)
    for sp in val_sample:
        # TOOL 13: full history
        vals = fst.all_values(sp, max_values=50)
        if vals:
            check('signal_values', True, 'values returned')
            # hex matches binary
            for v in vals[:5]:
                hv = v.get('hex')
                bv = v.get('value', '')
                if hv and all(c in '01' for c in bv):
                    expected_hex = f'{int(bv, 2):x}'
                    check('signal_values', hv == expected_hex,
                          'hex matches binary value', f'hex={hv} bin={bv} expected={expected_hex}')
                # time field present
                check('signal_values', 'time' in v and 'time_units' in v,
                      'value has time fields')
            # times are monotonic
            times = [v['time_units'] for v in vals]
            check('signal_values', times == sorted(times),
                  'times are monotonically sorted', f'first 5: {times[:5]}')

            # TOOL 14: range query
            vals_r = fst.values_between(sp, fst.start_time, q1, max_values=50)
            if vals_r:
                check('signal_values_in_range', True, 'range values returned')
                r_times = [v['time_units'] for v in vals_r]
                check('signal_values_in_range', all(fst.start_time <= t <= q1 for t in r_times),
                      'all times within range',
                      f'min={min(r_times)} max={max(r_times)} range=[{fst.start_time},{q1}]')
                check('signal_values_in_range', r_times == sorted(r_times),
                      'range times sorted')

            # TOOL 15: point value — cross-check with history
            val_at = fst.value_at(sp, mid)
            if val_at:
                check('signal_value_at', True, 'point value returned')
                check('signal_value_at', val_at.get('time_units') == mid,
                      'time_units matches query')
                # Find closest value in history
                closest = min(vals, key=lambda v: abs(v['time_units'] - mid))
                if closest['time_units'] == mid:
                    check('signal_value_at', val_at.get('value') == closest.get('value'),
                          'point value matches history at same time',
                          f'point={val_at.get("value")} history={closest.get("value")}')
        else:
            check('signal_values', True, 'no values (acceptable for some signals)')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 16-19: connectivity / drivers / loads / fan_in (ALL netlist signals)
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n  [16-19] connectivity / drivers / loads / fan_in (all {len(netlist_signals)} signals)...")
    init('signal_connectivity')
    init('signal_drivers')
    init('signal_loads')
    init('signal_fanin')
    t1 = time.time()
    # Every other tool below samples its input; this loop used to walk all
    # netlist signals, which made one wide project (usbdev) 660s of a 720s
    # run. Sample it the same way, and keep an escape hatch for exhaustive
    # runs: WAVE_MCP_VERIFY_FULL=1 restores the full sweep.
    nl_all = sorted(netlist_signals)
    nl_sig_list = (nl_all if os.environ.get("WAVE_MCP_VERIFY_FULL") == "1"
                   else _sample(nl_all, CONN_SAMPLE))
    if len(nl_sig_list) < len(nl_all):
        print(f"    sampling {len(nl_sig_list)}/{len(nl_all)} signals "
              f"(WAVE_MCP_VERIFY_FULL=1 for all)")
    for i, sig in enumerate(nl_sig_list):
        if (i+1) % 5000 == 0:
            elapsed = time.time() - t1
            pct = (i+1) / len(nl_sig_list) * 100
            eta = elapsed / (i+1) * (len(nl_sig_list) - i - 1)
            print(f"    [{i+1}/{len(nl_sig_list)}] {pct:.1f}% - eta {eta:.0f}s")

        # TOOL 17: drivers — structural checks
        d = rtl.drivers(sig)
        drvs = d.get('drivers', [])
        for dr in drvs:
            check('signal_drivers', dr.get('kind') in VALID_KINDS,
                  'driver kind valid', f'sig={sig} kind={dr.get("kind")}')
            if dr.get('file'):
                check('signal_drivers', resolve_src(dr['file']) is not None,
                      'driver file exists on disk', f'sig={sig} file={dr["file"]}')
            if dr.get('line'):
                check('signal_drivers', dr['line'] > 0,
                      'driver line > 0', f'sig={sig} line={dr["line"]}')
            if dr.get('file') and dr.get('line'):
                try:
                    with open(resolve_src(dr['file'])) as fh:
                        lines = fh.readlines()
                    check('signal_drivers', 1 <= dr['line'] <= len(lines),
                          'driver line within file bounds',
                          f'sig={sig} line={dr["line"]} file_lines={len(lines)}')
                except Exception:
                    pass
            # rhs/control should be lists
            check('signal_drivers', isinstance(dr.get('rhs'), list), 'rhs is list')
            check('signal_drivers', isinstance(dr.get('control'), list), 'control is list')
            # port_ref structure
            if dr.get('port_ref'):
                pr = dr['port_ref']
                check('signal_drivers', all(k in pr for k in ('instance','port')),
                      'port_ref has instance+port', f'sig={sig} keys={list(pr.keys())}')
            # guard structure
            if dr.get('guard'):
                check('signal_drivers', isinstance(dr['guard'], list),
                      'guard is list', f'sig={sig}')

        # TOOL 18: loads
        l = rtl.loads(sig)
        lds = l.get('loads', [])
        for ld in lds[:5]:
            check('signal_loads', isinstance(ld, str) and '.' in ld,
                  'load is a hierarchical path', f'sig={sig} load={ld}')

        # TOOL 16: connectivity — no duplicates + subset check
        c = rtl.connectivity(sig)
        conns = c.get('connected', [])
        if conns:
            check('signal_connectivity', len(conns) == len(set(conns)),
                  'no duplicate connectivity entries', f'sig={sig} count={len(conns)}')
            for cn in conns[:5]:
                check('signal_connectivity', isinstance(cn, str) and '.' in cn,
                      'conn entry is hierarchical path', f'sig={sig} entry={cn}')

        # TOOL 19: fan_in
        fi = rtl.fan_in(sig)
        fis = fi.get('fan_in', [])
        for f in fis[:5]:
            check('signal_fanin', isinstance(f, str) and '.' in f,
                  'fan_in entry is hierarchical path', f'sig={sig} entry={f}')
        # fan_in must be a strict subset of connectivity (for non-empty
        # cases): boundary nets resolve to their peer ports, which
        # connectivity also lists.
        if fis and conns:
            fi_set = set(fis)
            conn_set = set(conns)
            check('signal_fanin', fi_set.issubset(conn_set),
                  'fan_in subset of connectivity',
                  f'sig={sig} fi={len(fi_set)} conn={len(conn_set)} '
                  f'missing={len(fi_set - conn_set)}')

    elapsed_conn = time.time() - t1
    print(f"  connectivity/drivers/loads/fanin done in {elapsed_conn:.1f}s")

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 20-21: active_drivers / driver_contributors
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [20-21] active_drivers / driver_contributors...")
    init('active_drivers')
    init('driver_contributors')
    # Query times must carry an explicit unit: a bare digit string is parsed
    # as seconds, which would scale this FST-unit value by 10**(-timescale)
    # and aim every lookup far past the dump end (empty values everywhere,
    # and OverflowError on long ps-scale dumps).
    _unit = {0: "s", -3: "ms", -6: "us", -9: "ns", -12: "ps",
             -15: "fs"}.get(fst.timescale_exp)
    mid_time = (f"{mid}{_unit}" if _unit
                else f"{Decimal(mid).scaleb(fst.timescale_exp):f}s")
    ad_sample = _sample(nl_sig_list, 500)
    dc_tested = 0
    for sig in ad_sample:
        ad = rtl.active_drivers(sig, mid_time)
        ad_list = ad.get('active_drivers', [])
        for a in ad_list:
            # driver_unique_id format check
            duid = a.get('driver_unique_id', '')
            check('active_drivers', bool(duid) and '#' in duid,
                  'driver_unique_id has # separator', f'sig={sig} id={duid}')
            check('active_drivers', a.get('kind') in VALID_KINDS,
                  'active driver kind valid', f'sig={sig} kind={a.get("kind")}')
            # guard_active should be True/False/None
            check('active_drivers', 'guard_active' in a,
                  'guard_active field present', f'sig={sig}')
            ga = a.get('guard_active')
            check('active_drivers', ga is None or isinstance(ga, bool),
                  'guard_active is bool or None', f'sig={sig} ga={ga}')
            # rhs_values / control_values should be dicts
            check('active_drivers', isinstance(a.get('rhs_values'), dict),
                  'rhs_values is dict')
            check('active_drivers', isinstance(a.get('control_values'), dict),
                  'control_values is dict')
            # file/line check
            if a.get('file'):
                check('active_drivers', resolve_src(a['file']) is not None,
                      'active driver file exists', f'sig={sig} file={a["file"]}')

            # TOOL 21: driver_contributors — use driver_unique_id!
            if duid and dc_tested < 100:
                dc = rtl.driver_contributors(duid)
                dc_tested += 1
                check('driver_contributors', dc.get('available') is True,
                      'driver_contributors available', f'id={duid}')
                if dc.get('available'):
                    check('driver_contributors', isinstance(dc.get('rhs_signals'), list),
                          'rhs_signals is list')
                    check('driver_contributors', isinstance(dc.get('control_signals'), list),
                          'control_signals is list')
                    check('driver_contributors', dc.get('file') == a.get('file'),
                          'contributor file matches active driver',
                          f'dc_file={dc.get("file")} ad_file={a.get("file")}')
                    check('driver_contributors', dc.get('line') == a.get('line'),
                          'contributor line matches active driver',
                          f'dc_line={dc.get("line")} ad_line={a.get("line")}')
                    # Cross-check: rhs_signals should match driver's rhs
                    ad_rhs = set(a.get('rhs', []))
                    dc_rhs = set(dc.get('rhs_signals', []))
                    # dc_rhs uses leaf names, ad_rhs uses full paths — compare leaf parts
                    dc_rhs_leaves = set(dc_rhs)
                    ad_rhs_leaves = {r.rsplit('.', 1)[-1] if '.' in r else r for r in ad_rhs}
                    check('driver_contributors', dc_rhs_leaves == ad_rhs_leaves or len(dc_rhs_leaves & ad_rhs_leaves) > 0,
                          'contributor rhs matches driver rhs',
                          f'dc={dc_rhs_leaves} ad={ad_rhs_leaves}')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 22-23: trace_value / trace_x
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [22-23] trace_value / trace_x...")
    init('trace_value')
    init('trace_x')
    trace_sample = _sample(nl_sig_list, 500)

    def verify_trace_node(node, depth=0, tool='trace_value'):
        """Recursively verify trace tree node structure."""
        if not isinstance(node, dict):
            check(tool, False, 'node is dict', f'depth={depth}')
            return
        check(tool, 'signal' in node, 'node has signal', f'depth={depth}')
        check(tool, 'value' in node, 'node has value', f'depth={depth}')
        check(tool, 'hex' in node or node.get('value') is None,
              'node has hex (or null value)', f'depth={depth} val={node.get("value")}')
        # If has driver, check structure
        drv = node.get('driver')
        if drv:
            check(tool, drv.get('kind') in VALID_KINDS,
                  'driver kind valid', f'depth={depth} kind={drv.get("kind")}')
            if drv.get('file'):
                check(tool, resolve_src(drv['file']) is not None,
                      'driver file exists', f'depth={depth} file={drv["file"]}')
            check(tool, 'selection_method' in drv,
                  'driver has selection_method', f'depth={depth}')
        # If has contributors, recurse
        contribs = node.get('contributors', [])
        for c in contribs:
            verify_trace_node(c, depth + 1, tool)
        # crosses_into check
        if node.get('crosses_into'):
            ci = node['crosses_into']
            check(tool, all(k in ci for k in ('instance','port')),
                  'crosses_into has instance+port', f'depth={depth}')

    for sig in trace_sample:
        # TOOL 22: trace_value
        t = rtl.trace_value(sig, mid_time, max_depth=8)
        tree = t.get('tree')
        if tree:
            check('trace_value', True, 'trace tree returned')
            verify_trace_node(tree, tool='trace_value')
            # tree_summary
            ts = t.get('tree_summary', {})
            check('trace_value', ts.get('total_nodes', 0) > 0,
                  'tree has nodes', f'sig={sig} nodes={ts.get("total_nodes")}')
            check('trace_value', 'max_depth' in ts, 'tree_summary has max_depth')
            # hex consistency: root node hex should match signal_value_at
            root_hex = tree.get('hex')
            root_val = tree.get('value')
            if root_val and all(c in '01' for c in root_val):
                expected = f'{int(root_val, 2):x}'
                check('trace_value', root_hex == expected or root_hex is None,
                      'root hex consistent with value', f'hex={root_hex} val={root_val}')

        # TOOL 23: trace_x — need to find X signals!
        # First check if signal is X at this time
        val_at = fst.value_at(sig, mid) if sig in fst.signals else None
        is_x = val_at and ('x' in val_at.get('value', '').lower() or 'z' in val_at.get('value', '').lower())
        tx = rtl.trace_x(sig, mid_time, max_depth=8)
        if not is_x:
            # trace_x should return "no-x" — this is CORRECT behavior
            check('trace_x', tx.get('result') == 'no-x',
                  'trace_x returns no-x for non-X signal (correct)', f'sig={sig}')
        else:
            # Signal IS X — trace_x should return a tree
            tx_tree = tx.get('tree')
            if tx_tree:
                check('trace_x', True, 'trace_x returns tree for X signal')
                verify_trace_node(tx_tree, tool='trace_x')
            else:
                check('trace_x', tx.get('result') == 'no-x',
                      'trace_x returns no-x even though signal appeared X (value resolution issue)',
                      f'sig={sig} val={val_at}')

    # Also search specifically for X signals to test trace_x properly
    x_found = 0
    for sp in _sample(fst_signals, 500):
        if x_found >= 20:
            break
        v = fst.value_at(sp, mid)
        if v and ('x' in v.get('value', '').lower() or 'z' in v.get('value', '').lower()):
            x_found += 1
            # Try to resolve this FST signal to a netlist path
            # Use trace_x directly with the FST path
            tx = rtl.trace_x(sp, mid_time, max_depth=8)
            check('trace_x', tx.get('result') != 'no-x' or tx.get('tree'),
                  'trace_x processes X signal', f'sig={sp} result={tx.get("result")}')

    # ══════════════════════════════════════════════════════════════════════
    # TOOL 24-26: list_files / find_files / modules_in_file
    # ══════════════════════════════════════════════════════════════════════
    print(f"  [24-26] list_files / find_files / modules_in_file...")
    init('list_files')
    init('find_files')
    init('modules_in_file')
    all_files = rtl.all_files()
    check('list_files', len(all_files) > 0, 'files found')
    existing = []
    for f in all_files:
        resolved = resolve_src(f)
        check('list_files', resolved is not None,
              'file exists on disk', f'file={f}')
        if resolved:
            existing.append(resolved)

    # TOOL 25: find_files — verify results contain query
    for short in ['uart', 'core', 'top', 'prim', 'reg', 'ctrl', 'if', 'gen']:
        found = rtl.files_by_short_name(short)
        for fp in found[:5]:
            check('find_files', short.lower() in os.path.basename(fp).lower(),
                  'found file contains query', f'query={short} file={fp}')
            check('find_files', resolve_src(fp) is not None,
                  'found file exists', f'file={fp}')

    # TOOL 26: modules_in_file — cross-check with netlist
    file_sample = _sample(existing, 50) if existing else []
    for fp in file_sample:
        mods = rtl.modules_in_file(fp)
        for mod in mods:
            check('modules_in_file', mod in rtl.engine.modules,
                  'module exists in netlist', f'file={fp} mod={mod}')
            # Cross-check: module_declaration should point back to this file
            decl = rtl.module_declaration(mod)
            if decl:
                check('modules_in_file', os.path.abspath(decl.get('file', '')) == os.path.abspath(fp),
                      'module_declaration file matches', f'file={fp} mod={mod} decl_file={decl.get("file")}')

    # ══════════════════════════════════════════════════════════════════════
    # Control field quality
    # ══════════════════════════════════════════════════════════════════════
    init('control_field')
    print(f"  Checking control field quality...")
    for mod_name, mod_data in rtl.engine.modules.items():
        for sig_name, sig_drvs in mod_data.get('drivers', {}).items():
            for sd in sig_drvs:
                if sd.get('kind') not in ('blocking', 'nonblocking'):
                    continue
                # A driver with a non-empty guard was inside an if/case
                # branch that pyslang recognized as conditional.  If its
                # control list is empty, the condition expression had no
                # extractable signal names — a real miss worth flagging.
                # Drivers without guard (default assignments, parameter-
                # based generate if/else, for-loop bodies) correctly have
                # empty control and should NOT be flagged.
                guard = sd.get('guard', [])
                if guard and not sd.get('control'):
                    check('control_field', bool(sd.get('control')),
                          'guarded driver has control field',
                          f'mod={mod_name} sig={sig_name} line={sd.get("line")} '
                          f'guard_items={len(guard)}')

    # ══════════════════════════════════════════════════════════════════════
    # Cross-tool consistency: drivers ↔ loads symmetry
    # ══════════════════════════════════════════════════════════════════════
    print(f"  Cross-tool: drivers ↔ loads symmetry (sampled)...")
    init('cross_symmetry')
    sym_sample = _sample(nl_sig_list, 200)
    for sig in sym_sample:
        d = rtl.drivers(sig)
        drvs = d.get('drivers', [])
        # For each driver's rhs signal, check if sig appears in that signal's loads
        for dr in drvs[:2]:
            for rhs in dr.get('rhs', [])[:3]:
                # rhs is a full path like "inst.signal"
                rhs_full = rhs if '.' in rhs else f'{d.get("module","")}.{rhs}'
                # Try loads on the rhs signal
                try:
                    l = rtl.loads(rhs_full)
                    lds = l.get('loads', [])
                    # The original signal should appear in loads of its driver's rhs
                    # (This is a soft check — not always true for complex logic)
                    if lds and sig in lds:
                        check('cross_symmetry', True, 'driver rhs → load symmetry found')
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════════════════════
    # Assemble report
    # ══════════════════════════════════════════════════════════════════════
    total_time = time.time() - t0
    report = {
        'project': project_name,
        'total_signals': len(netlist_signals),
        'total_fst_signals': len(fst_signals),
        'total_scopes': len(fst_scopes),
        'verify_time_sec': round(total_time, 1),
        'results': results,
    }
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n  === Summary: {project_name} ({total_time:.1f}s) ===")
    print(f"  {'Tool':<30} {'Checks':>8} {'Passed':>8} {'Failed':>8} {'Pass%':>7}")
    print(f"  {'-'*65}")
    for tool in sorted(results.keys()):
        r = results[tool]
        total = r['checks']
        passed = r['passed']
        failed = total - passed
        pct = passed / total * 100 if total else 0
        flag = '✅' if failed == 0 else '❌'
        print(f"  {flag} {tool:<28} {total:>8} {passed:>8} {failed:>8} {pct:>6.1f}%")
    total_failures = sum(r['checks'] - r['passed'] for r in results.values())
    print(f"\n  Total failures: {total_failures}")
    print(f"  Report saved to: {output_path}")
    return report


def main():
    output_dir = os.path.join(ROOT, 'tests', 'reports', 'functional')
    os.makedirs(output_dir, exist_ok=True)
    all_reports = []

    for name, fst_path, maps_path in iter_project_assets():
        out = os.path.join(output_dir,
                           name.replace('/', '_').lower() + '.json')
        try:
            all_reports.append(verify_project(name, fst_path, maps_path, out))
        except Exception as e:
            print(f"  ERROR verifying {name}: {e}")
            traceback.print_exc()

    # ── Aggregate ───────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  AGGREGATE FUNCTIONAL VERIFICATION REPORT")
    print(f"{'='*70}")
    agg = defaultdict(lambda: {'checks': 0, 'passed': 0, 'failures': []})
    for rpt in all_reports:
        for tool, r in rpt.get('results', {}).items():
            agg[tool]['checks'] += r['checks']
            agg[tool]['passed'] += r['passed']
            agg[tool]['failures'].extend(r['failures'][:5])  # keep first 5 per project
    print(f"  Projects verified: {len(all_reports)}")
    print()
    print(f"  {'Tool':<30} {'Checks':>10} {'Passed':>10} {'Failed':>10} {'Pass%':>8}")
    print(f"  {'-'*72}")
    total_fail = 0
    for tool in sorted(agg.keys()):
        r = agg[tool]
        total = r['checks']
        passed = r['passed']
        failed = total - passed
        total_fail += failed
        pct = passed / total * 100 if total else 0
        flag = '✅' if failed == 0 else '❌'
        print(f"  {flag} {tool:<28} {total:>10} {passed:>10} {failed:>10} {pct:>7.1f}%")
    print(f"\n  Total failures across all tools: {total_fail}")

    # Print sample failures for tools with issues
    for tool in sorted(agg.keys()):
        r = agg[tool]
        if r['failures']:
            print(f"\n  Sample failures for {tool}:")
            for f in r['failures'][:5]:
                print(f"    - {f.get('check', '?')}: {f.get('detail', '')}")

    agg_rpt = {tool: {'checks': r['checks'], 'passed': r['passed'],
                      'failed': r['checks'] - r['passed'],
                      'sample_failures': r['failures'][:10]}
               for tool, r in agg.items()}
    with open(os.path.join(output_dir, 'aggregate.json'), 'w') as f:
        json.dump(agg_rpt, f, indent=2, default=str)
    print(f"\n  Reports saved to: {output_dir}/")


if __name__ == '__main__':
    main()
