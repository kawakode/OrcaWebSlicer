#include <catch2/catch_all.hpp>

#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/TriangleMesh.hpp"
#include "libslic3r/Web/ArtifactTransaction.hpp"
#include "libslic3r/Web/PlateThumbnail.hpp"
#include "libslic3r/Web/SinglePlateSlice.hpp"
#include "libslic3r/miniz_extension.hpp"

#include <boost/filesystem.hpp>
#include <filesystem>
#include <fstream>
#include <nlohmann/json.hpp>
#include <string>

using namespace Slic3r;
using namespace Slic3r::Web;

namespace {

// A job directory holding the 20 mm cube and three small job-local profiles,
// shaped like the flattened profiles the API stages. Removed afterwards.
struct ProjectJobRoot
{
    std::filesystem::path path;

    ProjectJobRoot()
    {
        const std::filesystem::path created = std::filesystem::temp_directory_path() /
                                              ("orca-project-" + boost::filesystem::unique_path().string());
        std::filesystem::create_directories(created / "input");
        std::filesystem::create_directories(created / "profiles");
        std::filesystem::copy_file(std::filesystem::path(TEST_DATA_DIR) / "20mm_cube.obj", created / "input" / "cube.obj");
        write(created / "profiles" / "machine.json", R"({"name": "Web Test Printer", "printer_model": "Web Test"})");
        write(created / "profiles" / "process.json", R"({"name": "0.20mm Web Test", "layer_height": "0.2"})");
        write(created / "profiles" / "filament.json",
              R"({"name": "Web Test PLA", "filament_id": "GFL99", "filament_colour": ["#C03020"]})");
        WorkerManifestError error;
        REQUIRE(canonicalize_job_root(created, path, error));
    }
    ~ProjectJobRoot() { std::filesystem::remove_all(path); }

    static void write(const std::filesystem::path &target, const std::string &contents)
    {
        std::ofstream output(target, std::ios::binary);
        output << contents;
    }

    std::string read(const std::string &relative) const
    {
        std::ifstream input(path / relative, std::ios::binary);
        return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
    }
};

std::string manifest(const std::string &operation, const std::string &payload)
{
    return R"({"protocol_version":1,"job_id":"project-1","operation":{"name":")" + operation +
           R"(","version":1,"payload":)" + payload + "}}";
}

SinglePlateSliceRequest valid_request(const std::string &serialized)
{
    SinglePlateSliceRequestValidation validation = validate_single_plate_slice_request(serialized);
    INFO(serialized);
    REQUIRE(validation.is_valid());
    return *validation.request;
}

const std::string PROFILES =
    R"("profiles": {"machine": "profiles/machine.json", "process": "profiles/process.json", "filament": "profiles/filament.json"})";

const std::string TWO_CUBES = R"("objects": [{"model": "input/cube.obj", "instances": [
    {"transform": [1,0,0,60, 0,1,0,80, 0,0,1,10, 0,0,0,1]},
    {"transform": [0,-1,0,140, 1,0,0,80, 0,0,1,10, 0,0,0,1]}
]}])";

// The engine defaults use relative extrusion, which needs a reset per layer.
const std::string RESET_EXTRUSION = R"("settings": {"layer_change_gcode": "G92 E0\n"})";

const std::string OVERRIDES = R"("settings": {"sparse_infill_density": "35%", "nozzle_temperature": "215"})";

BoundingBoxf3 instance_box(const ModelObject &object, std::size_t instance)
{
    return object.instance_bounding_box(instance);
}

} // namespace

TEST_CASE("Project export requests name a 3MF output", "[ProjectExport]")
{
    const SinglePlateSliceRequest request =
        valid_request(manifest("export_project", "{" + TWO_CUBES + R"(, "output_project": "output/project.3mf"})"));
    CHECK(request.operation == WorkerOperation::ExportProject);
    CHECK(request.output_project == "output/project.3mf");
    CHECK(request.output_gcode.empty());

    for (const char *payload : {R"({"input_model": "input/cube.obj"})",
                                R"({"input_model": "input/cube.obj", "output_project": "output/project.gcode"})",
                                R"({"input_model": "input/cube.obj", "output_project": "../project.3mf"})"}) {
        INFO(payload);
        const SinglePlateSliceRequestValidation refused = validate_single_plate_slice_request(manifest("export_project", payload));
        CHECK_FALSE(refused.is_valid());
    }

    const SinglePlateSliceRequestValidation thumbnails = validate_single_plate_slice_request(
        manifest("slice", R"({"input_model": "input/cube.obj", "output_gcode": "output/a.gcode", "thumbnails": "yes"})"));
    CHECK_FALSE(thumbnails.is_valid());
}

TEST_CASE("An exported project reloads in the desktop importer", "[ProjectExport]")
{
    ProjectJobRoot root;
    const SinglePlateSliceRequest request = valid_request(manifest(
        "export_project", "{" + TWO_CUBES + ", " + PROFILES + ", " + OVERRIDES + R"(, "output_project": "output/project.3mf"})"));

    const SinglePlateSliceResult result = slice_single_plate(request, root.path);
    INFO(result.code << ": " << result.message);
    REQUIRE(result.success);
    REQUIRE(result.artifacts.size() == 1);
    CHECK(result.artifacts.front().first == "project");
    // The exporter's staging directory and temporary archive are gone.
    for (const auto &entry : std::filesystem::directory_iterator(root.path))
        CHECK(entry.path().filename().string().rfind(".project-1", 0) == std::string::npos);
    CHECK_FALSE(std::filesystem::exists(root.path / "output" / "project.3mf.tmp"));

    const std::string archive = (root.path / "output" / "project.3mf").string();
    Model                     model;
    DynamicPrintConfig        config;
    ConfigSubstitutionContext substitutions(ForwardCompatibilitySubstitutionRule::Enable);
    PlateDataPtrs             plates;
    std::vector<Preset *>     presets;
    bool                      is_bbl_3mf = false;
    bool                      is_orca_3mf = false;
    Semver                    version;
    // The same strategy the desktop opens a project with.
    REQUIRE(load_bbs_3mf(archive.c_str(), &config, &substitutions, &model, &plates, &presets, &is_bbl_3mf, &is_orca_3mf,
                         &version, nullptr, LoadStrategy::LoadModel | LoadStrategy::LoadConfig | LoadStrategy::LoadAuxiliary));
    CHECK(is_orca_3mf);

    REQUIRE(plates.size() == 1);
    // The importer records plate membership per object; the desktop places
    // each instance on its plate from its position.
    CHECK(plates.front()->obj_inst_map.size() == 1);
    REQUIRE(model.objects.size() == 1);
    REQUIRE(model.objects.front()->instances.size() == 2);
    // Both copies keep their placement, the floating one dropped onto the bed.
    const BoundingBoxf3 first = instance_box(*model.objects.front(), 0);
    const BoundingBoxf3 second = instance_box(*model.objects.front(), 1);
    CHECK(first.min.x() == Catch::Approx(60.).margin(1e-3));
    CHECK(first.max.y() == Catch::Approx(100.).margin(1e-3));
    CHECK(first.min.z() == Catch::Approx(0.).margin(1e-3));
    // The second copy is turned a quarter about Z.
    CHECK(second.min.x() == Catch::Approx(120.).margin(1e-3));
    CHECK(second.max.x() == Catch::Approx(140.).margin(1e-3));
    CHECK(second.max.z() == Catch::Approx(20.).margin(1e-3));

    CHECK(config.opt_string("printer_settings_id") == "Web Test Printer");
    CHECK(config.opt_string("print_settings_id") == "0.20mm Web Test");
    CHECK(config.option<ConfigOptionStrings>("filament_settings_id")->values == std::vector<std::string>{"Web Test PLA"});
    CHECK(config.option<ConfigOptionStrings>("filament_ids")->values == std::vector<std::string>{"GFL99"});
    CHECK(config.opt_serialize("sparse_infill_density") == "35%");
    CHECK(config.option<ConfigOptionInts>("nozzle_temperature")->get_at(0) == 215);
    const std::vector<std::string> &different = config.option<ConfigOptionStrings>("different_settings_to_system")->values;
    REQUIRE(different.size() == 3);
    CHECK(different[0] == "sparse_infill_density");
    CHECK(different[1] == "nozzle_temperature");
    CHECK(different[2].empty());
    CHECK_FALSE(config.has("inherits"));

    mz_zip_archive zip;
    mz_zip_zero_struct(&zip);
    REQUIRE(open_zip_reader(&zip, archive));
    CHECK(mz_zip_reader_locate_file(&zip, "Metadata/plate_1.png", nullptr, 0) >= 0);
    close_zip_reader(&zip);

    release_PlateData_list(plates);
    for (Preset *preset : presets)
        delete preset;
}

TEST_CASE("An exported project reopens in the browser with its selection", "[ProjectExport]")
{
    ProjectJobRoot root;
    const SinglePlateSliceResult exported = slice_single_plate(
        valid_request(manifest("export_project", "{" + TWO_CUBES + ", " + PROFILES + ", " + OVERRIDES +
                                                     R"(, "output_project": "input/saved.3mf"})")),
        root.path);
    INFO(exported.code << ": " << exported.message);
    REQUIRE(exported.success);

    const SinglePlateSliceResult inspected = inspect_model(
        valid_request(manifest("inspect", R"({"input_model": "input/saved.3mf", "output_mesh": "output/mesh.bin",
                                             "output_inspection": "output/inspection.json"})")),
        root.path);
    INFO(inspected.code << ": " << inspected.message);
    REQUIRE(inspected.success);

    const nlohmann::json inspection = nlohmann::json::parse(root.read("output/inspection.json"));
    CHECK(inspection["declares_plate"] == true);
    REQUIRE(inspection["objects"].size() == 1);
    CHECK(inspection["objects"][0]["instances"].size() == 2);
    const nlohmann::json &project = inspection["project"];
    CHECK(project["printer"] == "Web Test Printer");
    CHECK(project["process"] == "0.20mm Web Test");
    CHECK(project["filaments"] == nlohmann::json::array({"Web Test PLA"}));
    CHECK(project["settings"] == nlohmann::json({{"sparse_infill_density", "35%"}, {"nozzle_temperature", "215"}}));

    // Slicing the reopened plate as the browser submits it keeps the placement.
    const nlohmann::json &first = inspection["objects"][0]["instances"][0]["transform"];
    const SinglePlateSliceResult sliced = slice_single_plate(
        valid_request(manifest("slice", R"({"objects": [{"model": "input/saved.3mf", "instances": [{"transform": )" +
                                            first.dump() + R"(}]}], "output_gcode": "output/plate.gcode", )" +
                                            RESET_EXTRUSION + "}")),
        root.path);
    INFO(sliced.code << ": " << sliced.message);
    CHECK(sliced.success);
}

TEST_CASE("Sliced G-code embeds rendered thumbnails only when asked", "[ProjectExport]")
{
    ProjectJobRoot root;
    const std::string plate = R"("objects": [{"model": "input/cube.obj", "instances": [
        {"transform": [1,0,0,100, 0,1,0,100, 0,0,1,10, 0,0,0,1]}]}])";

    const SinglePlateSliceResult plain = slice_single_plate(
        valid_request(manifest("slice", "{" + plate + R"(, "output_gcode": "output/plain.gcode", )" + RESET_EXTRUSION + "}")),
        root.path);
    REQUIRE(plain.success);
    CHECK(root.read("output/plain.gcode").find("thumbnail begin") == std::string::npos);

    const SinglePlateSliceResult thumbnails = slice_single_plate(
        valid_request(manifest("slice", "{" + plate + R"(, "output_gcode": "output/thumb.gcode", "thumbnails": true, )" +
                                            RESET_EXTRUSION + "}")),
        root.path);
    INFO(thumbnails.code << ": " << thumbnails.message);
    REQUIRE(thumbnails.success);
    const std::string gcode = root.read("output/thumb.gcode");
    // The engine default asks for 48x48 and 300x300 PNG thumbnails.
    CHECK(gcode.find("; thumbnail begin 48x48") != std::string::npos);
    CHECK(gcode.find("; thumbnail begin 300x300") != std::string::npos);
}

TEST_CASE("Plate thumbnails draw the placed objects on a transparent background", "[ProjectExport]")
{
    Model model;
    ModelObject *object = model.add_object();
    object->add_volume(make_cube(20., 20., 20.));
    object->add_instance()->set_offset(Vec3d(100., 100., 0.));

    const ThumbnailData thumbnail = render_plate_thumbnail(model, 64, 32, {200, 100, 50});
    REQUIRE(thumbnail.is_valid());
    std::size_t opaque = 0;
    for (std::size_t pixel = 0; pixel < 64 * 32; ++pixel)
        opaque += thumbnail.pixels[4 * pixel + 3] == 255;
    CHECK(opaque > 64 * 32 / 8);
    CHECK(thumbnail.pixels[3] == 0);
    CHECK(thumbnail.pixels[4 * (64 * 32 - 1) + 3] == 0);

    DynamicPrintConfig config;
    config.set_key_value("filament_colour", new ConfigOptionStrings({"#102030"}));
    CHECK(plate_thumbnail_colour(config) == std::array<unsigned char, 3>{0x10, 0x20, 0x30});
    config.set_key_value("filament_colour", new ConfigOptionStrings({"red"}));
    CHECK(plate_thumbnail_colour(config) == std::array<unsigned char, 3>{0x00, 0x96, 0x88});
}
