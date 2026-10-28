#include "ProjectExport.hpp"

#include "ArtifactTransaction.hpp"
#include "PlateThumbnail.hpp"
#include "ProjectArchive.hpp"

#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/Preset.hpp"

#include <algorithm>
#include <fstream>
#include <memory>
#include <set>
#include <system_error>

namespace Slic3r::Web {
namespace {

SinglePlateSliceResult failure(std::string code, std::string message,
                               WorkerErrorCategory category = WorkerErrorCategory::Internal)
{
    return {false, std::move(code), std::move(message), category};
}

// The preset a job-local profile was flattened from, as its `name` records it.
// The worker reads only job-local files, so an unreadable profile simply leaves
// the name blank; slicing already validated the same file.
nlohmann::json read_profile_metadata(const std::filesystem::path &job_root, const std::string &relative_path)
{
    std::filesystem::path resolved;
    WorkerManifestError   error;
    if (relative_path.empty() || !resolve_job_file(job_root, relative_path, WorkerErrorCategory::Profile, resolved, error))
        return nlohmann::json::object();
    std::ifstream        input(resolved, std::ios::binary);
    const nlohmann::json profile = nlohmann::json::parse(input, nullptr, false);
    return profile.is_object() ? profile : nlohmann::json::object();
}

std::string string_field(const nlohmann::json &profile, const char *key)
{
    const auto value = profile.find(key);
    return value != profile.end() && value->is_string() ? value->get<std::string>() : std::string();
}

// Records the selection the way PresetBundle::full_config() does, so the desktop
// reads it back: one `different_settings_to_system` entry for the process,
// one per filament, then one for the printer.
void record_selection(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                      DynamicPrintConfig &config)
{
    const nlohmann::json machine  = read_profile_metadata(job_root, request.machine_profile);
    const nlohmann::json process  = read_profile_metadata(job_root, request.process_profile);
    const nlohmann::json filament = read_profile_metadata(job_root, request.filament_profile);
    const std::size_t filaments = std::max<std::size_t>(1, config.option<ConfigOptionFloats>("filament_diameter", true)->size());

    if (!machine.empty())
        config.option<ConfigOptionString>("printer_settings_id", true)->value = string_field(machine, "name");
    if (!process.empty())
        config.option<ConfigOptionString>("print_settings_id", true)->value = string_field(process, "name");
    if (!filament.empty()) {
        config.option<ConfigOptionStrings>("filament_settings_id", true)->values.assign(filaments, string_field(filament, "name"));
        config.option<ConfigOptionStrings>("filament_ids", true)->values.assign(filaments, string_field(filament, "filament_id"));
    }

    std::vector<std::vector<std::string>> overridden(filaments + 2);
    const auto contains = [](const std::vector<std::string> &options, const std::string &key) {
        return std::find(options.begin(), options.end(), key) != options.end();
    };
    for (const auto &[key, value] : request.settings) {
        if (contains(Preset::print_options(), key))
            overridden.front().push_back(key);
        else if (contains(Preset::filament_options(), key))
            for (std::size_t index = 1; index <= filaments; ++index)
                overridden[index].push_back(key);
        else if (contains(Preset::printer_options(), key))
            overridden.back().push_back(key);
    }
    std::vector<std::string> different;
    for (std::vector<std::string> &keys : overridden) {
        std::sort(keys.begin(), keys.end());
        different.push_back(keys.empty() ? std::string() : escape_strings_cstyle(keys));
    }
    if (std::any_of(different.begin(), different.end(), [](const std::string &keys) { return !keys.empty(); }))
        config.set_key_value("different_settings_to_system", new ConfigOptionStrings(different));
    else
        config.erase("different_settings_to_system");

    // A flattened profile still carries the keys the desktop strips from a
    // project, because their print and filament forms clash.
    for (const char *key : {"inherits", "compatible_printers", "compatible_printers_condition", "compatible_prints",
                            "compatible_prints_condition"})
        config.erase(key);
}

// Engine defaults build enum lists without their names, which serialize as
// empty tokens that no importer can read back. Every list is given the names
// its definition declares before the configuration is written.
template<typename Enums> void name_enum_list(DynamicPrintConfig &config, const std::string &key)
{
    if (auto *values = dynamic_cast<Enums *>(config.option(key)); values != nullptr && values->keys_map == nullptr)
        if (const ConfigOptionDef *definition = config.def()->get(key))
            values->keys_map = definition->enum_keys_map;
}

void name_enum_lists(DynamicPrintConfig &config)
{
    for (const std::string &key : config.keys()) {
        name_enum_list<ConfigOptionEnumsGeneric>(config, key);
        name_enum_list<ConfigOptionEnumsGenericNullable>(config, key);
    }
}

} // namespace

SinglePlateSliceResult export_plate_project(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                            Model &model, DynamicPrintConfig &config,
                                            const SinglePlateSliceCallbacks &callbacks)
{
    if (callbacks.progress)
        callbacks.progress({"export", 40, "Writing project"});
    record_selection(request, job_root, config);
    name_enum_lists(config);

    WorkerManifestError path_error;
    std::optional<ArtifactTransaction> artifact =
        ArtifactTransaction::begin(job_root, request.output_project, request.envelope.job_id, path_error);
    if (!artifact)
        return failure(path_error.code, path_error.message, path_error.category);

    const ModelScratchDirectory scratch(model, job_root / ("." + request.envelope.job_id + ".project"));
    if (!scratch.ready())
        return failure("project_export_failed", "Unable to stage the project archive.");
    std::error_code filesystem_error;

    std::set<std::pair<int, int>> placed;
    for (std::size_t object = 0; object < model.objects.size(); ++object)
        for (std::size_t instance = 0; instance < model.objects[object]->instances.size(); ++instance)
            placed.emplace(int(object), int(instance));
    // The plate is the store's to read, never to own.
    const std::unique_ptr<PlateData> plate = std::make_unique<PlateData>(0, placed, false);
    plate->printer_model_id = config.opt_string("printer_model");
    plate->nozzle_diameters = config.option<ConfigOptionFloats>("nozzle_diameter", true)->serialize();

    if (callbacks.cancellation_requested && callbacks.cancellation_requested())
        return failure("job_canceled", "The project export was canceled.", WorkerErrorCategory::Cancellation);
    if (callbacks.progress)
        callbacks.progress({"export", 60, "Rendering thumbnail"});
    ThumbnailData thumbnail = render_plate_thumbnail(model, PROJECT_THUMBNAIL_SIZE, PROJECT_THUMBNAIL_SIZE,
                                                     plate_thumbnail_colour(config));

    if (callbacks.progress)
        callbacks.progress({"export", 80, "Writing project"});
    StoreParams store;
    store.path            = artifact->temporary_path().string();
    store.model           = &model;
    store.config          = &config;
    store.plate_data_list = {plate.get()};
    store.thumbnail_data  = {&thumbnail};
    // The same layout the desktop saves a project with. Auxiliary files are
    // skipped because the worker never loads them.
    store.strategy = SaveStrategy::Silence | SaveStrategy::SplitModel | SaveStrategy::ShareMesh |
                     SaveStrategy::SkipAuxiliary | SaveStrategy::Zip64;
    if (!store_bbs_3mf(store)) {
        std::filesystem::remove(store.path + ".tmp", filesystem_error);
        return failure("project_export_failed", "The project archive could not be written.");
    }

    const std::uintmax_t size = std::filesystem::file_size(artifact->temporary_path(), filesystem_error);
    if (filesystem_error || size == 0)
        return failure("project_export_failed", "The project export did not produce a non-empty archive.");
    if (request.max_output_bytes && size > *request.max_output_bytes)
        return failure("output_size_limit_exceeded", "The project archive exceeds the configured size limit.",
                       WorkerErrorCategory::ResourceLimit);

    if (callbacks.progress)
        callbacks.progress({"finalize", 99, "Validating artifact"});
    if (!artifact->commit(path_error))
        return failure(path_error.code, path_error.message, path_error.category);
    SinglePlateSliceResult succeeded {true, {}, {}, WorkerErrorCategory::Internal};
    succeeded.artifacts.emplace_back("project", request.output_project);
    return succeeded;
}

nlohmann::json describe_project_selection(const DynamicPrintConfig &config)
{
    const auto *printer   = config.option<ConfigOptionString>("printer_settings_id");
    const auto *process   = config.option<ConfigOptionString>("print_settings_id");
    const auto *filaments = config.option<ConfigOptionStrings>("filament_settings_id");
    if ((printer == nullptr || printer->value.empty()) && (process == nullptr || process->value.empty()) &&
        (filaments == nullptr || filaments->values.empty()))
        return nullptr;

    nlohmann::json settings = nlohmann::json::object();
    if (const auto *different = config.option<ConfigOptionStrings>("different_settings_to_system")) {
        for (const std::string &serialized : different->values) {
            std::vector<std::string> keys;
            if (!unescape_strings_cstyle(serialized, keys))
                continue;
            for (const std::string &key : keys) {
                const ConfigOption *option = config.option(key);
                if (key.empty() || option == nullptr || settings.contains(key))
                    continue;
                if (const auto *vector = dynamic_cast<const ConfigOptionVectorBase *>(option);
                    vector != nullptr && std::find(Preset::filament_options().begin(), Preset::filament_options().end(), key) !=
                                             Preset::filament_options().end()) {
                    const std::vector<std::string> values = vector->vserialize();
                    if (!values.empty())
                        settings[key] = values.front();
                } else {
                    settings[key] = option->serialize();
                }
            }
        }
    }
    return {
        {"printer", printer ? printer->value : std::string()},
        {"process", process ? process->value : std::string()},
        {"filaments", filaments ? filaments->values : std::vector<std::string>()},
        {"settings", std::move(settings)},
    };
}

} // namespace Slic3r::Web
