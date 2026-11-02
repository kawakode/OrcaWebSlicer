#pragma once

#include <string>
#include <vector>

namespace Slic3r {
class DynamicPrintConfig;
}

namespace Slic3r::Web {

// Most filaments one job may load: the desktop's own ceiling for a plate.
inline constexpr unsigned MAX_JOB_FILAMENTS = 16;

// The filament profile's options a web job treats per filament: the
// desktop's filament preset options plus `filament_colour`, which the desktop
// keeps beside the preset rather than in it.
const std::vector<std::string> &job_filament_options();

// Merges several flattened filament profiles into `config`, one value per
// filament, as PresetBundle::full_fff_config() does for the desktop: a vector
// option takes each filament's first value, a scalar the first filament's.
// A key a filament does not set keeps the value `config` already had. The
// filament-indexed print options that the desktop sizes alongside (the
// filament map and the filament roles' extruder numbers) are sized and
// clamped the same way.
void compose_filaments(DynamicPrintConfig &config, const std::vector<DynamicPrintConfig> &filaments);

// Fills `flush_volumes_matrix` from the filament colours, as the desktop does
// when a filament is added or recoloured, unless it already holds one value
// per pair of filaments and nozzle. `flush_multiplier` is given one value per
// nozzle. A single filament needs no matrix and is left alone.
void fill_flush_volumes(DynamicPrintConfig &config);

} // namespace Slic3r::Web
