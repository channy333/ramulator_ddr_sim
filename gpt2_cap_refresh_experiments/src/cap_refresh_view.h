#ifndef RAMULATOR_CAP_REFRESH_EXPERIMENT_VIEW_H
#define RAMULATOR_CAP_REFRESH_EXPERIMENT_VIEW_H
#include "ramulator/base/request.h"
namespace Ramulator {
inline constexpr int kCapExperimentPreSource = -70016;
// Deliberately separate from the stock row-policy interface and implementation.
class ICapClosureView {
 public:
  virtual ~ICapClosureView() = default;
  virtual bool cap_close_ready(int bank) const = 0;
  virtual void refresh_enqueued(const AddrVec_t& address) = 0;
};
} // namespace Ramulator
#endif
