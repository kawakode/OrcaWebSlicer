#include "WorkerProtocol.hpp"

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

nlohmann::json error_json(const WorkerManifestError &error)
{
    return {
        {"category", worker_error_category_name(error.category)},
        {"code", error.code},
        {"message", error.message}
    };
}

nlohmann::json warning_json(const WorkerWarning &warning)
{
    return {{"code", warning.code}, {"message", warning.message}};
}

nlohmann::json artifact_json(const WorkerArtifact &artifact)
{
    return {
        {"kind", artifact.kind},
        {"path", artifact.path},
        {"size_bytes", artifact.size_bytes},
        {"sha256", artifact.sha256}
    };
}

} // namespace

const char *worker_job_state_name(WorkerJobState state)
{
    switch (state) {
    case WorkerJobState::Accepted:  return "accepted";
    case WorkerJobState::Running:   return "running";
    case WorkerJobState::Succeeded: return "succeeded";
    case WorkerJobState::Failed:    return "failed";
    case WorkerJobState::Canceled:  return "canceled";
    }
    return "failed";
}

const char *worker_event_type_name(WorkerEventType type)
{
    switch (type) {
    case WorkerEventType::State:    return "state";
    case WorkerEventType::Progress: return "progress";
    case WorkerEventType::Warning:  return "warning";
    case WorkerEventType::Error:    return "error";
    case WorkerEventType::Artifact: return "artifact";
    }
    return "error";
}

std::string serialize_worker_event(const WorkerEvent &event)
{
    nlohmann::json serialized = {
        {"protocol_version", event.protocol_version},
        {"job_id", event.job_id},
        {"sequence", event.sequence},
        {"type", worker_event_type_name(event.type)}
    };
    if (event.state)
        serialized["state"] = worker_job_state_name(*event.state);
    if (event.progress) {
        serialized["progress"] = {
            {"stage", event.progress->stage},
            {"percent", event.progress->percent},
            {"message", event.progress->message}
        };
    }
    if (event.warning)
        serialized["warning"] = warning_json(*event.warning);
    if (event.error)
        serialized["error"] = error_json(*event.error);
    if (event.artifact)
        serialized["artifact"] = artifact_json(*event.artifact);
    return serialized.dump();
}

std::string serialize_worker_result(const WorkerResult &result)
{
    nlohmann::json warnings = nlohmann::json::array();
    for (const WorkerWarning &warning : result.warnings)
        warnings.push_back(warning_json(warning));

    nlohmann::json artifacts = nlohmann::json::array();
    for (const WorkerArtifact &artifact : result.artifacts)
        artifacts.push_back(artifact_json(artifact));

    nlohmann::json serialized = {
        {"protocol_version", result.protocol_version},
        {"job_id", result.job_id},
        {"outcome", worker_job_state_name(result.outcome)},
        {"warnings", std::move(warnings)},
        {"timing", {
            {"duration_ms", result.timing.duration_ms},
            {"cpu_time_ms", result.timing.cpu_time_ms}
        }},
        {"resource_usage", {{"peak_memory_bytes", result.resource_usage.peak_memory_bytes}}},
        {"artifacts", std::move(artifacts)}
    };
    if (result.error)
        serialized["error"] = error_json(*result.error);
    else
        serialized["error"] = nullptr;
    return serialized.dump();
}

} // namespace Slic3r::Web
