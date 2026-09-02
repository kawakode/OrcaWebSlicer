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
        "schema_version": 1,
        "job_id": "job-2026.09_02",
        "operation": "slice",
        "future_field": {"allowed": true}
    })");

    REQUIRE(validation.is_valid());
    REQUIRE(validation.manifest->schema_version == WORKER_MANIFEST_SCHEMA_VERSION);
    REQUIRE(validation.manifest->job_id == "job-2026.09_02");
    REQUIRE(validation.manifest->operation == "slice");
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

TEST_CASE("Worker manifest rejects missing and unsupported schema versions", "[WorkerManifest]")
{
    const WorkerManifestValidation missing = validate_worker_manifest(R"({"job_id":"job-1","operation":"slice"})");
    REQUIRE_FALSE(missing.is_valid());
    REQUIRE(has_error(missing, "invalid_schema_version"));

    const WorkerManifestValidation unsupported =
        validate_worker_manifest(R"({"schema_version":2,"job_id":"job-1","operation":"slice"})");
    REQUIRE_FALSE(unsupported.is_valid());
    REQUIRE(has_error(unsupported, "unsupported_schema_version"));
}

TEST_CASE("Worker manifest rejects unsafe job identifiers", "[WorkerManifest]")
{
    const WorkerManifestValidation path =
        validate_worker_manifest(R"({"schema_version":1,"job_id":"../job","operation":"slice"})");
    REQUIRE_FALSE(path.is_valid());
    REQUIRE(has_error(path, "invalid_job_id"));

    const WorkerManifestValidation empty =
        validate_worker_manifest(R"({"schema_version":1,"job_id":"","operation":"slice"})");
    REQUIRE_FALSE(empty.is_valid());
    REQUIRE(has_error(empty, "invalid_job_id"));
}

TEST_CASE("Worker manifest rejects unsupported operations", "[WorkerManifest]")
{
    const WorkerManifestValidation validation =
        validate_worker_manifest(R"({"schema_version":1,"job_id":"job-1","operation":"render"})");
    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "unsupported_operation"));
}
