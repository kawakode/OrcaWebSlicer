#pragma once

#include "WorkerManifest.hpp"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string>

namespace Slic3r::Web {

// Bounds applied to a project archive before any entry is decompressed. The
// importer streams entries out of the archive itself, so these are checked
// against the central directory first and a rejected archive is never opened by
// the importer at all.
struct ProjectArchiveLimits
{
    std::uintmax_t max_extracted_bytes {1024ull * 1024 * 1024};
    std::uintmax_t max_entry_bytes {1024ull * 1024 * 1024};
    std::size_t    max_entries {4096};
    // An entry may not claim to expand by more than this factor once it is
    // larger than compression_ratio_floor_bytes, which keeps small, highly
    // compressible XML entries from tripping the check.
    std::uintmax_t max_compression_ratio {1000};
    std::uintmax_t compression_ratio_floor_bytes {1024 * 1024};
};

struct ProjectArchiveInspection
{
    std::size_t    entry_count {0};
    std::uintmax_t extracted_bytes {0};
};

// Rejects archives whose central directory declares entries that escape the
// extraction root, are symbolic links, or exceed the configured extraction
// bounds. Returns false and fills error with a stable input or resource-limit
// failure.
bool inspect_project_archive(const std::filesystem::path &archive_path, const ProjectArchiveLimits &limits,
                             ProjectArchiveInspection &inspection, WorkerManifestError &error);

// Exposed for tests: the entry-name containment rule applied to every archive
// entry. Rejects absolute paths, drive letters, backslashes, parent traversal,
// and control characters.
bool is_contained_archive_entry_name(const std::string &name);

} // namespace Slic3r::Web
