#include <catch2/catch_all.hpp>

#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Web/SinglePlateSlice.hpp"
#include "test_utils.hpp"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <nlohmann/json.hpp>
#include <sstream>
#include <string>
#include <vector>

using namespace Slic3r::Web;
using namespace Slic3r;

namespace {

bool has_error(const SinglePlateSliceRequestValidation &validation, const std::string &code)
{
    return std::any_of(validation.errors.begin(), validation.errors.end(), [&code](const WorkerManifestError &error) {
        return error.code == code;
    });
}

// Stages one job directory holding a real mesh, so a slice runs through the
// same importer a real job uses rather than a stub.
std::filesystem::path stage_cube(const std::filesystem::path &job_root)
{
    const std::filesystem::path source = std::filesystem::path(std::string(TEST_DATA_DIR)) / "20mm_cube.obj";
    REQUIRE(std::filesystem::is_regular_file(source));
    std::filesystem::copy_file(source, job_root / "model.obj");
    return job_root / "model.obj";
}

// Flattens one bundled profile's "inherits" chain into a single JSON object,
// mirroring scripts/web_baseline.py's resolve_profile(): a child key always
// wins over its parent's (shallow, not deep-merged), and "inherits" names a
// sibling file in the same directory -- true of every profile actually
// shipped under resources/profiles. ConfigBase::load_from_json never walks
// this chain itself (see apply_profile_file in SinglePlateSlice.cpp), so a
// job-local profile has to already be flat; materialize_web_profiles.py does
// this for the worker's own docker fixtures, and this is the same algorithm
// so a test can materialize what it needs at run time instead of committing
// pre-flattened JSON to tests/data (a previous attempt did that; it was
// removed).
nlohmann::json resolve_bundled_profile(const std::filesystem::path &path)
{
    std::ifstream stream(path);
    REQUIRE(stream.is_open());
    nlohmann::json profile;
    stream >> profile;

    const auto inherits = profile.find("inherits");
    if (inherits != profile.end() && inherits->is_string()) {
        nlohmann::json parent = resolve_bundled_profile(path.parent_path() / (inherits->get<std::string>() + ".json"));
        for (const auto &[key, value] : profile.items())
            parent[key] = value;
        parent.erase("inherits");
        return parent;
    }
    profile.erase("inherits");
    return profile;
}

// Resolves one bundled profile and writes it as `<job_root>/profiles/<name>.json`,
// returning the job-relative path a request's `profiles` object should name.
std::string materialize_bundled_profile(const std::filesystem::path &job_root, const std::filesystem::path &bundled_path,
                                        const std::string &name)
{
    REQUIRE(std::filesystem::is_regular_file(bundled_path));
    const nlohmann::json resolved = resolve_bundled_profile(bundled_path);
    std::filesystem::create_directories(job_root / "profiles");
    const std::filesystem::path relative = std::filesystem::path("profiles") / (name + ".json");
    std::ofstream out(job_root / relative);
    out << resolved.dump(2);
    return relative.generic_string();
}

// The bundled printer every multi-filament test below slices on: a real
// single-nozzle machine/process/filament trio, matching the one
// docker/web/compose.yml's worker-smoke already exercises for the
// single-filament case.
struct BundledAnycubicProfiles
{
    std::string machine;
    std::string process;
    std::string filament;
};

BundledAnycubicProfiles materialize_anycubic_profiles(const std::filesystem::path &job_root)
{
    const std::filesystem::path profiles_dir(std::string(PROFILES_DIR));
    BundledAnycubicProfiles result;
    result.machine =
        materialize_bundled_profile(job_root, profiles_dir / "Anycubic" / "machine" / "Anycubic Kobra 0.4 nozzle.json",
                                    "machine");
    result.process = materialize_bundled_profile(
        job_root, profiles_dir / "Anycubic" / "process" / "0.20mm Standard @Anycubic Kobra.json", "process");
    result.filament = materialize_bundled_profile(
        job_root, profiles_dir / "Anycubic" / "filament" / "Anycubic Generic PLA.json", "filament");
    return result;
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
        "input_model": "model.step",
        "output_gcode": "preview.png"
        }}
    })");

    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "unsupported_input_format"));
    REQUIRE(has_error(validation, "unsupported_output_format"));
}

TEST_CASE("Single-plate request accepts a project archive and plate selection", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-project",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "input/project.3MF",
        "output_gcode": "output/project.gcode",
        "plate_index": 1
        }}
    })");

    REQUIRE(validation.is_valid());
    REQUIRE(validation.request->input_model == "input/project.3MF");
    REQUIRE(validation.request->plate_index == 1);
}

TEST_CASE("Single-plate request defaults to the first plate", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-project-default-plate",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "project.3mf",
        "output_gcode": "project.gcode"
        }}
    })");

    REQUIRE(validation.is_valid());
    REQUIRE(validation.request->plate_index == 1);
}

TEST_CASE("Single-plate request rejects an unusable plate selection", "[SliceRequest]")
{
    for (const auto &plate_index : {nlohmann::json(0), nlohmann::json(37), nlohmann::json(-1), nlohmann::json("1")}) {
        DYNAMIC_SECTION(plate_index.dump()) {
            nlohmann::json manifest = {
                {"protocol_version", 1},
                {"job_id", "slice-invalid-plate"},
                {"operation", {
                    {"name", "slice"},
                    {"version", 1},
                    {"payload", {
                        {"input_model", "project.3mf"},
                        {"output_gcode", "project.gcode"},
                        {"plate_index", plate_index}
                    }}
                }}
            };
            const SinglePlateSliceRequestValidation validation =
                validate_single_plate_slice_request(manifest.dump());
            REQUIRE_FALSE(validation.is_valid());
            REQUIRE(has_error(validation, "invalid_plate_index"));
        }
    }
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

TEST_CASE("Single-plate request accepts configured resource limits", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-resource-limits",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.obj",
        "output_gcode": "model.gcode",
        "limits": {
            "max_input_bytes": 1024,
            "max_triangles": 2048,
            "max_wall_time_ms": 3000,
            "max_memory_bytes": 8192,
            "max_output_bytes": 4096,
            "max_extracted_bytes": 16384
        }
        }}
    })");

    REQUIRE(validation.is_valid());
    REQUIRE(validation.request->max_input_bytes == 1024);
    REQUIRE(validation.request->max_triangles == 2048);
    REQUIRE(validation.request->max_wall_time_ms == 3000);
    REQUIRE(validation.request->max_memory_bytes == 8192);
    REQUIRE(validation.request->max_output_bytes == 4096);
    REQUIRE(validation.request->max_extracted_bytes == 16384);
}

TEST_CASE("Single-plate request accepts several filament profiles", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-multi-filament",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.obj",
        "output_gcode": "model.gcode",
        "profiles": {
            "machine": "profiles/machine.json",
            "process": "profiles/process.json",
            "filaments": ["profiles/filament-0.json", "profiles/filament-1.json"]
        }
        }}
    })");

    REQUIRE(validation.is_valid());
    REQUIRE(validation.request->filament_profiles.size() == 2);
    REQUIRE(validation.request->filament_profiles[0] == "profiles/filament-0.json");
    REQUIRE(validation.request->filament_profiles[1] == "profiles/filament-1.json");
}

TEST_CASE("Single-plate request rejects naming both filament and filaments", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-both-filament-spellings",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.obj",
        "output_gcode": "model.gcode",
        "profiles": {
            "filament": "profiles/filament.json",
            "filaments": ["profiles/filament-0.json"]
        }
        }}
    })");

    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "invalid_profiles"));
}

TEST_CASE("Single-plate request rejects an over-long filaments list", "[SliceRequest]")
{
    nlohmann::json filaments = nlohmann::json::array();
    for (unsigned index = 0; index < 17; ++index)
        filaments.push_back("profiles/filament-" + std::to_string(index) + ".json");

    nlohmann::json manifest = {
        {"protocol_version", 1},
        {"job_id", "slice-too-many-filaments"},
        {"operation", {
            {"name", "slice"},
            {"version", 1},
            {"payload", {
                {"input_model", "model.obj"},
                {"output_gcode", "model.gcode"},
                {"profiles", {{"filaments", std::move(filaments)}}}
            }}
        }}
    };

    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(manifest.dump());
    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "invalid_filaments"));
}

TEST_CASE("Single-plate request rejects an unsafe path inside filaments", "[SliceRequest]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-unsafe-filament-path",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.obj",
        "output_gcode": "model.gcode",
        "profiles": {
            "filaments": ["profiles/filament-0.json", "../escaped.json"]
        }
        }}
    })");

    REQUIRE_FALSE(validation.is_valid());
    REQUIRE(has_error(validation, "invalid_profile_path"));
}

TEST_CASE("Single-plate request rejects an out-of-range per-object filament", "[SliceRequest]")
{
    // Like source_object, a per-object filament can only be bounded once the
    // model (and, here, the filament count the request implies) is known, so
    // parsing accepts any unsigned value and slice_single_plate is what
    // rejects one that is actually out of range. No profiles are needed:
    // naming no filaments at all means exactly one implied slot, so
    // "filament": 2 is out of range against the single-plate default.
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(R"({
        "protocol_version": 1,
        "job_id": "slice-object-filament-out-of-range",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.obj",
        "output_gcode": "model.gcode",
        "objects": [{"source_object": 0, "transform": [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1], "filament": 2}]
        }}
    })");
    REQUIRE(validation.is_valid());
    REQUIRE(validation.request->objects[0].filament == 2);

    const ScopedTemporaryDir directory("orca-slice-object-filament");
    const std::filesystem::path job_root(directory.string());
    stage_cube(job_root);

    const SinglePlateSliceResult result = slice_single_plate(*validation.request, job_root);
    REQUIRE_FALSE(result.success);
    REQUIRE(result.code == "invalid_object_filament");
    REQUIRE(result.category == WorkerErrorCategory::Input);
    REQUIRE_FALSE(std::filesystem::exists(job_root / "model.gcode"));
}

TEST_CASE("Single-plate request rejects invalid resource limits", "[SliceRequest]")
{
    const std::vector<std::pair<std::string, std::string>> cases {
        {"max_input_bytes", "invalid_input_limit"},
        {"max_triangles", "invalid_triangle_limit"},
        {"max_wall_time_ms", "invalid_wall_time_limit"},
        {"max_memory_bytes", "invalid_memory_limit"},
        {"max_output_bytes", "invalid_output_limit"},
        {"max_extracted_bytes", "invalid_extracted_limit"}
    };
    for (const auto &[field, code] : cases) {
        DYNAMIC_SECTION(field) {
            nlohmann::json manifest = {
                {"protocol_version", 1},
                {"job_id", "slice-resource-limit-invalid"},
                {"operation", {
                    {"name", "slice"},
                    {"version", 1},
                    {"payload", {
                        {"input_model", "model.obj"},
                        {"output_gcode", "model.gcode"},
                        {"limits", {{field, 0}}}
                    }}
                }}
            };
            const SinglePlateSliceRequestValidation validation =
                validate_single_plate_slice_request(manifest.dump());
            REQUIRE_FALSE(validation.is_valid());
            REQUIRE(has_error(validation, code));
        }
    }
}

TEST_CASE("Multi-filament composition synthesizes colour, map, and flush matrix for every filament", "[SliceRequest]")
{
    const ScopedTemporaryDir directory("orca-multi-filament-compose");
    const std::filesystem::path job_root(directory.string());
    const std::filesystem::path bundled_filament =
        std::filesystem::path(std::string(PROFILES_DIR)) / "Anycubic" / "filament" / "Anycubic Generic PLA.json";
    if (!std::filesystem::exists(bundled_filament))
        SKIP("bundled profile not present in this checkout: " << bundled_filament.string());

    // Both slots are materialized from the same source profile on purpose: two
    // spools of the same colour is a legitimate configuration, and the
    // synthesis must not treat the resulting identical filament_colour entries
    // as a collision (see the comment on that resize in SinglePlateSlice.cpp).
    const std::string filament_a = materialize_bundled_profile(job_root, bundled_filament, "filament-a");
    const std::string filament_b = materialize_bundled_profile(job_root, bundled_filament, "filament-b");

    DynamicPrintConfig config;
    WorkerManifestError error;
    REQUIRE(compose_multi_filament_config_for_testing(job_root, {filament_a, filament_b}, config, error));

    const auto *filament_colour = config.option<ConfigOptionStrings>("filament_colour");
    const auto *filament_map = config.option<ConfigOptionInts>("filament_map");
    const auto *flush_matrix = config.option<ConfigOptionFloats>("flush_volumes_matrix");
    REQUIRE(filament_colour != nullptr);
    REQUIRE(filament_map != nullptr);
    REQUIRE(flush_matrix != nullptr);

    CHECK(filament_colour->values.size() == 2);
    CHECK(filament_map->values.size() == 2);
    // N x N for the bundled printer's single nozzle.
    CHECK(flush_matrix->values.size() == 4);

    CHECK(filament_colour->values[0] == filament_colour->values[1]);
    CHECK(filament_map->values == std::vector<int>{1, 1});
    // A filament never flushes into itself.
    CHECK(flush_matrix->values[0] == 0.0);
    CHECK(flush_matrix->values[3] == 0.0);
}

TEST_CASE("Multi-filament request slices two explicitly placed objects and emits a tool change", "[SliceRequest]")
{
    const ScopedTemporaryDir directory("orca-multi-filament-slice");
    const std::filesystem::path job_root(directory.string());
    const std::filesystem::path bundled_machine =
        std::filesystem::path(std::string(PROFILES_DIR)) / "Anycubic" / "machine" / "Anycubic Kobra 0.4 nozzle.json";
    if (!std::filesystem::exists(bundled_machine))
        SKIP("bundled profiles not present in this checkout: " << std::string(PROFILES_DIR));

    stage_cube(job_root);
    const BundledAnycubicProfiles profiles = materialize_anycubic_profiles(job_root);

    // Two placements 80 mm apart on the Kobra's 220 x 220 mm bed, one object
    // per filament slot -- explicit transforms replace arrangement, so this
    // is exactly what gets sliced. Nothing here sets filament_colour,
    // filament_map, flush_volumes_matrix, or wipe_tower_x/y: this manifest is
    // the shape a real client sends, and is what apply_filament_profiles's
    // synthesis and reposition_wipe_tower_if_needed both have to carry on
    // their own, with the prime tower left at its default (enabled).
    std::ostringstream manifest;
    manifest << R"({
        "protocol_version": 1,
        "job_id": "slice-multi-filament-tool-change",
        "operation": {"name":"slice","version":1,"payload":{
        "input_model": "model.obj",
        "output_gcode": "model.gcode",
        "profiles": {
            "machine": ")" << profiles.machine << R"(",
            "process": ")" << profiles.process << R"(",
            "filaments": [")" << profiles.filament << R"(", ")" << profiles.filament << R"("]
        },
        "objects": [
            {"source_object": 0, "transform": [1,0,0,0, 0,1,0,0, 0,0,1,0, 70,110,0,1], "filament": 1},
            {"source_object": 0, "transform": [1,0,0,0, 0,1,0,0, 0,0,1,0, 150,110,0,1], "filament": 2}
        ]
        }}
    })";

    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(manifest.str());
    REQUIRE(validation.is_valid());

    const SinglePlateSliceResult result = slice_single_plate(*validation.request, job_root);
    INFO("code: " << result.code << " message: " << result.message);
    REQUIRE(result.success);

    std::ifstream gcode(job_root / "model.gcode");
    REQUIRE(gcode.is_open());
    const std::string contents((std::istreambuf_iterator<char>(gcode)), std::istreambuf_iterator<char>());
    CHECK(contents.find("\nT1") != std::string::npos);
}
