"""GPT-2 CPU SE-mode configuration for Ramulator 2.1 + gem5 v25.1.

Run this with gem5.opt, not with the host's python interpreter.
The default /workspace layout assumes this directory is under the Ramulator root.
"""

import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--ramulator-root", type=Path, default=Path(__file__).resolve().parent.parent)
parser.add_argument("--binary", type=Path, required=True)
parser.add_argument("--cpu", choices=("timing", "o3"), default="timing")
parser.add_argument("--cache", choices=("none", "l1", "l1l2"), default="l1l2")
parser.add_argument("--l1d-prefetch", choices=("on", "off"), default="on")
parser.add_argument("--l2-prefetch", choices=("on", "off"), default="on")
parser.add_argument("--policy", choices=("open_rr", "cap_rr", "cap_rr_share", "cap_leastq", "cap_closed", "cap_aware"), required=True)
parser.add_argument("--cap", type=int, default=16)
parser.add_argument("--record-commands", action="store_true")
parser.add_argument("--expect-roi", action="store_true", help="reset at WORKBEGIN and stop at WORKEND")
parser.add_argument("workload_args", nargs=argparse.REMAINDER)
args = parser.parse_args()

root = args.ramulator_root.resolve()
binary = args.binary.resolve()
if not (root / "python" / "ramulator").is_dir():
    parser.error(f"Not a Ramulator source directory: {root}")
if not binary.is_file():
    parser.error(f"Workload binary not found: {binary}")
sys.path.insert(0, str(root / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import m5
import ramulator
from policies import components
from gem5.components.boards.simple_board import SimpleBoard
from cache_controls import make_cache_hierarchy
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource
from gem5.simulate.simulator import Simulator
from gem5.simulate.exit_event import ExitEvent

out = Path(m5.options.outdir).resolve()
out.mkdir(parents=True, exist_ok=True)
plugins = [
    ramulator.controller_plugin.CAPExperimentStats(path=str(out / "cap_metrics")),
    ramulator.controller_plugin.CommandCounter(
        commands_to_count=["ACT", "RD", "RDA", "WR", "WRA", "PREpb", "PREab", "PREsb", "REFab", "REFsb"],
        path=str(out / "command_counts.csv"),
    )
]
if args.record_commands:
    plugins.append(ramulator.controller_plugin.CmdTraceRecorder(path=str(out / "commands.csv")))

dram = ramulator.dram.DDR5(
    org_preset="DDR5_16Gb_x8", timing_preset="DDR5_4800AN", rank=1,
)
row_policy, refresh = components(ramulator, args.policy, args.cap)
ctrl = ramulator.controller.GenericDDR(
    dram=dram,
    scheduler=ramulator.scheduler.FRFCFS(),
    refresh_manager=refresh,
    row_policy=row_policy,
    addr_mapper=ramulator.addr_mapper.RoBaRaCoCh(),
    controller_plugins=plugins,
)
mem_sys = ramulator.memory_system.GenericDRAM(
    # The gem5 wrapper schedules memory ticks using DRAM tCK.
    # The standalone frontend/memory clock_ratio loop is not used here.
    clock_ratio=1,
    controllers=[ctrl],
    channel_mapper=ramulator.channel_mapper.CacheLineInterleave(),
)
# 16Gb x8 dies on a 32-bit-wide, one-rank channel model have 8 GiB capacity.
memory = ramulator.gem5.Memory(mem_sys, size="8GiB")
cache = make_cache_hierarchy(
    args.cache, l1d_prefetch=args.l1d_prefetch == "on", l2_prefetch=args.l2_prefetch == "on",
)
board = SimpleBoard(
    clk_freq="3GHz",
    processor=SimpleProcessor(
        cpu_type=CPUTypes.TIMING if args.cpu == "timing" else CPUTypes.O3,
        isa=ISA.X86,
        num_cores=1,
    ),
    memory=memory,
    cache_hierarchy=cache,
)
workload_args = args.workload_args
if workload_args[:1] == ["--"]:
    workload_args = workload_args[1:]
board.set_se_binary_workload(
    binary=BinaryResource(local_path=str(binary)),
    arguments=workload_args,
)
print(f"Workload: {binary}, args={workload_args}")
print(f"CPU={args.cpu}, cache={args.cache}, policy={args.policy}, CAP={args.cap}")
(out / "experiment.json").write_text(json.dumps({"policy": args.policy, "cap": args.cap, "cpu": args.cpu, "cache": args.cache, "l1d_prefetch": args.l1d_prefetch, "l2_prefetch": args.l2_prefetch, "binary": str(binary), "workload_args": workload_args}, indent=2) + "\n")
print(f"Prefetch: L1D={args.l1d_prefetch if args.cache != 'none' else 'N/A'}, "
      f"L2={args.l2_prefetch if args.cache == 'l1l2' else 'N/A'}; L1I keeps the stock setting")
roi = {}
def roi_begin():
    while True:
        if "begin_tick" in roi:
            raise RuntimeError("This config expects one ROI per simulation")
        roi["begin_tick"] = int(m5.curTick())
        m5.stats.reset()
        print(f"ROI begins at gem5 tick {roi['begin_tick']}")
        yield False

def roi_end():
    while True:
        if "begin_tick" not in roi:
            raise RuntimeError("WORKEND occurred before WORKBEGIN")
        roi["end_tick"] = int(m5.curTick())
        roi["ticks"] = roi["end_tick"] - roi["begin_tick"]
        roi["seconds"] = roi["ticks"] / 1e12
        m5.stats.dump()
        (out / "roi.json").write_text(json.dumps(roi, indent=2) + "\n")
        print(f"ROI ends at gem5 tick {roi['end_tick']}; stopping before output/file cleanup")
        yield True

handlers = {ExitEvent.WORKBEGIN: roi_begin(), ExitEvent.WORKEND: roi_end()}
Simulator(board=board, on_exit_event=handlers).run()
if args.expect_roi and "end_tick" not in roi:
    raise RuntimeError("Expected an ROI, but none completed: build WITH_M5=1 and pass workload --roi")
print(f"Output directory: {out}")
if not roi:
    print("Whole-program run: initialization and output are included.")
