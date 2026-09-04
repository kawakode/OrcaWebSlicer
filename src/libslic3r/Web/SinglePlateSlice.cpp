#include "SinglePlateSlice.hpp"

#include "ArtifactTransaction.hpp"

#include "libslic3r/Format/OBJ.hpp"
#include "libslic3r/Format/STL.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/ModelArrange.hpp"
#include "libslic3r/Print.hpp"

#include <algorithm>
#include <atomic>
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

    const auto input_model = payload.find("input_model");
    if (input_model == payload.end() || !input_model->is_string() || !is_safe_relative_path(input_model->get_ref<const std::string &>())) {
        add_error(result, "invalid_input_model", "input_model must be a safe relative path.", WorkerErrorCategory::Input);
    } else if (const std::string extension = lowercase_extension(input_model->get_ref<const std::string &>());
               extension != ".stl" && extension != ".obj") {
        add_error(result, "unsupported_input_format", "input_model must be an STL or OBJ file.", WorkerErrorCategory::Input);
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

        Model model;
        bool loaded = false;
        if (lowercase_extension(request.input_model) == ".stl") {
            loaded = load_stl(input_path.string().c_str(), &model);
        } else {
            ObjInfo obj_info;
            std::string message;
            loaded = load_obj(input_path.string().c_str(), &model, obj_info, message);
        }
        if (!loaded || model.objects.empty())
            return failure("model_load_failed", "The input model could not be loaded.", WorkerErrorCategory::Input);

        std::uintmax_t triangle_count = 0;
        for (const ModelObject *object : model.objects) {
            const std::uintmax_t object_triangles = object->facets_count();
            if (request.max_triangles && (triangle_count > *request.max_triangles ||
                object_triangles > *request.max_triangles - triangle_count))
                return failure("triangle_limit_exceeded", "The input model exceeds the configured triangle limit.",
                               WorkerErrorCategory::ResourceLimit);
            triangle_count += object_triangles;
        }

        throw_if_canceled();
        progress("input", 10, "Model loaded");
        model.add_default_instances();
        DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
        progress("configuration", 12, "Loading profiles");
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
        for (const auto &[key, value] : request.settings)
            config.set_deserialize_strict(key, value);

        progress("arrangement", 20, "Arranging objects");
        ArrangeParams arrange_params(scaled(min_object_distance(config)));
        arrange_params.progressind = [](unsigned, std::string) {};
        arrange_objects(model, get_bed_shape(config), arrange_params);
        throw_if_canceled();

        std::optional<ArtifactTransaction> artifact =
            ArtifactTransaction::begin(job_root, request.output_gcode, request.envelope.job_id, path_error);
        if (!artifact)
            return failure(path_error.code, path_error.message, path_error.category);

        Print print;
        PrintCancellationMonitor cancellation_monitor(print, request, callbacks, artifact->temporary_path(), started,
                                                       limit_exceeded);
        for (ModelObject *object : model.objects) {
            object->ensure_on_bed();
            print.auto_assign_extruders(object);
        }
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
        print.export_gcode(artifact->temporary_path().string(), nullptr, nullptr);
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
        return {true, {}, {}, WorkerErrorCategory::Slicing};
    } catch (const CanceledException &) {
        if (const ResourceLimitExceeded limit = limit_exceeded.load(); limit != ResourceLimitExceeded::None)
            return resource_limit_failure(limit);
        return failure("job_canceled", "The slicing job was canceled.", WorkerErrorCategory::Cancellation);
    } catch (const std::exception &error) {
        return failure("slice_failed", error.what());
    }
}

} // namespace Slic3r::Web
