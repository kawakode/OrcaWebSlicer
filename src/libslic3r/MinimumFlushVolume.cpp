#include "MinimumFlushVolume.hpp"

#include <cmath>

namespace Slic3r {

std::vector<int> get_min_flush_volumes(const DynamicPrintConfig &full_config, size_t nozzle_id)
{
    const auto *filament_diameter = full_config.option<ConfigOptionFloats>("filament_diameter");
    if (filament_diameter == nullptr)
        return {};

    const size_t filament_count = filament_diameter->values.size();
    std::vector<int> min_flush_volumes;
    min_flush_volumes.reserve(filament_count);

    const auto *nozzle_volume = full_config.option<ConfigOptionFloatsNullable>("nozzle_volume");
    const int nozzle_volume_value = nozzle_volume != nullptr && !nozzle_volume->values.empty()
        ? static_cast<int>(nozzle_volume->get_at(nozzle_id))
        : 0;

    const auto *enable_long_retraction = full_config.option<ConfigOptionInt>("enable_long_retraction_when_cut");
    const int machine_enabled_level = enable_long_retraction != nullptr ? enable_long_retraction->value : LongRectrationLevel::Disabled;

    const auto *long_retractions = full_config.option<ConfigOptionBools>("long_retractions_when_cut");
    const bool machine_activated = long_retractions != nullptr && !long_retractions->values.empty()
        ? long_retractions->get_at(nozzle_id)
        : false;

    const auto *filament_retraction_distances = full_config.option<ConfigOptionFloats>("filament_retraction_distances_when_cut");
    const auto *printer_retraction_distances = full_config.option<ConfigOptionFloats>("retraction_distances_when_cut");
    const auto *filament_long_retractions = full_config.option<ConfigOptionBools>("filament_long_retractions_when_cut");

    const double printer_retraction_distance = printer_retraction_distances != nullptr && !printer_retraction_distances->values.empty()
        ? printer_retraction_distances->get_at(nozzle_id)
        : 18.0;

    for (size_t filament_id = 0; filament_id < filament_count; ++filament_id) {
        int retract_length = machine_enabled_level != LongRectrationLevel::Disabled && machine_activated
            ? static_cast<int>(printer_retraction_distance)
            : 0;

        const bool filament_activated = filament_long_retractions != nullptr && !filament_long_retractions->values.empty()
            ? filament_long_retractions->get_at(filament_id)
            : false;
        const double filament_retraction_distance = filament_retraction_distances != nullptr && !filament_retraction_distances->values.empty()
            ? filament_retraction_distances->get_at(filament_id)
            : 18.0;

        if (!filament_activated) {
            retract_length = 0;
        } else if (machine_enabled_level == LongRectrationLevel::EnableFilament) {
            retract_length = static_cast<int>(std::isnan(filament_retraction_distance)
                ? printer_retraction_distance
                : filament_retraction_distance);
        }

        int min_flush_volume = nozzle_volume_value;
        min_flush_volume -= PI * 1.75 * 1.75 / 4 * retract_length;
        min_flush_volumes.emplace_back(min_flush_volume);
    }

    return min_flush_volumes;
}

} // namespace Slic3r
