#!/usr/bin/env bash
# Full GPT-2 forward ROI runner for the existing gem5_gpt2_se.py starter.
set -Eeuo pipefail

usage() {
    cat <<'HELP'
Usage: bash run_gpt2_cache.sh MODE --outdir DIRECTORY [OPTIONS]

  off                 Run full GPT-2 with refresh disabled.
  allbank             Run full GPT-2 with AllBank / REFab refresh.
  refsb               Run full GPT-2 with the supplied DDR5SameBank manager.
  both                Run allbank, then off, with the same workload (default).
  pair                Run off, then refsb (prefetch experiment pair).
  all                 Run allbank, off, then refsb, with the same workload.
  --outdir DIRECTORY  Choose the output directory; no timestamp is appended.
  --dry-run           Print commands without starting gem5 or creating results.
  --record-commands   Also record the command trace; can create large files.
  --cache MODE        l1l2 (default), l1 (no L2), none (no L1 or L2).
  --l1d-prefetch MODE  on (default) or off; L1 data prefetcher only.
  --l2-prefetch MODE   on (default) or off; L2 prefetcher only.

Unzip the gpt2_cache_experiments directory under /workspace.
The existing gpt2_cpu_port binary, model, and REFsb manager are reused.
Single mode: files are written directly inside DIRECTORY.
both/all: files are written inside DIRECTORY/<policy>/.
An existing nonempty output directory is rejected to preserve previous results.

Optional environment overrides:
  RAMULATOR_ROOT  GEM5_BIN  GEM5_CONFIG  GPT2_BINARY  MODEL  TOKENS
  OUTDIR  PYTHON_BIN  CPU  CACHE  TCK_PS  L1D_PREFETCH  L2_PREFETCH

Examples:
  bash run_gpt2_cache.sh off --outdir /workspace/results/gpt2_noPF_off --l1d-prefetch off --l2-prefetch off
  bash run_gpt2_cache.sh refsb --outdir /workspace/results/gpt2_noPF_refsb --l1d-prefetch off --l2-prefetch off
  bash run_gpt2_cache.sh both --outdir /workspace/results/noPF_compare --l1d-prefetch off --l2-prefetch off
  bash run_gpt2_cache.sh off --cache none --outdir /workspace/results/no_cache

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
cache=${CACHE:-l1l2}
l1d_prefetch=${L1D_PREFETCH:-on}
l2_prefetch=${L2_PREFETCH:-on}
while (($#)); do
    case "$1" in
        --dry-run) dry_run=1 ;;
        --record-commands) record_commands=1 ;;
        --outdir)
            (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || die '--outdir needs a directory.'
            output_dir=$2
            shift ;;
        --outdir=*) output_dir=${1#--outdir=} ;;
        --cache|--l1d-prefetch|--l2-prefetch)
            (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || die "$1 needs a value."
            case "$1" in
                --cache) cache=$2 ;;
                --l1d-prefetch) l1d_prefetch=$2 ;;
                --l2-prefetch) l2_prefetch=$2 ;;
            esac
            shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "Unknown option: $1 (use --help)" ;;
    esac
    shift
done
case "$mode" in
    off|allbank|refsb) policies=("$mode") ;;
    both) policies=(allbank off) ;;
    pair) policies=(off refsb) ;;
    all) policies=(allbank off refsb) ;;
    -h) usage; exit 0 ;;
    *) die "Unknown mode: $mode (use off, allbank, refsb, both, pair, or all)" ;;
esac
[[ -n "$output_dir" ]] || die 'Specify --outdir DIRECTORY (or set OUTDIR).'

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=${RAMULATOR_ROOT:-$(cd -- "$script_dir/.." && pwd)}
gem5=${GEM5_BIN:-$root/tools/gem5/build/X86/gem5.opt}
config=${GEM5_CONFIG:-$script_dir/gem5_gpt2_cache_se.py}
binary=${GPT2_BINARY:-$root/gpt2_cpu_port/bin/gpt2_cpu_m5}
model=${MODEL:-$root/workloads/gpt2/models/gpt2_cpu.bin}
tokens=${TOKENS:-$root/workloads/gpt2/models/tokens.txt}
python_bin=${PYTHON_BIN:-python}
cpu=${CPU:-timing}
tck_ps=${TCK_PS:-416}
[[ "$cpu" == timing || "$cpu" == o3 ]] || die 'CPU must be timing or o3.'
[[ "$cache" == l1l2 || "$cache" == l1 || "$cache" == none ]] || die 'CACHE must be l1l2, l1, or none.'
[[ "$l1d_prefetch" == on || "$l1d_prefetch" == off ]] || die 'L1D prefetch must be on or off.'
[[ "$l2_prefetch" == on || "$l2_prefetch" == off ]] || die 'L2 prefetch must be on or off.'
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
        --cpu "$cpu" --cache "$cache" --l1d-prefetch "$l1d_prefetch"
        --l2-prefetch "$l2_prefetch" --refresh "$policy" --expect-roi)
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
    "$python_bin" - "$out" "$policy" "$cache" "$l1d_prefetch" "$l2_prefetch" <<'PY' | tee "$out/run_check.txt"
import configparser
import csv
import json
from pathlib import Path
import sys

directory, policy = Path(sys.argv[1]), sys.argv[2]
cache_mode, l1d_prefetch, l2_prefetch = sys.argv[3:]
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
cache_prefix = "board.cache_hierarchy."
present = {}
for level in ("l1i", "l1d", "l2"):
    sections = [name for name in config.sections()
                if name.startswith(cache_prefix + level + "-cache-")
                and name[len(cache_prefix):].count(".") == 0]
    # L1-only upstream hierarchy stores caches in lists.
    if not sections:
        sections = [name for name in config.sections()
                    if name.startswith(cache_prefix + level + "caches")
                    and name[len(cache_prefix):].count(".") == 0]
    present[level] = sections
    expected_present = cache_mode != "none" if level != "l2" else cache_mode == "l1l2"
    assert bool(sections) == expected_present, f"Unexpected presence of {level} caches"
    if level in ("l1d", "l2") and sections:
        enabled = (l1d_prefetch if level == "l1d" else l2_prefetch) == "on"
        for section in sections:
            link = config[section].get("prefetcher", "Null")
            actual_enabled = link.lower() not in ("null", "none", "")
            assert actual_enabled == enabled, f"Wrong prefetch setting at {section}: {link}"
            print(f"PASS: {level} prefetch={'on' if actual_enabled else 'off'}")

# Select the ROI dump rather than adding repeated dumps.
blocks, block = [], None
for line in (directory / "stats.txt").read_text().splitlines():
    if "Begin Simulation Statistics" in line:
        block = {}
    elif "End Simulation Statistics" in line:
        if block is not None:
            blocks.append(block)
        block = None
    elif block is not None:
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2:
            try:
                block[fields[0]] = float(fields[1])
            except ValueError:
                pass
matching = [b for b in blocks if b.get("finalTick") == roi["end_tick"] and b.get("simTicks") == roi["ticks"]]
assert matching, "No gem5 dump matches this ROI"
stats = matching[0]
tick_to_ns = 1e9 / stats["simFreq"]
for level in ("l1d", "l2"):
    if not present[level]:
        continue
    prefix = present[level][0] + "."
    print(f"{level} demand misses: {stats.get(prefix + 'demandMisses::total', 'unavailable')}")
    avg = stats.get(prefix + "demandAvgMissLatency::total")
    if avg is not None:
        print(f"{level} mean demand miss latency: {avg * tick_to_ns:.9f} ns")
    latency = stats.get(prefix + "demandMissLatency::total")
    if latency is not None:
        print(f"{level} total demand miss latency: {latency / stats['simFreq'] * 1000:.9f} ms")
    for key in ("pfIssued", "pfUseful"):
        print(f"{level} {key}: {stats.get(prefix + 'prefetcher.' + key, 0)}")
PY
    summary=$root/gpt2_cpu_port/summarize_roi.py
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
result = {"roi_seconds": times}
print(f"Off ROI:     {times['off'] * 1000:.9f} ms")
if "allbank" in times:
    increase = (times["allbank"] / times["off"] - 1) * 100
    result["allbank_time_increase_over_off_pct"] = increase
    print(f"AllBank ROI: {times['allbank'] * 1000:.9f} ms")
    print(f"AllBank time increase over off: {increase:.6f}%")
if "refsb" in times:
    result["refsb_time_increase_over_off_pct"] = (times["refsb"] / times["off"] - 1) * 100
    result["refsb_minus_off_ms"] = (times["refsb"] - times["off"]) * 1000
    print(f"REFsb ROI:   {times['refsb'] * 1000:.9f} ms")
    print(f"REFsb time increase over off: {result['refsb_time_increase_over_off_pct']:.6f}%")
    print(f"REFsb minus off: {result['refsb_minus_off_ms']:.9f} ms")
    if "allbank" in times:
        result["refsb_time_reduction_vs_allbank_pct"] = (1 - times["refsb"] / times["allbank"]) * 100
        print(f"REFsb time reduction vs allbank: {result['refsb_time_reduction_vs_allbank_pct']:.6f}%")
(directory / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
PY
fi

if ((dry_run)); then
    printf '\nDry run complete; gem5 was not started.\n'
else
    printf '\nCompleted. Results: %s\n' "$group"
fi
