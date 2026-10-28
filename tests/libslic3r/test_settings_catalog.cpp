#include <catch2/catch_all.hpp>

#include "libslic3r/Web/ProfileCompatibility.hpp"
#include "libslic3r/Web/SettingsCatalog.hpp"

#include <boost/filesystem.hpp>
#include <filesystem>
#include <fstream>
#include <nlohmann/json.hpp>
#include <set>
#include <string>

using namespace Slic3r::Web;

namespace {

const nlohmann::json *find_setting(const nlohmann::json &catalog, const std::string &key)
{
    for (const nlohmann::json &setting : catalog["settings"])
        if (setting["key"] == key)
            return &setting;
    return nullptr;
}

struct TemporaryRoot
{
    std::filesystem::path path;

    TemporaryRoot()
        : path(std::filesystem::temp_directory_path() /
               ("orca-compatibility-" + boost::filesystem::unique_path().string()))
    {
        std::filesystem::create_directories(path / "printers");
    }
    ~TemporaryRoot() { std::filesystem::remove_all(path); }

    void write(const std::string &relative, const nlohmann::json &contents) const
    {
        std::ofstream(path / relative) << contents.dump();
    }
};

} // namespace

TEST_CASE("Settings catalog serializes the engine definition of every profile setting", "[SettingsCatalog]")
{
    const nlohmann::json catalog = nlohmann::json::parse(serialize_settings_catalog());

    REQUIRE(catalog["catalog_version"] == SETTINGS_CATALOG_VERSION);
    REQUIRE_FALSE(catalog["groups"].empty());
    // The whole process and filament catalog, not a curated subset.
    CHECK(catalog["settings"].size() > 300);

    std::set<std::string> groups;
    for (const nlohmann::json &group : catalog["groups"])
        CHECK(groups.insert(group["id"].get<std::string>()).second);
    std::set<std::string> modes;
    for (const nlohmann::json &setting : catalog["settings"]) {
        INFO(setting["key"]);
        CHECK(groups.count(setting["group"].get<std::string>()) == 1);
        CHECK(setting["mode"] != "develop");
        modes.insert(setting["mode"].get<std::string>());
    }
    CHECK(modes == std::set<std::string>{"simple", "advanced", "expert"});

    const nlohmann::json *layer_height = find_setting(catalog, "layer_height");
    REQUIRE(layer_height != nullptr);
    CHECK((*layer_height)["type"] == "float");
    CHECK((*layer_height)["scope"] == "process");
    CHECK((*layer_height)["group"] == "quality");
    CHECK((*layer_height)["unit"] == "mm");
    CHECK((*layer_height)["default"] == "0.2");
    CHECK((*layer_height)["per_object"] == true);
    CHECK((*layer_height)["per_range"] == true);

    const nlohmann::json *support_type = find_setting(catalog, "support_type");
    REQUIRE(support_type != nullptr);
    CHECK((*support_type)["type"] == "enum");
    CHECK((*support_type)["enabled_by"] == "enable_support");
    CHECK_FALSE((*support_type)["enum"].empty());
    // Supports are generated per object, never per height range.
    CHECK((*support_type)["per_object"] == true);
    CHECK((*support_type)["per_range"] == false);

    const nlohmann::json *skirt = find_setting(catalog, "skirt_loops");
    REQUIRE(skirt != nullptr);
    CHECK((*skirt)["per_object"] == false);

    const nlohmann::json *temperature = find_setting(catalog, "nozzle_temperature");
    REQUIRE(temperature != nullptr);
    CHECK((*temperature)["scope"] == "filament");
    CHECK((*temperature)["vector"] == true);

    const nlohmann::json *start_gcode = find_setting(catalog, "filament_start_gcode");
    REQUIRE(start_gcode != nullptr);
    CHECK((*start_gcode)["multiline"] == true);

    // Profile bookkeeping is not a setting a request may override.
    for (const char *key : {"inherits", "compatible_printers_condition", "print_settings_id", "post_process"})
        CHECK(find_setting(catalog, key) == nullptr);
}

TEST_CASE("Per-object and height range settings follow the engine's config classes", "[SettingsCatalog]")
{
    CHECK(is_object_setting("wall_loops"));
    CHECK(is_object_setting("enable_support"));
    CHECK_FALSE(is_object_setting("skirt_loops"));
    CHECK_FALSE(is_object_setting("nozzle_temperature"));
    CHECK(is_layer_range_setting("layer_height"));
    CHECK(is_layer_range_setting("sparse_infill_density"));
    CHECK_FALSE(is_layer_range_setting("enable_support"));
}

TEST_CASE("Profile compatibility evaluates conditions with the engine parser", "[SettingsCatalog]")
{
    TemporaryRoot root;
    root.write("printers/kobra.json", {{"printer_model", "Kobra"}, {"nozzle_diameter", {"0.4"}}});
    root.write("printers/large.json", {{"printer_model", "Large"}, {"nozzle_diameter", {"0.6"}}});

    const nlohmann::json request {
        {"catalog_version", COMPATIBILITY_REQUEST_VERSION},
        {"printers",
         {{{"id", "v/machine/kobra"}, {"name", "Kobra 0.4"}, {"profile", "printers/kobra.json"}},
          {{"id", "v/machine/large"}, {"name", "Large 0.6"}, {"profile", "printers/large.json"}}}},
        {"candidates",
         {{{"id", "fine"}, {"condition", "nozzle_diameter[0]==0.4"}},
          {{"id", "named"}, {"condition", "printer_preset==\"Large 0.6\""}},
          {{"id", "broken"}, {"condition", "nozzle_diameter[0] ==="}}}},
    };

    std::string         response;
    WorkerManifestError error;
    REQUIRE(evaluate_profile_compatibility(request.dump(), root.path, response, error));
    const nlohmann::json answer = nlohmann::json::parse(response);
    CHECK(answer["compatibility"]["fine"] == nlohmann::json::array({"v/machine/kobra"}));
    CHECK(answer["compatibility"]["named"] == nlohmann::json::array({"v/machine/large"}));
    // An unreadable condition stays compatible, as on the desktop, and is named.
    CHECK(answer["compatibility"]["broken"].size() == 2);
    CHECK(answer["unevaluated"].size() == 2);
}

TEST_CASE("Profile compatibility refuses printers outside its root", "[SettingsCatalog]")
{
    TemporaryRoot root;
    const nlohmann::json request {
        {"catalog_version", COMPATIBILITY_REQUEST_VERSION},
        {"printers", {{{"id", "escape"}, {"name", "Escape"}, {"profile", "../outside.json"}}}},
        {"candidates", nlohmann::json::array()},
    };

    std::string         response;
    WorkerManifestError error;
    REQUIRE_FALSE(evaluate_profile_compatibility(request.dump(), root.path, response, error));
    CHECK_FALSE(error.code.empty());

    REQUIRE_FALSE(evaluate_profile_compatibility(R"({"catalog_version": 99})", root.path, response, error));
    CHECK(error.code == "unsupported_catalog_version");
}
