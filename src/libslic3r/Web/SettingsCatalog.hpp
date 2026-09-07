#pragma once

#include <string>

namespace Slic3r::Web {

inline constexpr int SETTINGS_CATALOG_VERSION = 1;

// Serializes the engine's own definition of every curated MVP setting: type,
// scope, unit, range, enum values, default, and dependency. The browser
// generates its settings form from this document so no type, range, or default
// is ever restated outside PrintConfig.cpp.
std::string serialize_settings_catalog();

} // namespace Slic3r::Web
