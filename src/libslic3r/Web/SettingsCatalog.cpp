#include "SettingsCatalog.hpp"

#include "libslic3r/Preset.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r_version.h"

#include <algorithm>
#include <cctype>
#include <cfloat>
#include <cstring>
#include <set>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

// Settings the engine only honors while another one is on. The browser uses
// this to disable the control rather than to hide it. The desktop states these
// rules in GUI code, so the few the web form relies on are restated here.
struct SettingDependency
{
    const char *key;
    const char *enabled_by;
};

constexpr SettingDependency SETTING_DEPENDENCIES[] = {
    {"support_type", "enable_support"},
    {"support_threshold_angle", "enable_support"},
    {"support_style", "enable_support"},
    {"support_on_build_plate_only", "enable_support"},
};

// Profile bookkeeping and host-side hooks that are options in PrintConfigDef
// but not slicing settings: an override could only break profile resolution
// or name a program the worker never runs.
constexpr const char *EXCLUDED_SETTINGS[] = {
    "inherits",           "compatible_printers", "compatible_printers_condition", "compatible_prints",
    "compatible_prints_condition", "print_settings_id", "filament_settings_id", "filament_vendor",
    "filament_ids",       "post_process",        "filename_format",               "notes",
    "filament_notes",
};

// Value shapes the browser cannot edit as one serialized string.
bool is_editable_type(ConfigOptionType type)
{
    return type != coPoint && type != coPoints && type != coPoint3 && type != coPointsGroups && type != coIntsGroups &&
           type != coNone;
}

// The desktop's object list offers the object and region options of the
// engine for an object, and the region options plus the layer height for a
// height range; the same sets are used here.
const std::set<std::string> &object_keys()
{
    static const std::set<std::string> keys = [] {
        std::set<std::string> collected;
        for (const std::string &key : PrintObjectConfig().keys())
            collected.insert(key);
        for (const std::string &key : PrintRegionConfig().keys())
            collected.insert(key);
        return collected;
    }();
    return keys;
}

const std::set<std::string> &range_keys()
{
    static const std::set<std::string> keys = [] {
        std::set<std::string> collected {"layer_height"};
        for (const std::string &key : PrintRegionConfig().keys())
            collected.insert(key);
        return collected;
    }();
    return keys;
}

// A category is the engine's own grouping; an uncategorized option falls back
// to the profile kind that carries it.
std::string group_label(const ConfigOptionDef &definition, const char *scope)
{
    if (!definition.category.empty() && definition.category != "Other")
        return definition.category;
    return std::strcmp(scope, "filament") == 0 ? "Filament" : "Others";
}

std::string group_id(const std::string &label)
{
    std::string id;
    for (const char character : label)
        id += std::isalnum(static_cast<unsigned char>(character)) ? char(std::tolower(static_cast<unsigned char>(character))) : '_';
    return id;
}

const char *option_type_name(ConfigOptionType type)
{
    switch (type) {
    case coFloat:            return "float";
    case coFloats:           return "floats";
    case coInt:              return "int";
    case coInts:             return "ints";
    case coString:           return "string";
    case coStrings:          return "strings";
    case coPercent:          return "percent";
    case coPercents:         return "percents";
    case coFloatOrPercent:   return "float_or_percent";
    case coFloatsOrPercents: return "floats_or_percents";
    case coPoint:            return "point";
    case coPoints:           return "points";
    case coPoint3:           return "point3";
    case coBool:             return "bool";
    case coBools:            return "bools";
    case coEnum:             return "enum";
    case coEnums:            return "enums";
    case coPointsGroups:     return "points_groups";
    case coIntsGroups:       return "ints_groups";
    default:                 return "unknown";
    }
}

const char *option_mode_name(ConfigOptionMode mode)
{
    switch (mode) {
    case comSimple:   return "simple";
    case comAdvanced: return "advanced";
    case comExpert:   return "expert";
    default:          return "develop";
    }
}

nlohmann::json describe_setting(const std::string &key, const char *group, const char *scope,
                                const ConfigOptionDef &definition)
{
    nlohmann::json described {
        {"key", key},
        {"group", group},
        {"scope", scope},
        {"type", option_type_name(definition.type)},
        {"vector", !definition.is_scalar()},
        {"nullable", definition.nullable},
        {"mode", option_mode_name(definition.mode)},
        // A full label names the setting on its own, which is how search
        // results and per-object lists show it.
        {"label", definition.full_label.empty() ? definition.label : definition.full_label},
        {"category", definition.category},
        {"tooltip", definition.tooltip},
        {"unit", definition.sidetext},
        {"per_object", is_object_setting(key)},
        {"per_range", is_layer_range_setting(key)},
    };
    if (definition.multiline)
        described["multiline"] = true;
    // FLT_MAX is the engine's "unbounded" sentinel; forwarding it would make
    // the browser render a meaningless range.
    if (definition.min > -FLT_MAX)
        described["min"] = definition.min;
    if (definition.max < FLT_MAX)
        described["max"] = definition.max;
    if (!definition.ratio_over.empty())
        described["ratio_over"] = definition.ratio_over;
    for (const SettingDependency &dependency : SETTING_DEPENDENCIES)
        if (key == dependency.key)
            described["enabled_by"] = dependency.enabled_by;
    if (!definition.enum_values.empty()) {
        described["enum"] = nlohmann::json::array();
        for (std::size_t index = 0; index < definition.enum_values.size(); ++index)
            described["enum"].push_back({
                {"value", definition.enum_values[index]},
                {"label", index < definition.enum_labels.size() ? definition.enum_labels[index]
                                                                : definition.enum_values[index]}
            });
    }
    // The default is serialized in the same string form the slice manifest
    // carries, so a browser can round-trip it without a second encoding.
    if (definition.default_value)
        described["default"] = definition.default_value->serialize();
    return described;
}

} // namespace

bool is_object_setting(const std::string &key) { return object_keys().count(key) > 0; }

bool is_layer_range_setting(const std::string &key) { return range_keys().count(key) > 0; }

std::string serialize_settings_catalog()
{
    nlohmann::json catalog {
        {"catalog_version", SETTINGS_CATALOG_VERSION},
        {"engine_version", SLIC3R_VERSION},
        {"groups", nlohmann::json::array()},
        {"settings", nlohmann::json::array()},
    };
    // Every process and filament option a profile can carry, in the order the
    // engine lists them. Machine options describe the printer the plater
    // draws and are changed by choosing another printer profile instead.
    std::set<std::string> seen;
    std::vector<std::string> groups;
    const auto add_options = [&](const std::vector<std::string> &keys, const char *scope) {
        for (const std::string &key : keys) {
            const ConfigOptionDef *definition = print_config_def.get(key);
            if (definition == nullptr || definition->mode == comDevelop || definition->readonly ||
                !is_editable_type(definition->type) || !seen.insert(key).second ||
                std::any_of(std::begin(EXCLUDED_SETTINGS), std::end(EXCLUDED_SETTINGS),
                            [&key](const char *excluded) { return key == excluded; }))
                continue;
            const std::string label = group_label(*definition, scope);
            const std::string id    = group_id(label);
            if (std::find(groups.begin(), groups.end(), label) == groups.end()) {
                groups.push_back(label);
                catalog["groups"].push_back({{"id", id}, {"label", label}});
            }
            catalog["settings"].push_back(describe_setting(key, id.c_str(), scope, *definition));
        }
    };
    add_options(Preset::print_options(), "process");
    add_options(Preset::filament_options(), "filament");
    return catalog.dump();
}

} // namespace Slic3r::Web
