#include <catch2/catch_all.hpp>

#include "libslic3r/Web/SinglePlateSlice.hpp"

#include <algorithm>
#include <nlohmann/json.hpp>
#include <string>

using namespace Slic3r::Web;

namespace {

bool has_error(const SinglePlateSliceRequestValidation &validation, const std::string &code)
{
    return std::any_of(validation.errors.begin(), validation.errors.end(), [&code](const WorkerManifestError &error) {
        return error.code == code;
    });
}

} // namespace

TEST_CASE("Single-plate request accepts safe artifact paths and serialized settings", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-1",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "input/model.obj",
        "output_gcode": "output/model.gcode",
        "profiles": {
            "machine": "profiles/machine.json",
            "process": "profiles/process.json",
            "filament": "profiles/filament.json"
        },
        "settings": {"layer_height": "0.2", "brim_type": "no_brim"}
        }}
    })");

    REQUIRE(validation.is_valid());
    REQUIRE(validation.request->input_model == "input/model.obj");
    REQUIRE(validation.request->output_gcode == "output/model.gcode");
    REQUIRE(validation.request->machine_profile == "profiles/machine.json");
    REQUIRE(validation.request->settings.size() == 2);
}


TEST_CASE("Single-plate request requires a complete resolved profile set", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-4",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.obj",
        "output_gcode": "model.gcode",
        "profiles": {"machine": "../machine.json"}
        }}
    })");

    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "invalid_profile_path"));
}

TEST_CASE("Single-plate request rejects paths outside its job directory", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-2",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "../model.obj",
        "output_gcode": "C:\\output.gcode"
        }}
    })");

    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "invalid_input_model"));
    REQUIRE(has_error(validation, "invalid_output_gcode"));
}

TEST_CASE("Single-plate request limits initial model and artifact formats", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-3",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.3mf",
        "output_gcode": "preview.png"
        }}
    })");

    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "unsupported_input_format"));
    REQUIRE(has_error(validation, "unsupported_output_format"));
}

TEST_CASE("Single-plate request limits serialized setting count", "[SliceRequest]")
{
    nlohmann::json settings = nlohmann::json::object();
    for (unsigned index = 0; index < 257; ++index)
        settings["setting_" + std::to_string(index)] = "value";

    nlohmann::json manifest = {
        {"protocol_version", 1},
        {"job_id", "slice-settings-limit"},
        {"operation", {
            {"name", "slice"},
            {"version", 1},
            {"payload", {
                {"input_model", "model.obj"},
                {"output_gcode", "model.gcode"},
                {"settings", std::move(settings)}
            }}
        }}
    };

    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(manifest.dump());
    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "invalid_settings"));
}

TEST_CASE("Single-plate request accepts a configured output size limit", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-output-limit",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.obj",
        "output_gcode": "model.gcode",
        "limits": {"max_output_bytes": 4096}
        }}
    })");

    REQUIRE(validation.is_valid());
    REQUIRE(validation.request->max_output_bytes == 4096);
}

TEST_CASE("Single-plate request rejects invalid output size limits", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-output-limit-invalid",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.obj",
        "output_gcode": "model.gcode",
        "limits": {"max_output_bytes": 0}
        }}
    })");

    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "invalid_output_limit"));
}
