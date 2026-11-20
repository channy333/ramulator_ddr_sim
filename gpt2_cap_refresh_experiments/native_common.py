"""Helpers for the real C++ controller harness (no gem5 dependency)."""
import sys
from pathlib import Path

def bootstrap(root):
    root = Path(root).resolve()
    sys.path[:0] = [str(root / 'python'), str(root)]
    return root

def make(policy='cap_aware', cap=16, path=None, rank=1, refresh=None, row=None):
    import ramulator
    from policies import components
    from tests.controller_scheduling.harness import ControllerUnderTest
    default_row, default_refresh = components(ramulator, policy, cap)
    plugins = [] if path is None else [ramulator.controller_plugin.CAPExperimentStats(path=str(path))]
    return ControllerUnderTest.make_generic_ddr(
        ramulator.dram.DDR5(org_preset='DDR5_16Gb_x8', timing_preset='DDR5_4800AN', rank=rank),
        row_policy=row or default_row, refresh_manager=refresh or default_refresh, controller_plugins=plugins)

def addr(d, bank=0, bg=0, row=3, rank=0, column=0):
    return d.addr_vec(Channel=0, Rank=rank, BankGroup=bg, Bank=bank, Row=row, Column=column)

def tick_to(d, clock):
    # Drivers call tick_to from clock zero or after this helper; real clock is
    # tracked here because an idle controller can issue no commands for a while.
    now = getattr(d, '_driver_clock', 0)
    for _ in range(now, clock):
        d.tick()
    d._driver_clock = clock

def issued_refs(d):
    return [h for h in d.history if h.command == 'REFsb']

def signature(d):
    return [(h.clk, h.command, h.addr_vec, h.type_id) for h in d.history]

def open_all(d):
    for bg in range(8):
        for bank in range(4):
            d.send_request('Read', addr(d, bank, bg))
    tick_to(d, 900)

def flat_stats(stats):
    from analyze import flatten
    return {k.split('.')[-1]: v for k, v in flatten(stats).items()}
