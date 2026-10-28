#include "PlateThumbnail.hpp"

#include "libslic3r/Model.hpp"
#include "libslic3r/PrintConfig.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <limits>
#include <vector>

namespace Slic3r::Web {
namespace {

// The desktop's default object colour, used when no filament colour is known.
constexpr std::array<unsigned char, 3> DEFAULT_COLOUR {0x00, 0x96, 0x88};
constexpr double                       AMBIENT = 0.35;
// Fraction of the image left empty around the fitted contents.
constexpr double                       MARGIN = 0.06;

struct Camera
{
    Vec3d right;
    Vec3d up;
    Vec3d towards_viewer;
};

// Looks down at the plate from the front right, as the desktop's default
// isometric thumbnail does.
Camera isometric_camera()
{
    const double azimuth   = -PI / 4.;
    const double elevation = PI / 5.;
    const Vec3d  towards_viewer(std::cos(elevation) * std::sin(-azimuth), -std::cos(elevation) * std::cos(azimuth),
                                std::sin(elevation));
    const Vec3d  right = (-towards_viewer).cross(Vec3d::UnitZ()).normalized();
    return {right, right.cross(-towards_viewer).normalized(), towards_viewer};
}

struct Triangle
{
    std::array<Vec3d, 3> view;
    double               shade;
};

} // namespace

std::array<unsigned char, 3> plate_thumbnail_colour(const DynamicPrintConfig &config)
{
    const auto *colours = config.option<ConfigOptionStrings>("filament_colour");
    if (colours == nullptr || colours->values.empty())
        return DEFAULT_COLOUR;
    const std::string &hex = colours->values.front();
    if (hex.size() != 7 || hex[0] != '#')
        return DEFAULT_COLOUR;
    std::array<unsigned char, 3> colour {};
    for (std::size_t channel = 0; channel < 3; ++channel) {
        const std::string pair = hex.substr(1 + 2 * channel, 2);
        if (!std::all_of(pair.begin(), pair.end(), [](unsigned char c) { return std::isxdigit(c); }))
            return DEFAULT_COLOUR;
        colour[channel] = static_cast<unsigned char>(std::stoi(pair, nullptr, 16));
    }
    return colour;
}

ThumbnailData render_plate_thumbnail(const Model &model, unsigned width, unsigned height,
                                     const std::array<unsigned char, 3> &colour)
{
    ThumbnailData thumbnail;
    thumbnail.set(width, height);
    if (!thumbnail.is_valid())
        return thumbnail;
    std::fill(thumbnail.pixels.begin(), thumbnail.pixels.end(), 0);

    const Camera camera = isometric_camera();
    const Vec3d  light  = (camera.towards_viewer + 0.6 * camera.up + 0.3 * camera.right).normalized();
    std::vector<Triangle> triangles;
    BoundingBoxf screen;
    for (const ModelObject *object : model.objects) {
        for (const ModelInstance *instance : object->instances) {
            if (!object->printable || !instance->printable)
                continue;
            for (const ModelVolume *volume : object->volumes) {
                if (!volume->is_model_part())
                    continue;
                const Transform3d          matrix = instance->get_matrix() * volume->get_matrix();
                const indexed_triangle_set &its   = volume->mesh().its;
                for (const stl_triangle_vertex_indices &indices : its.indices) {
                    std::array<Vec3d, 3> world;
                    for (int corner = 0; corner < 3; ++corner)
                        world[corner] = matrix * its.vertices[indices[corner]].cast<double>();
                    const Vec3d normal = (world[1] - world[0]).cross(world[2] - world[0]);
                    if (normal.squaredNorm() <= 0.)
                        continue;
                    // Shading uses the face as seen from the camera, so a mirrored
                    // instance or an inward-facing triangle still renders lit.
                    const double lit = std::abs(normal.normalized().dot(light));
                    Triangle     triangle {{}, AMBIENT + (1. - AMBIENT) * lit};
                    for (int corner = 0; corner < 3; ++corner) {
                        triangle.view[corner] = {world[corner].dot(camera.right), world[corner].dot(camera.up),
                                                 world[corner].dot(camera.towards_viewer)};
                        screen.merge(Vec2d(triangle.view[corner].x(), triangle.view[corner].y()));
                    }
                    triangles.push_back(triangle);
                }
            }
        }
    }
    if (triangles.empty() || !screen.defined)
        return thumbnail;

    const Vec2d  extent = screen.size();
    const double usable = 1. - 2. * MARGIN;
    const double scale  = std::min(width * usable / std::max(extent.x(), 1e-6), height * usable / std::max(extent.y(), 1e-6));
    const Vec2d  offset(0.5 * (width - extent.x() * scale), 0.5 * (height - extent.y() * scale));
    std::vector<double> depth(std::size_t(width) * height, -std::numeric_limits<double>::infinity());

    for (Triangle &triangle : triangles) {
        for (Vec3d &corner : triangle.view) {
            corner.x() = (corner.x() - screen.min.x()) * scale + offset.x();
            corner.y() = (corner.y() - screen.min.y()) * scale + offset.y();
        }
        const Vec3d &a = triangle.view[0], &b = triangle.view[1], &c = triangle.view[2];
        const double area = (b.x() - a.x()) * (c.y() - a.y()) - (b.y() - a.y()) * (c.x() - a.x());
        if (std::abs(area) < 1e-12)
            continue;
        const int min_x = std::max(0, int(std::floor(std::min({a.x(), b.x(), c.x()}))));
        const int max_x = std::min(int(width) - 1, int(std::ceil(std::max({a.x(), b.x(), c.x()}))));
        const int min_y = std::max(0, int(std::floor(std::min({a.y(), b.y(), c.y()}))));
        const int max_y = std::min(int(height) - 1, int(std::ceil(std::max({a.y(), b.y(), c.y()}))));
        const std::array<unsigned char, 4> rgba {
            static_cast<unsigned char>(std::min(255., colour[0] * triangle.shade)),
            static_cast<unsigned char>(std::min(255., colour[1] * triangle.shade)),
            static_cast<unsigned char>(std::min(255., colour[2] * triangle.shade)), 255};
        for (int y = min_y; y <= max_y; ++y) {
            for (int x = min_x; x <= max_x; ++x) {
                // Pixel centres, with barycentric weights from edge functions.
                const double px = x + 0.5, py = y + 0.5;
                const double w0 = ((b.x() - px) * (c.y() - py) - (b.y() - py) * (c.x() - px)) / area;
                const double w1 = ((c.x() - px) * (a.y() - py) - (c.y() - py) * (a.x() - px)) / area;
                const double w2 = 1. - w0 - w1;
                if (w0 < 0. || w1 < 0. || w2 < 0.)
                    continue;
                const double z = w0 * a.z() + w1 * b.z() + w2 * c.z();
                const std::size_t pixel = std::size_t(y) * width + x;
                if (z <= depth[pixel])
                    continue;
                depth[pixel] = z;
                std::copy(rgba.begin(), rgba.end(), thumbnail.pixels.begin() + 4 * pixel);
            }
        }
    }
    return thumbnail;
}

ThumbnailsGeneratorCallback plate_thumbnail_generator(const Model &model, const DynamicPrintConfig &config)
{
    const std::array<unsigned char, 3> colour = plate_thumbnail_colour(config);
    return [&model, colour](const ThumbnailsParams &params) {
        ThumbnailsList thumbnails;
        for (const Vec2d &size : params.sizes)
            thumbnails.push_back(render_plate_thumbnail(model, static_cast<unsigned>(size.x()),
                                                        static_cast<unsigned>(size.y()), colour));
        return thumbnails;
    };
}

} // namespace Slic3r::Web
