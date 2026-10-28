#pragma once

#include "WorkerManifest.hpp"

#include "libslic3r/BoundingBox.hpp"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string>

namespace Slic3r {
class DynamicPrintConfig;
}

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

// Where the desktop puts each plate of a project. Its plate list lays every
// plate out on one shared coordinate system, in a grid whose plates sit one bed
// plus a fifth apart, and a project archive stores instance placements in those
// shared coordinates. This mirrors that layout so the worker can move each
// plate's objects back onto its own bed without the desktop plate list.
struct ProjectPlateGrid
{
    // Printable-area bounds of the first plate.
    BoundingBoxf bed;
    Vec2d        stride {0., 0.};
    int          columns {1};
    int          plate_count {1};

    // Offset of the 0-based plate from the first one.
    Vec2d origin(int plate) const;
    // The 0-based plate whose bed is centred nearest to the point, or -1 when
    // the point lies beyond every plate, as an object off the plates does.
    int plate_at(const Vec2d &point) const;
};

// The layout of `plate_count` plates for the printer `config` describes, as the
// desktop computes it from the archive's own printable area.
ProjectPlateGrid project_plate_grid(const DynamicPrintConfig &config, int plate_count);

// Exposed for tests: the entry-name containment rule applied to every archive
// entry. Rejects absolute paths, drive letters, backslashes, parent traversal,
// and control characters.
bool is_contained_archive_entry_name(const std::string &name);

} // namespace Slic3r::Web
