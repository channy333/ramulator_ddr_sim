#!/usr/bin/env python3
"""Summarize one ROI from the supplied GPT-2 gem5 + Ramulator starter.

Usage: python summarize_roi.py /workspace/results/gpt2_block1_roi --tck-ps 416
Requires PyYAML (already a dependency of the Ramulator Python environment).
Single CPU core and single DRAM controller only. Does not sum statistics dumps.
"""

import argparse
import csv
import json
import math
from pathlib import Path
import sys


def parse_stats(path):
    blocks = []
    current = None
    for line in path.read_text().splitlines():
        if "Begin Simulation Statistics" in line:
            current = {}
        elif "End Simulation Statistics" in line:
            if current is not None:
                blocks.append(current)
            current = None
        elif current is not None:
            fields = line.split("#", 1)[0].split()
            if len(fields) >= 2:
                try:
                    current[fields[0]] = float(fields[1])
                except ValueError:
                    pass
    if not blocks:
        raise ValueError(f"No complete statistics dump in {path}")
    return blocks


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def emit(label, value, unit="", digits=6):
    formatted = f"{value:,.{digits}f}" if finite(value) else "unavailable"
    print(f"{label:34s} {formatted}{(' ' + unit) if unit else ''}")


def summarize(directory, tck_ps):
    import yaml

    roi = json.loads((directory / "roi.json").read_text())
    begin, end, ticks = (int(roi[k]) for k in ("begin_tick", "end_tick", "ticks"))
    if ticks <= 0 or end - begin != ticks:
        raise ValueError("roi.json has inconsistent begin/end/ticks")
    seconds = float(roi["seconds"])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("roi.json seconds must be finite and positive")

    blocks = parse_stats(directory / "stats.txt")
    matches = [(i, b) for i, b in enumerate(blocks)
               if b.get("finalTick") == end and b.get("simTicks") == ticks]
    if not matches:
        raise ValueError("No dump matches BOTH ROI end_tick and ticks; check resetStats and result paths")
    index, stats = matches[0]
    frequency = stats.get("simFreq")
    if not finite(frequency) or frequency <= 0:
        raise ValueError("Matching stats dump has no valid simFreq")
    if not math.isclose(ticks / frequency, seconds, rel_tol=1e-10, abs_tol=1 / frequency):
        raise ValueError("roi.json seconds disagrees with stats simFreq and ROI ticks")

    snapshot = directory / f"ramulator_stats.{end}.yaml"
    ram_path = snapshot if snapshot.is_file() else directory / "ramulator_stats.yaml"
    memory = yaml.safe_load(ram_path.read_text())["memory_system"]
    ctrl = memory.get("controller", memory.get("controllers"))
    if isinstance(ctrl, list):
        if len(ctrl) != 1:
            raise ValueError("This starter summary supports exactly one DRAM controller")
        ctrl = ctrl[0]
    if not isinstance(ctrl, dict):
        raise ValueError("Cannot locate a single Ramulator controller")

    cpu = "board.processor.cores.core."
    cpu_cycle_keys = [k for k in stats if k.startswith("board.processor.") and k.endswith(".numCycles")]
    if cpu_cycle_keys != [cpu + "numCycles"]:
        raise ValueError("This starter summary supports one core at board.processor.cores.core")
    dram_cycles = ctrl.get("cycles")
    if not finite(dram_cycles):
        raise ValueError("No Ramulator measured cycles statistic")
    dram_seconds = dram_cycles * tck_ps * 1e-12
    if abs(dram_seconds - seconds) > max(2 * tck_ps * 1e-12, seconds * 1e-9):
        raise ValueError(
            f"Ramulator cycles imply {dram_seconds:.12g} s, ROI is {seconds:.12g} s. "
            "Check --tck-ps, YAML snapshot, and Ramulator resetStats support."
        )

    print(f"Result directory: {directory.resolve()}")
    print(f"gem5 dump: {index + 1}/{len(blocks)}; matching end_tick={end}")
    print(f"Ramulator file: {ram_path.name}; DRAM period: {tck_ps:g} ps")
    print("PASS: ROI ticks match gem5 dump and Ramulator measured cycles")
    emit("ROI execution time", seconds * 1e3, "ms")
    emit("CPU cycles", stats.get(cpu + "numCycles"), "cycles", 0)
    emit("Committed instructions", stats.get("simInsts"), "instructions", 0)
    emit("IPC", stats.get(cpu + "ipc"), "instructions/cycle")
    for label, name in (("L1D", "l1d"), ("L1I", "l1i"), ("L2", "l2")):
        rate = stats.get(f"board.cache_hierarchy.{name}-cache-0.overallMissRate::total")
        emit(f"{label} overall miss rate", rate * 100 if finite(rate) else None, "%")
    emit("DRAM measured cycles", dram_cycles, "cycles", 0)
    emit("DRAM reads served", ctrl.get("num_read_reqs_served"), "requests", 0)
    emit("DRAM writes served", ctrl.get("num_write_reqs_served"), "requests", 0)
    latency = ctrl.get("avg_read_latency")
    emit("DRAM mean read latency", latency, "DRAM cycles")
    emit("DRAM mean read latency", latency * tck_ps / 1000 if finite(latency) else None, "ns")
    rows = [ctrl.get(k) for k in ("row_hits", "row_misses", "row_conflicts")]
    hit_rate = 100 * rows[0] / sum(rows) if all(finite(x) for x in rows) and sum(rows) > 0 else None
    emit("DRAM row hit rate (read+write)", hit_rate, "%")
    emit("DRAM mean read queue length", ctrl.get("read_queue_len_avg"), "requests")
    emit("DRAM transaction bandwidth", ctrl.get("total_throughput_MBps"), "MB/s")
    prefix = "board.memory.ramulator2."
    rd_bytes, wr_bytes = (stats.get(prefix + n + "::total") for n in ("bytesRead", "bytesWritten"))
    emit("gem5 memory bytes read", rd_bytes, "bytes", 0)
    emit("gem5 memory bytes written", wr_bytes, "bytes", 0)
    bandwidth = (rd_bytes + wr_bytes) / seconds / 1e6 if finite(rd_bytes) and finite(wr_bytes) else None
    emit("gem5 memory payload bandwidth", bandwidth, "MB/s")
    read_total = stats.get(prefix + "numReads::total")
    prefetch = sum(v for k, v in stats.items() if k.startswith(prefix + "numReads::") and k.endswith(".prefetcher"))
    emit("Prefetch share of DRAM reads", 100 * prefetch / read_total if finite(read_total) and read_total else None, "%")

    counts_path = directory / "command_counts.csv"
    if counts_path.is_file():
        counts = {}
        with counts_path.open(newline="") as handle:
            for row in csv.reader(handle):
                if len(row) == 2:
                    try:
                        counts[row[0].strip()] = int(row[1])
                    except ValueError:
                        pass
        for command in ("ACT", "RD", "WR", "PREpb", "PREab", "PREsb", "REFab", "REFsb"):
            emit(f"Command {command}", counts.get(command), "commands", 0)
    else:
        print("Command counts: unavailable (command_counts.csv missing)")
    print("MB/s uses decimal MB (1,000,000 bytes).")
    print("CPU and DRAM cycles use different clock periods; hostSeconds is simulator wall time.")
    print("ROI reset preserves cache contents, open rows, and refresh phase.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("result_directory", type=Path)
    parser.add_argument("--tck-ps", type=float, required=True, help="Actual DRAM clock period; current DDR5 preset uses 416")
    args = parser.parse_args()
    if not math.isfinite(args.tck_ps) or args.tck_ps <= 0:
        parser.error("--tck-ps must be finite and positive")
    try:
        summarize(args.result_directory, args.tck_ps)
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
