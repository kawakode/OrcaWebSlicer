#include "SinglePlateSlice.hpp"

#include "ArtifactTransaction.hpp"
#include "PlateArtifacts.hpp"
#include "ProjectArchive.hpp"

#include "libslic3r/ClipperUtils.hpp"
#include "libslic3r/GCode/GCodeProcessor.hpp"

#include "libslic3r/Format/OBJ.hpp"
#include "libslic3r/Format/STL.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/ModelArrange.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/Print.hpp"
#include "libslic3r/Semver.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cctype>
#include <chrono>
#include <condition_variable>
#include <exception>
#include <filesystem>
#include <map>
#include <mutex>
#include <system_error>
#include <thread>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

void add_error(SinglePlateSliceRequestValidation &result, const char *code, const char *message,
               WorkerErrorCategory category = WorkerErrorCategory::Request)
{
    result.errors.push_back({code, message, category});
}

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

// Mirrors the desktop plate-count ceiling so an out-of-range selection is
// rejected while parsing instead of after opening the archive.
constexpr std::uint64_t MAX_PLATE_INDEX = 36;

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

struct ProjectPlate
{
    DynamicPrintConfig config;
    // True only when the archive declares plates. A plain 3MF is a mesh
    // container whose coordinates carry no plate placement, so it is arranged
    // like a loose mesh instead.
    bool declares_plate {false};
    // True once a project archive supplied `config`.
    bool loaded {false};
};

// Loads one plate of a project archive into the neutral worker model. The
// importer streams the archive itself, so containment and extraction limits are
// checked against the central directory before it is handed over.
SinglePlateSliceResult load_project_plate(const SinglePlateSliceRequest &request, const std::filesystem::path &input_path,
                                          Model &model, ProjectPlate &project)
{
    ProjectArchiveLimits archive_limits;
    if (request.max_extracted_bytes) {
        archive_limits.max_extracted_bytes = *request.max_extracted_bytes;
        archive_limits.max_entry_bytes = std::min(archive_limits.max_entry_bytes, *request.max_extracted_bytes);
    }
    ProjectArchiveInspection inspection;
    WorkerManifestError archive_error;
    if (!inspect_project_archive(input_path, archive_limits, inspection, archive_error))
        return failure(archive_error.code, archive_error.message, archive_error.category);

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
                      static_cast<int>(request.plate_index)))
        return failure("project_load_failed", "The project archive could not be imported.", WorkerErrorCategory::Input);

    // Slicing a plate other than the first requires the desktop plate-list
    // layout that offsets every plate on one shared coordinate system, which is
    // outside the declared single-plate scope.
    if (plate_data_list.size() > 1)
        return failure("multi_plate_project_unsupported",
                       "The project contains more than one plate, which is outside the single-plate worker scope.",
                       WorkerErrorCategory::Input);
    if (request.plate_index != 1)
        return failure("plate_index_out_of_range", "The project does not contain the requested plate.",
                       WorkerErrorCategory::Input);

    project.config = std::move(loaded_config);
    project.loaded = true;
    project.declares_plate = !plate_data_list.empty();
    if (project.declares_plate) {
        const PlateData &plate = *plate_data_list.front();
        project.config.apply(plate.config, true);
        if (!plate.filament_maps.empty())
            project.config.option<ConfigOptionInts>("filament_map", true)->values = plate.filament_maps;
    }
    return {true, {}, {}, WorkerErrorCategory::Input};
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

bool is_model_extension(const std::string &extension)
{
    return extension == ".stl" || extension == ".obj" || extension == ".3mf";
}

// Reads an optional output path. A present but unsafe path, or one with the
// wrong extension, is refused; an absent one leaves `target` empty.
void read_output_path(const nlohmann::json &payload, const char *key, const char *extension, std::string &target,
                      SinglePlateSliceRequestValidation &result)
{
    const auto value = payload.find(key);
    if (value == payload.end())
        return;
    if (!value->is_string() || !is_safe_relative_path(value->get_ref<const std::string &>()) ||
        lowercase_extension(value->get_ref<const std::string &>()) != extension) {
        const std::string message = std::string(key) + " must be a safe relative path ending in " + extension + ".";
        add_error(result, "invalid_output_path", message.c_str());
        return;
    }
    target = value->get<std::string>();
}

// Bounded so a request cannot ask the worker to load an unbounded number of
// files or instantiate an unbounded number of copies.
constexpr std::size_t MAX_PLATE_OBJECTS    = 256;
constexpr std::size_t MAX_OBJECT_INSTANCES = 64;
constexpr std::size_t MAX_PLATE_MODELS     = 16;
// A plate is at most a few metres across; anything beyond this is a mistake.
constexpr double      MAX_TRANSLATION_MM   = 100000.;

// A placement is an affine, invertible transform in millimetres. Scale and
// rotation are otherwise unconstrained, exactly as the desktop allows.
bool is_valid_transform(const std::array<double, 16> &values)
{
    if (!std::all_of(values.begin(), values.end(), [](double value) { return std::isfinite(value); }))
        return false;
    if (std::abs(values[12]) > 1e-9 || std::abs(values[13]) > 1e-9 || std::abs(values[14]) > 1e-9 ||
        std::abs(values[15] - 1.) > 1e-9)
        return false;
    if (std::abs(values[3]) > MAX_TRANSLATION_MM || std::abs(values[7]) > MAX_TRANSLATION_MM ||
        std::abs(values[11]) > MAX_TRANSLATION_MM)
        return false;
    const double determinant = values[0] * (values[5] * values[10] - values[6] * values[9]) -
                               values[1] * (values[4] * values[10] - values[6] * values[8]) +
                               values[2] * (values[4] * values[9] - values[5] * values[8]);
    return std::abs(determinant) > 1e-9 && std::abs(determinant) < 1e9;
}

void read_plate_objects(const nlohmann::json &objects, SinglePlateSliceRequest &request,
                        SinglePlateSliceRequestValidation &result)
{
    if (!objects.is_array() || objects.empty() || objects.size() > MAX_PLATE_OBJECTS) {
        add_error(result, "invalid_plate_objects", "objects must be an array of 1 to 256 placed objects.",
                  WorkerErrorCategory::Input);
        return;
    }
    std::vector<std::string> models;
    for (const nlohmann::json &described : objects) {
        PlateObject object;
        const auto model = described.is_object() ? described.find("model") : described.end();
        if (model == described.end() || !model->is_string() || !is_safe_relative_path(model->get_ref<const std::string &>()) ||
            !is_model_extension(lowercase_extension(model->get_ref<const std::string &>()))) {
            add_error(result, "invalid_plate_object", "Every object must name a safe relative STL, OBJ, or 3MF model.",
                      WorkerErrorCategory::Input);
            return;
        }
        object.model = model->get<std::string>();
        if (std::find(models.begin(), models.end(), object.model) == models.end())
            models.push_back(object.model);

        const auto index = described.find("object");
        if (index != described.end()) {
            if (!index->is_number_unsigned() || index->get<std::uint64_t>() >= MAX_PLATE_OBJECTS) {
                add_error(result, "invalid_plate_object", "object must be the 0-based index of an object in its model.",
                          WorkerErrorCategory::Input);
                return;
            }
            object.object_index = index->get<unsigned>();
        }

        const auto instances = described.find("instances");
        if (instances == described.end() || !instances->is_array() || instances->empty() ||
            instances->size() > MAX_OBJECT_INSTANCES) {
            add_error(result, "invalid_plate_object", "Every object must declare 1 to 64 instances.",
                      WorkerErrorCategory::Input);
            return;
        }
        for (const nlohmann::json &instance : *instances) {
            const auto transform = instance.is_object() ? instance.find("transform") : instance.end();
            std::array<double, 16> values {};
            bool readable = transform != instance.end() && transform->is_array() && transform->size() == values.size();
            for (std::size_t position = 0; readable && position < values.size(); ++position) {
                readable = (*transform)[position].is_number();
                if (readable)
                    values[position] = (*transform)[position].get<double>();
            }
            if (!readable || !is_valid_transform(values)) {
                add_error(result, "invalid_instance_transform",
                          "Every instance transform must be a finite, invertible, row-major 4x4 affine matrix in millimetres.",
                          WorkerErrorCategory::Input);
                return;
            }
            object.instances.push_back(values);
        }
        request.objects.push_back(std::move(object));
    }
    if (models.size() > MAX_PLATE_MODELS)
        add_error(result, "invalid_plate_objects", "A plate may reference at most 16 model files.",
                  WorkerErrorCategory::Input);
}

// Loads one model file the request names. A project archive contributes its
// selected plate, and its configuration is kept in `project`.
SinglePlateSliceResult load_model_file(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                       const std::string &relative_path, Model &model, ProjectPlate &project)
{
    std::filesystem::path input_path;
    WorkerManifestError path_error;
    if (!resolve_job_file(job_root, relative_path, WorkerErrorCategory::Input, input_path, path_error))
        return failure(path_error.code, path_error.message, path_error.category);
    std::error_code filesystem_error;
    const std::uintmax_t input_size = std::filesystem::file_size(input_path, filesystem_error);
    if (filesystem_error)
        return failure("input_size_unavailable", "Unable to determine the input model size.", WorkerErrorCategory::Input);
    if (request.max_input_bytes && input_size > *request.max_input_bytes)
        return failure("input_size_limit_exceeded", "The input model exceeds the configured size limit.",
                       WorkerErrorCategory::ResourceLimit);

    const std::string extension = lowercase_extension(relative_path);
    if (extension == ".3mf") {
        if (const SinglePlateSliceResult loaded = load_project_plate(request, input_path, model, project); !loaded.success)
            return loaded;
        if (model.objects.empty())
            return failure("empty_plate_selection", "The selected plate contains no printable objects.",
                           WorkerErrorCategory::Input);
        return {true, {}, {}, WorkerErrorCategory::Input};
    }
    bool loaded = false;
    if (extension == ".stl") {
        loaded = load_stl(input_path.string().c_str(), &model);
    } else {
        ObjInfo obj_info;
        std::string message;
        loaded = load_obj(input_path.string().c_str(), &model, obj_info, message);
    }
    if (!loaded || model.objects.empty())
        return failure("model_load_failed", "The input model could not be loaded.", WorkerErrorCategory::Input);
    return {true, {}, {}, WorkerErrorCategory::Input};
}

SinglePlateSliceResult check_triangle_limit(const SinglePlateSliceRequest &request, const Model &model)
{
    std::uintmax_t triangle_count = 0;
    for (const ModelObject *object : model.objects) {
        const std::uintmax_t object_triangles = object->facets_count();
        if (request.max_triangles && (triangle_count > *request.max_triangles ||
            object_triangles > *request.max_triangles - triangle_count))
            return failure("triangle_limit_exceeded", "The input model exceeds the configured triangle limit.",
                           WorkerErrorCategory::ResourceLimit);
        triangle_count += object_triangles;
    }
    return {true, {}, {}, WorkerErrorCategory::Input};
}

// Builds the plate the browser composed: each listed object is copied out of
// its model file and given exactly the submitted instance transforms.
SinglePlateSliceResult assemble_plate(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                      Model &model, ProjectPlate &project,
                                      const std::function<void()> &throw_if_canceled)
{
    std::map<std::string, Model> sources;
    for (const PlateObject &placed : request.objects) {
        auto source = sources.find(placed.model);
        if (source == sources.end()) {
            Model loaded;
            // Only the first archive's configuration seeds the plate, so the
            // order of the request decides which project settings apply.
            ProjectPlate file_project;
            if (const SinglePlateSliceResult result = load_model_file(request, job_root, placed.model, loaded, file_project);
                !result.success)
                return result;
            if (file_project.loaded && !project.loaded) {
                project.config = std::move(file_project.config);
                project.loaded = true;
            }
            source = sources.emplace(placed.model, std::move(loaded)).first;
            throw_if_canceled();
        }
        if (placed.object_index >= source->second.objects.size())
            return failure("unknown_model_object", "An object index does not exist in its model file.",
                           WorkerErrorCategory::Input);
        ModelObject *object = model.add_object(*source->second.objects[placed.object_index]);
        object->clear_instances();
        for (const std::array<double, 16> &values : placed.instances) {
            Transform3d transform = Transform3d::Identity();
            transform.matrix() = Eigen::Map<const Eigen::Matrix<double, 4, 4, Eigen::RowMajor>>(values.data());
            object->add_instance(Geometry::Transformation(transform));
        }
        object->invalidate_bounding_box();
    }
    return {true, {}, {}, WorkerErrorCategory::Input};
}

// Refuses a submitted placement that leaves the printable volume, which the
// desktop also refuses to slice. Arranged plates are never checked here, so
// existing single-model behaviour is unchanged.
SinglePlateSliceResult check_placement(const Model &model, const DynamicPrintConfig &config)
{
    const Polygon  bed(get_bed_shape(config));
    const Polygons printable = offset(bed, float(scaled(0.05)));
    const double   height    = config.opt_float("printable_height");
    for (const ModelObject *object : model.objects) {
        for (std::size_t index = 0; index < object->instances.size(); ++index) {
            const Polygon hull = object->convex_hull_2d(object->instances[index]->get_matrix());
            const bool    inside = std::all_of(hull.points.begin(), hull.points.end(), [&printable](const Point &point) {
                return std::any_of(printable.begin(), printable.end(),
                                   [&point](const Polygon &polygon) { return polygon.contains(point); });
            });
            if (!inside || object->instance_bounding_box(index).max.z() > height + 0.05)
                return failure("object_outside_plate", "\"" + object->name + "\" does not fit inside the printable volume.",
                               WorkerErrorCategory::Validation);
        }
    }
    return {true, {}, {}, WorkerErrorCategory::Validation};
}

} // namespace

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

    request.operation = request.envelope.operation == "inspect" ? WorkerOperation::Inspect : WorkerOperation::Slice;
    const bool inspecting = request.operation == WorkerOperation::Inspect;

    const auto input_model = payload.find("input_model");
    const auto objects = payload.find("objects");
    if (input_model != payload.end() && objects != payload.end()) {
        add_error(result, "ambiguous_plate", "A request names either input_model or objects, not both.",
                  WorkerErrorCategory::Input);
    } else if (objects != payload.end() && !inspecting) {
        read_plate_objects(*objects, request, result);
    } else if (input_model == payload.end() || !input_model->is_string() || !is_safe_relative_path(input_model->get_ref<const std::string &>())) {
        add_error(result, "invalid_input_model", "input_model must be a safe relative path.", WorkerErrorCategory::Input);
    } else if (!is_model_extension(lowercase_extension(input_model->get_ref<const std::string &>()))) {
        add_error(result, "unsupported_input_format", "input_model must be an STL, OBJ, or 3MF file.",
                  WorkerErrorCategory::Input);
    } else {
        request.input_model = input_model->get<std::string>();
    }

    if (inspecting) {
        read_output_path(payload, "output_mesh", ".bin", request.output_mesh, result);
        read_output_path(payload, "output_inspection", ".json", request.output_inspection, result);
        if (request.output_mesh.empty() || request.output_inspection.empty())
            add_error(result, "invalid_output_path", "An inspection requires output_mesh and output_inspection.");
    } else {
        const auto output_gcode = payload.find("output_gcode");
        if (output_gcode == payload.end() || !output_gcode->is_string() || !is_safe_relative_path(output_gcode->get_ref<const std::string &>())) {
            add_error(result, "invalid_output_gcode", "output_gcode must be a safe relative path.");
        } else if (lowercase_extension(output_gcode->get_ref<const std::string &>()) != ".gcode") {
            add_error(result, "unsupported_output_format", "output_gcode must use the .gcode extension.");
        } else {
            request.output_gcode = output_gcode->get<std::string>();
        }
        read_output_path(payload, "output_preview", ".bin", request.output_preview, result);
        read_output_path(payload, "output_preview_index", ".json", request.output_preview_index, result);
        if (request.output_preview.empty() != request.output_preview_index.empty())
            add_error(result, "invalid_output_path", "output_preview and output_preview_index are given together.");
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
            read_profile_path(*profiles, "filament", request.filament_profile, result);
        }
    }

    if (result.errors.empty())
        result.request = std::move(request);
    return result;
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
        const bool explicit_placement = !request.objects.empty();
        const bool is_project = !explicit_placement && lowercase_extension(request.input_model) == ".3mf";
        Model model;
        ProjectPlate project;
        if (explicit_placement) {
            if (const SinglePlateSliceResult assembled = assemble_plate(request, job_root, model, project, throw_if_canceled);
                !assembled.success)
                return assembled;
        } else if (const SinglePlateSliceResult loaded = load_model_file(request, job_root, request.input_model, model, project);
                   !loaded.success) {
            return loaded;
        }
        if (const SinglePlateSliceResult limited = check_triangle_limit(request, model); !limited.success)
            return limited;
        WorkerManifestError path_error;

        throw_if_canceled();
        progress("input", 10, "Model loaded");
        if (!is_project && !explicit_placement)
            model.add_default_instances();
        DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
        progress("configuration", 12, "Loading profiles");
        // The embedded project configuration is the base for a project archive;
        // profiles resolved by the request and curated overrides still win.
        if (project.loaded)
            config.apply(project.config, true);
        for (const std::string *profile_path : {&request.machine_profile, &request.process_profile, &request.filament_profile}) {
            if (profile_path->empty())
                continue;

            DynamicPrintConfig profile;
            ConfigSubstitutionContext substitutions(ForwardCompatibilitySubstitutionRule::Disable);
            std::map<std::string, std::string> metadata;
            std::string reason;
            std::filesystem::path resolved_profile;
            if (!resolve_job_file(job_root, *profile_path, WorkerErrorCategory::Profile, resolved_profile, path_error))
                return failure(path_error.code, path_error.message, path_error.category);
            if (profile.load_from_json(resolved_profile.string(), substitutions, true, metadata, reason) != 0)
                return failure("profile_load_failed", reason.empty() ? "A resolved profile could not be loaded." : reason,
                               WorkerErrorCategory::Profile);
            config.apply(profile);
            throw_if_canceled();
        }
        for (const auto &[key, value] : request.settings) {
            try {
                config.set_deserialize_strict(key, value);
            } catch (const std::exception &error) {
                SinglePlateSliceResult refused = failure("invalid_setting_value", error.what(), WorkerErrorCategory::Validation);
                refused.setting = key;
                return refused;
            }
        }

        // A plate carries the placement its author chose, so only loose meshes
        // and plate-less archives are arranged.
        if (explicit_placement) {
            progress("arrangement", 20, "Keeping submitted placement");
        } else if (project.declares_plate) {
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
            // A plate may deliberately sink an object below the bed, so only
            // fully buried objects are lifted, matching the desktop importer.
            object->ensure_on_bed(project.declares_plate || explicit_placement);
            print.auto_assign_extruders(object);
        }
        if (explicit_placement)
            if (const SinglePlateSliceResult placed = check_placement(model, config); !placed.success)
                return placed;
        print.apply(model, config);
        const StringObjectException validation_error = print.validate();
        if (!validation_error.string.empty()) {
            SinglePlateSliceResult refused =
                failure("slice_validation_failed", validation_error.string, WorkerErrorCategory::Validation);
            refused.setting = validation_error.opt_key;
            return refused;
        }

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
        const bool wants_preview = !request.output_preview.empty();
        GCodeProcessorResult processed;
        print.export_gcode(artifact->temporary_path().string(), wants_preview ? &processed : nullptr, nullptr);
        if (!std::filesystem::is_regular_file(artifact->temporary_path()))
            return failure("gcode_export_failed", "G-code export did not produce a non-empty artifact.");
        const std::uintmax_t output_size = std::filesystem::file_size(artifact->temporary_path());
        if (request.max_output_bytes && output_size > *request.max_output_bytes)
            return resource_limit_failure(ResourceLimitExceeded::Output);
        if (output_size == 0)
            return failure("gcode_export_failed", "G-code export did not produce a non-empty artifact.");

        std::optional<ArtifactTransaction> preview;
        std::optional<ArtifactTransaction> preview_index;
        if (wants_preview) {
            throw_if_canceled();
            progress("export", 98, "Writing layer preview");
            preview = ArtifactTransaction::begin(job_root, request.output_preview, request.envelope.job_id, path_error);
            if (!preview)
                return failure(path_error.code, path_error.message, path_error.category);
            preview_index =
                ArtifactTransaction::begin(job_root, request.output_preview_index, request.envelope.job_id, path_error);
            if (!preview_index)
                return failure(path_error.code, path_error.message, path_error.category);
            std::string preview_error;
            if (!write_gcode_preview(processed, preview->temporary_path(), preview_index->temporary_path(), preview_error))
                return failure("preview_export_failed", preview_error);
        }

        throw_if_canceled();
        progress("finalize", 99, "Validating artifact");
        SinglePlateSliceResult succeeded {true, {}, {}, WorkerErrorCategory::Slicing};
        if (!artifact->commit(path_error))
            return failure(path_error.code, path_error.message, path_error.category);
        succeeded.artifacts.emplace_back("gcode", request.output_gcode);
        if (wants_preview) {
            // The G-code is already published, so a preview that fails to
            // publish withdraws it rather than leaving a half-reported job.
            if (!preview->commit(path_error) || !preview_index->commit(path_error)) {
                std::error_code ignored;
                for (const std::string *published : {&request.output_gcode, &request.output_preview})
                    std::filesystem::remove(job_root / *published, ignored);
                return failure(path_error.code, path_error.message, path_error.category);
            }
            succeeded.artifacts.emplace_back("preview", request.output_preview);
            succeeded.artifacts.emplace_back("preview_index", request.output_preview_index);
        }
        return succeeded;
    } catch (const CanceledException &) {
        if (const ResourceLimitExceeded limit = limit_exceeded.load(); limit != ResourceLimitExceeded::None)
            return resource_limit_failure(limit);
        return failure("job_canceled", "The slicing job was canceled.", WorkerErrorCategory::Cancellation);
    } catch (const std::exception &error) {
        return failure("slice_failed", error.what());
    }
}

SinglePlateSliceResult inspect_model(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                     const SinglePlateSliceCallbacks &callbacks)
{
    try {
        if (callbacks.progress)
            callbacks.progress({"input", 5, "Loading model"});
        Model model;
        ProjectPlate project;
        if (const SinglePlateSliceResult loaded = load_model_file(request, job_root, request.input_model, model, project);
            !loaded.success)
            return loaded;
        if (const SinglePlateSliceResult limited = check_triangle_limit(request, model); !limited.success)
            return limited;
        if (callbacks.cancellation_requested && callbacks.cancellation_requested())
            return failure("job_canceled", "The inspection was canceled.", WorkerErrorCategory::Cancellation);
        // A loose mesh declares no placement; the identity it is given here is
        // what the browser replaces when it centers the object on the bed.
        model.add_default_instances();

        if (callbacks.progress)
            callbacks.progress({"export", 60, "Exporting display mesh"});
        WorkerManifestError path_error;
        std::optional<ArtifactTransaction> mesh =
            ArtifactTransaction::begin(job_root, request.output_mesh, request.envelope.job_id, path_error);
        if (!mesh)
            return failure(path_error.code, path_error.message, path_error.category);
        std::optional<ArtifactTransaction> inspection =
            ArtifactTransaction::begin(job_root, request.output_inspection, request.envelope.job_id, path_error);
        if (!inspection)
            return failure(path_error.code, path_error.message, path_error.category);
        std::string export_error;
        const std::string model_format = lowercase_extension(request.input_model).substr(1);
        if (!write_model_inspection(model, project.declares_plate, model_format, mesh->temporary_path(),
                                    inspection->temporary_path(), export_error))
            return failure("mesh_export_failed", export_error, WorkerErrorCategory::Internal);
        if (request.max_output_bytes) {
            std::error_code ignored;
            if (std::filesystem::file_size(mesh->temporary_path(), ignored) > *request.max_output_bytes)
                return resource_limit_failure(ResourceLimitExceeded::Output);
        }

        if (callbacks.progress)
            callbacks.progress({"finalize", 99, "Validating artifact"});
        if (!mesh->commit(path_error))
            return failure(path_error.code, path_error.message, path_error.category);
        if (!inspection->commit(path_error)) {
            std::error_code ignored;
            std::filesystem::remove(job_root / request.output_mesh, ignored);
            return failure(path_error.code, path_error.message, path_error.category);
        }
        SinglePlateSliceResult succeeded {true, {}, {}, WorkerErrorCategory::Input};
        succeeded.artifacts.emplace_back("mesh", request.output_mesh);
        succeeded.artifacts.emplace_back("inspection", request.output_inspection);
        return succeeded;
    } catch (const std::exception &error) {
        return failure("inspection_failed", error.what(), WorkerErrorCategory::Input);
    }
}

} // namespace Slic3r::Web
