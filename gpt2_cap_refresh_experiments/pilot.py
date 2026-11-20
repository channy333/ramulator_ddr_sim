#!/usr/bin/env python3
"""Small deterministic DRAM-only workloads. No CPU/cache/prefetch performance is modeled."""
import argparse
import csv
import json
from collections import deque
from pathlib import Path
from native_common import bootstrap, make, addr, tick_to, open_all, flat_stats
from policies import POLICIES

def run_stream(policy, cap, case, out):
    prefix = out / f'{case}_{policy}'
    d = make(policy, cap, prefix)
    # Same *arrival* stream for every policy, with retry-on-backpressure.
    events = []
    for i in range(1200):
        if case == 'long_row_hits':
            bank, bg, row = (i // 96) % 4, (i // 24) % 8, 3
        else:
            bank, bg, row = i % 4, (i // 4) % 8, (i // 32) % 4
        events.append((1 + i * 16, addr(d, bank, bg, row, column=(i % 16) * 16)))
    waiting = deque(); cursor = 0
    for clock in range(1, 500001):
        while cursor < len(events) and events[cursor][0] <= clock:
            waiting.append(events[cursor][1]); cursor += 1
        if waiting:
            try:
                d.send_request('Read', waiting[0])
                waiting.popleft()
            except RuntimeError as exc:
                if 'failed to enqueue request' not in str(exc): raise
        d.tick()
        if cursor == len(events) and not waiting and d.is_idle(): break
    else:
        raise RuntimeError('Pilot did not drain within 500000 cycles')
    s = flat_stats(d.stats())
    return {'case': case, 'policy': policy, 'cap': cap, 'completion_ck': clock,
        'requests': len(events), 'ACT': s['act'], 'effective_bank_precharges': s['effective_bank_precharges'],
        'read_latency_ck': s['avg_read_latency'], 'REFsb': s['issued_refsb'],
        'selected_cap_ready_banks': s['selected_cap_ready_banks'],
        'slots_with_cap_candidate': s['slots_with_cap_candidate'],
        'cap_pre_cancelled': s.get('cap_pre_cancelled', 0),
        'cap_reopen_then_refresh_close': s['cap_reopen_then_refresh_close']}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--outdir', type=Path, required=True)
    parser.add_argument('--cap', type=int, default=16)
    args = parser.parse_args()
    bootstrap(args.root)
    if args.cap <= 0: parser.error('--cap must be positive')
    out = args.outdir.resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        parser.error('Output directory must be new or empty')
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in ('long_row_hits', 'row_conflicts'):
        for policy in POLICIES:
            row = run_stream(policy, args.cap, case, out)
            rows.append(row)
            print(f"{case:14s} {policy:12s} cycles={row['completion_ck']:6d} "
                  f"ACT={row['ACT']:5d} bank-PRE={row['effective_bank_precharges']:5d} "
                  f"CAP-ready={row['selected_cap_ready_banks']}")
    with (out / 'pilot.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    (out / 'scope.json').write_text(json.dumps({'scope': 'native DRAM-only synthetic traffic',
        'cap': args.cap, 'gem5_run': False, 'energy_measured': False,
        'end': 'last read completion; a trailing refresh may still be pending'}, indent=2) + '\n')
    print('Saved:', out / 'pilot.csv')

if __name__ == '__main__': main()
