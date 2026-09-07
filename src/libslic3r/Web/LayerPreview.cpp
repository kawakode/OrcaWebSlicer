#include "LayerPreview.hpp"

#include "ArtifactTransaction.hpp"

#include "libslic3r/ExtrusionEntity.hpp"
#include "libslic3r/GCode/GCodeProcessor.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <fstream>
#include <limits>
#include <map>
#include <set>
#include <vector>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

// The preview's own role vocabulary. Only the stable id is declared here: the
// label is the engine's own `role_to_string`, which is also the text the G-code
// comment carries, so the two can never drift apart.
struct RoleName
{
    ExtrusionRole role;
    const char   *id;
};

constexpr RoleName ROLE_NAMES[] = {
    {erNone, "none"},
    {erPerimeter, "inner_wall"},
    {erExternalPerimeter, "outer_wall"},
    {erOverhangPerimeter, "overhang_wall"},
    {erInternalInfill, "sparse_infill"},
    {erSolidInfill, "internal_solid_infill"},
    {erTopSolidInfill, "top_surface"},
    {erBottomSurface, "bottom_surface"},
    {erIroning, "ironing"},
    {erBridgeInfill, "bridge"},
    {erInternalBridgeInfill, "internal_bridge"},
    {erGapFill, "gap_infill"},
    {erSkirt, "skirt"},
    {erBrim, "brim"},
    {erSupportMaterial, "support"},
    {erSupportMaterialInterface, "support_interface"},
    {erSupportTransition, "support_transition"},
    {erWipeTower, "prime_tower"},
    {erCustom, "custom"},
    {erMixed, "multiple"},
};

const RoleName &role_name(ExtrusionRole role)
{
    for (const RoleName &named : ROLE_NAMES)
        if (named.role == role)
            return named;
    return ROLE_NAMES[0];
}

constexpr std::uint8_t KIND_EXTRUDE = 0;
constexpr std::uint8_t KIND_TRAVEL = 1;
// Header: kind, role, tool, reserved, point count, width, height.
constexpr std::size_t BATCH_HEADER_BYTES = 16;
constexpr std::size_t POINT_BYTES = 12;

std::int32_t quantize(float millimetres)
{
    return static_cast<std::int32_t>(std::lround(static_cast<double>(millimetres) / LAYER_PREVIEW_QUANTUM_MM));
}

// Width and height only set a rendered ribbon's size, so they are rounded to
// 0.01 mm before they take part in batching. Exact float equality would split
// one wall into hundreds of batches.
float quantize_extent(float millimetres)
{
    return std::round(millimetres * 100.0f) / 100.0f;
}

void append(std::vector<char> &blob, const void *bytes, std::size_t size)
{
    const char *source = static_cast<const char *>(bytes);
    blob.insert(blob.end(), source, source + size);
}

template<typename T> void append_scalar(std::vector<char> &blob, T value)
{
    append(blob, &value, sizeof(value));
}

// Coordinates within 10 nm are the same point: consecutive moves share the
// vertex between them, and a zero-length move draws nothing.
bool same_point(const Vec3f &left, const Vec3f &right)
{
    return (left - right).isZero(1e-5f);
}

struct Batch
{
    std::uint8_t kind {KIND_EXTRUDE};
    std::uint8_t role {0};
    std::uint8_t tool {0};
    float        width {0.f};
    float        height {0.f};
    std::vector<Vec3f> points;

    bool continues(std::uint8_t next_kind, std::uint8_t next_role, std::uint8_t next_tool, float next_width,
                   float next_height, const Vec3f &from) const
    {
        return !points.empty() && kind == next_kind && role == next_role && tool == next_tool &&
               width == next_width && height == next_height && same_point(points.back(), from);
    }
};

struct Layer
{
    unsigned          id {0};
    float             z {0.f};
    bool              seen {false};
    std::vector<char> blob;
    std::set<std::uint8_t> roles;
    std::set<std::uint8_t> tools;
    std::size_t       segments {0};
    bool              has_extrusion {false};
};

void flush(Layer &layer, Batch &batch)
{
    if (batch.points.size() < 2) {
        batch.points.clear();
        return;
    }
    layer.blob.reserve(layer.blob.size() + BATCH_HEADER_BYTES + batch.points.size() * POINT_BYTES);
    append_scalar<std::uint8_t>(layer.blob, batch.kind);
    append_scalar<std::uint8_t>(layer.blob, batch.role);
    append_scalar<std::uint8_t>(layer.blob, batch.tool);
    append_scalar<std::uint8_t>(layer.blob, 0);
    append_scalar<std::uint32_t>(layer.blob, static_cast<std::uint32_t>(batch.points.size()));
    append_scalar<float>(layer.blob, batch.width);
    append_scalar<float>(layer.blob, batch.height);
    for (const Vec3f &point : batch.points) {
        append_scalar<std::int32_t>(layer.blob, quantize(point.x()));
        append_scalar<std::int32_t>(layer.blob, quantize(point.y()));
        append_scalar<std::int32_t>(layer.blob, quantize(point.z()));
    }
    layer.segments += batch.points.size() - 1;
    if (batch.kind == KIND_EXTRUDE) {
        layer.roles.insert(batch.role);
        layer.tools.insert(batch.tool);
        layer.has_extrusion = true;
    }
    batch.points.clear();
}

bool commit_file(const std::filesystem::path &job_root, const std::string &relative, const std::string &job_id,
                 const char *bytes, std::size_t size, std::uintmax_t &written, WorkerManifestError &error)
{
    std::optional<ArtifactTransaction> artifact =
        ArtifactTransaction::begin(job_root, relative, job_id, error);
    if (!artifact)
        return false;
    std::ofstream output(artifact->temporary_path(), std::ios::binary | std::ios::trunc);
    if (output)
        output.write(bytes, static_cast<std::streamsize>(size));
    if (!output) {
        error = {"preview_write_failed", "The layer preview could not be written.", WorkerErrorCategory::Internal};
        return false;
    }
    output.close();
    if (!output) {
        error = {"preview_write_failed", "The layer preview could not be written.", WorkerErrorCategory::Internal};
        return false;
    }
    if (!artifact->commit(error))
        return false;
    written = size;
    return true;
}

} // namespace

bool write_layer_preview(const GCodeProcessorResult &moves, const std::filesystem::path &job_root,
                         const std::string &index_path, const std::string &data_path,
                         const std::string &job_id, std::optional<std::uintmax_t> max_preview_bytes,
                         LayerPreviewResult &result, WorkerManifestError &error)
{
    std::map<unsigned, Layer> layers;
    Batch                     batch;
    unsigned                  batch_layer = 0;
    std::uintmax_t            total_bytes = 0;
    Vec3f                     minimum = Vec3f::Constant(std::numeric_limits<float>::max());
    Vec3f                     maximum = Vec3f::Constant(std::numeric_limits<float>::lowest());
    bool                      any_geometry = false;

    for (std::size_t index = 1; index < moves.moves.size(); ++index) {
        const GCodeProcessorResult::MoveVertex &move = moves.moves[index];
        const std::uint8_t kind = move.type == EMoveType::Extrude ? KIND_EXTRUDE
                                  : move.type == EMoveType::Travel ? KIND_TRAVEL
                                                                   : 0xff;
        if (kind == 0xff)
            continue;
        const Vec3f &from = moves.moves[index - 1].position;
        if (same_point(from, move.position))
            continue;

        const std::uint8_t role = kind == KIND_EXTRUDE
                                      ? static_cast<std::uint8_t>(&role_name(move.extrusion_role) - ROLE_NAMES)
                                      : 0;
        const float width = kind == KIND_EXTRUDE ? quantize_extent(move.width) : 0.f;
        const float height = kind == KIND_EXTRUDE ? quantize_extent(move.height) : 0.f;

        Layer &layer = layers[move.layer_id];
        if (!layer.seen) {
            layer.id = move.layer_id;
            layer.seen = true;
        }
        // A layer's z is the highest height it actually extrudes at, taken from
        // the move's own position: a travel may be hopped, and a spiral vase
        // rises continuously, so only extrusion defines the layer.
        if (kind == KIND_EXTRUDE && (!layer.has_extrusion || move.position.z() > layer.z))
            layer.z = move.position.z();

        if (batch_layer != move.layer_id ||
            !batch.continues(kind, role, move.extruder_id, width, height, from)) {
            if (layers.count(batch_layer) != 0)
                flush(layers[batch_layer], batch);
            batch = Batch{kind, role, move.extruder_id, width, height, {from}};
            batch_layer = move.layer_id;
            total_bytes += BATCH_HEADER_BYTES + POINT_BYTES;
        }
        batch.points.push_back(move.position);

        if (kind == KIND_EXTRUDE) {
            minimum = minimum.cwiseMin(from).cwiseMin(move.position);
            maximum = maximum.cwiseMax(from).cwiseMax(move.position);
            any_geometry = true;
            layer.has_extrusion = true;
        }

        total_bytes += POINT_BYTES;
        if (max_preview_bytes && total_bytes > *max_preview_bytes) {
            result.omitted_reason = "The layer preview would exceed the configured preview size limit.";
            return true;
        }
    }
    if (layers.count(batch_layer) != 0)
        flush(layers[batch_layer], batch);

    // A layer with no extrusion is start-up or shutdown motion, not a printed
    // layer, so it never appears in the preview's layer list.
    std::vector<const Layer *> printed;
    for (const auto &[id, layer] : layers)
        if (layer.has_extrusion)
            printed.push_back(&layer);
    if (printed.empty() || !any_geometry) {
        result.omitted_reason = "The G-code contains no extrusion to preview.";
        return true;
    }
    // Printing by object repeats a z across objects, so the layer id breaks the
    // tie and the order stays the order the G-code prints in.
    std::stable_sort(printed.begin(), printed.end(), [](const Layer *left, const Layer *right) {
        return left->z != right->z ? left->z < right->z : left->id < right->id;
    });

    nlohmann::json described_layers = nlohmann::json::array();
    std::vector<char> data;
    std::set<std::uint8_t> tools;
    std::size_t segments = 0;
    for (std::size_t index = 0; index < printed.size(); ++index) {
        const Layer &layer = *printed[index];
        nlohmann::json roles = nlohmann::json::array();
        for (std::uint8_t role : layer.roles)
            roles.push_back(ROLE_NAMES[role].id);
        nlohmann::json layer_tools = nlohmann::json::array();
        for (std::uint8_t tool : layer.tools) {
            layer_tools.push_back(tool);
            tools.insert(tool);
        }
        described_layers.push_back({
            {"index", index},
            {"z", layer.z},
            {"offset", data.size()},
            {"length", layer.blob.size()},
            {"segments", layer.segments},
            {"roles", roles},
            {"tools", layer_tools},
        });
        segments += layer.segments;
        data.insert(data.end(), layer.blob.begin(), layer.blob.end());
    }

    nlohmann::json roles = nlohmann::json::array();
    for (const RoleName &named : ROLE_NAMES)
        roles.push_back({{"id", named.id}, {"label", ExtrusionEntity::role_to_string(named.role)}});
    nlohmann::json described_tools = nlohmann::json::array();
    for (std::uint8_t tool : tools)
        described_tools.push_back(tool);

    const std::string index_document = nlohmann::json{
        {"preview_version", LAYER_PREVIEW_VERSION},
        {"units", "mm"},
        {"quantum_mm", LAYER_PREVIEW_QUANTUM_MM},
        {"data", data_path},
        {"data_bytes", data.size()},
        {"segment_count", segments},
        {"tools", described_tools},
        {"roles", roles},
        {"bounding_box", {{"min", {minimum.x(), minimum.y(), minimum.z()}},
                          {"max", {maximum.x(), maximum.y(), maximum.z()}}}},
        {"layers", described_layers},
    }.dump();

    if (!commit_file(job_root, data_path, job_id, data.data(), data.size(), result.data_bytes, error))
        return false;
    if (!commit_file(job_root, index_path, job_id, index_document.data(), index_document.size(),
                     result.index_bytes, error))
        return false;
    result.written = true;
    result.layer_count = printed.size();
    return true;
}

} // namespace Slic3r::Web
