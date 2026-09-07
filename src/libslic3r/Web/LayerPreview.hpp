#pragma once

#include "WorkerManifest.hpp"

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>

namespace Slic3r {
struct GCodeProcessorResult;
}

namespace Slic3r::Web {

inline constexpr int LAYER_PREVIEW_VERSION = 1;

// Micrometres. Every coordinate in the binary blob is an integer count of these,
// which keeps a 400 mm bed inside a signed 32-bit value with room to spare.
inline constexpr double LAYER_PREVIEW_QUANTUM_MM = 0.001;

struct LayerPreviewResult
{
    bool           written {false};
    std::uintmax_t index_bytes {0};
    std::uintmax_t data_bytes {0};
    std::size_t    layer_count {0};
    // Set when the preview was skipped rather than failed, so the caller can
    // warn without losing an otherwise good slice.
    std::string    omitted_reason;
};

// Turns the toolpaths the G-code exporter already produced into the two-file
// preview described in docs/web/preview-format.md: a small JSON index and a
// binary blob of quantized polylines the API can serve one layer at a time.
//
// The moves come from the same `GCodeProcessorResult` the exporter filled, so
// nothing is re-parsed and no desktop viewer, OpenGL context, or framebuffer is
// involved. Returns false only on a real failure; a preview that would exceed
// `max_preview_bytes` is reported as omitted, because the G-code is still good.
bool write_layer_preview(const GCodeProcessorResult &moves, const std::filesystem::path &job_root,
                         const std::string &index_path, const std::string &data_path,
                         const std::string &job_id, std::optional<std::uintmax_t> max_preview_bytes,
                         LayerPreviewResult &result, WorkerManifestError &error);

} // namespace Slic3r::Web
