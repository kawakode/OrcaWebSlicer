#include <catch2/catch_all.hpp>

#include "libslic3r/Web/ArtifactTransaction.hpp"
#include "libslic3r/Web/SinglePlateSlice.hpp"

#include <algorithm>
#include <boost/filesystem.hpp>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <nlohmann/json.hpp>
#include <set>
#include <sstream>
#include <string>

using namespace Slic3r::Web;

namespace {

const std::string IDENTITY_AT_CENTER = "[1,0,0,100, 0,1,0,100, 0,0,1,0, 0,0,0,1]";

std::string slice_manifest(const std::string &payload)
{
    return R"({"protocol_version":1,"job_id":"plate-1","operation":{"name":"slice","version":1,"payload":)" + payload +
           "}}";
}

bool has_error(const SinglePlateSliceRequestValidation &validation, const std::string &code)
{
    for (const WorkerManifestError &error : validation.errors)
        if (error.code == code)
            return true;
    return false;
}

// A job directory holding the 20 mm cube fixture, removed afterwards.
struct JobRoot
{
    std::filesystem::path path;

    JobRoot()
    {
        const std::filesystem::path created = std::filesystem::temp_directory_path() /
                                              ("orca-plate-" + boost::filesystem::unique_path().string());
        std::filesystem::create_directories(created / "input");
        std::filesystem::copy_file(std::filesystem::path(TEST_DATA_DIR) / "20mm_cube.obj", created / "input" / "cube.obj");
        WorkerManifestError error;
        REQUIRE(canonicalize_job_root(created, path, error));
    }
    ~JobRoot() { std::filesystem::remove_all(path); }

    std::string read(const std::string &relative) const
    {
        std::ifstream input(path / relative, std::ios::binary);
        return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
    }
};

SinglePlateSliceRequest valid_request(const std::string &serialized)
{
    SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(serialized);
    REQUIRE(validation.is_valid());
    return *validation.request;
}

} // namespace

TEST_CASE("Plate request accepts explicitly placed objects", "[PlateArtifacts]")
{
    const SinglePlateSliceRequest request = valid_request(slice_manifest(R"({
        "objects": [
            {"model": "input/a.stl", "instances": [{"transform": )" + IDENTITY_AT_CENTER + R"(}]},
            {"model": "input/b.3mf", "object": 2, "instances": [
                {"transform": [2,0,0,10, 0,2,0,20, 0,0,2,0, 0,0,0,1]},
                {"transform": [0,-1,0,50, 1,0,0,60, 0,0,1,0, 0,0,0,1]}
            ]}
        ],
        "output_gcode": "output/plate.gcode",
        "output_preview": "output/preview.bin",
        "output_preview_index": "output/preview.json"
    })"));

    REQUIRE(request.input_model.empty());
    REQUIRE(request.objects.size() == 2);
    CHECK(request.objects[1].object_index == 2);
    CHECK(request.objects[1].instances.size() == 2);
    CHECK(request.objects[1].instances[0][3] == 10.);
    CHECK(request.output_preview == "output/preview.bin");
}

TEST_CASE("Plate request refuses unusable placements", "[PlateArtifacts]")
{
    const auto refused = [](const std::string &payload, const std::string &code) {
        const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(slice_manifest(payload));
        INFO(payload);
        CHECK_FALSE(validation.is_valid());
        CHECK(has_error(validation, code));
    };
    const std::string output = R"("output_gcode": "output/plate.gcode")";
    refused(R"({"input_model": "a.stl", "objects": [], )" + output + "}", "ambiguous_plate");
    refused(R"({"objects": [], )" + output + "}", "invalid_plate_objects");
    refused(R"({"objects": [{"model": "../a.stl", "instances": [{"transform": )" + IDENTITY_AT_CENTER + "}]}], " +
                output + "}",
            "invalid_plate_object");
    refused(R"({"objects": [{"model": "a.stl", "instances": []}], )" + output + "}", "invalid_plate_object");
    // Singular, projective, non-finite, and runaway transforms.
    for (const char *transform : {"[0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,1]", "[1,0,0,0, 0,1,0,0, 0,0,1,0, 1,0,0,1]",
                                  "[1,0,0,1e9, 0,1,0,0, 0,0,1,0, 0,0,0,1]", "[1,0,0,0]"})
        refused(std::string(R"({"objects": [{"model": "a.stl", "instances": [{"transform": )") + transform + "}]}], " +
                    output + "}",
                "invalid_instance_transform");
    refused(R"({"input_model": "a.stl", "output_preview": "output/preview.bin", )" + output + "}", "invalid_output_path");
}

TEST_CASE("Inspection request requires both display outputs", "[PlateArtifacts]")
{
    const SinglePlateSliceRequestValidation missing = validate_single_plate_slice_request(
        R"({"protocol_version":1,"job_id":"inspect-1","operation":{"name":"inspect","version":1,"payload":{
            "input_model": "input/cube.obj", "output_mesh": "output/mesh.bin"}}})");
    REQUIRE_FALSE(missing.is_valid());
    CHECK(has_error(missing, "invalid_output_path"));
}

TEST_CASE("Inspection exports display geometry with object bounds", "[PlateArtifacts]")
{
    JobRoot root;
    const SinglePlateSliceRequest request = valid_request(
        R"({"protocol_version":1,"job_id":"inspect-1","operation":{"name":"inspect","version":1,"payload":{
            "input_model": "input/cube.obj", "output_mesh": "output/mesh.bin",
            "output_inspection": "output/inspection.json"}}})");

    const SinglePlateSliceResult result = inspect_model(request, root.path);
    INFO(result.message);
    REQUIRE(result.success);
    REQUIRE(result.artifacts.size() == 2);

    const nlohmann::json inspection = nlohmann::json::parse(root.read("output/inspection.json"));
    REQUIRE(inspection["objects"].size() == 1);
    const nlohmann::json &object = inspection["objects"][0];
    const nlohmann::json &box = object["bounding_box"];
    for (int axis = 0; axis < 3; ++axis)
        CHECK(box["max"][axis].get<double>() - box["min"][axis].get<double>() == Catch::Approx(20.));
    CHECK(object["instances"].size() == 1);

    const std::string mesh = root.read("output/mesh.bin");
    REQUIRE(mesh.compare(0, 8, "OWMESH01") == 0);
    std::uint32_t vertices = 0;
    std::uint32_t triangles = 0;
    const std::size_t offset = object["offset"].get<std::size_t>();
    std::memcpy(&vertices, mesh.data() + offset, 4);
    std::memcpy(&triangles, mesh.data() + offset + 4, 4);
    CHECK(vertices == object["vertex_count"].get<std::uint32_t>());
    CHECK(triangles == object["triangle_count"].get<std::uint32_t>());
    CHECK(mesh.size() == offset + 8 + vertices * 12u + triangles * 12u);
}

TEST_CASE("Explicit placement is sliced as submitted and previewed", "[PlateArtifacts]")
{
    JobRoot root;
    const SinglePlateSliceRequest request = valid_request(slice_manifest(R"({
        "objects": [{"model": "input/cube.obj", "instances": [
            {"transform": [1,0,0,50, 0,1,0,100, 0,0,1,10, 0,0,0,1]},
            {"transform": [1,0,0,130, 0,1,0,100, 0,0,1,10, 0,0,0,1]}
        ]}],
        "output_gcode": "output/plate.gcode",
        "output_preview": "output/preview.bin",
        "output_preview_index": "output/preview.json",
        "settings": {"layer_change_gcode": "G92 E0\n"}
    })"));

    const SinglePlateSliceResult result = slice_single_plate(request, root.path);
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);
    REQUIRE(result.artifacts.size() == 3);

    const std::string gcode = root.read("output/plate.gcode");
    std::size_t layer_changes = 0;
    for (std::size_t at = gcode.find(";LAYER_CHANGE"); at != std::string::npos; at = gcode.find(";LAYER_CHANGE", at + 1))
        ++layer_changes;

    const nlohmann::json index = nlohmann::json::parse(root.read("output/preview.json"));
    CHECK(index["layers"].size() == layer_changes);
    // Both copies are printed where they were placed, and the floating copy is
    // dropped onto the bed: outer walls span X 50 to 70 and 130 to 150, Z 0 to
    // 20. Skirt and brim are left out, since they surround the objects.
    int outer_wall = -1;
    for (const nlohmann::json &role : index["roles"])
        if (role["name"] == "Outer wall")
            outer_wall = role["id"].get<int>();
    REQUIRE(outer_wall >= 0);
    const std::string segments = root.read("output/preview.bin");
    float min_x = 1e9f, max_x = -1e9f, max_z = 0.f;
    for (std::size_t at = 0; at + 36 <= segments.size(); at += 36) {
        if (static_cast<unsigned char>(segments[at + 32]) != outer_wall)
            continue;
        float values[8];
        std::memcpy(values, segments.data() + at, sizeof(values));
        min_x = std::min({min_x, values[0], values[3]});
        max_x = std::max({max_x, values[0], values[3]});
        max_z = std::max({max_z, values[2], values[5]});
    }
    CHECK(min_x == Catch::Approx(50.).margin(0.5));
    CHECK(max_x == Catch::Approx(150.).margin(0.5));
    CHECK(max_z == Catch::Approx(20.).margin(0.3));

    const nlohmann::json &last = index["layers"].back();
    CHECK(root.read("output/preview.bin").size() ==
          last["offset"].get<std::size_t>() + last["count"].get<std::size_t>() * index["segment_bytes"].get<std::size_t>());
}

TEST_CASE("Explicit placement outside the printable area is refused", "[PlateArtifacts]")
{
    JobRoot root;
    const SinglePlateSliceRequest request = valid_request(slice_manifest(R"({
        "objects": [{"model": "input/cube.obj", "instances": [{"transform": [1,0,0,-40, 0,1,0,100, 0,0,1,10, 0,0,0,1]}]}],
        "output_gcode": "output/plate.gcode"
    })"));

    const SinglePlateSliceResult result = slice_single_plate(request, root.path);
    REQUIRE_FALSE(result.success);
    CHECK(result.code == "object_outside_plate");
    CHECK_FALSE(std::filesystem::exists(root.path / "output" / "plate.gcode"));
}

TEST_CASE("Plate request reads per-object settings and height ranges", "[PlateArtifacts]")
{
    const SinglePlateSliceRequest request = valid_request(slice_manifest(R"({
        "objects": [{"model": "input/cube.obj", "instances": [{"transform": )" + IDENTITY_AT_CENTER + R"(}],
            "settings": {"wall_loops": "4", "layer_height": "0.12"},
            "layer_ranges": [
                {"min_z": 10, "max_z": 15, "settings": {"sparse_infill_density": "40%"}},
                {"min_z": 2, "max_z": 5, "settings": {"layer_height": "0.1"}}
            ]}],
        "output_gcode": "output/plate.gcode"
    })"));

    REQUIRE(request.objects.size() == 1);
    CHECK(request.objects[0].settings.size() == 2);
    REQUIRE(request.objects[0].layer_ranges.size() == 2);
    // Ranges are kept in height order whatever order they were sent in.
    CHECK(request.objects[0].layer_ranges[0].min_z == 2.);
    CHECK(request.objects[0].layer_ranges[1].settings[0].first == "sparse_infill_density");
}

TEST_CASE("Plate request refuses settings an object or a range cannot override", "[PlateArtifacts]")
{
    const auto refused = [](const std::string &object_fields, const std::string &code, const std::string &setting) {
        const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(slice_manifest(
            R"({"objects": [{"model": "input/cube.obj", "instances": [{"transform": )" + IDENTITY_AT_CENTER + "}], " +
            object_fields + R"(}], "output_gcode": "output/plate.gcode"})"));
        INFO(object_fields);
        REQUIRE_FALSE(validation.is_valid());
        CHECK(validation.errors.front().code == code);
        CHECK(validation.errors.front().setting == setting);
    };
    // A skirt surrounds the whole plate, and supports are generated per object.
    refused(R"("settings": {"skirt_loops": "2"})", "setting_not_per_object", "skirt_loops");
    refused(R"("layer_ranges": [{"min_z": 0, "max_z": 5, "settings": {"enable_support": "1"}}])", "setting_not_per_object",
            "enable_support");
    refused(R"("settings": {"wall_loops": 4})", "invalid_setting_value", "");
    refused(R"("layer_ranges": [{"min_z": 5, "max_z": 5, "settings": {"wall_loops": "2"}}])", "invalid_layer_ranges", "");
    refused(R"("layer_ranges": [{"min_z": 0, "max_z": 5, "settings": {}}])", "invalid_layer_ranges", "");
    refused(R"("layer_ranges": [{"min_z": 0, "max_z": 6, "settings": {"wall_loops": "2"}},
                                {"min_z": 5, "max_z": 9, "settings": {"wall_loops": "3"}}])",
            "invalid_layer_ranges", "");
}

namespace {

// Distinct heights of the outer walls printed on either side of X = 100, from
// a sliced plate's layer preview.
std::pair<std::set<int>, std::set<int>> outer_wall_heights(const JobRoot &root)
{
    const nlohmann::json index = nlohmann::json::parse(root.read("output/preview.json"));
    int outer_wall = -1;
    for (const nlohmann::json &role : index["roles"])
        if (role["name"] == "Outer wall")
            outer_wall = role["id"].get<int>();
    REQUIRE(outer_wall >= 0);
    const std::string segments = root.read("output/preview.bin");
    std::set<int> left, right;
    for (std::size_t at = 0; at + 36 <= segments.size(); at += 36) {
        if (static_cast<unsigned char>(segments[at + 32]) != outer_wall)
            continue;
        float values[6];
        std::memcpy(values, segments.data() + at, sizeof(values));
        (values[3] < 100.f ? left : right).insert(int(std::lround(values[5] * 1000.f)));
    }
    return {left, right};
}

std::string two_cubes(const std::string &second_cube_fields)
{
    return slice_manifest(R"({
        "objects": [
            {"model": "input/cube.obj", "instances": [{"transform": [1,0,0,50, 0,1,0,100, 0,0,1,10, 0,0,0,1]}]},
            {"model": "input/cube.obj", "instances": [{"transform": [1,0,0,140, 0,1,0,100, 0,0,1,10, 0,0,0,1]}], )" +
                          second_cube_fields + R"(}
        ],
        "output_gcode": "output/plate.gcode",
        "output_preview": "output/preview.bin",
        "output_preview_index": "output/preview.json",
        "settings": {"layer_height": "0.2", "initial_layer_print_height": "0.2"}
    })");
}

} // namespace

TEST_CASE("A per-object override changes the G-code of that object only", "[PlateArtifacts]")
{
    JobRoot root;
    const SinglePlateSliceResult result = slice_single_plate(valid_request(two_cubes(R"("settings": {"layer_height": "0.1"})")),
                                                             root.path);
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);

    const auto [plain, overridden] = outer_wall_heights(root);
    // The plate keeps 0.2 mm layers over the 20 mm cube; the overridden copy
    // is printed in 0.1 mm layers after the shared first layer.
    CHECK(plain.size() == 100);
    CHECK(overridden.size() == 199);
    CHECK(overridden.count(100) == 0);
    CHECK(overridden.count(300) == 1);
    CHECK(plain.count(300) == 0);
}

TEST_CASE("A height range override changes only that part of its object", "[PlateArtifacts]")
{
    JobRoot root;
    const SinglePlateSliceResult result = slice_single_plate(
        valid_request(two_cubes(R"("layer_ranges": [{"min_z": 10, "max_z": 20, "settings": {"layer_height": "0.1"}}])")),
        root.path);
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);

    const auto [plain, ranged] = outer_wall_heights(root);
    CHECK(plain.size() == 100);
    const auto below = std::count_if(ranged.begin(), ranged.end(), [](int z) { return z <= 10000; });
    CHECK(below == 50);
    CHECK(ranged.size() - below == 100);
}
