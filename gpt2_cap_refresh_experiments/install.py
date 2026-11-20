#!/usr/bin/env python3
"""Add independent experiment components; preserve existing controller edits."""
import argparse
import datetime
import shutil
from pathlib import Path

def main():
    package = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=package.parent)
    args = parser.parse_args()
    root = args.root.resolve()
    base = root / 'src/ramulator/controller'
    header, cmake = base / 'controller_base.h', base / 'CMakeLists.txt'
    if not header.is_file() or not cmake.is_file():
        parser.error(f'Not the supported Ramulator 2.1 source layout: {root}')
    destinations = {
        'cap_refresh_view.h': 'cap_refresh_view.h',
        'closed_cap_experiment.cpp': 'rowpolicy/impl/closed_cap_experiment.cpp',
        'ddr5_cap_refresh.cpp': 'refresh/impl/ddr5_cap_refresh.cpp',
        'cap_experiment_stats.cpp': 'plugin/impl/cap_experiment_stats.cpp',
    }
    planned = {base / dest: (package / 'src' / src).read_bytes() for src, dest in destinations.items()}
    text = header.read_text()
    anchor = 'class ControllerBase : public IController, public Implementation {'
    if text.count(anchor) != 1:
        parser.error('ControllerBase declaration changed; no files modified.')
    for cls in ('ClosedCAPExperiment', 'DDR5CAPRefresh'):
        friend = f'  friend class {cls};'
        if friend not in text:
            text = text.replace(anchor, anchor + '\n' + friend)
    planned[header] = text.encode()
    text = cmake.read_text()
    anchors = {
        'refresh/impl/no_refresh.cpp': 'refresh/impl/ddr5_cap_refresh.cpp',
        'rowpolicy/impl/closed_cap.cpp': 'rowpolicy/impl/closed_cap_experiment.cpp',
        'plugin/impl/command_counter.cpp': 'plugin/impl/cap_experiment_stats.cpp',
    }
    for anchor, addition in anchors.items():
        if addition not in text:
            if text.count(anchor) != 1:
                parser.error(f'CMake anchor changed: {anchor}; no files modified.')
            text = text.replace(anchor, anchor + '\n  ' + addition)
    planned[cmake] = text.encode()
    changes = {p: data for p, data in planned.items() if not p.exists() or p.read_bytes() != data}
    if not changes:
        print('Already installed; no source changes.')
        return
    backup = root / '.cap_refresh_backups' / datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    for path, data in changes.items():
        if path.exists():
            dest = backup / path.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print('Installed:', path.relative_to(root))
    print('Original modified files backed up:', backup)

if __name__ == '__main__':
    main()
