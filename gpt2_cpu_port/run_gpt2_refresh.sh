#!/usr/bin/env bash
# Full GPT-2 forward ROI runner for the existing gem5_gpt2_se.py starter.
set -Eeuo pipefail

usage() {
    cat <<'HELP'
Usage: bash run_gpt2_refresh.sh MODE --outdir DIRECTORY [--dry-run] [--record-commands]

  off                 Run full GPT-2 with refresh disabled.
  allbank             Run full GPT-2 with AllBank / REFab refresh.
  refsb               Run full GPT-2 with the supplied DDR5SameBank manager.
  both                Run allbank, then off, with the same workload (default).
  all                 Run allbank, off, then refsb, with the same workload.
  --outdir DIRECTORY  Choose the output directory; no timestamp is appended.
  --dry-run           Print commands without starting gem5 or creating results.
  --record-commands   Also record the command trace; can create large files.

Place this script in /workspace/gpt2_cpu_port, beside gem5_gpt2_se.py.
Single mode: files are written directly inside DIRECTORY.
both/all: files are written inside DIRECTORY/<policy>/.
An existing nonempty output directory is rejected to preserve previous results.

Optional environment overrides:
  RAMULATOR_ROOT  GEM5_BIN  GEM5_CONFIG  GPT2_BINARY  MODEL  TOKENS
  OUTDIR  PYTHON_BIN  CPU  CACHE  TCK_PS

Examples:
  bash run_gpt2_refresh.sh off --outdir /workspace/results/gpt2_REFno_roi
  bash run_gpt2_refresh.sh refsb --outdir /workspace/results/gpt2_REFsb_roi
  bash run_gpt2_refresh.sh all --outdir /workspace/results/gpt2_compare
  bash run_gpt2_refresh.sh both --outdir /workspace/results/test --dry-run

REFsb requires installation of the supplied C++ manager and updated gem5 config.
HELP
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

mode=both
if (($#)) && [[ "$1" != --* ]]; then
    mode=$1
    shift
fi
dry_run=0
record_commands=0
output_dir=${OUTDIR:-}
while (($#)); do
    case "$1" in
        --dry-run) dry_run=1 ;;
        --record-commands) record_commands=1 ;;
        --outdir)
            (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || die '--outdir needs a directory.'
            output_dir=$2
            shift ;;
        --outdir=*) output_dir=${1#--outdir=} ;;
        --help|-h) usage; exit 0 ;;
        *) die "Unknown option: $1 (use --help)" ;;
    esac
    shift
done
case "$mode" in
    off|allbank|refsb) policies=("$mode") ;;
    both) policies=(allbank off) ;;
    all) policies=(allbank off refsb) ;;
    -h) usage; exit 0 ;;
    *) die "Unknown mode: $mode (use off, allbank, refsb, both, or all)" ;;
esac
[[ -n "$output_dir" ]] || die 'Specify --outdir DIRECTORY (or set OUTDIR).'

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=${RAMULATOR_ROOT:-$(cd -- "$script_dir/.." && pwd)}
gem5=${GEM5_BIN:-$root/tools/gem5/build/X86/gem5.opt}
config=${GEM5_CONFIG:-$root/gpt2_cpu_port/gem5_gpt2_se.py}
binary=${GPT2_BINARY:-$root/gpt2_cpu_port/bin/gpt2_cpu_m5}
model=${MODEL:-$root/workloads/gpt2/models/gpt2_cpu.bin}
tokens=${TOKENS:-$root/workloads/gpt2/models/tokens.txt}
python_bin=${PYTHON_BIN:-python}
cpu=${CPU:-timing}
cache=${CACHE:-l1l2}
tck_ps=${TCK_PS:-416}
[[ "$cpu" == timing || "$cpu" == o3 ]] || die 'CPU must be timing or o3.'
[[ "$cache" == l1l2 || "$cache" == none ]] || die 'CACHE must be l1l2 or none.'
# Anchor relative environment overrides before changing directory for gem5.
anchor() { [[ "$1" == /* ]] && printf '%s\n' "$1" || printf '%s/%s\n' "$PWD" "$1"; }
root=$(anchor "$root")
gem5=$(anchor "$gem5")
config=$(anchor "$config")
binary=$(anchor "$binary")
model=$(anchor "$model")
tokens=$(anchor "$tokens")
group=$(anchor "$output_dir")

if (( ! dry_run )); then
    [[ -d "$root/python/ramulator" ]] || die "Ramulator source not found: $root/python/ramulator"
    [[ -x "$gem5" ]] || die "gem5 executable not found: $gem5"
    [[ -x "$binary" ]] || die "ROI executable not found: $binary (build with WITH_M5=1)"
    for path in "$config" "$model" "$tokens"; do
        [[ -f "$path" ]] || die "Input file not found: $path"
    done
    command -v "$python_bin" >/dev/null || die "Python not found: $python_bin"
    if [[ -e "$group" ]]; then
        [[ -d "$group" ]] || die "Output path is not a directory: $group"
        shopt -s nullglob dotglob
        existing=("$group"/*)
        shopt -u nullglob dotglob
        ((${#existing[@]} == 0)) || die "Output directory is not empty; choose another --outdir: $group"
    else
        mkdir -p -- "$group"
    fi
fi

for policy in "${policies[@]}"; do
    if ((${#policies[@]} == 1)); then
        out=$group
    else
        out=$group/$policy
    fi
    cmd=("$gem5" "--outdir=$out" "$config"
        --ramulator-root "$root" --binary "$binary"
        --cpu "$cpu" --cache "$cache" --refresh "$policy" --expect-roi)
    if ((record_commands)); then
        cmd+=(--record-commands)
    fi
    cmd+=(-- --model "$model" --tokens "$tokens" --mode logits --roi)
    printf '\nPolicy: %s\nOutput: %s\n' "$policy" "$out"
    printf '%q ' "${cmd[@]}"
    printf '\n'
    if ((dry_run)); then
        continue
    fi

    mkdir -p -- "$out"
    {
        printf '#!/usr/bin/env bash\nset -euo pipefail\ncd -- %q\n' "$root"
        printf '%q ' "${cmd[@]}"
        printf '\n'
    } > "$out/command.sh"
    # pipefail propagates gem5 failures even when output is piped through tee.
    (cd -- "$root" && "${cmd[@]}") 2>&1 | tee "$out/terminal.log"

    # Check completion and the actual refresh implementation written by gem5.
    "$python_bin" - "$out" "$policy" <<'PY' | tee "$out/run_check.txt"
import configparser
import csv
import json
from pathlib import Path
import sys

directory, policy = Path(sys.argv[1]), sys.argv[2]
roi = json.loads((directory / "roi.json").read_text())
assert roi["ticks"] == roi["end_tick"] - roi["begin_tick"] > 0, "Invalid or incomplete ROI"
config = configparser.ConfigParser(interpolation=None)
with (directory / "config.ini").open() as handle:
    config.read_file(handle)
memory = json.loads(config["board.memory.ramulator2"]["ramulator_config"])["memory_system"]
controllers = memory["controllers"]
assert len(controllers) == 1, "Expected the single-controller starter"
actual = controllers[0]["refresh_manager"]["impl"]
expected = {"allbank": "AllBank", "off": "NoRefresh", "refsb": "DDR5SameBank"}[policy]
assert actual == expected, f"Wrong refresh manager: {actual}, expected {expected}"
with (directory / "command_counts.csv").open(newline="") as handle:
    counts = {r[0].strip(): int(r[1]) for r in csv.reader(handle) if len(r) == 2}
assert "REFab" in counts and "REFsb" in counts, "Refresh command counters are missing"
if policy != "refsb":
    assert counts["REFsb"] == 0, "Unexpected REFsb in allbank/off run"
if policy != "allbank":
    assert counts["REFab"] == 0, "REFab appeared in off/refsb run"
if policy == "refsb" and roi["seconds"] >= 0.00001:
    assert counts["REFsb"] > 0, "No REFsb appeared in a completed, sufficiently long ROI"
for name in ("stats.txt", "ramulator_stats.yaml"):
    assert (directory / name).is_file(), f"Missing result: {name}"
print(f"PASS: complete ROI; refresh manager={actual}")
print(f"ROI time: {roi['seconds'] * 1000:.9f} ms")
print(f"REFab: {counts['REFab']}; REFsb: {counts['REFsb']}")
PY
    summary=$script_dir/summarize_roi.py
    if [[ -f "$summary" ]]; then
        "$python_bin" "$summary" "$out" --tck-ps "$tck_ps" | tee "$out/summary.txt"
    fi
done

if (( ! dry_run )) && ((${#policies[@]} > 1)); then
    "$python_bin" - "$group" "${policies[@]}" <<'PY' | tee "$group/comparison.txt"
import json
from pathlib import Path
import sys

directory = Path(sys.argv[1])
times = {p: json.loads((directory / p / "roi.json").read_text())["seconds"]
         for p in sys.argv[2:]}
if not all(t > 0 for t in times.values()):
    raise ValueError("ROI times must be positive")
increase = (times["allbank"] / times["off"] - 1) * 100
result = {"roi_seconds": times, "allbank_time_increase_over_off_pct": increase}
print(f"AllBank ROI: {times['allbank'] * 1000:.9f} ms")
print(f"Off ROI:     {times['off'] * 1000:.9f} ms")
print(f"AllBank time increase over off: {increase:.6f}%")
if "refsb" in times:
    result["refsb_time_increase_over_off_pct"] = (times["refsb"] / times["off"] - 1) * 100
    result["refsb_time_reduction_vs_allbank_pct"] = (1 - times["refsb"] / times["allbank"]) * 100
    print(f"REFsb ROI:   {times['refsb'] * 1000:.9f} ms")
    print(f"REFsb time increase over off: {result['refsb_time_increase_over_off_pct']:.6f}%")
    print(f"REFsb time reduction vs allbank: {result['refsb_time_reduction_vs_allbank_pct']:.6f}%")
(directory / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
PY
fi

if ((dry_run)); then
    printf '\nDry run complete; gem5 was not started.\n'
else
    printf '\nCompleted. Results: %s\n' "$group"
fi
