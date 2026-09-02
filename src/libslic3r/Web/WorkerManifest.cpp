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

void add_error(WorkerManifestValidation &result, const char *code, const char *message)
{
    result.errors.push_back({code, message});
}

} // namespace

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

    const auto schema_version = envelope.find("schema_version");
    if (schema_version == envelope.end() || !schema_version->is_number_integer()) {
        add_error(result, "invalid_schema_version", "schema_version must be an integer.");
    } else if (*schema_version != WORKER_MANIFEST_SCHEMA_VERSION) {
        add_error(result, "unsupported_schema_version", "schema_version is not supported.");
    }

    const auto job_id = envelope.find("job_id");
    if (job_id == envelope.end() || !job_id->is_string() || !is_safe_job_id(job_id->get_ref<const std::string &>())) {
        add_error(result, "invalid_job_id", "job_id must contain 1-128 letters, digits, dots, dashes, or underscores.");
    } else {
        manifest.job_id = job_id->get<std::string>();
    }

    const auto operation = envelope.find("operation");
    if (operation == envelope.end() || !operation->is_string() || *operation != "slice") {
        add_error(result, "unsupported_operation", "operation must be 'slice'.");
    } else {
        manifest.operation = operation->get<std::string>();
    }

    if (result.errors.empty())
        result.manifest = std::move(manifest);

    return result;
}

} // namespace Slic3r::Web
