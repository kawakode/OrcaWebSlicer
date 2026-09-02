#include <catch2/catch_all.hpp>

#include "libslic3r/MinimumFlushVolume.hpp"

#include <limits>

using namespace Slic3r;

namespace {

DynamicPrintConfig make_flush_config()
{
    DynamicPrintConfig config;
    config.set_key_value("nozzle_volume", new ConfigOptionFloatsNullable({100.0}));
    config.set_key_value("filament_diameter", new ConfigOptionFloats({1.75, 1.75}));
    config.set_key_value("enable_long_retraction_when_cut", new ConfigOptionInt(LongRectrationLevel::Disabled));
    config.set_key_value("long_retractions_when_cut", new ConfigOptionBools({false}));
    config.set_key_value("retraction_distances_when_cut", new ConfigOptionFloats({10.0}));
    config.set_key_value("filament_long_retractions_when_cut", new ConfigOptionBools({false, false}));
    config.set_key_value("filament_retraction_distances_when_cut", new ConfigOptionFloats({4.0, 4.0}));
    return config;
}

} // namespace

TEST_CASE("Minimum flush volumes omit retraction when it is disabled", "[FlushVolumes]")
{
    const DynamicPrintConfig config = make_flush_config();
    REQUIRE(get_min_flush_volumes(config, 0) == std::vector<int>{100, 100});
}

TEST_CASE("Minimum flush volumes use machine and filament retraction settings", "[FlushVolumes]")
{
    DynamicPrintConfig config = make_flush_config();
    config.option<ConfigOptionInt>("enable_long_retraction_when_cut")->value = LongRectrationLevel::EnableMachine;
    config.option<ConfigOptionBools>("long_retractions_when_cut")->values = {true};
    config.option<ConfigOptionBools>("filament_long_retractions_when_cut")->values = {true, false};
    REQUIRE(get_min_flush_volumes(config, 0) == std::vector<int>{75, 100});

    config.option<ConfigOptionInt>("enable_long_retraction_when_cut")->value = LongRectrationLevel::EnableFilament;
    config.option<ConfigOptionBools>("filament_long_retractions_when_cut")->values = {true, true};
    config.option<ConfigOptionFloats>("filament_retraction_distances_when_cut")->values = {
        4.0, std::numeric_limits<double>::quiet_NaN()};
    REQUIRE(get_min_flush_volumes(config, 0) == std::vector<int>{90, 75});
}

TEST_CASE("Minimum flush volumes handle incomplete worker configuration", "[FlushVolumes]")
{
    DynamicPrintConfig config;
    REQUIRE(get_min_flush_volumes(config, 0).empty());

    config.set_key_value("filament_diameter", new ConfigOptionFloats({1.75, 1.75}));
    config.set_key_value("long_retractions_when_cut", new ConfigOptionBools({true}));
    config.set_key_value("filament_long_retractions_when_cut", new ConfigOptionBools({true}));
    REQUIRE(get_min_flush_volumes(config, 3) == std::vector<int>{0, 0});
}
