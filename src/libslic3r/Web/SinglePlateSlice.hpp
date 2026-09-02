#pragma once

#include "WorkerManifest.hpp"

#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace Slic3r::Web {

struct SinglePlateSliceRequest
{
    WorkerManifest envelope;
    std::string input_model;
    std::string output_gcode;
    std::string machine_profile;
    std::string process_profile;
    std::string filament_profile;
    std::vector<std::pair<std::string, std::string>> settings;
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
};

SinglePlateSliceRequestValidation validate_single_plate_slice_request(std::string_view serialized);
SinglePlateSliceResult slice_single_plate(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root);

} // namespace Slic3r::Web
