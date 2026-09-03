#pragma once

#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace Slic3r::Web {

inline constexpr int WORKER_PROTOCOL_VERSION = 1;
inline constexpr int SLICE_OPERATION_VERSION = 1;

enum class WorkerErrorCategory {
    Request,
    Input,
    Profile,
    Validation,
    Slicing,
    Cancellation,
    ResourceLimit,
    Internal
};

const char *worker_error_category_name(WorkerErrorCategory category);

struct WorkerManifest
{
    int         protocol_version {WORKER_PROTOCOL_VERSION};
    std::string job_id;
    std::string operation;
    int         operation_version {SLICE_OPERATION_VERSION};
};

struct WorkerManifestError
{
    std::string code;
    std::string message;
    WorkerErrorCategory category {WorkerErrorCategory::Request};
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
