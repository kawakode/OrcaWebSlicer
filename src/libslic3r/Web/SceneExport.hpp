#pragma once

#include "WorkerManifest.hpp"

#include <cstdint>
#include <filesystem>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace Slic3r::Web {

inline constexpr int INSPECT_OPERATION_VERSION = 1;

// The `scene_version` field inside scene.json, documented in
// docs/web/scene-format.md. Independent of INSPECT_OPERATION_VERSION, exactly
// as LAYER_PREVIEW_VERSION is independent of SLICE_OPERATION_VERSION.
inline constexpr int SCENE_VERSION = 1;

// Millimetres per quantized integer unit in scene.bin, the same quantum
// LayerPreview uses for the same reason: a bed several times larger than any
// this MVP supports still fits a signed 32-bit value with room to spare.
inline constexpr double SCENE_QUANTUM_MM = 0.001;

struct SceneExportRequest
{
    WorkerManifest envelope;
    std::string input_model;
    std::string output_scene;
    // Empty when the request supplies no machine profile: bed shape and
    // printable height then come from the engine's own defaults.
    std::string machine_profile;
    // 1-based plate selection inside a project archive. Mesh inputs describe a
    // single implicit plate and ignore it.
    unsigned plate_index {1};
    std::optional<std::uintmax_t> max_input_bytes;
    std::optional<std::uintmax_t> max_triangles;
    std::optional<std::uintmax_t> max_scene_bytes;
};

struct SceneExportRequestValidation
{
    std::optional<SceneExportRequest> request;
    std::vector<WorkerManifestError> errors;

    bool is_valid() const { return request.has_value() && errors.empty(); }
};

struct SceneExportArtifact
{
    std::string kind;
    std::string path;
};

struct SceneExportResult
{
    bool success {false};
    std::string code;
    std::string message;
    WorkerErrorCategory category {WorkerErrorCategory::Input};
    // Every file this run committed, in publication order. The caller hashes
    // and records them, and removes exactly these if the job is abandoned.
    std::vector<SceneExportArtifact> artifacts;
};

struct SceneExportProgress
{
    std::string stage;
    unsigned    percent {0};
    std::string message;
};

struct SceneExportCallbacks
{
    std::function<void(const SceneExportProgress &)> progress;
    std::function<bool()> cancellation_requested;
};

// Validates only the stable job envelope for the `inspect` operation, exactly
// as validate_worker_manifest does for `slice`. Exposed so the worker CLI can
// emit an Accepted event carrying the job ID before the operation-specific
// payload is validated, the same two-step sequence `--slice-manifest` uses.
//
// validate_worker_manifest itself is not reused here because it hardcodes
// operation.name == "slice"; this mirrors its envelope checks and stable error
// codes for "inspect" instead of changing a file this operation does not own.
WorkerManifestValidation validate_inspect_envelope(std::string_view serialized);

SceneExportRequestValidation validate_scene_export_request(std::string_view serialized);

// Loads a model through the same importer slice_single_plate uses and
// publishes the compact scene described in docs/web/scene-format.md: a small
// JSON index (`scene.json`) naming a bed, a per-object bounding box and
// placement transform, and a binary blob (`scene.bin`) of quantized local-frame
// triangle-soup vertices. Never arranges or sinks the imported placement: the
// browser plater is what edits placement, so inspect reports it as imported.
SceneExportResult export_scene(const SceneExportRequest &request, const std::filesystem::path &job_root,
                               const SceneExportCallbacks &callbacks = {});

} // namespace Slic3r::Web
