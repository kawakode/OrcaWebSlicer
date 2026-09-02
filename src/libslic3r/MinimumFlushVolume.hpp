#pragma once

#include "PrintConfig.hpp"

namespace Slic3r {

std::vector<int> get_min_flush_volumes(const DynamicPrintConfig &full_config, size_t nozzle_id);

} // namespace Slic3r
