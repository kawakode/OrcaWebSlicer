#pragma once

#include "FacetPaint.hpp"
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
}

namespace Slic3r::Web {

enum class WorkerOperation {
    // Slice one plate into G-code and, when asked, a layer preview.
    Slice,
    // Load one model and export its display geometry for the plater.
    Inspect,
    // Save the composed plate as a desktop-compatible project archive.
    ExportProject,
    // Apply one object tool, such as a planar cut or a mesh repair, and
    // publish the resulting objects as new models.
    Edit
};

using SerializedSettings = std::vector<std::pair<std::string, std::string>>;

// Settings that apply to one object between two heights above its bottom, as
// the desktop's height range modifiers do.
struct PlateLayerRange
{
    double             min_z {0.};
    double             max_z {0.};
    SerializedSettings settings;
};

// The filament one volume of an object is printed with, overriding the
// object's own, as the desktop's object list assigns it per part.
struct PlateVolumeExtruder
{
    unsigned volume {0};
    unsigned extruder {1};
};

// One object of an uploaded model file, placed on the plate by the browser.
// Each instance is a row-major 4x4 matrix in millimetres that replaces the
// placement the file declared; the worker neither arranges nor centers it.
// Its settings override the plate's for this object only. `extruder` is the
// 1-based filament the object is printed with, 0 to keep the file's choice.
struct PlateObject
{
    std::string                         model;
    unsigned                            object_index {0};
    std::vector<std::array<double, 16>> instances;
    SerializedSettings                  settings;
    std::vector<PlateLayerRange>        layer_ranges;
    unsigned                            extruder {0};
    std::vector<PlateVolumeExtruder>    volume_extruders;
    // Painted faces of the object's parts, replacing the file's own painting
    // of each kind a volume names.
    std::vector<VolumePaint>            paint;
};

// One filament of a multi-material job: a flattened filament profile and the
// overrides that apply to that filament only.
struct JobFilament
{
    std::string        profile;
    SerializedSettings settings;
};

// The tool an edit applies to one object of its input model. The object is
// first placed with `transform`, a row-major 4x4 matrix in millimetres, so the
// plane and the produced models are all in plate coordinates.
struct ModelEdit
{
    enum class Tool { Cut, Repair };

    Tool                   tool {Tool::Repair};
    unsigned               object_index {0};
    std::array<double, 16> transform {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
    // Cut only: a point on the plane and its normal, which points at the upper part.
    std::array<double, 3>  plane_point {0, 0, 0};
    std::array<double, 3>  plane_normal {0, 0, 1};
    bool                   keep_upper {true};
    bool                   keep_lower {true};
    // Turn the upper part upside down, as the desktop cut tool offers.
    bool                   flip_upper {false};
    // Lay each part on its cut face, which matters only for a tilted plane.
    bool                   place_on_cut {false};
};

struct SinglePlateSliceRequest
{
    WorkerManifest envelope;
    WorkerOperation operation {WorkerOperation::Slice};
    // Exactly one of input_model and objects names a slice's geometry. A model
    // given alone is arranged; explicit objects keep their submitted placement.
    std::string input_model;
    std::vector<PlateObject> objects;
    std::string output_gcode;
    // Optional preview of the produced G-code, as a segment file and its index.
    std::string output_preview;
    std::string output_preview_index;
    // Required by inspection: the display mesh and its JSON index.
    std::string output_mesh;
    std::string output_inspection;
    // Required by a project export: the 3MF archive to write.
    std::string output_project;
    // Embed rendered thumbnails in the G-code, at the sizes the printer's
    // `thumbnails` setting asks for. Off by default, as in G4.
    bool gcode_thumbnails {false};
    // Required by an edit: the tool, the paths its models may be written to in
    // order, and its JSON report.
    ModelEdit edit;
    std::vector<std::string> output_models;
    std::string output_report;
    std::string machine_profile;
    std::string process_profile;
    std::string filament_profile;
    // A multi-material job's filaments, in extruder order; it then names no
    // `filament_profile`. Empty for a single-filament job.
    std::vector<JobFilament> filaments;
    SerializedSettings settings;
    // The front left corner of the plate's prime tower in millimetres, which
    // the desktop stores per plate rather than in a profile.
    std::optional<std::array<double, 2>> wipe_tower;
    // The 1-based filaments in the order the first layer prints them; empty
    // leaves the order to the engine.
    std::vector<unsigned> filament_order;
    // 1-based plate of a project archive. An `input_model` slice keeps only
    // that plate's objects; an `objects` slice takes that plate's own settings
    // from the first archive it uses. Mesh inputs describe a single implicit
    // plate and ignore it.
    unsigned plate_index {1};
    std::optional<std::uintmax_t> max_input_bytes;
    std::optional<std::uintmax_t> max_triangles;
    std::optional<std::uint64_t> max_wall_time_ms;
    std::optional<std::uintmax_t> max_memory_bytes;
    std::optional<std::uintmax_t> max_output_bytes;
    std::optional<std::uintmax_t> max_extracted_bytes;
};

struct SinglePlateSliceRequestValidation
{
    std::optional<SinglePlateSliceRequest> request;
    std::vector<WorkerManifestError> errors;

    bool is_valid() const { return request.has_value() && errors.empty(); }
};

struct SinglePlateSliceResult
{
    bool success {false};
    std::string code;
    std::string message;
    WorkerErrorCategory category {WorkerErrorCategory::Slicing};
    // The setting a validation failure names, when the engine reports one.
    std::string setting;
    // On success, the committed artifacts as (kind, relative path) pairs.
    std::vector<std::pair<std::string, std::string>> artifacts;
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
SinglePlateSliceResult inspect_model(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                     const SinglePlateSliceCallbacks &callbacks = {});
// Loads every plate of `input_model` under the request's size, archive, and triangle limits,
// giving every object at least one instance, as inspections and edits need.
SinglePlateSliceResult load_input_model(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                        Model &model);

} // namespace Slic3r::Web
