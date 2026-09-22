#!/usr/bin/env python3
"""Full-quantity quality check for the full wave-mcp tool set across all IPs."""
import json, sys, os, time, traceback
from collections import Counter, defaultdict
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from wave_mcp.sources.rtl_source import RtlSource
from wave_mcp.sources.fst_source import FstSource
sys.path.insert(0, HERE)
from functional_verify import iter_project_assets

def _sample(items, n):
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]

def check_control_quality(mod_name, mod_data):
    """Check control field quality using guard-based detection.

    A driver with a non-empty guard was inside an if/case branch that
    pyslang recognized as conditional.  If its control list is empty,
    the condition expression had no extractable signal names — a real
    miss.  Drivers without guard (default assignments, parameter-based
    generate if/else, for-loop bodies) correctly have empty control.
    """
    missing = []
    total_conditional = 0
    for sig_name, sig_drvs in mod_data.get('drivers', {}).items():
        for sd in sig_drvs:
            if sd.get('kind') not in ('blocking', 'nonblocking'):
                continue
            guard = sd.get('guard', [])
            if not guard:
                continue
            total_conditional += 1
            if not sd.get('control'):
                missing.append({
                    'module': mod_name, 'signal': sig_name,
                    'file': os.path.basename(sd.get('file', '')), 'line': sd.get('line'),
                    'kind': sd.get('kind'), 'rhs': sd.get('rhs', []),
                    'snippet': sd.get('snippet', '')[:120]})
    return total_conditional, missing

def full_quality_check_all_tools(project_name, fst_path, maps_path, output_path):
    print(f"\n{'='*70}")
    print(f"  Full Quality Check (all tools): {project_name}")
    print(f"{'='*70}")
    t0 = time.time()
    fst = FstSource(fst_path)
    rtl = RtlSource(maps_path=maps_path, fst=fst)
    # Connect FST scopes with netlist module definitions — without this,
    # all definition-based queries (instances_of_module, list_child_instances
    # module_type, etc.) return empty.
    if rtl.has_netlist:
        resolver_map = rtl.engine.resolve_definitions(list(fst.scopes.keys()))
        fst.apply_definition_map(resolver_map, source='netlist')
        fst.width_hint = rtl.signal_width
    # Build signal list from FST scopes (which match fst.signals keys),
    # NOT from netlist instance_tree (whose paths don't match FST scopes).
    inst_to_mod = dict(rtl.engine.instance_tree) if rtl.has_netlist else {}
    all_signals = []
    for fp, sig in fst.signals.items():
        if sig.scope:
            all_signals.append(fp)
    total_signals = len(all_signals)
    # Use FST scope paths for instance-based queries
    fst_scopes = list(fst.scopes.keys())
    print(f"  Total instances: {len(inst_to_mod)} (netlist), {len(fst_scopes)} (FST scopes)")
    print(f"  Total signals: {total_signals}")
    print(f"  Load time: {time.time()-t0:.1f}s")
    stats = defaultdict(lambda: {'total': 0, 'empty': 0, 'errors': 0})
    issues = defaultdict(list)
    tool_detail = {}

    # TOOL 1-3: session_info
    print(f"\n  [1-3] Session management...")
    # Pick a top-level FST scope that actually has children (e.g. tb_uart),
    # not a *_pkg package scope which has no children.
    root_with_children = [s for s in fst_scopes if '.' not in s and fst.scopes[s].children]
    top_inst = root_with_children[0] if root_with_children else (fst_scopes[0] if fst_scopes else '')
    stats['session_info']['total'] = 1
    stats['session_info']['empty'] = 0 if top_inst else 1
    tool_detail['session_info'] = {'top': top_inst, 'instances': len(inst_to_mod), 'signals': total_signals, 'netlist': rtl.has_netlist}

    # TOOL 5: convert_vcd_to_fst
    print(f"  [5] convert_vcd_to_fst (FST validity)...")
    fst_size = os.path.getsize(fst_path)
    stats['convert_vcd_to_fst']['total'] = 1
    stats['convert_vcd_to_fst']['empty'] = 0 if fst_size > 0 else 1
    tool_detail['convert_vcd_to_fst'] = {'fst_size_bytes': fst_size, 'timescale_exp': fst.timescale_exp, 'start_time': fst.start_time, 'end_time': fst.end_time, 'scope_count': len(fst.scopes), 'signal_count': len(fst.signals)}

    # TOOL 6: list_child_instances
    print(f"  [6] list_child_instances...")
    children = fst.child_instances(top_inst, levels=1, max_scopes=5000, filter_noise=True)
    stats['list_child_instances']['total'] = 1
    stats['list_child_instances']['empty'] = 0 if children else 1
    no_modtype = [c for c in children if not c.get('module_type')]
    tool_detail['list_child_instances'] = {'top_children': len(children), 'without_module_type': len(no_modtype), 'scope_kinds': dict(Counter(c.get('scope_kind', '?') for c in children))}
    if no_modtype:
        issues['list_child_instances'].append({'issue': 'children_without_module_type', 'count': len(no_modtype), 'examples': [c['full_path'] for c in no_modtype[:10]]})

    # TOOL 7: list_modules
    print(f"  [7] list_modules...")
    all_mods = fst.all_module_names()
    stats['list_modules']['total'] = 1
    stats['list_modules']['empty'] = 0 if all_mods else 1
    tool_detail['list_modules'] = {'module_count': len(all_mods), 'sample': all_mods[:20]}

    # TOOL 8-9: instances_of_module / matching
    print(f"  [8-9] instances_of_module / matching...")
    mod_inst_counts = {}
    for mod in all_mods:
        paths = fst.instances_by_module(mod)
        mod_inst_counts[mod] = len(paths)
        stats['instances_of_module']['total'] += 1
        if not paths:
            stats['instances_of_module']['empty'] += 1
            if len(issues['instances_of_module']) < 20:
                issues['instances_of_module'].append({'module': mod, 'count': 0})
    tool_detail['instances_of_module'] = {'modules_checked': len(all_mods), 'total_instances': sum(mod_inst_counts.values()), 'modules_with_zero_instances': sum(1 for v in mod_inst_counts.values() if v == 0)}
    test_mod = next((m for m, c in mod_inst_counts.items() if c > 0), None)
    if test_mod:
        paths = fst.instances_by_module(test_mod)
        test_sub = paths[0].split('.')[-1] if paths else ''
        if test_sub:
            filtered = fst.instances_by_module(test_mod, test_sub)
            stats['instances_of_module_matching']['total'] = 1
            stats['instances_of_module_matching']['empty'] = 0 if filtered else 1
            tool_detail['instances_of_module_matching'] = {'test_module': test_mod, 'filter': test_sub, 'matched': len(filtered), 'expected_at_least': 1}

    # TOOL 10: scope_info
    print(f"  [10] scope_info...")
    scope_sample = _sample(list(fst.scopes.keys()), 100)
    no_decl = 0
    for sp in scope_sample:
        info = fst.scope_info(sp)
        stats['scope_info']['total'] += 1
        if not info:
            stats['scope_info']['empty'] += 1
            continue
        if rtl.has_netlist:
            decl = rtl.module_declaration(info.get('module_type', ''))
            if not decl or not decl.get('file'):
                no_decl += 1
    tool_detail['scope_info'] = {'sampled': len(scope_sample), 'without_declaration': no_decl}

    # TOOL 11: list_signals
    print(f"  [11] list_signals...")
    # Use FST scope paths (not netlist instance paths) for signals_of_instance.
    inst_sample = _sample(fst_scopes, 200)
    total_sig_listed = 0
    no_width = 0
    no_type = 0
    bus_count = 0
    width_mismatch = 0
    for inst in inst_sample:
        sigs = fst.signals_of_instance(inst, limit=5000)
        stats['list_signals']['total'] += 1
        if not sigs:
            stats['list_signals']['empty'] += 1
        total_sig_listed += len(sigs)
        for s in sigs:
            if not s.get('width'):
                no_width += 1
            if not s.get('type'):
                no_type += 1
            if s.get('element_count', 1) > 1:
                bus_count += 1
                if s.get('width_matches_rtl') is False:
                    width_mismatch += 1
    tool_detail['list_signals'] = {'instances_sampled': len(inst_sample), 'total_signals_listed': total_sig_listed, 'without_width': no_width, 'without_type': no_type, 'aggregated_buses': bus_count, 'width_mismatch_rtl': width_mismatch}

    # TOOL 12: signal_info
    print(f"  [12] signal_info...")
    sig_sample_for_info = _sample(all_signals, 500)
    no_decl_sig = 0
    for sig_path in sig_sample_for_info:
        info = fst.signal_info(sig_path)
        stats['signal_info']['total'] += 1
        if not info:
            stats['signal_info']['empty'] += 1
            continue
        if rtl.has_netlist:
            leaf = info.get('name', '').split('[')[0]
            decl = rtl.signal_declaration(leaf)
            if not decl or not decl.get('line'):
                no_decl_sig += 1
    tool_detail['signal_info'] = {'sampled': len(sig_sample_for_info), 'without_declaration': no_decl_sig}

    # TOOL 13-15: signal_values / in_range / value_at
    print(f"  [13-15] signal_values / in_range / value_at...")
    fst_sig_paths = list(fst.signals.keys())
    val_sample = _sample(fst_sig_paths, 300)
    total_values = 0
    no_hex = 0
    mid = (fst.start_time + fst.end_time) // 2
    q1 = fst.start_time + (mid - fst.start_time) // 2
    for sp in val_sample:
        vals = fst.all_values(sp, max_values=100)
        stats['signal_values']['total'] += 1
        if vals is None or not vals:
            stats['signal_values']['empty'] += 1
        else:
            total_values += len(vals)
            for v in vals:
                if not v.get('hex'):
                    no_hex += 1
        vals_r = fst.values_between(sp, fst.start_time, q1, max_values=50)
        stats['signal_values_in_range']['total'] += 1
        if vals_r is None or not vals_r:
            stats['signal_values_in_range']['empty'] += 1
        val_at = fst.value_at(sp, mid)
        stats['signal_value_at']['total'] += 1
        if val_at is None:
            stats['signal_value_at']['empty'] += 1
    tool_detail['signal_values'] = {'sampled': len(val_sample), 'total_value_rows': total_values, 'without_hex': no_hex}
    tool_detail['signal_values_in_range'] = {'sampled': len(val_sample), 'range': f'{fst.start_time}-{q1}'}
    tool_detail['signal_value_at'] = {'sampled': len(val_sample), 'time_point': mid}

    # TOOL 16-19: connectivity / drivers / loads / fan_in (ALL signals)
    print(f"\n  [16-19] connectivity / drivers / loads / fan_in (all {total_signals} signals)...")
    t1 = time.time()
    for i, sig in enumerate(all_signals):
        if (i+1) % 1000 == 0:
            elapsed = time.time() - t1
            pct = (i+1) / total_signals * 100
            eta = elapsed / (i+1) * (total_signals - i - 1)
            print(f"    [{i+1}/{total_signals}] {pct:.1f}% - elapsed {elapsed:.0f}s, eta {eta:.0f}s")
        try:
            d = rtl.drivers(sig)
            drvs = d.get('drivers', [])
            stats['signal_drivers']['total'] += 1
            if not drvs:
                stats['signal_drivers']['empty'] += 1
                if len(issues['signal_drivers']) < 50:
                    issues['signal_drivers'].append({'signal': sig, 'reason': d.get('reason', '')})
        except Exception:
            stats['signal_drivers']['errors'] += 1
            stats['signal_drivers']['empty'] += 1
        try:
            l = rtl.loads(sig)
            lds = l.get('loads', [])
            stats['signal_loads']['total'] += 1
            if not lds:
                stats['signal_loads']['empty'] += 1
                if len(issues['signal_loads']) < 50:
                    issues['signal_loads'].append({'signal': sig, 'reason': l.get('reason', '')})
        except Exception:
            stats['signal_loads']['errors'] += 1
            stats['signal_loads']['empty'] += 1
        try:
            c = rtl.connectivity(sig)
            conns = c.get('connected', [])
            stats['signal_connectivity']['total'] += 1
            if not conns:
                stats['signal_connectivity']['empty'] += 1
                if len(issues['signal_connectivity']) < 50:
                    issues['signal_connectivity'].append({'signal': sig, 'reason': c.get('reason', '')})
        except Exception:
            stats['signal_connectivity']['errors'] += 1
            stats['signal_connectivity']['empty'] += 1
        try:
            fi = rtl.fan_in(sig)
            fis = fi.get('fan_in', [])
            stats['signal_fanin']['total'] += 1
            if not fis:
                stats['signal_fanin']['empty'] += 1
                if len(issues['signal_fanin']) < 50:
                    issues['signal_fanin'].append({'signal': sig, 'reason': fi.get('reason', '')})
        except Exception:
            stats['signal_fanin']['errors'] += 1
            stats['signal_fanin']['empty'] += 1
    elapsed_conn = time.time() - t1
    print(f"  connectivity/drivers/loads/fanin done in {elapsed_conn:.1f}s")

    # TOOL 20-21: active_drivers / driver_contributors
    print(f"  [20-21] active_drivers / driver_contributors (sampled)...")
    ad_sample = _sample(all_signals, 500)
    ad_empty = 0
    ad_errors = 0
    dc_total = 0
    dc_empty = 0
    mid_time = (fst.start_time + fst.end_time) // 2
    # Query times must carry an explicit unit: a bare digit string is parsed
    # as seconds, which would scale this FST-unit value by 10**(-timescale)
    # and aim every lookup far past the dump end (empty values everywhere,
    # and OverflowError on long ps-scale dumps).
    _unit = {0: "s", -3: "ms", -6: "us", -9: "ns", -12: "ps",
             -15: "fs"}.get(fst.timescale_exp)
    mid_time_str = (f"{mid_time}{_unit}" if _unit
                    else f"{Decimal(mid_time).scaleb(fst.timescale_exp):f}s")
    for sig in ad_sample:
        try:
            ad = rtl.active_drivers(sig, mid_time_str)
            stats['active_drivers']['total'] += 1
            ad_list = ad.get('active_drivers', ad.get('drivers', []))
            if not ad_list:
                ad_empty += 1
                stats['active_drivers']['empty'] += 1
                if len(issues['active_drivers']) < 30:
                    issues['active_drivers'].append({'signal': sig, 'reason': ad.get('reason', '')})
            else:
                first_drv = ad_list[0]
                # active_drivers records carry 'driver_unique_id'.
                drv_id = first_drv.get('driver_unique_id', '')
                if drv_id:
                    dc = rtl.driver_contributors(drv_id)
                    dc_total += 1
                    stats['driver_contributors']['total'] += 1
                    contribs = (dc.get('rhs_signals', [])
                                + dc.get('control_signals', []))
                    if not contribs:
                        dc_empty += 1
                        stats['driver_contributors']['empty'] += 1
                        if len(issues['driver_contributors']) < 20:
                            issues['driver_contributors'].append({'driver_id': drv_id, 'signal': sig})
        except Exception:
            ad_errors += 1
            stats['active_drivers']['errors'] += 1
    tool_detail['active_drivers'] = {'sampled': len(ad_sample), 'time_point': mid_time_str, 'empty': ad_empty, 'errors': ad_errors}
    tool_detail['driver_contributors'] = {'tested': dc_total, 'empty': dc_empty}

    # TOOL 22-23: trace_value / trace_x
    print(f"  [22-23] trace_value / trace_x (sampled)...")
    trace_sample = _sample(all_signals, 500)
    tv_depths = []
    tv_empty = 0
    tv_errors = 0
    tx_empty = 0
    tx_errors = 0
    for sig in trace_sample:
        try:
            t = rtl.trace_value(sig, 0, max_depth=12)
            stats['trace_value']['total'] += 1
            tree = t.get('tree', [])
            if not tree:
                tv_empty += 1
                stats['trace_value']['empty'] += 1
                if len(issues['trace_value']) < 30:
                    issues['trace_value'].append({'signal': sig, 'mode': t.get('mode', '')})
            else:
                tv_depths.append(len(tree))
        except Exception:
            tv_errors += 1
            stats['trace_value']['errors'] += 1
        try:
            tx = rtl.trace_x(sig, 0, max_depth=12)
            stats['trace_x']['total'] += 1
            tx_result = tx.get('result', '')
            tree_x = tx.get('tree', tx.get('trace', []))
            # trace_x on a non-X signal returns result='no-x' with empty tree
            # — this is correct behavior, NOT an empty-result defect.
            if not tree_x and tx_result != 'no-x':
                tx_empty += 1
                stats['trace_x']['empty'] += 1
        except Exception:
            tx_errors += 1
            stats['trace_x']['errors'] += 1
    tool_detail['trace_value'] = {'sampled': len(trace_sample), 'empty': tv_empty, 'errors': tv_errors, 'avg_depth': sum(tv_depths)/len(tv_depths) if tv_depths else 0, 'max_depth': max(tv_depths) if tv_depths else 0, 'min_depth': min(tv_depths) if tv_depths else 0}
    tool_detail['trace_x'] = {'sampled': len(trace_sample), 'empty': tx_empty, 'errors': tx_errors}

    # TOOL 24-26: list_files / find_files / modules_in_file
    print(f"  [24-26] list_files / find_files / modules_in_file...")
    all_files = rtl.all_files()
    stats['list_files']['total'] = 1
    stats['list_files']['empty'] = 0 if all_files else 1
    existing_files = [f for f in all_files if os.path.exists(f)]
    tool_detail['list_files'] = {'total_files': len(all_files), 'existing_on_disk': len(existing_files), 'missing_on_disk': len(all_files) - len(existing_files)}
    if len(all_files) != len(existing_files):
        issues['list_files'].append({'issue': 'files_missing_on_disk', 'count': len(all_files) - len(existing_files), 'examples': [f for f in all_files if not os.path.exists(f)][:5]})
    test_short_names = ['uart', 'core', 'top', 'prim', 'reg']
    for short in test_short_names:
        found = rtl.files_by_short_name(short)
        stats['find_files']['total'] += 1
        if not found:
            stats['find_files']['empty'] += 1
    tool_detail['find_files'] = {'queries_tested': len(test_short_names), 'queries': test_short_names}
    file_sample = _sample(existing_files, 50) if existing_files else []
    total_mods_in_files = 0
    empty_files = 0
    for fp in file_sample:
        mods = rtl.modules_in_file(fp)
        stats['modules_in_file']['total'] += 1
        if not mods:
            empty_files += 1
            stats['modules_in_file']['empty'] += 1
        else:
            total_mods_in_files += len(mods)
    tool_detail['modules_in_file'] = {'files_sampled': len(file_sample), 'total_modules_found': total_mods_in_files, 'files_without_modules': empty_files}

    # Control field quality
    print(f"\n  Checking control field quality across all modules...")
    total_cond = 0
    total_missing_ctrl = 0
    for mod_name, mod_data in rtl.engine.modules.items():
        cond, missing = check_control_quality(mod_name, mod_data)
        total_cond += cond
        total_missing_ctrl += len(missing)
        if missing:
            issues['missing_control'].extend(missing[:3])
    tool_detail['_control'] = {'total_conditional': total_cond, 'missing': total_missing_ctrl}
    print(f"  Conditional drivers: {total_cond}, missing control: {total_missing_ctrl}")

    total_check_time = time.time() - t0
    stats_serial = {k: dict(v) for k, v in stats.items()}
    report = {'project': project_name, 'total_signals': total_signals, 'total_instances': len(inst_to_mod), 'check_time_sec': round(total_check_time, 1), 'tools_checked': len(stats_serial), 'stats': stats_serial, 'tool_detail': tool_detail, 'issues': {k: v for k, v in issues.items() if v}}
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n  === Summary: {project_name} ({total_check_time:.1f}s) ===")
    print(f"  {'Tool':<30} {'Total':>8} {'Empty':>8} {'Empty%':>7} {'Errors':>7}")
    print(f"  {'-'*65}")
    for tool in ['session_info', 'convert_vcd_to_fst', 'list_child_instances', 'list_modules', 'instances_of_module', 'instances_of_module_matching', 'scope_info', 'list_signals', 'signal_info', 'signal_values', 'signal_values_in_range', 'signal_value_at', 'signal_connectivity', 'signal_drivers', 'signal_loads', 'signal_fanin', 'active_drivers', 'driver_contributors', 'trace_value', 'trace_x', 'list_files', 'find_files', 'modules_in_file']:
        s = stats.get(tool, {})
        total = s.get('total', 0)
        if total == 0:
            continue
        empty = s.get('empty', 0)
        errors = s.get('errors', 0)
        pct = empty / total * 100 if total else 0
        print(f"  {tool:<30} {total:>8} {empty:>8} {pct:>6.1f}% {errors:>7}")
    ctrl = tool_detail.get('_control', {})
    print(f"\n  Control: conditional={ctrl.get('total_conditional',0)} missing={ctrl.get('missing',0)}")
    print(f"  Reports saved to: {output_path}")
    return report

def main():
    output_dir = os.path.join(ROOT, 'tests', 'reports', 'quality_v2')
    os.makedirs(output_dir, exist_ok=True)
    all_reports = []
    for name, fst_path, maps_path in iter_project_assets():
        out = os.path.join(output_dir,
                           name.replace('/', '_').lower() + '.json')
        try:
            all_reports.append(full_quality_check_all_tools(name, fst_path, maps_path, out))
        except Exception as e:
            print(f"  ERROR checking {name}: {e}")
            traceback.print_exc()
    print(f"\n{'='*70}")
    print(f"  AGGREGATE QUALITY REPORT (all tools)")
    print(f"{'='*70}")
    agg = defaultdict(lambda: {'total': 0, 'empty': 0, 'errors': 0})
    total_signals_all = 0
    all_tool_detail = defaultdict(lambda: defaultdict(list))
    for rpt in all_reports:
        total_signals_all += rpt['total_signals']
        for tool, s in rpt.get('stats', {}).items():
            agg[tool]['total'] += s.get('total', 0)
            agg[tool]['empty'] += s.get('empty', 0)
            agg[tool]['errors'] += s.get('errors', 0)
        for tool, detail in rpt.get('tool_detail', {}).items():
            for k, v in detail.items():
                if isinstance(v, (int, float)):
                    all_tool_detail[tool][k].append(v)
    print(f"  Total signals across all projects: {total_signals_all}")
    print(f"  Projects checked: {len(all_reports)}")
    print()
    print(f"  {'Tool':<30} {'Total':>10} {'Empty':>10} {'Empty%':>8} {'Errors':>8}")
    print(f"  {'-'*70}")
    tool_order = ['session_info', 'convert_vcd_to_fst', 'list_child_instances', 'list_modules', 'instances_of_module', 'instances_of_module_matching', 'scope_info', 'list_signals', 'signal_info', 'signal_values', 'signal_values_in_range', 'signal_value_at', 'signal_connectivity', 'signal_drivers', 'signal_loads', 'signal_fanin', 'active_drivers', 'driver_contributors', 'trace_value', 'trace_x', 'list_files', 'find_files', 'modules_in_file']
    for tool in tool_order:
        s = agg.get(tool, {})
        total = s.get('total', 0)
        if total == 0:
            print(f"  {tool:<30} {'N/A':>10}")
            continue
        empty = s.get('empty', 0)
        errors = s.get('errors', 0)
        pct = empty / total * 100 if total else 0
        print(f"  {tool:<30} {total:>10} {empty:>10} {pct:>7.1f}% {errors:>8}")
    print(f"\n  Key Detail Metrics:")
    for tool in tool_order:
        detail = all_tool_detail.get(tool, {})
        if not detail:
            continue
        parts = []
        for k, vals in detail.items():
            if vals and isinstance(vals[0], (int, float)):
                parts.append(f"{k}={sum(vals)}/{len(vals)} projects")
        if parts:
            print(f"    {tool}: {', '.join(parts[:4])}")
    agg_rpt = {'total_signals': total_signals_all, 'projects': len(all_reports), 'tools_checked': len(agg), 'stats': {k: dict(v) for k, v in agg.items()}}
    with open(os.path.join(output_dir, 'aggregate.json'), 'w') as f:
        json.dump(agg_rpt, f, indent=2, default=str)
    print(f"\n  Reports saved to: {output_dir}/")

if __name__ == '__main__':
    main()
