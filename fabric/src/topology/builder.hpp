#pragma once

#include "topology/model.hpp"
#include "topology/source.hpp"

namespace ostia::fabric::topology {

// Pure: the same facts always give the same model (RFC-0003 §6).
Model build(const Facts& facts);

} // namespace ostia::fabric::topology
