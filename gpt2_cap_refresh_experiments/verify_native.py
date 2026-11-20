#!/usr/bin/env python3
"""Exercise compiled C++ scheduling, refresh cadence, CAP equivalence and ROI reset."""
import argparse
import csv
import json
import tempfile
import unittest
from pathlib import Path
from native_common import bootstrap, make, addr, tick_to, issued_refs, signature, open_all, flat_stats

class NativeTests(unittest.TestCase):
    def test_rr_matches_existing_manager_with_traffic(self):
        import ramulator
        if not hasattr(ramulator.refresh_manager, 'DDR5SameBank'):
            self.skipTest('Prior DDR5SameBank manager is not installed; exact cadence tested separately')
        ds = [make('open_rr'), make('open_rr', refresh=ramulator.refresh_manager.DDR5SameBank())]
        for d in ds:
            for t in range(1, 20001):
                if t % 83 == 1:
                    d.send_request('Read', addr(d, (t // 83) % 4, (t // 332) % 8, (t // 2656) % 3))
                d.tick()
        self.assertEqual(signature(ds[0]), signature(ds[1]))

    def test_cap_copy_matches_stock_including_auto_precharge(self):
        import ramulator
        for cap in (1, 4, 16):
            with self.subTest(cap=cap):
                ds = [make('cap_rr', cap, refresh=ramulator.refresh_manager.NoRefresh()),
                      make('cap_rr', cap, refresh=ramulator.refresh_manager.NoRefresh(), row=ramulator.row_policy.ClosedCAP(cap=cap))]
                for d in ds:
                    for t in range(1, 7001):
                        if t % 20 == 1:
                            d.send_request('Read', addr(d, bank=(t // 600) % 4, bg=(t // 1200) % 8))
                        d.tick()
                self.assertEqual(signature(ds[0]), signature(ds[1]))

    def test_cadence_rounds_and_multiple_ranks(self):
        from policies import POLICIES
        for policy in POLICIES:
            with self.subTest(policy=policy):
                d = make(policy, rank=2)
                tick_to(d, 18760)  # Rank 1's last command occupies the following CA slot.
                rank = d.level_names.index('Rank'); bank = d.level_names.index('Bank')
                for r in range(2):
                    refs = [h for h in issued_refs(d) if h.addr_vec[rank] == r]
                    self.assertEqual(len(refs), 16)
                    self.assertEqual([h.clk - r for h in refs], [(i * 9375 + 7) // 8 for i in range(1, 17)])
                    for i in range(0, 16, 4):
                        self.assertEqual({h.addr_vec[bank] for h in refs[i:i+4]}, set(range(4)))

    def test_closed_and_leastq_select_group_instead_of_single_bank(self):
        # 3 groups have one open bank; bank-ID 3 is entirely closed.
        for policy in ('cap_closed', 'cap_leastq'):
            d = make(policy, cap=100)
            for b in range(3): d.send_request('Read', addr(d, b))
            tick_to(d, 1171)
            if policy == 'cap_leastq':
                for b in range(3): d.send_request('Read', addr(d, b, column=64))
            tick_to(d, 1600)
            self.assertEqual(issued_refs(d)[0].addr_vec[d.level_names.index('Bank')], 3)

    def test_cap_ready_opportunity_and_pre_sharing(self):
        stats = {}
        for policy in ('cap_rr', 'cap_rr_share', 'cap_closed', 'cap_aware'):
            d = make(policy, cap=2)
            open_all(d)
            tick_to(d, 1152)
            d.send_request('Read', addr(d, 2, column=64))
            tick_to(d, 1600)
            stats[policy] = flat_stats(d.stats())
            first = issued_refs(d)[0].addr_vec[d.level_names.index('Bank')]
            self.assertEqual(first, 2 if policy == 'cap_aware' else 0)
        self.assertEqual(stats['cap_aware']['selected_cap_ready_banks'], 1)
        self.assertGreater(stats['cap_aware']['cap_pre_shared_bank_cycles'], 0)
        self.assertEqual(stats['cap_aware']['cap_pre_injected'], 0)
        self.assertEqual(stats['cap_rr']['cap_pre_injected'], 1)

    def test_auto_precharge_matches_stock(self):
        import ramulator
        ds = [make('cap_rr', 1, refresh=ramulator.refresh_manager.NoRefresh()),
              make('cap_rr', 1, refresh=ramulator.refresh_manager.NoRefresh(), row=ramulator.row_policy.ClosedCAP(cap=1))]
        for d in ds:
            for _ in range(4): d.send_request('Read', addr(d))
            tick_to(d, 1000)
        self.assertEqual(signature(ds[0]), signature(ds[1]))
        self.assertTrue(any(h.command == 'RDA' for h in ds[0].history))

    def test_reordered_rounds_stay_complete_under_load(self):
        from policies import POLICIES
        for policy in POLICIES:
            d = make(policy, cap=4)
            for t in range(1, 45001):
                if t % 47 == 1:
                    d.send_request('Read', addr(d, (t // 47) % 4, (t // 188) % 8, (t // 1504) % 4))
                d.tick()
            seq = [h.addr_vec[d.level_names.index('Bank')] for h in issued_refs(d)]
            self.assertGreater(len(seq), 32)
            for i in range(0, len(seq) - 3, 4):
                self.assertEqual(set(seq[i:i+4]), set(range(4)))

    def test_sharing_removes_only_own_pending_pre(self):
        import ramulator
        # Earlier external REFsb holds the FIFO head while CAP queues its PRE.
        # At 1172 the regular REFsb covers that CAP PRE; remove it alone.
        d = make('cap_rr_share', cap=2)
        open_all(d)
        tick_to(d, 1151)
        d.send_request('Read', addr(d, 0, column=64))
        tick_to(d, 1170)
        d.priority_send('REFsb', d.addr_vec(Channel=0, Rank=0, BankGroup=-1, Bank=1))
        d.priority_send('PREpb', addr(d, 0))  # user-owned PRE must survive
        tick_to(d, 1800)
        stats = flat_stats(d.stats())
        self.assertEqual(stats['cap_pre_cancelled'], 1)
        self.assertTrue(any(h.command == 'PREpb' and h.source_id != -70016 for h in d.history))
        self.assertFalse(any(h.command == 'PREpb' and h.source_id == -70016 for h in d.history))

    def test_reset_keeps_physical_row_count_and_refresh_phase(self):
        for policy in ('cap_rr', 'cap_aware', 'cap_closed'):
            a, b = make(policy, 4), make(policy, 4)
            for d in (a, b):
                for t in range(1, 16001):
                    if t % 60 == 1: d.send_request('Read', addr(d, (t // 600) % 4))
                    if d is b and t == 5300: d.reset_stats()
                    d.tick()
            self.assertEqual(signature(a), signature(b))
            bs = flat_stats(b.stats())
            self.assertEqual(bs['cycles'], 16001 - 5300)

    def test_effective_precharges_and_roi_observer(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp) / 'metrics'
            d = make('open_rr', path=prefix)
            d.send_request('Read', addr(d, bg=7))
            tick_to(d, 900)
            d.reset_stats()
            tick_to(d, 1600)
            d.stats()
            with Path(str(prefix) + '.ch0.csv').open() as f:
                m = {r['key']: int(r['value']) for r in csv.DictReader(f)}
            self.assertEqual(m['pre_commands'], 1)
            self.assertEqual(m['effective_bank_precharges'], 1)  # PREsb targets 8 but only one was open
            self.assertEqual(m['issued_refsb'], 1)
            self.assertEqual(m['act'], 0)
            self.assertEqual(m['end_dram_cycle'] - m['begin_dram_cycle'], 700)
            self.assertEqual(m['duplicate_ids_in_round'], 0)

    def test_invalid_parameters_fail(self):
        import ramulator
        with self.assertRaises((ValueError, RuntimeError)): make(cap=0)
        with self.assertRaises(RuntimeError): make(refresh=ramulator.refresh_manager.DDR5CAPRefresh(policy='unknown'))

    def test_analyzer_with_native_roi_and_synthetic_gem5_metadata(self):
        # Test file parsing/alignment with *explicitly synthetic* CPU metadata.
        # This does not substitute for a gem5 integration run.
        import yaml
        from analyze import analyze
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            d = make('open_rr', 2, p / 'cap_metrics')
            d.send_request('Read', addr(d))
            tick_to(d, 900); d.reset_stats(); tick_to(d, 1800)
            (p / 'ramulator_stats.yaml').write_text(yaml.safe_dump({'memory_system': {'controller': [d.stats()]}}))
            begin, end = 900 * 416, 1800 * 416
            (p / 'roi.json').write_text(json.dumps({'begin_tick': begin, 'end_tick': end, 'ticks': end - begin, 'seconds': (end - begin) / 1e12}))
            (p / 'experiment.json').write_text(json.dumps({'policy': 'open_rr', 'cap': 2}))
            (p / 'stats.txt').write_text('\n'.join(['---------- Begin Simulation Statistics ----------',
                f'finalTick {end}', f'simTicks {end - begin}', 'simFreq 1000000000000',
                'board.processor.cores.core.numCycles 1124', 'simInsts 100',
                '---------- End Simulation Statistics ----------']))
            with (p / 'command_counts.csv').open('w') as f:
                for name in ('ACT', 'RD', 'WR', 'RDA', 'WRA', 'PREpb', 'PREsb', 'PREab', 'REFsb', 'REFab'):
                    f.write(f"{name},{sum(h.command == name and h.clk > 900 for h in d.history)}\n")
            summary, _ = analyze(p)
            self.assertEqual(summary['REFsb'], 1)
            self.assertEqual(summary['ACT'], 0)
            self.assertEqual(summary['effective_bank_precharges'], 1)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    bootstrap(args.root)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.report:
        args.report.write_text(json.dumps({'tests_run': result.testsRun, 'failures': len(result.failures),
            'errors': len(result.errors), 'skipped': len(result.skipped), 'passed': result.wasSuccessful(),
            'scope': 'native compiled Ramulator controller; gem5 not run'}, indent=2) + '\n')
    raise SystemExit(not result.wasSuccessful())

if __name__ == '__main__': main()
