#!/usr/bin/env bash
set -Eeuo pipefail
package=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=${RAMULATOR_ROOT:-$(cd -- "$package/.." && pwd)}
python_bin=${PYTHON_BIN:-python}
cmake_bin=${CMAKE_BIN:-cmake}
"$python_bin" "$package/install.py" --root "$root"
if [[ ! -f "$root/build/CMakeCache.txt" ]]; then
  "$cmake_bin" -S "$root" -B "$root/build" -DCMAKE_BUILD_TYPE=Release
fi
"$cmake_bin" --build "$root/build" -j "${BUILD_JOBS:-2}"
PYTHONPATH="$root/python:$root${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" - <<'PY'
import ramulator
assert hasattr(ramulator.refresh_manager, 'DDR5CAPRefresh')
assert hasattr(ramulator.row_policy, 'ClosedCAPExperiment')
assert hasattr(ramulator.controller_plugin, 'CAPExperimentStats')
print('PASS: new Python components are available.')
PY
