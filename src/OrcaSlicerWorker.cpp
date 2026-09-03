#include "libslic3r/Web/SinglePlateSlice.hpp"
#include "libslic3r/Web/ArtifactTransaction.hpp"
#include "libslic3r/Web/WorkerManifest.hpp"
#include "libslic3r/Web/WorkerProtocol.hpp"
#include "libslic3r/Utils.hpp"
#include "libslic3r_version.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include <openssl/evp.h>

#ifdef _WIN32
#include <windows.h>
#include <psapi.h>
#else
#include <sys/resource.h>
#endif

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
    SliceFailed     = 5,
    Canceled        = 6,
    ResourceLimit   = 7,
    InternalError   = 8
};

class EventEmitter
{
public:
    explicit EventEmitter(std::string job_id) : m_job_id(std::move(job_id)) {}

    void state(Slic3r::Web::WorkerJobState state)
    {
        Slic3r::Web::WorkerEvent event;
        event.job_id = m_job_id;
        event.sequence = m_sequence++;
        event.type = Slic3r::Web::WorkerEventType::State;
        event.state = state;
        emit(event);
    }

    void progress(const Slic3r::Web::SinglePlateSliceProgress &progress)
    {
        const unsigned monotonic_percent = std::max(m_last_percent, std::min(progress.percent, 100u));
        if (monotonic_percent == m_last_percent && progress.stage == m_last_stage)
            return;
        m_last_percent = monotonic_percent;
        m_last_stage = progress.stage;
        Slic3r::Web::WorkerEvent event;
        event.job_id = m_job_id;
        event.sequence = m_sequence++;
        event.type = Slic3r::Web::WorkerEventType::Progress;
        event.progress = Slic3r::Web::WorkerProgress{progress.stage, monotonic_percent, progress.message};
        emit(event);
    }

    void warning(const Slic3r::Web::WorkerWarning &warning)
    {
        Slic3r::Web::WorkerEvent event;
        event.job_id = m_job_id;
        event.sequence = m_sequence++;
        event.type = Slic3r::Web::WorkerEventType::Warning;
        event.warning = warning;
        emit(event);
    }

    void error(const Slic3r::Web::WorkerManifestError &error)
    {
        Slic3r::Web::WorkerEvent event;
        event.job_id = m_job_id;
        event.sequence = m_sequence++;
        event.type = Slic3r::Web::WorkerEventType::Error;
        event.error = error;
        emit(event);
    }

    void artifact(const Slic3r::Web::WorkerArtifact &artifact)
    {
        Slic3r::Web::WorkerEvent event;
        event.job_id = m_job_id;
        event.sequence = m_sequence++;
        event.type = Slic3r::Web::WorkerEventType::Artifact;
        event.artifact = artifact;
        emit(event);
    }

private:
    static void emit(const Slic3r::Web::WorkerEvent &event)
    {
        std::cout << Slic3r::Web::serialize_worker_event(event) << '\n' << std::flush;
    }

    std::string   m_job_id;
    std::uint64_t m_sequence {0};
    unsigned      m_last_percent {0};
    std::string   m_last_stage;
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
            response["errors"].push_back({
                {"category", Slic3r::Web::worker_error_category_name(error.category)},
                {"code", error.code},
                {"message", error.message}
            });
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
        response["protocol_version"] = validation.manifest->protocol_version;
        response["job_id"]         = validation.manifest->job_id;
        response["operation"]      = validation.manifest->operation;
        response["operation_version"] = validation.manifest->operation_version;
    } else {
        response["errors"] = nlohmann::json::array();
        for (const Slic3r::Web::WorkerManifestError &error : validation.errors)
            response["errors"].push_back({
                {"category", Slic3r::Web::worker_error_category_name(error.category)},
                {"code", error.code},
                {"message", error.message}
            });
    }
    return response;
}

std::uintmax_t peak_memory_bytes()
{
#ifdef _WIN32
    PROCESS_MEMORY_COUNTERS counters {};
    if (GetProcessMemoryInfo(GetCurrentProcess(), &counters, sizeof(counters)) != 0)
        return counters.PeakWorkingSetSize;
    return 0;
#else
    rusage usage {};
    if (getrusage(RUSAGE_SELF, &usage) != 0)
        return 0;
#ifdef __APPLE__
    return static_cast<std::uintmax_t>(usage.ru_maxrss);
#else
    return static_cast<std::uintmax_t>(usage.ru_maxrss) * 1024;
#endif
#endif
}

std::string sha256_file(const std::filesystem::path &path, std::string &error)
{
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        error = "Unable to read the generated artifact.";
        return {};
    }

    EVP_MD_CTX *context = EVP_MD_CTX_new();
    if (context == nullptr || EVP_DigestInit_ex(context, EVP_sha256(), nullptr) != 1) {
        EVP_MD_CTX_free(context);
        error = "Unable to initialize artifact hashing.";
        return {};
    }

    std::array<char, 64 * 1024> buffer {};
    while (input) {
        input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const std::streamsize count = input.gcount();
        if (count > 0 && EVP_DigestUpdate(context, buffer.data(), static_cast<std::size_t>(count)) != 1) {
            EVP_MD_CTX_free(context);
            error = "Unable to hash the generated artifact.";
            return {};
        }
    }
    if (!input.eof()) {
        EVP_MD_CTX_free(context);
        error = "Unable to read the complete generated artifact.";
        return {};
    }

    std::array<unsigned char, EVP_MAX_MD_SIZE> digest {};
    unsigned int digest_size = 0;
    if (EVP_DigestFinal_ex(context, digest.data(), &digest_size) != 1) {
        EVP_MD_CTX_free(context);
        error = "Unable to finish artifact hashing.";
        return {};
    }
    EVP_MD_CTX_free(context);

    std::ostringstream encoded;
    encoded << std::hex << std::setfill('0');
    for (unsigned int index = 0; index < digest_size; ++index)
        encoded << std::setw(2) << static_cast<unsigned>(digest[index]);
    return encoded.str();
}

bool write_result(const std::filesystem::path &job_root, const Slic3r::Web::WorkerResult &result, std::string &error)
{
    Slic3r::Web::WorkerManifestError artifact_error;
    std::optional<Slic3r::Web::ArtifactTransaction> artifact =
        Slic3r::Web::ArtifactTransaction::begin(job_root, "result.json", result.job_id, artifact_error);
    if (!artifact) {
        error = artifact_error.message;
        return false;
    }

    std::ofstream output(artifact->temporary_path(), std::ios::binary | std::ios::trunc);
    if (!output) {
        error = "Unable to create the temporary result file.";
        return false;
    }
    output << Slic3r::Web::serialize_worker_result(result) << '\n';
    output.close();
    if (!output) {
        error = "Unable to write the complete result file.";
        return false;
    }
    if (!artifact->commit(artifact_error)) {
        error = artifact_error.message;
        return false;
    }
    return true;
}

void finish_timing(Slic3r::Web::WorkerResult &result,
                   const std::chrono::steady_clock::time_point &started, std::clock_t cpu_started)
{
    result.timing.duration_ms = static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - started).count());
    result.timing.cpu_time_ms = static_cast<std::uint64_t>(
        1000.0 * static_cast<double>(std::clock() - cpu_started) / static_cast<double>(CLOCKS_PER_SEC));
    result.resource_usage.peak_memory_bytes = peak_memory_bytes();
}

} // namespace

int main(int argc, char **argv)
{
    if (argc == 2 && std::string(argv[1]) == "--version") {
        std::cout << "orca-slicer-worker " << SLIC3R_VERSION
                  << " (worker protocol " << Slic3r::Web::WORKER_PROTOCOL_VERSION << ")\n";
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
        const auto started = std::chrono::steady_clock::now();
        const std::clock_t cpu_started = std::clock();
        std::string contents;
        std::string error;
        if (!read_manifest(argv[2], contents, error)) {
            std::cerr << error << '\n';
            return static_cast<int>(ExitCode::ManifestIoError);
        }

        const Slic3r::Web::WorkerManifestValidation envelope_validation =
            Slic3r::Web::validate_worker_manifest(contents);
        if (!envelope_validation.is_valid()) {
            std::cerr << request_validation_response({std::nullopt, envelope_validation.errors}).dump() << '\n';
            return static_cast<int>(ExitCode::InvalidManifest);
        }

        const std::string &job_id = envelope_validation.manifest->job_id;
        EventEmitter emitter(job_id);
        emitter.state(Slic3r::Web::WorkerJobState::Accepted);

        Slic3r::Web::WorkerResult final_result;
        final_result.job_id = job_id;

        const std::filesystem::path manifest_path = std::filesystem::absolute(argv[2]);
        std::filesystem::path job_root;
        Slic3r::Web::WorkerManifestError job_root_error;
        if (!Slic3r::Web::canonicalize_job_root(manifest_path.parent_path(), job_root, job_root_error)) {
            emitter.error(job_root_error);
            emitter.state(Slic3r::Web::WorkerJobState::Failed);
            return static_cast<int>(ExitCode::InternalError);
        }

        const Slic3r::Web::SinglePlateSliceRequestValidation validation =
            Slic3r::Web::validate_single_plate_slice_request(contents);
        if (!validation.is_valid()) {
            for (const Slic3r::Web::WorkerManifestError &request_error : validation.errors)
                emitter.error(request_error);
            final_result.outcome = Slic3r::Web::WorkerJobState::Failed;
            final_result.error = validation.errors.front();
            finish_timing(final_result, started, cpu_started);
            if (!write_result(job_root, final_result, error))
                std::cerr << error << '\n';
            emitter.state(Slic3r::Web::WorkerJobState::Failed);
            return static_cast<int>(ExitCode::InvalidManifest);
        }

        if (!initialize_resources(argv[0], error)) {
            const Slic3r::Web::WorkerManifestError resource_error {
                "resources_not_found", error, Slic3r::Web::WorkerErrorCategory::Internal
            };
            emitter.error(resource_error);
            final_result.outcome = Slic3r::Web::WorkerJobState::Failed;
            final_result.error = resource_error;
            finish_timing(final_result, started, cpu_started);
            if (!write_result(job_root, final_result, error))
                std::cerr << error << '\n';
            emitter.state(Slic3r::Web::WorkerJobState::Failed);
            return static_cast<int>(ExitCode::InternalError);
        }

        emitter.state(Slic3r::Web::WorkerJobState::Running);
        std::vector<Slic3r::Web::WorkerWarning> warnings;
        Slic3r::Web::SinglePlateSliceCallbacks callbacks;
        callbacks.progress = [&emitter](const Slic3r::Web::SinglePlateSliceProgress &progress) {
            emitter.progress(progress);
        };
        callbacks.warning = [&emitter, &warnings](const std::string &message) {
            Slic3r::Web::WorkerWarning warning {"slicing_warning", message};
            warnings.push_back(warning);
            emitter.warning(warning);
        };
        const Slic3r::Web::SinglePlateSliceResult result =
            Slic3r::Web::slice_single_plate(*validation.request, job_root, callbacks);
        final_result.warnings = std::move(warnings);
        if (result.success) {
            const std::filesystem::path artifact_path = job_root / validation.request->output_gcode;
            const std::string sha256 = sha256_file(artifact_path, error);
            if (sha256.empty()) {
                std::error_code ignored;
                std::filesystem::remove(artifact_path, ignored);
                const Slic3r::Web::WorkerManifestError hash_error {
                    "artifact_hash_failed", error, Slic3r::Web::WorkerErrorCategory::Internal
                };
                emitter.error(hash_error);
                final_result.outcome = Slic3r::Web::WorkerJobState::Failed;
                final_result.error = hash_error;
            } else {
                Slic3r::Web::WorkerArtifact artifact {
                    "gcode", validation.request->output_gcode, std::filesystem::file_size(artifact_path), sha256
                };
                final_result.outcome = Slic3r::Web::WorkerJobState::Succeeded;
                final_result.artifacts.push_back(artifact);
                emitter.artifact(artifact);
            }
        } else {
            const Slic3r::Web::WorkerManifestError slice_error {
                result.code, result.message, result.category
            };
            emitter.error(slice_error);
            final_result.outcome = Slic3r::Web::WorkerJobState::Failed;
            final_result.error = slice_error;
        }

        finish_timing(final_result, started, cpu_started);
        if (!write_result(job_root, final_result, error)) {
            if (final_result.outcome == Slic3r::Web::WorkerJobState::Succeeded) {
                std::error_code ignored;
                std::filesystem::remove(job_root / validation.request->output_gcode, ignored);
            }
            const Slic3r::Web::WorkerManifestError result_error {
                "result_publish_failed", error, Slic3r::Web::WorkerErrorCategory::Internal
            };
            emitter.error(result_error);
            emitter.state(Slic3r::Web::WorkerJobState::Failed);
            return static_cast<int>(ExitCode::InternalError);
        }

        emitter.state(final_result.outcome);
        return static_cast<int>(final_result.outcome == Slic3r::Web::WorkerJobState::Succeeded ?
            ExitCode::Success : ExitCode::SliceFailed);
    }

    print_usage(std::cerr);
    return static_cast<int>(ExitCode::UsageError);
}
