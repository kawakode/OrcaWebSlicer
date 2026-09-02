#include "libslic3r/Web/SinglePlateSlice.hpp"
#include "libslic3r/Web/WorkerManifest.hpp"
#include "libslic3r/Utils.hpp"
#include "libslic3r_version.h"

#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

// libslic3r owns SVG parsing APIs, while NanoSVG's header-only implementation
// is instantiated once by each native executable that links those APIs.
#define NANOSVG_IMPLEMENTATION
#include "nanosvg/nanosvg.h"

namespace {

constexpr std::uintmax_t MAX_MANIFEST_SIZE = 1024 * 1024;

enum class ExitCode : int {
    Success         = 0,
    UsageError      = 2,
    ManifestIoError = 3,
    InvalidManifest = 4,
    SliceFailed     = 5
};

void print_usage(std::ostream &output)
{
    output << "Usage:\n"
           << "  orca-slicer-worker --version\n"
           << "  orca-slicer-worker --validate-manifest <path>\n"
           << "  orca-slicer-worker --slice-manifest <path>\n";
}

nlohmann::json request_validation_response(const Slic3r::Web::SinglePlateSliceRequestValidation &validation)
{
    nlohmann::json response = {{"valid", validation.is_valid()}};
    if (!validation.is_valid()) {
        response["errors"] = nlohmann::json::array();
        for (const Slic3r::Web::WorkerManifestError &error : validation.errors)
            response["errors"].push_back({{"code", error.code}, {"message", error.message}});
    }
    return response;
}

bool initialize_resources(const char *executable, std::string &error)
{
    std::vector<std::filesystem::path> candidates;
    if (const char *configured = std::getenv("ORCA_SLICER_RESOURCES"); configured != nullptr && *configured != '\0')
        candidates.emplace_back(configured);
    candidates.emplace_back(std::filesystem::current_path() / "resources");

    std::filesystem::path ancestor = std::filesystem::absolute(executable).parent_path();
    for (int depth = 0; depth < 6 && !ancestor.empty(); ++depth) {
        candidates.emplace_back(ancestor / "resources");
        ancestor = ancestor.parent_path();
    }

    for (const std::filesystem::path &candidate : candidates) {
        if (std::filesystem::is_regular_file(candidate / "info" / "nozzle_info.json")) {
            Slic3r::set_resources_dir(candidate.string());
            return true;
        }
    }

    error = "Unable to locate OrcaSlicer resources. Set ORCA_SLICER_RESOURCES to the resources directory.";
    return false;
}

bool read_manifest(const std::string &path, std::string &contents, std::string &error)
{
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) {
        error = "Unable to open manifest file.";
        return false;
    }

    const std::streampos end = input.tellg();
    if (end < 0) {
        error = "Unable to determine manifest size.";
        return false;
    }
    if (static_cast<std::uintmax_t>(end) > MAX_MANIFEST_SIZE) {
        error = "Manifest exceeds the 1 MiB limit.";
        return false;
    }

    contents.resize(static_cast<std::size_t>(end));
    input.seekg(0, std::ios::beg);
    if (!contents.empty() && !input.read(contents.data(), static_cast<std::streamsize>(contents.size()))) {
        error = "Unable to read manifest file.";
        return false;
    }
    return true;
}

nlohmann::json validation_response(const Slic3r::Web::WorkerManifestValidation &validation)
{
    nlohmann::json response = {{"valid", validation.is_valid()}};
    if (validation.is_valid()) {
        response["schema_version"] = validation.manifest->schema_version;
        response["job_id"]         = validation.manifest->job_id;
        response["operation"]      = validation.manifest->operation;
    } else {
        response["errors"] = nlohmann::json::array();
        for (const Slic3r::Web::WorkerManifestError &error : validation.errors)
            response["errors"].push_back({{"code", error.code}, {"message", error.message}});
    }
    return response;
}

} // namespace

int main(int argc, char **argv)
{
    if (argc == 2 && std::string(argv[1]) == "--version") {
        std::cout << "orca-slicer-worker " << SLIC3R_VERSION
                  << " (manifest protocol " << Slic3r::Web::WORKER_MANIFEST_SCHEMA_VERSION << ")\n";
        return static_cast<int>(ExitCode::Success);
    }

    if (argc == 3 && std::string(argv[1]) == "--validate-manifest") {
        std::string contents;
        std::string error;
        if (!read_manifest(argv[2], contents, error)) {
            std::cerr << error << '\n';
            return static_cast<int>(ExitCode::ManifestIoError);
        }

        const Slic3r::Web::WorkerManifestValidation validation =
            Slic3r::Web::validate_worker_manifest(contents);
        std::cout << validation_response(validation).dump() << '\n';
        return static_cast<int>(validation.is_valid() ? ExitCode::Success : ExitCode::InvalidManifest);
    }

    if (argc == 3 && std::string(argv[1]) == "--slice-manifest") {
        std::string contents;
        std::string error;
        if (!read_manifest(argv[2], contents, error)) {
            std::cerr << error << '\n';
            return static_cast<int>(ExitCode::ManifestIoError);
        }

        const Slic3r::Web::SinglePlateSliceRequestValidation validation =
            Slic3r::Web::validate_single_plate_slice_request(contents);
        if (!validation.is_valid()) {
            std::cout << request_validation_response(validation).dump() << '\n';
            return static_cast<int>(ExitCode::InvalidManifest);
        }

        if (!initialize_resources(argv[0], error)) {
            std::cout << nlohmann::json({
                {"success", false},
                {"job_id", validation.request->envelope.job_id},
                {"error", {{"code", "resources_not_found"}, {"message", error}}}
            }).dump() << '\n';
            return static_cast<int>(ExitCode::SliceFailed);
        }

        const std::filesystem::path manifest_path = std::filesystem::absolute(argv[2]);
        const Slic3r::Web::SinglePlateSliceResult result =
            Slic3r::Web::slice_single_plate(*validation.request, manifest_path.parent_path());
        nlohmann::json response = {
            {"success", result.success},
            {"job_id", validation.request->envelope.job_id}
        };
        if (result.success) {
            response["artifacts"] = {{{"kind", "gcode"}, {"path", validation.request->output_gcode}}};
        } else {
            response["error"] = {{"code", result.code}, {"message", result.message}};
        }
        std::cout << response.dump() << '\n';
        return static_cast<int>(result.success ? ExitCode::Success : ExitCode::SliceFailed);
    }

    print_usage(std::cerr);
    return static_cast<int>(ExitCode::UsageError);
}
