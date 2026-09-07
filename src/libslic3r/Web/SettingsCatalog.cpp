#include "SettingsCatalog.hpp"

#include "libslic3r/Preset.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r_version.h"

#include <algorithm>
#include <cfloat>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

// The curated MVP settings. Only the key, its group, and the setting that
// enables it are stated here; everything the browser renders comes from
// PrintConfig.cpp so the two can never disagree.
struct CuratedSetting
{
    const char *key;
    const char *group;
    // Non-empty when the engine only honors this setting while another one is
    // on, which the browser uses to disable the control rather than to hide it.
    const char *enabled_by {""};
};

constexpr CuratedSetting CURATED_SETTINGS[] = {
    {"layer_height", "quality"},
    {"initial_layer_print_height", "quality"},
    {"wall_loops", "quality"},
    {"top_shell_layers", "quality"},
    {"bottom_shell_layers", "quality"},
    {"seam_position", "quality"},
    {"sparse_infill_density", "infill"},
    {"sparse_infill_pattern", "infill"},
    {"enable_support", "support"},
    {"support_type", "support", "enable_support"},
    {"support_threshold_angle", "support", "enable_support"},
    {"brim_type", "adhesion"},
    {"brim_width", "adhesion"},
    {"skirt_loops", "adhesion"},
    {"outer_wall_speed", "speed"},
    {"inner_wall_speed", "speed"},
    {"sparse_infill_speed", "speed"},
    {"nozzle_temperature", "filament"},
    {"nozzle_temperature_initial_layer", "filament"},
    {"hot_plate_temp", "filament"},
    {"filament_flow_ratio", "filament"},
    {"spiral_mode", "advanced"},
    {"print_sequence", "advanced"},
};

struct CuratedGroup
{
    const char *id;
    const char *label;
};

constexpr CuratedGroup CURATED_GROUPS[] = {
    {"quality", "Quality"},   {"infill", "Infill"},     {"support", "Support"},
    {"adhesion", "Adhesion"}, {"speed", "Speed"},       {"filament", "Filament"},
    {"advanced", "Advanced"},
};

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

// A setting belongs to the profile whose preset carries it, which is what the
// API needs to tell a user which selected profile an override displaces.
const char *option_scope(const std::string &key)
{
    const auto contains = [&key](const std::vector<std::string> &options) {
        return std::find(options.begin(), options.end(), key) != options.end();
    };
    if (contains(Preset::print_options()))
        return "process";
    if (contains(Preset::filament_options()))
        return "filament";
    if (contains(Preset::printer_options()))
        return "machine";
    return "other";
}

nlohmann::json describe_setting(const CuratedSetting &curated, const ConfigOptionDef &definition)
{
    nlohmann::json described {
        {"key", curated.key},
        {"group", curated.group},
        {"scope", option_scope(curated.key)},
        {"type", option_type_name(definition.type)},
        {"vector", !definition.is_scalar()},
        {"nullable", definition.nullable},
        {"mode", option_mode_name(definition.mode)},
        {"label", definition.label},
        {"category", definition.category},
        {"tooltip", definition.tooltip},
        {"unit", definition.sidetext},
    };
    // FLT_MAX is the engine's "unbounded" sentinel; forwarding it would make
    // the browser render a meaningless range.
    if (definition.min > -FLT_MAX)
        described["min"] = definition.min;
    if (definition.max < FLT_MAX)
        described["max"] = definition.max;
    if (!definition.ratio_over.empty())
        described["ratio_over"] = definition.ratio_over;
    if (*curated.enabled_by != '\0')
        described["enabled_by"] = curated.enabled_by;
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

std::string serialize_settings_catalog()
{
    nlohmann::json catalog {
        {"catalog_version", SETTINGS_CATALOG_VERSION},
        {"engine_version", SLIC3R_VERSION},
        {"groups", nlohmann::json::array()},
        {"settings", nlohmann::json::array()},
    };
    for (const CuratedGroup &group : CURATED_GROUPS)
        catalog["groups"].push_back({{"id", group.id}, {"label", group.label}});
    for (const CuratedSetting &curated : CURATED_SETTINGS) {
        const ConfigOptionDef *definition = print_config_def.get(curated.key);
        // A curated key that the engine dropped is reported as a missing
        // definition rather than silently disappearing from the browser form.
        if (definition == nullptr)
            catalog["settings"].push_back({{"key", curated.key}, {"group", curated.group}, {"missing", true}});
        else
            catalog["settings"].push_back(describe_setting(curated, *definition));
    }
    return catalog.dump();
}

} // namespace Slic3r::Web
