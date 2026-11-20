#include <algorithm>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>
#include "ramulator/base/param.h"
#include "ramulator/controller/controller_base.h"
#include "ramulator/controller/refresh/i_refresh_manager.h"
#include "ramulator/controller/rowpolicy/i_row_policy.h"
#include "ramulator/controller/cap_refresh_view.h"

namespace Ramulator {

class DDR5CAPRefresh : public IRefreshManager, public Implementation {
  RAMULATOR_REGISTER_IMPLEMENTATION(IRefreshManager, DDR5CAPRefresh, "DDR5CAPRefresh")
  ControllerBase* m_ctrl = nullptr;
  ICapClosureView* m_cap_view = nullptr;
  std::string m_policy;
  int m_command = -1, m_closed = -1;
  Clk_t m_nrefi = 0, m_divisor = 0, m_event = 1, m_next_due = 0;
  std::vector<std::vector<AddrVec_t>> m_targets;
  std::vector<unsigned> m_masks;
  std::vector<int> m_cursor;
  uint64_t s_selected = 0, s_selected_closed = 0, s_selected_cap_ready = 0;
  uint64_t s_selected_queue = 0, s_changed_from_rr = 0, s_slots_with_cap_option = 0;

  Clk_t due(Clk_t event) const { return (event * m_nrefi + m_divisor - 1) / m_divisor; }
  struct Score { int closed = 0, cap = 0, queue = 0; };
  Score score(const AddrVec_t& address) const {
    Score value;
    auto banks = m_ctrl->m_device.get_target_banks(m_command, address);
    for (int bank : banks) {
      if (m_ctrl->m_device.m_bank_nodes[bank]->m_state == m_closed) ++value.closed;
      else if (m_cap_view && m_cap_view->cap_close_ready(bank)) ++value.cap;
    }
    auto scan = [&](const ReqBuffer& buffer) {
      for (const auto& req : buffer.buffer) {
        int bank = m_ctrl->m_device.get_flat_bank_id(req.addr_vec);
        if (std::find(banks.begin(), banks.end(), bank) != banks.end()) ++value.queue;
      }
    };
    scan(m_ctrl->m_read_buffer); scan(m_ctrl->m_write_buffer); scan(m_ctrl->m_active_buffer);
    return value;
  }
  bool better(const Score& a, const Score& b) const {
    if (m_policy == "leastq") return a.queue < b.queue;
    if (m_policy == "closed") return a.closed > b.closed;
    if (m_policy == "cap") {
      if (a.closed + a.cap != b.closed + b.cap) return a.closed + a.cap > b.closed + b.cap;
      return a.closed > b.closed;
    }
    return false; // RR: the first eligible cursor position wins.
  }

 public:
  void init() override {
    m_ctrl = cast_parent<ControllerBase>();
    RAMULATOR_PARSE_PARAM(m_policy, std::string, "policy").default_val("rr");
    if (m_policy != "rr" && m_policy != "leastq" && m_policy != "closed" && m_policy != "cap")
      throw std::runtime_error("DDR5CAPRefresh: policy must be rr, leastq, closed, or cap");
    const auto& spec = *m_ctrl->m_device.m_spec;
    if (spec.standard_name != "DDR5") throw std::runtime_error("DDR5CAPRefresh requires DDR5");
    m_command = spec.get_command_id("REFsb"); m_closed = spec.get_state_id("Closed");
    if (spec.bank_targets[m_command] != BankTarget::SameBank)
      throw std::runtime_error("DDR5CAPRefresh requires same-bank REFsb targeting");
    int banks = spec.get_level_size("Bank");
    m_nrefi = spec.get_timing_value("nREFI"); m_divisor = 2 * banks;
    if (banks <= 0 || banks > 16 || m_nrefi / m_divisor < spec.get_timing_value("nRFCsb"))
      throw std::runtime_error("DDR5CAPRefresh: invalid organization/cadence");
    int rank_level = spec.get_level_id("Rank"), bg_level = spec.get_level_id("BankGroup");
    m_ctrl->m_device.m_root->for_each_at_level(rank_level, [&](DRAMNode* rank) {
      AddrVec_t base(spec.level_count, -1);
      for (auto* node = rank; node; node = node->m_parent_node) base[node->m_level] = node->m_node_id;
      base[bg_level] = -1;
      std::vector<AddrVec_t> targets;
      for (int bank = 0; bank < banks; ++bank) {
        auto target = base; target[spec.get_level_id("Bank")] = bank; targets.push_back(target);
        if (m_ctrl->m_device.get_target_banks(m_command, target).size() !=
            static_cast<size_t>(spec.get_level_size("BankGroup")))
          throw std::runtime_error("DDR5CAPRefresh: unexpected target coverage");
      }
      m_targets.push_back(std::move(targets));
    });
    if (m_targets.empty()) throw std::runtime_error("DDR5CAPRefresh: no ranks");
    m_masks.assign(m_targets.size(), 0); m_cursor.assign(m_targets.size(), 0);
    m_next_due = due(m_event);
    m_stats.add("selected_refsb", s_selected); m_stats.add("selected_closed_banks", s_selected_closed);
    m_stats.add("selected_cap_ready_banks", s_selected_cap_ready); m_stats.add("selected_queued_accesses", s_selected_queue);
    m_stats.add("changed_from_rr", s_changed_from_rr); m_stats.add("slots_with_cap_candidate", s_slots_with_cap_option);
  }

  void setup(IFrontEnd*, IMemorySystem*) override {
    m_cap_view = dynamic_cast<ICapClosureView*>(m_ctrl->m_rowpolicy);
    if (m_policy == "cap" && !m_cap_view) throw std::runtime_error("CAP-aware refresh requires ClosedCAPExperiment");
  }

  void tick() override {
    if (m_ctrl->m_clk < m_next_due) return;
    for (size_t rank = 0; rank < m_targets.size(); ++rank) {
      const int count = static_cast<int>(m_targets[rank].size());
      const unsigned full = (1u << count) - 1;
      if (m_masks[rank] == full) m_masks[rank] = 0;
      int chosen = -1, rr = -1; Score best; bool cap_option = false;
      for (int offset = 0; offset < count; ++offset) {
        int bank = (m_cursor[rank] + offset) % count;
        if (m_masks[rank] & (1u << bank)) continue;
        auto addr = m_targets[rank][bank]; addr[0] = m_ctrl->m_device.m_root->m_node_id;
        Score current = score(addr); cap_option = cap_option || current.cap > 0;
        if (rr < 0) rr = bank;
        if (chosen < 0 || better(current, best)) { chosen = bank; best = current; }
      }
      if (chosen < 0) throw std::runtime_error("DDR5CAPRefresh: no eligible bank ID");
      auto address = m_targets[rank][chosen]; address[0] = m_ctrl->m_device.m_root->m_node_id;
      Request req(address, Request::Cmd, m_command);
      req.arrive = m_next_due; // Used only to measure enqueue-to-issue lateness.
      if (!m_ctrl->priority_send(req)) throw std::runtime_error("DDR5CAPRefresh: priority buffer full");
      if (m_cap_view) m_cap_view->refresh_enqueued(address);
      m_masks[rank] |= 1u << chosen; m_cursor[rank] = (chosen + 1) % count;
      ++s_selected; s_selected_closed += best.closed; s_selected_cap_ready += best.cap;
      s_selected_queue += best.queue; s_changed_from_rr += chosen != rr; s_slots_with_cap_option += cap_option;
    }
    m_next_due = due(++m_event);
  }

  void reset_stats() override {
    s_selected = s_selected_closed = s_selected_cap_ready = s_selected_queue = 0;
    s_changed_from_rr = s_slots_with_cap_option = 0;
    // Preserve due times, round masks and cursor across ROI reset.
  }
};
} // namespace Ramulator
