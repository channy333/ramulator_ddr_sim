"""Keep CAP semantics, timing, and H identical across the CAP comparisons."""
POLICIES = ('open_rr', 'cap_rr', 'cap_rr_share', 'cap_leastq', 'cap_closed', 'cap_aware')

def components(ramulator, policy, cap=16):
    if policy not in POLICIES:
        raise ValueError(f'Unknown policy: {policy}')
    if cap <= 0:
        raise ValueError('cap must be positive')
    selection = {'cap_leastq': 'leastq', 'cap_closed': 'closed', 'cap_aware': 'cap'}.get(policy, 'rr')
    row = (ramulator.row_policy.Open() if policy == 'open_rr' else
           ramulator.row_policy.ClosedCAPExperiment(cap=cap, share_precharge=policy != 'cap_rr'))
    return row, ramulator.refresh_manager.DDR5CAPRefresh(policy=selection)
