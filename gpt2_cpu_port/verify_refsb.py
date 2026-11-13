#!/usr/bin/env python3
"""Short real-controller smoke check; no GPT-2 weights or pytest required."""

from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root / "python"), str(root)]

import ramulator
from tests.controller_scheduling.harness import ControllerUnderTest


dram = ramulator.dram.DDR5(org_preset="DDR5_16Gb_x8", timing_preset="DDR5_4800AN")
dut = ControllerUnderTest.make_generic_ddr(
    dram, refresh_manager=ramulator.refresh_manager.DDR5SameBank()
)
history = []
for _ in range(dut.timing("nREFI")):
    history.extend(dut.tick())
refs = [x for x in history if x.command == "REFsb"]
bank = dut.level_names.index("Bank")
assert len(refs) == 8, f"Expected 8 REFsb per normal interval; observed {len(refs)}"
assert [x.addr_vec[bank] for x in refs] == [0, 1, 2, 3, 0, 1, 2, 3]
assert not any(x.command == "REFab" for x in history)
print("PASS: DDR5SameBank issues REFsb through the actual C++ controller")
print("PASS: FGR cadence and bank-ID coverage: 0,1,2,3,0,1,2,3")
print("REFsb clocks:", [x.clk for x in refs])
print("Ready for the gem5 --refresh refsb experiment.")
