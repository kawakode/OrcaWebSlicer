#include "FacetPaint.hpp"

#include "libslic3r/Model.hpp"

#include <algorithm>
#include <charconv>
#include <limits>

namespace Slic3r::Web {
namespace {

// Deeper trees than this are never produced by the desktop's brushes, whose
// subdivision stops at a fraction of the cursor size.
constexpr std::size_t MAX_SPLIT_DEPTH = 32;

FacetsAnnotation &facets(ModelVolume &volume, PaintKind kind)
{
    switch (kind) {
    case PaintKind::Supports: return volume.supported_facets;
    case PaintKind::Seam: return volume.seam_facets;
    case PaintKind::Color: return volume.mmu_segmentation_facets;
    case PaintKind::FuzzySkin: break;
    }
    return volume.fuzzy_skin_facets;
}

const FacetsAnnotation &facets(const ModelVolume &volume, PaintKind kind)
{
    return facets(const_cast<ModelVolume &>(volume), kind);
}

int hex_digit(char digit)
{
    if (digit >= '0' && digit <= '9')
        return digit - '0';
    if (digit >= 'A' && digit <= 'F')
        return digit - 'A' + 10;
    return -1;
}

// Walks one triangle's code the way TriangleSelector::deserialize reads it:
// nibbles from the last character to the first, each node either a leaf with
// its state or a split with 2 to 4 children that follow it. Returns the
// highest leaf state, or -1 when the code is malformed or exceeds `max_state`.
int check_code(std::string_view code, int max_state)
{
    std::size_t position = code.size();
    auto next = [&]() { return position == 0 ? -1 : hex_digit(code[--position]); };
    // Children still to read at each open split, outermost first.
    std::vector<int> pending {1};
    int              highest = 0;
    while (!pending.empty()) {
        const int nibble = next();
        if (nibble < 0)
            return -1;
        --pending.back();
        if (const int split_sides = nibble & 0b11; split_sides != 0) {
            // The special side names one of three sides, and a split of all
            // three always records the first.
            const int special_side = nibble >> 2;
            if (special_side > 2 || (split_sides == 3 && special_side != 0) || pending.size() > MAX_SPLIT_DEPTH)
                return -1;
            pending.push_back(split_sides + 1);
        } else {
            int state = nibble >> 2;
            if (state == 0b11) {
                const int low = next();
                if (low < 0)
                    return -1;
                if (low == 0b1111) {
                    const int high = next();
                    if (high < 0)
                        return -1;
                    state = high + 18;
                } else {
                    state = low + 3;
                }
            }
            if (state > max_state)
                return -1;
            highest = std::max(highest, state);
        }
        while (!pending.empty() && pending.back() == 0)
            pending.pop_back();
    }
    return position == 0 ? highest : -1;
}

} // namespace

const char *paint_kind_name(PaintKind kind)
{
    switch (kind) {
    case PaintKind::Supports: return "supports";
    case PaintKind::Seam: return "seam";
    case PaintKind::Color: return "color";
    case PaintKind::FuzzySkin: break;
    }
    return "fuzzy_skin";
}

int max_paint_state(PaintKind kind)
{
    switch (kind) {
    case PaintKind::Supports:
    case PaintKind::Seam: return int(EnforcerBlockerType::BLOCKER);
    case PaintKind::Color: return int(EnforcerBlockerType::Extruder16);
    case PaintKind::FuzzySkin: break;
    }
    return int(EnforcerBlockerType::FUZZY_SKIN);
}

std::optional<PaintLayer> decode_paint_layer(std::string_view encoded, PaintKind kind, std::string &error)
{
    PaintLayer layer;
    long long  next_free = 0;
    while (!encoded.empty()) {
        const std::size_t end = std::min(encoded.find(';'), encoded.size());
        const std::string_view run = encoded.substr(0, end);
        encoded.remove_prefix(end == encoded.size() ? end : end + 1);

        const std::size_t colon = run.find(':');
        int               start = 0;
        const auto [parsed, failure] = std::from_chars(run.data(), run.data() + std::min(colon, run.size()), start);
        if (colon == std::string_view::npos || failure != std::errc() || parsed != run.data() + colon || start < next_free) {
            error = "A paint run must start with an increasing triangle index and a colon.";
            return std::nullopt;
        }
        std::string_view codes    = run.substr(colon + 1);
        long long        triangle = start;
        while (true) {
            const std::size_t comma = std::min(codes.find(','), codes.size());
            const std::string_view code = codes.substr(0, comma);
            if (triangle > std::numeric_limits<int>::max()) {
                error = "A paint run goes beyond the largest triangle index.";
                return std::nullopt;
            }
            // "0" is an unpainted leaf, which the desktop never stores.
            if (!code.empty() && code != "0") {
                const int highest = check_code(code, max_paint_state(kind));
                if (highest < 0) {
                    error = std::string("A ") + paint_kind_name(kind) + " paint code is malformed or out of range.";
                    return std::nullopt;
                }
                layer.highest_state = std::max(layer.highest_state, highest);
                layer.triangles.emplace_back(int(triangle), std::string(code));
            }
            ++triangle;
            if (comma == codes.size())
                break;
            codes.remove_prefix(comma + 1);
        }
        next_free = triangle;
    }
    return layer;
}

std::string encode_paint_layer(const ModelVolume &volume, PaintKind kind)
{
    const FacetsAnnotation &annotation = facets(volume, kind);
    std::string             encoded;
    int                     next = -1;
    for (const auto &mapping : annotation.get_data().triangles_to_split) {
        const int triangle = mapping.triangle_idx;
        // Short gaps cost less as empty codes than as a new run.
        if (next >= 0 && triangle >= next && triangle - next <= 3) {
            encoded.append(std::size_t(triangle - next), ',');
            encoded += ',';
        } else {
            if (!encoded.empty())
                encoded += ';';
            encoded += std::to_string(triangle) + ':';
        }
        encoded += annotation.get_triangle_as_string(triangle);
        next = triangle + 1;
    }
    return encoded;
}

bool apply_paint_layer(ModelVolume &volume, PaintKind kind, const PaintLayer &layer)
{
    const std::size_t triangles = volume.mesh().its.indices.size();
    if (!layer.triangles.empty() && std::size_t(layer.triangles.back().first) >= triangles)
        return false;
    FacetsAnnotation &annotation = facets(volume, kind);
    annotation.reset();
    annotation.reserve(int(layer.triangles.size()));
    for (const auto &[triangle, code] : layer.triangles)
        annotation.set_triangle_from_string(triangle, code);
    annotation.shrink_to_fit();
    return true;
}

} // namespace Slic3r::Web
