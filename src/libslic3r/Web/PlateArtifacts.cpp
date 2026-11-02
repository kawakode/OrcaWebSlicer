#include "PlateArtifacts.hpp"
#include "ProjectExport.hpp"

#include "libslic3r/ExtrusionEntity.hpp"
#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "libslic3r/Model.hpp"

#include <array>
#include <cmath>
#include <cstring>
#include <fstream>
#include <set>
#include <vector>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

// Every record is written in host order, so a big-endian host would produce a
// file the browser misreads. No supported platform is big-endian today.
bool is_little_endian()
{
    const std::uint16_t probe = 1;
    unsigned char       first = 0;
    std::memcpy(&first, &probe, 1);
    return first == 1;
}

template<typename T> void write_value(std::ofstream &output, T value)
{
    output.write(reinterpret_cast<const char *>(&value), sizeof(T));
}

nlohmann::json matrix_json(const Transform3d &transform)
{
    nlohmann::json values = nlohmann::json::array();
    for (int row = 0; row < 4; ++row)
        for (int column = 0; column < 4; ++column)
            values.push_back(transform.matrix()(row, column));
    return values;
}

nlohmann::json point_json(const Vec3d &point) { return {point.x(), point.y(), point.z()}; }

bool finish(std::ofstream &output, const char *what, std::string &error)
{
    output.close();
    if (output)
        return true;
    error = std::string("Unable to write the complete ") + what + ".";
    return false;
}

} // namespace

bool write_model_inspection(const Model &model, bool declares_plate, const InspectedPlates &plates,
                            const std::string &model_format,
                            const std::filesystem::path &mesh, const std::filesystem::path &inspection,
                            std::string &error, const DynamicPrintConfig *project_config)
{
    if (!is_little_endian()) {
        error = "Mesh export requires a little-endian host.";
        return false;
    }
    std::ofstream output(mesh, std::ios::binary | std::ios::trunc);
    if (!output) {
        error = "Unable to create the mesh artifact.";
        return false;
    }
    output.write("OWMESH01", 8);
    write_value<std::uint32_t>(output, static_cast<std::uint32_t>(model.objects.size()));

    nlohmann::json objects = nlohmann::json::array();
    for (std::size_t index = 0; index < model.objects.size(); ++index) {
        const ModelObject         &object = *model.objects[index];
        const std::uint64_t        offset = static_cast<std::uint64_t>(output.tellp());
        const indexed_triangle_set its    = object.raw_indexed_triangle_set();
        write_value<std::uint32_t>(output, static_cast<std::uint32_t>(its.vertices.size()));
        write_value<std::uint32_t>(output, static_cast<std::uint32_t>(its.indices.size()));
        BoundingBoxf3 bounds;
        for (const stl_vertex &vertex : its.vertices) {
            output.write(reinterpret_cast<const char *>(vertex.data()), 3 * sizeof(float));
            bounds.merge(vertex.cast<double>());
        }
        for (const stl_triangle_vertex_indices &triangle : its.indices)
            for (int corner = 0; corner < 3; ++corner)
                write_value<std::uint32_t>(output, static_cast<std::uint32_t>(triangle[corner]));

        nlohmann::json instances = nlohmann::json::array();
        for (std::size_t instance = 0; instance < object.instances.size(); ++instance) {
            nlohmann::json described = {{"transform", matrix_json(object.instances[instance]->get_matrix())}};
            if (!plates.names.empty()) {
                const unsigned plate = index < plates.instance_plates.size() &&
                                               instance < plates.instance_plates[index].size()
                                           ? plates.instance_plates[index][instance]
                                           : 0;
                described["plate"] = plate == 0 ? nlohmann::json(nullptr) : nlohmann::json(plate);
            }
            instances.push_back(std::move(described));
        }
        objects.push_back({
            {"index", index},
            {"name", object.name},
            {"offset", offset},
            {"vertex_count", its.vertices.size()},
            {"triangle_count", its.indices.size()},
            {"bounding_box", {{"min", point_json(bounds.min)}, {"max", point_json(bounds.max)}}},
            {"instances", std::move(instances)},
        });
    }
    if (!finish(output, "mesh artifact", error))
        return false;

    std::ofstream index_output(inspection, std::ios::binary | std::ios::trunc);
    if (!index_output) {
        error = "Unable to create the inspection artifact.";
        return false;
    }
    nlohmann::json described_plates = nlohmann::json::array();
    for (std::size_t plate = 0; plate < plates.names.size(); ++plate)
        described_plates.push_back({{"index", plate + 1}, {"name", plates.names[plate]}});
    nlohmann::json described {
        {"format", "orca-web-mesh"},
        {"version", MESH_FORMAT_VERSION},
        {"model_format", model_format},
        {"declares_plate", declares_plate},
        {"plates", std::move(described_plates)},
        {"objects", std::move(objects)},
    };
    if (project_config != nullptr)
        if (nlohmann::json project = describe_project_selection(*project_config); !project.is_null())
            described["project"] = std::move(project);
    index_output << described.dump() << '\n';
    return finish(index_output, "inspection artifact", error);
}

bool write_gcode_preview(const GCodeProcessorResult &result, const std::filesystem::path &segments,
                         const std::filesystem::path &index, std::string &error)
{
    if (!is_little_endian()) {
        error = "Preview export requires a little-endian host.";
        return false;
    }
    std::ofstream output(segments, std::ios::binary | std::ios::trunc);
    if (!output) {
        error = "Unable to create the preview artifact.";
        return false;
    }

    struct Layer
    {
        unsigned      id;
        float         z;
        float         height;
        std::uint64_t offset;
        std::uint64_t count;
    };
    std::vector<Layer>    layers;
    std::set<unsigned>    tools;
    std::set<int>         roles;
    BoundingBoxf3         bounds;
    const Vec3f          *previous = nullptr;
    for (const auto &move : result.moves) {
        // A segment runs from the previous position to this one, whatever the
        // previous move was, so travels and retractions still move the pen.
        const Vec3f *from = previous;
        previous          = &move.position;
        if (from == nullptr || move.type != EMoveType::Extrude || move.width <= 0.f)
            continue;

        if (layers.empty() || layers.back().id != move.layer_id)
            layers.push_back({move.layer_id, move.position.z(), move.height,
                              static_cast<std::uint64_t>(output.tellp()), 0});
        Layer &layer = layers.back();
        layer.z      = std::max(layer.z, move.position.z());
        ++layer.count;

        const std::array<float, 8> values {from->x(), from->y(), from->z(), move.position.x(), move.position.y(),
                                           move.position.z(), move.width, move.height};
        output.write(reinterpret_cast<const char *>(values.data()), sizeof(values));
        write_value<std::uint8_t>(output, static_cast<std::uint8_t>(move.extrusion_role));
        write_value<std::uint8_t>(output, move.extruder_id);
        write_value<std::uint16_t>(output, 0);
        tools.insert(move.extruder_id);
        roles.insert(static_cast<int>(move.extrusion_role));
        bounds.merge(from->cast<double>());
        bounds.merge(move.position.cast<double>());
    }
    if (!finish(output, "preview artifact", error))
        return false;

    nlohmann::json role_table = nlohmann::json::array();
    for (int role = 0; role < static_cast<int>(erCount); ++role)
        role_table.push_back({{"id", role},
                              {"name", ExtrusionEntity::role_to_string(static_cast<ExtrusionRole>(role))},
                              {"used", roles.count(role) > 0}});
    nlohmann::json layer_table = nlohmann::json::array();
    for (std::size_t position = 0; position < layers.size(); ++position)
        layer_table.push_back({{"index", position},
                               {"z", layers[position].z},
                               {"height", layers[position].height},
                               {"offset", layers[position].offset},
                               {"count", layers[position].count}});

    std::ofstream index_output(index, std::ios::binary | std::ios::trunc);
    if (!index_output) {
        error = "Unable to create the preview index.";
        return false;
    }
    nlohmann::json document {
        {"format", "orca-web-preview"},
        {"version", PREVIEW_FORMAT_VERSION},
        {"segment_bytes", 36},
        {"roles", std::move(role_table)},
        {"tools", nlohmann::json(std::vector<unsigned>(tools.begin(), tools.end()))},
        {"layers", std::move(layer_table)},
    };
    if (!layers.empty())
        document["bounding_box"] = {{"min", point_json(bounds.min)}, {"max", point_json(bounds.max)}};
    index_output << document.dump() << '\n';
    return finish(index_output, "preview index", error);
}

} // namespace Slic3r::Web
