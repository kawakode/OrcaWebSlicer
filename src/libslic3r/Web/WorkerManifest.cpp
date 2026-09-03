#include "WorkerManifest.hpp"

#include <algorithm>
#include <utility>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

bool is_safe_job_id(const std::string &job_id)
{
    if (job_id.empty() || job_id.size() > 128)
        return false;

    return std::all_of(job_id.begin(), job_id.end(), [](unsigned char c) {
        const bool ascii_letter = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
        const bool ascii_digit  = c >= '0' && c <= '9';
        return ascii_letter || ascii_digit || c == '-' || c == '_' || c == '.';
    });
}

void add_error(WorkerManifestValidation &result, const char *code, const char *message,
               WorkerErrorCategory category = WorkerErrorCategory::Request)
{
    result.errors.push_back({code, message, category});
}

} // namespace

const char *worker_error_category_name(WorkerErrorCategory category)
{
    switch (category) {
    case WorkerErrorCategory::Request:       return "request";
    case WorkerErrorCategory::Input:         return "input";
    case WorkerErrorCategory::Profile:       return "profile";
    case WorkerErrorCategory::Validation:    return "validation";
    case WorkerErrorCategory::Slicing:       return "slicing";
    case WorkerErrorCategory::Cancellation:  return "cancellation";
    case WorkerErrorCategory::ResourceLimit: return "resource_limit";
    case WorkerErrorCategory::Internal:      return "internal";
    }
    return "internal";
}

WorkerManifestValidation validate_worker_manifest(std::string_view serialized)
{
    WorkerManifestValidation result;
    const nlohmann::json envelope = nlohmann::json::parse(serialized.begin(), serialized.end(), nullptr, false);

    if (envelope.is_discarded()) {
        add_error(result, "invalid_json", "Manifest is not valid JSON.");
        return result;
    }
    if (!envelope.is_object()) {
        add_error(result, "invalid_envelope", "Manifest root must be a JSON object.");
        return result;
    }

    WorkerManifest manifest;

    const auto protocol_version = envelope.find("protocol_version");
    if (protocol_version == envelope.end() || !protocol_version->is_number_integer()) {
        add_error(result, "invalid_protocol_version", "protocol_version must be an integer.");
    } else if (*protocol_version != WORKER_PROTOCOL_VERSION) {
        add_error(result, "unsupported_protocol_version", "protocol_version is not supported.");
    }

    const auto job_id = envelope.find("job_id");
    if (job_id == envelope.end() || !job_id->is_string() || !is_safe_job_id(job_id->get_ref<const std::string &>())) {
        add_error(result, "invalid_job_id", "job_id must contain 1-128 letters, digits, dots, dashes, or underscores.");
    } else {
        manifest.job_id = job_id->get<std::string>();
    }

    const auto operation = envelope.find("operation");
    if (operation == envelope.end() || !operation->is_object()) {
        add_error(result, "invalid_operation", "operation must be an object.");
    } else {
        const auto name = operation->find("name");
        if (name == operation->end() || !name->is_string() || *name != "slice") {
            add_error(result, "unsupported_operation", "operation.name must be 'slice'.");
        } else {
            manifest.operation = name->get<std::string>();
        }

        const auto version = operation->find("version");
        if (version == operation->end() || !version->is_number_integer()) {
            add_error(result, "invalid_operation_version", "operation.version must be an integer.");
        } else if (*version != SLICE_OPERATION_VERSION) {
            add_error(result, "unsupported_operation_version", "The slice operation version is not supported.");
        } else {
            manifest.operation_version = version->get<int>();
        }

        const auto payload = operation->find("payload");
        if (payload == operation->end() || !payload->is_object())
            add_error(result, "invalid_operation_payload", "operation.payload must be an object.");
    }

    if (result.errors.empty())
        result.manifest = std::move(manifest);

    return result;
}

} // namespace Slic3r::Web
