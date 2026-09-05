#include <catch2/catch_all.hpp>

#include "libslic3r/Web/ProjectArchive.hpp"
#include "libslic3r/miniz_extension.hpp"
#include "test_utils.hpp"

#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

using namespace Slic3r;
using namespace Slic3r::Web;

namespace {

struct ArchiveEntry
{
    std::string name;
    std::string contents;
};

void write_archive(const std::filesystem::path &path, const std::vector<ArchiveEntry> &entries)
{
    mz_zip_archive archive;
    mz_zip_zero_struct(&archive);
    REQUIRE(open_zip_writer(&archive, path.string()));
    for (const ArchiveEntry &entry : entries)
        REQUIRE(mz_zip_writer_add_mem(&archive, entry.name.c_str(), entry.contents.data(), entry.contents.size(),
                                      MZ_BEST_COMPRESSION));
    // close_zip_writer only releases the writer; the central directory has to be
    // written first or the archive has no readable index.
    REQUIRE(mz_zip_writer_finalize_archive(&archive));
    REQUIRE(close_zip_writer(&archive));
}

std::string read_bytes(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary);
    REQUIRE(input.good());
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

void write_bytes(const std::filesystem::path &path, const std::string &bytes)
{
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    REQUIRE(output.good());
    output.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    output.close();
    REQUIRE(output.good());
}

// miniz's writer cannot record Unix external attributes, so the last central
// directory record is rewritten in place to describe a symbolic link.
void mark_last_entry_as_symlink(const std::filesystem::path &path)
{
    std::string bytes = read_bytes(path);
    const std::size_t record = bytes.rfind(std::string("PK\x01\x02", 4));
    REQUIRE(record != std::string::npos);
    // Version made by: host 3 (Unix) lives in the high byte.
    bytes[record + 5] = static_cast<char>(3);
    // External attributes: mode 0120777 (S_IFLNK | 0777) in the high half.
    bytes[record + 38] = static_cast<char>(0x00);
    bytes[record + 39] = static_cast<char>(0x00);
    bytes[record + 40] = static_cast<char>(0xFF);
    bytes[record + 41] = static_cast<char>(0xA1);
    write_bytes(path, bytes);
}

const std::string UNICODE_ENTRY = "Metadata/\xe3\x83\x97\xe3\x83\xac\xe3\x83\xbc\xe3\x83\x88.json";

} // namespace

TEST_CASE("Archive entry names reject paths that escape the archive root", "[ProjectArchive]")
{
    CHECK(is_contained_archive_entry_name("3D/3dmodel.model"));
    CHECK(is_contained_archive_entry_name("Metadata/"));
    CHECK(is_contained_archive_entry_name(UNICODE_ENTRY));

    CHECK_FALSE(is_contained_archive_entry_name(""));
    CHECK_FALSE(is_contained_archive_entry_name("/etc/passwd"));
    CHECK_FALSE(is_contained_archive_entry_name("../3dmodel.model"));
    CHECK_FALSE(is_contained_archive_entry_name("3D/../../3dmodel.model"));
    CHECK_FALSE(is_contained_archive_entry_name("3D/./3dmodel.model"));
    CHECK_FALSE(is_contained_archive_entry_name("C:/3dmodel.model"));
    CHECK_FALSE(is_contained_archive_entry_name("3D\\3dmodel.model"));
    CHECK_FALSE(is_contained_archive_entry_name("3D/model\n.model"));
    CHECK_FALSE(is_contained_archive_entry_name("3D//3dmodel.model"));
}

TEST_CASE("Project archive inspection accepts a contained archive", "[ProjectArchive]")
{
    const ScopedTemporaryDir directory("orca-archive");
    const std::filesystem::path archive = std::filesystem::path(directory.string()) / "project.3mf";
    write_archive(archive, {{"[Content_Types].xml", "<Types/>"}, {"3D/3dmodel.model", "<model/>"}, {UNICODE_ENTRY, "{}"}});

    ProjectArchiveInspection inspection;
    WorkerManifestError error;
    REQUIRE(inspect_project_archive(archive, {}, inspection, error));
    CHECK(inspection.entry_count == 3);
    CHECK(inspection.extracted_bytes == 18);
}

TEST_CASE("Project archive inspection rejects an unreadable container", "[ProjectArchive]")
{
    const ScopedTemporaryDir directory("orca-archive");
    const std::filesystem::path archive = std::filesystem::path(directory.string()) / "broken.3mf";
    write_bytes(archive, "this is not a zip archive");

    ProjectArchiveInspection inspection;
    WorkerManifestError error;
    REQUIRE_FALSE(inspect_project_archive(archive, {}, inspection, error));
    CHECK(error.code == "archive_unreadable");
    CHECK(error.category == WorkerErrorCategory::Input);
}

TEST_CASE("Project archive inspection rejects escaping entries", "[ProjectArchive]")
{
    const ScopedTemporaryDir directory("orca-archive");
    const std::filesystem::path archive = std::filesystem::path(directory.string()) / "escape.3mf";
    write_archive(archive, {{"3D/3dmodel.model", "<model/>"}, {"../escaped.config", "escaped"}});

    ProjectArchiveInspection inspection;
    WorkerManifestError error;
    REQUIRE_FALSE(inspect_project_archive(archive, {}, inspection, error));
    CHECK(error.code == "archive_entry_unsafe");
    CHECK(error.category == WorkerErrorCategory::Input);
}

TEST_CASE("Project archive inspection rejects symbolic link entries", "[ProjectArchive]")
{
    const ScopedTemporaryDir directory("orca-archive");
    const std::filesystem::path archive = std::filesystem::path(directory.string()) / "symlink.3mf";
    write_archive(archive, {{"3D/3dmodel.model", "/etc/passwd"}});
    mark_last_entry_as_symlink(archive);

    ProjectArchiveInspection inspection;
    WorkerManifestError error;
    REQUIRE_FALSE(inspect_project_archive(archive, {}, inspection, error));
    CHECK(error.code == "archive_entry_unsafe");
}

TEST_CASE("Project archive inspection enforces the extracted-content limit", "[ProjectArchive]")
{
    const ScopedTemporaryDir directory("orca-archive");
    const std::filesystem::path archive = std::filesystem::path(directory.string()) / "large.3mf";
    write_archive(archive,
                  {{"3D/3dmodel.model", std::string(4096, 'a')}, {"Metadata/plate.json", std::string(4096, 'b')}});

    ProjectArchiveLimits limits;
    limits.max_extracted_bytes = 6000;
    ProjectArchiveInspection inspection;
    WorkerManifestError error;
    REQUIRE_FALSE(inspect_project_archive(archive, limits, inspection, error));
    CHECK(error.code == "archive_extracted_size_limit_exceeded");
    CHECK(error.category == WorkerErrorCategory::ResourceLimit);
}

TEST_CASE("Project archive inspection enforces the entry size limit", "[ProjectArchive]")
{
    const ScopedTemporaryDir directory("orca-archive");
    const std::filesystem::path archive = std::filesystem::path(directory.string()) / "entry.3mf";
    write_archive(archive, {{"3D/3dmodel.model", std::string(4096, 'a')}});

    ProjectArchiveLimits limits;
    limits.max_entry_bytes = 1024;
    ProjectArchiveInspection inspection;
    WorkerManifestError error;
    REQUIRE_FALSE(inspect_project_archive(archive, limits, inspection, error));
    CHECK(error.code == "archive_entry_size_limit_exceeded");
}

TEST_CASE("Project archive inspection rejects decompression bombs", "[ProjectArchive]")
{
    const ScopedTemporaryDir directory("orca-archive");
    const std::filesystem::path archive = std::filesystem::path(directory.string()) / "bomb.3mf";
    write_archive(archive, {{"3D/3dmodel.model", std::string(256 * 1024, '\0')}});

    ProjectArchiveLimits limits;
    limits.compression_ratio_floor_bytes = 1024;
    limits.max_compression_ratio = 2;
    ProjectArchiveInspection inspection;
    WorkerManifestError error;
    REQUIRE_FALSE(inspect_project_archive(archive, limits, inspection, error));
    CHECK(error.code == "archive_compression_ratio_limit_exceeded");
    CHECK(error.category == WorkerErrorCategory::ResourceLimit);
}

TEST_CASE("Project archive inspection enforces the entry count limit", "[ProjectArchive]")
{
    const ScopedTemporaryDir directory("orca-archive");
    const std::filesystem::path archive = std::filesystem::path(directory.string()) / "many.3mf";
    std::vector<ArchiveEntry> entries;
    for (unsigned index = 0; index < 8; ++index)
        entries.push_back({"Metadata/entry_" + std::to_string(index) + ".json", "{}"});
    write_archive(archive, entries);

    ProjectArchiveLimits limits;
    limits.max_entries = 4;
    ProjectArchiveInspection inspection;
    WorkerManifestError error;
    REQUIRE_FALSE(inspect_project_archive(archive, limits, inspection, error));
    CHECK(error.code == "archive_entry_count_limit_exceeded");
}
