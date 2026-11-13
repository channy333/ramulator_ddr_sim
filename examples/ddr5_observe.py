"""Example Ramulator2 configuration and simulation script"""
# JC start
from pathlib import Path

out = Path("results/ddr5_observe")
out.mkdir(parents=True, exist_ok=True)
# JC finish
import ramulator

# Configure the simulation frontend that sends memory requests
frontend = ramulator.frontend.SimpleO3(
    clock_ratio=8,
    traces=["./examples/traces/example_inst.trace"],
    num_expected_insts=500000,
    translation=ramulator.translation.NoTranslation(max_addr=2147483648),
)

# Create DRAM configuration
dram = ramulator.dram.DDR5(
    org_preset="DDR5_16Gb_x8",
    timing_preset="DDR5_4800AN",
    rank=1,
    verbose=True,   # This prints the final timing constraints
)
# Instantiate the memory controller with the DRAM configuation
ctrl = ramulator.controller.GenericDDR(
    dram=dram,
    scheduler=ramulator.scheduler.FRFCFS(),
    refresh_manager=ramulator.refresh_manager.AllBank(),
    row_policy=ramulator.row_policy.Open(),
    addr_mapper=ramulator.addr_mapper.RoBaRaCoCh(),
    # JC start
    controller_plugins=[
        ramulator.controller_plugin.CommandCounter(
            commands_to_count=[
                "ACT", "RD", "WR",
                "PREpb", "PREab", "PREsb",
                "REFab", "REFsb",
            ],
            path="results/ddr5_observe/command_counts.csv",
        ),
        ramulator.controller_plugin.CmdTraceRecorder(
            path="results/ddr5_observe/commands.csv",
        ),
    ],
    # JC finish
)
# Create a memory system with the controller
mem = ramulator.memory_system.GenericDRAM(
    clock_ratio=3,
    controllers=[ctrl],
    channel_mapper=ramulator.channel_mapper.CacheLineInterleave(),
)

# Run the simulation
sim = ramulator.Simulation(frontend, mem)
sim.run()
# JC start
sim.finalize()

(out / "stats.yaml").write_text(
    sim.stats_yaml,
    encoding="utf-8",
)
# JC finish
# sim.stats returns a nested Python dict of all simulation statistics
stats = sim.stats

# Guard here for `ramulator export`, which does not run the simulation 
# but only exports the config for pure C++ Ramulator library
if stats:
    # Controller stats are under memory_system → controller
    ctrl_stats = stats["memory_system"]["controller"]

    print(f"Controller cycles:     {ctrl_stats['cycles']}")
    print(f"Avg read latency:      {ctrl_stats['avg_read_latency']:.1f} cycles")
    print(f"Read requests:         {ctrl_stats['num_read_reqs']}")
    print(f"Write requests:        {ctrl_stats['num_write_reqs']}")
    print(f"Row hits:              {ctrl_stats['row_hits']}")
    print(f"Row misses:            {ctrl_stats['row_misses']}")
    print(f"Row conflicts:         {ctrl_stats['row_conflicts']}")
