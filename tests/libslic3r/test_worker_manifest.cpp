#include <catch2/catch_all.hpp>

#include "libslic3r/Web/WorkerManifest.hpp"

#include <algorithm>
#include <string>

using namespace Slic3r::Web;

namespace {

bool has_error(const WorkerManifestValidation &validation, const std::string &code)
{
    return std::any_of(validation.errors.begin(), validation.errors.end(), [&code](const WorkerManifestError &error) {
        return error.code == code;
    });
}

} // namespace

TEST_CASE("Worker manifest accepts the versioned slice envelope", "[WorkerManifest]")
{
    const WorkerManifestValidation validation = validate_worker_manifest(R"({
        "protocol_version": 1,
        "job_id": "job-2026.09_02",
        "operation": {"name":"slice","version":1,"payload":{"future_payload_field":true},"future_operation_field":true},
        "future_field": {"allowed": true}
    })");

    REQUIRE(validation.is_valid());
    REQUIRE(validation.manifest->protocol_version == WORKER_PROTOCOL_VERSION);
    REQUIRE(validation.manifest->job_id == "job-2026.09_02");
    REQUIRE(validation.manifest->operation == "slice");
    REQUIRE(validation.manifest->operation_version == SLICE_OPERATION_VERSION);
}

TEST_CASE("Worker manifest rejects malformed JSON and non-object roots", "[WorkerManifest]")
{
    const WorkerManifestValidation malformed = validate_worker_manifest("{");
    REQUIRE_FALSE(malformed.is_valid());
    REQUIRE(has_error(malformed, "invalid_json"));

    const WorkerManifestValidation array = validate_worker_manifest("[]");
    REQUIRE_FALSE(array.is_valid());
    REQUIRE(has_error(array, "invalid_envelope"));
}

TEST_CASE("Worker manifest rejects missing and unsupported protocol versions", "[WorkerManifest]")
{
    const WorkerManifestValidation missing = validate_worker_manifest(
        R"({"job_id":"job-1","operation":{"name":"slice","version":1,"payload":{}}})");
    REQUIRE_FALSE(missing.is_valid());
    REQUIRE(has_error(missing, "invalid_protocol_version"));

    const WorkerManifestValidation unsupported =
        validate_worker_manifest(R"({"protocol_version":2,"job_id":"job-1","operation":{"name":"slice","version":1,"payload":{}}})");
    REQUIRE_FALSE(unsupported.is_valid());
    REQUIRE(has_error(unsupported, "unsupported_protocol_version"));
}

TEST_CASE("Worker manifest rejects unsafe job identifiers", "[WorkerManifest]")
{
    const WorkerManifestValidation path =
        validate_worker_manifest(R"({"protocol_version":1,"job_id":"../job","operation":{"name":"slice","version":1,"payload":{}}})");
    REQUIRE_FALSE(path.is_valid());
    REQUIRE(has_error(path, "invalid_job_id"));

    const WorkerManifestValidation empty =
        validate_worker_manifest(R"({"protocol_version":1,"job_id":"","operation":{"name":"slice","version":1,"payload":{}}})");
    REQUIRE_FALSE(empty.is_valid());
    REQUIRE(has_error(empty, "invalid_job_id"));
}

TEST_CASE("Worker manifest rejects unsupported operations", "[WorkerManifest]")
{
    const WorkerManifestValidation validation =
        validate_worker_manifest(R"({"protocol_version":1,"job_id":"job-1","operation":{"name":"render","version":1,"payload":{}}})");
    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "unsupported_operation"));
}

TEST_CASE("Worker manifest rejects unsupported operation versions", "[WorkerManifest]")
{
    const WorkerManifestValidation validation = validate_worker_manifest(
        R"({"protocol_version":1,"job_id":"job-1","operation":{"name":"slice","version":2,"payload":{}}})");
    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "unsupported_operation_version"));
}
