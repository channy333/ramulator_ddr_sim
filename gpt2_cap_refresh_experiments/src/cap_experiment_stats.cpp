#include <algorithm>
#include <fstream>
#include <map>
#include <string>
#include <stdexcept>
#include <vector>
#include "ramulator/base/param.h"
#include "ramulator/controller/controller_base.h"
#include "ramulator/controller/plugin/i_controller_plugin.h"
#include "ramulator/controller/cap_refresh_view.h"

namespace Ramulator {

// A read-only observer. Device actions run before on_issue, so maintain a
// shadow row state to count banks that were actually open BEFORE precharge.
class CAPExperimentStats : public IControllerPlugin, public Implementation {
  RAMULATOR_REGISTER_IMPLEMENTATION(IControllerPlugin, CAPExperimentStats, "CAPExperimentStats")
  struct Row {
    bool open = false, last_close_cap = false, reopened_after_cap = false;
    int row = -1;
  };
  ControllerBase* m_ctrl = nullptr;
  std::string m_path;
  std::vector<Row> m_rows;
  std::map<std::string, uint64_t> m_counts;
  std::map<int, unsigned> m_masks;
  int m_ref = -1, m_rank = -1, m_row = -1;
  Clk_t m_begin = 0;

 public:
  void init() override { RAMULATOR_PARSE_PARAM(m_path, std::string, "path").required(); }
  void setup(IFrontEnd*, IMemorySystem*) override {
    m_ctrl = cast_parent<ControllerBase>();
    const auto& spec = *m_ctrl->m_device.m_spec;
    m_ref = spec.get_command_id("REFsb"); m_rank = spec.get_level_id("Rank"); m_row = spec.get_level_id("Row");
    m_rows.resize(m_ctrl->m_device.m_bank_nodes.size());
    for (const char* key : {"act", "column_accesses", "pre_commands", "auto_precharge_commands",
          "effective_bank_precharges", "effective_explicit_bank_precharges", "effective_auto_bank_precharges",
          "cap_explicit_closes", "refresh_explicit_closes", "act_after_cap_close",
          "cap_reopen_then_refresh_close", "issued_refsb", "duplicate_ids_in_round",
          "refsb_delay_sum_ck", "refsb_delay_max_ck", "refsb_delay_samples"}) {
      m_counts[key] = 0; m_stats.add(key, m_counts[key]);
    }
  }
  void on_issue(const Request& req) override {
    const auto& spec = *m_ctrl->m_device.m_spec;
    const auto& meta = spec.command_meta[req.command];
    const bool cap = req.source_id == kCapExperimentPreSource;
    const bool refresh_close = req.final_command == m_ref && meta.is_closing;
    if (meta.is_opening) {
      ++m_counts["act"];
      m_ctrl->m_device.for_each_target_bank(req.command, req.addr_vec, [&](int bank) {
        auto& row = m_rows[bank];
        if (row.last_close_cap) ++m_counts["act_after_cap_close"];
        row.reopened_after_cap = row.last_close_cap;
        row.open = true; row.row = req.addr_vec[m_row]; row.last_close_cap = false;
      });
    }
    if (meta.is_accessing) ++m_counts["column_accesses"];
    if (meta.is_closing) {
      ++m_counts[meta.is_accessing ? "auto_precharge_commands" : "pre_commands"];
      m_ctrl->m_device.for_each_target_bank(req.command, req.addr_vec, [&](int bank) {
        auto& row = m_rows[bank];
        if (row.open) {
          ++m_counts["effective_bank_precharges"];
          ++m_counts[meta.is_accessing ? "effective_auto_bank_precharges" : "effective_explicit_bank_precharges"];
          if (cap) ++m_counts["cap_explicit_closes"];
          if (refresh_close) {
            ++m_counts["refresh_explicit_closes"];
            if (row.reopened_after_cap) ++m_counts["cap_reopen_then_refresh_close"];
          }
          row.last_close_cap = cap || meta.is_accessing;
        }
        row.open = false; row.reopened_after_cap = false;
      });
    }
    if (req.command != m_ref) return;
    ++m_counts["issued_refsb"];
    unsigned full = (1u << spec.get_level_size("Bank")) - 1;
    auto& mask = m_masks[req.addr_vec[m_rank]];
    if (mask == full) mask = 0;
    unsigned bit = 1u << req.addr_vec[m_ctrl->m_device.m_bank_level];
    if (mask & bit) ++m_counts["duplicate_ids_in_round"];
    mask |= bit;
    if (req.arrive >= 0) {
      auto delay = static_cast<uint64_t>(std::max<Clk_t>(0, m_ctrl->m_clk - req.arrive));
      m_counts["refsb_delay_sum_ck"] += delay;
      m_counts["refsb_delay_max_ck"] = std::max(m_counts["refsb_delay_max_ck"], delay);
      ++m_counts["refsb_delay_samples"];
    }
  }
  void reset_stats() override {
    for (auto& [_, count] : m_counts) count = 0;
    m_begin = m_ctrl->m_clk;
    // Preserve physical open rows and REFsb-round masks. Clear diagnostic
    // history to prevent attributing pre-ROI closes to the measured interval.
    for (auto& row : m_rows) row.last_close_cap = row.reopened_after_cap = false;
  }
  void finalize() override {
    std::ofstream out(m_path + ".ch" + std::to_string(m_ctrl->m_channel_id) + ".csv");
    if (!out) throw std::runtime_error("CAPExperimentStats cannot open output");
    out << "key,value\n";
    out << "schema_version,1\nbegin_dram_cycle," << m_begin << "\nend_dram_cycle," << m_ctrl->m_clk << '\n';
    out << "tck_ps," << m_ctrl->m_device.m_spec->get_timing_value("tCK_ps") << '\n';
    for (const auto& [key, count] : m_counts) out << key << ',' << count << '\n';
  }
};
} // namespace Ramulator
