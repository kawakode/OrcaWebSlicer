#pragma once

#include "WorkerManifest.hpp"

#include <filesystem>
#include <string>
#include <string_view>

namespace Slic3r::Web {

inline constexpr int COMPATIBILITY_REQUEST_VERSION = 1;

// Answers "which of these printers does each candidate profile suit?" using the
// engine's own placeholder parser, so `compatible_printers_condition`
// expressions are never reimplemented outside libslic3r.
//
// The request names printer profiles by paths relative to `root`, exactly as a
// slice manifest names its inputs, and the same containment rules apply. On
// success `response` receives one compact JSON document; on failure `error`
// carries a stable code and message.
bool evaluate_profile_compatibility(std::string_view serialized, const std::filesystem::path &root,
                                    std::string &response, WorkerManifestError &error);

} // namespace Slic3r::Web
