#!/usr/bin/env python3
"""Read-only preflight: reuse completed, matching ROIs; reject partial runs."""
import argparse
import contextlib
import io
import json
from pathlib import Path
from summarize_roi import summarize

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--cap', type=int, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--l1d-prefetch', required=True)
    parser.add_argument('--l2-prefetch', required=True)
    # parse_known_args retains arbitrary workload options after the separator.
    args, workload = parser.parse_known_args()
    if not workload or workload[0] != '--':
        parser.error('Pass workload arguments after --')
    workload = workload[1:]
    directory = args.directory.resolve()
    if not directory.exists() or (directory.is_dir() and not any(directory.iterdir())):
        print('New condition:', directory)
        return
    if not directory.is_dir():
        parser.error(f'Output path is not a directory: {directory}')
    required = ('experiment.json', 'roi.json', 'stats.txt', 'command_counts.csv', 'cap_metrics.ch0.csv')
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        parser.error(f'Partial/incomplete run at {directory}; missing {missing}. '
                     'Preserve/move this condition directory before rerunning; files were not overwritten.')
    try:
        actual = json.loads((directory / 'experiment.json').read_text())
        expected = {'policy': args.policy, 'cap': args.cap, 'cpu': 'timing', 'cache': 'l1l2',
                    'l1d_prefetch': args.l1d_prefetch, 'l2_prefetch': args.l2_prefetch,
                    'binary': str(args.binary.resolve()), 'workload_args': workload}
        mismatch = [key for key, value in expected.items() if actual.get(key) != value]
        if mismatch:
            parser.error(f'Resume settings differ at {directory}: {mismatch}; files were not overwritten.')
        # A WORKEND marker alone is insufficient: validate its matching stats dump
        # and Ramulator measured cycles before deciding to skip gem5.
        with contextlib.redirect_stdout(io.StringIO()):
            summarize(directory, 416)
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        parser.error(f'Cannot reuse {directory}: {exc}')
    print('Completed ROI can be reused:', directory)

if __name__ == '__main__':
    main()
