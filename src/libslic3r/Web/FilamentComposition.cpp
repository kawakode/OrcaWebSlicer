#include "FilamentComposition.hpp"

#include "libslic3r/Color.hpp"
#include "libslic3r/FlushVolCalc.hpp"
#include "libslic3r/MinimumFlushVolume.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/PrintConfig.hpp"

#include <algorithm>
#include <array>
#include <memory>
#include <string>

namespace Slic3r::Web {
namespace {

const ConfigOption *first_usable(const ConfigOption *option)
{
    if (option == nullptr)
        return nullptr;
    if (const auto *vector = dynamic_cast<const ConfigOptionVectorBase *>(option); vector != nullptr && vector->empty())
        return nullptr;
    return option;
}

} // namespace

void compose_filaments(DynamicPrintConfig &config, const std::vector<DynamicPrintConfig> &filaments)
{
    if (filaments.empty())
        return;
    for (const std::string &key : Preset::filament_options()) {
        // These clash with the print profile's keys of the same name, which
        // the desktop resolves by never merging them.
        if (key == "compatible_prints" || key == "compatible_printers" || key == "compatible_prints_condition" ||
            key == "compatible_printers_condition" || key == "inherits")
            continue;
        ConfigOption *target = config.option(key, true);
        if (target == nullptr)
            continue;
        if (!target->is_vector()) {
            if (const ConfigOption *source = filaments.front().option(key))
                target->set(source);
            continue;
        }
        const std::unique_ptr<ConfigOption> kept(target->clone());
        const ConfigOption *fallback = first_usable(kept.get());
        std::vector<const ConfigOption *> sources;
        for (const DynamicPrintConfig &filament : filaments) {
            const ConfigOption *source = first_usable(filament.option(key));
            sources.push_back(source != nullptr ? source : fallback);
        }
        if (std::find(sources.begin(), sources.end(), nullptr) == sources.end())
            static_cast<ConfigOptionVectorBase *>(target)->set(sources);
    }

    const int count = static_cast<int>(filaments.size());
    // Every filament feeds the first extruder unless a project said otherwise.
    config.option<ConfigOptionInts>("filament_map", true)->values.resize(filaments.size(), 1);
    for (const char *key : {"support_filament", "support_interface_filament", "wipe_tower_filament"})
        if (auto *option = config.option<ConfigOptionInt>(key))
            option->value = std::clamp(option->value, 0, count);
    for (const char *key : {"outer_wall_filament_id", "inner_wall_filament_id", "sparse_infill_filament_id",
                            "internal_solid_filament_id", "top_surface_filament_id", "bottom_surface_filament_id"})
        if (auto *option = config.option<ConfigOptionInt>(key); option != nullptr && (option->value < 0 || option->value > count))
            option->value = 0;
}

void fill_flush_volumes(DynamicPrintConfig &config)
{
    const auto *colours = config.option<ConfigOptionStrings>("filament_colour");
    const auto *nozzles = config.option<ConfigOptionFloats>("nozzle_diameter");
    if (colours == nullptr || nozzles == nullptr || colours->size() < 2)
        return;
    const std::size_t filaments = colours->size();
    const std::size_t heads     = std::max<std::size_t>(1, nozzles->size());

    auto &multipliers = config.option<ConfigOptionFloats>("flush_multiplier", true)->values;
    if (multipliers.empty())
        multipliers.push_back(1.);
    multipliers.resize(heads, multipliers.front());

    std::vector<double> &matrix = config.option<ConfigOptionFloats>("flush_volumes_matrix", true)->values;
    if (matrix.size() == filaments * filaments * heads)
        return;

    std::vector<std::array<unsigned char, 4>> decoded(filaments, {0, 0, 0, 255});
    for (std::size_t index = 0; index < filaments; ++index)
        if (!decode_color(colours->values[index], decoded[index]))
            decoded[index] = {0, 0, 0, 255};
    const auto *supports = config.option<ConfigOptionBools>("filament_is_support");
    const auto  is_support = [supports](std::size_t index) {
        return supports != nullptr && !supports->values.empty() && supports->get_at(index);
    };
    const auto *datasets = config.option<ConfigOptionIntsNullable>("nozzle_flush_dataset");

    std::vector<double> computed;
    computed.reserve(filaments * filaments * heads);
    for (std::size_t head = 0; head < heads; ++head) {
        const std::vector<int> minimum = get_min_flush_volumes(config, head);
        const int dataset = datasets != nullptr && !datasets->values.empty() && !datasets->is_nil(std::min(head, datasets->size() - 1))
                                ? datasets->get_at(head)
                                : 0;
        for (std::size_t from = 0; from < filaments; ++from)
            for (std::size_t to = 0; to < filaments; ++to) {
                if (from == to) {
                    computed.push_back(0.);
                    continue;
                }
                if (is_support(to)) {
                    computed.push_back(g_flush_volume_to_support);
                    continue;
                }
                FlushVolCalculator calculator(from < minimum.size() ? minimum[from] : 0, g_max_flush_volume, dataset);
                const auto &a = decoded[from];
                const auto &b = decoded[to];
                int volume = calculator.calc_flush_vol(a[3], a[0], a[1], a[2], b[3], b[0], b[1], b[2]);
                if (is_support(from))
                    volume = std::max(volume, g_min_flush_volume_from_support);
                computed.push_back(volume);
            }
    }
    matrix = std::move(computed);
}

} // namespace Slic3r::Web
