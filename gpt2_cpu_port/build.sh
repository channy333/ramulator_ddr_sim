#!/usr/bin/env bash
set -euo pipefail
port_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$port_dir/bin"
flags=(-O2 -std=c11 -Wall -Wextra -Wpedantic -static -march=x86-64 -mtune=generic
       -fno-tree-vectorize -ffp-contract=off)
extras=()
output="$port_dir/bin/gpt2_cpu"
if [[ ${WITH_M5:-0} == 1 ]]; then
    gem5_root=${GEM5_ROOT:-/workspace/tools/gem5}
    m5_library="$gem5_root/util/m5/build/x86/out/libm5.a"
    if [[ ! -f $m5_library ]]; then
        printf 'Build libm5.a first: cd %s/util/m5 && scons build/x86/out/libm5.a\n' "$gem5_root" >&2
        exit 1
    fi
    flags+=(-DGPT2_USE_M5 -I"$gem5_root/include")
    extras+=("$m5_library")
    output="$port_dir/bin/gpt2_cpu_m5"
fi
"${CC:-gcc}" "${flags[@]}" "$port_dir/gpt2_cpu_reference.c" "${extras[@]}" -lm -o "$output"
printf 'Built %s\n' "$output"
