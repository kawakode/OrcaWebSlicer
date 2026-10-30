#pragma once

#include "SinglePlateSlice.hpp"

#include <filesystem>

#include <nlohmann/json.hpp>

namespace Slic3r {
class DynamicPrintConfig;
class Model;
} // namespace Slic3r

namespace Slic3r::Web {

// Side length of the plate thumbnail stored in a project, the desktop's own.
inline constexpr unsigned PROJECT_THUMBNAIL_SIZE = 512;

// Writes the composed plate as a desktop-compatible project archive through the
// engine's own 3MF exporter: every object with its instance placements, one
// plate holding all of them, the effective configuration, and a thumbnail.
//
// The configuration names the selected presets the way the desktop does
// (`printer_settings_id`, `print_settings_id`, `filament_settings_id`) and
// lists the request's overrides in `different_settings_to_system`, so the
// desktop opens the project on the same presets, marked as modified, and the
// browser can restore the same selection from an inspection.
SinglePlateSliceResult export_plate_project(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                            Model &model, DynamicPrintConfig &config,
                                            const SinglePlateSliceCallbacks &callbacks);

// The preset selection and overridden settings a project configuration
// records, or null when it names no preset. Each override is serialized as a
// request would submit it; a filament setting keeps the first filament's value.
nlohmann::json describe_project_selection(const DynamicPrintConfig &config);

} // namespace Slic3r::Web
