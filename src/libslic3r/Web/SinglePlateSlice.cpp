#include "SinglePlateSlice.hpp"

#include "ArtifactTransaction.hpp"
#include "LayerPreview.hpp"
#include "ProjectArchive.hpp"

#include "libslic3r/BoundingBox.hpp"
#include "libslic3r/Color.hpp"
#include "libslic3r/FlushVolCalc.hpp"
#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "libslic3r/Format/OBJ.hpp"
#include "libslic3r/Format/STL.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Geometry.hpp"
#include "libslic3r/MinimumFlushVolume.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/ModelArrange.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/Print.hpp"
#include "libslic3r/Semver.hpp"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <exception>
#include <filesystem>
#include <iomanip>
#include <map>
#include <mutex>
#include <sstream>
#include <system_error>
#include <thread>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {

bool is_safe_relative_path(const std::string &value)
{
    if (value.empty() || value.size() > 512 || value.find('\\') != std::string::npos || value.find(':') != std::string::npos)
        return false;

    const std::filesystem::path path(value);
    if (path.is_absolute() || path.has_root_path())
        return false;

    return std::all_of(path.begin(), path.end(), [](const std::filesystem::path &component) {
        return component != "." && component != ".." && !component.empty();
    });
}

std::string lowercase_extension(const std::string &value)
{
    std::string extension = std::filesystem::path(value).extension().string();
    std::transform(extension.begin(), extension.end(), extension.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return extension;
}

bool count_model_triangles(const Model &model, std::optional<std::uintmax_t> max_triangles,
                           std::uintmax_t &triangle_count, WorkerManifestError &error)
{
    triangle_count = 0;
    for (const ModelObject *object : model.objects) {
        const std::uintmax_t object_triangles = object->facets_count();
        if (max_triangles && (triangle_count > *max_triangles || object_triangles > *max_triangles - triangle_count)) {
            error = {"triangle_limit_exceeded", "The input model exceeds the configured triangle limit.",
                    WorkerErrorCategory::ResourceLimit};
            return false;
        }
        triangle_count += object_triangles;
    }
    return true;
}

bool apply_profile_file(const std::filesystem::path &job_root, const std::string &relative_path,
                        WorkerErrorCategory category, DynamicPrintConfig &config, WorkerManifestError &error)
{
    std::filesystem::path resolved_profile;
    if (!resolve_job_file(job_root, relative_path, category, resolved_profile, error))
        return false;

    DynamicPrintConfig profile;
    ConfigSubstitutionContext substitutions(ForwardCompatibilitySubstitutionRule::Disable);
    std::map<std::string, std::string> metadata;
    std::string reason;
    if (profile.load_from_json(resolved_profile.string(), substitutions, true, metadata, reason) != 0) {
        error = {"profile_load_failed", reason.empty() ? "A resolved profile could not be loaded." : reason, category};
        return false;
    }
    config.apply(profile);
    return true;
}

namespace {

void add_error(SinglePlateSliceRequestValidation &result, const char *code, const char *message,
               WorkerErrorCategory category = WorkerErrorCategory::Request)
{
    result.errors.push_back({code, message, category});
}

SinglePlateSliceResult failure(std::string code, std::string message,
                               WorkerErrorCategory category = WorkerErrorCategory::Slicing)
{
    return {false, std::move(code), std::move(message), category};
}

enum class ResourceLimitExceeded {
    None,
    WallTime,
    Memory,
    Output
};

ResourceLimitExceeded exceeded_limit(const SinglePlateSliceRequest &request,
                                      const SinglePlateSliceCallbacks &callbacks,
                                      const std::chrono::steady_clock::time_point &started)
{
    const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - started).count();
    if (request.max_wall_time_ms && static_cast<std::uint64_t>(elapsed_ms) >= *request.max_wall_time_ms)
        return ResourceLimitExceeded::WallTime;
    if (request.max_memory_bytes && callbacks.memory_usage_bytes &&
        callbacks.memory_usage_bytes() > *request.max_memory_bytes)
        return ResourceLimitExceeded::Memory;
    return ResourceLimitExceeded::None;
}

SinglePlateSliceResult resource_limit_failure(ResourceLimitExceeded limit)
{
    switch (limit) {
    case ResourceLimitExceeded::WallTime:
        return failure("wall_time_limit_exceeded", "The slicing job exceeded its configured wall-time limit.",
                       WorkerErrorCategory::ResourceLimit);
    case ResourceLimitExceeded::Memory:
        return failure("memory_limit_exceeded", "The slicing job exceeded its configured memory limit.",
                       WorkerErrorCategory::ResourceLimit);
    case ResourceLimitExceeded::Output:
        return failure("output_size_limit_exceeded", "G-code output exceeds the configured size limit.",
                       WorkerErrorCategory::ResourceLimit);
    case ResourceLimitExceeded::None:
        break;
    }
    return failure("resource_limit_exceeded", "The slicing job exceeded a configured resource limit.",
                   WorkerErrorCategory::ResourceLimit);
}

class PrintCancellationMonitor
{
public:
    PrintCancellationMonitor(Print &print, const SinglePlateSliceRequest &request,
                             const SinglePlateSliceCallbacks &callbacks,
                             const std::filesystem::path &output_path,
                             const std::chrono::steady_clock::time_point &started,
                             std::atomic<ResourceLimitExceeded> &limit_exceeded)
        : m_print(print), m_request(request), m_callbacks(callbacks), m_output_path(output_path), m_started(started),
          m_limit_exceeded(limit_exceeded)
    {
        if (m_callbacks.cancellation_requested || m_request.max_wall_time_ms ||
            (m_request.max_memory_bytes && m_callbacks.memory_usage_bytes) || m_request.max_output_bytes)
            m_thread = std::thread([this]() { monitor(); });
    }

    ~PrintCancellationMonitor()
    {
        {
            std::lock_guard<std::mutex> lock(m_mutex);
            m_stopped = true;
        }
        m_condition.notify_one();
        if (m_thread.joinable())
            m_thread.join();
    }

    PrintCancellationMonitor(const PrintCancellationMonitor &) = delete;
    PrintCancellationMonitor &operator=(const PrintCancellationMonitor &) = delete;

private:
    void exceed(ResourceLimitExceeded limit)
    {
        ResourceLimitExceeded expected = ResourceLimitExceeded::None;
        m_limit_exceeded.compare_exchange_strong(expected, limit);
        m_print.cancel();
    }

    void monitor()
    {
        std::unique_lock<std::mutex> lock(m_mutex);
        while (!m_condition.wait_for(lock, std::chrono::milliseconds(10), [this]() { return m_stopped; })) {
            lock.unlock();
            if (m_callbacks.cancellation_requested && m_callbacks.cancellation_requested()) {
                m_print.cancel();
                return;
            }
            if (const ResourceLimitExceeded limit = exceeded_limit(m_request, m_callbacks, m_started);
                limit != ResourceLimitExceeded::None) {
                exceed(limit);
                return;
            }
            if (m_request.max_output_bytes) {
                std::error_code error;
                const std::uintmax_t size = std::filesystem::file_size(m_output_path, error);
                if (!error && size > *m_request.max_output_bytes) {
                    exceed(ResourceLimitExceeded::Output);
                    return;
                }
            }
            lock.lock();
        }
    }

    Print                                &m_print;
    const SinglePlateSliceRequest        &m_request;
    const SinglePlateSliceCallbacks      &m_callbacks;
    const std::filesystem::path          &m_output_path;
    std::chrono::steady_clock::time_point m_started;
    std::atomic<ResourceLimitExceeded>   &m_limit_exceeded;
    std::mutex                            m_mutex;
    std::condition_variable               m_condition;
    std::thread                           m_thread;
    bool                                  m_stopped {false};
};

template<typename T>
void read_positive_limit(const nlohmann::json &limits, const char *key, std::optional<T> &target,
                         const char *code, SinglePlateSliceRequestValidation &result)
{
    const auto value = limits.find(key);
    if (value == limits.end())
        return;
    if (!value->is_number_unsigned() || value->get<std::uint64_t>() == 0) {
        const std::string message = std::string(key) + " must be a positive unsigned integer.";
        add_error(result, code, message.c_str());
        return;
    }
    target = static_cast<T>(value->get<std::uint64_t>());
}

// An explicit placement per plated copy is small on the wire; this keeps a
// pathological request from asking the worker to duplicate a heavy mesh an
// unbounded number of times.
constexpr std::size_t MAX_OBJECT_TRANSFORMS = 64;

// Releases the owning collections load_bbs_3mf fills in, which the desktop
// otherwise frees through the plater.
struct ProjectImportGuard
{
    PlateDataPtrs        &plate_data_list;
    std::vector<Preset *> &project_presets;

    ~ProjectImportGuard()
    {
        release_PlateData_list(plate_data_list);
        for (Preset *preset : project_presets)
            delete preset;
        project_presets.clear();
    }
};

// Loads one plate of a project archive into the neutral worker model. The
// importer streams the archive itself, so containment and extraction limits are
// checked against the central directory before it is handed over.
bool load_project_plate(const std::filesystem::path &input_path, unsigned plate_index,
                        std::optional<std::uintmax_t> max_extracted_bytes, Model &model, bool &declares_plate,
                        DynamicPrintConfig *project_config, WorkerManifestError &error)
{
    ProjectArchiveLimits archive_limits;
    if (max_extracted_bytes) {
        archive_limits.max_extracted_bytes = *max_extracted_bytes;
        archive_limits.max_entry_bytes = std::min(archive_limits.max_entry_bytes, *max_extracted_bytes);
    }
    ProjectArchiveInspection inspection;
    if (!inspect_project_archive(input_path, archive_limits, inspection, error))
        return false;

    DynamicPrintConfig        loaded_config;
    ConfigSubstitutionContext substitutions(ForwardCompatibilitySubstitutionRule::Enable);
    PlateDataPtrs             plate_data_list;
    std::vector<Preset *>     project_presets;
    bool                      is_bbl_3mf = false;
    bool                      is_orca_3mf = false;
    Semver                    file_version;
    const ProjectImportGuard  import_guard {plate_data_list, project_presets};

    // LoadAuxiliary is deliberately omitted: the worker never needs the
    // embedded thumbnails or auxiliary files the desktop renders.
    const LoadStrategy strategy = LoadStrategy::LoadModel | LoadStrategy::LoadConfig | LoadStrategy::AddDefaultInstances;
    if (!load_bbs_3mf(input_path.string().c_str(), &loaded_config, &substitutions, &model, &plate_data_list,
                      &project_presets, &is_bbl_3mf, &is_orca_3mf, &file_version, nullptr, strategy, nullptr,
                      static_cast<int>(plate_index))) {
        error = {"project_load_failed", "The project archive could not be imported.", WorkerErrorCategory::Input};
        return false;
    }

    // Slicing a plate other than the first requires the desktop plate-list
    // layout that offsets every plate on one shared coordinate system, which is
    // outside the declared single-plate scope.
    if (plate_data_list.size() > 1) {
        error = {"multi_plate_project_unsupported",
                "The project contains more than one plate, which is outside the single-plate worker scope.",
                WorkerErrorCategory::Input};
        return false;
    }
    if (plate_index != 1) {
        error = {"plate_index_out_of_range", "The project does not contain the requested plate.",
                WorkerErrorCategory::Input};
        return false;
    }

    declares_plate = !plate_data_list.empty();
    if (declares_plate) {
        const PlateData &plate = *plate_data_list.front();
        loaded_config.apply(plate.config, true);
        if (!plate.filament_maps.empty())
            loaded_config.option<ConfigOptionInts>("filament_map", true)->values = plate.filament_maps;
    }
    if (project_config)
        *project_config = std::move(loaded_config);
    return true;
}

bool read_profile_path(const nlohmann::json &profiles, const char *key, std::string &target,
                       SinglePlateSliceRequestValidation &result)
{
    const auto value = profiles.find(key);
    if (value == profiles.end() || !value->is_string() || !is_safe_relative_path(value->get_ref<const std::string &>()) ||
        lowercase_extension(value->get_ref<const std::string &>()) != ".json") {
        add_error(result, "invalid_profile_path", "machine, process, and filament profiles must be safe relative JSON paths.",
                  WorkerErrorCategory::Profile);
        return false;
    }
    target = value->get<std::string>();
    return true;
}

// Reads `profiles.filaments`: 1-16 safe relative JSON paths, one per filament
// slot in order. Rejects the same way a single `profiles.filament` does when
// an individual entry is unsafe, so both spellings fail identically.
bool read_filament_paths(const nlohmann::json &profiles, std::vector<std::string> &target,
                         SinglePlateSliceRequestValidation &result)
{
    const auto value = profiles.find("filaments");
    if (value == profiles.end())
        return true;
    if (!value->is_array() || value->empty() || value->size() > MAX_FILAMENTS) {
        add_error(result, "invalid_filaments", "filaments must be an array of 1-16 safe relative JSON paths.",
                  WorkerErrorCategory::Profile);
        return false;
    }
    std::vector<std::string> parsed;
    parsed.reserve(value->size());
    for (const nlohmann::json &entry : *value) {
        if (!entry.is_string() || !is_safe_relative_path(entry.get<std::string>()) ||
            lowercase_extension(entry.get<std::string>()) != ".json") {
            add_error(result, "invalid_profile_path", "machine, process, and filament profiles must be safe relative JSON paths.",
                      WorkerErrorCategory::Profile);
            return false;
        }
        parsed.push_back(entry.get<std::string>());
    }
    target = std::move(parsed);
    return true;
}

} // namespace

bool import_single_plate_model(const std::filesystem::path &input_path, const std::string &extension,
                               unsigned plate_index, std::optional<std::uintmax_t> max_extracted_bytes,
                               Model &model, bool &declares_plate, DynamicPrintConfig *project_config,
                               WorkerManifestError &error)
{
    declares_plate = false;
    if (extension == ".3mf") {
        if (!load_project_plate(input_path, plate_index, max_extracted_bytes, model, declares_plate, project_config,
                                error))
            return false;
        if (model.objects.empty()) {
            error = {"empty_plate_selection", "The selected plate contains no printable objects.",
                    WorkerErrorCategory::Input};
            return false;
        }
    } else {
        bool loaded = false;
        if (extension == ".stl") {
            loaded = load_stl(input_path.string().c_str(), &model);
        } else {
            ObjInfo obj_info;
            std::string message;
            loaded = load_obj(input_path.string().c_str(), &model, obj_info, message);
        }
        if (!loaded || model.objects.empty()) {
            error = {"model_load_failed", "The input model could not be loaded.", WorkerErrorCategory::Input};
            return false;
        }
    }
    // A mesh has no instance until one is added, and a project archive that
    // declares no plate carries none either; every other caller (arrangement,
    // scene export) needs at least one to place the object.
    model.add_default_instances();
    return true;
}

SinglePlateSliceRequestValidation validate_single_plate_slice_request(std::string_view serialized)
{
    SinglePlateSliceRequestValidation result;
    const WorkerManifestValidation envelope_validation = validate_worker_manifest(serialized);
    if (!envelope_validation.is_valid()) {
        result.errors = envelope_validation.errors;
        return result;
    }

    const nlohmann::json manifest = nlohmann::json::parse(serialized.begin(), serialized.end(), nullptr, false);
    const nlohmann::json &payload = manifest.at("operation").at("payload");
    SinglePlateSliceRequest request;
    request.envelope = *envelope_validation.manifest;

    const auto input_model = payload.find("input_model");
    if (input_model == payload.end() || !input_model->is_string() || !is_safe_relative_path(input_model->get_ref<const std::string &>())) {
        add_error(result, "invalid_input_model", "input_model must be a safe relative path.", WorkerErrorCategory::Input);
    } else if (const std::string extension = lowercase_extension(input_model->get_ref<const std::string &>());
               extension != ".stl" && extension != ".obj" && extension != ".3mf") {
        add_error(result, "unsupported_input_format", "input_model must be an STL, OBJ, or 3MF file.",
                  WorkerErrorCategory::Input);
    } else {
        request.input_model = input_model->get<std::string>();
    }

    const auto output_gcode = payload.find("output_gcode");
    if (output_gcode == payload.end() || !output_gcode->is_string() || !is_safe_relative_path(output_gcode->get_ref<const std::string &>())) {
        add_error(result, "invalid_output_gcode", "output_gcode must be a safe relative path.");
    } else if (lowercase_extension(output_gcode->get_ref<const std::string &>()) != ".gcode") {
        add_error(result, "unsupported_output_format", "output_gcode must use the .gcode extension.");
    } else {
        request.output_gcode = output_gcode->get<std::string>();
    }

    const auto output_preview = payload.find("output_preview");
    if (output_preview != payload.end() && !output_preview->is_null()) {
        if (!output_preview->is_string() || !is_safe_relative_path(output_preview->get_ref<const std::string &>())) {
            add_error(result, "invalid_output_preview", "output_preview must be a safe relative path.");
        } else if (lowercase_extension(output_preview->get_ref<const std::string &>()) != ".json") {
            add_error(result, "unsupported_preview_format", "output_preview must use the .json extension.");
        } else {
            request.output_preview = output_preview->get<std::string>();
        }
    }

    const auto settings = payload.find("settings");
    if (settings != payload.end()) {
        if (!settings->is_object() || settings->size() > 256) {
            add_error(result, "invalid_settings", "settings must be an object with at most 256 entries.");
        } else {
            for (auto item = settings->begin(); item != settings->end(); ++item) {
                if (!item.value().is_string()) {
                    add_error(result, "invalid_setting_value", "Every setting value must use OrcaSlicer's serialized string form.");
                    break;
                }
                request.settings.emplace_back(item.key(), item.value().get<std::string>());
            }
        }
    }

    const auto limits = payload.find("limits");
    if (limits != payload.end()) {
        if (!limits->is_object()) {
            add_error(result, "invalid_limits", "limits must be an object.");
        } else {
            read_positive_limit(*limits, "max_input_bytes", request.max_input_bytes, "invalid_input_limit", result);
            read_positive_limit(*limits, "max_triangles", request.max_triangles, "invalid_triangle_limit", result);
            read_positive_limit(*limits, "max_wall_time_ms", request.max_wall_time_ms, "invalid_wall_time_limit", result);
            read_positive_limit(*limits, "max_memory_bytes", request.max_memory_bytes, "invalid_memory_limit", result);
            read_positive_limit(*limits, "max_output_bytes", request.max_output_bytes, "invalid_output_limit", result);
            read_positive_limit(*limits, "max_extracted_bytes", request.max_extracted_bytes, "invalid_extracted_limit",
                                result);
            read_positive_limit(*limits, "max_preview_bytes", request.max_preview_bytes, "invalid_preview_limit",
                                result);
        }
    }

    const auto plate_index = payload.find("plate_index");
    if (plate_index != payload.end()) {
        if (!plate_index->is_number_unsigned() || plate_index->get<std::uint64_t>() == 0 ||
            plate_index->get<std::uint64_t>() > MAX_PLATE_INDEX) {
            add_error(result, "invalid_plate_index", "plate_index must be a 1-based plate number.",
                      WorkerErrorCategory::Input);
        } else {
            request.plate_index = plate_index->get<unsigned>();
        }
    }

    const auto profiles = payload.find("profiles");
    if (profiles != payload.end()) {
        if (!profiles->is_object()) {
            add_error(result, "invalid_profiles", "profiles must be an object.");
        } else {
            read_profile_path(*profiles, "machine", request.machine_profile, result);
            read_profile_path(*profiles, "process", request.process_profile, result);

            const bool has_single_filament = profiles->contains("filament");
            const bool has_multi_filament = profiles->contains("filaments");
            if (has_single_filament && has_multi_filament) {
                // Naming both is refused outright rather than silently picking
                // one, so a client that got confused about which spelling it
                // sent finds out instead of slicing with the wrong filament(s).
                add_error(result, "invalid_profiles", "Name either profiles.filament or profiles.filaments, not both.");
            } else if (has_multi_filament) {
                read_filament_paths(*profiles, request.filament_profiles, result);
            } else if (has_single_filament) {
                std::string filament_profile;
                if (read_profile_path(*profiles, "filament", filament_profile, result))
                    request.filament_profiles = {std::move(filament_profile)};
            }
        }
    }

    const auto objects = payload.find("objects");
    if (objects != payload.end()) {
        if (!objects->is_array() || objects->size() > MAX_OBJECT_TRANSFORMS) {
            add_error(result, "invalid_objects", "objects must be an array with at most 64 entries.",
                      WorkerErrorCategory::Input);
        } else {
            std::vector<SliceObjectTransform> parsed_objects;
            bool objects_valid = true;
            for (const nlohmann::json &entry : *objects) {
                const auto source_object = entry.is_object() ? entry.find("source_object") : entry.end();
                const auto transform = entry.is_object() ? entry.find("transform") : entry.end();
                const auto filament = entry.is_object() ? entry.find("filament") : entry.end();
                if (!entry.is_object() || source_object == entry.end() || !source_object->is_number_unsigned() ||
                    transform == entry.end() || !transform->is_array() || transform->size() != 16 ||
                    (filament != entry.end() && !filament->is_number_unsigned())) {
                    objects_valid = false;
                    break;
                }
                SliceObjectTransform parsed;
                parsed.source_object = source_object->get<std::uint32_t>();
                if (filament != entry.end())
                    parsed.filament = filament->get<unsigned>();
                for (std::size_t index = 0; objects_valid && index < 16; ++index) {
                    const nlohmann::json &scalar = (*transform)[index];
                    if (!scalar.is_number() || !std::isfinite(scalar.get<double>()))
                        objects_valid = false;
                    else
                        parsed.transform[index] = scalar.get<double>();
                }
                if (!objects_valid)
                    break;
                parsed_objects.push_back(parsed);
            }
            if (!objects_valid)
                add_error(result, "invalid_object_transform",
                          "Every objects entry needs an unsigned source_object, a 16-element finite transform, and "
                          "an optional unsigned filament.",
                          WorkerErrorCategory::Input);
            else
                request.objects = std::move(parsed_objects);
        }
    }

    if (result.errors.empty())
        result.request = std::move(request);
    return result;
}

namespace {

// Identity/compatibility keys a filament profile may declare that describe
// the *preset*, not anything the sliced part needs: which printers/processes
// it's compatible with, and the vendor's own catalog IDs. The desktop CLI
// skips these for the same reason (src/OrcaSlicer.cpp, ~line 3300) when it
// composes several filament presets into one multi-filament config.
//
// filament_settings_id is deliberately *not* in this list, unlike the CLI:
// the CLI skips it because it manages that field itself to drive its preset
// UI, which the worker has no equivalent of. Keeping it per slot costs
// nothing here and leaves the composed config naming which filament preset
// actually filled each slot.
constexpr std::array<std::string_view, 4> FILAMENT_KEYS_SKIPPED_PER_SLOT {
    "compatible_printers", "compatible_prints", "model_id", "dev_model_name"
};

// N x N purge-volume matrix between every pair of filaments, mirrored from
// the desktop CLI's own synthesis (src/OrcaSlicer.cpp, ~lines 3446-3518)
// minus its GUI dependency: Slic3r::GUI::BitmapCache::parse_color4 is
// replaced with Slic3r::decode_color (Color.hpp), which parses the same
// #RRGGBB[AA] strings without linking any GUI code into this worker. A
// colour string decode_color can't parse decodes to opaque black rather than
// failing -- the same as the CLI, which never checks its parser's return
// either -- so a malformed filament_colour degrades the purge estimate
// instead of crashing the slice.
//
// get_flush_volumes_matrix/set_flush_volumes_matrix (PrintConfig.hpp) already
// index the matrix per nozzle; for the single-nozzle case every bundled
// printer has today, that collapses to one N x N pass, exactly as the CLI's
// own per-nozzle loop does, so this stays correct if a multi-nozzle printer
// is ever named without special-casing it here.
void synthesize_flush_volumes_matrix(DynamicPrintConfig &config, std::size_t filament_count)
{
    const auto *nozzle_diameter = config.option<ConfigOptionFloats>("nozzle_diameter");
    const std::size_t nozzle_count =
        nozzle_diameter != nullptr && !nozzle_diameter->values.empty() ? nozzle_diameter->values.size() : 1;

    const auto *filament_colour = config.option<ConfigOptionStrings>("filament_colour");
    const auto *filament_is_support = config.option<ConfigOptionBools>("filament_is_support");
    const auto *nozzle_flush_dataset = config.option<ConfigOptionIntsNullable>("nozzle_flush_dataset");

    std::vector<double> &matrix = config.option<ConfigOptionFloats>("flush_volumes_matrix", true)->values;
    matrix.assign(filament_count * filament_count * nozzle_count, 0.);

    for (std::size_t nozzle_id = 0; nozzle_id < nozzle_count; ++nozzle_id) {
        const std::vector<int> min_flush_volumes = get_min_flush_volumes(config, nozzle_id);
        // nozzle_flush_dataset selects a hardware-specific purge curve; every bundled
        // printer today ships one dataset (index 0), and get_at clamps to it when
        // nozzle_id runs past the option's own size, so this stays correct for that
        // case without reproducing the CLI's variant remap, which needs a printer's
        // extruder-id/variant tables this worker never loads.
        const int flush_dataset = nozzle_flush_dataset != nullptr ? nozzle_flush_dataset->get_at(nozzle_id) : 0;
        std::vector<double> nozzle_matrix(filament_count * filament_count, 0.);
        for (std::size_t from = 0; from < filament_count; ++from) {
            std::array<unsigned char, 4> from_rgba {};
            decode_color(filament_colour->get_at(from), from_rgba);
            const bool from_is_support = filament_is_support->get_at(from);
            for (std::size_t to = 0; to < filament_count; ++to) {
                if (from == to)
                    continue;
                int flush_volume;
                if (filament_is_support->get_at(to)) {
                    flush_volume = g_flush_volume_to_support;
                } else {
                    std::array<unsigned char, 4> to_rgba {};
                    decode_color(filament_colour->get_at(to), to_rgba);
                    FlushVolCalculator calculator(min_flush_volumes[from], g_max_flush_volume, flush_dataset);
                    flush_volume = calculator.calc_flush_vol(from_rgba[3], from_rgba[0], from_rgba[1], from_rgba[2],
                                                             to_rgba[3], to_rgba[0], to_rgba[1], to_rgba[2]);
                    if (from_is_support)
                        flush_volume = std::max(g_min_flush_volume_from_support, flush_volume);
                }
                nozzle_matrix[filament_count * from + to] = flush_volume;
            }
        }
        set_flush_volumes_matrix(matrix, nozzle_matrix, nozzle_id, nozzle_count);
    }
}

// Composes 2+ filament profiles into one job config, one slot per filament.
// `set_num_filaments` first resizes every key in the curated
// `filament_option_keys()` list (retraction/wipe/z-hop/filament_colour/
// filament_diameter -- about 25 keys) to `filament_profiles.size()`. Each
// profile is then loaded and, for every *vector* option it declares
// (scalars aren't per-filament and are skipped), `set_at` writes that
// filament's value into its own slot -- the same operation
// src/OrcaSlicer.cpp uses, without the variant/uptodate bookkeeping that
// exists there only to reconcile a 3MF project's stored config, which the
// worker never does.
//
// Neither pass alone leaves every per-filament option consistently sized:
// filament_option_keys() is a curated subset (it excludes filament_type,
// nozzle_temperature, filament_flow_ratio, filament_max_volumetric_speed,
// filament_is_support, filament_map, and more), and set_at only grows the
// specific key a given profile happens to declare, so a key that some
// filament slots declare and others don't can end up shorter than the
// filament count once every profile has been applied. That inconsistency
// -- some filament-scoped vectors sized N, others still sized 1 -- is
// exactly the shape of bug that produces heap corruption once downstream
// code indexes them by filament: the desktop CLI never runs a consistency
// pass at all, which is the leading explanation for the crashes it hits
// slicing more than one filament. Preset::normalize (Preset.cpp) is the
// engine's own consistency pass: it re-derives the filament count from
// filament_diameter's size and resizes every key in the much broader
// Preset::filament_options() list to that count from FullPrintConfig
// defaults, so it's run once below after every slot has been composed,
// rather than duplicating its key list and skip rules here.
bool apply_filament_profiles(const std::filesystem::path &job_root, const std::vector<std::string> &filament_paths,
                             DynamicPrintConfig &config, WorkerManifestError &error)
{
    config.set_num_filaments(static_cast<unsigned>(filament_paths.size()));
    for (std::size_t index = 0; index < filament_paths.size(); ++index) {
        std::filesystem::path resolved_profile;
        if (!resolve_job_file(job_root, filament_paths[index], WorkerErrorCategory::Profile, resolved_profile, error))
            return false;

        DynamicPrintConfig filament_profile;
        ConfigSubstitutionContext substitutions(ForwardCompatibilitySubstitutionRule::Disable);
        std::map<std::string, std::string> metadata;
        std::string reason;
        if (filament_profile.load_from_json(resolved_profile.string(), substitutions, true, metadata, reason) != 0) {
            error = {"profile_load_failed", reason.empty() ? "A resolved profile could not be loaded." : reason,
                    WorkerErrorCategory::Profile};
            return false;
        }

        for (const std::string &key : filament_profile.keys()) {
            const ConfigOption *source_opt = filament_profile.option(key);
            if (source_opt->is_scalar())
                continue;
            if (std::find(FILAMENT_KEYS_SKIPPED_PER_SLOT.begin(), FILAMENT_KEYS_SKIPPED_PER_SLOT.end(), key) !=
                FILAMENT_KEYS_SKIPPED_PER_SLOT.end())
                continue;
            // config.option(key, true) only creates storage for a key
            // print_config_def actually defines; it returns null for anything
            // it does not recognize (a vendor extension, or a key from a newer
            // profile format this engine predates). Both share the same
            // definition table filament_profile was loaded against, so this
            // should not happen in practice, but casting a null or non-vector
            // option would silently corrupt memory instead of failing loudly
            // -- unacceptable in code written specifically to rule out heap
            // corruption. The desktop CLI hits the identical case and treats
            // it as a fatal profile error (src/OrcaSlicer.cpp, the "can not
            // create option ... from filament" branch); this does the same.
            ConfigOption *dest_opt = config.option(key, true);
            if (dest_opt == nullptr || !dest_opt->is_vector()) {
                error = {"profile_option_unsupported",
                        "Filament profile \x27" + filament_paths[index] + "\x27 declares \x27" + key +
                            "\x27, which this engine does not support as a per-filament option.",
                        WorkerErrorCategory::Profile};
                return false;
            }
            static_cast<ConfigOptionVectorBase *>(dest_opt)->set_at(source_opt, index, 0);
        }
    }
    // Backfills every per-filament option that no single profile's own keys
    // grew to a consistent size N, the same way the desktop's preset
    // pipeline does before a config is ever handed to Print -- see the
    // function comment above for why set_num_filaments + set_at alone
    // cannot guarantee this. filament_diameter is already size N (it's in
    // filament_option_keys(), resized above), so normalize derives the
    // right N from it without being told.
    Preset::normalize(config);

    const std::size_t filament_count = filament_paths.size();

    // filament_colour: normalize deliberately skips this key (it's commented
    // out of Preset::filament_options(), Preset.cpp ~line 1342) because on
    // the desktop PresetBundle owns it instead. It already comes out sized N
    // here regardless -- filament_colour is one of the ~25 keys
    // set_num_filaments resizes up front (filament_option_keys()), and the
    // per-profile loop above set_at's it from whichever slots declare their
    // own colour -- so this resize is a no-op in practice; it exists to make
    // "declared colour, else the engine default" an explicit invariant of
    // this function rather than an accident of two other mechanisms agreeing.
    // Growing reuses the *default* colour (FullPrintConfig's, matching
    // resize()'s own fill semantics used throughout this file), never a
    // neighboring slot's, so two slots that end up identical -- two spools of
    // the same colour -- is unremarkable, not a synthesized collision.
    static_cast<ConfigOptionVectorBase *>(config.option("filament_colour", true))
        ->resize(filament_count, FullPrintConfig::defaults().option("filament_colour"));

    // filament_map: 1-based extruder each filament feeds. No filament preset
    // carries this -- it is a printer/project-level assignment, not a
    // property of the filament -- so unlike filament_colour, nothing above
    // ever sets it. PresetBundle synthesizes it the same way on the desktop
    // (PresetBundle.cpp, e.g. `filament_maps.resize(num_filaments, 1)`):
    // every filament defaults to extruder 1. That is also the only correct
    // value for every bundled printer that actually reaches this function: a
    // single-filament request never calls apply_filament_profiles at all
    // (see the caller in slice_single_plate), so a single-filament slice on a
    // multi-nozzle printer -- e.g. BBL/machine/fdm_bbl_3dp_002_common.json's
    // 2 nozzles, or the WEMAKE3D commons' 4 -- is unaffected by the check
    // below. A *multi-filament* request against a multi-nozzle printer would
    // need real assignment logic here -- deciding which of several nozzles
    // each filament actually feeds, e.g. via the grouping in
    // FilamentGroup.cpp -- which is out of scope. That combination must not
    // silently fall through to "map everything to extruder 1": on hardware
    // where a wrong nozzle assignment is physically reachable, mis-slicing
    // is worse than refusing, so it is rejected as a profile error here
    // rather than asserted -- an assert compiles out of the Release build
    // this worker ships, which would make the silent-wrong-answer path the
    // only one ever exercised in production.
    const auto *nozzle_diameter = config.option<ConfigOptionFloats>("nozzle_diameter");
    const std::size_t extruder_count =
        nozzle_diameter != nullptr && !nozzle_diameter->values.empty() ? nozzle_diameter->values.size() : 1;
    if (extruder_count > 1) {
        error = {"multi_nozzle_filament_map_unsupported",
                "This printer profile declares more than one nozzle; multi-filament requests do not yet support "
                "assigning filaments to a specific nozzle.",
                WorkerErrorCategory::Profile};
        return false;
    }
    config.option<ConfigOptionInts>("filament_map", true)->values.assign(filament_count, 1);

    synthesize_flush_volumes_matrix(config, filament_count);
    return true;
}

// Replaces every imported object with one explicitly placed copy per
// `objects` entry. Each copy is a full clone rather than another instance of
// the same ModelObject: the print engine assumes every instance of one object
// shares sliced layer geometry, which does not hold once a duplicate's
// transform differs from its source by more than a Z rotation and an XY
// offset. Validates every source_object and filament before mutating the
// model, so a rejected request leaves it untouched. `filament_count` is the
// number of filament slots the composed job config will end up with (1 when
// the request names none or one), which bounds what a per-object `filament`
// may legally select.
bool apply_explicit_object_transforms(Model &model, const std::vector<SliceObjectTransform> &objects,
                                      unsigned filament_count, WorkerManifestError &error)
{
    const std::size_t source_count = model.objects.size();
    for (const SliceObjectTransform &entry : objects) {
        if (entry.source_object >= source_count) {
            error = {"invalid_source_object", "source_object is out of range for the imported model.",
                    WorkerErrorCategory::Input};
            return false;
        }
        if (entry.filament > filament_count) {
            error = {"invalid_object_filament", "filament is out of range for the requested filament profiles.",
                    WorkerErrorCategory::Input};
            return false;
        }
    }

    for (const SliceObjectTransform &entry : objects) {
        ModelObject *copy = model.add_object(*model.objects[entry.source_object]);
        while (copy->instances.size() > 1)
            copy->delete_instance(copy->instances.size() - 1);
        if (copy->instances.empty())
            copy->add_instance();
        Transform3d matrix = Transform3d::Identity();
        matrix.matrix() = Eigen::Map<const Eigen::Matrix<double, 4, 4>>(entry.transform.data());
        copy->instances.front()->set_transformation(Geometry::Transformation(matrix));
        // 0 leaves whatever extruder assignment the source object already
        // carried (e.g. from a project archive) untouched.
        if (entry.filament != 0)
            copy->config.set_key_value("extruder", new ConfigOptionInt(static_cast<int>(entry.filament)));
    }
    // The originals are replaced, not duplicated alongside their copies.
    for (std::size_t index = 0; index < source_count; ++index)
        model.delete_object(std::size_t(0));
    return true;
}

// On the desktop, PartPlate positions the prime tower on its plate
// (PartPlate::estimate_wipe_tower_polygon, GUI-only) before a print is ever
// validated. The worker has no PartPlateList -- nothing else in this
// codebase ever repositions the tower -- so without this, a slice just gets
// the config's own default (wipe_tower_x=15, wipe_tower_y=220;
// PrintConfig.cpp ~line 7541), which sits at the back edge of every bundled
// single-nozzle printer's bed (e.g. the 220 mm-deep Anycubic Kobra) or
// beyond it, and Print::validate() correctly refuses that as soon as more
// than one filament makes the tower real ("Prime Tower is partially outside
// the printable area"). This mirrors just enough of PartPlate's placement
// logic -- without its GUI dependency -- to make a plain multi-filament
// request work: if the configured footprint (tower body plus its brim) does
// not fit the bed get_bed_shape(config) reports, move it just inside the
// bed's near corner and warn, so the change is visible rather than silent.
// A footprint that already fits (including one the caller placed
// deliberately) is left exactly as configured. Rotation
// (wipe_tower_rotation_angle) is not accounted for: every bundled printer
// leaves it at its default of zero, and a rotated footprint would need the
// same polygon math Print::validate() itself uses, which is out of scope for
// a default placement.
//
// Returns true when it changed wipe_tower_x/y, so the caller knows to
// re-apply config to print before validating.
bool reposition_wipe_tower_if_needed(const Print &print, DynamicPrintConfig &config, unsigned filament_count,
                                     const SinglePlateSliceCallbacks &callbacks)
{
    if (filament_count <= 1 || !print.has_wipe_tower())
        return false;

    const Points bed_shape = get_bed_shape(config);
    if (bed_shape.empty())
        return false;
    const BoundingBox bed_box(bed_shape);
    const double bed_min_x = unscale<double>(bed_box.min.x());
    const double bed_min_y = unscale<double>(bed_box.min.y());
    const double bed_max_x = unscale<double>(bed_box.max.x());
    const double bed_max_y = unscale<double>(bed_box.max.y());

    auto *wipe_tower_x = config.option<ConfigOptionFloats>("wipe_tower_x", true);
    auto *wipe_tower_y = config.option<ConfigOptionFloats>("wipe_tower_y", true);
    const double width = config.option<ConfigOptionFloat>("prime_tower_width", true)->value;
    // Pre-generation depth/brim estimate: the same one Print::validate() itself
    // reads before the tower mesh exists (Print.cpp, the has_wipe_tower()
    // branch of the collision-check function, just above the "partially
    // outside the printable area" message).
    const WipeTowerData &tower_data = print.wipe_tower_data(filament_count);
    const double depth = tower_data.depth;
    const double brim = std::max(0.f, tower_data.brim_width);

    const double x = wipe_tower_x->get_at(0);
    const double y = wipe_tower_y->get_at(0);
    const bool fits = (x - brim) >= bed_min_x && (y - brim) >= bed_min_y && (x + width + brim) <= bed_max_x &&
                      (y + depth + brim) <= bed_max_y;
    if (fits)
        return false;

    // A small clearance beyond the brim so the repositioned tower doesn't sit
    // flush against the bed edge.
    constexpr double BED_MARGIN_MM = 2.0;
    const double new_x = bed_min_x + brim + BED_MARGIN_MM;
    const double new_y = bed_min_y + brim + BED_MARGIN_MM;
    wipe_tower_x->values[0] = new_x;
    wipe_tower_y->values[0] = new_y;
    if (callbacks.warning) {
        std::ostringstream message;
        message << "Prime tower did not fit the printable area at its configured position; moved it to ("
                << std::fixed << std::setprecision(1) << new_x << ", " << new_y << ") mm.";
        callbacks.warning(message.str());
    }
    return true;
}

} // namespace

// Test-only entry point: composes 2+ filament profiles into one job config
// the same way slice_single_plate does for a multi-filament request, without
// running an actual slice. apply_filament_profiles itself has internal
// linkage (it's file-local to this translation unit, alongside the other
// slicing helpers above), so a [SliceRequest] test that wants to assert the
// per-filament synthesis directly -- filament_colour/filament_map/
// flush_volumes_matrix coming out sized correctly for N filaments -- needs
// this thin, declared wrapper. Building a full Model and Print to observe
// the same config would cost as much as a real slice for no extra coverage
// of the synthesis logic itself.
bool compose_multi_filament_config_for_testing(const std::filesystem::path &job_root,
                                               const std::vector<std::string> &filament_profiles,
                                               DynamicPrintConfig &config, WorkerManifestError &error)
{
    config = DynamicPrintConfig::full_print_config();
    return apply_filament_profiles(job_root, filament_profiles, config, error);
}

SinglePlateSliceResult slice_single_plate(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                          const SinglePlateSliceCallbacks &callbacks)
{
    const auto started = std::chrono::steady_clock::now();
    std::atomic<ResourceLimitExceeded> limit_exceeded {ResourceLimitExceeded::None};
    try {
        const auto progress = [&callbacks](const char *stage, unsigned percent, const char *message) {
            if (callbacks.progress)
                callbacks.progress({stage, percent, message});
        };
        const auto throw_if_canceled = [&callbacks, &request, &started, &limit_exceeded]() {
            if (const ResourceLimitExceeded limit = exceeded_limit(request, callbacks, started);
                limit != ResourceLimitExceeded::None) {
                limit_exceeded = limit;
                throw CanceledException();
            }
            if (callbacks.cancellation_requested && callbacks.cancellation_requested())
                throw CanceledException();
        };

        throw_if_canceled();
        progress("input", 2, "Loading model");
        std::filesystem::path input_path;
        WorkerManifestError path_error;
        if (!resolve_job_file(job_root, request.input_model, WorkerErrorCategory::Input, input_path, path_error))
            return failure(path_error.code, path_error.message, path_error.category);
        std::error_code filesystem_error;
        const std::uintmax_t input_size = std::filesystem::file_size(input_path, filesystem_error);
        if (filesystem_error)
            return failure("input_size_unavailable", "Unable to determine the input model size.", WorkerErrorCategory::Input);
        if (request.max_input_bytes && input_size > *request.max_input_bytes)
            return failure("input_size_limit_exceeded", "The input model exceeds the configured size limit.",
                           WorkerErrorCategory::ResourceLimit);

        const std::string extension = lowercase_extension(request.input_model);
        const bool is_project = extension == ".3mf";
        Model model;
        bool declares_plate = false;
        DynamicPrintConfig project_config;
        if (!import_single_plate_model(input_path, extension, request.plate_index, request.max_extracted_bytes, model,
                                       declares_plate, &project_config, path_error))
            return failure(path_error.code, path_error.message, path_error.category);

        std::uintmax_t triangle_count = 0;
        if (!count_model_triangles(model, request.max_triangles, triangle_count, path_error))
            return failure(path_error.code, path_error.message, path_error.category);

        throw_if_canceled();
        progress("input", 10, "Model loaded");

        // Explicit transforms replace every imported object with one placed
        // copy per entry, so the placement the browser displayed is exactly
        // what gets sliced instead of whatever arrangement would choose. The
        // filament count is a pure function of the request (independent of
        // which profile files actually resolve), so it's known this early.
        const unsigned filament_count = static_cast<unsigned>(std::max<std::size_t>(1, request.filament_profiles.size()));
        const bool objects_explicit = !request.objects.empty();
        if (objects_explicit) {
            if (!apply_explicit_object_transforms(model, request.objects, filament_count, path_error))
                return failure(path_error.code, path_error.message, path_error.category);
            // A duplicate multiplies triangle count, so the configured limit is
            // enforced again against what will actually be sliced.
            if (!count_model_triangles(model, request.max_triangles, triangle_count, path_error))
                return failure(path_error.code, path_error.message, path_error.category);
        }

        DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
        progress("configuration", 12, "Loading profiles");
        // The embedded project configuration is the base for a project archive;
        // profiles resolved by the request and curated overrides still win.
        if (is_project)
            config.apply(project_config, true);
        for (const std::string *profile_path : {&request.machine_profile, &request.process_profile}) {
            if (profile_path->empty())
                continue;
            if (!apply_profile_file(job_root, *profile_path, WorkerErrorCategory::Profile, config, path_error))
                return failure(path_error.code, path_error.message, path_error.category);
            throw_if_canceled();
        }
        // Exactly one filament takes the same config.apply(profile) path every
        // existing caller already relies on producing byte-identical G-code
        // for; only 2+ filaments compose through set_num_filaments + set_at.
        if (request.filament_profiles.size() == 1) {
            if (!apply_profile_file(job_root, request.filament_profiles.front(), WorkerErrorCategory::Profile, config,
                                    path_error))
                return failure(path_error.code, path_error.message, path_error.category);
        } else if (request.filament_profiles.size() > 1) {
            if (!apply_filament_profiles(job_root, request.filament_profiles, config, path_error))
                return failure(path_error.code, path_error.message, path_error.category);
        }
        throw_if_canceled();
        for (const auto &[key, value] : request.settings)
            config.set_deserialize_strict(key, value);

        // An explicit or plate-declared placement is the author's own choice,
        // so only a loose, unplaced mesh is arranged.
        if (objects_explicit) {
            progress("arrangement", 20, "Applying explicit placement");
        } else if (declares_plate) {
            progress("arrangement", 20, "Keeping project placement");
        } else {
            progress("arrangement", 20, "Arranging objects");
            ArrangeParams arrange_params(scaled(min_object_distance(config)));
            arrange_params.progressind = [](unsigned, std::string) {};
            arrange_objects(model, get_bed_shape(config), arrange_params);
        }
        throw_if_canceled();

        std::optional<ArtifactTransaction> artifact =
            ArtifactTransaction::begin(job_root, request.output_gcode, request.envelope.job_id, path_error);
        if (!artifact)
            return failure(path_error.code, path_error.message, path_error.category);

        Print print;
        PrintCancellationMonitor cancellation_monitor(print, request, callbacks, artifact->temporary_path(), started,
                                                       limit_exceeded);
        for (ModelObject *object : model.objects) {
            // A plate, or the browser's own placement, may deliberately sink an
            // object below the bed, so only fully buried objects are lifted,
            // matching the desktop importer.
            object->ensure_on_bed(declares_plate || objects_explicit);
            print.auto_assign_extruders(object);
        }
        print.apply(model, config);
        // Only after apply(): wipe_tower_data() reads object heights off the
        // Print's own m_objects, which apply() is what populates. Reapplying
        // when the position actually moved makes print.config() (what
        // validate()'s own tower-containment check reads) see the correction.
        if (reposition_wipe_tower_if_needed(print, config, filament_count, callbacks))
            print.apply(model, config);
        const StringObjectException validation_error = print.validate();
        if (!validation_error.string.empty())
            return failure("slice_validation_failed", validation_error.string, WorkerErrorCategory::Validation);

        unsigned last_public_percent = 25;
        bool exporting = false;
        print.set_status_callback([&callbacks, &last_public_percent, &exporting](const PrintBase::SlicingStatus &status) {
            if (status.warning_step >= 0 && !status.text.empty() && callbacks.warning)
                callbacks.warning(status.text);
            if (status.percent < 0 || !callbacks.progress)
                return;
            const unsigned engine_percent = static_cast<unsigned>(std::clamp(status.percent, 0, 100));
            const unsigned public_percent = exporting ? 90 + (engine_percent * 8 / 100) : 25 + (engine_percent * 60 / 100);
            last_public_percent = std::max(last_public_percent, public_percent);
            callbacks.progress({exporting ? "export" : "slicing", last_public_percent,
                                exporting ? "Exporting G-code" : "Slicing model"});
        });
        throw_if_canceled();
        progress("slicing", 25, "Slicing model");
        print.process();
        throw_if_canceled();
        exporting = true;
        progress("export", 90, "Exporting G-code");
        // The exporter already runs the G-code processor, so taking its result
        // here is what makes the layer preview free of a second parse.
        GCodeProcessorResult processed;
        print.export_gcode(artifact->temporary_path().string(), &processed, nullptr);
        if (!std::filesystem::is_regular_file(artifact->temporary_path()))
            return failure("gcode_export_failed", "G-code export did not produce a non-empty artifact.");
        const std::uintmax_t output_size = std::filesystem::file_size(artifact->temporary_path());
        if (request.max_output_bytes && output_size > *request.max_output_bytes)
            return resource_limit_failure(ResourceLimitExceeded::Output);
        if (output_size == 0)
            return failure("gcode_export_failed", "G-code export did not produce a non-empty artifact.");

        throw_if_canceled();
        progress("finalize", 99, "Validating artifact");
        if (!artifact->commit(path_error))
            return failure(path_error.code, path_error.message, path_error.category);

        SinglePlateSliceResult published {true, {}, {}, WorkerErrorCategory::Slicing};
        published.artifacts.push_back({"gcode", request.output_gcode});
        if (!request.output_preview.empty()) {
            progress("finalize", 99, "Writing layer preview");
            const std::string preview_data =
                std::filesystem::path(request.output_preview).replace_extension(".bin").generic_string();
            LayerPreviewResult preview;
            if (!write_layer_preview(processed, job_root, request.output_preview, preview_data,
                                     request.envelope.job_id, request.max_preview_bytes, preview, path_error))
                return failure(path_error.code, path_error.message, path_error.category);
            if (preview.written) {
                published.artifacts.push_back({"preview", request.output_preview});
                published.artifacts.push_back({"preview_data", preview_data});
            } else if (callbacks.warning && !preview.omitted_reason.empty()) {
                // A missing preview never invalidates good G-code, so this is a
                // warning on the job rather than a failed slice.
                callbacks.warning(preview.omitted_reason);
            }
        }
        return published;
    } catch (const CanceledException &) {
        if (const ResourceLimitExceeded limit = limit_exceeded.load(); limit != ResourceLimitExceeded::None)
            return resource_limit_failure(limit);
        return failure("job_canceled", "The slicing job was canceled.", WorkerErrorCategory::Cancellation);
    } catch (const std::exception &error) {
        return failure("slice_failed", error.what());
    }
}

} // namespace Slic3r::Web
