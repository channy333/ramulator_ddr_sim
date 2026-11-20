#!/usr/bin/env python3
"""Check the actual shell + summary CLI using native DRAM fixtures and a mock gem5.

CPU statistics are synthetic. This tests orchestration, not gem5 performance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from native_common import bootstrap, make, addr, tick_to
from policies import POLICIES

PACKAGE = Path(__file__).resolve().parent

def fixture(path, policy, root):
    import yaml
    path.mkdir(parents=True)
    d = make(policy, 16, path / 'cap_metrics')
    d.send_request('Read', addr(d))
    tick_to(d, 900); d.reset_stats(); tick_to(d, 1800)
    (path / 'ramulator_stats.yaml').write_text(yaml.safe_dump({'memory_system': {'controller': [d.stats()]}}))
    begin, end = 900 * 416, 1800 * 416
    (path / 'roi.json').write_text(json.dumps({'begin_tick': begin, 'end_tick': end, 'ticks': end - begin, 'seconds': (end - begin) / 1e12}))
    (path / 'experiment.json').write_text(json.dumps({'policy': policy, 'cap': 16, 'cpu': 'timing', 'cache': 'l1l2',
        'l1d_prefetch': 'on', 'l2_prefetch': 'off', 'binary': str(root / 'gpt2_cpu_port/bin/gpt2_cpu_m5'),
        'workload_args': ['--model', str(root / 'workloads/gpt2/models/gpt2_cpu.bin'), '--tokens',
                          str(root / 'workloads/gpt2/models/tokens.txt'), '--mode', 'hidden', '--blocks', '1', '--roi']}))
    (path / 'stats.txt').write_text('\n'.join(['---------- Begin Simulation Statistics ----------',
        f'finalTick {end}', f'simTicks {end - begin}', 'simFreq 1000000000000',
        'board.processor.cores.core.numCycles 1124', 'simInsts 100', '---------- End Simulation Statistics ----------']))
    with (path / 'command_counts.csv').open('w') as f:
        for name in ('ACT', 'RD', 'WR', 'RDA', 'WRA', 'PREpb', 'PREsb', 'PREab', 'REFsb', 'REFab'):
            f.write(f"{name},{sum(h.command == name and h.clk > 900 for h in d.history)}\n")

def raw_hash(path):
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in path.iterdir()
            if f.name in ('stats.txt', 'roi.json', 'experiment.json', 'command_counts.csv',
                          'cap_metrics.ch0.csv', 'ramulator_stats.yaml')}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=PACKAGE.parent)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    bootstrap(args.root)
    checks = []
    with tempfile.TemporaryDirectory(prefix='CAP runner ') as tmp:
        base = Path(tmp); root = base / 'fake root'; root.mkdir()
        for name in ('gpt2_cpu_port/bin/gpt2_cpu_m5', 'workloads/gpt2/models/gpt2_cpu.bin', 'workloads/gpt2/models/tokens.txt'):
            f = root / name; f.parent.mkdir(parents=True, exist_ok=True); f.touch(); f.chmod(0o755)
        templates = base / 'templates'
        for policy in POLICIES: fixture(templates / policy, policy, root)
        calls = base / 'calls.txt'
        fake = root / 'gem5_mock'
        fake.write_text('#!' + sys.executable + '\n' + '''import os, pathlib, shutil, sys
a = sys.argv[1:]
policy = a[a.index('--policy') + 1]
with open(os.environ['CAP_TEST_CALLS'], 'a') as f: f.write(policy + '\\n')
if os.environ.get('CAP_TEST_FAIL'): sys.exit(19)
out = pathlib.Path(next(x.split('=', 1)[1] for x in a if x.startswith('--outdir=')))
shutil.copytree(pathlib.Path(os.environ['CAP_TEST_TEMPLATES']) / policy, out, dirs_exist_ok=True)
''')
        fake.chmod(0o755)
        env = {**os.environ, 'RAMULATOR_ROOT': str(root), 'GEM5_BIN': str(fake), 'PYTHON_BIN': sys.executable,
               'CAP_TEST_CALLS': str(calls), 'CAP_TEST_TEMPLATES': str(templates)}
        def run(out, *extra, environment=None):
            return subprocess.run(['bash', str(PACKAGE / 'run_experiment.sh'), '--workload', 'block1',
                '--policy', 'all', '--cap', '16', '--prefetch', 'l1', '--outdir', str(out), *extra],
                env=environment or env, capture_output=True, text=True)
        def call_count(): return len(calls.read_text().splitlines()) if calls.exists() else 0
        fresh = base / 'fresh results'
        r = run(fresh); assert r.returncode == 0, r.stdout + r.stderr
        assert call_count() == 6 and (fresh / 'comparison.csv').is_file()
        checks.append('fresh_all_six_conditions_and_required_summary_cli')
        before = {p: raw_hash(fresh / p) for p in POLICIES}
        r = run(fresh, '--resume'); assert r.returncode == 0, r.stdout + r.stderr
        assert call_count() == 6 and r.stdout.count('Reusing completed ROI') == 6
        assert before == {p: raw_hash(fresh / p) for p in POLICIES}
        checks.append('completed_resume_skips_all_gem5_and_preserves_raw_files')
        stopped = base / 'stopped after open_rr'
        shutil.copytree(templates / 'open_rr', stopped / 'open_rr')
        before = raw_hash(stopped / 'open_rr')
        r = run(stopped, '--resume'); assert r.returncode == 0, r.stdout + r.stderr
        assert call_count() == 11 and raw_hash(stopped / 'open_rr') == before
        assert all((stopped / p / 'cap_summary.json').is_file() for p in POLICIES)
        checks.append('original_failure_recovery_reuses_open_rr_and_runs_five_remaining')
        r = run(stopped, '--resume', '--cap', '32')
        assert r.returncode != 0 and 'Resume settings differ' in r.stderr and call_count() == 11
        checks.append('mismatched_settings_rejected_before_any_simulation')
        partial = base / 'partial'; (partial / 'cap_rr').mkdir(parents=True)
        (partial / 'cap_rr/terminal.log').write_text('incomplete')
        r = run(partial, '--resume')
        assert r.returncode != 0 and 'Partial/incomplete' in r.stderr and call_count() == 11
        assert not (partial / 'open_rr').exists()
        checks.append('partial_run_rejected_during_preflight_without_overwrite')
        r = run(stopped)
        assert r.returncode != 0 and 'not empty' in r.stderr and call_count() == 11
        checks.append('default_nonempty_output_protection')
        r = run(base / 'failed', environment={**env, 'CAP_TEST_FAIL': '1'})
        assert r.returncode == 19 and call_count() == 12
        checks.append('gem5_failure_stops_before_summary_and_later_conditions')
        out = base / 'dry run'; r = run(out, '--resume', '--dry-run')
        assert r.returncode == 0 and not out.exists() and call_count() == 12
        checks.append('dry_run_is_read_only')
    report = {'passed': True, 'checks': checks, 'scope': 'actual shell, summary CLI and analyzer; mock gem5, synthetic CPU stats, native C++ DRAM fixtures'}
    if args.report: args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(f'PASS: {len(checks)} runner/recovery integration checks. No actual gem5 was run.')

if __name__ == '__main__': main()
