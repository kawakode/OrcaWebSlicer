#pragma once

#include "WorkerManifest.hpp"

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace Slic3r::Web {

enum class WorkerJobState {
    Accepted,
    Running,
    Succeeded,
    Failed,
    Canceled
};

enum class WorkerEventType {
    State,
    Progress,
    Warning,
    Error,
    Artifact
};

struct WorkerProgress
{
    std::string stage;
    unsigned    percent {0};
    std::string message;
};

struct WorkerWarning
{
    std::string code;
    std::string message;
};

struct WorkerArtifact
{
    std::string  kind;
    std::string  path;
    std::uintmax_t size_bytes {0};
    std::string  sha256;
};

struct WorkerEvent
{
    int                         protocol_version {WORKER_PROTOCOL_VERSION};
    std::string                 job_id;
    std::uint64_t               sequence {0};
    WorkerEventType             type {WorkerEventType::State};
    std::optional<WorkerJobState> state;
    std::optional<WorkerProgress> progress;
    std::optional<WorkerWarning> warning;
    std::optional<WorkerManifestError> error;
    std::optional<WorkerArtifact> artifact;
};

struct WorkerTiming
{
    std::uint64_t duration_ms {0};
    std::uint64_t cpu_time_ms {0};
};

struct WorkerResourceUsage
{
    std::uintmax_t peak_memory_bytes {0};
};

struct WorkerResult
{
    int                            protocol_version {WORKER_PROTOCOL_VERSION};
    std::string                    job_id;
    WorkerJobState                 outcome {WorkerJobState::Failed};
    std::vector<WorkerWarning>     warnings;
    WorkerTiming                   timing;
    WorkerResourceUsage            resource_usage;
    std::vector<WorkerArtifact>    artifacts;
    std::optional<WorkerManifestError> error;
};

const char *worker_job_state_name(WorkerJobState state);
const char *worker_event_type_name(WorkerEventType type);

// Worker stdout is newline-delimited JSON. These functions return one compact
// JSON record without a trailing newline.
std::string serialize_worker_event(const WorkerEvent &event);
std::string serialize_worker_result(const WorkerResult &result);

} // namespace Slic3r::Web
