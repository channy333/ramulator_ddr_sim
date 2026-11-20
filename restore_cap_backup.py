#!/usr/bin/env python3
"""CAP 실험 설치 전 백업 복원. 기존 DDR5SameBank와 results는 유지합니다."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BASE = Path('src/ramulator/controller')
ORIGINALS = (BASE / 'controller_base.h', BASE / 'CMakeLists.txt')
ADDED = (
    BASE / 'cap_refresh_view.h',
    BASE / 'refresh/impl/ddr5_cap_refresh.cpp',
    BASE / 'rowpolicy/impl/closed_cap_experiment.cpp',
    BASE / 'plugin/impl/cap_experiment_stats.cpp',
)
GENERATED = (
    Path('python/ramulator/refresh_manager/ddr5_cap_refresh.py'),
    Path('python/ramulator/row_policy/closed_cap_experiment.py'),
    Path('python/ramulator/controller_plugin/cap_experiment_stats.py'),
)
INITS = tuple(p.parent / '__init__.py' for p in GENERATED)

def clean_snapshot(path):
    if not all((path / rel).is_file() for rel in ORIGINALS):
        return False
    header = (path / ORIGINALS[0]).read_text()
    cmake = (path / ORIGINALS[1]).read_text()
    if 'class ControllerBase : public IController, public Implementation {' not in header:
        return False
    if 'refresh/impl/ddr5_same_bank.cpp' not in cmake:
        return False  # Do not select a snapshot predating the original REFsb update.
    if any(name in header + cmake for name in
           ('ClosedCAPExperiment', 'DDR5CAPRefresh', 'closed_cap_experiment.cpp',
            'ddr5_cap_refresh.cpp', 'cap_experiment_stats.cpp')):
        return False
    # This restorer targets the first installation's two-file snapshot.
    return not any((path / rel).exists() for rel in ADDED)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/workspace'))
    parser.add_argument('--backup', type=Path, help='생략하면 CAP 설치 전의 첫 유효한 백업을 자동 선택')
    parser.add_argument('--cmake', default=os.environ.get('CMAKE_BIN', 'cmake'))
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--dry-run', action='store_true', help='복원 계획만 출력하고 파일은 변경하지 않음')
    args = parser.parse_args()
    root = args.root.resolve()
    snapshots = root / '.cap_refresh_backups'
    if not all((root / rel).is_file() for rel in ORIGINALS):
        parser.error(f'Ramulator source not found: {root}')
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    if args.backup:
        selected = (args.backup if args.backup.is_absolute() else snapshots / args.backup).resolve()
        if not clean_snapshot(selected):
            parser.error('Selected snapshot is not a pre-CAP backup preserving DDR5SameBank. No files changed.')
    else:
        choices = sorted(p for p in snapshots.iterdir() if p.is_dir() and clean_snapshot(p)) if snapshots.is_dir() else []
        if not choices:
            parser.error('No complete pre-CAP backup preserving DDR5SameBank was found. No files changed.')
        selected = choices[0]
    if not (root / BASE / 'refresh/impl/ddr5_same_bank.cpp').is_file():
        parser.error('Original DDR5SameBank source is missing. No files changed.')
    print('Pre-CAP snapshot:', selected, flush=True)
    print('Restore controller_base.h and CMakeLists.txt; archive four CAP sources.', flush=True)
    print('Preserve original DDR5SameBank, stock ClosedCAP, GPT-2 workloads and results.', flush=True)
    if args.dry_run:
        return
    cmake = shutil.which(args.cmake)
    if not cmake or not (root / 'build/CMakeCache.txt').is_file():
        parser.error('cmake or the existing build/CMakeCache.txt is missing. No files changed.')
    cmake = str(Path(cmake).resolve())

    archive = root / '.cap_restore_backups' / datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    archive.mkdir(parents=True, exist_ok=False)
    record = {'source_snapshot': str(selected), 'root': str(root), 'saved_files': {}, 'build_verified': False}
    for rel in ORIGINALS + ADDED + GENERATED + INITS:
        source = root / rel
        if not source.is_file():
            continue
        dest = archive / 'current' / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Preserve every current file before modifying any source.
        shutil.copyfile(source, dest)
        record['saved_files'][str(rel)] = hashlib.sha256(source.read_bytes()).hexdigest()
    log = archive / 'restore.json'
    log.write_text(json.dumps(record, indent=2) + '\n')
    print('Current experimental state saved:', archive, flush=True)
    for rel in ADDED:
        source = root / rel
        if source.is_file():
            dest = archive / 'disabled' / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest), copy_function=shutil.copyfile)
    for rel in ORIGINALS:
        shutil.copyfile(selected / rel, root / rel)

    try:
        # copy2 preserves the old CMakeLists timestamp. Explicitly regenerate
        # the cached build graph before removing the old objects from the link.
        subprocess.run([cmake, '-S', str(root), '-B', str(root / 'build')], cwd=root, check=True)
        subprocess.run([cmake, '--build', str(root / 'build'), '-j', str(args.jobs)], cwd=root, check=True)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(root / 'python') + os.pathsep + str(root) + os.pathsep + env.get('PYTHONPATH', '')
        check = '''import ramulator
assert hasattr(ramulator.refresh_manager, 'DDR5SameBank'), 'Original REFsb manager unavailable'
assert hasattr(ramulator.row_policy, 'Open')
assert hasattr(ramulator.row_policy, 'ClosedCAP'), 'Stock ClosedCAP should be preserved'
assert not hasattr(ramulator.refresh_manager, 'DDR5CAPRefresh')
assert not hasattr(ramulator.row_policy, 'ClosedCAPExperiment')
assert not hasattr(ramulator.controller_plugin, 'CAPExperimentStats')
assert ramulator.refresh_manager.DDR5SameBank().to_config()['impl'] == 'DDR5SameBank'
print('PASS: original DDR5SameBank/Open available; CAP experiment components disabled.')
'''
        # Codegen needs the old Python files until it regenerates __init__.py.
        # Only archive orphan wrappers after the new imports have been checked.
        subprocess.run([sys.executable, '-c', check], cwd=root, env=env, check=True)
        for rel in GENERATED:
            source = root / rel
            if source.is_file():
                dest = archive / 'disabled' / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest), copy_function=shutil.copyfile)
        assert all((root / rel).read_bytes() == (selected / rel).read_bytes() for rel in ORIGINALS)
    except Exception:
        print(f'Restore/build did not finish. Preserved state: {archive}', file=sys.stderr)
        print('No result directories were modified. Fix the reported error and rerun this script.', file=sys.stderr)
        raise
    record['build_verified'] = True
    log.write_text(json.dumps(record, indent=2) + '\n')
    print('RESTORE COMPLETE: CAP installation has been reverted.', flush=True)
    print('Use the previous gpt2_cpu_port/run_gpt2_refresh.sh for experiments.', flush=True)
    print('Running gpt2_cap_refresh_experiments/setup.sh would reinstall CAP.', flush=True)

if __name__ == '__main__':
    main()
