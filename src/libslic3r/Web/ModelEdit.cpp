#include "ModelEdit.hpp"

#include "ArtifactTransaction.hpp"

#include "libslic3r/CutUtils.hpp"
#include "libslic3r/Geometry.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/TriangleMesh.hpp"

#include <admesh/stl.h>

#include <algorithm>
#include <cmath>
#include <fstream>
#include <limits>
#include <optional>
#include <system_error>
#include <vector>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

SinglePlateSliceResult failure(std::string code, std::string message,
                               WorkerErrorCategory category = WorkerErrorCategory::Slicing)
{
    return {false, std::move(code), std::move(message), category};
}

Transform3d to_transform(const std::array<double, 16> &values)
{
    Transform3d transform = Transform3d::Identity();
    transform.matrix() = Eigen::Map<const Eigen::Matrix<double, 4, 4, Eigen::RowMajor>>(values.data());
    return transform;
}

// The printable parts of one instance, merged into one mesh in plate
// coordinates. Modifiers and negative volumes do not survive as geometry.
indexed_triangle_set placed_mesh(const ModelObject &object, const ModelInstance &instance)
{
    indexed_triangle_set merged;
    for (const ModelVolume *volume : object.volumes) {
        if (!volume->is_model_part())
            continue;
        indexed_triangle_set its = volume->mesh().its;
        // A mirroring placement flips the winding back, so normals stay outward.
        its_transform(its, instance.get_matrix() * volume->get_matrix(), true);
        its_merge(merged, its);
    }
    return merged;
}

struct RepairReport
{
    std::size_t open_edges_before {0};
    std::size_t open_edges_after {0};
    int         merged_vertices {0};
    int         degenerate_facets {0};
    int         edges_fixed {0};
    int         facets_removed {0};
    int         facets_added {0};
    int         facets_reversed {0};
    int         backwards_edges {0};

    nlohmann::json describe() const
    {
        return {{"open_edges_before", open_edges_before}, {"open_edges_after", open_edges_after},
                {"merged_vertices", merged_vertices},     {"degenerate_facets", degenerate_facets},
                {"edges_fixed", edges_fixed},             {"facets_removed", facets_removed},
                {"facets_added", facets_added},           {"facets_reversed", facets_reversed},
                {"backwards_edges", backwards_edges}};
    }
};

// Welds coincident vertices and drops degenerate faces, then runs admesh's full
// repair: nearby edges are joined, unconnected facets removed, holes filled,
// and normals made consistent and outward.
indexed_triangle_set repair_mesh(indexed_triangle_set its, RepairReport &report)
{
    report.open_edges_before += its_num_open_edges(its);
    report.merged_vertices += its_merge_vertices(its);
    report.degenerate_facets += its_remove_degenerate_faces(its);
    its_compactify_vertices(its);
    if (its.indices.empty())
        return its;

    stl_file stl;
    stl.stats.type                = inmemory;
    stl.stats.number_of_facets    = static_cast<uint32_t>(its.indices.size());
    stl.stats.original_num_facets = stl.stats.number_of_facets;
    stl_allocate(&stl);
    bool first = true;
    for (std::size_t index = 0; index < its.indices.size(); ++index) {
        stl_facet facet;
        for (int corner = 0; corner < 3; ++corner)
            facet.vertex[corner] = its.vertices[its.indices[index][corner]];
        stl_calculate_normal(facet.normal, &facet);
        stl_normalize_vector(facet.normal);
        facet.extra[0] = facet.extra[1] = 0;
        stl.facet_start[index] = facet;
        stl_facet_stats(&stl, facet, first);
    }
    stl.stats.size              = stl.stats.max - stl.stats.min;
    stl.stats.bounding_diameter = stl.stats.size.norm();

    // fixall, exact, nearby (2 iterations from the shortest edge), remove
    // unconnected, fill holes, normal directions, and normal values.
    stl_repair(&stl, true, true, false, 0.f, false, 0.f, true, 2, true, true, true, true, false, false);
    report.edges_fixed += stl.stats.edges_fixed;
    report.facets_removed += stl.stats.facets_removed;
    report.facets_added += stl.stats.facets_added;
    report.facets_reversed += stl.stats.facets_reversed;
    report.backwards_edges += stl.stats.backwards_edges;

    indexed_triangle_set repaired;
    stl_generate_shared_vertices(&stl, repaired);
    report.open_edges_after += its_num_open_edges(repaired);
    return repaired;
}

struct Part
{
    std::string          side;
    std::string          name;
    indexed_triangle_set mesh;
};

SinglePlateSliceResult cut_object(const ModelEdit &edit, const ModelObject &object, std::vector<Part> &parts)
{
    const Vec3d point(edit.plane_point[0], edit.plane_point[1], edit.plane_point[2]);
    const Vec3d normal = Vec3d(edit.plane_normal[0], edit.plane_normal[1], edit.plane_normal[2]).normalized();

    // The desktop disables a cut whose plane does not cross the object.
    const indexed_triangle_set whole = placed_mesh(object, *object.instances.front());
    double nearest = std::numeric_limits<double>::max();
    double farthest = std::numeric_limits<double>::lowest();
    for (const stl_vertex &vertex : whole.vertices) {
        const double distance = normal.dot(vertex.cast<double>() - point);
        nearest = std::min(nearest, distance);
        farthest = std::max(farthest, distance);
    }
    if (whole.vertices.empty() || nearest >= -EPSILON || farthest <= EPSILON)
        return failure("cut_plane_misses_object", "The cut plane does not cross the object.", WorkerErrorCategory::Validation);

    // `Cut` expresses its plane relative to the instance offset, rotated so
    // that its Z axis is the plane normal.
    const Transform3d cut_matrix =
        Geometry::translation_transform(point - object.instances.front()->get_offset()) *
        Transform3d(Eigen::Quaterniond::FromTwoVectors(Vec3d::UnitZ(), normal));
    ModelObjectCutAttributes attributes;
    if (edit.keep_upper)
        attributes = attributes | ModelObjectCutAttribute::KeepUpper;
    if (edit.keep_lower)
        attributes = attributes | ModelObjectCutAttribute::KeepLower;
    if (edit.flip_upper)
        attributes = attributes | ModelObjectCutAttribute::FlipUpper;
    if (edit.place_on_cut)
        attributes = attributes | ModelObjectCutAttribute::PlaceOnCutUpper | ModelObjectCutAttribute::PlaceOnCutLower;

    std::vector<std::string> sides;
    if (edit.keep_upper)
        sides.emplace_back("upper");
    if (edit.keep_lower)
        sides.emplace_back("lower");
    Cut                    cut(&object, 0, cut_matrix, attributes);
    const ModelObjectPtrs &cut_objects = cut.perform_with_plane();
    if (cut_objects.size() != sides.size())
        return failure("cut_failed", "The cut did not produce the requested parts.");
    for (std::size_t index = 0; index < cut_objects.size(); ++index) {
        const ModelObject &cut_object = *cut_objects[index];
        parts.push_back({sides[index], object.name + " (" + sides[index] + ")",
                         placed_mesh(cut_object, *cut_object.instances.front())});
    }
    return {true, {}, {}, WorkerErrorCategory::Slicing};
}

nlohmann::json point_json(const Vec3d &point) { return {point.x(), point.y(), point.z()}; }

} // namespace

SinglePlateSliceResult edit_model(const SinglePlateSliceRequest &request, const std::filesystem::path &job_root,
                                  const SinglePlateSliceCallbacks &callbacks)
{
    const auto progress = [&callbacks](const char *stage, unsigned percent, const char *message) {
        if (callbacks.progress)
            callbacks.progress({stage, percent, message});
    };
    const auto canceled = [&callbacks]() { return callbacks.cancellation_requested && callbacks.cancellation_requested(); };
    try {
        progress("input", 5, "Loading model");
        Model model;
        if (const SinglePlateSliceResult loaded = load_input_model(request, job_root, model); !loaded.success)
            return loaded;
        const ModelEdit &edit = request.edit;
        if (edit.object_index >= model.objects.size())
            return failure("unknown_model_object", "An object index does not exist in its model file.",
                           WorkerErrorCategory::Input);
        if (canceled())
            return failure("job_canceled", "The edit was canceled.", WorkerErrorCategory::Cancellation);

        // The object is edited exactly where the plater placed it.
        ModelObject &object = *model.objects[edit.object_index];
        object.clear_instances();
        object.add_instance()->set_transformation(Geometry::Transformation(to_transform(edit.transform)));

        std::vector<Part> parts;
        nlohmann::json    report {{"format", "orca-web-edit"}, {"version", EDIT_REPORT_FORMAT_VERSION}};
        if (edit.tool == ModelEdit::Tool::Cut) {
            progress("edit", 20, "Cutting object");
            report["tool"] = "cut";
            if (const SinglePlateSliceResult cut = cut_object(edit, object, parts); !cut.success)
                return cut;
        } else {
            progress("edit", 20, "Repairing mesh");
            report["tool"] = "repair";
            RepairReport repair;
            indexed_triangle_set repaired;
            // Each part is its own shell, so each is repaired on its own.
            for (const ModelVolume *volume : object.volumes) {
                if (!volume->is_model_part())
                    continue;
                indexed_triangle_set its = volume->mesh().its;
                its_transform(its, object.instances.front()->get_matrix() * volume->get_matrix(), true);
                its_merge(repaired, repair_mesh(std::move(its), repair));
            }
            report["repair"] = repair.describe();
            parts.push_back({"whole", object.name, std::move(repaired)});
        }
        if (canceled())
            return failure("job_canceled", "The edit was canceled.", WorkerErrorCategory::Cancellation);
        if (parts.size() > request.output_models.size())
            return failure("too_many_parts", "The edit produced more parts than the request has outputs for.",
                           WorkerErrorCategory::Internal);
        if (std::any_of(parts.begin(), parts.end(), [](const Part &part) { return part.mesh.indices.empty(); }))
            return failure("empty_edit_result", "The edit left a part with no printable geometry.",
                           WorkerErrorCategory::Validation);

        progress("export", 70, "Writing models");
        WorkerManifestError                path_error;
        std::vector<ArtifactTransaction>   transactions;
        std::uintmax_t                     written = 0;
        nlohmann::json                     described = nlohmann::json::array();
        for (std::size_t index = 0; index < parts.size(); ++index) {
            const Part &part = parts[index];
            std::optional<ArtifactTransaction> model_file =
                ArtifactTransaction::begin(job_root, request.output_models[index], request.envelope.job_id, path_error);
            if (!model_file)
                return failure(path_error.code, path_error.message, path_error.category);
            if (!its_write_stl_binary(model_file->temporary_path().string().c_str(), part.name.c_str(), part.mesh))
                return failure("model_export_failed", "Unable to write an edited model.", WorkerErrorCategory::Internal);
            std::error_code ignored;
            written += std::filesystem::file_size(model_file->temporary_path(), ignored);
            const BoundingBoxf3 bounds = bounding_box(part.mesh);
            described.push_back({{"side", part.side},
                                 {"name", part.name},
                                 {"model", request.output_models[index]},
                                 {"triangle_count", part.mesh.indices.size()},
                                 {"bounding_box",
                                  {{"min", point_json(bounds.min)}, {"max", point_json(bounds.max)}}}});
            transactions.push_back(std::move(*model_file));
        }
        if (request.max_output_bytes && written > *request.max_output_bytes)
            return failure("output_size_limit_exceeded", "The edited models exceed the configured size limit.",
                           WorkerErrorCategory::ResourceLimit);
        report["parts"] = std::move(described);

        std::optional<ArtifactTransaction> report_file =
            ArtifactTransaction::begin(job_root, request.output_report, request.envelope.job_id, path_error);
        if (!report_file)
            return failure(path_error.code, path_error.message, path_error.category);
        {
            std::ofstream output(report_file->temporary_path(), std::ios::binary | std::ios::trunc);
            output << report.dump() << '\n';
            output.close();
            if (!output)
                return failure("report_export_failed", "Unable to write the edit report.", WorkerErrorCategory::Internal);
        }

        if (canceled())
            return failure("job_canceled", "The edit was canceled.", WorkerErrorCategory::Cancellation);
        progress("finalize", 99, "Validating artifacts");
        SinglePlateSliceResult succeeded {true, {}, {}, WorkerErrorCategory::Slicing};
        // Everything is withdrawn if any one artifact fails to publish.
        const auto withdraw = [&job_root, &succeeded]() {
            std::error_code ignored;
            for (const auto &[kind, path] : succeeded.artifacts)
                std::filesystem::remove(job_root / path, ignored);
        };
        for (std::size_t index = 0; index < transactions.size(); ++index) {
            if (!transactions[index].commit(path_error)) {
                withdraw();
                return failure(path_error.code, path_error.message, path_error.category);
            }
            succeeded.artifacts.emplace_back("model", request.output_models[index]);
        }
        if (!report_file->commit(path_error)) {
            withdraw();
            return failure(path_error.code, path_error.message, path_error.category);
        }
        succeeded.artifacts.emplace_back("edit_report", request.output_report);
        return succeeded;
    } catch (const std::exception &error) {
        return failure("edit_failed", error.what());
    }
}

} // namespace Slic3r::Web
