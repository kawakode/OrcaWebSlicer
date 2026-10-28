#pragma once

#include "SinglePlateSlice.hpp"

#include <filesystem>

namespace Slic3r::Web {

inline constexpr int EDIT_REPORT_FORMAT_VERSION = 1;

// Applies `request.edit` to one object of `request.input_model` and publishes
// every resulting object as a binary STL of its printable parts, in plate
// coordinates: the object's placement is baked into the geometry, so a part
// keeps exactly the position it had on the plate. `output_report` receives the
// JSON report naming each part, its side of a cut, its bounds, and, for a
// repair, what was fixed.
//
// A cut goes through the engine's own `Cut`, the code behind the desktop cut
// tool. A repair runs the admesh repair the desktop applies on import, and also
// fills holes, which import deliberately leaves to the slicer.
SinglePlateSliceResult edit_model(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                  const SinglePlateSliceCallbacks &callbacks = {});

} // namespace Slic3r::Web
