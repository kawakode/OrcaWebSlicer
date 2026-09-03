#include "ArtifactTransaction.hpp"

#include <system_error>
#include <utility>

namespace Slic3r::Web {
namespace {

void set_error(WorkerManifestError &error, const char *code, const char *message, WorkerErrorCategory category)
{
    error = {code, message, category};
}

bool is_safe_relative_path(std::string_view value)
{
    if (value.empty() || value.size() > 512 || value.find('\\') != std::string_view::npos ||
        value.find(':') != std::string_view::npos)
        return false;

    const std::filesystem::path path(value);
    if (path.is_absolute() || path.has_root_path())
        return false;

    for (const std::filesystem::path &component : path)
        if (component.empty() || component == "." || component == "..")
            return false;
    return true;
}

bool is_contained_by(const std::filesystem::path &root, const std::filesystem::path &candidate)
{
    auto candidate_it = candidate.begin();
    for (auto root_it = root.begin(); root_it != root.end(); ++root_it, ++candidate_it)
        if (candidate_it == candidate.end() || *candidate_it != *root_it)
            return false;
    return true;
}

bool path_is_absent(const std::filesystem::path &path, std::error_code &error)
{
    const std::filesystem::file_status status = std::filesystem::symlink_status(path, error);
    if (status.type() != std::filesystem::file_type::not_found)
        return false;
    error.clear();
    return true;
}

} // namespace

bool canonicalize_job_root(const std::filesystem::path &job_root, std::filesystem::path &canonical_root,
                           WorkerManifestError &error)
{
    std::error_code filesystem_error;
    canonical_root = std::filesystem::canonical(job_root, filesystem_error);
    if (filesystem_error || !std::filesystem::is_directory(canonical_root, filesystem_error)) {
        set_error(error, "invalid_job_root", "The job root does not exist or is not a directory.",
                  WorkerErrorCategory::Internal);
        return false;
    }
    return true;
}

bool resolve_job_file(const std::filesystem::path &canonical_root, std::string_view relative_path,
                      WorkerErrorCategory category, std::filesystem::path &resolved_path,
                      WorkerManifestError &error)
{
    if (!is_safe_relative_path(relative_path)) {
        set_error(error, "invalid_job_path", "A job file path is not a safe relative path.", category);
        return false;
    }

    std::error_code filesystem_error;
    resolved_path = std::filesystem::canonical(canonical_root / std::filesystem::path(relative_path), filesystem_error);
    if (filesystem_error) {
        set_error(error, "job_file_not_found", "A required job file does not exist or is not a regular file.", category);
        return false;
    }
    if (!is_contained_by(canonical_root, resolved_path)) {
        set_error(error, "job_path_escape", "A job file path resolves outside the job root.", category);
        return false;
    }
    if (!std::filesystem::is_regular_file(resolved_path, filesystem_error) || filesystem_error) {
        set_error(error, "job_file_not_found", "A required job file does not exist or is not a regular file.", category);
        return false;
    }
    return true;
}

bool remove_abandoned_artifacts(const std::filesystem::path &canonical_root, std::string_view job_id,
                                WorkerManifestError &error)
{
    const std::string suffix = "." + std::string(job_id) + ".partial";
    std::error_code filesystem_error;
    std::filesystem::recursive_directory_iterator entry(canonical_root, filesystem_error);
    const std::filesystem::recursive_directory_iterator end;
    while (!filesystem_error && entry != end) {
        const std::filesystem::path &path = entry->path();
        const std::string filename = path.filename().string();
        const std::filesystem::file_status status = entry->symlink_status(filesystem_error);
        if (filesystem_error)
            break;

        if (status.type() == std::filesystem::file_type::regular && filename.size() > suffix.size() &&
            filename.front() == '.' && filename.compare(filename.size() - suffix.size(), suffix.size(), suffix) == 0) {
            std::filesystem::remove(path, filesystem_error);
            if (filesystem_error)
                break;
        }
        entry.increment(filesystem_error);
    }

    if (filesystem_error) {
        set_error(error, "artifact_cleanup_failed", "Unable to remove abandoned temporary artifacts.",
                  WorkerErrorCategory::Internal);
        return false;
    }
    return true;
}

ArtifactTransaction::ArtifactTransaction(std::filesystem::path target_path, std::filesystem::path temporary_path)
    : m_target_path(std::move(target_path)), m_temporary_path(std::move(temporary_path))
{
}

ArtifactTransaction::ArtifactTransaction(ArtifactTransaction &&other) noexcept
    : m_target_path(std::move(other.m_target_path)), m_temporary_path(std::move(other.m_temporary_path)),
      m_active(other.m_active)
{
    other.m_active = false;
}

ArtifactTransaction &ArtifactTransaction::operator=(ArtifactTransaction &&other) noexcept
{
    if (this != &other) {
        remove_temporary();
        m_target_path = std::move(other.m_target_path);
        m_temporary_path = std::move(other.m_temporary_path);
        m_active = other.m_active;
        other.m_active = false;
    }
    return *this;
}

ArtifactTransaction::~ArtifactTransaction()
{
    remove_temporary();
}

std::optional<ArtifactTransaction> ArtifactTransaction::begin(const std::filesystem::path &canonical_root,
                                                              std::string_view relative_path, std::string_view job_id,
                                                              WorkerManifestError &error)
{
    if (!is_safe_relative_path(relative_path)) {
        set_error(error, "invalid_artifact_path", "An artifact path is not a safe relative path.",
                  WorkerErrorCategory::Validation);
        return std::nullopt;
    }

    const std::filesystem::path requested_path = canonical_root / std::filesystem::path(relative_path);
    std::error_code filesystem_error;
    const std::filesystem::path resolved_parent = std::filesystem::weakly_canonical(requested_path.parent_path(), filesystem_error);
    if (filesystem_error || !is_contained_by(canonical_root, resolved_parent)) {
        set_error(error, "artifact_path_escape", "The artifact path resolves outside the job root.",
                  WorkerErrorCategory::Validation);
        return std::nullopt;
    }

    std::filesystem::create_directories(resolved_parent, filesystem_error);
    if (filesystem_error) {
        set_error(error, "artifact_directory_failed", "Unable to create the artifact directory.",
                  WorkerErrorCategory::Internal);
        return std::nullopt;
    }

    const std::filesystem::path canonical_parent = std::filesystem::canonical(resolved_parent, filesystem_error);
    if (filesystem_error || !is_contained_by(canonical_root, canonical_parent)) {
        set_error(error, "artifact_path_escape", "The artifact path resolves outside the job root.",
                  WorkerErrorCategory::Validation);
        return std::nullopt;
    }

    const std::filesystem::path target_path = canonical_parent / requested_path.filename();
    if (!path_is_absent(target_path, filesystem_error)) {
        set_error(error, "artifact_exists", "The artifact target already exists.", WorkerErrorCategory::Validation);
        return std::nullopt;
    }

    const std::string temporary_name = "." + requested_path.filename().string() + "." + std::string(job_id) + ".partial";
    const std::filesystem::path temporary_path = canonical_parent / temporary_name;
    if (!path_is_absent(temporary_path, filesystem_error)) {
        set_error(error, "artifact_temporary_exists", "The temporary artifact target already exists.",
                  WorkerErrorCategory::Validation);
        return std::nullopt;
    }

    return ArtifactTransaction(target_path, temporary_path);
}

bool ArtifactTransaction::commit(WorkerManifestError &error)
{
    if (!m_active) {
        set_error(error, "artifact_transaction_closed", "The artifact transaction is already closed.",
                  WorkerErrorCategory::Internal);
        return false;
    }

    std::error_code filesystem_error;
    const std::filesystem::file_status temporary_status = std::filesystem::symlink_status(m_temporary_path, filesystem_error);
    if (filesystem_error || temporary_status.type() != std::filesystem::file_type::regular) {
        set_error(error, "artifact_temporary_invalid", "The temporary artifact is missing or is not a regular file.",
                  WorkerErrorCategory::Internal);
        return false;
    }
    if (!path_is_absent(m_target_path, filesystem_error)) {
        set_error(error, "artifact_exists", "The artifact target already exists.", WorkerErrorCategory::Validation);
        return false;
    }

    std::filesystem::rename(m_temporary_path, m_target_path, filesystem_error);
    if (filesystem_error) {
        set_error(error, "artifact_publish_failed", "Unable to publish the artifact atomically.",
                  WorkerErrorCategory::Internal);
        return false;
    }
    m_active = false;
    return true;
}

void ArtifactTransaction::remove_temporary() noexcept
{
    if (!m_active || m_temporary_path.empty())
        return;
    std::error_code ignored;
    std::filesystem::remove(m_temporary_path, ignored);
    m_active = false;
}

} // namespace Slic3r::Web
