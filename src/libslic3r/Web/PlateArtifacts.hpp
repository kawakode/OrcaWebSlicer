#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace Slic3r {
class Model;
struct GCodeProcessorResult;
} // namespace Slic3r

namespace Slic3r::Web {

inline constexpr int MESH_FORMAT_VERSION = 1;
inline constexpr int PREVIEW_FORMAT_VERSION = 1;

// The plates a project archive declares, for the inspection index: each
// plate's name and, per object and instance, the 1-based plate holding that
// instance, or 0 when it lies on no plate. Both are empty for a mesh.
struct InspectedPlates
{
    std::vector<std::string>           names;
    std::vector<std::vector<unsigned>> instance_plates;
};

// Display geometry for the browser plater. `mesh` receives every object's
// printable parts in object coordinates, little-endian:
//
//   "OWMESH01", uint32 object count, then per object:
//   uint32 vertex count, uint32 triangle count,
//   float32 xyz per vertex, uint32 vertex index triple per triangle.
//
// `inspection` receives the JSON index that names each object, its byte offset,
// its object-space bounds, and the instance transforms the file declared, as
// row-major 4x4 matrices in millimetres. A slice request that submits those
// matrices unchanged reproduces the file's own placement. A project's
// placements are relative to the plate each instance is on, which `plates`
// names alongside every instance.
bool write_model_inspection(const Model &model, bool declares_plate, const InspectedPlates &plates,
                            const std::string &model_format,
                            const std::filesystem::path &mesh, const std::filesystem::path &inspection,
                            std::string &error);

// Layer preview of produced G-code, derived from the engine's own G-code
// processor so no desktop viewer or OpenGL context is needed. `segments`
// receives fixed 36-byte little-endian records, grouped by layer:
//
//   float32 x0, y0, z0, x1, y1, z1, width, height,
//   uint8 extrusion role, uint8 tool, uint16 reserved.
//
// `index` receives the JSON document naming the role table, the tools used,
// and each layer's Z, byte offset, and segment count, so a client can page a
// large preview layer by layer. Only extrusion moves are recorded.
bool write_gcode_preview(const GCodeProcessorResult &result, const std::filesystem::path &segments,
                         const std::filesystem::path &index, std::string &error);

} // namespace Slic3r::Web
