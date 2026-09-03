#include "SinglePlateSlice.hpp"

#include "ArtifactTransaction.hpp"

#include "libslic3r/Format/OBJ.hpp"
#include "libslic3r/Format/STL.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/ModelArrange.hpp"
#include "libslic3r/Print.hpp"

#include <algorithm>
#include <cctype>
#include <exception>
#include <filesystem>
#include <map>
#include <system_error>

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
    try {
        const auto progress = [&callbacks](const char *stage, unsigned percent, const char *message) {
            if (callbacks.progress)
                callbacks.progress({stage, percent, message});
        };

        progress("input", 2, "Loading model");
        std::filesystem::path input_path;
        WorkerManifestError path_error;
        if (!resolve_job_file(job_root, request.input_model, WorkerErrorCategory::Input, input_path, path_error))
            return failure(path_error.code, path_error.message, path_error.category);

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
        }
        for (const auto &[key, value] : request.settings)
            config.set_deserialize_strict(key, value);

        progress("arrangement", 20, "Arranging objects");
        ArrangeParams arrange_params(scaled(min_object_distance(config)));
        arrange_params.progressind = [](unsigned, std::string) {};
        arrange_objects(model, get_bed_shape(config), arrange_params);

        Print print;
        for (ModelObject *object : model.objects) {
            object->ensure_on_bed();
            print.auto_assign_extruders(object);
        }
        print.apply(model, config);
        const StringObjectException validation_error = print.validate();
        if (!validation_error.string.empty())
            return failure("slice_validation_failed", validation_error.string, WorkerErrorCategory::Validation);

        std::optional<ArtifactTransaction> artifact =
            ArtifactTransaction::begin(job_root, request.output_gcode, request.envelope.job_id, path_error);
        if (!artifact)
            return failure(path_error.code, path_error.message, path_error.category);

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
        progress("slicing", 25, "Slicing model");
        print.process();
        exporting = true;
        progress("export", 90, "Exporting G-code");
        print.export_gcode(artifact->temporary_path().string(), nullptr, nullptr);
        if (!std::filesystem::is_regular_file(artifact->temporary_path()) ||
            std::filesystem::file_size(artifact->temporary_path()) == 0)
            return failure("gcode_export_failed", "G-code export did not produce a non-empty artifact.");

        progress("finalize", 99, "Validating artifact");
        if (!artifact->commit(path_error))
            return failure(path_error.code, path_error.message, path_error.category);
        return {true, {}, {}, WorkerErrorCategory::Slicing};
    } catch (const std::exception &error) {
        return failure("slice_failed", error.what());
    }
}

} // namespace Slic3r::Web
