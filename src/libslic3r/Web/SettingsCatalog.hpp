#pragma once

#include <string>

namespace Slic3r::Web {

inline constexpr int SETTINGS_CATALOG_VERSION = 2;

// Serializes the engine's own definition of every process and filament setting
// a profile can carry: group, mode, type, scope, unit, range, enum values,
// default, dependency, and whether an object or a height range may override
// it. The browser generates its settings form from this document so no type,
// range, or default is ever restated outside PrintConfig.cpp.
std::string serialize_settings_catalog();

// True when the engine applies the setting per object (an object or region
// option), which is what a plate object may override.
bool is_object_setting(const std::string &key);
// True when a height range of an object may override the setting: the layer
// height and the region options, as on the desktop.
bool is_layer_range_setting(const std::string &key);

} // namespace Slic3r::Web
