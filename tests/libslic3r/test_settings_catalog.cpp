#include <catch2/catch_all.hpp>

#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Web/SettingsCatalog.hpp"

#include <algorithm>
#include <cfloat>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

using namespace Slic3r;
using namespace Slic3r::Web;

namespace {

nlohmann::json setting_named(const nlohmann::json &catalog, const std::string &key)
{
    for (const nlohmann::json &setting : catalog.at("settings"))
        if (setting.at("key") == key)
            return setting;
    return nlohmann::json::object();
}

} // namespace

TEST_CASE("The settings catalog declares a version and groups every setting", "[SettingsCatalog]")
{
    const nlohmann::json catalog = nlohmann::json::parse(serialize_settings_catalog());

    REQUIRE(catalog.at("catalog_version") == SETTINGS_CATALOG_VERSION);
    REQUIRE_FALSE(catalog.at("settings").empty());

    std::vector<std::string> group_ids;
    for (const nlohmann::json &group : catalog.at("groups"))
        group_ids.push_back(group.at("id"));

    for (const nlohmann::json &setting : catalog.at("settings")) {
        const std::string key = setting.at("key");
        DYNAMIC_SECTION("setting " << key)
        {
            // A curated key that no longer exists in the engine must say so
            // rather than quietly vanishing from the browser form.
            REQUIRE_FALSE(setting.value("missing", false));
            REQUIRE(std::find(group_ids.begin(), group_ids.end(), setting.at("group")) != group_ids.end());
            REQUIRE(print_config_def.has(key));
        }
    }
}

TEST_CASE("A catalog setting repeats the engine definition it came from", "[SettingsCatalog]")
{
    const nlohmann::json catalog = nlohmann::json::parse(serialize_settings_catalog());
    const nlohmann::json layer_height = setting_named(catalog, "layer_height");
    const ConfigOptionDef *definition = print_config_def.get("layer_height");
    REQUIRE(definition != nullptr);

    CHECK(layer_height.at("type") == "float");
    CHECK(layer_height.at("scope") == "process");
    CHECK(layer_height.at("vector") == false);
    CHECK(layer_height.at("label") == definition->label);
    CHECK(layer_height.at("unit") == definition->sidetext);
    CHECK(layer_height.at("default") == definition->default_value->serialize());
    // The engine bounds this one, so the browser is given the same bound.
    REQUIRE(layer_height.contains("min"));
    CHECK_THAT(layer_height.at("min").get<double>(),
               Catch::Matchers::WithinAbs(static_cast<double>(definition->min), 1e-6));
}

TEST_CASE("An enum setting carries its values, labels, and gate", "[SettingsCatalog]")
{
    const nlohmann::json catalog = nlohmann::json::parse(serialize_settings_catalog());
    const nlohmann::json support_type = setting_named(catalog, "support_type");
    const ConfigOptionDef *definition = print_config_def.get("support_type");
    REQUIRE(definition != nullptr);
    REQUIRE_FALSE(definition->enum_values.empty());

    CHECK(support_type.at("type") == "enum");
    CHECK(support_type.at("enabled_by") == "enable_support");
    REQUIRE(support_type.at("enum").size() == definition->enum_values.size());
    for (std::size_t index = 0; index < definition->enum_values.size(); ++index) {
        DYNAMIC_SECTION("value " << definition->enum_values[index])
        {
            CHECK(support_type.at("enum")[index].at("value") == definition->enum_values[index]);
            CHECK_FALSE(support_type.at("enum")[index].at("label").get<std::string>().empty());
        }
    }
}

TEST_CASE("A filament setting is reported as a filament-scoped vector", "[SettingsCatalog]")
{
    const nlohmann::json catalog = nlohmann::json::parse(serialize_settings_catalog());
    const nlohmann::json temperature = setting_named(catalog, "nozzle_temperature");

    CHECK(temperature.at("scope") == "filament");
    // Per-extruder settings are vectors, and the browser must know that before
    // it decides how to edit one.
    CHECK(temperature.at("vector") == true);
}

TEST_CASE("An unbounded setting reports no range", "[SettingsCatalog]")
{
    const nlohmann::json catalog = nlohmann::json::parse(serialize_settings_catalog());
    for (const nlohmann::json &setting : catalog.at("settings")) {
        const ConfigOptionDef *definition = print_config_def.get(setting.at("key").get<std::string>());
        REQUIRE(definition != nullptr);
        DYNAMIC_SECTION("setting " << setting.at("key").get<std::string>())
        {
            // FLT_MAX is the engine's "no limit" sentinel and must never reach
            // the browser as a number.
            CHECK(setting.contains("max") == (definition->max < FLT_MAX));
            CHECK(setting.contains("min") == (definition->min > -FLT_MAX));
        }
    }
}
