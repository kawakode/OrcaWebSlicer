#pragma once

#include "WorkerManifest.hpp"

#include <filesystem>
#include <optional>
#include <string>
#include <string_view>

namespace Slic3r::Web {

bool canonicalize_job_root(const std::filesystem::path &job_root, std::filesystem::path &canonical_root,
                           WorkerManifestError &error);

bool resolve_job_file(const std::filesystem::path &canonical_root, std::string_view relative_path,
                      WorkerErrorCategory category, std::filesystem::path &resolved_path,
                      WorkerManifestError &error);

bool remove_abandoned_artifacts(const std::filesystem::path &canonical_root, std::string_view job_id,
                                WorkerManifestError &error);

class ArtifactTransaction
{
public:
    ArtifactTransaction(const ArtifactTransaction &) = delete;
    ArtifactTransaction &operator=(const ArtifactTransaction &) = delete;
    ArtifactTransaction(ArtifactTransaction &&other) noexcept;
    ArtifactTransaction &operator=(ArtifactTransaction &&other) noexcept;
    ~ArtifactTransaction();

    static std::optional<ArtifactTransaction> begin(const std::filesystem::path &canonical_root,
                                                    std::string_view relative_path, std::string_view job_id,
                                                    WorkerManifestError &error);

    const std::filesystem::path &temporary_path() const { return m_temporary_path; }
    const std::filesystem::path &target_path() const { return m_target_path; }

    bool commit(WorkerManifestError &error);

private:
    ArtifactTransaction(std::filesystem::path target_path, std::filesystem::path temporary_path);
    void remove_temporary() noexcept;

    std::filesystem::path m_target_path;
    std::filesystem::path m_temporary_path;
    bool                  m_active {true};
};

} // namespace Slic3r::Web
