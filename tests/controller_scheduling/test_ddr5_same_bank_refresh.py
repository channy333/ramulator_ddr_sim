"""Integration tests for the added manager and existing DDR5 command model."""

import pytest

import ramulator
from tests.controller_scheduling.harness import ControllerUnderTest


pytestmark = pytest.mark.controller_scheduling


def make_dut(**overrides):
    dram = ramulator.dram.DDR5(
        org_preset="DDR5_16Gb_x8", timing_preset="DDR5_4800AN", **overrides
    )
    return ControllerUnderTest.make_generic_ddr(
        dram, refresh_manager=ramulator.refresh_manager.DDR5SameBank()
    )


def tick_until(dut, count, limit=22000):
    found = []
    for _ in range(limit):
        found.extend(item for item in dut.tick() if item.command == "REFsb")
        if len(found) >= count:
            return found
    raise AssertionError(f"Only observed {len(found)}/{count} REFsb commands")


def test_fgr_rate_and_round_robin_cover_every_bank_id():
    dut = make_dut()
    # Normal REFab interval is 3.9 us. FGR needs eight REFsb commands in it,
    # not four: each of the four bank IDs is refreshed twice per interval.
    issued = []
    for _ in range(dut.timing("nREFI")):
        issued.extend(dut.tick())
    refs = [x for x in issued if x.command == "REFsb"]
    assert len(refs) == 8
    bank = dut.level_names.index("Bank")
    assert [x.addr_vec[bank] for x in refs] == [0, 1, 2, 3, 0, 1, 2, 3]
    assert [x.clk for x in refs] == [1172, 2344, 3516, 4688, 5860, 7032, 8204, 9375]
    assert not any(x.command == "REFab" for x in issued)


def test_fractional_deadlines_do_not_drift():
    dut = make_dut()
    refs = tick_until(dut, 128, limit=150000)
    assert refs[-1].clk == 150000  # 16 normal intervals, each 9375 nCK.
    assert {refs[i + 1].clk - refs[i].clk for i in range(len(refs) - 1)} == {1171, 1172}


def test_every_rank_receives_a_complete_bank_id_round():
    dut = make_dut(rank=2)
    refs = tick_until(dut, 16)
    rank = dut.level_names.index("Rank")
    bank = dut.level_names.index("Bank")
    for target_rank in (0, 1):
        sequence = [x.addr_vec[bank] for x in refs if x.addr_vec[rank] == target_rank]
        assert sequence == [0, 1, 2, 3, 0, 1, 2, 3]


def test_non_target_bank_can_activate_during_target_refresh():
    dut = make_dut()
    ref = tick_until(dut, 1)[0]
    dut.send_request("Read", dut.addr_vec(Channel=0, Rank=0, BankGroup=0, Bank=0, Row=1, Column=0))
    dut.send_request("Read", dut.addr_vec(Channel=0, Rank=0, BankGroup=0, Bank=1, Row=1, Column=0))
    history = []
    for _ in range(600):
        history.extend(dut.tick())
    bank = dut.level_names.index("Bank")
    acts = {x.addr_vec[bank]: x.clk for x in history if x.command == "ACT"}
    # IssuedCommand records the first CA cycle. The DDR5 model applies these
    # constraints at the second cycle of a two-cycle ACT command.
    act_cycles = dut.dram.command_cycles["ACT"]
    assert ref.clk + dut.timing("nREFSBRD") <= acts[1] + act_cycles - 1
    assert acts[1] < ref.clk + dut.timing("nRFCsb")
    assert acts[0] + act_cycles - 1 >= ref.clk + dut.timing("nRFCsb")


def test_open_target_in_another_bank_group_requires_presb():
    dut = make_dut()
    dut.send_request("Read", dut.addr_vec(Channel=0, Rank=0, BankGroup=7, Bank=0, Row=3, Column=0))
    dut.run_until_idle()
    refs = tick_until(dut, 1)
    pres = [x for x in dut.history if x.command == "PREsb"]
    assert len(pres) == 1
    assert refs[0].clk - pres[0].clk >= dut.timing("nRP")
    # PREsb from representative BG0 must also close Bank0 in BG7.
    dut.send_request("Read", dut.addr_vec(Channel=0, Rank=0, BankGroup=7, Bank=0, Row=3, Column=0))
    start = len(dut.history)
    for _ in range(600):
        dut.tick()
    assert any(x.command == "ACT" for x in dut.history[start:])


def test_roi_reset_preserves_refresh_phase_and_bank_rotation():
    dut = make_dut()
    tick_until(dut, 5)
    dut.reset_stats()
    refs = tick_until(dut, 3)
    bank = dut.level_names.index("Bank")
    assert [x.addr_vec[bank] for x in refs] == [1, 2, 3]
    assert refs[-1].clk == 9375


def test_non_ddr5_is_rejected():
    with pytest.raises(RuntimeError, match="requires the DDR5"):
        ControllerUnderTest.make_generic_ddr(
            ramulator.dram.DDR4(org_preset="DDR4_8Gb_x8", timing_preset="DDR4_2400R"),
            refresh_manager=ramulator.refresh_manager.DDR5SameBank(),
        )
