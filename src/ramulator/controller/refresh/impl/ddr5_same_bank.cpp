#include <cstdint>
#include <stdexcept>
#include <utility>
#include <vector>

#include "ramulator/controller/controller_base.h"
#include "ramulator/controller/refresh/i_refresh_manager.h"
#include "ramulator/dram/node.h"

namespace Ramulator {

// DDR5 FGR same-bank refresh baseline: round-robin bank IDs in each rank.
// Reuses existing REFsb/PREsb bank targeting and DRAM timing constraints.
// nREFI remains the normal-mode all-bank interval; every bank-ID set needs
// twice that refresh frequency in FGR: REFsb interval = nREFI/(2*banks_per_BG).
// Micron DDR5 core datasheet, Table 182: tREFIsb = tREFI/(2*n).
// No dynamic temperature, postponement/pull-in, mode-register transition,
// retention/error, or ECC model is added by this manager.
class DDR5SameBankRefresh : public IRefreshManager, public Implementation {
  RAMULATOR_REGISTER_IMPLEMENTATION(IRefreshManager, DDR5SameBankRefresh, "DDR5SameBank")

 private:
  ControllerBase* m_ctrl = nullptr;
  int m_command = -1;
  Clk_t m_nrefi = 0;
  Clk_t m_divisor = 0;
  Clk_t m_event = 1;
  Clk_t m_next_due = 0;
  size_t m_next_bank = 0;
  std::vector<std::vector<AddrVec_t>> m_rank_targets;

  Clk_t due(Clk_t event) const {
    // Absolute rational deadlines avoid truncation drift (9375/8 is fractional).
    return (event * m_nrefi + m_divisor - 1) / m_divisor;
  }

  void init() override {
    m_ctrl = cast_parent<ControllerBase>();
    const auto& info = *m_ctrl->m_device.m_spec;
    if (info.standard_name != "DDR5") {
      throw std::runtime_error("DDR5SameBank requires the DDR5 device model");
    }
    m_command = info.get_command_id("REFsb");
    if (info.bank_targets[m_command] != BankTarget::SameBank) {
      throw std::runtime_error("DDR5SameBank requires same-bank REFsb targeting");
    }
    const int banks_per_bg = info.get_level_size("Bank");
    const int bank_groups = info.get_level_size("BankGroup");
    m_nrefi = info.get_timing_value("nREFI");
    m_divisor = 2 * banks_per_bg;
    if (banks_per_bg <= 0 || bank_groups <= 0 || m_nrefi < m_divisor ||
        m_nrefi / m_divisor < info.get_timing_value("nRFCsb")) {
      throw std::runtime_error("DDR5SameBank: REFsb cadence is too short for nRFCsb");
    }
    const int rank_level = info.get_level_id("Rank");
    const int bg_level = info.get_level_id("BankGroup");
    const int bank_level = info.get_level_id("Bank");
    m_ctrl->m_device.m_root->for_each_at_level(rank_level, [&](DRAMNode* rank) {
      AddrVec_t base(info.level_count, -1);
      for (auto* node = rank; node != nullptr; node = node->m_parent_node) {
        base[node->m_level] = node->m_node_id;
      }
      // A wildcard is required by the existing same-bank state/timing dispatch.
      base[bg_level] = -1;
      std::vector<AddrVec_t> targets;
      for (int bank = 0; bank < banks_per_bg; ++bank) {
        auto target = base;
        target[bank_level] = bank;
        if (m_ctrl->m_device.get_target_banks(m_command, target).size() !=
            static_cast<size_t>(bank_groups)) {
          throw std::runtime_error("DDR5SameBank: unexpected REFsb target coverage");
        }
        targets.push_back(std::move(target));
      }
      m_rank_targets.push_back(std::move(targets));
    });
    if (m_rank_targets.empty()) {
      throw std::runtime_error("DDR5SameBank: no ranks found");
    }
    m_next_due = due(m_event);
  }

  void tick() override {
    if (m_ctrl->m_clk < m_next_due) {
      return;
    }
    for (const auto& rank : m_rank_targets) {
      auto address = rank[m_next_bank];
      // Channel IDs are assigned during controller setup, after manager init.
      address[0] = m_ctrl->m_device.m_root->m_node_id;
      Request request(address, Request::Cmd, m_command);
      if (!m_ctrl->priority_send(request)) {
        throw std::runtime_error("DDR5SameBank: priority buffer cannot accept REFsb");
      }
    }
    m_next_bank = (m_next_bank + 1) % m_rank_targets.front().size();
    m_next_due = due(++m_event);
  }
};

}  // namespace Ramulator
