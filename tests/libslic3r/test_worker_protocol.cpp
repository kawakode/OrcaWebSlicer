#include <catch2/catch_all.hpp>

#include "libslic3r/Web/WorkerProtocol.hpp"

#include <fstream>
#include <nlohmann/json.hpp>
#include <string>

using namespace Slic3r::Web;

namespace {

nlohmann::json fixture(const char *name)
{
    const std::string path = std::string(TEST_DATA_DIR) + "/web-worker/contracts/" + name;
    std::ifstream input(path);
    REQUIRE(input.good());
    return nlohmann::json::parse(input);
}

nlohmann::json serialized(const WorkerEvent &event)
{
    return nlohmann::json::parse(serialize_worker_event(event));
}

nlohmann::json serialized(const WorkerResult &result)
{
    return nlohmann::json::parse(serialize_worker_result(result));
}

} // namespace

TEST_CASE("Worker protocol serializes accepted and running states", "[WorkerProtocol]")
{
    WorkerEvent accepted;
    accepted.job_id = "contract-job";
    accepted.sequence = 0;
    accepted.state = WorkerJobState::Accepted;
    REQUIRE(serialized(accepted) == fixture("accepted-event.json"));

    WorkerEvent running = accepted;
    running.sequence = 1;
    running.state = WorkerJobState::Running;
    REQUIRE(serialized(running) == fixture("running-event.json"));
}

TEST_CASE("Worker protocol serializes every terminal result", "[WorkerProtocol]")
{
    WorkerResult succeeded;
    succeeded.job_id = "contract-job";
    succeeded.outcome = WorkerJobState::Succeeded;
    succeeded.warnings.push_back({"thin_wall", "A thin wall was detected."});
    succeeded.timing = {1250, 1100};
    succeeded.resource_usage = {67108864};
    succeeded.artifacts.push_back({"gcode", "output/model.gcode", 1234,
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"});
    REQUIRE(serialized(succeeded) == fixture("succeeded-result.json"));

    WorkerResult failed;
    failed.job_id = "contract-job";
    failed.outcome = WorkerJobState::Failed;
    failed.timing = {25, 20};
    failed.resource_usage = {1048576};
    failed.error = WorkerManifestError{
        "invalid_input_model", "input_model must be a safe relative path.", WorkerErrorCategory::Input
    };
    REQUIRE(serialized(failed) == fixture("failed-result.json"));

    WorkerResult canceled;
    canceled.job_id = "contract-job";
    canceled.outcome = WorkerJobState::Canceled;
    canceled.timing = {500, 300};
    canceled.resource_usage = {33554432};
    canceled.error = WorkerManifestError{
        "job_canceled", "The slicing job was canceled.", WorkerErrorCategory::Cancellation
    };
    REQUIRE(serialized(canceled) == fixture("canceled-result.json"));
}

TEST_CASE("Worker protocol serializes typed progress warning error and artifact records", "[WorkerProtocol]")
{
    WorkerEvent progress;
    progress.job_id = "contract-job";
    progress.sequence = 2;
    progress.type = WorkerEventType::Progress;
    progress.progress = WorkerProgress{"slicing", 42, "Slicing model"};
    const nlohmann::json progress_json = serialized(progress);
    REQUIRE(progress_json.at("progress").at("stage") == "slicing");
    REQUIRE(progress_json.at("progress").at("percent") == 42);

    WorkerEvent warning = progress;
    warning.sequence = 3;
    warning.type = WorkerEventType::Warning;
    warning.progress.reset();
    warning.warning = WorkerWarning{"thin_wall", "A thin wall was detected."};
    REQUIRE(serialized(warning).at("warning").at("code") == "thin_wall");

    WorkerEvent error = warning;
    error.sequence = 4;
    error.type = WorkerEventType::Error;
    error.warning.reset();
    error.error = WorkerManifestError{"slice_failed", "Slicing failed.", WorkerErrorCategory::Slicing};
    REQUIRE(serialized(error).at("error").at("category") == "slicing");

    WorkerEvent artifact = error;
    artifact.sequence = 5;
    artifact.type = WorkerEventType::Artifact;
    artifact.error.reset();
    artifact.artifact = WorkerArtifact{"gcode", "output/model.gcode", 1234, "abcd"};
    REQUIRE(serialized(artifact).at("artifact").at("size_bytes") == 1234);
}
