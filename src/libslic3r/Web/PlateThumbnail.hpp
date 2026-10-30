#pragma once

#include "libslic3r/GCode/ThumbnailData.hpp"

#include <array>
#include <string>

namespace Slic3r {
class DynamicPrintConfig;
class Model;
} // namespace Slic3r

namespace Slic3r::Web {

// Renders every printable part of the model's instances, as placed, into an
// RGBA thumbnail with a transparent background. The view is a fixed isometric
// orthographic projection fitted to the plate's contents, shaded on the CPU so
// no OpenGL context is needed. Rows are stored bottom-up, as the desktop's
// OpenGL renderer returns them and as every thumbnail encoder expects.
ThumbnailData render_plate_thumbnail(const Model &model, unsigned width, unsigned height,
                                     const std::array<unsigned char, 3> &colour);

// The first filament's colour, which the desktop also paints objects with, or
// the desktop's default object colour when the profile names none.
std::array<unsigned char, 3> plate_thumbnail_colour(const DynamicPrintConfig &config);

// Supplies every thumbnail size G-code export asks for.
ThumbnailsGeneratorCallback plate_thumbnail_generator(const Model &model, const DynamicPrintConfig &config);

} // namespace Slic3r::Web
