#pragma once

#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace Slic3r::Web {

inline constexpr int WORKER_MANIFEST_SCHEMA_VERSION = 1;

struct WorkerManifest
{
    int         schema_version {WORKER_MANIFEST_SCHEMA_VERSION};
    std::string job_id;
    std::string operation;
};

struct WorkerManifestError
{
    std::string code;
    std::string message;
};

struct WorkerManifestValidation
{
    std::optional<WorkerManifest> manifest;
    std::vector<WorkerManifestError> errors;

    bool is_valid() const { return manifest.has_value() && errors.empty(); }
};

// Validates only the stable job envelope. Operation-specific fields are left
// untouched so the slicing protocol can evolve independently.
WorkerManifestValidation validate_worker_manifest(std::string_view serialized);

} // namespace Slic3r::Web
