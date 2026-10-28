#include <catch2/catch_all.hpp>

#include "libslic3r/Model.hpp"
#include "libslic3r/Format/OBJ.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Web/ArtifactTransaction.hpp"
#include "libslic3r/Web/SinglePlateSlice.hpp"

#include <algorithm>
#include <boost/filesystem.hpp>
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

// Writes a desktop-style project with two plates on the default 200 mm bed,
// whose plates the desktop lays out 240 mm apart: cube A spans X 90 to 110 on
// the first plate, and cube B X 50 to 70 and Y 70 to 90 on the second, so X 290
// to 310 in the shared coordinates the archive stores. Saved as
// tests/data/web-worker/two-plates.3mf for the browser suite.
void write_two_plate_project(const std::filesystem::path &target)
{
    Slic3r::Model model;
    Slic3r::ObjInfo obj_info;
    std::string message;
    REQUIRE(Slic3r::load_obj((std::filesystem::path(TEST_DATA_DIR) / "20mm_cube.obj").string().c_str(), &model,
                             obj_info, message));
    model.objects.front()->name = "A";
    model.add_object(*model.objects.front())->name = "B";
    model.add_default_instances();
    model.objects[0]->instances[0]->set_offset({90., 90., 0.});
    model.objects[1]->instances[0]->set_offset({290., 70., 0.});
    const std::filesystem::path backup = target.parent_path() / "backup";
    std::filesystem::create_directories(backup);
    model.set_backup_path(backup.string());

    // Only what the test relies on is stored; the worker fills in the rest from
    // the engine defaults, as it does for any project. The engine defaults use
    // relative extrusion without resetting it.
    Slic3r::DynamicPrintConfig config;
    config.set_key_value("printable_area", new Slic3r::ConfigOptionPoints{
                                               Slic3r::Vec2d(0, 0), Slic3r::Vec2d(200, 0), Slic3r::Vec2d(200, 200),
                                               Slic3r::Vec2d(0, 200)});
    config.set_key_value("layer_change_gcode", new Slic3r::ConfigOptionString("G92 E0\n"));
    Slic3r::PlateData first;
    Slic3r::PlateData second;
    first.plate_index = 0;
    first.plate_name = "First";
    first.objects_and_instances = {{0, 0}};
    second.plate_index = 1;
    second.plate_name = "Second";
    second.objects_and_instances = {{1, 0}};

    Slic3r::StoreParams params;
    const std::string path = target.string();
    params.path = path.c_str();
    params.model = &model;
    params.config = &config;
    params.plate_data_list = {&first, &second};
    params.strategy = Slic3r::SaveStrategy::Zip64 | Slic3r::SaveStrategy::Silence;
    REQUIRE(Slic3r::store_bbs_3mf(params));
}

// The extents of the outer walls a sliced preview drew.
struct OuterWalls
{
    float min_x {1e9f}, max_x {-1e9f}, min_y {1e9f}, max_y {-1e9f}, max_z {0.f};
};

OuterWalls outer_walls(const JobRoot &root, const std::string &preview, const std::string &preview_index)
{
    const nlohmann::json index = nlohmann::json::parse(root.read(preview_index));
    int outer_wall = -1;
    for (const nlohmann::json &role : index["roles"])
        if (role["name"] == "Outer wall")
            outer_wall = role["id"].get<int>();
    REQUIRE(outer_wall >= 0);
    const std::string segments = root.read(preview);
    OuterWalls walls;
    for (std::size_t at = 0; at + 36 <= segments.size(); at += 36) {
        if (static_cast<unsigned char>(segments[at + 32]) != outer_wall)
            continue;
        float values[8];
        std::memcpy(values, segments.data() + at, sizeof(values));
        walls.min_x = std::min({walls.min_x, values[0], values[3]});
        walls.max_x = std::max({walls.max_x, values[0], values[3]});
        walls.min_y = std::min({walls.min_y, values[1], values[4]});
        walls.max_y = std::max({walls.max_y, values[1], values[4]});
        walls.max_z = std::max({walls.max_z, values[2], values[5]});
    }
    return walls;
}

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
    const OuterWalls walls = outer_walls(root, "output/preview.bin", "output/preview.json");
    CHECK(walls.min_x == Catch::Approx(50.).margin(0.5));
    CHECK(walls.max_x == Catch::Approx(150.).margin(0.5));
    CHECK(walls.max_z == Catch::Approx(20.).margin(0.3));

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

TEST_CASE("A multi-plate project is inspected plate by plate", "[PlateArtifacts]")
{
    JobRoot root;
    write_two_plate_project(root.path / "input" / "project.3mf");
    const SinglePlateSliceRequest request = valid_request(
        R"({"protocol_version":1,"job_id":"inspect-1","operation":{"name":"inspect","version":1,"payload":{
            "input_model": "input/project.3mf", "output_mesh": "output/mesh.bin",
            "output_inspection": "output/inspection.json"}}})");

    const SinglePlateSliceResult result = inspect_model(request, root.path);
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);

    const nlohmann::json inspection = nlohmann::json::parse(root.read("output/inspection.json"));
    CHECK(inspection["declares_plate"] == true);
    REQUIRE(inspection["plates"].size() == 2);
    CHECK(inspection["plates"][0]["name"] == "First");
    CHECK(inspection["plates"][1]["index"] == 2);
    CHECK(inspection["plates"][1]["name"] == "Second");

    // Every object of every plate is listed, each instance with its plate and
    // a placement relative to that plate's own bed. The importer centres each
    // mesh on its origin, so a translation is the cube's centre.
    REQUIRE(inspection["objects"].size() == 2);
    const nlohmann::json &first = inspection["objects"][0]["instances"][0];
    const nlohmann::json &second = inspection["objects"][1]["instances"][0];
    CHECK(first["plate"] == 1);
    CHECK(first["transform"][3].get<double>() == Catch::Approx(100.));
    CHECK(second["plate"] == 2);
    CHECK(second["transform"][3].get<double>() == Catch::Approx(60.));
    CHECK(second["transform"][7].get<double>() == Catch::Approx(80.));
}

TEST_CASE("Each plate of a project slices on its own bed", "[PlateArtifacts]")
{
    JobRoot root;
    write_two_plate_project(root.path / "input" / "project.3mf");
    const auto manifest = [](unsigned plate) {
        return slice_manifest(R"({"input_model": "input/project.3mf", "plate_index": )" + std::to_string(plate) + R"(,
            "output_gcode": "output/plate.gcode", "output_preview": "output/preview.bin",
            "output_preview_index": "output/preview.json"})");
    };

    // Plate 2 holds only cube B, printed where it sits on that plate.
    const SinglePlateSliceResult second = slice_single_plate(valid_request(manifest(2)), root.path);
    INFO(second.code << ": " << second.message);
    REQUIRE(second.success);
    const OuterWalls walls = outer_walls(root, "output/preview.bin", "output/preview.json");
    CHECK(walls.min_x == Catch::Approx(50.).margin(0.5));
    CHECK(walls.max_x == Catch::Approx(70.).margin(0.5));
    CHECK(walls.min_y == Catch::Approx(70.).margin(0.5));
    CHECK(walls.max_y == Catch::Approx(90.).margin(0.5));

    const SinglePlateSliceResult missing = slice_single_plate(valid_request(manifest(3)), root.path);
    REQUIRE_FALSE(missing.success);
    CHECK(missing.code == "plate_index_out_of_range");
}

TEST_CASE("Objects of a later project plate are placed by index", "[PlateArtifacts]")
{
    JobRoot root;
    write_two_plate_project(root.path / "input" / "project.3mf");
    // Object 1 is cube B: indices span every plate, as the inspection lists them.
    const SinglePlateSliceRequest request = valid_request(slice_manifest(R"({
        "objects": [{"model": "input/project.3mf", "object": 1, "instances": [
            {"transform": [1,0,0,40, 0,1,0,50, 0,0,1,10, 0,0,0,1]}]}],
        "plate_index": 2,
        "output_gcode": "output/plate.gcode",
        "output_preview": "output/preview.bin",
        "output_preview_index": "output/preview.json"
    })"));

    const SinglePlateSliceResult result = slice_single_plate(request, root.path);
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);
    const OuterWalls walls = outer_walls(root, "output/preview.bin", "output/preview.json");
    // The archive centred cube B on its origin, so it spans 30 to 50 by 40 to 60.
    CHECK(walls.min_x == Catch::Approx(30.).margin(0.5));
    CHECK(walls.max_y == Catch::Approx(60.).margin(0.5));
}
