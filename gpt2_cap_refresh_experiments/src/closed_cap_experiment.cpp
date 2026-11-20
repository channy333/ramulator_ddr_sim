#include <stdexcept>
#include <unordered_set>
#include <vector>
#include "ramulator/base/param.h"
#include "ramulator/controller/controller_base.h"
#include "ramulator/controller/rowpolicy/i_row_policy.h"
#include "ramulator/controller/cap_refresh_view.h"

namespace Ramulator {

// Same hard-CAP counting / AP-or-PRE behavior as pinned upstream ClosedCAP.
// The optional sharing path cancels only this policy's OWN queued PREpb requests
// when a queued REFsb already covers their target bank. It changes no timings.
class ClosedCAPExperiment : public IRowPolicy, public Implementation, public ICapClosureView {
  RAMULATOR_REGISTER_IMPLEMENTATION(IRowPolicy, ClosedCAPExperiment, "ClosedCAPExperiment")
  ControllerBase* m_ctrl = nullptr;
  DRAMDevice* m_device = nullptr;
  const DRAMSpec* m_spec = nullptr;
  int m_cap = 16, m_pre = -1, m_ref = -1, m_closed = -1;
  int m_rd = -1, m_wr = -1, m_rda = -1, m_wra = -1;
  bool m_share_precharge = false;
  std::vector<int> m_accesses;
  std::vector<bool> m_injected;
  std::unordered_set<int64_t> m_own_pres;
  uint64_t s_cap_pre_injected = 0, s_cap_pre_cancelled = 0, s_cap_pre_shared_bank_cycles = 0;

  AddrVec_t address(int bank) const {
    AddrVec_t result(m_spec->level_count, -1);
    for (auto* node = m_device->m_bank_nodes[bank]; node; node = node->m_parent_node)
      result[node->m_level] = node->m_node_id;
    return result;
  }
  bool refresh_covers(int bank) const {
    for (const auto& req : m_ctrl->m_priority_buffer.buffer) {
      if (req.final_command != m_ref) continue;
      auto targets = m_device->get_target_banks(m_ref, req.addr_vec);
      for (int target : targets) if (target == bank) return true;
    }
    return false;
  }

 public:
  void init() override {
    m_ctrl = cast_parent<ControllerBase>(); m_device = &m_ctrl->m_device; m_spec = m_device->m_spec;
    RAMULATOR_PARSE_PARAM(m_cap, int, "cap").default_val(16);
    RAMULATOR_PARSE_PARAM(m_share_precharge, bool, "share_precharge").default_val(false);
    if (m_cap <= 0) throw std::runtime_error("ClosedCAPExperiment: cap must be positive");
    if (m_spec->standard_name != "DDR5") throw std::runtime_error("ClosedCAPExperiment requires DDR5");
    m_pre = m_spec->get_command_id("PREpb"); m_ref = m_spec->get_command_id("REFsb");
    m_closed = m_spec->get_state_id("Closed");
    m_rd = m_spec->get_command_id("RD"); m_wr = m_spec->get_command_id("WR");
    m_rda = m_spec->get_command_id("RDA"); m_wra = m_spec->get_command_id("WRA");
    m_accesses.assign(m_device->m_bank_nodes.size(), 0);
    m_injected.assign(m_accesses.size(), false);
    m_stats.add("cap_pre_injected", s_cap_pre_injected);
    m_stats.add("cap_pre_cancelled", s_cap_pre_cancelled);
    m_stats.add("cap_pre_shared_bank_cycles", s_cap_pre_shared_bank_cycles);
  }

  bool cap_close_ready(int bank) const override {
    if (m_device->m_bank_nodes.at(bank)->m_state == m_closed || m_accesses.at(bank) < m_cap) return false;
    return m_injected.at(bank) || m_ctrl->check_timing(m_pre, address(bank));
  }

  void refresh_enqueued(const AddrVec_t& target) override {
    if (!m_share_precharge) return;
    std::unordered_set<int> banks;
    for (int bank : m_device->get_target_banks(m_ref, target)) banks.insert(bank);
    auto& queue = m_ctrl->m_priority_buffer.buffer;
    for (auto it = queue.begin(); it != queue.end();) {
      if (m_own_pres.count(it->id) && banks.count(m_device->get_flat_bank_id(it->addr_vec))) {
        int bank = m_device->get_flat_bank_id(it->addr_vec);
        m_own_pres.erase(it->id); m_injected[bank] = false;
        it = queue.erase(it); ++s_cap_pre_cancelled;
      } else ++it;
    }
  }

  void try_upgrade_command(Request& req) override {
    if (req.command != m_rd && req.command != m_wr) return;
    if (req.addr_vec.empty()) return;
    int bank = m_device->get_flat_bank_id(req.addr_vec);
    if (m_accesses.at(bank) < m_cap) return;
    if (req.command == m_rd && m_ctrl->check_timing(m_rda, req.addr_vec))
      req.command = req.final_command = m_rda;
    else if (req.command == m_wr && m_ctrl->check_timing(m_wra, req.addr_vec))
      req.command = req.final_command = m_wra;
  }

  void on_issue(const Request& req) override {
    m_own_pres.erase(req.id);
    const auto& meta = m_spec->command_meta[req.command];
    if (meta.is_closing) m_device->for_each_target_bank(req.command, req.addr_vec, [&](int bank) {
      m_accesses[bank] = 0; m_injected[bank] = false;
    });
    if (meta.is_accessing && !meta.is_closing && !req.addr_vec.empty())
      ++m_accesses[m_device->get_flat_bank_id(req.addr_vec)];
  }

  void pre_schedule() override {
    for (int bank = 0; bank < static_cast<int>(m_accesses.size()); ++bank) {
      if (m_accesses[bank] < m_cap || m_injected[bank]) continue;
      if (m_share_precharge && refresh_covers(bank)) { ++s_cap_pre_shared_bank_cycles; continue; }
      auto addr = address(bank);
      if (!m_ctrl->check_timing(m_pre, addr)) continue;
      Request req(std::move(addr), Request::Cmd, m_pre);
      req.source_id = kCapExperimentPreSource;
      if (m_ctrl->priority_send(req)) {
        m_injected[bank] = true; m_own_pres.insert(req.id); ++s_cap_pre_injected;
      }
    }
  }

  void reset_stats() override {
    s_cap_pre_injected = s_cap_pre_cancelled = s_cap_pre_shared_bank_cycles = 0;
    // Preserve row lifetime counters and pending commands across ROI reset.
  }
};
} // namespace Ramulator
