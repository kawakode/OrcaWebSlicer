#pragma once

#include "WorkerManifest.hpp"

#include <array>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace Slic3r {
class Model;
class DynamicPrintConfig;
} // namespace Slic3r

namespace Slic3r::Web {

// Mirrors the desktop plate-count ceiling so an out-of-range selection is
// rejected while parsing instead of after opening the archive. Shared with
// SceneExport's `inspect` operation, which selects a plate the same way.
inline constexpr std::uint64_t MAX_PLATE_INDEX = 36;

// One explicit placement the browser asked the worker to slice instead of
// arranging. `source_object` indexes `scene.json`'s `objects` array, i.e. the
// order the shared importer below fills `Model::objects` in; the same
// `source_object` may repeat to request a duplicate copy.
struct SliceObjectTransform
{
    std::uint32_t source_object {0};
    // Column-major 4x4 matrix in millimetres, the same layout SceneExport
    // publishes and the browser edits.
    std::array<double, 16> transform {};
};

struct SinglePlateSliceRequest
{
    WorkerManifest envelope;
    std::string input_model;
    std::string output_gcode;
    // Empty when no layer preview is wanted. The binary companion is this path
    // with a `.bin` extension, so one request field names both files.
    std::string output_preview;
    std::string machine_profile;
    std::string process_profile;
    std::string filament_profile;
    std::vector<std::pair<std::string, std::string>> settings;
    // 1-based plate selection inside a project archive. Mesh inputs describe a
    // single implicit plate and ignore it.
    unsigned plate_index {1};
    // Empty means "arrange the imported objects", exactly as before this field
    // existed. Non-empty replaces every imported object with one explicitly
    // placed copy per entry, so the sliced placement always matches what the
    // browser displayed.
    std::vector<SliceObjectTransform> objects;
    std::optional<std::uintmax_t> max_input_bytes;
    std::optional<std::uintmax_t> max_triangles;
    std::optional<std::uint64_t> max_wall_time_ms;
    std::optional<std::uintmax_t> max_memory_bytes;
    std::optional<std::uintmax_t> max_output_bytes;
    std::optional<std::uintmax_t> max_extracted_bytes;
    std::optional<std::uintmax_t> max_preview_bytes;
};

struct SinglePlateSliceRequestValidation
{
    std::optional<SinglePlateSliceRequest> request;
    std::vector<WorkerManifestError> errors;

    bool is_valid() const { return request.has_value() && errors.empty(); }
};

struct SinglePlateSliceArtifact
{
    std::string kind;
    std::string path;
};

struct SinglePlateSliceResult
{
    bool success {false};
    std::string code;
    std::string message;
    WorkerErrorCategory category {WorkerErrorCategory::Slicing};
    // Every file this run committed, in publication order. The caller hashes
    // and records them, and removes exactly these if the job is abandoned.
    std::vector<SinglePlateSliceArtifact> artifacts;
};

struct SinglePlateSliceProgress
{
    std::string stage;
    unsigned    percent {0};
    std::string message;
};

struct SinglePlateSliceCallbacks
{
    std::function<void(const SinglePlateSliceProgress &)> progress;
    std::function<void(const std::string &)> warning;
    std::function<bool()> cancellation_requested;
    std::function<std::uintmax_t()> memory_usage_bytes;
};

SinglePlateSliceRequestValidation validate_single_plate_slice_request(std::string_view serialized);
SinglePlateSliceResult slice_single_plate(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                          const SinglePlateSliceCallbacks &callbacks = {});

// --- Shared with SceneExport's `inspect` operation -------------------------
//
// `inspect` reads model geometry the same way `slice` does, so the worker
// never has two importers to keep in sync. These are exposed here rather than
// duplicated because they are the parts of slice_single_plate's own pipeline
// that inspect also needs.

// True when a manifest string field is a relative path with no drive letter,
// backslash, or `.`/`..` component. Applied to every path a request names,
// whether an input model, an output artifact, or a profile.
bool is_safe_relative_path(const std::string &value);

// Lowercased filesystem extension, e.g. ".stl". Used to route input formats
// and to validate output artifact names.
std::string lowercase_extension(const std::string &value);

// Loads a model through exactly the importer slice_single_plate uses: STL and
// OBJ meshes through their format loaders, and a 3MF's selected plate through
// the core project importer, with the same archive-containment and
// single-plate rules. Every ModelObject ends up with at least one instance.
// `declares_plate` is set only when a project archive stores real plate
// placement, which callers use to decide whether to arrange or keep the
// imported placement. `project_config`, when non-null, receives the plate's
// embedded configuration; pass nullptr when the caller only wants geometry
// (inspect never slices, so it never needs the project's printer settings).
bool import_single_plate_model(const std::filesystem::path &input_path, const std::string &extension,
                               unsigned plate_index, std::optional<std::uintmax_t> max_extracted_bytes,
                               Model &model, bool &declares_plate, DynamicPrintConfig *project_config,
                               WorkerManifestError &error);

// Sums facet counts across every object, failing with the same
// `triangle_limit_exceeded` resource-limit error slice_single_plate and
// export_scene both report.
bool count_model_triangles(const Model &model, std::optional<std::uintmax_t> max_triangles,
                           std::uintmax_t &triangle_count, WorkerManifestError &error);

// Resolves one profile JSON file inside the job root and applies it onto
// `config`. Shared by slice_single_plate's machine/process/filament chain and
// export_scene's single optional machine profile.
bool apply_profile_file(const std::filesystem::path &job_root, const std::string &relative_path,
                        WorkerErrorCategory category, DynamicPrintConfig &config, WorkerManifestError &error);

} // namespace Slic3r::Web
