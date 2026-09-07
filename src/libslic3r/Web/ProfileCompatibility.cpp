#include "ProfileCompatibility.hpp"

#include "ArtifactTransaction.hpp"

#include "libslic3r/PlaceholderParser.hpp"
#include "libslic3r/PrintConfig.hpp"

#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace Slic3r::Web {
namespace {

// Bounded so one malformed request cannot make the evaluator load an unbounded
// number of profiles or run an unbounded number of expressions.
constexpr std::size_t MAX_PRINTERS = 512;
constexpr std::size_t MAX_CANDIDATES = 4096;
constexpr std::size_t MAX_CONDITION_CHARS = 4096;

bool fail(WorkerManifestError &error, const char *code, std::string message,
          WorkerErrorCategory category = WorkerErrorCategory::Request)
{
    error = {code, std::move(message), category};
    return false;
}

struct Printer
{
    std::string        id;
    DynamicPrintConfig config;
    // The name the condition sees through `printer_preset`, and the name a
    // `compatible_printers` list is matched against.
    std::string        name;
    DynamicConfig      context;
};

bool read_string(const nlohmann::json &object, const char *key, std::string &target)
{
    const auto value = object.find(key);
    if (value == object.end() || !value->is_string())
        return false;
    target = value->get<std::string>();
    return true;
}

bool load_printer(const nlohmann::json &described, const std::filesystem::path &root, Printer &printer,
                  WorkerManifestError &error)
{
    std::string profile_path;
    if (!described.is_object() || !read_string(described, "id", printer.id) ||
        !read_string(described, "name", printer.name) || !read_string(described, "profile", profile_path))
        return fail(error, "invalid_compatibility_printer",
                    "Every printer must declare a string id, name, and profile path.");

    std::filesystem::path resolved;
    if (!resolve_job_file(root, profile_path, WorkerErrorCategory::Profile, resolved, error))
        return false;

    ConfigSubstitutionContext          substitutions(ForwardCompatibilitySubstitutionRule::Enable);
    std::map<std::string, std::string> metadata;
    std::string                        reason;
    if (printer.config.load_from_json(resolved.string(), substitutions, true, metadata, reason) != 0)
        return fail(error, "profile_load_failed", reason.empty() ? "A printer profile could not be read." : reason,
                    WorkerErrorCategory::Profile);

    // A condition reads the selected printer through `printer_preset` and
    // `num_extruders`, which the desktop supplies the same way.
    printer.context.set_key_value("printer_preset", new ConfigOptionString(printer.name));
    if (const ConfigOption *nozzles = printer.config.option("nozzle_diameter"); nozzles != nullptr)
        printer.context.set_key_value(
            "num_extruders", new ConfigOptionInt(static_cast<int>(static_cast<const ConfigOptionFloats *>(nozzles)->values.size())));
    return true;
}

} // namespace

bool evaluate_profile_compatibility(std::string_view serialized, const std::filesystem::path &root,
                                    std::string &response, WorkerManifestError &error)
{
    const nlohmann::json request = nlohmann::json::parse(serialized.begin(), serialized.end(), nullptr, false);
    if (request.is_discarded() || !request.is_object())
        return fail(error, "invalid_compatibility_request", "The compatibility request must be a JSON object.");

    const auto version = request.find("catalog_version");
    if (version == request.end() || !version->is_number_integer() || version->get<int>() != COMPATIBILITY_REQUEST_VERSION)
        return fail(error, "unsupported_catalog_version",
                    "The compatibility request declares an unsupported catalog version.");

    const auto printers = request.find("printers");
    const auto candidates = request.find("candidates");
    if (printers == request.end() || !printers->is_array() || printers->size() > MAX_PRINTERS)
        return fail(error, "invalid_compatibility_request", "printers must be an array of at most 512 printers.");
    if (candidates == request.end() || !candidates->is_array() || candidates->size() > MAX_CANDIDATES)
        return fail(error, "invalid_compatibility_request", "candidates must be an array of at most 4096 profiles.");

    std::vector<Printer> loaded;
    loaded.reserve(printers->size());
    for (const nlohmann::json &described : *printers) {
        Printer printer;
        if (!load_printer(described, root, printer, error))
            return false;
        loaded.push_back(std::move(printer));
    }

    nlohmann::json compatibility = nlohmann::json::object();
    nlohmann::json unevaluated = nlohmann::json::array();
    for (const nlohmann::json &described : *candidates) {
        std::string candidate_id;
        std::string condition;
        if (!described.is_object() || !read_string(described, "id", candidate_id) ||
            !read_string(described, "condition", condition))
            return fail(error, "invalid_compatibility_candidate",
                        "Every candidate must declare a string id and condition.");
        if (condition.size() > MAX_CONDITION_CHARS)
            return fail(error, "invalid_compatibility_candidate",
                        "A compatible_printers_condition may not exceed 4096 characters.");

        nlohmann::json matched = nlohmann::json::array();
        for (const Printer &printer : loaded) {
            bool compatible = true;
            try {
                compatible = PlaceholderParser::evaluate_boolean_expression(condition, printer.config, &printer.context);
            } catch (const std::runtime_error &) {
                // The desktop treats an unparsable condition as "compatible with
                // everything"; the API is told which conditions did that so the
                // bundle can be fixed instead of the failure staying invisible.
                unevaluated.push_back({{"id", candidate_id}, {"printer", printer.id}});
                compatible = true;
            }
            if (compatible)
                matched.push_back(printer.id);
        }
        compatibility[candidate_id] = std::move(matched);
    }

    response = nlohmann::json{
        {"catalog_version", COMPATIBILITY_REQUEST_VERSION},
        {"compatibility", std::move(compatibility)},
        {"unevaluated", std::move(unevaluated)},
    }.dump();
    return true;
}

} // namespace Slic3r::Web
