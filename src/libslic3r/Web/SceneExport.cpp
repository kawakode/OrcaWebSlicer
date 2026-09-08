#include "SceneExport.hpp"

#include "ArtifactTransaction.hpp"
#include "SinglePlateSlice.hpp"

#include "libslic3r/Model.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/TriangleMesh.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <fstream>
#include <limits>
#include <system_error>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

void add_error(SceneExportRequestValidation &result, const char *code, const char *message,
               WorkerErrorCategory category = WorkerErrorCategory::Request)
{
    result.errors.push_back({code, message, category});
}

SceneExportResult failure(std::string code, std::string message,
                          WorkerErrorCategory category = WorkerErrorCategory::Input)
{
    return {false, std::move(code), std::move(message), category};
}

// Mirrors the private predicate validate_worker_manifest uses in
// WorkerManifest.cpp. Duplicated rather than shared because that file is not
// this operation's to change, and the check is a few lines with no other
// natural shared home.
bool is_safe_job_id(const std::string &job_id)
{
    if (job_id.empty() || job_id.size() > 128)
        return false;
    return std::all_of(job_id.begin(), job_id.end(), [](unsigned char c) {
        const bool ascii_letter = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
        const bool ascii_digit  = c >= '0' && c <= '9';
        return ascii_letter || ascii_digit || c == '-' || c == '_' || c == '.';
    });
}

template<typename T>
void read_positive_limit(const nlohmann::json &limits, const char *key, std::optional<T> &target, const char *code,
                         SceneExportRequestValidation &result)
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

std::int32_t quantize(float millimetres)
{
    return static_cast<std::int32_t>(std::lround(static_cast<double>(millimetres) / SCENE_QUANTUM_MM));
}

void append(std::vector<char> &blob, const void *bytes, std::size_t size)
{
    const char *source = static_cast<const char *>(bytes);
    blob.insert(blob.end(), source, source + size);
}

template<typename T> void append_scalar(std::vector<char> &blob, T value)
{
    append(blob, &value, sizeof(value));
}

bool commit_file(const std::filesystem::path &job_root, const std::string &relative, const std::string &job_id,
                 const char *bytes, std::size_t size, WorkerManifestError &error)
{
    std::optional<ArtifactTransaction> artifact = ArtifactTransaction::begin(job_root, relative, job_id, error);
    if (!artifact)
        return false;
    std::ofstream output(artifact->temporary_path(), std::ios::binary | std::ios::trunc);
    if (output)
        output.write(bytes, static_cast<std::streamsize>(size));
    if (!output) {
        error = {"scene_write_failed", "The scene artifact could not be written.", WorkerErrorCategory::Internal};
        return false;
    }
    output.close();
    if (!output) {
        error = {"scene_write_failed", "The scene artifact could not be written.", WorkerErrorCategory::Internal};
        return false;
    }
    return artifact->commit(error);
}

} // namespace

WorkerManifestValidation validate_inspect_envelope(std::string_view serialized)
{
    WorkerManifestValidation result;
    const nlohmann::json envelope = nlohmann::json::parse(serialized.begin(), serialized.end(), nullptr, false);

    if (envelope.is_discarded()) {
        result.errors.push_back({"invalid_json", "Manifest is not valid JSON.", WorkerErrorCategory::Request});
        return result;
    }
    if (!envelope.is_object()) {
        result.errors.push_back({"invalid_envelope", "Manifest root must be a JSON object.", WorkerErrorCategory::Request});
        return result;
    }

    WorkerManifest manifest;

    const auto protocol_version = envelope.find("protocol_version");
    if (protocol_version == envelope.end() || !protocol_version->is_number_integer()) {
        result.errors.push_back({"invalid_protocol_version", "protocol_version must be an integer.",
                                 WorkerErrorCategory::Request});
    } else if (*protocol_version != WORKER_PROTOCOL_VERSION) {
        result.errors.push_back({"unsupported_protocol_version", "protocol_version is not supported.",
                                 WorkerErrorCategory::Request});
    }

    const auto job_id = envelope.find("job_id");
    if (job_id == envelope.end() || !job_id->is_string() || !is_safe_job_id(job_id->get_ref<const std::string &>())) {
        result.errors.push_back({"invalid_job_id", "job_id must contain 1-128 letters, digits, dots, dashes, or underscores.",
                                 WorkerErrorCategory::Request});
    } else {
        manifest.job_id = job_id->get<std::string>();
    }

    const auto operation = envelope.find("operation");
    if (operation == envelope.end() || !operation->is_object()) {
        result.errors.push_back({"invalid_operation", "operation must be an object.", WorkerErrorCategory::Request});
    } else {
        const auto name = operation->find("name");
        if (name == operation->end() || !name->is_string() || *name != "inspect") {
            result.errors.push_back({"unsupported_operation", "operation.name must be 'inspect'.",
                                     WorkerErrorCategory::Request});
        } else {
            manifest.operation = name->get<std::string>();
        }

        const auto version = operation->find("version");
        if (version == operation->end() || !version->is_number_integer()) {
            result.errors.push_back({"invalid_operation_version", "operation.version must be an integer.",
                                     WorkerErrorCategory::Request});
        } else if (*version != INSPECT_OPERATION_VERSION) {
            result.errors.push_back({"unsupported_operation_version", "The inspect operation version is not supported.",
                                     WorkerErrorCategory::Request});
        } else {
            manifest.operation_version = version->get<int>();
        }

        const auto payload = operation->find("payload");
        if (payload == operation->end() || !payload->is_object())
            result.errors.push_back({"invalid_operation_payload", "operation.payload must be an object.",
                                     WorkerErrorCategory::Request});
    }

    if (result.errors.empty())
        result.manifest = std::move(manifest);
    return result;
}

SceneExportRequestValidation validate_scene_export_request(std::string_view serialized)
{
    SceneExportRequestValidation result;
    const WorkerManifestValidation envelope_validation = validate_inspect_envelope(serialized);
    if (!envelope_validation.is_valid()) {
        result.errors = envelope_validation.errors;
        return result;
    }

    const nlohmann::json manifest = nlohmann::json::parse(serialized.begin(), serialized.end(), nullptr, false);
    const nlohmann::json &payload = manifest.at("operation").at("payload");
    SceneExportRequest request;
    request.envelope = *envelope_validation.manifest;

    const auto input_model = payload.find("input_model");
    if (input_model == payload.end() || !input_model->is_string() ||
        !is_safe_relative_path(input_model->get_ref<const std::string &>())) {
        add_error(result, "invalid_input_model", "input_model must be a safe relative path.", WorkerErrorCategory::Input);
    } else if (const std::string extension = lowercase_extension(input_model->get_ref<const std::string &>());
               extension != ".stl" && extension != ".obj" && extension != ".3mf") {
        add_error(result, "unsupported_input_format", "input_model must be an STL, OBJ, or 3MF file.",
                  WorkerErrorCategory::Input);
    } else {
        request.input_model = input_model->get<std::string>();
    }

    const auto output_scene = payload.find("output_scene");
    if (output_scene == payload.end() || !output_scene->is_string() ||
        !is_safe_relative_path(output_scene->get_ref<const std::string &>())) {
        add_error(result, "invalid_output_scene", "output_scene must be a safe relative path.");
    } else if (lowercase_extension(output_scene->get_ref<const std::string &>()) != ".json") {
        add_error(result, "unsupported_scene_format", "output_scene must use the .json extension.");
    } else {
        request.output_scene = output_scene->get<std::string>();
    }

    const auto profiles = payload.find("profiles");
    if (profiles != payload.end()) {
        if (!profiles->is_object()) {
            add_error(result, "invalid_profiles", "profiles must be an object.");
        } else if (const auto machine = profiles->find("machine"); machine != profiles->end()) {
            if (!machine->is_string() || !is_safe_relative_path(machine->get_ref<const std::string &>()) ||
                lowercase_extension(machine->get_ref<const std::string &>()) != ".json") {
                add_error(result, "invalid_profile_path", "machine must be a safe relative JSON path.",
                          WorkerErrorCategory::Profile);
            } else {
                request.machine_profile = machine->get<std::string>();
            }
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

    const auto limits = payload.find("limits");
    if (limits != payload.end()) {
        if (!limits->is_object()) {
            add_error(result, "invalid_limits", "limits must be an object.");
        } else {
            read_positive_limit(*limits, "max_input_bytes", request.max_input_bytes, "invalid_input_limit", result);
            read_positive_limit(*limits, "max_triangles", request.max_triangles, "invalid_triangle_limit", result);
            read_positive_limit(*limits, "max_scene_bytes", request.max_scene_bytes, "invalid_scene_limit", result);
        }
    }

    if (result.errors.empty())
        result.request = std::move(request);
    return result;
}

SceneExportResult export_scene(const SceneExportRequest &request, const std::filesystem::path &job_root,
                               const SceneExportCallbacks &callbacks)
{
    try {
        const auto progress = [&callbacks](const char *stage, unsigned percent, const char *message) {
            if (callbacks.progress)
                callbacks.progress({stage, percent, message});
        };
        const auto canceled = [&callbacks]() {
            return callbacks.cancellation_requested && callbacks.cancellation_requested();
        };
        const SceneExportResult canceled_result =
            failure("job_canceled", "The inspect job was canceled.", WorkerErrorCategory::Cancellation);

        progress("input", 2, "Loading model");
        std::filesystem::path input_path;
        WorkerManifestError path_error;
        if (!resolve_job_file(job_root, request.input_model, WorkerErrorCategory::Input, input_path, path_error))
            return failure(path_error.code, path_error.message, path_error.category);

        std::error_code filesystem_error;
        const std::uintmax_t input_size = std::filesystem::file_size(input_path, filesystem_error);
        if (filesystem_error)
            return failure("input_size_unavailable", "Unable to determine the input model size.",
                          WorkerErrorCategory::Input);
        if (request.max_input_bytes && input_size > *request.max_input_bytes)
            return failure("input_size_limit_exceeded", "The input model exceeds the configured size limit.",
                          WorkerErrorCategory::ResourceLimit);

        // inspect never slices, so it does not need the project's embedded
        // printer configuration: only its geometry and (for a plate) whether a
        // real placement was declared, which it ignores because it reports
        // placement exactly as imported either way.
        const std::string extension = lowercase_extension(request.input_model);
        Model model;
        bool declares_plate = false;
        if (!import_single_plate_model(input_path, extension, request.plate_index, std::nullopt, model,
                                       declares_plate, nullptr, path_error))
            return failure(path_error.code, path_error.message, path_error.category);

        std::uintmax_t triangle_count = 0;
        if (!count_model_triangles(model, request.max_triangles, triangle_count, path_error))
            return failure(path_error.code, path_error.message, path_error.category);

        if (canceled())
            return canceled_result;
        progress("input", 30, "Model loaded");

        DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
        if (!request.machine_profile.empty()) {
            progress("configuration", 35, "Loading machine profile");
            if (!apply_profile_file(job_root, request.machine_profile, WorkerErrorCategory::Profile, config,
                                    path_error))
                return failure(path_error.code, path_error.message, path_error.category);
        }

        nlohmann::json bed_shape_json = nlohmann::json::array();
        for (const Point &point : get_bed_shape(config))
            bed_shape_json.push_back({unscale<double>(point.x()), unscale<double>(point.y())});

        if (canceled())
            return canceled_result;
        progress("export", 50, "Building scene");

        std::vector<char> data;
        nlohmann::json objects_json = nlohmann::json::array();
        for (std::size_t index = 0; index < model.objects.size(); ++index) {
            const ModelObject *object = model.objects[index];
            const TriangleMesh mesh = object->raw_mesh();
            const indexed_triangle_set &its = mesh.its;
            const std::uintmax_t object_triangle_count = its.indices.size();
            const std::uintmax_t vertex_count = object_triangle_count * 3;
            const std::uintmax_t length = vertex_count * 12; // 3 x int32 per vertex

            if (request.max_scene_bytes && static_cast<std::uintmax_t>(data.size()) + length > *request.max_scene_bytes)
                return failure("scene_size_limit_exceeded", "The exported scene exceeds the configured size limit.",
                              WorkerErrorCategory::ResourceLimit);

            Vec3f minimum = Vec3f::Constant(std::numeric_limits<float>::max());
            Vec3f maximum = Vec3f::Constant(std::numeric_limits<float>::lowest());
            std::vector<char> object_blob;
            object_blob.reserve(length);
            // A triangle soup, not an indexed mesh: every triangle contributes
            // its own three vertex positions, so the browser groups the blob by
            // three and derives a flat normal per triangle without an index
            // buffer of its own.
            for (const Vec3i32 &triangle : its.indices) {
                for (int corner = 0; corner < 3; ++corner) {
                    const Vec3f &vertex = its.vertices[triangle[corner]];
                    minimum = minimum.cwiseMin(vertex);
                    maximum = maximum.cwiseMax(vertex);
                    append_scalar<std::int32_t>(object_blob, quantize(vertex.x()));
                    append_scalar<std::int32_t>(object_blob, quantize(vertex.y()));
                    append_scalar<std::int32_t>(object_blob, quantize(vertex.z()));
                }
            }
            if (object_triangle_count == 0)
                minimum = maximum = Vec3f::Zero();

            // The object's own placement, as imported: raw_mesh() already bakes
            // in every volume's own transform, so only the instance transform
            // remains, and inspect never arranges or sinks it.
            Transform3d matrix = Transform3d::Identity();
            if (!object->instances.empty())
                matrix = object->instances.front()->get_matrix();
            std::array<double, 16> matrix_data {};
            Eigen::Map<Eigen::Matrix<double, 4, 4>>(matrix_data.data()) = matrix.matrix();
            nlohmann::json transform_json = nlohmann::json::array();
            for (double value : matrix_data)
                transform_json.push_back(value);

            objects_json.push_back({
                {"index", index},
                {"name", object->name},
                {"triangle_count", object_triangle_count},
                {"bounding_box", {{"min", {minimum.x(), minimum.y(), minimum.z()}},
                                  {"max", {maximum.x(), maximum.y(), maximum.z()}}}},
                {"transform", transform_json},
                {"offset", data.size()},
                {"length", length},
                {"vertex_count", vertex_count},
            });
            data.insert(data.end(), object_blob.begin(), object_blob.end());

            if (canceled())
                return canceled_result;
        }

        progress("finalize", 90, "Writing scene");
        const std::string data_path =
            std::filesystem::path(request.output_scene).replace_extension(".bin").generic_string();
        const std::string scene_document = nlohmann::json{
            {"scene_version", SCENE_VERSION},
            {"units", "mm"},
            {"quantum_mm", SCENE_QUANTUM_MM},
            {"bed", {{"shape", bed_shape_json}, {"printable_height", config.opt_float("printable_height")}}},
            {"data", data_path},
            {"data_bytes", data.size()},
            {"objects", objects_json},
        }.dump();

        WorkerManifestError write_error;
        if (!commit_file(job_root, data_path, request.envelope.job_id, data.data(), data.size(), write_error))
            return failure(write_error.code, write_error.message, write_error.category);
        if (!commit_file(job_root, request.output_scene, request.envelope.job_id, scene_document.data(),
                         scene_document.size(), write_error))
            return failure(write_error.code, write_error.message, write_error.category);

        SceneExportResult published {true, {}, {}, WorkerErrorCategory::Input};
        published.artifacts.push_back({"scene", request.output_scene});
        published.artifacts.push_back({"scene_data", data_path});
        return published;
    } catch (const std::exception &error) {
        return failure("inspect_failed", error.what(), WorkerErrorCategory::Internal);
    }
}

} // namespace Slic3r::Web
