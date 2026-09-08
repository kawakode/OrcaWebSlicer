#include <catch2/catch_all.hpp>

#include "libslic3r/Web/SceneExport.hpp"
#include "test_utils.hpp"

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

using namespace Slic3r::Web;

namespace {

constexpr std::size_t POINT_BYTES = 12;

std::string inspect_manifest(const std::string &input_model, const std::string &output_scene,
                             const nlohmann::json &limits = nlohmann::json::object())
{
    nlohmann::json payload {{"input_model", input_model}, {"output_scene", output_scene}};
    if (!limits.empty())
        payload["limits"] = limits;
    return nlohmann::json{{"protocol_version", 1},
                          {"job_id", "job-scene-test"},
                          {"operation", {{"name", "inspect"}, {"version", INSPECT_OPERATION_VERSION},
                                         {"payload", payload}}}}
        .dump();
}

// Stages one job directory holding a real mesh, so the export runs through the
// same importer a slice job uses rather than a stub.
std::filesystem::path stage_cube(const std::filesystem::path &job_root)
{
    const std::filesystem::path source = std::filesystem::path(std::string(TEST_DATA_DIR)) / "20mm_cube.obj";
    REQUIRE(std::filesystem::is_regular_file(source));
    std::filesystem::copy_file(source, job_root / "model.obj");
    return job_root / "model.obj";
}

std::vector<char> read_bytes(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary);
    REQUIRE(input.good());
    return std::vector<char>(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
}

std::int32_t read_int32(const std::vector<char> &blob, std::size_t offset)
{
    std::int32_t value = 0;
    std::memcpy(&value, blob.data() + offset, sizeof(value));
    return value;
}

bool has_error(const SceneExportRequestValidation &validation, const std::string &code)
{
    return std::any_of(validation.errors.begin(), validation.errors.end(),
                       [&code](const WorkerManifestError &error) { return error.code == code; });
}

} // namespace

TEST_CASE("An inspect request accepts a safe model and scene path", "[SceneExport]")
{
    const SceneExportRequestValidation validation =
        validate_scene_export_request(inspect_manifest("input/model.stl", "output/scene.json"));

    REQUIRE(validation.is_valid());
    CHECK(validation.request->input_model == "input/model.stl");
    CHECK(validation.request->output_scene == "output/scene.json");
    // A request that names no machine profile still inspects; the bed then
    // comes from the engine's own defaults.
    CHECK(validation.request->machine_profile.empty());
    CHECK(validation.request->plate_index == 1);
}

TEST_CASE("An inspect request refuses paths and formats it cannot contain", "[SceneExport]")
{
    CHECK(has_error(validate_scene_export_request(inspect_manifest("../escape.stl", "output/scene.json")),
                    "invalid_input_model"));
    CHECK(has_error(validate_scene_export_request(inspect_manifest("input/model.gcode", "output/scene.json")),
                    "unsupported_input_format"));
    CHECK(has_error(validate_scene_export_request(inspect_manifest("input/model.stl", "../scene.json")),
                    "invalid_output_scene"));
    CHECK(has_error(validate_scene_export_request(inspect_manifest("input/model.stl", "output/scene.bin")),
                    "unsupported_scene_format"));
}

TEST_CASE("An inspect request refuses a plate outside the single-plate scope", "[SceneExport]")
{
    nlohmann::json manifest = nlohmann::json::parse(inspect_manifest("input/model.3mf", "output/scene.json"));
    manifest["operation"]["payload"]["plate_index"] = 0;
    CHECK(has_error(validate_scene_export_request(manifest.dump()), "invalid_plate_index"));
    manifest["operation"]["payload"]["plate_index"] = 999;
    CHECK(has_error(validate_scene_export_request(manifest.dump()), "invalid_plate_index"));
}

TEST_CASE("An exported scene indexes a blob its objects tile exactly", "[SceneExport]")
{
    const ScopedTemporaryDir directory("orca-scene");
    const std::filesystem::path job_root(directory.string());
    stage_cube(job_root);

    const SceneExportRequestValidation validation =
        validate_scene_export_request(inspect_manifest("model.obj", "scene.json"));
    REQUIRE(validation.is_valid());
    const SceneExportResult result = export_scene(*validation.request, job_root);
    REQUIRE(result.success);

    // Both files are published, and the caller is told about both so it can
    // hash them and clean them up together.
    REQUIRE(result.artifacts.size() == 2);
    CHECK(result.artifacts[0].kind == "scene");
    CHECK(result.artifacts[1].kind == "scene_data");

    const std::vector<char> index_bytes = read_bytes(job_root / "scene.json");
    const nlohmann::json scene = nlohmann::json::parse(std::string(index_bytes.data(), index_bytes.size()));
    const std::vector<char> data = read_bytes(job_root / "scene.bin");

    CHECK(scene.at("scene_version") == SCENE_VERSION);
    CHECK(scene.at("units") == "mm");
    CHECK(scene.at("data") == "scene.bin");
    CHECK(scene.at("data_bytes").get<std::size_t>() == data.size());
    REQUIRE_FALSE(scene.at("objects").empty());
    REQUIRE_FALSE(scene.at("bed").at("shape").empty());
    CHECK(scene.at("bed").at("printable_height").get<double>() > 0.0);

    std::size_t expected_offset = 0;
    for (const nlohmann::json &object : scene.at("objects")) {
        const std::size_t vertex_count = object.at("vertex_count").get<std::size_t>();
        const std::size_t length = object.at("length").get<std::size_t>();
        DYNAMIC_SECTION("object " << object.at("index").get<std::size_t>())
        {
            // Every object's range is contiguous, holds whole triangles, and
            // says the same thing as its own triangle count.
            CHECK(object.at("offset").get<std::size_t>() == expected_offset);
            CHECK(length == vertex_count * POINT_BYTES);
            CHECK(vertex_count == object.at("triangle_count").get<std::size_t>() * 3);
            CHECK(vertex_count % 3 == 0);
        }
        expected_offset += length;
    }
    CHECK(expected_offset == data.size());
}

TEST_CASE("An exported object's bounding box matches its own vertices", "[SceneExport]")
{
    const ScopedTemporaryDir directory("orca-scene");
    const std::filesystem::path job_root(directory.string());
    stage_cube(job_root);

    const SceneExportRequestValidation validation =
        validate_scene_export_request(inspect_manifest("model.obj", "scene.json"));
    REQUIRE(validation.is_valid());
    REQUIRE(export_scene(*validation.request, job_root).success);

    const std::vector<char> index_bytes = read_bytes(job_root / "scene.json");
    const nlohmann::json scene = nlohmann::json::parse(std::string(index_bytes.data(), index_bytes.size()));
    const std::vector<char> data = read_bytes(job_root / "scene.bin");
    const nlohmann::json &object = scene.at("objects")[0];

    double minimum[3] = {1e30, 1e30, 1e30};
    double maximum[3] = {-1e30, -1e30, -1e30};
    const std::size_t offset = object.at("offset").get<std::size_t>();
    for (std::size_t vertex = 0; vertex < object.at("vertex_count").get<std::size_t>(); ++vertex)
        for (int axis = 0; axis < 3; ++axis) {
            const double value =
                read_int32(data, offset + vertex * POINT_BYTES + axis * 4) * scene.at("quantum_mm").get<double>();
            minimum[axis] = std::min(minimum[axis], value);
            maximum[axis] = std::max(maximum[axis], value);
        }

    for (int axis = 0; axis < 3; ++axis) {
        DYNAMIC_SECTION("axis " << axis)
        {
            // The quantum is 1 um, so the declared box and the decoded one may
            // differ by at most one quantum per axis.
            CHECK_THAT(object.at("bounding_box").at("min")[axis].get<double>(),
                       Catch::Matchers::WithinAbs(minimum[axis], 1e-3));
            CHECK_THAT(object.at("bounding_box").at("max")[axis].get<double>(),
                       Catch::Matchers::WithinAbs(maximum[axis], 1e-3));
        }
    }
    // A 20 mm cube is 20 mm across whichever axis it is measured on.
    CHECK_THAT(maximum[0] - minimum[0], Catch::Matchers::WithinAbs(20.0, 1e-2));
}

TEST_CASE("An oversized scene is refused as a resource limit", "[SceneExport]")
{
    const ScopedTemporaryDir directory("orca-scene");
    const std::filesystem::path job_root(directory.string());
    stage_cube(job_root);

    const SceneExportRequestValidation validation =
        validate_scene_export_request(inspect_manifest("model.obj", "scene.json", {{"max_scene_bytes", 16}}));
    REQUIRE(validation.is_valid());
    const SceneExportResult result = export_scene(*validation.request, job_root);

    REQUIRE_FALSE(result.success);
    CHECK(result.code == "scene_size_limit_exceeded");
    CHECK(result.category == WorkerErrorCategory::ResourceLimit);
    // Unlike the layer preview, a scene the browser asked for and did not get
    // is a failure, and nothing partial is left behind.
    CHECK_FALSE(std::filesystem::exists(job_root / "scene.json"));
    CHECK_FALSE(std::filesystem::exists(job_root / "scene.bin"));
}

TEST_CASE("An inspect envelope is versioned independently of slice", "[SceneExport]")
{
    nlohmann::json manifest = nlohmann::json::parse(inspect_manifest("model.obj", "scene.json"));
    manifest["operation"]["name"] = "slice";
    CHECK_FALSE(validate_inspect_envelope(manifest.dump()).is_valid());

    manifest["operation"]["name"] = "inspect";
    manifest["operation"]["version"] = INSPECT_OPERATION_VERSION + 1;
    CHECK_FALSE(validate_inspect_envelope(manifest.dump()).is_valid());

    manifest["operation"]["version"] = INSPECT_OPERATION_VERSION;
    manifest["operation"]["payload"]["future_field"] = true;
    CHECK(validate_inspect_envelope(manifest.dump()).is_valid());
}
