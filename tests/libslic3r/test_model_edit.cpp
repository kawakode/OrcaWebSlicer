#include <catch2/catch_all.hpp>

#include "libslic3r/Format/STL.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/TriangleMesh.hpp"
#include "libslic3r/Web/ArtifactTransaction.hpp"
#include "libslic3r/Web/ModelEdit.hpp"

#include <boost/filesystem.hpp>
#include <filesystem>
#include <fstream>
#include <nlohmann/json.hpp>
#include <string>

using namespace Slic3r;
using namespace Slic3r::Web;

namespace {

// The 20 mm cube spans 0 to 20 on every axis, so this places it at X and Y 100 to 120.
const std::string AT_100 = "[1,0,0,100, 0,1,0,100, 0,0,1,0, 0,0,0,1]";
const std::string OUTPUTS = R"("output_models": ["output/part-0.stl", "output/part-1.stl"], "output_report": "output/edit.json")";

std::string edit_manifest(const std::string &payload)
{
    return R"({"protocol_version":1,"job_id":"edit-1","operation":{"name":"edit","version":1,"payload":)" + payload +
           "}}";
}

bool has_error(const SinglePlateSliceRequestValidation &validation, const std::string &code)
{
    for (const WorkerManifestError &error : validation.errors)
        if (error.code == code)
            return true;
    return false;
}

struct JobRoot
{
    std::filesystem::path path;

    JobRoot()
    {
        const std::filesystem::path created = std::filesystem::temp_directory_path() /
                                              ("orca-edit-" + boost::filesystem::unique_path().string());
        std::filesystem::create_directories(created / "input");
        std::filesystem::copy_file(std::filesystem::path(TEST_DATA_DIR) / "20mm_cube.obj", created / "input" / "cube.obj");
        WorkerManifestError error;
        REQUIRE(canonicalize_job_root(created, path, error));
    }
    ~JobRoot() { std::filesystem::remove_all(path); }

    nlohmann::json report() const
    {
        std::ifstream input(path / "output" / "edit.json", std::ios::binary);
        return nlohmann::json::parse(input);
    }

    // Reads a produced part back through the importer a later upload uses.
    BoundingBoxf3 part_bounds(const std::string &relative) const
    {
        Model model;
        REQUIRE(load_stl((path / relative).string().c_str(), &model));
        REQUIRE(model.objects.size() == 1);
        return bounding_box(model.objects.front()->raw_indexed_triangle_set());
    }
};

SinglePlateSliceResult run(const JobRoot &root, const std::string &payload)
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(edit_manifest(payload));
    INFO(payload);
    REQUIRE(validation.is_valid());
    return edit_model(*validation.request, root.path);
}

void check_box(const BoundingBoxf3 &box, const Vec3d &min, const Vec3d &max)
{
    for (int axis = 0; axis < 3; ++axis) {
        CHECK(box.min[axis] == Catch::Approx(min[axis]).margin(1e-4));
        CHECK(box.max[axis] == Catch::Approx(max[axis]).margin(1e-4));
    }
}

} // namespace

TEST_CASE("Edit request names a tool, a placement, and its outputs", "[ModelEdit]")
{
    const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(edit_manifest(
        R"({"input_model": "input/cube.obj", "object": 0, "transform": )" + AT_100 + R"(, "tool": "cut",
            "cut": {"point": [110, 110, 5], "normal": [0, 0, 2], "keep": "upper", "flip_upper": true}, )" +
        OUTPUTS + "}"));
    REQUIRE(validation.is_valid());
    const SinglePlateSliceRequest &request = *validation.request;
    CHECK(request.operation == WorkerOperation::Edit);
    CHECK(request.edit.tool == ModelEdit::Tool::Cut);
    CHECK(request.edit.transform[3] == 100.);
    CHECK(request.edit.plane_point[2] == 5.);
    CHECK(request.edit.keep_upper);
    CHECK_FALSE(request.edit.keep_lower);
    CHECK(request.edit.flip_upper);
    CHECK(request.output_models.size() == 2);
    CHECK(request.output_report == "output/edit.json");
}

TEST_CASE("Edit request refuses unusable tools and outputs", "[ModelEdit]")
{
    const auto refused = [](const std::string &payload, const std::string &code) {
        const SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(edit_manifest(payload));
        INFO(payload);
        CHECK_FALSE(validation.is_valid());
        CHECK(has_error(validation, code));
    };
    const std::string model = R"({"input_model": "input/cube.obj", )";
    const std::string placed = model + R"("transform": )" + AT_100 + ", ";
    refused(placed + R"("tool": "drill", )" + OUTPUTS + "}", "invalid_edit_tool");
    refused(model + R"("tool": "repair", )" + OUTPUTS + "}", "invalid_instance_transform");
    refused(model + R"("transform": [0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,1], "tool": "repair", )" + OUTPUTS + "}",
            "invalid_instance_transform");
    refused(placed + R"("tool": "cut", )" + OUTPUTS + "}", "invalid_cut_plane");
    refused(placed + R"("tool": "cut", "cut": {"point": [0, 0, 0], "normal": [0, 0, 0]}, )" + OUTPUTS + "}",
            "invalid_cut_plane");
    refused(placed + R"("tool": "cut", "cut": {"point": [0, 0, 0], "normal": [0, 0, 1], "keep": "middle"}, )" +
                OUTPUTS + "}",
            "invalid_cut_plane");
    refused(placed + R"("tool": "repair", "output_report": "output/edit.json"})", "invalid_output_path");
    refused(placed + R"("tool": "repair", "output_models": ["a.stl", "b.stl", "c.stl"], "output_report": "e.json"})",
            "invalid_output_path");
    refused(placed + R"("tool": "repair", "output_models": ["output/part.obj"], "output_report": "e.json"})",
            "invalid_output_path");
    refused(placed + R"("tool": "repair", "output_models": ["output/part.stl"]})", "invalid_output_path");
}

TEST_CASE("Cutting a cube in half gives two parts with the right bounds", "[ModelEdit]")
{
    JobRoot root;
    const SinglePlateSliceResult result = run(root, R"({"input_model": "input/cube.obj", "transform": )" + AT_100 +
                                                        R"(, "tool": "cut", "cut": {"point": [0, 0, 10], "normal": [0, 0, 1]}, )" +
                                                        OUTPUTS + "}");
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);
    REQUIRE(result.artifacts.size() == 3);
    CHECK(result.artifacts[0] == std::make_pair(std::string("model"), std::string("output/part-0.stl")));
    CHECK(result.artifacts[2] == std::make_pair(std::string("edit_report"), std::string("output/edit.json")));

    // Both parts keep their place on the plate, and each is closed by its cap.
    check_box(root.part_bounds("output/part-0.stl"), {100, 100, 10}, {120, 120, 20});
    check_box(root.part_bounds("output/part-1.stl"), {100, 100, 0}, {120, 120, 10});
    const nlohmann::json report = root.report();
    CHECK(report["tool"] == "cut");
    REQUIRE(report["parts"].size() == 2);
    CHECK(report["parts"][0]["side"] == "upper");
    CHECK(report["parts"][1]["side"] == "lower");
    CHECK(report["parts"][1]["bounding_box"]["max"][2].get<double>() == Catch::Approx(10.));
    for (const char *path : {"output/part-0.stl", "output/part-1.stl"}) {
        TriangleMesh mesh;
        REQUIRE(mesh.ReadSTLFile((root.path / path).string().c_str()));
        CHECK(its_num_open_edges(mesh.its) == 0);
        CHECK(mesh.volume() == Catch::Approx(20. * 20. * 10.).epsilon(1e-4));
    }
}

TEST_CASE("A cut keeps only the requested side and bakes the placement in", "[ModelEdit]")
{
    JobRoot root;
    // Scaled by 2 and raised 5 mm: the cube spans Z 5 to 45, cut at 15.
    const SinglePlateSliceResult result = run(
        root, R"({"input_model": "input/cube.obj", "transform": [2,0,0,50, 0,2,0,60, 0,0,2,5, 0,0,0,1], "tool": "cut",
                  "cut": {"point": [0, 0, 15], "normal": [0, 0, 1], "keep": "lower"}, )" +
                  OUTPUTS + "}");
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);
    REQUIRE(result.artifacts.size() == 2);
    check_box(root.part_bounds("output/part-0.stl"), {50, 60, 5}, {90, 100, 15});
    CHECK(root.report()["parts"][0]["side"] == "lower");
    CHECK_FALSE(std::filesystem::exists(root.path / "output" / "part-1.stl"));
}

TEST_CASE("A flipped upper part keeps its size", "[ModelEdit]")
{
    JobRoot root;
    const SinglePlateSliceResult result = run(root, R"({"input_model": "input/cube.obj", "transform": )" + AT_100 +
                                                        R"(, "tool": "cut", "cut": {"point": [0, 0, 5], "normal": [0, 0, 1],
                                                        "keep": "upper", "flip_upper": true}, )" +
                                                        OUTPUTS + "}");
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);
    const BoundingBoxf3 box = root.part_bounds("output/part-0.stl");
    CHECK(box.size().x() == Catch::Approx(20.));
    CHECK(box.size().y() == Catch::Approx(20.));
    CHECK(box.size().z() == Catch::Approx(15.));
}

TEST_CASE("A tilted cut laid on its cut face rests on that face", "[ModelEdit]")
{
    JobRoot root;
    // A plane through the cube's centre, tilted 45 degrees about Y.
    const SinglePlateSliceResult result = run(root, R"({"input_model": "input/cube.obj", "transform": )" + AT_100 +
                                                        R"(, "tool": "cut", "cut": {"point": [110, 110, 10], "normal": [1, 0, 1],
                                                        "place_on_cut": true}, )" +
                                                        OUTPUTS + "}");
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);
    for (const char *path : {"output/part-0.stl", "output/part-1.stl"}) {
        TriangleMesh mesh;
        REQUIRE(mesh.ReadSTLFile((root.path / path).string().c_str()));
        // The lowest facets are the cut cap, which now faces straight down.
        const double lowest = mesh.bounding_box().min.z();
        bool cap_down = false;
        for (std::size_t index = 0; index < mesh.its.indices.size(); ++index) {
            const Vec3f normal = its_face_normal(mesh.its, index);
            bool on_floor = true;
            for (int corner = 0; corner < 3; ++corner)
                on_floor = on_floor && std::abs(mesh.its.vertices[mesh.its.indices[index][corner]].z() - lowest) < 1e-3;
            cap_down = cap_down || (on_floor && normal.z() < -0.999f);
        }
        CHECK(cap_down);
        CHECK(mesh.volume() == Catch::Approx(20. * 20. * 20. / 2.).epsilon(1e-3));
    }
}

TEST_CASE("A cut plane that misses the object is refused", "[ModelEdit]")
{
    JobRoot root;
    const SinglePlateSliceResult result = run(root, R"({"input_model": "input/cube.obj", "transform": )" + AT_100 +
                                                        R"(, "tool": "cut", "cut": {"point": [0, 0, 25], "normal": [0, 0, 1]}, )" +
                                                        OUTPUTS + "}");
    REQUIRE_FALSE(result.success);
    CHECK(result.code == "cut_plane_misses_object");
    CHECK(result.category == WorkerErrorCategory::Validation);
    CHECK_FALSE(std::filesystem::exists(root.path / "output" / "part-0.stl"));
}

TEST_CASE("An unknown object index is refused", "[ModelEdit]")
{
    JobRoot root;
    const SinglePlateSliceResult result =
        run(root, R"({"input_model": "input/cube.obj", "object": 3, "transform": )" + AT_100 + R"(, "tool": "repair", )" +
                      OUTPUTS + "}");
    REQUIRE_FALSE(result.success);
    CHECK(result.code == "unknown_model_object");
}

TEST_CASE("Repair closes a hole and keeps the placement", "[ModelEdit]")
{
    JobRoot root;
    // A cube missing one triangle of its top face.
    indexed_triangle_set open = its_make_cube(20, 20, 20);
    for (std::size_t index = 0; index < open.indices.size(); ++index) {
        const Vec3f normal = its_face_normal(open, index);
        if (normal.z() > 0.99f) {
            open.indices.erase(open.indices.begin() + index);
            break;
        }
    }
    REQUIRE(its_num_open_edges(open) == 3);
    REQUIRE(its_write_stl_binary((root.path / "input" / "open.stl").string().c_str(), "open", open));

    const SinglePlateSliceResult result =
        run(root, R"({"input_model": "input/open.stl", "transform": )" + AT_100 + R"(, "tool": "repair", )" + OUTPUTS + "}");
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);
    REQUIRE(result.artifacts.size() == 2);

    const nlohmann::json report = root.report();
    CHECK(report["tool"] == "repair");
    REQUIRE(report["parts"].size() == 1);
    CHECK(report["parts"][0]["side"] == "whole");
    CHECK(report["repair"]["open_edges_before"] == 3);
    CHECK(report["repair"]["open_edges_after"] == 0);
    CHECK(report["repair"]["facets_added"].get<int>() >= 1);

    TriangleMesh repaired;
    REQUIRE(repaired.ReadSTLFile((root.path / "output" / "part-0.stl").string().c_str()));
    CHECK(its_num_open_edges(repaired.its) == 0);
    check_box(repaired.bounding_box(), {100, 100, 0}, {120, 120, 20});
}
