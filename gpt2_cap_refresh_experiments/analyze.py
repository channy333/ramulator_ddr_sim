#!/usr/bin/env python3
"""Summarize ROI-aligned controller measurements; never infer power from PRE counts."""
import argparse
import csv
import json
from pathlib import Path
import yaml
from summarize_roi import parse_stats, summarize

def flatten(value, prefix=''):
    out = {}
    if isinstance(value, dict):
        for key, child in value.items():
            out.update(flatten(child, prefix + str(key) + '.'))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            out.update(flatten(child, prefix + str(i) + '.'))
    else:
        out[prefix.rstrip('.')] = value
    return out

def metric_csv(path):
    with path.open() as f:
        return {r['key']: int(r['value']) for r in csv.DictReader(f)}

def analyze(directory):
    roi = json.loads((directory / 'roi.json').read_text())
    # Reuse strict checks for matching gem5 dump, DRAM cycles and time units.
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        summarize(directory, 416)
    config = json.loads((directory / 'experiment.json').read_text())
    snapshot = directory / f"ramulator_stats.{roi['end_tick']}.yaml"
    if not snapshot.exists():
        snapshot = directory / 'ramulator_stats.yaml'
    flat = flatten(yaml.safe_load(snapshot.read_text()))
    def find(name, default=None):
        vals = [v for k, v in flat.items() if k.split('.')[-1] == name]
        if len(vals) > 1:
            raise ValueError(f'Multiple controllers/components expose {name}: {directory}')
        return vals[0] if vals else default
    metrics = metric_csv(directory / 'cap_metrics.ch0.csv')
    if metrics['duplicate_ids_in_round']:
        raise ValueError(f'Repeated bank ID in an incomplete refresh round: {directory}')
    measured_ck = metrics['end_dram_cycle'] - metrics['begin_dram_cycle']
    if abs(measured_ck * 416e-12 - roi['seconds']) > 2 * 416e-12:
        raise ValueError('Observer measurement does not match the ROI')
    stats = next(b for b in parse_stats(directory / 'stats.txt')
                 if b.get('finalTick') == roi['end_tick'] and b.get('simTicks') == roi['ticks'])
    counts = {}
    with (directory / 'command_counts.csv').open() as f:
        for row in csv.reader(f):
            if len(row) >= 2:
                counts[row[0].strip()] = int(row[1].strip())
    if counts.get('ACT') != metrics['act'] or counts.get('REFsb') != metrics['issued_refsb']:
        raise ValueError('CommandCounter and experiment observer disagree')
    s = {'policy': config['policy'], 'cap': config['cap'], 'roi_ms': roi['seconds'] * 1e3,
         'cpu_cycles': stats.get('board.processor.cores.core.numCycles'),
         'instructions': stats.get('simInsts'), 'ACT': counts['ACT'], 'REFsb': counts['REFsb'],
         'effective_bank_precharges': metrics['effective_bank_precharges'],
         'cap_reopen_then_refresh_close': metrics['cap_reopen_then_refresh_close'],
         'dram_avg_read_latency_ck': find('avg_read_latency'),
         'reads_served': find('num_read_reqs_served'), 'writes_served': find('num_write_reqs_served')}
    total = sum(find(k, 0) for k in ('row_hits', 'row_misses', 'row_conflicts'))
    s['row_hit_pct'] = find('row_hits', 0) / total * 100 if total else None
    samples = metrics['refsb_delay_samples']
    s['refsb_mean_delay_ck'] = metrics['refsb_delay_sum_ck'] / samples if samples else None
    s['refsb_max_delay_ck'] = metrics['refsb_delay_max_ck']
    for key in ('cap_pre_injected', 'cap_pre_cancelled', 'cap_pre_shared_bank_cycles',
                'selected_refsb', 'selected_closed_banks', 'selected_cap_ready_banks',
                'selected_queued_accesses', 'changed_from_rr', 'slots_with_cap_candidate'):
        s[key] = find(key, 0)
    selected = s['selected_refsb']
    s['cap_candidate_slot_pct'] = s['slots_with_cap_candidate'] / selected * 100 if selected else None
    s['selected_cap_banks_per_refsb'] = s['selected_cap_ready_banks'] / selected if selected else None
    # Keep per-bank effective closures and raw instructions separate.
    s.update({f'cmd_{k}': v for k, v in counts.items()})
    (directory / 'cap_summary.json').write_text(json.dumps(s, indent=2) + '\n')
    print(f"{s['policy']}: ROI={s['roi_ms']:.6f} ms ACT={s['ACT']:,} "
          f"bank-precharges={s['effective_bank_precharges']:,} "
          f"CAP-candidate-slots={s['cap_candidate_slot_pct'] or 0:.3f}%")
    return s, config

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    directory = parser.parse_args().directory.resolve()
    dirs = [directory] if (directory / 'roi.json').exists() else sorted(p.parent for p in directory.glob('*/roi.json'))
    if not dirs:
        parser.error('No completed ROI found')
    result = [analyze(p) for p in dirs]
    rows = [s for s, _ in result]
    configs = [c for _, c in result]
    keys = ('cap', 'cpu', 'cache', 'l1d_prefetch', 'l2_prefetch', 'binary', 'workload_args')
    if any(any(c[k] != configs[0][k] for k in keys) for c in configs[1:]):
        raise ValueError('Comparison mixes workloads, CAP values or CPU/cache/prefetch settings')
    base = next((s for s in rows if s['policy'] == 'cap_rr'), None)
    if base:
        for row in rows:
            row['time_change_vs_cap_rr_pct'] = (row['roi_ms'] / base['roi_ms'] - 1) * 100
            row['ACT_change_vs_cap_rr_pct'] = (row['ACT'] / base['ACT'] - 1) * 100 if base['ACT'] else None
    with (directory / 'comparison.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    print('Saved:', directory / 'comparison.csv')

if __name__ == '__main__':
    main()
