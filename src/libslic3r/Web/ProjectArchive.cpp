#include "ProjectArchive.hpp"

#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/miniz_extension.hpp"

#include <algorithm>
#include <cmath>
#include <string>
#include <string_view>

namespace Slic3r::Web {
namespace {

constexpr std::size_t MAX_ARCHIVE_ENTRY_NAME_LENGTH = 1024;

// High nibble of the Unix mode stored in the upper half of the external
// attributes. Archives produced by DOS-style writers leave that half zero, so a
// symlink can only be claimed by a writer that recorded a Unix mode.
constexpr std::uint32_t UNIX_FILE_TYPE_MASK = 0xF000;
constexpr std::uint32_t UNIX_FILE_TYPE_SYMLINK = 0xA000;

bool is_symlink_entry(const mz_zip_archive_file_stat &stat)
{
    return ((stat.m_external_attr >> 16) & UNIX_FILE_TYPE_MASK) == UNIX_FILE_TYPE_SYMLINK;
}

WorkerManifestError input_error(const char *code, const char *message)
{
    return {code, message, WorkerErrorCategory::Input};
}

WorkerManifestError limit_error(const char *code, const char *message)
{
    return {code, message, WorkerErrorCategory::ResourceLimit};
}

// PartPlate.cpp keeps plates a fifth of a bed apart.
constexpr double PLATE_GAP = 1. / 5.;

// compute_colum_count() in PartPlate.hpp, including its float rounding.
int plate_columns(int count)
{
    const float value       = std::sqrt(static_cast<float>(count));
    const float round_value = std::round(value);
    return value > round_value ? static_cast<int>(round_value) + 1 : static_cast<int>(round_value);
}

} // namespace

Vec2d ProjectPlateGrid::origin(int plate) const
{
    return {(plate % columns) * stride.x(), -(plate / columns) * stride.y()};
}

int ProjectPlateGrid::plate_at(const Vec2d &point) const
{
    const Vec2d  relative = point - bed.center();
    const double column   = std::round(relative.x() / stride.x());
    const double row      = std::round(-relative.y() / stride.y());
    if (column < 0 || column >= columns || row < 0)
        return -1;
    const double plate = row * columns + column;
    return plate < plate_count ? static_cast<int>(plate) : -1;
}

ProjectPlateGrid project_plate_grid(const DynamicPrintConfig &config, int plate_count)
{
    ProjectPlateGrid grid;
    if (const ConfigOptionPoints *area = config.option<ConfigOptionPoints>("printable_area"); area && !area->values.empty())
        grid.bed = BoundingBoxf(area->values);
    else
        grid.bed = BoundingBoxf(Vec2d(0., 0.), Vec2d(200., 200.));
    // The desktop stores the plate size as whole millimetres.
    const Vec2d size = grid.bed.size();
    grid.stride      = Vec2d(std::max(1., std::trunc(size.x())), std::max(1., std::trunc(size.y()))) * (1. + PLATE_GAP);
    grid.plate_count = std::max(1, plate_count);
    grid.columns     = plate_columns(grid.plate_count);
    return grid;
}

bool is_contained_archive_entry_name(const std::string &name)
{
    if (name.empty() || name.size() > MAX_ARCHIVE_ENTRY_NAME_LENGTH)
        return false;
    if (name.front() == '/' || name.find('\\') != std::string::npos)
        return false;
    if (name.size() >= 2 && name[1] == ':')
        return false;
    if (std::any_of(name.begin(), name.end(), [](unsigned char character) { return character < 0x20 || character == 0x7F; }))
        return false;

    std::size_t begin = 0;
    while (begin <= name.size()) {
        const std::size_t end = std::min(name.find('/', begin), name.size());
        const std::string_view component(name.data() + begin, end - begin);
        // A trailing separator marks a directory entry and yields one empty
        // component, which is the only empty component we accept.
        if (component == "." || component == ".." || (component.empty() && end != name.size()))
            return false;
        begin = end + 1;
    }
    return true;
}

bool inspect_project_archive(const std::filesystem::path &archive_path, const ProjectArchiveLimits &limits,
                             ProjectArchiveInspection &inspection, WorkerManifestError &error)
{
    inspection = {};

    mz_zip_archive archive;
    mz_zip_zero_struct(&archive);
    if (!open_zip_reader(&archive, archive_path.string())) {
        error = input_error("archive_unreadable", "The project archive could not be opened as a ZIP container.");
        return false;
    }

    struct ArchiveReaderGuard
    {
        mz_zip_archive *archive;
        ~ArchiveReaderGuard() { close_zip_reader(archive); }
    } guard {&archive};

    const mz_uint entry_count = mz_zip_reader_get_num_files(&archive);
    if (entry_count == 0) {
        error = input_error("archive_empty", "The project archive contains no entries.");
        return false;
    }
    if (entry_count > limits.max_entries) {
        error = limit_error("archive_entry_count_limit_exceeded",
                            "The project archive exceeds the configured entry-count limit.");
        return false;
    }

    for (mz_uint index = 0; index < entry_count; ++index) {
        mz_zip_archive_file_stat stat;
        if (!mz_zip_reader_file_stat(&archive, index, &stat)) {
            error = input_error("archive_entry_unreadable", "A project archive entry could not be read.");
            return false;
        }
        if (is_symlink_entry(stat)) {
            error = input_error("archive_entry_unsafe", "The project archive contains a symbolic link entry.");
            return false;
        }
        if (!is_contained_archive_entry_name(decode_archive_entry_path(&archive, stat))) {
            error = input_error("archive_entry_unsafe",
                                "The project archive contains an entry whose path escapes the archive root.");
            return false;
        }
        if (stat.m_is_directory)
            continue;
        if (stat.m_uncomp_size > limits.max_entry_bytes) {
            error = limit_error("archive_entry_size_limit_exceeded",
                                "A project archive entry exceeds the configured entry size limit.");
            return false;
        }
        if (stat.m_uncomp_size >= limits.compression_ratio_floor_bytes && stat.m_comp_size > 0 &&
            stat.m_uncomp_size / stat.m_comp_size > limits.max_compression_ratio) {
            error = limit_error("archive_compression_ratio_limit_exceeded",
                                "A project archive entry exceeds the configured compression-ratio limit.");
            return false;
        }
        if (stat.m_uncomp_size > limits.max_extracted_bytes - inspection.extracted_bytes) {
            error = limit_error("archive_extracted_size_limit_exceeded",
                                "The project archive exceeds the configured extracted-content limit.");
            return false;
        }
        inspection.extracted_bytes += stat.m_uncomp_size;
        ++inspection.entry_count;
    }

    if (inspection.entry_count == 0) {
        error = input_error("archive_empty", "The project archive contains no file entries.");
        return false;
    }
    return true;
}

} // namespace Slic3r::Web
