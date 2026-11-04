#pragma once

#include <array>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace Slic3r {
class ModelVolume;
}

namespace Slic3r::Web {

// What a face of a model part is painted with, as the desktop's painting
// gizmos store it: support enforcers and blockers, seam enforcers and blockers,
// filaments, and fuzzy skin.
enum class PaintKind { Supports, Seam, Color, FuzzySkin };

inline constexpr std::array<PaintKind, 4> PAINT_KINDS {PaintKind::Supports, PaintKind::Seam, PaintKind::Color,
                                                       PaintKind::FuzzySkin};

// The request and inspection field naming a kind: "supports", "seam",
// "color", or "fuzzy_skin".
const char *paint_kind_name(PaintKind kind);

// The highest state a face may carry: 2 (blocker) for supports and seams,
// 16 filaments for colours, and 1 for fuzzy skin.
int max_paint_state(PaintKind kind);

// One painted triangle: its index in the volume's mesh and its code, the
// hexadecimal string a desktop 3MF stores in that triangle's paint attribute.
using PaintedTriangle = std::pair<int, std::string>;

// The painting of one kind on one volume, decoded from its wire form.
struct PaintLayer
{
    std::vector<PaintedTriangle> triangles;
    // The highest state any face of the layer carries, 0 when none is painted.
    int highest_state {0};
};

// The painting a request gives one volume. A kind left out keeps whatever the
// model file painted; a kind given replaces it, so an empty one clears it.
struct VolumePaint
{
    unsigned                                volume {0};
    std::array<std::optional<PaintLayer>, 4> layers;
};

// Decodes the wire form of a paint layer: runs separated by `;`, each the
// 0-based index of its first triangle, a `:`, and one code per consecutive
// triangle separated by `,`, where an empty code leaves a triangle unpainted:
//
//   "0:4,4,,8;120:0C"
//
// Runs are in increasing order and never overlap. Each code is checked to be a
// complete split tree of the desktop's TriangleSelector encoding whose leaves
// stay within the kind's states, so the engine never reads past a code.
std::optional<PaintLayer> decode_paint_layer(std::string_view encoded, PaintKind kind, std::string &error);

// Encodes a volume's painting of one kind in the same form, empty when the
// volume carries none.
std::string encode_paint_layer(const ModelVolume &volume, PaintKind kind);

// Replaces the volume's painting of one kind. Fails when a painted triangle is
// beyond the volume's mesh.
bool apply_paint_layer(ModelVolume &volume, PaintKind kind, const PaintLayer &layer);

} // namespace Slic3r::Web
