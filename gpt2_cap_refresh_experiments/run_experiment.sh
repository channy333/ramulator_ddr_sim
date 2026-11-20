#!/usr/bin/env bash
set -Eeuo pipefail
package=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=${RAMULATOR_ROOT:-$(cd -- "$package/.." && pwd)}
root=$(cd -- "$root" && pwd)
outdir=; workload=full; prefetch=l1; policy=all; cap=16; dry_run=0; resume=0
while (($#)); do
  case "$1" in
    --outdir|--workload|--prefetch|--policy|--cap)
      (($# >= 2)) || { printf 'Missing value for %s\n' "$1" >&2; exit 1; }
      case "$1" in
        --outdir) outdir=$2;; --workload) workload=$2;; --prefetch) prefetch=$2;; --policy) policy=$2;; --cap) cap=$2;;
      esac
      shift;;
    --dry-run) dry_run=1;;
    --resume) resume=1;;
    --help|-h)
      printf '%s\n' 'Usage: bash run_experiment.sh --outdir DIR [--policy all|open_rr|cap_rr|cap_rr_share|cap_leastq|cap_closed|cap_aware] [--cap 16] [--workload tiny|block1|full] [--prefetch l1|both|l2|none] [--resume] [--dry-run]'
      exit 0;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 1;;
  esac
  shift
done
[[ -n "$outdir" ]] || { printf 'Specify --outdir DIR.\n' >&2; exit 1; }
[[ "$cap" =~ ^[1-9][0-9]*$ ]] || { printf 'CAP must be a positive integer.\n' >&2; exit 1; }
case "$policy" in
  all) policies=(open_rr cap_rr cap_rr_share cap_leastq cap_closed cap_aware);;
  open_rr|cap_rr|cap_rr_share|cap_leastq|cap_closed|cap_aware) policies=("$policy");;
  *) printf 'Invalid --policy.\n' >&2; exit 1;;
esac
case "$prefetch" in
  l1) l1=on; l2=off;; both) l1=on; l2=on;; l2) l1=off; l2=on;; none) l1=off; l2=off;;
  *) printf 'Invalid --prefetch.\n' >&2; exit 1;;
esac
case "$workload" in
  tiny) model="$root/gpt2_cpu_port/tiny_fixture/tiny.bin"; tokens="$root/gpt2_cpu_port/tiny_fixture/tokens.txt"; mode=(--mode logits);;
  block1) model="$root/workloads/gpt2/models/gpt2_cpu.bin"; tokens="$root/workloads/gpt2/models/tokens.txt"; mode=(--mode hidden --blocks 1);;
  full) model="$root/workloads/gpt2/models/gpt2_cpu.bin"; tokens="$root/workloads/gpt2/models/tokens.txt"; mode=(--mode logits);;
  *) printf 'Invalid --workload.\n' >&2; exit 1;;
esac
anchor() { [[ "$1" == /* ]] && printf '%s\n' "$1" || printf '%s/%s\n' "$PWD" "$1"; }
root=$(anchor "$root"); outdir=$(anchor "$outdir")
gem5=${GEM5_BIN:-$root/tools/gem5/build/X86/gem5.opt}
binary=${GPT2_BINARY:-$root/gpt2_cpu_port/bin/gpt2_cpu_m5}
python_bin=${PYTHON_BIN:-python}
if ((!dry_run)); then
  [[ -x "$gem5" && -x "$binary" && -f "$model" && -f "$tokens" ]] || { printf 'Missing gem5/binary/model/tokens.\n' >&2; exit 1; }
  "$python_bin" -c 'import yaml' || { printf 'Install PyYAML in this Python environment.\n' >&2; exit 1; }
  if [[ -e "$outdir" ]]; then
    [[ -d "$outdir" ]] || { printf 'Output path is not a directory.\n' >&2; exit 1; }
    shopt -s nullglob dotglob; contents=("$outdir"/*); shopt -u nullglob dotglob
    if ((${#contents[@]} != 0 && !resume)); then
      printf 'Output directory is not empty; choose another --outdir or use --resume.\n' >&2; exit 1
    fi
  else mkdir -p -- "$outdir"; fi
fi
cd -- "$root"
if ((resume && !dry_run)); then
  # Check every existing condition before launching any new simulation.
  for selected in "${policies[@]}"; do
    target=$outdir
    [[ "$policy" != all ]] || target="$outdir/$selected"
    "$python_bin" "$package/check_resume.py" "$target" --policy "$selected" --cap "$cap" \
      --binary "$binary" --l1d-prefetch "$l1" --l2-prefetch "$l2" -- \
      --model "$model" --tokens "$tokens" "${mode[@]}" --roi
  done
fi
for selected in "${policies[@]}"; do
  target=$outdir
  [[ "$policy" != all ]] || target="$outdir/$selected"
  command=("$gem5" "--outdir=$target" "$package/gem5_cap_se.py"
    --ramulator-root "$root" --binary "$binary" --cpu timing --cache l1l2
    --l1d-prefetch "$l1" --l2-prefetch "$l2" --policy "$selected" --cap "$cap" --expect-roi --
    --model "$model" --tokens "$tokens" "${mode[@]}" --roi)
  printf '%q ' "${command[@]}"; printf '\n'
  if ((dry_run)); then continue; fi
  if ((resume)) && [[ -f "$target/roi.json" ]]; then
    printf 'Reusing completed ROI; gem5 is skipped: %s\n' "$target"
  else
    mkdir -p -- "$target"
    { printf '#!/usr/bin/env bash\nset -Eeuo pipefail\ncd %q\n' "$root"; printf '%q ' "${command[@]}"; printf '\n'; } > "$target/command.sh"
    "${command[@]}" 2>&1 | tee "$target/terminal.log"
  fi
  "$python_bin" "$package/summarize_roi.py" "$target" --tck-ps 416 | tee "$target/summary.txt"
  "$python_bin" "$package/analyze.py" "$target"
done
if ((!dry_run)); then
  "$python_bin" "$package/analyze.py" "$outdir"
  printf '\nCompleted: %s\n' "$outdir"
fi
