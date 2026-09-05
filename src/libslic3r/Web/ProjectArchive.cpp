#include "ProjectArchive.hpp"

#include "libslic3r/miniz_extension.hpp"

#include <algorithm>
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

} // namespace

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
