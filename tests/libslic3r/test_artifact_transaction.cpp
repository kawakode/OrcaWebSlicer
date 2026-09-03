#include <catch2/catch_all.hpp>

#include "libslic3r/Web/ArtifactTransaction.hpp"

#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>

using namespace Slic3r::Web;

namespace {

class TemporaryDirectory
{
public:
    TemporaryDirectory()
    {
        static std::atomic_uint64_t counter {0};
        const auto timestamp = std::chrono::steady_clock::now().time_since_epoch().count();
        m_path = std::filesystem::temp_directory_path() /
                 ("orca-artifact-test-" + std::to_string(timestamp) + "-" + std::to_string(counter++));
        if (!std::filesystem::create_directory(m_path))
            throw std::runtime_error("Unable to create a temporary test directory.");
    }

    ~TemporaryDirectory()
    {
        std::error_code ignored;
        std::filesystem::remove_all(m_path, ignored);
    }

    const std::filesystem::path &path() const { return m_path; }

private:
    std::filesystem::path m_path;
};

void write_file(const std::filesystem::path &path, const std::string &contents)
{
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    REQUIRE(output.good());
    output << contents;
    output.close();
    REQUIRE(output.good());
}

std::string read_file(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary);
    REQUIRE(input.good());
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

std::filesystem::path canonical_job(const std::filesystem::path &path)
{
    WorkerManifestError error;
    std::filesystem::path canonical;
    REQUIRE(canonicalize_job_root(path, canonical, error));
    return canonical;
}

} // namespace

TEST_CASE("Artifact transaction publishes a nested output atomically", "[ArtifactTransaction]")
{
    TemporaryDirectory temporary;
    const std::filesystem::path job = temporary.path() / "job";
    REQUIRE(std::filesystem::create_directory(job));
    write_file(job / "model.stl", "solid model\nendsolid model\n");
    const std::filesystem::path root = canonical_job(job);

    WorkerManifestError error;
    std::filesystem::path input;
    REQUIRE(resolve_job_file(root, "model.stl", WorkerErrorCategory::Input, input, error));
    REQUIRE(input == std::filesystem::canonical(job / "model.stl"));

    std::optional<ArtifactTransaction> artifact =
        ArtifactTransaction::begin(root, "nested/output/model.gcode", "job-1", error);
    REQUIRE(artifact.has_value());
    REQUIRE_FALSE(std::filesystem::exists(artifact->target_path()));
    REQUIRE(artifact->temporary_path().parent_path() == std::filesystem::canonical(job / "nested/output"));

    write_file(artifact->temporary_path(), "G1 X1 Y1\n");
    REQUIRE_FALSE(std::filesystem::exists(artifact->target_path()));
    REQUIRE(artifact->commit(error));
    REQUIRE(read_file(artifact->target_path()) == "G1 X1 Y1\n");
    REQUIRE_FALSE(std::filesystem::exists(artifact->temporary_path()));
}

TEST_CASE("Artifact transaction preserves an existing target", "[ArtifactTransaction]")
{
    TemporaryDirectory temporary;
    const std::filesystem::path job = temporary.path() / "job";
    REQUIRE(std::filesystem::create_directory(job));
    write_file(job / "result.gcode", "existing\n");

    WorkerManifestError error;
    const std::optional<ArtifactTransaction> artifact =
        ArtifactTransaction::begin(canonical_job(job), "result.gcode", "job-2", error);
    REQUIRE_FALSE(artifact.has_value());
    REQUIRE(error.code == "artifact_exists");
    REQUIRE(read_file(job / "result.gcode") == "existing\n");
}

TEST_CASE("Artifact transaction removes an interrupted temporary output", "[ArtifactTransaction]")
{
    TemporaryDirectory temporary;
    const std::filesystem::path job = temporary.path() / "job";
    REQUIRE(std::filesystem::create_directory(job));
    std::filesystem::path partial;
    std::filesystem::path target;

    {
        WorkerManifestError error;
        std::optional<ArtifactTransaction> artifact =
            ArtifactTransaction::begin(canonical_job(job), "result.gcode", "job-3", error);
        REQUIRE(artifact.has_value());
        partial = artifact->temporary_path();
        target = artifact->target_path();
        write_file(partial, "partial\n");
    }

    REQUIRE_FALSE(std::filesystem::exists(partial));
    REQUIRE_FALSE(std::filesystem::exists(target));
}

TEST_CASE("Job paths reject symlinks that escape the root", "[ArtifactTransaction]")
{
    TemporaryDirectory temporary;
    const std::filesystem::path job = temporary.path() / "job";
    const std::filesystem::path outside = temporary.path() / "outside";
    REQUIRE(std::filesystem::create_directory(job));
    REQUIRE(std::filesystem::create_directory(outside));
    write_file(outside / "model.stl", "outside\n");

    std::error_code symlink_error;
    std::filesystem::create_directory_symlink(outside, job / "escape", symlink_error);
    if (symlink_error) {
        SUCCEED("Directory symlinks are unavailable on this filesystem.");
        return;
    }

    const std::filesystem::path root = canonical_job(job);
    WorkerManifestError error;
    std::filesystem::path input;
    REQUIRE_FALSE(resolve_job_file(root, "escape/model.stl", WorkerErrorCategory::Input, input, error));
    REQUIRE(error.code == "job_path_escape");

    const std::optional<ArtifactTransaction> artifact =
        ArtifactTransaction::begin(root, "escape/result.gcode", "job-4", error);
    REQUIRE_FALSE(artifact.has_value());
    REQUIRE(error.code == "artifact_path_escape");
    REQUIRE_FALSE(std::filesystem::exists(outside / "result.gcode"));
}
