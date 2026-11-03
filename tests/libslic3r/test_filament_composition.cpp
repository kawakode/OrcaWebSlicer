#include <catch2/catch_all.hpp>

#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Web/FilamentComposition.hpp"

using namespace Slic3r;
using namespace Slic3r::Web;

namespace {

DynamicPrintConfig filament(const std::string &colour, double temperature, const std::string &type)
{
    DynamicPrintConfig config;
    config.set_key_value("filament_colour", new ConfigOptionStrings {colour});
    config.set_key_value("nozzle_temperature", new ConfigOptionInts {int(temperature)});
    config.set_key_value("filament_type", new ConfigOptionStrings {type});
    return config;
}

} // namespace

TEST_CASE("Composed filaments give every filament option one value per filament", "[FilamentComposition]")
{
    DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
    config.set_key_value("filament_diameter", new ConfigOptionFloats {2.85});
    DynamicPrintConfig second = filament("#0000FF", 240, "PETG");
    second.erase("filament_type");

    compose_filaments(config, {filament("#FF0000", 210, "PLA"), second, filament("#00FF00", 220, "PLA")});

    CHECK(config.option<ConfigOptionStrings>("filament_colour")->values ==
          std::vector<std::string> {"#FF0000", "#0000FF", "#00FF00"});
    CHECK(config.option<ConfigOptionInts>("nozzle_temperature")->values == std::vector<int> {210, 240, 220});
    // A key a filament leaves out keeps the value the configuration had.
    CHECK(config.option<ConfigOptionStrings>("filament_type")->values ==
          std::vector<std::string> {"PLA", config.option<ConfigOptionStrings>("filament_type")->values[1], "PLA"});
    CHECK(config.option<ConfigOptionFloats>("filament_diameter")->values == std::vector<double> {2.85, 2.85, 2.85});
    CHECK(config.option<ConfigOptionInts>("filament_map")->values.size() == 3);
}

TEST_CASE("Composed filaments clamp filament roles to the filaments loaded", "[FilamentComposition]")
{
    DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
    config.set_key_value("wipe_tower_filament", new ConfigOptionInt(5));
    config.set_key_value("outer_wall_filament_id", new ConfigOptionInt(4));

    compose_filaments(config, {filament("#FF0000", 210, "PLA"), filament("#0000FF", 220, "PLA")});

    CHECK(config.opt_int("wipe_tower_filament") == 2);
    CHECK(config.opt_int("outer_wall_filament_id") == 0);
}

TEST_CASE("Flush volumes are computed from the filament colours", "[FilamentComposition]")
{
    DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
    compose_filaments(config, {filament("#FFFFFF", 210, "PLA"), filament("#000000", 210, "PLA")});
    config.set_key_value("flush_volumes_matrix", new ConfigOptionFloats {0., 1., 2.});

    fill_flush_volumes(config);

    const std::vector<double> &matrix = config.option<ConfigOptionFloats>("flush_volumes_matrix")->values;
    REQUIRE(matrix.size() == 4);
    CHECK(matrix[0] == 0.);
    CHECK(matrix[3] == 0.);
    // Covering black with white (row 1, column 0) takes more purging than the reverse.
    CHECK(matrix[2] > 0.);
    CHECK(matrix[2] > matrix[1]);
    CHECK(config.option<ConfigOptionFloats>("flush_multiplier")->values.size() == 1);
}

TEST_CASE("A flush matrix that already fits the filaments is kept", "[FilamentComposition]")
{
    DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
    compose_filaments(config, {filament("#FFFFFF", 210, "PLA"), filament("#000000", 210, "PLA")});
    config.set_key_value("flush_volumes_matrix", new ConfigOptionFloats {0., 111., 222., 0.});

    fill_flush_volumes(config);

    CHECK(config.option<ConfigOptionFloats>("flush_volumes_matrix")->values == std::vector<double> {0., 111., 222., 0.});
}

TEST_CASE("A single filament leaves the flush matrix alone", "[FilamentComposition]")
{
    DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
    const std::vector<double> before = config.option<ConfigOptionFloats>("flush_volumes_matrix")->values;

    fill_flush_volumes(config);

    CHECK(config.option<ConfigOptionFloats>("flush_volumes_matrix")->values == before);
}
